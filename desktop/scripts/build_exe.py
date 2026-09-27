"""Build a standalone Zaytoon-desktop executable with PyInstaller.

    python desktop/scripts/build_exe.py            # build
    python desktop/scripts/build_exe.py --clean    # rebuild from scratch

This replaces what Tauri's ``msi``/``nsis`` bundlers used to do. The
difference worth knowing: PyInstaller produces a **portable** ``.exe`` that
needs no installer, and it does **not** bundle the Python backend. The shell
still finds the backend through the same ``ZAYTOON_BACKEND_*`` variables it
uses in development, so a portable build expects ``backend/`` to sit next to
the executable (or to be pointed at with ``ZAYTOON_REPO_ROOT``).

The backend is deliberately not frozen into the exe: ``faster-whisper`` plus
``edge-tts`` pushes a PyInstaller bundle past 500 MB, and freezing the model
cache into an exe makes upgrades painful. Ship the backend as files and let the
exe drive it.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

DESKTOP = Path(__file__).resolve().parent.parent
REPO = DESKTOP.parent
ENTRY = DESKTOP / "main.py"
ICON = DESKTOP / "icons" / "source.png"
DIST = DESKTOP / "dist"
BUILD = DESKTOP / "build"

#: Hidden imports that PyInstaller's static analysis cannot see. `keyboard`
#: and `pystray` both resolve their backend at runtime, and the PortAudio
#: bindings in `sounddevice` are loaded through cffi.
HIDDEN_IMPORTS = [
    "app",
    "app.app",
    "app.audio",
    "app.backend",
    "app.client",
    "app.config",
    "app.hotkeys",
    "app.orb",
    "app.panel",
    "app.protocol",
    "app.theme",
    "app.tray",
    "keyboard",
    "pystray",
    "pystray._win32",
    "sounddevice",
    "websockets.sync.client",
    "websockets.sync.connection",
]


def check_prerequisites() -> list[str]:
    """Return a list of human-readable problems; empty means we can build."""
    problems: list[str] = []

    if sys.version_info < (3, 9):
        problems.append(f"Python 3.9+ required, found {sys.version.split()[0]}")

    if not ENTRY.exists():
        problems.append(f"missing entry point: {ENTRY}")

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        problems.append("PyInstaller is not installed (pip install pyinstaller)")

    for module in ("customtkinter", "numpy", "PIL", "websockets"):
        try:
            __import__(module)
        except ImportError:
            problems.append(f"missing runtime dependency: {module}")

    if not ICON.exists():
        # Cosmetic, but the exe would get a default icon.
        problems.append(f"missing icon asset: {ICON}")

    return problems


def build(clean: bool = False, console: bool = False) -> int:
    problems = check_prerequisites()
    if problems:
        print("Cannot build:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    if clean:
        for path in (DIST, BUILD):
            shutil.rmtree(path, ignore_errors=True)

    separator = ";" if sys.platform == "win32" else ":"
    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--name",
        "Zaytoon",
        "--distpath",
        str(DIST),
        "--workpath",
        str(BUILD),
        "--specpath",
        str(BUILD),
        "--paths",
        str(DESKTOP),
    ]
    args.append("--console" if console else "--windowed")
    if ICON.exists():
        args += ["--icon", str(ICON)]
    for module in HIDDEN_IMPORTS:
        args += ["--hidden-import", module]
    args.append(str(ENTRY))

    print("Running PyInstaller…")
    print("  " + " ".join(args[2:]))
    result = subprocess.run(args, cwd=str(REPO))
    if result.returncode != 0:
        return result.returncode

    exe = DIST / ("Zaytoon.exe" if sys.platform == "win32" else "Zaytoon")
    print()
    if exe.exists():
        size_mb = exe.stat().st_size / (1024 * 1024)
        print(f"Built {exe}  ({size_mb:.1f} MB)")
        print()
        print("The executable does NOT contain the backend. Either:")
        print(f"  - copy it next to a checkout's backend/ directory, or")
        print(f"  - set ZAYTOON_REPO_ROOT to the repository root, or")
        print(f"  - set ZAYTOON_BACKEND_PYTHON / ZAYTOON_BACKEND_MAIN directly.")
        print()
        print("Verify it with:  Zaytoon.exe --diagnose")
    else:
        print(f"PyInstaller reported success but {exe} is missing.")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Zaytoon desktop executable")
    parser.add_argument("--clean", action="store_true", help="remove dist/ and build/ first")
    parser.add_argument("--console", action="store_true", help="keep a console window (debugging)")
    args = parser.parse_args(argv)
    return build(clean=args.clean, console=args.console)


if __name__ == "__main__":
    raise SystemExit(main())
