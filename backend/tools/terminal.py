"""Terminal and code-execution tools.

There used to be two implementations of each of these. ``ToolExecutor``'s was
the better one — it used ``powershell -EncodedCommand`` and supported five
languages for ``run_code`` — while ``ToolAgent``'s copies interpolated user text
into a command line and supported three. This module is the surviving version.

Two rules hold regardless of what is being run:

* The command is tokenised by :func:`tools.sandbox.split_command` first, so
  shell metacharacters and chaining are refused outright. **Approval is granted
  for one command, not for whatever follows a separator**, so this check is not
  overridable by the user saying yes.
* Every process is started with an argument vector and ``shell=False``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import tempfile
from typing import Any, Dict, List, Optional

from tools.base import BaseTool, ToolResult, ToolValidationError, _prop
from tools.sandbox import (
    DENIED_CODE_LANGUAGES,
    SecurityError,
    encode_powershell_command,
    powershell_sendkeys_script,
    split_command,
)

logger = logging.getLogger("Zaytoon.Tools.Terminal")

#: Wall-clock caps. Generous enough for a real build, short enough that a
#: runaway process cannot wedge the session.
COMMAND_TIMEOUT_S = 30
CODE_TIMEOUT_S = 30

MAX_OUTPUT_CHARS = 20_000

#: Shell builtins with no standalone executable, dispatched through the shell
#: binary with an already-validated argument vector. Because ``parts`` provably
#: contains no metacharacters, the shell has no syntax left to interpret.
_WINDOWS_BUILTINS = frozenset(
    {"echo", "dir", "date", "time", "ver", "vol", "cls", "type", "copy",
     "move", "del", "ren", "md", "rd", "set", "path", "title"}
)
_POSIX_BUILTINS = frozenset(
    {"echo", "pwd", "cd", "export", "set", "exit", "true", "false", ":",
     "read", "test", "printf", "wait", "umask", "alias"}
)

_IS_WINDOWS = os.name == "nt"


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text)} chars total]"


def _build_argv(parts: List[str]) -> Optional[List[str]]:
    """Resolve tokenised parts to an argv, or ``None`` if not runnable.

    Returns ``None`` when the executable cannot be found and the name is not a
    known shell builtin, so the caller can report "command not found" rather
    than spawning a shell that will fail confusingly.
    """
    if shutil.which(parts[0]) is not None:
        return parts
    base = os.path.splitext(parts[0].lower())[0]
    if _IS_WINDOWS:
        if base not in _WINDOWS_BUILTINS:
            return None
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", *parts]
    if parts[0] not in _POSIX_BUILTINS:
        return None
    return ["/bin/sh", "-c", " ".join(parts)]


class SystemCommand(BaseTool):
    """Arbitrary terminal execution. Confirmation-required for every command.

    This used to be content-inspected only, so ``echo hello`` ran with no
    prompt. Any command reaches an interpreter, so the harmless-looking case is
    precisely what an attacker would use to reach a harmful one.
    """

    permission = "confirm"

    def name(self) -> str:
        return "system_command"

    def aliases(self) -> List[str]:
        return ["cmd", "run_shell", "exec", "run_command"]

    def description(self) -> str:
        return (
            "Run a single terminal command. Shell chaining and metacharacters "
            "are refused. Requires confirmation."
        )

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": _prop(
                    "string",
                    "A single command with arguments. No pipes, redirects, "
                    "backgrounding or variable expansion.",
                ),
                "cwd": _prop(
                    "string",
                    "Working directory, relative to the workspace root.",
                ),
            },
            "required": ["command"],
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        command = params.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ToolValidationError("No command provided")
        # SecurityError is raised, not wrapped, so the registry can mark the
        # result `blocked`: approval cannot make a chained command legal.
        split_command(command)

    async def execute(self, command: str = "", cwd: str = "", **_: Any) -> ToolResult:
        parts = split_command(command)
        argv = _build_argv(parts)
        if argv is None:
            return ToolResult.fail(f"Command not found: {parts[0]}")

        workdir = None
        if cwd:
            from tools.sandbox import resolve_workspace

            workdir = resolve_workspace(cwd)
            if not workdir:
                return ToolResult.denied(
                    f"cwd '{cwd}' is outside the workspace"
                )

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=COMMAND_TIMEOUT_S
            )
        except FileNotFoundError:
            return ToolResult.fail(f"Command not found: {parts[0]}")
        except asyncio.TimeoutError:
            _kill(proc)
            return ToolResult.fail(f"Command timed out after {COMMAND_TIMEOUT_S}s")
        except OSError as e:
            return ToolResult.fail(f"Could not run command: {e}")

        out = _truncate(stdout.decode("utf-8", errors="replace"))
        err = _truncate(stderr.decode("utf-8", errors="replace"))
        if proc.returncode == 0:
            return ToolResult.ok(out or "(no output)", {"exit_code": 0, "stdout": out})
        detail = err or out or f"exit {proc.returncode}"
        return ToolResult(
            success=False,
            output=out,
            error=detail,
            data={"exit_code": proc.returncode, "stderr": err},
        )


def _kill(proc) -> None:
    try:
        proc.kill()
    except (ProcessLookupError, OSError):  # pragma: no cover
        pass


class RunCode(BaseTool):
    """Execute a snippet in a chosen language."""

    permission = "dangerous"

    def name(self) -> str:
        return "run_code"

    def description(self) -> str:
        return (
            "Run a code snippet in Python, JavaScript, TypeScript, Go, Rust, "
            "Ruby or SQLite. Shell interpreters are refused even after approval."
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
        # A refused interpreter is a *policy* decision, not a malformed
        # request, so it raises SecurityError and surfaces as `blocked` rather
        # than as a validation error the user could "fix" by retrying.
        if normalised in DENIED_CODE_LANGUAGES:
            raise SecurityError(
                f"Language '{normalised}' is not permitted for code execution."
            )
        if normalised not in RUNNERS:
            raise ToolValidationError(
                f"Unsupported language: {language}. "
                f"Available: {', '.join(sorted(RUNNERS))}"
            )

    def risk_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        # Both keys are scanned by the command/code pattern rules.
        return {"code": params.get("code", ""), "language": params.get("language", "")}

    async def execute(self, code: str = "", language: str = "", **_: Any) -> ToolResult:
        language = (language or "").strip().lower()
        code = (code or "").strip()

        # Defence in depth: refused even after a human approves, because these
        # interpreters exist to run arbitrary system commands.
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

        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=runner["ext"], delete=False, encoding="utf-8"
        )
        try:
            handle.write(code)
            handle.close()
            argv = [part.replace("{file}", handle.name) for part in runner["cmd"]]

            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=tempfile.gettempdir(),
                )
            except FileNotFoundError:
                return ToolResult.fail(
                    f"'{runner['cmd'][0]}' is not installed or not on PATH."
                )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=CODE_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                _kill(proc)
                return ToolResult.fail(f"Execution timed out after {CODE_TIMEOUT_S}s")

            out = _truncate(stdout.decode("utf-8", errors="replace"))
            err = _truncate(stderr.decode("utf-8", errors="replace"))
            if proc.returncode == 0:
                return ToolResult.ok(
                    out or "(no output)",
                    {"exit_code": 0, "language": language},
                )
            return ToolResult(
                success=False,
                output=out,
                error=err or f"exit {proc.returncode}",
                data={"exit_code": proc.returncode, "language": language, "stderr": err},
            )
        except OSError as e:
            return ToolResult.fail(f"Execution failed: {e}")
        finally:
            try:
                os.unlink(handle.name)
            except (OSError, NameError):
                pass


class SendKeys(BaseTool):
    """Inject text or a key combination into the focused window.

    Confirmation-required: the payload lands in whatever window has focus and
    can trigger a saved macro, a shortcut, or a destructive button.
    """

    permission = "confirm"

    def name(self) -> str:
        return "type_text"

    def aliases(self) -> List[str]:
        return ["key_press", "send_keys", "type_keys"]

    def description(self) -> str:
        return (
            "Type text, or press a key combination, into the currently focused "
            "window. Requires confirmation."
        )

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": _prop(
                    "string",
                    "Literal text to type. For a key combination such as "
                    "ctrl+c use the `keys` field instead.",
                ),
                "keys": _prop(
                    "string",
                    "Key combination, e.g. enter, ^c (ctrl+c), %{F4} (alt+F4).",
                ),
            },
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        has_text = isinstance(params.get("text"), str) and params["text"].strip()
        has_keys = isinstance(params.get("keys"), str) and params["keys"].strip()
        if not has_text and not has_keys:
            raise ToolValidationError("one of 'text' or 'keys' is required")

    def risk_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "text": params.get("text", ""),
            "keys": params.get("keys", ""),
        }

    async def execute(self, text: str = "", keys: str = "", **_: Any) -> ToolResult:
        from tools.sandbox import assert_sendkeys_keys, assert_sendkeys_text

        is_combo = bool(keys and keys.strip())
        payload_source = keys if is_combo else text
        try:
            # Literal text escapes SendKeys metacharacters; a key combination
            # must NOT, because `^`/`+`/`%` are the modifier syntax.
            payload = (
                assert_sendkeys_keys(payload_source)
                if is_combo
                else assert_sendkeys_text(payload_source)
            )
        except SecurityError as e:
            return ToolResult.denied(str(e))

        script = powershell_sendkeys_script(payload)
        if _IS_WINDOWS:
            # -EncodedCommand: the payload travels as base64 UTF-16LE and never
            # appears on a command line, so there is no quoting to get wrong.
            argv = [
                "powershell", "-NoProfile", "-NonInteractive",
                "-EncodedCommand", encode_powershell_command(script),
            ]
        else:
            argv = ["xdotool", "key" if is_combo else "type", "--clearmodifiers", payload]

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
        except FileNotFoundError:
            tool = "powershell" if _IS_WINDOWS else "xdotool"
            return ToolResult.fail(f"{tool} is not available on this system.")
        except OSError as e:
            return ToolResult.fail(f"Could not deliver keystrokes: {e}")

        if proc.returncode != 0:
            detail = _truncate(stderr.decode("utf-8", errors="replace"), 500)
            return ToolResult.fail(f"Keystroke delivery failed: {detail}")
        return ToolResult.ok(
            f"Pressed: {payload_source}" if is_combo else f"Typed: {payload_source[:80]}"
        )


#: Language → argv template. ``{file}`` is replaced with the temp file path.
RUNNERS: Dict[str, Dict[str, Any]] = {
    "python": {"cmd": [sys.executable, "{file}"], "ext": ".py"},
    "python3": {"cmd": [sys.executable, "{file}"], "ext": ".py"},
    "javascript": {"cmd": ["node", "{file}"], "ext": ".js"},
    "js": {"cmd": ["node", "{file}"], "ext": ".js"},
    "typescript": {"cmd": ["npx", "ts-node", "{file}"], "ext": ".ts"},
    "ts": {"cmd": ["npx", "ts-node", "{file}"], "ext": ".ts"},
    "sqlite": {"cmd": ["sqlite3", "{file}"], "ext": ".sql"},
    "ruby": {"cmd": ["ruby", "{file}"], "ext": ".rb"},
    "go": {"cmd": ["go", "run", "{file}"], "ext": ".go"},
    "rust": {"cmd": ["rustc", "{file}", "-o", "{file}.out"], "ext": ".rs"},
}


def build_tools() -> List[BaseTool]:
    return [SystemCommand(), RunCode(), SendKeys()]
