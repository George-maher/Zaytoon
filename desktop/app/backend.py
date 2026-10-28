"""Manage the FastAPI backend as a child process.

This is a direct port of the behaviour the Rust shell used to provide, and it
keeps the two safety properties that mattered most:

1. **Never touch a backend we did not start.** If something is already
   answering ``/health`` on the port, the shell uses it and leaves it alone. The
   child handle is only ever set when *we* spawn, and only that handle is killed.
2. **Resolve the backend deliberately.** An explicit
   ``ZAYTOON_BACKEND_PYTHON`` wins, then the **bundled** backend (a frozen
   build runs ``Zaytoon.exe --serve`` — the same executable, backend-worker
   mode, so an installed app never needs a second Python or a checkout), then
   ``backend/.venv``, then the interpreter running this app. See
   ``resolve_backend_command``.

**Ready is a health answer, not a process.** A spawned child is only reported
running after ``GET /health`` returns 200 — a TCP accept proves something is
listening, not that it is the Zaytoon backend. A port that accepts connections
but never answers ``/health`` is reported as a port conflict, not as a slow
start, and the child's output is captured to a log file so a real startup
failure has an actionable message instead of a guess.

Ownership is the whole design: a child we started is killed *as a tree*, because
``uvicorn`` is not the only process involved — a dev server or watcher started
through ``process_start`` is a grandchild of the shell, and terminating only the
direct child leaves it holding the port and the database lock. A backend we did
not start is never signalled at all.
"""

from __future__ import annotations

import json
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

#: One ``GET /health`` probe is bounded well below the spawn timeout: the
#: endpoint is served by the app itself, so a slow answer means the backend is
#: busy or broken, not that the network is.
HEALTH_PROBE_TIMEOUT_S = 3.0

#: How many lines of the captured backend log a failure hint carries.
LOG_TAIL_LINES = 40

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


def backend_health(
    host: str, port: int, timeout: float = HEALTH_PROBE_TIMEOUT_S
) -> dict | None:
    """``GET /health`` as a readiness probe. Parsed JSON on HTTP 200, else ``None``.

    This is what "the backend is up" means everywhere in this module. A TCP
    accept only proves *something* is listening on the port — it can be a
    leftover dev server, an unrelated application, or a half-started uvicorn —
    and reporting any of those as a ready backend is how a blank window ends up
    with a green status.

    Uses only the standard library on purpose: this module runs inside the
    desktop shell, whose bundle must not gain an HTTP-client dependency for one
    GET request. Every failure class (refused, timeout, non-200, malformed
    body) collapses to ``None``: the caller wants a boolean fact, and a probe
    must never raise into a window-close handler or a health thread.
    """
    url = f"http://{host}:{port}/health"
    try:
        from urllib.request import urlopen

        with urlopen(url, timeout=timeout) as response:  # noqa: S310 - scheme is http, host/port from config
            if response.status != 200:
                return None
            body = response.read(64 * 1024)
        parsed = json.loads(body.decode("utf-8", "replace"))
        return parsed if isinstance(parsed, dict) else {"status": "ok"}
    except Exception:  # noqa: BLE001 - a probe collapses every failure to "not healthy"
        return None


def wait_for_ready(
    host: str, port: int, timeout: float, interval: float = 0.4
) -> bool:
    """Poll until ``/health`` answers 200 or ``timeout`` elapses.

    The readiness contract behind "the application is ready": a spawned process
    is not a started backend, and the caller (the window's startup path, and
    ``--diagnose --start-backend``) must not claim otherwise.
    """
    deadline = time.monotonic() + timeout
    while True:
        if backend_health(host, port) is not None:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


@dataclass(frozen=True)
class BackendCommand:
    """A resolved decision about how to start the backend.

    ``argv`` is a full argument vector (never a shell string — the spawn site
    passes it to ``Popen`` with no shell), ``cwd`` is the working directory the
    backend must run in, and ``kind`` names the resolution tier so diagnostics
    can say *which* backend an installed application would start:

    ``bundled``  the frozen executable's own backend worker (``--serve``)
    ``override`` an explicit ``ZAYTOON_BACKEND_PYTHON`` (tests, unusual layouts)
    ``venv``     the repository virtualenv (a development checkout)
    ``source``   the running interpreter plus ``backend/main.py``
    """

    argv: tuple[str, ...]
    cwd: str
    kind: str

    def describe(self) -> str:
        return " ".join(self.argv)


def resolve_backend_command(config) -> BackendCommand | None:
    """Return how to start the backend, or ``None`` if that is not possible.

    ``None`` means no usable backend could be found, in which case the caller
    surfaces an actionable message rather than crash.

    Resolution order (priority):

    1. ``ZAYTOON_BACKEND_PYTHON`` env var — explicit override, takes total
       precedence (tests, and pointing the shell at an unusual interpreter).
    2. **Frozen build**: this same executable in backend-worker mode
       (``Zaytoon.exe --serve``). The backend, its Python runtime and every
       dependency are bundled into the executable, so an installed application
       never looks for a checkout, a virtualenv, or a system Python — and there
       is no second artifact whose path could go stale.
    3. The repository virtualenv (``<repo>/backend/.venv``).
    4. The interpreter running this app plus ``backend/main.py``.
    """
    # 1. explicit override, for tests and unusual layouts
    if config.backend_python:
        exe = Path(config.backend_python)
        if exe.exists() or exe.name.lower().startswith("python"):
            main = Path(config.backend_main) if config.backend_main else None
            if main is None:
                # The repo layout is the meaningful answer; the old fallback was
                # a cwd-relative ``backend/main.py``, which silently depended on
                # wherever the shell happened to be launched from.
                root = Path(config.repo_root) if config.repo_root else Path(".")
                main = root / "backend" / "main.py"
                if not main.exists():
                    meipass = Path(sys._MEIPASS) if hasattr(sys, "_MEIPASS") else None
                    if meipass and (meipass / "backend" / "main.py").exists():
                        main = meipass / "backend" / "main.py"
            cwd = Path(config.backend_cwd) if config.backend_cwd else main.parent
            return BackendCommand((str(exe), str(main)), str(cwd), "override")

    # 2. frozen build: the bundled backend worker
    if getattr(sys, "frozen", False):
        cwd = (
            str(config.backend_cwd)
            if config.backend_cwd
            else os.environ.get("ZAYTOON_HOME")
            or str(Path(sys.executable).resolve().parent)
        )
        return BackendCommand((sys.executable, "--serve"), cwd, "bundled")

    # 3. the repository virtualenv
    root = Path(config.repo_root) if config.repo_root else Path(".")
    backend_dir = root / "backend"
    for relative in VENV_PYTHON_CANDIDATES:
        candidate = backend_dir / ".venv" / relative
        if candidate.exists():
            main = Path(config.backend_main) if config.backend_main else backend_dir / "main.py"
            cwd = Path(config.backend_cwd) if config.backend_cwd else backend_dir
            return BackendCommand((str(candidate), str(main)), str(cwd), "venv")

    # 4. the interpreter running this app (handy when launched from a venv)
    main = Path(config.backend_main) if config.backend_main else (backend_dir / "main.py")
    cwd = Path(config.backend_cwd) if config.backend_cwd else backend_dir
    if not main.exists():
        return None
    return BackendCommand((sys.executable, str(main)), str(cwd), "source")


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
        #: The handle capturing the child's stdout/stderr (see `_open_log`).
        self._log_handle = None

    # -- introspection ---------------------------------------------------
    @property
    def managed(self) -> bool:
        return self._spawned_by_us

    @property
    def pid(self) -> int | None:
        return self._child.pid if self._child is not None else None

    def status(self) -> BackendStatus:
        """A health-based status: "running" means ``/health`` answered 200."""
        health = backend_health(self.config.host, self.config.port, timeout=1.5)
        up = health is not None
        hint = "" if up else "Backend is not responding."
        if not up and backend_is_up(self.config.host, self.config.port):
            hint = (
                f"Something is listening on {self.config.backend_addr} but "
                "/health does not answer — the port may be used by another "
                "application."
            )
        return BackendStatus(
            running=up,
            managed=self._spawned_by_us,
            hint=hint,
            address=self.config.backend_addr,
            pid=self.pid,
        )

    # -- backend log ------------------------------------------------------
    def _log_dir(self) -> Path:
        base = os.environ.get("ZAYTOON_HOME") or getattr(
            self.config, "state_dir", Path.home() / ".zaytoon"
        )
        return Path(base).expanduser() / "logs"

    @property
    def log_path(self) -> Path:
        """Where the captured backend stdout/stderr goes.

        A windowed application has no console, so without this a backend that
        dies on startup would leave *nothing* to inspect — which is exactly the
        situation the failure hints point users at.
        """
        return self._log_dir() / "backend.log"

    def _open_log(self):
        """Open the log file for the child's stdout/stderr; ``None`` on failure.

        A log file that cannot be opened is not a reason to refuse to start the
        backend — the child's output is simply lost, which is the old behaviour.
        """
        try:
            directory = self._log_dir()
            directory.mkdir(parents=True, exist_ok=True)
            return open(self.log_path, "ab", buffering=0)  # noqa: SIM115 - closed in shutdown()
        except OSError:
            logger.warning("could not open backend log at %s", self.log_path)
            return None

    def _log_tail(self, lines: int = LOG_TAIL_LINES) -> str:
        """The last lines of the captured backend log, or ``''``."""
        try:
            with open(self.log_path, "rb") as handle:
                content = handle.read(256 * 1024)
        except OSError:
            return ""
        decoded = content.decode("utf-8", "replace").splitlines()
        return "\n".join(decoded[-lines:])

    # -- lifecycle -------------------------------------------------------
    def ensure_running(self) -> BackendStatus:
        """Return a healthy backend, spawning one only if necessary.

        Never raises: on failure the returned status carries a ``hint`` the UI
        can display verbatim, and the tail of the backend log when we have one.

        "Healthy" is a ``GET /health`` answer. A listener that accepts TCP but
        never serves ``/health`` is a **port conflict**, not a starting
        backend, and is reported as such without touching the foreign process.
        """
        cfg = self.config

        if backend_health(cfg.host, cfg.port) is not None:
            if self._child is not None and self._child.poll() is not None:
                # Our child died but something else owns the port now.
                self._child = None
                self._spawned_by_us = False
            return self.status()

        if backend_is_up(cfg.host, cfg.port):
            # Something holds the port but is not the Zaytoon backend. Spawn
            # would only fail to bind (and killing the listener is not ours to
            # do), so this is a stated conflict, not a retry loop.
            if self._child is not None and self._child.poll() is not None:
                self._child = None
                self._spawned_by_us = False
            status = self.status()
            status.hint = (
                f"Port {cfg.port} is in use by another application: it accepts "
                "connections but /health does not answer. Close that "
                "application, or set ZAYTOON_BACKEND_ADDR to a different port."
            )
            return status

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

        env = dict(os.environ)
        env.setdefault("PYTHONUNBUFFERED", "1")
        kwargs: dict = {"cwd": resolved.cwd, "env": env}
        if os.name == "nt":
            kwargs["creationflags"] = CREATE_NO_WINDOW
        else:
            # Its own process group, so `shutdown` can signal the backend *and*
            # whatever the backend started. Without this, SIGTERM reaches uvicorn
            # only and a managed child of the agent outlives the shell.
            kwargs["start_new_session"] = True

        log_handle = self._open_log()
        if log_handle is not None:
            kwargs["stdout"] = log_handle
            kwargs["stderr"] = subprocess.STDOUT

        # Re-check the port right before spawning: between the probe above and
        # here another instance (or a service set to auto-start) may have taken
        # it, and a second uvicorn would just die on the bind error — after
        # which we would report *our* failed start for someone else's port.
        if backend_is_up(cfg.host, cfg.port):
            if log_handle is not None:
                log_handle.close()
            return self.status()

        try:
            self._child = subprocess.Popen(  # noqa: S603 - argv list, no shell
                list(resolved.argv),
                **kwargs,
            )
        except OSError as exc:
            if log_handle is not None:
                log_handle.close()
            logger.error("could not spawn backend with %s: %s", resolved.describe(), exc)
            status = self.status()
            status.hint = f"Failed to start the backend: {exc}"
            return status

        self._spawned_by_us = True
        self._log_handle = log_handle
        logger.info(
            "spawned backend (%s) pid=%s via %s",
            resolved.kind,
            self._child.pid,
            resolved.describe(),
        )

        if not wait_for_ready(
            self.config.host, self.config.port, self.config.backend_timeout
        ):
            tail = self._log_tail()
            # Roll back so a later attempt is not blocked by a half-dead child.
            self.shutdown()
            hint = (
                f"Backend did not become healthy on {self.config.backend_addr} "
                f"within {self.config.backend_timeout:.0f}s."
            )
            if tail:
                hint += f" Last backend log lines ({self.log_path}):\n{tail}"
            else:
                hint += f" Check the backend log: {self.log_path}"
            status = self.status()
            status.hint = hint
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
        except Exception:  # noqa: BLE001 - deliberately broad
            # `shutdown()` is called from a window-close handler and from a
            # `finally`, and its contract is "never raise": a teardown that throws
            # takes the caller's cleanup with it and leaves the backend running.
            # OSError and SubprocessError cover the expected cases; anything else
            # (a stubbed `subprocess` in a test, an unsupported flag, a fake child)
            # is still a reason to give up on signalling, not a reason to propagate.
            logger.debug("tree signalling failed", exc_info=True)
            return False

    def shutdown(self) -> None:
        """Terminate the child — but only if this process started it.

        Bounded at ``SHUTDOWN_GRACE_S + KILL_WAIT_S`` and exception-free by
        contract: this runs from the window-close handler, and a raise here used
        to surface as a traceback during shutdown (``child.wait`` after the kill
        was unguarded, so a `TimeoutExpired` escaped into `_on_closed`).
        """
        child, self._child = self._child, None
        log_handle, self._log_handle = self._log_handle, None
        if child is None or not self._spawned_by_us:
            self._spawned_by_us = False
            if log_handle is not None:
                try:
                    log_handle.close()
                except OSError:  # pragma: no cover - best effort
                    pass
            return
        self._spawned_by_us = False
        try:
            if child.poll() is not None:
                return
            logger.info("stopping backend pid=%s (and its tree)", child.pid)
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
        except Exception as exc:  # noqa: BLE001 - deliberately broad
            # "Exception-free by contract" has to mean every step, not every step
            # we predicted: `poll`/`terminate`/`kill` raise `OSError` when the
            # process is already gone (the expected case) and anything else when the
            # handle itself is unusual, and this runs inside a `finally` and inside
            # the window-close handler, where a raise replaces cleanup with a
            # traceback. A regression test on the Windows leg found the fallback
            # path unguarded.
            logger.warning("error stopping backend: %s", exc)
        finally:
            if log_handle is not None:
                try:
                    log_handle.close()
                except OSError:  # pragma: no cover - best effort
                    pass

    def __enter__(self) -> "BackendProcess":
        self.ensure_running()
        return self

    def __exit__(self, *exc_info) -> None:
        self.shutdown()
