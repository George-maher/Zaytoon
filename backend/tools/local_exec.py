"""Structured local execution: one command tool and one code tool.

``system_command`` and ``run_code`` (in :mod:`tools.terminal`) answer "did it
work?" with a rendered string. That is enough for a chat transcript and not
enough for anything that has to *decide* from the result: an agent that just ran
a test suite needs the exit code, the two streams separately, how long it took
and whether the process was killed — not a paragraph that happens to contain
them somewhere.

This module adds the structured counterpart:

* :class:`ExecuteLocalCommand` — run one command, get ``exit_code`` /
  ``stdout`` / ``stderr`` / ``duration_ms`` / ``timed_out`` / ``truncated``.
* :class:`ExecuteCode` — the same contract for a source snippet.

Both reuse :func:`run_argv`, so there is one place that spawns a process, one
place that enforces the deadline, and one result shape.

**What this deliberately does not add: a shell mode.** ``tools.sandbox``'s
unconditional checks refuse separators (``&``, ``|``, ``;``, ``>``, backticks,
``$(...)``), metacharacters (quotes, globs, ``~``, ``^``, …) and ``%VAR%`` /
``!VAR!`` expansion *before* the approval gate, and every process is started
with ``create_subprocess_exec`` — an argument vector, never a string handed to
an interpreter. Under those constraints ``cmd /c <command>`` and
``<command>`` as argv are the same capability, so a ``shell`` flag would buy
nothing and would be a second thing to get wrong. The interpreter is still used
for one narrow purpose — dispatching shell *builtins* such as ``echo`` and
``dir``, which have no standalone executable — and that path receives an
already-validated argv (:func:`tools.terminal._build_argv`), so there is no
syntax left for it to interpret.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from common.reporting import safe_error
from tools import sandbox
from tools.base import BaseTool, ToolResult, ToolValidationError, _prop
from tools.sandbox import (
    DENIED_CODE_LANGUAGES,
    SecurityError,
    resolve_workspace,
    split_command,
)
from tools.terminal import (
    CODE_TIMEOUT_S,
    COMMAND_TIMEOUT_S,
    MAX_OUTPUT_CHARS,
    RUNNERS,
    _build_argv,
    _kill,
)

logger = logging.getLogger("Zaytoon.Tools.LocalExec")

#: Bounds on the caller-supplied ``timeout``. A tool call that could run forever
#: would wedge the agent loop, so the ceiling is checked in ``validate`` — before
#: the approval gate — rather than trusted by :func:`run_argv`.
MIN_TIMEOUT_S = 1
MAX_TIMEOUT_S = 120

#: How long a killed process is given to actually die and to close its pipes.
#: Short enough that a wedged child cannot stall the turn, long enough for the
#: OS to reap it.
KILL_GRACE_S = 2.0


def _clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Truncate to ``limit`` and say so, so nobody reads a cut as a result."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text)} chars total]"


def check_timeout(value: Any, default: float = COMMAND_TIMEOUT_S) -> float:
    """Validate a caller-supplied timeout. Raises :class:`ToolValidationError`.

    A ``bool`` is refused even though ``True == 1`` in Python: it is almost
    certainly a type mistake, and silently turning it into a one-second deadline
    would look like a bug in the timeout logic rather than in the caller.
    """
    if value is None or value == "":
        return float(default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolValidationError(
            f"'timeout' must be a number between {MIN_TIMEOUT_S} and {MAX_TIMEOUT_S}"
        )
    seconds = float(value)
    if not (MIN_TIMEOUT_S <= seconds <= MAX_TIMEOUT_S):
        raise ToolValidationError(
            f"'timeout' must be between {MIN_TIMEOUT_S} and {MAX_TIMEOUT_S} "
            f"seconds, got {seconds:g}"
        )
    return seconds


def _task_text(task: "asyncio.Task") -> bytes:
    """Bytes read by ``task``, or ``b""`` if it did not finish cleanly."""
    if not task.done() or task.cancelled():
        return b""
    exc = task.exception()
    if exc is not None:
        return b""
    result = task.result()
    return result if isinstance(result, (bytes, bytearray)) else b""


@dataclass
class ExecOutcome:
    """What one local process did, recorded uniformly.

    This is the contract the two tools exist to deliver. ``exit_code`` is
    ``None`` only when the process never produced one — i.e. it was killed at
    the deadline — so ``None`` and ``0`` can never be confused.
    """

    argv: List[str]
    exit_code: Optional[int]
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    truncated: bool
    timeout_s: float
    cwd: str = ""

    @property
    def succeeded(self) -> bool:
        return (not self.timed_out) and self.exit_code == 0

    @property
    def head(self) -> str:
        """One-line verdict: the first thing a reader should see."""
        if self.timed_out:
            return f"TIMED OUT after {self.duration_ms}ms (limit {self.timeout_s:g}s)"
        return f"exit={self.exit_code} in {self.duration_ms}ms"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "truncated": self.truncated,
            "timeout_s": self.timeout_s,
            "cwd": self.cwd,
            "success": self.succeeded,
        }

    def render(self) -> str:
        """Human/model-readable rendering: verdict line plus captured output.

        Both streams are kept, and ``stderr`` is labelled — a process that
        writes warnings to stderr while succeeding is common enough that
        silently dropping it would hide real problems.
        """
        out = self.stdout.rstrip("\n")
        err = self.stderr.rstrip("\n")
        if out and err:
            return f"{self.head}\n{out}\n--- stderr ---\n{err}"
        if out or err:
            return f"{self.head}\n{out or err}"
        return f"{self.head}\n(no output)"


async def run_argv(
    argv: List[str],
    *,
    cwd: Optional[str] = None,
    timeout: float = COMMAND_TIMEOUT_S,
) -> ExecOutcome:
    """Run one process to completion — or to the deadline — and record it.

    The streams are read with two independent tasks rather than
    ``Process.communicate`` so that a timeout can still return **partial**
    output: ``asyncio.wait`` does not cancel the readers, so killing the child
    closes the pipes, the reads finish, and whatever the process managed to say
    before the deadline is kept. ``communicate()`` would be cancelled with it.
    """
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    out_task = asyncio.create_task(proc.stdout.read())
    err_task = asyncio.create_task(proc.stderr.read())
    timed_out = False
    try:
        _done, pending = await asyncio.wait(
            {out_task, err_task}, timeout=timeout
        )
        if pending:
            timed_out = True
            _kill(proc)
        try:
            # Reap the child so no zombie (POSIX) or dangling handle (Windows)
            # is left behind, on both the clean and the killed path.
            await asyncio.wait_for(proc.wait(), timeout=KILL_GRACE_S)
        except (asyncio.TimeoutError, OSError):  # pragma: no cover - defensive
            logger.warning("Process %s did not exit within the grace period", argv[:1])
        if pending:
            # The kill closes the pipes; give the readers a moment to drain.
            await asyncio.wait({out_task, err_task}, timeout=KILL_GRACE_S)
    finally:
        for task in (out_task, err_task):
            if not task.done():
                task.cancel()

    stdout = _task_text(out_task).decode("utf-8", errors="replace")
    stderr = _task_text(err_task).decode("utf-8", errors="replace")
    out, cut_out = _clip(stdout), len(stdout) > MAX_OUTPUT_CHARS
    err, cut_err = _clip(stderr), len(stderr) > MAX_OUTPUT_CHARS
    return ExecOutcome(
        argv=list(argv),
        exit_code=None if timed_out else proc.returncode,
        stdout=out,
        stderr=err,
        duration_ms=int((time.monotonic() - started) * 1000),
        timed_out=timed_out,
        truncated=cut_out or cut_err,
        timeout_s=float(timeout),
        cwd=str(cwd or ""),
    )


def _failure(outcome: ExecOutcome) -> ToolResult:
    """One shape for every non-zero/timeout outcome.

    ``error`` carries the same rendering as ``output`` because the agent loop
    feeds ``error`` back to the model as the whole observation on failure — a
    test run that dies at line 40 is useless to the model if only "exit 1"
    survives.
    """
    body = outcome.render()
    return ToolResult(success=False, output=body, error=body, data=outcome.to_dict())


class ExecuteLocalCommand(BaseTool):
    """Run one command and report what it actually did.

    The structured counterpart of ``system_command``: same sandbox, same gate,
    same refusal of shell syntax — plus a tunable deadline and a result an
    agent can branch on.
    """

    permission = "confirm"

    def name(self) -> str:
        return "execute_local_command"

    def aliases(self) -> List[str]:
        return ["run_local_command", "exec_local"]

    def description(self) -> str:
        return (
            "Run one local command and get a structured result: exit code, "
            "stdout, stderr, duration and whether it timed out. Shell chaining "
            "and metacharacters are refused. Requires confirmation."
        )

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": _prop(
                    "string",
                    "The command as plain words, e.g. 'python -m pytest -q'. "
                    "No shell syntax: no quotes, pipes, redirects or chaining.",
                ),
                "cwd": _prop(
                    "string",
                    "Working directory inside the workspace: a relative path "
                    "resolves against the workspace root and an absolute one "
                    "must be contained by it. Defaults to the workspace root.",
                    default="",
                ),
                "timeout": _prop(
                    "integer",
                    f"Seconds before the process is killed "
                    f"({MIN_TIMEOUT_S}-{MAX_TIMEOUT_S}).",
                    default=COMMAND_TIMEOUT_S,
                ),
            },
            "required": ["command"],
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        super().validate(params)
        command = params.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ToolValidationError("'command' is required")
        cwd = params.get("cwd", "")
        if cwd and not isinstance(cwd, str):
            raise ToolValidationError("'cwd' must be a string")
        check_timeout(params.get("timeout"))
        # Unconditional sandbox checks, deliberately here: separators, shell
        # metacharacters and %VAR% expansion are refused outright, before the
        # gate, because approving them would not help. `split_command` raises
        # SecurityError, which the registry reports as `blocked: true`.
        split_command(command)

    async def execute(
        self,
        command: str = "",
        cwd: str = "",
        timeout: Optional[float] = None,
        **_: Any,
    ) -> ToolResult:
        try:
            parts = split_command(command)
        except SecurityError as e:  # re-checked: the gate sits between the two
            return ToolResult.denied(str(e))

        argv = _build_argv(parts)
        if argv is None:
            return ToolResult.fail(f"Command not found: {parts[0]}")

        # Default to the workspace root, not the process cwd: the backend is
        # launched from `backend/`, so inheriting it would make every bare
        # command run in the assistant's own source tree. A relative `cwd`
        # resolves against the root (one definition, `resolve_workspace`), and
        # an absolute one is rejected unless it is contained.
        #
        # Reached through the `sandbox` module so this honours the same
        # indirection (and the same test seam) as every file tool.
        workdir = resolve_workspace(cwd) if cwd else sandbox.workspace_root()
        if not workdir:
            return ToolResult.denied(f"cwd '{cwd}' is outside the workspace")
        if not os.path.isdir(workdir):
            return ToolResult.fail(f"Working directory does not exist: {workdir}")

        seconds = check_timeout(timeout)
        try:
            outcome = await run_argv(argv, cwd=workdir, timeout=seconds)
        except FileNotFoundError:
            return ToolResult.fail(f"Command not found: {parts[0]}")
        except OSError as e:
            return ToolResult.fail(
                safe_error(
                    e,
                    operation="execute_local_command",
                    component="tools.local_exec",
                    detail="could not start the process",
                )
            )

        if outcome.succeeded:
            return ToolResult.ok(outcome.render(), outcome.to_dict())
        return _failure(outcome)


class ExecuteCode(BaseTool):
    """Run a source snippet and report the same structured outcome.

    Same policy as ``run_code``: the language is allow-listed, shell
    interpreters are refused even after a human approves, and the tier is
    DANGEROUS because arbitrary code execution is.
    """

    permission = "dangerous"

    def name(self) -> str:
        return "execute_code"

    def aliases(self) -> List[str]:
        return ["run_snippet", "exec_code"]

    def description(self) -> str:
        return (
            "Run a code snippet in Python, JavaScript, TypeScript, Go, Rust, "
            "Ruby or SQLite and get a structured result (exit code, stdout, "
            "stderr, duration, timeout). Shell interpreters are refused even "
            "after approval. Requires confirmation."
        )

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "code": _prop("string", "Source to execute."),
                "language": _prop(
                    "string",
                    "One of: python, javascript, typescript, go, rust, ruby, sqlite.",
                ),
                "timeout": _prop(
                    "integer",
                    f"Seconds before the process is killed "
                    f"({MIN_TIMEOUT_S}-{MAX_TIMEOUT_S}).",
                    default=CODE_TIMEOUT_S,
                ),
            },
            "required": ["code", "language"],
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        code = params.get("code")
        if not isinstance(code, str) or not code.strip():
            raise ToolValidationError("'code' is required")
        language = params.get("language")
        if not isinstance(language, str) or not language.strip():
            raise ToolValidationError("'language' is required")
        normalised = language.strip().lower()
        # A refused interpreter is a *policy* decision, not a malformed request,
        # so it surfaces as `blocked` rather than as an error to retry.
        if normalised in DENIED_CODE_LANGUAGES:
            raise SecurityError(
                f"Language '{normalised}' is not permitted for code execution."
            )
        if normalised not in RUNNERS:
            raise ToolValidationError(
                f"Unsupported language: {language}. "
                f"Available: {', '.join(sorted(RUNNERS))}"
            )
        check_timeout(params.get("timeout"))

    def risk_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        # The timeout and the rendered result are irrelevant to the pattern
        # rules; only the source and its language are judged.
        return {"code": params.get("code", ""), "language": params.get("language", "")}

    async def execute(
        self,
        code: str = "",
        language: str = "",
        timeout: Optional[float] = None,
        **_: Any,
    ) -> ToolResult:
        language = (language or "").strip().lower()
        # Defence in depth: refused even after a human approves.
        if language in DENIED_CODE_LANGUAGES:
            return ToolResult.denied(
                f"Language '{language}' is not permitted for code execution."
            )
        runner = RUNNERS.get(language)
        if runner is None:
            return ToolResult.fail(
                f"Unsupported language: {language}. "
                f"Available: {', '.join(sorted(RUNNERS))}"
            )
        seconds = check_timeout(timeout, default=CODE_TIMEOUT_S)

        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=runner["ext"], delete=False, encoding="utf-8"
        )
        path = handle.name
        try:
            handle.write(code)
            handle.close()
            argv = [part.replace("{file}", path) for part in runner["cmd"]]
            try:
                outcome = await run_argv(
                    argv, cwd=tempfile.gettempdir(), timeout=seconds
                )
            except FileNotFoundError:
                return ToolResult.fail(
                    f"'{runner['cmd'][0]}' is not installed or not on PATH."
                )
            except OSError as e:
                return ToolResult.fail(
                    safe_error(
                        e,
                        operation="execute_code",
                        component="tools.local_exec",
                        detail="could not start the interpreter",
                    )
                )
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

        data = outcome.to_dict()
        data["language"] = language
        if outcome.succeeded:
            return ToolResult.ok(outcome.render(), data)
        body = outcome.render()
        return ToolResult(success=False, output=body, error=body, data=data)


def build_tools() -> List[BaseTool]:
    return [ExecuteLocalCommand(), ExecuteCode()]
