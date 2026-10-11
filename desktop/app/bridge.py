"""The native bridge between React and the desktop host.

Every method is explicitly registered. No arbitrary Python execution is exposed
to JavaScript — the bridge is a closed set of operations, each with a clear
purpose and explicit parameters.

Security invariants:
- No method accepts a Python expression or callable.
- No method spawns a shell or interprets a command string.
- Destructive operations (file deletion, etc.) are NOT here — they go through
  the backend's gated tool layer, which has its own confirmation flow.
- Every method returns a JSON-serialisable value or ``None``.
"""

from __future__ import annotations

import base64
import logging
import os
import platform
import subprocess
import sys
from pathlib import Path

from . import paths as path_policy
from .backend import BackendProcess
from .config import DesktopConfig

logger = logging.getLogger(__name__)


def _ps_single_quote(value: str) -> str:
    """Escape a value for a single-quoted PowerShell string literal.

    PowerShell's only escape inside ``'...'`` is a doubled quote, so every
    other character (including ``$``, backticks and double quotes) is already
    literal.
    """
    return value.replace("'", "''")


def _win_clipboard_api():
    """Declare the Win32 signatures the clipboard calls need.

    Without ``argtypes`` ctypes narrows every argument to ``c_int``, so a
    64-bit handle raises ``OverflowError`` — which the blanket ``except`` in
    the callers turns into a silent empty clipboard. ``wintypes`` has no
    ``SIZE_T`` either, which is why the sizes use ``c_size_t``.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32

    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = wintypes.HGLOBAL
    user32.SetClipboardData.argtypes = [ctypes.c_uint, wintypes.HGLOBAL]
    user32.SetClipboardData.restype = wintypes.HGLOBAL
    return user32, kernel32


class NativeBridge:
    """Explicit bridge between React and the desktop host.

    The host creates this object and passes it to ``webview.create_window`` as
    the ``js_api`` parameter. Every public method is callable from JavaScript
    via ``window.pywebview.api.<method>(...)``.

    The bridge holds a reference to the window, set by the host after creation,
    so that window controls (minimize, maximize, close) can be invoked from
    JavaScript.
    """

    def __init__(self, config: DesktopConfig, backend: BackendProcess) -> None:
        self.config = config
        self.backend = backend
        self._window = None

    def set_window(self, window) -> None:
        """Set the window reference (called by the host after creation)."""
        self._window = window

    # ------------------------------------------------------------------
    # Window controls
    # ------------------------------------------------------------------

    def minimize(self) -> None:
        """Minimize the window to the taskbar."""
        if self._window is not None:
            self._window.minimize()

    def maximize(self) -> None:
        """Maximize the window."""
        if self._window is not None:
            self._window.maximize()

    def restore(self) -> None:
        """Restore the window from minimized or maximized state."""
        if self._window is not None:
            self._window.restore()

    def close(self) -> None:
        """Close the window and exit the application."""
        if self._window is not None:
            self._window.destroy()

    def is_maximized(self) -> bool:
        """Return True when the window is maximized."""
        if self._window is not None:
            return bool(getattr(self._window, "maximized", False))
        return False

    def set_always_on_top(self, on_top: bool) -> None:
        """Set whether the window stays on top of other windows."""
        if self._window is not None:
            try:
                self._window.on_top = on_top
            except Exception:
                logger.debug("set_always_on_top not supported on this platform")

    def get_window_state(self) -> dict:
        """Return the current window geometry and state."""
        if self._window is None:
            return {}
        return {
            "width": getattr(self._window, "width", 0),
            "height": getattr(self._window, "height", 0),
            "x": getattr(self._window, "x", 0),
            "y": getattr(self._window, "y", 0),
            "maximized": bool(getattr(self._window, "maximized", False)),
        }

    # ------------------------------------------------------------------
    # File system operations
    # ------------------------------------------------------------------

    def open_folder_dialog(self) -> str | None:
        """Open a native folder picker and return the selected path.

        Returns ``None`` when the user cancels the dialog.
        """
        if self._window is None:
            return None
        try:
            import webview

            result = self._window.create_file_dialog(
                webview.FOLDER_DIALOG,
                allow_multiple=False,
            )
            if result and len(result) > 0:
                return str(result[0])
        except Exception as exc:
            logger.warning("folder dialog failed: %s", exc)
        return None

    def open_file_dialog(self, file_types: str = "") -> str | None:
        """Open a native file picker and return the selected path.

        ``file_types`` is a semicolon-separated list of extensions, e.g.
        ``".py;.js;.txt"``. Returns ``None`` when the user cancels.
        """
        if self._window is None:
            return None
        try:
            import webview

            types = tuple(file_types.split(";")) if file_types else ()
            result = self._window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=types,
            )
            if result and len(result) > 0:
                return str(result[0])
        except Exception as exc:
            logger.warning("file dialog failed: %s", exc)
        return None

    def reveal_in_os(self, path: str) -> bool:
        """Reveal a file or folder in the OS file manager.

        Returns True on success, False on failure.
        """
        if not path:
            return False
        # The path is the user's (or the page's) input to a *file manager* call,
        # so containment is checked but no executable rule is needed here: nothing
        # this branch runs can execute the target, it only shows its parent.
        allowed, reason = path_policy.classify(path, self.config)
        if not allowed:
            # Same policy as `open_path`, and the reason goes to the log only: a
            # bool across the bridge cannot tell an attacker whether a file exists.
            logger.warning("reveal_in_os refused %s: %s", path, reason)
            return False
        p = Path(path).resolve()
        try:
            if sys.platform == "win32":
                # explorer /select,"path" opens the parent with the item selected
                if p.is_dir():
                    subprocess.Popen(["explorer", str(p)], shell=False)
                else:
                    subprocess.Popen(["explorer", "/select,", str(p)], shell=False)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(p)], shell=False)
            else:
                # Linux: xdg-open the parent directory
                parent = p.parent if p.is_file() else p
                subprocess.Popen(["xdg-open", str(parent)], shell=False)
            return True
        except Exception as exc:
            logger.warning("reveal_in_os failed for %s: %s", path, exc)
            return False

    def open_path(self, path: str) -> bool:
        """Open a file or folder with the default OS application.

        Returns True on success, False on failure.
        """
        if not path:
            return False
        # `os.startfile` / `open` / `xdg-open` *execute* what they are given when
        # the OS thinks the type is a program, so this is the sink that needs both
        # checks: inside a granted root, and not something runnable. See
        # `app/paths.py`.
        allowed, reason = path_policy.classify(path, self.config)
        if not allowed:
            logger.warning("open_path refused %s: %s", path, reason)
            return False
        p = Path(path).resolve()
        try:
            if sys.platform == "win32":
                os.startfile(str(p))  # noqa: S606 - os.startfile is the Windows API
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(p)], shell=False)
            else:
                subprocess.Popen(["xdg-open", str(p)], shell=False)
            return True
        except Exception as exc:
            logger.warning("open_path failed for %s: %s", path, exc)
            return False

    # ------------------------------------------------------------------
    # System information
    # ------------------------------------------------------------------

    def get_system_info(self) -> dict:
        """Return system information as a JSON-serialisable dict."""
        return {
            "platform": sys.platform,
            "os": platform.system(),
            "os_version": platform.version(),
            "architecture": platform.machine(),
            "processor": platform.processor(),
            "python_version": platform.python_version(),
            "hostname": platform.node(),
        }

    def get_app_path(self) -> str:
        """Return the application root directory."""
        return str(self.config.repo_root)

    def get_backend_url(self) -> str:
        """Return the backend WebSocket URL."""
        return self.config.ws_url

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    def notify(self, title: str, message: str) -> bool:
        """Show a native OS notification.

        Returns True on success, False on failure.
        """
        try:
            if sys.platform == "win32":
                self._notify_windows(title, message)
            elif sys.platform == "darwin":
                self._notify_macos(title, message)
            else:
                self._notify_linux(title, message)
            return True
        except Exception as exc:
            logger.warning("notify failed: %s", exc)
            return False

    def _notify_windows(self, title: str, message: str) -> None:
        """Windows notification.

        MessageBoxW (a direct ctypes call) is the primary path. The PowerShell
        fallback passes the script as ``-EncodedCommand`` — base64 UTF-16LE —
        so no user text ever appears on a command line, and the strings are
        embedded in single-quoted PowerShell literals with ``'`` doubled. A
        raw ``-Command f'...{message}...'`` would let a quote in a
        notification break out and run arbitrary PowerShell, which is exactly
        what this bridge promises not to expose.
        """
        try:
            import ctypes

            # Use MessageBoxW as a simple, dependency-free notification
            # For toast notifications, win10toast would be needed but adds a dependency
            ctypes.windll.user32.MessageBoxW(  # noqa: SLF001
                0, message, title, 0x40 | 0x1000  # MB_ICONINFORMATION | MB_SYSTEMMODAL
            )
        except Exception:
            script = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "[System.Windows.Forms.MessageBox]::Show("
                f"'{_ps_single_quote(message)}', '{_ps_single_quote(title)}')"
            )
            encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            subprocess.Popen(
                ["powershell", "-NoProfile", "-EncodedCommand", encoded],
                shell=False,
            )

    def _notify_macos(self, title: str, message: str) -> None:
        """macOS notification via osascript.

        The strings are escaped for AppleScript literal syntax, so a quote in
        the message cannot end the string and turn the rest into code.
        """
        def _s(value: str) -> str:
            return value.replace("\\", "\\\\").replace('"', '\\"')

        subprocess.Popen(
            [
                "osascript",
                "-e",
                f'display notification "{_s(message)}" with title "{_s(title)}"',
            ],
            shell=False,
        )

    def _notify_linux(self, title: str, message: str) -> None:
        """Linux notification via notify-send."""
        subprocess.Popen(["notify-send", title, message], shell=False)

    # ------------------------------------------------------------------
    # External URLs
    # ------------------------------------------------------------------

    def open_url(self, url: str) -> bool:
        """Open a URL in the default browser.

        Returns True on success, False on failure.
        """
        if not url:
            return False
        allowed, reason = path_policy.classify_url(url)
        if not allowed:
            logger.warning("open_url refused %r: %s", url, reason)
            return False
        try:
            import webbrowser

            webbrowser.open(url)
            return True
        except Exception as exc:
            logger.warning("open_url failed for %s: %s", url, exc)
            return False

    # ------------------------------------------------------------------
    # Backend management
    # ------------------------------------------------------------------

    def get_backend_status(self) -> dict:
        """Return the backend status as a JSON-serialisable dict."""
        return self.backend.status().as_dict()

    def restart_backend(self) -> dict:
        """Restart the backend process.

        Returns the new backend status.
        """
        self.backend.shutdown()
        return self.backend.ensure_running().as_dict()

    # ------------------------------------------------------------------
    # Clipboard
    # ------------------------------------------------------------------

    def get_clipboard(self) -> str:
        """Get the clipboard text. Returns empty string on failure."""
        try:
            if sys.platform == "win32":
                import ctypes

                user32, kernel32 = _win_clipboard_api()
                cf_unicode_text = 13  # CF_UNICODETEXT: the buffer holds UTF-16
                if not user32.OpenClipboard(0):
                    return ""
                try:
                    handle = user32.GetClipboardData(cf_unicode_text)
                    if not handle:
                        return ""
                    ptr = kernel32.GlobalLock(handle)
                    if not ptr:
                        return ""
                    try:
                        return ctypes.wstring_at(ptr)
                    finally:
                        kernel32.GlobalUnlock(handle)
                finally:
                    user32.CloseClipboard()
            elif sys.platform == "darwin":
                result = subprocess.run(
                    ["pbpaste"], capture_output=True, text=True, check=False
                )
                return result.stdout
            else:
                # Linux: try xclip first, then xsel
                for cmd in (["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"]):
                    try:
                        result = subprocess.run(
                            cmd, capture_output=True, text=True, check=False
                        )
                        if result.returncode == 0:
                            return result.stdout
                    except FileNotFoundError:
                        continue
                return ""
        except Exception as exc:
            logger.warning("get_clipboard failed: %s", exc)
            return ""

    def set_clipboard(self, text: str) -> bool:
        """Set the clipboard text. Returns True on success."""
        try:
            if sys.platform == "win32":
                import ctypes

                user32, kernel32 = _win_clipboard_api()
                cf_unicode_text = 13  # CF_UNICODETEXT — matches get_clipboard
                data = text.encode("utf-16-le") + b"\x00\x00"
                if not user32.OpenClipboard(0):
                    return False
                try:
                    user32.EmptyClipboard()
                    handle = kernel32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
                    if not handle:
                        return False
                    ptr = kernel32.GlobalLock(handle)
                    if not ptr:
                        kernel32.GlobalFree(handle)
                        return False
                    # The text must actually be written into the buffer —
                    # without this the clipboard receives uninitialised memory.
                    ctypes.memmove(ptr, data, len(data))
                    kernel32.GlobalUnlock(handle)
                    if not user32.SetClipboardData(cf_unicode_text, handle):
                        kernel32.GlobalFree(handle)
                        return False
                    # Success: the system owns the handle and frees it later.
                    return True
                finally:
                    user32.CloseClipboard()
            elif sys.platform == "darwin":
                subprocess.run(["pbcopy"], input=text, text=True, check=False)
                return True
            else:
                for cmd in (["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
                    try:
                        result = subprocess.run(
                            cmd, input=text, text=True, check=False
                        )
                        if result.returncode == 0:
                            return True
                    except FileNotFoundError:
                        continue
                return False
        except Exception as exc:
            logger.warning("set_clipboard failed: %s", exc)
            return False
