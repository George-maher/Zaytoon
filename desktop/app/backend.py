"""Manage the FastAPI backend as a child process.

This is a direct port of the behaviour the Rust shell used to provide, and it
keeps the two safety properties that mattered most:

1. **Never touch a backend we did not start.** If something is already
   answering on the port, the shell uses it and leaves it alone. The child
   handle is only ever set when *we* spawn, and only that handle is killed.
2. **Resolve the interpreter deliberately.** An explicit
   ``ZAYTOON_BACKEND_PYTHON`` wins, then ``backend/.venv``, then ``python`` on
   ``PATH`` — so a PyInstaller build can point at a different interpreter via
   the environment without code changes.

The TCP probe is a bare ``socket.connect_ex``, so this module never needs an
HTTP client and never blocks on a slow response.

Ownership is the whole design: a child we started is killed *as a tree*, because
``uvicorn`` is not the only process involved — a dev server or watcher started
through ``process_start`` is a grandchild of the shell, and terminating only the
direct child leaves it holding the port and the database lock. A backend we did
not start is never signalled at all.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: ``CREATE_NO_WINDOW`` — keeps a console window from flashing on Windows.
CREATE_NO_WINDOW = 0x08000000

#: How long a managed backend gets to stop itself before the tree is killed.
SHUTDOWN_GRACE_S = 8.0

#: And how long it then gets to actually be gone. After this the child is
#: abandoned with a log line: a shell that hangs on exit is a worse outcome for a
#: user than one that reports an unkillable grandchild.
KILL_WAIT_S = 5.0

VENV_PYTHON_CANDIDATES = (
    Path("Scripts") / "python.exe",  # Windows
    Path("bin") / "python",  # POSIX
)


def backend_is_up(host: str, port: int, timeout: float = 0.35) -> bool:
    """True when something accepts a TCP connection on ``host:port``."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex((host, port)) == 0
    except OSError:
        return False


def wait_for_backend(host: str, port: int, timeout: float, interval: float = 0.4) -> bool:
    """Poll until the port answers or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if backend_is_up(host, port):
            return True
        time.sleep(interval)
    return backend_is_up(host, port)


def resolve_backend_command(config) -> tuple[str, Path, str] | None:
    """Return ``(python_executable, main_script, cwd)`` or ``None``.

    ``None`` means no usable interpreter could be found, in which case the
    caller should surface an actionable message rather than crash.
    """
    root = Path(config.repo_root)
    backend_dir = root / "backend"

    # 1. explicit override, for frozen builds and unusual layouts
    if config.backend_python:
        exe = Path(config.backend_python)
        if exe.exists() or exe.name.lower().startswith("python"):
            main = Path(config.backend_main) if config.backend_main else backend_dir / "main.py"
            cwd = Path(config.backend_cwd) if config.backend_cwd else backend_dir
            return str(exe), main, str(cwd)

    # 2. the repository virtualenv
    venv = backend_dir / ".venv"
    for relative in VENV_PYTHON_CANDIDATES:
        candidate = venv / relative
        if candidate.exists():
            main = Path(config.backend_main) if config.backend_main else backend_dir / "main.py"
            cwd = Path(config.backend_cwd) if config.backend_cwd else backend_dir
            return str(candidate), main, str(cwd)

    # 3. the interpreter running this app (handy when launched from a venv)
    main = Path(config.backend_main) if config.backend_main else backend_dir / "main.py"
    cwd = Path(config.backend_cwd) if config.backend_cwd else backend_dir
    if not main.exists():
        return None
    return sys.executable, main, str(cwd)


@dataclass
class BackendStatus:
    """What the UI needs to show about the backend."""

    running: bool = False
    managed: bool = False
    hint: str = ""
    address: str = ""
    pid: int | None = None

    def as_dict(self) -> dict:
        return {
            "running": self.running,
            "managed": self.managed,
            "hint": self.hint,
            "address": self.address,
            "pid": self.pid,
        }


class BackendProcess:
    """Owns at most one child process: the FastAPI backend."""

    def __init__(self, config) -> None:
        self.config = config
        self._child: subprocess.Popen | None = None
        self._spawned_by_us = False

    # -- introspection ---------------------------------------------------
    @property
    def managed(self) -> bool:
        return self._spawned_by_us

    @property
    def pid(self) -> int | None:
        return self._child.pid if self._child is not None else None

    def status(self) -> BackendStatus:
        up = backend_is_up(self.config.host, self.config.port)
        return BackendStatus(
            running=up,
            managed=self._spawned_by_us,
            hint="" if up else "Backend is not responding.",
            address=self.config.backend_addr,
            pid=self.pid,
        )

    # -- lifecycle -------------------------------------------------------
    def ensure_running(self) -> BackendStatus:
        """Return a running backend, spawning one only if necessary.

        Never raises: on failure the returned status carries a ``hint`` the UI
        can display verbatim.
        """
        if backend_is_up(self.config.host, self.config.port):
            if self._child is not None and self._child.poll() is not None:
                # Our child died but something else owns the port now.
                self._child = None
                self._spawned_by_us = False
            return self.status()

        if not self.config.manage_backend:
            status = self.status()
            status.hint = (
                f"Backend not running on {self.config.backend_addr} and "
                "ZAYTOON_MANAGE_BACKEND is off. Start it manually."
            )
            return status

        resolved = resolve_backend_command(self.config)
        if resolved is None:
            status = self.status()
            status.hint = (
                "Could not find the backend. Expected "
                f"{Path(self.config.repo_root) / 'backend' / 'main.py'}."
            )
            return status

        python_exe, main_script, cwd = resolved
        env = dict(os.environ)
        env.setdefault("PYTHONUNBUFFERED", "1")
        kwargs: dict = {"cwd": cwd, "env": env}
        if os.name == "nt":
            kwargs["creationflags"] = CREATE_NO_WINDOW
        else:
            # Its own process group, so `shutdown` can signal the backend *and*
            # whatever the backend started. Without this, SIGTERM reaches uvicorn
            # only and a managed child of the agent outlives the shell.
            kwargs["start_new_session"] = True

        try:
            self._child = subprocess.Popen(  # noqa: S603 - argv list, no shell
                [python_exe, str(main_script)],
                **kwargs,
            )
        except OSError as exc:
            logger.error("could not spawn backend with %s: %s", python_exe, exc)
            status = self.status()
            status.hint = f"Failed to start the backend: {exc}"
            return status

        self._spawned_by_us = True
        logger.info("spawned backend pid=%s via %s", self._child.pid, python_exe)

        if not wait_for_backend(
            self.config.host, self.config.port, self.config.backend_timeout
        ):
            # Roll back so a later attempt is not blocked by a half-dead child.
            self.shutdown()
            status = self.status()
            status.hint = (
                f"Backend did not answer on {self.config.backend_addr} within "
                f"{self.config.backend_timeout:.0f}s. Check backend/.env."
            )
            return status

        return self.status()

    def _is_our_group_leader(self, child: subprocess.Popen) -> bool:
        """True when the child heads the process group we gave it.

        The guard that makes tree-signalling safe: `start_new_session=True` makes
        the child a leader, so its group contains the backend and nothing else. If
        that ever failed, or the child re-parented itself, `os.getpgid(pid)` would
        be *our* group and `killpg` would send SIGKILL to the shell itself.
        """
        try:
            return os.getpgid(child.pid) == child.pid
        except (OSError, AttributeError):  # AttributeError: no getpgid on Windows
            return False

    def _signal_tree(self, child: subprocess.Popen, *, force: bool) -> bool:
        """Signal the backend and everything it started. Returns whether it worked.

        `force=False` is the polite request (SIGTERM / `taskkill` without `/F`),
        which matters because a backend that exits promptly on SIGTERM would
        otherwise leave *its own* children — a dev server started through
        `process_start`, a watcher — holding the port and the SQLite lock. Killing
        the tree is the point of owning the process, not an escalation footnote.
        """
        try:
            if os.name == "nt":
                # argv list, no shell. `TerminateProcess` (what Popen.kill does)
                # kills one process and orphans the rest, hence taskkill /T.
                argv = ["taskkill", "/PID", str(child.pid), "/T"] + (["/F"] if force else [])
                result = subprocess.run(  # noqa: S603 - fixed argv, no user input
                    argv,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW,
                    timeout=15,
                    check=False,
                )
                return result.returncode == 0
            if not self._is_our_group_leader(child):
                return False
            os.killpg(os.getpgid(child.pid), signal.SIGKILL if force else signal.SIGTERM)
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    def shutdown(self) -> None:
        """Terminate the child — but only if this process started it.

        Bounded at ``SHUTDOWN_GRACE_S + KILL_WAIT_S`` and exception-free by
        contract: this runs from the window-close handler, and a raise here used
        to surface as a traceback during shutdown (``child.wait`` after the kill
        was unguarded, so a `TimeoutExpired` escaped into `_on_closed`).
        """
        child, self._child = self._child, None
        if child is None or not self._spawned_by_us:
            self._spawned_by_us = False
            return
        self._spawned_by_us = False
        if child.poll() is not None:
            return
        logger.info("stopping backend pid=%s (and its tree)", child.pid)
        try:
            if not self._signal_tree(child, force=False):
                child.terminate()  # the group was unavailable: at least take the child
            try:
                child.wait(timeout=SHUTDOWN_GRACE_S)
            except subprocess.TimeoutExpired:
                logger.warning("backend pid=%s ignored terminate; killing its tree", child.pid)
                if not self._signal_tree(child, force=True):
                    child.kill()
                try:
                    child.wait(timeout=KILL_WAIT_S)
                except subprocess.TimeoutExpired:  # pragma: no cover - unkillable
                    logger.warning(
                        "backend pid=%s did not exit %.0fs after a tree kill; abandoning it",
                        child.pid,
                        KILL_WAIT_S,
                    )
        except OSError as exc:  # pragma: no cover - process already gone
            logger.warning("error stopping backend: %s", exc)

    def __enter__(self) -> "BackendProcess":
        self.ensure_running()
        return self

    def __exit__(self, *exc_info) -> None:
        self.shutdown()
