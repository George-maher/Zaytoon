"""Make the ``app`` package importable from the desktop test suite.

The desktop application is a standalone package with its own import root, so
its tests need ``desktop/`` on ``sys.path`` exactly the way ``desktop/main.py``
does.
"""

import sys
from pathlib import Path

DESKTOP_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = DESKTOP_DIR.parent

for path in (str(DESKTOP_DIR), str(REPO_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)


import os  # noqa: E402

# --- Tk availability ------------------------------------------------------
#
# Seven modules exercise widgets that draw (the panel layout, the confirmation
# dialog, the orb animation). They import `customtkinter`, which imports
# `tkinter`, and an interpreter without Tk cannot even *collect* them — which is
# what a Linux or macOS desktop leg would hit, and it would look like seven
# broken tests rather than one missing system library.
#
# Skipping is stated, never silent: the reason is printed, and under GitHub
# Actions it is also emitted as a `::notice` annotation so the leg says out loud
# which guarantees it did not check. A stale list is not a risk-free shortcut —
# if a module starts or stops needing Tk, this list has to move with it, and a
# wrong entry shows up as a collection error on the leg that has Tk.
_TK_DEPENDENT = [
    "test_activity_model.py",
    "test_app_wiring.py",
    "test_confirmation_dialog.py",
    "test_core_animation.py",
    "test_panel_layout.py",
    "test_sidepanel_parsers.py",
    "test_telemetry.py",
]


def _tk_unavailable_reason() -> str:
    try:
        import customtkinter  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - any failure here means "no Tk"
        return f"{type(exc).__name__}: {exc}"
    return ""


#: Modules that reach `sounddevice`, which loads the PortAudio shared library at
#: import time and raises `OSError: PortAudio library not found` on a Linux box
#: without `libportaudio2` — a collection *error*, indistinguishable from a broken
#: audio pipeline. Same policy as Tk above: skip, name the missing library, and
#: let the CI leg install it so the tests run rather than vanish.
_AUDIO_DEPENDENT = [
    "test_audio_pipeline.py",
    "test_telemetry.py",
]


def _native_module_reason(module: str) -> str:
    try:
        __import__(module)
    except Exception as exc:  # noqa: BLE001 - "cannot be imported" is the question
        return f"{type(exc).__name__}: {exc}"
    return ""


_TK_MISSING = _tk_unavailable_reason()
_AUDIO_MISSING = _native_module_reason("sounddevice") if not _TK_MISSING else ""

for _reason, _modules in ((_TK_MISSING, _TK_DEPENDENT), (_AUDIO_MISSING, _AUDIO_DEPENDENT)):
    if _reason:
        print(
            f"desktop suite: skipping {len(_modules)} module(s) needing a native library "
            f"this host cannot provide ({_reason})"
        )

if _TK_MISSING or _AUDIO_MISSING:
    _skipped = (_TK_DEPENDENT if _TK_MISSING else []) + (
        _AUDIO_DEPENDENT if _AUDIO_MISSING else []
    )
    collect_ignore = _skipped
    _missing = "; ".join(
        reason for reason in (_TK_MISSING, _AUDIO_MISSING) if reason
    )
    _message = (
        f"desktop suite: {len(_skipped)} native-dependent modules skipped on this host "
        f"({_missing}); everything else — the palette parity, the wire protocol, the "
        f"single-instance guard — still runs. On Linux: "
        f"`sudo apt-get install -y python3-tk libportaudio2`."
    )
    print(_message)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        escaped = _message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::notice title=Desktop suite coverage on this host::{escaped}")
