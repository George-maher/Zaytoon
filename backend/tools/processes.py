"""Background processes Zaytoon started, and nothing else.

A dev server does not finish; it runs until it is stopped, which is precisely
the case :mod:`tools.local_exec` is wrong for. This module owns those: start a
process, keep its recent output, show whether it is alive, stop it again.

Two rules make this safe rather than merely convenient:

* **Only what we started is tracked.** A pid enters the registry solely because
  :meth:`ProcessRegistry.start` spawned it. Nothing is ever discovered by
  scanning the process table, and :meth:`ProcessRegistry.stop` refuses an id it
  does not own, so Zaytoon cannot kill the user's editor, their database, or
  anything else it merely noticed.
* **The same sandbox as every other command.** The command line goes through
  :func:`tools.sandbox.split_command` and the working directory through
  :func:`tools.sandbox.resolve_workspace` — the same two gates, in the same
  order, as ``system_command`` and ``execute_local_command``. A pipe or a
  separator is refused outright here too; approval does not unlock it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

from common.reporting import redact, safe_error
from tools.base import BaseTool, ToolResult, ToolValidationError, _prop
from tools.sandbox import resolve_workspace, split_command
from tools import sandbox
from tools.local_exec import MAX_TIMEOUT_S, MIN_TIMEOUT_S, check_timeout
from tools.terminal import _build_argv

logger = logging.getLogger("Zaytoon.Tools.Processes")

#: Output kept per process. A dev server can talk for hours; the tail is what a
#: human needs to see the last error, and an unbounded buffer is a memory leak
#: with extra steps.
LOG_LINES = 500

#: How long a stopped process is given to exit before it is killed.
STOP_GRACE_S = 3.0

#: Upper bound on tracked processes. Exited ones are pruned oldest-first, so
#: this bounds the *table*, not the number of live dev servers.
MAX_TRACKED = 32

#: The tail shipped with a list response, so a client can show the last few
#: lines without a second round trip.
LIST_TAIL = 20


@dataclass
class ManagedProcess:
    """One process Zaytoon spawned."""

    id: str
    name: str
    argv: List[str]
    cwd: str
    pid: int
    started_at: float
    status: str = "running"
    exit_code: Optional[int] = None
    timeout_s: Optional[float] = None
    lines: Deque[str] = field(default_factory=lambda: deque(maxlen=LOG_LINES))
    #: Tasks reading stdout/stderr and reaping the child. Kept so the registry
    #: can await them on stop instead of leaving orphans behind.
    pumps: List["asyncio.Task"] = field(default_factory=list)
    proc: Any = None

    def duration_ms(self) -> int:
        return int((time.monotonic() - self.started_at) * 1000)

    def tail(self, count: int = LIST_TAIL) -> List[str]:
        count = max(1, min(int(count), LOG_LINES))
        return list(self.lines)[-count:]

    def to_dict(self, with_lines: bool = True) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "command": " ".join(self.argv),
            "cwd": self.cwd,
            "pid": self.pid,
            "status": self.status,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms(),
        }
        if with_lines:
            payload["lines"] = self.tail()
        return payload


class ProcessRegistry:
    """The tracked-process table.

    Module-level singleton (:data:`registry`) so the tools, the REST routes and
    the tests all address the same table.
    """

    def __init__(self) -> None:
        self._procs: Dict[str, ManagedProcess] = {}
        self._counter = 0
        self._lock = asyncio.Lock()

    # -- queries ---------------------------------------------------------
    def get(self, process_id: str) -> Optional[ManagedProcess]:
        return self._procs.get(str(process_id))

    def list(self) -> List[ManagedProcess]:
        """Newest first — the process a user just asked about is the one they
        are looking for."""
        return sorted(self._procs.values(), key=lambda p: p.started_at, reverse=True)

    def logs(self, process_id: str, tail: int = LIST_TAIL) -> Optional[List[str]]:
        entry = self.get(process_id)
        return None if entry is None else entry.tail(tail)

    def snapshot(self) -> List[Dict[str, Any]]:
        return [p.to_dict() for p in self.list()]

    # -- lifecycle -------------------------------------------------------
    async def start(
        self,
        argv: List[str],
        *,
        cwd: str,
        name: str = "",
        timeout: Optional[float] = None,
    ) -> ManagedProcess:
        """Spawn a tracked process and begin capturing its output."""
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
            # Own process group where the platform allows it, so stopping a dev
            # server that spawned children does not leave the children running.
            **(_own_group()),
        )
        async with self._lock:
            self._counter += 1
            process_id = f"proc_{self._counter}_{int(time.time())}"
            self._prune_locked()
            entry = ManagedProcess(
                id=process_id,
                name=name or argv[0],
                argv=list(argv),
                cwd=cwd,
                pid=proc.pid,
                started_at=time.monotonic(),
                timeout_s=timeout,
                proc=proc,
            )
            self._procs[process_id] = entry

        entry.pumps = [
            asyncio.create_task(self._pump(entry, proc.stdout), name=f"pump:{process_id}"),
            asyncio.create_task(self._watch(entry, proc), name=f"watch:{process_id}"),
        ]
        logger.info(
            "Tracked process %s (%s) pid=%s cwd=%s", process_id, entry.name, proc.pid, cwd
        )
        return entry

    async def _pump(self, entry: ManagedProcess, stream: Any) -> None:
        """Append each output line to the ring buffer until the pipe closes.

        Output is redacted on the way in: a dev server that prints an env
        variable it read must not turn the log tail into a credential leak.
        """
        if stream is None:  # pragma: no cover - defensive
            return
        try:
            while True:
                raw = await stream.readline()
                if not raw:
                    break
                line = redact(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
                if line:
                    entry.lines.append(line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a reader must never die loudly
            logger.warning(
                safe_error(
                    exc,
                    operation="process.pump",
                    component="tools.processes",
                    detail=f"process={entry.id}",
                )
            )

    async def _watch(self, entry: ManagedProcess, proc: Any) -> None:
        """Record how the child ended, and honour an optional deadline."""
        try:
            if entry.timeout_s:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=entry.timeout_s)
                except asyncio.TimeoutError:
                    entry.status = "timed_out"
                    await _terminate(entry)
                    return
            await proc.wait()
            entry.exit_code = proc.returncode
            entry.status = "exited" if entry.status == "running" else entry.status
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                safe_error(
                    exc,
                    operation="process.watch",
                    component="tools.processes",
                    detail=f"process={entry.id}",
                )
            )
        finally:
            logger.info("Process %s ended: status=%s exit=%s", entry.id, entry.status, entry.exit_code)

    async def stop(self, process_id: str) -> Optional[Dict[str, Any]]:
        """Stop a tracked process, escalating terminate → kill.

        Returns ``None`` for an unknown id: this is the property that keeps the
        registry from being a remote kill switch for the whole machine.
        """
        entry = self.get(process_id)
        if entry is None:
            return None
        if entry.status == "running":
            await _terminate(entry)
        for task in entry.pumps:
            if not task.done():
                task.cancel()
        return entry.to_dict(with_lines=False)

    def _prune_locked(self) -> None:
        """Forget the oldest *exited* entries once the table is full."""
        if len(self._procs) < MAX_TRACKED:
            return
        finished = [p for p in self._procs.values() if p.status != "running"]
        finished.sort(key=lambda p: p.started_at)
        for entry in finished[: max(0, len(self._procs) - MAX_TRACKED + 1)]:
            self._procs.pop(entry.id, None)


def _own_group() -> Dict[str, Any]:
    """``creationflags`` for a new process group, Windows only."""
    if os.name != "nt":
        return {}
    return {"creationflags": getattr(asyncio.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)}


async def _terminate(entry: ManagedProcess) -> None:
    """Ask politely, then insist."""
    proc = entry.proc
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.terminate()
    except (ProcessLookupError, OSError):
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=STOP_GRACE_S)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=STOP_GRACE_S)
        except (ProcessLookupError, OSError, asyncio.TimeoutError):
            logger.warning("Process %s did not die after kill", entry.id)
    entry.exit_code = proc.returncode
    if entry.status != "timed_out":
        entry.status = "stopped"


#: The table both clients and the REST routes share.
registry = ProcessRegistry()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
class ProcessStart(BaseTool):
    """Start a long-running process (dev server, watcher) in the background."""

    permission = "confirm"

    def name(self) -> str:
        return "process_start"

    def description(self) -> str:
        return (
            "Start a long-running background process such as a dev server, and "
            "track it so it can be listed, inspected and stopped later. Use this "
            "for something that does not finish on its own; use "
            "execute_local_command for a one-shot command. Requires confirmation."
        )

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": _prop(
                    "string",
                    "The command as plain words, e.g. 'npm run dev'. No shell "
                    "syntax: no quotes, pipes, redirects or chaining.",
                ),
                "name": _prop("string", "Short label for the process, e.g. 'vite'.", default=""),
                "cwd": _prop(
                    "string",
                    "Working directory inside the workspace; relative paths "
                    "resolve against the workspace root. Defaults to the root.",
                    default="",
                ),
                "timeout": _prop(
                    "integer",
                    "Optional seconds after which the process is stopped. "
                    "Leave unset for a server that runs until stopped.",
                    default=0,
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
        if params.get("timeout"):
            check_timeout(params["timeout"])
        # Unconditional sandbox refusal, before the gate — same as every other
        # execution tool.
        split_command(command)

    async def execute(
        self,
        command: str = "",
        name: str = "",
        cwd: str = "",
        timeout: Optional[float] = None,
        **_: Any,
    ) -> ToolResult:
        try:
            parts = split_command(command)
        except Exception as e:  # re-checked: the gate sits between the two
            return ToolResult.denied(str(e))

        argv = _build_argv(parts)
        if argv is None:
            return ToolResult.fail(f"Command not found: {parts[0]}")

        # Never silently inherit the backend's own directory: resolve through
        # the workspace exactly as `execute_local_command` does.
        workdir = resolve_workspace(cwd) if cwd else sandbox.workspace_root()
        if not workdir:
            return ToolResult.denied(f"cwd '{cwd}' is outside the workspace")
        if not os.path.isdir(workdir):
            return ToolResult.fail(f"Working directory does not exist: {workdir}")

        seconds = check_timeout(timeout) if timeout else None
        try:
            entry = await registry.start(
                argv, cwd=workdir, name=name or parts[0], timeout=seconds
            )
        except FileNotFoundError:
            return ToolResult.fail(f"Command not found: {parts[0]}")
        except OSError as e:
            return ToolResult.fail(
                safe_error(
                    e,
                    operation="process_start",
                    component="tools.processes",
                    detail="could not start the process",
                )
            )

        data = entry.to_dict(with_lines=False)
        return ToolResult.ok(
            f"Started '{entry.name}' (pid {entry.pid}) in {entry.cwd}. "
            f"Use process_list / process_logs to check on it and process_stop "
            f"to end it.",
            data,
        )


class ProcessList(BaseTool):
    """Show the tracked processes and whether they are alive."""

    permission = "safe"

    def name(self) -> str:
        return "process_list"

    def description(self) -> str:
        return (
            "List background processes Zaytoon started, with pid, status, "
            "duration and recent output."
        )

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }

    async def execute(self, **_: Any) -> ToolResult:
        entries = registry.snapshot()
        if not entries:
            return ToolResult.ok("No background processes are being tracked.", {"processes": []})
        rows = [
            f"{e['id']}  {e['name']}  {e['status']}  pid={e['pid']}  "
            f"{e['duration_ms']}ms  {e['command']}"
            for e in entries
        ]
        return ToolResult.ok("\n".join(rows), {"processes": entries})


class ProcessLogs(BaseTool):
    """Read what a tracked process has printed."""

    permission = "safe"

    def name(self) -> str:
        return "process_logs"

    def description(self) -> str:
        return "Show the recent output of a background process started by Zaytoon."

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "id": _prop("string", "The process id from process_start / process_list."),
                "tail": _prop("integer", f"How many recent lines (1-{LOG_LINES}).", default=LIST_TAIL),
            },
            "required": ["id"],
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        super().validate(params)
        if not params.get("id"):
            raise ToolValidationError("'id' is required")

    async def execute(self, id: str = "", tail: int = LIST_TAIL, **_: Any) -> ToolResult:
        entry = registry.get(id)
        if entry is None:
            return ToolResult.fail(f"No such process: {id}")
        try:
            count = max(1, min(int(tail or LIST_TAIL), LOG_LINES))
        except (TypeError, ValueError):
            count = LIST_TAIL
        lines = entry.tail(count)
        header = f"{entry.name} ({entry.status}, pid {entry.pid}, {entry.cwd})"
        return ToolResult.ok(
            "\n".join([header, *lines]) if lines else f"{header}\n(no output yet)",
            {"id": entry.id, "status": entry.status, "exit_code": entry.exit_code, "lines": lines},
        )


class ProcessStop(BaseTool):
    """Stop a tracked process — and only a tracked one."""

    permission = "confirm"

    def name(self) -> str:
        return "process_stop"

    def description(self) -> str:
        return (
            "Stop a background process that Zaytoon started. Processes not "
            "started by Zaytoon cannot be stopped through this tool. "
            "Requires confirmation."
        )

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "id": _prop("string", "The process id from process_start / process_list."),
            },
            "required": ["id"],
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        super().validate(params)
        if not params.get("id"):
            raise ToolValidationError("'id' is required")

    async def execute(self, id: str = "", **_: Any) -> ToolResult:
        result = await registry.stop(id)
        if result is None:
            return ToolResult.fail(
                f"No such process: {id}. Only processes Zaytoon started can be stopped."
            )
        return ToolResult.ok(
            f"Stopped {result['name']} (pid {result['pid']}).", result
        )


def build_tools() -> List[BaseTool]:
    return [ProcessStart(), ProcessList(), ProcessLogs(), ProcessStop()]