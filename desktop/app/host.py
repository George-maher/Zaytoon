"""The Zaytoon desktop host — a pywebview window that loads the React app.

This replaces the previous Tk/customTkinter shell. The host:

1. Creates a native window via pywebview (WebView2 on Windows).
2. Loads the React production build (or Vite dev server in dev mode).
3. Exposes a native bridge for OS integration.
4. Manages the backend process lifecycle.
5. Handles window state persistence.
6. Enforces single-instance operation.

The React app is the primary UI. The host is a thin native shell.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path

from .backend import BackendProcess, backend_is_up
from .bridge import NativeBridge
from .config import DesktopConfig

logger = logging.getLogger(__name__)

#: Default window dimensions
DEFAULT_WINDOW_WIDTH = 1280
DEFAULT_WINDOW_HEIGHT = 800
DEFAULT_WINDOW_MIN_WIDTH = 800
DEFAULT_WINDOW_MIN_HEIGHT = 600

#: Default seconds between backend health samples. `DesktopConfig
#: .health_check_interval_s` (ZAYTOON_HEALTH_INTERVAL) overrides it; the host used
#: to halve the interval by checking whether `pytest` was in `sys.modules`, which
#: made production code branch on the identity of its own interpreter.
HEALTH_CHECK_INTERVAL = 5.0

#: Ports the desktop's own asset server may bind in production mode.
#:
#: pywebview picks a *random* port when it serves local files, and a random
#: origin is not one the backend's CORS allowlist recognises — every REST
#: call from the page (reminders, health, settings) would then be rejected
#: while the UI still looked fine. The backend allowlists ``517\d+`` on
#: purpose, so the desktop picks from this range instead of taking whatever
#: port is free. ``ZAYTOON_DESKTOP_PORT`` overrides it (and must sit inside
#: the same range, or CORS must be widened to match).
DESKTOP_PORT_RANGE = range(5175, 5180)


def pick_asset_server_port() -> int | None:
    """Return a free port from ``DESKTOP_PORT_RANGE``, or ``None`` if all are taken.

    The port must be verified as bindable *before* it is handed to pywebview:
    a busy port makes bottle fail inside its thread, which leaves pywebview
    reporting a healthy address while nothing is listening — a blank window
    with no error anywhere.
    """
    override = os.environ.get("ZAYTOON_DESKTOP_PORT")
    if override:
        try:
            candidates = [int(override)]
        except ValueError:
            logger.warning("ZAYTOON_DESKTOP_PORT=%r is not a port number", override)
            candidates = list(DESKTOP_PORT_RANGE)
    else:
        candidates = list(DESKTOP_PORT_RANGE)

    for port in candidates:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                logger.warning("desktop asset port %s is busy", port)
                continue
        return port
    return None

#: The EdgeUpdate client id of the WebView2 Evergreen runtime.
WEBVIEW2_CLIENT_ID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

#: Where a missing runtime can be obtained (shown by --diagnose and on failure).
WEBVIEW2_DOWNLOAD_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"


def webview2_runtime_present() -> bool:
    """True when the WebView2 runtime is available (or the platform needs none).

    Windows needs the WebView2 Evergreen runtime before pywebview can open a
    window. It installs per-machine, per-user, or as 32-/64-bit registry views,
    so all of them are consulted; a ``pv`` of ``0.0.0.0`` means uninstalled.
    A fixed-path install is caught by the ``Application`` directory fallback.

    On every other platform pywebview uses the system WebKit, which has no
    separate runtime, so the answer is True and nothing here applies.
    """
    if sys.platform != "win32":
        return True

    try:
        import winreg
    except ImportError:
        return False

    roots = (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER)
    suffix = rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_ID}"
    wow = rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_ID}"

    for root in roots:
        for subkey in (wow, suffix):
            try:
                with winreg.OpenKey(root, subkey) as key:
                    value, _ = winreg.QueryValueEx(key, "pv")
            except OSError:
                continue
            if value and value != "0.0.0.0":
                return True

    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(env)
        if not base:
            continue
        app_dir = Path(base) / "Microsoft" / "EdgeWebView" / "Application"
        try:
            if app_dir.is_dir() and any(app_dir.iterdir()):
                return True
        except OSError:
            continue
    return False


class WebViewHost:
    """Manages the pywebview window, native bridge, and backend process.

    Usage::

        config = DesktopConfig()
        host = WebViewHost(config)
        host.run()  # blocks until the window is closed
    """

    def __init__(self, config: DesktopConfig) -> None:
        self.config = config
        self.backend = BackendProcess(config)
        self.bridge = NativeBridge(config, self.backend)
        self.window = None
        self._shutting_down = False
        self._started = False
        self._health_check_thread: threading.Thread | None = None
        #: The last sampled backend state. A bool assignment, so reading it from
        #: the UI thread needs no lock.
        self.backend_up: bool = False
        self._window_state = self._load_window_state()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(self) -> int:
        """Start the host. Blocks until the window is closed.

        Re-entry is refused rather than tolerated: a second call would attach a
        second set of window handlers and start a second health thread on the same
        host, and `closed` would then fire each handler twice.
        """
        if self._started:
            raise RuntimeError("WebViewHost.run() is single-shot; construct a new host to retry")
        self._started = True

        if not webview2_runtime_present():
            message = (
                "The WebView2 runtime is not installed, so the Zaytoon window "
                "cannot open.\n"
                f"  Install it once from {WEBVIEW2_DOWNLOAD_URL}\n"
                "  then start Zaytoon again. (Run 'main.py --diagnose' to re-check.)"
            )
            logger.error("webview2 runtime missing")
            print(message, file=sys.stderr)
            return 1

        # From the `import webview` to the end of the window loop, everything is inside a
        # try/finally. An `import webview` that fails (no GTK/WebKit on the box),
        # a `create_window` that fails (no display, or a Wayland/X11 mismatch), or
        # a `webview.start()` that raises used to leave the backend we had *just*
        # started running with nothing owning it — and with the single-instance
        # guard released by `main()`, so the next launch attached to that orphan
        # and reported nothing about how the previous one died.
        try:
            # The import is inside the guard on purpose: on a Linux box without
            # GTK/WebKit this is the failure that actually happens, and a test of
            # the guard below caught the first version orphaning the backend here.
            import webview

            # Start backend before showing the window
            status = self.backend.ensure_running()
            if not status.running:
                logger.warning("backend not running: %s", status.hint)

            # Determine URL (dev mode or production)
            url = self._resolve_url()

            # Create the window
            self.window = webview.create_window(
                title="Zaytoon",
                url=url,
                js_api=self.bridge,
                width=self._window_state.get("width", DEFAULT_WINDOW_WIDTH),
                height=self._window_state.get("height", DEFAULT_WINDOW_HEIGHT),
                min_size=(
                    self._window_state.get("min_width", DEFAULT_WINDOW_MIN_WIDTH),
                    self._window_state.get("min_height", DEFAULT_WINDOW_MIN_HEIGHT),
                ),
            )

            # Give the bridge a reference to the window
            self.bridge.set_window(self.window)

            # Set up event handlers
            self.window.events.closed += self._on_closed
            self.window.events.loaded += self._on_loaded
            self.window.events.minimized += self._on_minimized
            self.window.events.maximized += self._on_maximized
            self.window.events.restored += self._on_restored

            # Start health check thread
            self._start_health_check()

            # Start the webview event loop. In production pywebview serves the
            # dist itself; give it a CORS-allowlisted port (see
            # DESKTOP_PORT_RANGE) so REST calls from the page origin work.
            logger.info("starting webview with url=%s", url)
            http_port = pick_asset_server_port()
            if http_port is None:
                logger.warning(
                    "no free desktop asset port in %s — the page will load from a "
                    "random origin and REST calls will be blocked by CORS "
                    "(WebSocket is unaffected)",
                    list(DESKTOP_PORT_RANGE),
                )
                webview.start()
            else:
                logger.info("desktop asset server port=%s", http_port)
                webview.start(http_port=http_port)

            return 0
        finally:
            self._teardown()

    def _teardown(self) -> None:
        """Stop the health thread and the backend — once, whatever the reason."""
        self._shutting_down = True
        thread, self._health_check_thread = self._health_check_thread, None
        if thread is not None and thread is not threading.current_thread():
            # The loop sleeps in slices and checks the flag, so this returns
            # promptly. An unbounded join here would hang shutdown on a wedged
            # UI call, which is the failure this refactor is about avoiding.
            thread.join(timeout=2.0)
        self.backend.shutdown()

    def _resolve_url(self) -> str:
        """Determine the URL to load (dev mode or production)."""
        if self.config.dev_mode:
            return f"http://localhost:{self.config.dev_port}"

        dist = self.config.frontend_dist
        index = dist / "index.html"
        if index.exists():
            # Use file:// URL for local files
            return str(index)
        else:
            # Fallback to dev mode if no production build exists
            logger.warning(
                "no production build found at %s, falling back to dev mode", dist
            )
            return f"http://localhost:{self.config.dev_port}"

    # ------------------------------------------------------------------
    # Window event handlers
    # ------------------------------------------------------------------

    def _on_closed(self) -> None:
        """Handle window close."""
        self._save_window_state()
        # `_teardown` is idempotent, and `run()`'s finally will call it again on
        # the way out: the close handler is not the only path that ends a session.
        self._teardown()
        logger.info("window closed, shutting down")

    def _on_loaded(self) -> None:
        """Handle page load complete."""
        logger.info("page loaded")

    def _on_minimized(self) -> None:
        """Handle window minimize."""
        pass

    def _on_maximized(self) -> None:
        """Handle window maximize."""
        pass

    def _on_restored(self) -> None:
        """Handle window restore."""
        pass

    # ------------------------------------------------------------------
    # Window state persistence
    # ------------------------------------------------------------------

    def _get_state_file(self) -> Path:
        """The window state file: `ZAYTOON_HOME` now, else the resolved state dir.

        `ZAYTOON_HOME` is read at call time, not baked into the config, because
        `DesktopConfig` is built before the entry point applies its own overrides
        and the shell's tests point it at a temp dir per case.

        The fallback used to be `<repo>/Config` unconditionally, which is a silent
        failure in an installed application: `Program Files` is not writable by a
        standard user, `write_text` raised, the exception went into a warning log,
        and the window simply stopped remembering its size. `default_state_dir()`
        keeps `<repo>/Config` while it *is* writable — an upgrade must not move a
        user's saved geometry — and otherwise falls back to the per-OS location.
        """
        override = os.environ.get("ZAYTOON_HOME")
        directory = Path(override).expanduser() if override else Path(self.config.state_dir)
        return directory / "window_state.json"

    def _geometry(self, name: str, fallback):
        """Read one window property, tolerating a window that is going away.

        pywebview's ``width``/``height``/``x``/``y`` getters unpack the result
        of ``gui.get_size()``, which returns ``None`` once the native window
        is gone — during ``closed`` that raises ``TypeError: cannot unpack
        non-iterable NoneType`` and the state was never saved at all. A field
        that cannot be read falls back to the value loaded at startup.
        """
        try:
            value = getattr(self.window, name)
        except Exception:
            return fallback
        return fallback if value is None else value

    def _save_window_state(self) -> None:
        """Save the window state to a JSON file."""
        if self.window is None:
            return
        try:
            previous = self._window_state
            state = {
                "width": self._geometry("width", previous.get("width", DEFAULT_WINDOW_WIDTH)),
                "height": self._geometry("height", previous.get("height", DEFAULT_WINDOW_HEIGHT)),
                "x": self._geometry("x", previous.get("x", 0)),
                "y": self._geometry("y", previous.get("y", 0)),
                "maximized": bool(
                    self._geometry("maximized", previous.get("maximized", False))
                ),
                "min_width": DEFAULT_WINDOW_MIN_WIDTH,
                "min_height": DEFAULT_WINDOW_MIN_HEIGHT,
            }
            state_file = self._get_state_file()
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps(state), encoding="utf-8")
        except Exception as exc:
            logger.warning("could not save window state: %s", exc)

    def _load_window_state(self) -> dict:
        """Load the window state from a JSON file."""
        state_file = self._get_state_file()
        if state_file.exists():
            try:
                return json.loads(state_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def _start_health_check(self) -> None:
        """Start the backend health check thread."""
        self._health_check_thread = threading.Thread(
            target=self._health_check_loop,
            name="zaytoon-health-check",
            daemon=True,
        )
        self._health_check_thread.start()

    def _health_check_loop(self) -> None:
        """Sample backend health for the host's own diagnostics.

        Two problems with the previous version. It called `window.evaluate_js`
        from this thread — pywebview marshals nothing, so a UI call from a foreign
        thread on the GTK (Linux) or Cocoa (macOS) backend is a crash or a hang
        waiting to happen, and it survived on Windows only because WebView2
        tolerates it. And the event it dispatched, `backend-health`, has no
        listener anywhere in the frontend: the page learned nothing, while the React
        app already polls `/health` for its disconnected state. The sample stays
        (it is what `diagnose()` and the log show); the cross-thread JS push goes.

        The sleep is sliced so shutdown is observed within about half a second
        instead of up to a whole interval after the window is already gone.
        """
        interval = max(0.5, float(self.config.health_check_interval_s))
        while not self._shutting_down:
            try:
                self.backend_up = backend_is_up(self.config.host, self.config.port)
                if not self.backend_up:
                    logger.debug("health sample: backend down at %s", self.config.backend_addr)
            except Exception as exc:  # noqa: BLE001 - a probe must not kill the loop
                logger.debug("health check failed: %s", exc)
            waited = 0.0
            while not self._shutting_down and waited < interval:
                time.sleep(min(0.5, interval - waited))
                waited += 0.5

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def diagnose(self) -> dict:
        """Return diagnostic information about the host."""
        return {
            "config": {
                "repo_root": str(self.config.repo_root),
                "backend_addr": self.config.backend_addr,
                "ws_url": self.config.ws_url,
                "dev_mode": self.config.dev_mode,
                "dev_port": self.config.dev_port,
                "frontend_dist": str(self.config.frontend_dist),
            },
            "backend": self.backend.status().as_dict(),
            "window_state": self._window_state,
            "url": self._resolve_url(),
            "backend_up": self.backend_up,
            "state_file": str(self._get_state_file()),
        }
