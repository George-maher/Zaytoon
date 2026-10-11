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


_TK_MISSING = _tk_unavailable_reason()

if _TK_MISSING:
    collect_ignore = list(_TK_DEPENDENT)
    _message = (
        f"desktop suite: {len(_TK_DEPENDENT)} Tk-dependent modules skipped because "
        f"this interpreter cannot import Tk ({_TK_MISSING}); the other "
        f"modules — including the palette, protocol and instance guards — still run."
    )
    print(_message)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        escaped = _message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::notice title=Desktop suite coverage on this host::{escaped}")
