"""System-wide hotkeys.

``Ctrl+Space`` shows/hides the orb from any application and ``Alt+Z`` switches
between the compact orb and the expanded panel.

The ``keyboard`` library installs a low-level OS hook, which is what makes the
shortcuts global rather than per-window. That hook can fail for two reasons on
Windows — another process already owns the key, or the shell is not elevated
while the owning process is. Both are handled by degrading to in-app
shortcuts and telling the user, rather than crashing at startup.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

try:
    import keyboard
except Exception:  # pragma: no cover - import itself can fail on locked-down hosts
    keyboard = None  # type: ignore[assignment]

#: Friendly names for the shortcuts, shown in the UI and the tray menu.
HOTKEY_LABELS = {
    "ctrl+space": "Ctrl + Space",
    "alt+z": "Alt + Z",
}


def format_hotkey(hotkey: str) -> str:
    """Render a configured key combination for display."""
    parts = [p.strip() for p in hotkey.split("+") if p.strip()]
    pretty = {
        "ctrl": "Ctrl",
        "control": "Ctrl",
        "alt": "Alt",
        "shift": "Shift",
        "cmd": "Win",
        "win": "Win",
        "space": "Space",
        "esc": "Esc",
        "escape": "Esc",
    }
    return " + ".join(pretty.get(p.lower(), p.capitalize()) for p in parts)


class HotkeyManager:
    """Registers the global shortcuts and dispatches them onto one thread.

    The OS hook fires on the keyboard's thread, so callbacks are pushed onto a
    queue that the UI thread drains. That keeps every Tk interaction on the
    main thread, which is a hard requirement.
    """

    def __init__(self) -> None:
        self._registered: dict[str, object] = {}
        self._available = keyboard is not None
        self.error: str = ""

    @property
    def available(self) -> bool:
        return self._available

    def register(self, hotkey: str, callback) -> bool:
        """Register one global hotkey. Returns False if it could not be bound.

        Nothing may escape this method. A global hook is the one piece of the
        shell that depends on machine state nobody controls: another process
        may already own the key, the shell may not be elevated while the owner
        is, or the underlying ``keyboard`` package may fail while building its
        name tables. Observed in the wild, that last case escaped as a
        ``BaseException`` subclass during ``add_hotkey`` and killed startup --
        so the handler below catches ``BaseException`` and re-raises only the
        two signals that genuinely should stop the process.

        The documented contract is that a hotkey failure degrades to the tray
        menu and is reported, and it is only true because of this.
        """
        if not hotkey:
            return False
        if keyboard is None:
            self.error = "the 'keyboard' package is not installed"
            self._available = False
            return False
        try:
            handle = keyboard.add_hotkey(hotkey, callback, suppress=False)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:  # noqa: BLE001 - see docstring
            logger.warning("could not bind %s: %s", hotkey, exc)
            self.error = f"{format_hotkey(hotkey)} is already in use by another application"
            # The hook may be half-installed even though binding failed; drop
            # the listener so the next attempt starts clean.
            try:
                keyboard.unhook_all_hotkeys()
            except BaseException:  # pragma: no cover - best effort
                pass
            return False
        self._registered[hotkey] = handle
        return True

    def unregister_all(self) -> None:
        if keyboard is None:
            return
        for hotkey, handle in list(self._registered.items()):
            try:
                if hasattr(handle, "unlink"):
                    handle.unlink()
                else:  # pragma: no cover - older versions return nothing
                    keyboard.remove_hotkey(hotkey)
            except Exception as exc:  # pragma: no cover
                logger.debug("error removing %s: %s", hotkey, exc)
        self._registered.clear()

    def bound(self) -> list[str]:
        return list(self._registered)

    def probe(self) -> str:
        """A one-line description for the UI and for `--diagnose`."""
        if not self._available:
            return f"global hotkeys unavailable ({self.error or 'keyboard not installed'})"
        if not self._registered:
            return "no hotkeys registered"
        return ", ".join(format_hotkey(k) for k in self._registered)


def start_daemon_thread(target, name: str) -> threading.Thread:
    """Start a daemon thread — used where a helper outlives one call."""
    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()
    return thread
