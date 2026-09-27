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
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: ``CREATE_NO_WINDOW`` — keeps a console window from flashing on Windows.
CREATE_NO_WINDOW = 0x08000000

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

    def shutdown(self) -> None:
        """Terminate the child — but only if this process started it."""
        child, self._child = self._child, None
        if child is None or not self._spawned_by_us:
            self._spawned_by_us = False
            return
        self._spawned_by_us = False
        if child.poll() is not None:
            return
        logger.info("stopping backend pid=%s", child.pid)
        try:
            child.terminate()
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                logger.warning("backend pid=%s ignored terminate; killing", child.pid)
                child.kill()
                child.wait(timeout=5)
        except OSError as exc:  # pragma: no cover - process already gone
            logger.warning("error stopping backend: %s", exc)

    def __enter__(self) -> "BackendProcess":
        self.ensure_running()
        return self

    def __exit__(self, *exc_info) -> None:
        self.shutdown()
