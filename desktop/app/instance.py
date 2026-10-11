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
process dies, so a hard crash cannot leave a stale lock behind.

Linux and macOS implement the same guarantee with `flock` on a lock file, which
the kernel also drops when the process dies — including on SIGKILL, which is the
property that makes the mutex worth having in the first place. Before that this
module was a documented no-op off Windows, so a second shell on the platforms
most of the repository runs on contended for the keyboard hook anyway: the exact
`BaseException` inside `keyboard` that the module exists to prevent. `ZAYTOON_
DESKTOP_LOCK_DIR` moves the file (tests use a tmp dir).
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys

logger = logging.getLogger(__name__)

#: Deliberately namespaced, so it cannot collide with an unrelated mutex.
_MUTEX_NAME = "Local\\Zaytoon.Desktop.Shell.Instance"


def lock_path(name: str) -> str:
    """Where the POSIX guard's lock file lives, derived from the lock's name."""
    directory = os.environ.get("ZAYTOON_DESKTOP_LOCK_DIR") or os.path.join(
        os.path.expanduser("~"), ".zaytoon", "run"
    )
    # Hashed rather than sanitised: the Windows name is `Local\\...\\Instance`,
    # and a filename built from backslashes and dots is how a lock file ends up
    # outside the directory it was meant to be in.
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:16]
    return os.path.join(directory, f"zaytoon-{digest}.lock")


class SingleInstance:
    """Context manager holding the named mutex for the shell's lifetime."""

    def __init__(self, name: str = _MUTEX_NAME) -> None:
        self._name = name
        self._handle = None
        self._file = None
        self.acquired = False
        # Supported everywhere now: a mutex on Windows, flock on anything with
        # `fcntl`. `supported` stays False only when the mechanism itself is
        # unavailable (a stripped Python, an unwritable directory), which is the
        # case the caller is told about by being allowed to run.
        self.supported = True

    def acquire(self) -> bool:
        """Try to become the single instance. False means one already is."""
        if sys.platform == "win32":
            return self._acquire_windows()
        return self._acquire_posix()

    def _acquire_posix(self) -> bool:
        import fcntl

        path = lock_path(self._name)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # "a+b": create it without truncating, so the holder's pid survives
            # long enough for a second launch to read it before overwriting.
            handle = open(path, "a+b")
        except OSError as exc:
            self.supported = False
            self.acquired = True
            logger.warning("single-instance lock unavailable (%s); continuing", exc)
            return True

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            self.acquired = False
            return False

        try:
            holder = ""
            handle.seek(0)
            holder = handle.read().decode("utf-8", "replace").strip()
            handle.seek(0)
            handle.truncate(0)
            handle.write(f"{os.getpid()}\n".encode("utf-8"))
            handle.flush()
        except OSError:  # pragma: no cover - the lock is held either way
            holder = ""
        self.holder_pid = holder or None
        self._file = handle
        self.acquired = True
        return True

    def _acquire_windows(self) -> bool:
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
        if self._file is not None:
            # Closing the descriptor is the unlock; flock is released on any
            # descriptor close for the file, and on process exit regardless.
            try:
                self._file.close()
            except OSError:  # pragma: no cover - best effort on shutdown
                pass
            self._file = None
            self.acquired = False
            return

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