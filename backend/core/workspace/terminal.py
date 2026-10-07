"""Streaming terminal sessions.

The existing ``tools/processes.py`` already owns long-running processes with the
right safety property — **only what Zaytoon started can be stopped** — so this
does not replace it. It adds what a terminal panel needs:

* **Multiple named sessions**, so a dev server and a test run coexist.
* **Line streaming**, so output appears as it happens rather than on exit.
* **Input**, because an interactive process (a REPL, a prompt) needs it.

Deliberately reusing ``tools.sandbox.split_command``: a pipe or a separator is
refused here exactly as it is for ``system_command``. If a shell feature ever
became available, it would have to be added to *both* paths, which is the
correct kind of hard.

The streaming pump is a task that lives as long as the session. Every path that
ends a session cancels it, because a pump outliving its session is a task that
leaks a pipe handle per invocation.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional

from tools.sandbox import SecurityError, resolve_workspace, split_command, workspace_root

#: A subscription-removal handle, returned by :meth:`TerminalManager.add_sink`.
Unsubscribe = Callable[[], None]

logger = logging.getLogger("Zaytoon.Core.Terminal")

#: Ring buffer depth per session.
MAX_LINES = 1000

#: Cap on concurrent sessions. A user opening twelve terminals is a mistake, and
#: each one is a process holding a pipe.
MAX_SESSIONS = 12

#: Longest single input line accepted.
MAX_INPUT_CHARS = 8000

#: Bounds for the ``env`` overlay: enough for ``NODE_ENV``/``PORT``-style
#: configuration, not enough to dump a secrets file into a child.
MAX_ENV_ENTRIES = 16
MAX_ENV_VALUE_CHARS = 1024
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Wall-clock ceiling for an explicit session timeout. A session with no
#: timeout runs until closed; this only bounds the opt-in.
MAX_TIMEOUT_S = 24 * 60 * 60

#: Subclasses get their own process group so a stop takes the children with it.
_CREATE_NEW_PROCESS_GROUP = getattr(
    asyncio.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
)


class TerminalError(RuntimeError):
    """A terminal operation failed, with a reason the user can act on.

    ``blocked`` distinguishes a **policy refusal** from a malformed request.
    The sandbox refusing to chain two commands is not something a user can
    approve away — telling them "check your command and try again" would be
    actively misleading, so the API maps this to 403 rather than 400.
    """

    def __init__(self, message: str, blocked: bool = False):
        super().__init__(message)
        self.blocked = blocked


@dataclass
class TerminalSession:
    """One terminal."""

    id: str
    command: str
    argv: List[str]
    cwd: str
    name: str = ""
    pid: Optional[int] = None
    status: str = "running"
    exit_code: Optional[int] = None
    started_at: float = field(default_factory=time.monotonic)
    #: Wall-clock limit in seconds, or ``None`` for "until closed".
    timeout_s: Optional[float] = None
    #: Environment overlay keys (names only — never values; see `_validate_env`).
    env_keys: List[str] = field(default_factory=list)
    #: Monotonic creation counter. Breaks ties in `list()`, which the clock
    #: cannot: two sessions opened in the same tick are otherwise
    #: indistinguishable.
    sequence: int = 0
    lines: Deque[Dict[str, Any]] = field(default_factory=lambda: deque(maxlen=MAX_LINES))
    proc: Any = None
    stdin: Any = None
    pumps: List["asyncio.Task"] = field(default_factory=list)

    def duration_ms(self) -> int:
        return int((time.monotonic() - self.started_at) * 1000)

    def tail(self, count: int = 200) -> List[Dict[str, Any]]:
        try:
            n = max(1, min(int(count or 200), MAX_LINES))
        except (TypeError, ValueError):
            n = 200
        return list(self.lines)[-n:]

    def to_dict(self, with_lines: bool = True) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "id": self.id,
            "name": self.name or self.argv[0],
            "command": self.command,
            "cwd": self.cwd,
            "pid": self.pid,
            "status": self.status,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms(),
            "timeout_s": self.timeout_s,
            # Key names only — never values. See TerminalManager._validate_env.
            "env_keys": list(self.env_keys),
        }
        if with_lines:
            payload["lines"] = self.tail()
        return payload


#: Called with each output line. Set by the API layer to relay to WebSocket
#: clients; the terminal itself does not know a socket exists.
LineSink = Callable[[TerminalSession, Dict[str, Any]], Optional[Awaitable[None]]]


class TerminalManager:
    """Owns the session table."""

    def __init__(self, sink: Optional[LineSink] = None) -> None:
        self._sessions: Dict[str, TerminalSession] = {}
        self._lock = asyncio.Lock()
        self._counter = 0
        #: Output relays, in registration order. A **list**, not a single sink:
        #: two clients watching two different terminals must both receive their
        #: own lines, and a single-slot sink makes the second subscriber either
        # replace the first or wrap it — both of which break when the socket that
        #: wrapped it goes away.
        self._sinks: List[LineSink] = [sink] if sink else []

    def add_sink(self, sink: LineSink) -> Callable[[], None]:
        """Register an output relay. Returns a function that removes it.

        The unsubscribe handle exists because a WebSocket session must be able
        to detach on disconnect; a subscription with no way to detach is a leak
        that only shows up as a growing list of dead sockets.
        """
        self._sinks.append(sink)

        def remove() -> None:
            try:
                self._sinks.remove(sink)
            except ValueError:
                pass

        return remove

    def set_sink(self, sink: Optional[LineSink]) -> None:
        """Replace the relay set. Used by tests and by a single-client setup."""
        self._sinks = [sink] if sink else []

    def remove_sink(self, sink: LineSink) -> None:
        try:
            self._sinks.remove(sink)
        except ValueError:
            pass

    # -- queries ----------------------------------------------------------
    def get(self, session_id: str) -> Optional[TerminalSession]:
        return self._sessions.get(session_id)

    def list(self) -> List[TerminalSession]:
        """Newest first.

        The sequence number is the tie-break, not decoration:
        ``time.monotonic()`` on Windows has a resolution coarse enough that two
        sessions opened back to back report the *same* value, and sorting on
        that alone leaves the order to dict insertion — which happens to work
        until a session is closed and re-opened, at which point the newest is
        listed first and nothing is wrong except the list reads as arbitrary.
        """
        return sorted(
            self._sessions.values(),
            key=lambda s: (s.started_at, s.sequence),
            reverse=True,
        )

    def snapshot(self, with_lines: bool = False) -> List[Dict[str, Any]]:
        return [s.to_dict(with_lines) for s in self.list()]

    # -- lifecycle --------------------------------------------------------
    @staticmethod
    def _validate_env(env: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
        """Check an env overlay before it reaches a child process.

        Values are passed via ``exec``'s environment, never through a shell,
        so there is no command-injection surface — but keys still have to be
        real POSIX names, and an unbounded dict is a place to lose a secret by
        accident. Capped and validated; ``to_dict`` echoes the key **names**
        only, because a terminal that prints its credential *values* into the
        API is a credential leak with a UI.
        """
        if not env:
            return None
        if not isinstance(env, dict):
            raise TerminalError("'env' must be an object of name → value")
        if len(env) > MAX_ENV_ENTRIES:
            raise TerminalError(f"'env' accepts at most {MAX_ENV_ENTRIES} entries")
        clean: Dict[str, str] = {}
        for key, value in env.items():
            if not isinstance(key, str) or not ENV_KEY_RE.match(key):
                raise TerminalError(f"Invalid environment variable name: {key!r}")
            if not isinstance(value, str) or len(value) > MAX_ENV_VALUE_CHARS:
                raise TerminalError(f"Environment value for {key} must be a short string")
            clean[key] = value
        return clean

    async def open(
        self,
        command: str,
        cwd: str = "",
        name: str = "",
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> TerminalSession:
        """Start a session. The command goes through the same sandbox as every
        other command path — no separators, no shell syntax, no ``%VAR%``.

        ``env`` overlays the inherited environment (validated, capped). The
        child always inherits by default: an empty environment would break
        every tool that needs ``PATH``-like context.

        ``timeout`` is a wall-clock limit in seconds. Long-lived sessions run
        until closed by default; a timeout is opt-in because a dev server with
        a deadline would be a broken feature.

        A sandbox refusal is wrapped as a ``blocked`` :class:`TerminalError`
        rather than propagating ``SecurityError``. Both are refusals, but the
        API layer maps them to different status codes, and the caller of a
        terminal is not otherwise coupled to ``tools.sandbox``.
        """
        try:
            parts = split_command(command)
        except SecurityError as exc:
            raise TerminalError(str(exc), blocked=True) from exc

        env_overlay = self._validate_env(env)
        if timeout is not None:
            try:
                timeout = float(timeout)
            except (TypeError, ValueError):
                raise TerminalError("'timeout' must be a number of seconds") from None
            if not 0 < timeout <= MAX_TIMEOUT_S:
                raise TerminalError(f"'timeout' must be between 0 and {MAX_TIMEOUT_S:g}s")

        workdir = resolve_workspace(cwd) if cwd else workspace_root()
        if not workdir:
            raise TerminalError(f"'{cwd}' is outside the workspace", blocked=True)
        if not os.path.isdir(workdir):
            raise TerminalError(f"Working directory does not exist: {cwd}")

        from tools.terminal import _build_argv

        argv = _build_argv(parts)
        if argv is None:
            raise TerminalError(f"Command not found: {parts[0]}")

        async with self._lock:
            live = [s for s in self._sessions.values() if s.status == "running"]
            if len(live) >= MAX_SESSIONS:
                raise TerminalError(
                    f"{MAX_SESSIONS} terminal sessions are already open. "
                    "Close one first."
                )

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.PIPE,
                cwd=workdir,
                # Overlay, not replacement: the child keeps PATH and friends.
                env={**os.environ, **env_overlay} if env_overlay else None,
                **({"creationflags": _CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}),
            )
        except FileNotFoundError as exc:
            raise TerminalError(f"Command not found: {parts[0]}") from exc
        except OSError as exc:
            raise TerminalError(f"Could not start the command: {exc.strerror}") from exc

        self._counter += 1
        session = TerminalSession(
            id=f"term_{secrets.token_hex(4)}",
            command=command,
            argv=argv,
            cwd=workdir,
            name=name or parts[0],
            pid=proc.pid,
            proc=proc,
            stdin=proc.stdin,
            sequence=self._counter,
            timeout_s=timeout,
            env_keys=sorted(env_overlay) if env_overlay else [],
        )
        self._sessions[session.id] = session
        session.pumps = [
            asyncio.create_task(self._pump(session), name=f"term:{session.id}"),
            asyncio.create_task(self._watch(session), name=f"term-watch:{session.id}"),
        ]
        if timeout:
            # Lives in `pumps`, so every path that ends the session cancels it —
            # a timeout task waking for a session that already closed would
            # otherwise try to close it again.
            session.pumps.append(
                asyncio.create_task(
                    self._timeout_watch(session, timeout),
                    name=f"term-timeout:{session.id}",
                )
            )
        self._prune()
        logger.info(
            "Terminal %s started: %s (pid %s) in %s", session.id, command, proc.pid, workdir
        )
        return session

    async def _emit(self, session: TerminalSession, entry: Dict[str, Any]) -> None:
        """Record a line and hand it to the sink.

        Output is redacted on the way in, for the same reason the process log
        tail is: a dev server printing the environment it was handed must not
        turn a terminal into a credential leak.
        """
        from common.reporting import redact

        entry = {
            "ts": time.time(),
            "stream": entry.get("stream", "stdout"),
            "text": redact(str(entry.get("text", "")))[:4000],
        }
        if not entry["text"]:
            return
        session.lines.append(entry)
        # Copied before iterating: a sink that unsubscribes itself (a socket
        # closing) would otherwise mutate the list mid-iteration.
        for sink in list(self._sinks):
            if sink is None:
                continue
            try:
                result = sink(session, entry)
                if asyncio.iscoroutine(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a sink must not kill the pump
                logger.warning("Terminal sink failed: %s", type(exc).__name__)

    async def _pump(self, session: TerminalSession) -> None:
        stream = session.proc.stdout if session.proc else None
        if stream is None:
            return
        try:
            while True:
                raw = await stream.readline()
                if not raw:
                    break
                await self._emit(session, {
                    "stream": "stdout",
                    "text": raw.decode("utf-8", errors="replace").rstrip("\r\n"),
                })
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Terminal %s pump ended: %s", session.id, type(exc).__name__)

    async def _watch(self, session: TerminalSession) -> None:
        try:
            await session.proc.wait()
            session.exit_code = session.proc.returncode
            if session.status == "running":
                session.status = "exited"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Terminal %s watch ended: %s", session.id, type(exc).__name__)
        finally:
            self._cancel_pumps(session)

    async def _timeout_watch(self, session: TerminalSession, seconds: float) -> None:
        """Stop a session when its wall-clock limit elapses.

        The reason goes into the output *before* the stop, because "the
        terminal closed itself" is a mystery whereas "stopped after 30s" is an
        answer. The session id is re-fetched: by the time the sleep ends the
        session may have been closed and pruned, and closing it again would
        resurrect a status transition on a dead record.
        """
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            raise
        if self._sessions.get(session.id) is not session or session.status != "running":
            return
        await self._emit(
            session, {"stream": "system", "text": f"[timeout: stopped after {seconds:g}s]"}
        )
        await self.close(session.id)

    def _cancel_pumps(self, session: TerminalSession) -> None:
        for task in session.pumps:
            if task is not asyncio.current_task() and not task.done():
                task.cancel()

    async def write(self, session_id: str, data: str) -> Dict[str, Any]:
        """Send input to a running session."""
        session = self.get(session_id)
        if session is None:
            raise TerminalError(f"No such terminal: {session_id}")
        if session.status != "running":
            raise TerminalError(f"Terminal {session_id} has already exited")
        payload = (data or "")[:MAX_INPUT_CHARS]
        if not payload:
            raise TerminalError("Nothing to send")
        try:
            session.stdin.write(payload.encode("utf-8"))
            await session.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise TerminalError("The process closed its input") from exc
        return {"id": session_id, "sent": len(payload)}

    async def close(self, session_id: str) -> Dict[str, Any]:
        """Close a session, escalating terminate → kill.

        Returns ``None`` for an unknown id. Only sessions this manager started
        exist at all, which is the same ownership property the process registry
        has and the reason neither can be used as a remote kill switch.
        """
        session = self.get(session_id)
        if session is None:
            return None
        if session.status == "running":
            await self._terminate(session)
        self._cancel_pumps(session)
        self._sessions.pop(session_id, None)
        logger.info("Terminal %s closed (status=%s)", session_id, session.status)
        return session.to_dict(with_lines=False)

    async def _terminate(self, session: TerminalSession) -> None:
        proc = session.proc
        if proc is None or proc.returncode is not None:
            return
        try:
            if proc.returncode is None:
                proc.terminate()
        except (ProcessLookupError, OSError):
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except (ProcessLookupError, OSError, asyncio.TimeoutError):
                logger.warning("Terminal %s did not die after kill", session.id)
        session.exit_code = proc.returncode
        session.status = "stopped"

    def _prune(self) -> None:
        """Forget the oldest exited sessions once the table is full."""
        if len(self._sessions) < MAX_SESSIONS + 4:
            return
        finished = sorted(
            (s for s in self._sessions.values() if s.status != "running"),
            key=lambda s: (s.started_at, s.sequence),
        )
        for session in finished[: max(0, len(self._sessions) - MAX_SESSIONS)]:
            self._sessions.pop(session.id, None)

    async def close_all(self, reason: str = "shutdown") -> int:
        """Close every session. Returns how many were closed.

        Iterates the **ids**, not the session objects: :meth:`close` removes
        each entry from the table as it goes, so iterating the live list would
        mutate it mid-loop and then call `.id` on a string.
        """
        count = 0
        for session_id in list(self._sessions.keys()):
            if await self.close(session_id) is not None:
                count += 1
        if count:
            logger.info("Closed %d terminal session(s): %s", count, reason)
        return count


#: The process-wide table.
manager = TerminalManager()


def get_manager() -> TerminalManager:
    return manager


__all__ = [
    "MAX_INPUT_CHARS",
    "MAX_LINES",
    "MAX_SESSIONS",
    "TerminalError",
    "TerminalManager",
    "TerminalSession",
    "get_manager",
    "manager",
]