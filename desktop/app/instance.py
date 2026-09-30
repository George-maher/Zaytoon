"""Single-instance guard for the desktop shell.

The shell owns two genuinely global resources: the OS keyboard hook for its
hotkeys, and an always-on-top orb. A second launch therefore does not merely
open a duplicate window -- it contends for the hook, and depending on timing
and elevation that contention surfaces as a failure inside the ``keyboard``
package rather than as a clean "already running" message.

Observed during development: a leftover instance from an earlier launch held
``Ctrl+Space``, and the next launch died with a ``BaseException`` raised while
``keyboard`` built its key-name tables. The hook failure was made survivable
in ``hotkeys.py``, but the better answer is to not start at all: one orb, one
hook, and a clear message naming what is already running.

Windows implementation: a named mutex, which the kernel releases if the owning
process dies, so a hard crash cannot leave a stale lock behind. On other
platforms the guard is a no-op and the shell degrades to its previous
best-effort behaviour.
"""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)

#: Deliberately namespaced, so it cannot collide with an unrelated mutex.
_MUTEX_NAME = "Local\\Zaytoon.Desktop.Shell.Instance"


class SingleInstance:
    """Context manager holding the named mutex for the shell's lifetime."""

    def __init__(self, name: str = _MUTEX_NAME) -> None:
        self._name = name
        self._handle = None
        self.acquired = False
        self.supported = sys.platform == "win32"

    def acquire(self) -> bool:
        """Try to become the single instance. False means one already is."""
        if not self.supported:
            # Not claiming a guarantee we cannot make; the caller may still run.
            self.acquired = True
            return True

        import ctypes
        from ctypes import wintypes

        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except (OSError, AttributeError):  # pragma: no cover - non-Windows
            self.supported = False
            self.acquired = True
            return True

        # CreateMutexW returns the handle either way; the difference is
        # GetLastError() == ERROR_ALREADY_EXISTS when someone else owns it.
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        handle = kernel32.CreateMutexW(None, True, self._name)
        ERROR_ALREADY_EXISTS = 183

        if not handle:
            # Could not tell. Better to run than to refuse.
            logger.warning("could not check for an existing instance; continuing")
            self.acquired = True
            self.supported = False
            return True

        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            # Close our handle so we do not become the owner by accident.
            kernel32.CloseHandle(handle)
            self.acquired = False
            return False

        self._handle = handle
        self.acquired = True
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            import ctypes

            ctypes.WinDLL("kernel32").CloseHandle(self._handle)
        except Exception:  # pragma: no cover - best effort on shutdown
            pass
        finally:
            self._handle = None
            self.acquired = False

    def __enter__(self) -> "SingleInstance":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


ALREADY_RUNNING_MESSAGE = (
    "Zaytoon is already running.\n\n"
    "The floating orb is owned by the other instance -- press "
    "Ctrl+Space to hide or show it, or use the tray icon.\n"
    "Close it first if you meant to start a fresh one."
)


def claim_or_report(name: str = _MUTEX_NAME) -> SingleInstance | None:
    """Claim the single-instance lock, or print why not and return ``None``."""
    guard = SingleInstance(name)
    if guard.acquire():
        return guard
    print(ALREADY_RUNNING_MESSAGE, file=sys.stderr)
    return None