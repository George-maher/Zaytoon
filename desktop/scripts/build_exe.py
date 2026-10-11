"""Build a standalone Zaytoon desktop executable with PyInstaller.

    python desktop/scripts/build_exe.py            # build
    python desktop/scripts/build_exe.py --clean    # rebuild from scratch

The desktop application hosts the React/Vite UI through pywebview (WebView2 on
Windows). The backend is deliberately not frozen into the exe — the host finds
it through the same ``ZAYTOON_BACKEND_*`` variables it uses in development.
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
DIST = DESKTOP / "dist"
BUILD = DESKTOP / "build"

#: Hidden imports that PyInstaller's static analysis cannot see.
HIDDEN_IMPORTS = [
    "app",
    "app.backend",
    "app.bridge",
    "app.config",
    "app.host",
    "app.instance",
    "app.paths",
    "app.protocol",
    "webview",
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

    for module in ("webview", "websockets"):
        try:
            __import__(module)
        except ImportError:
            problems.append(f"missing runtime dependency: {module}")

    return problems


def build(clean: bool = False, console: bool = False, onedir: bool = False) -> int:
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
        # `--onefile` is the default because it is one artifact to hand someone,
        # and it costs an extraction step on every launch (the backend import
        # alone is measured at ~2.5 s in docs/DESKTOP_AUDIT.md). `--onedir` trades
        # a folder for a faster start and is what a real install should ship.
        "--onedir" if onedir else "--onefile",
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
    for module in HIDDEN_IMPORTS:
        args += ["--hidden-import", module]
    args.append(str(ENTRY))

    print("Running PyInstaller…")
    print("  " + " ".join(args[2:]))
    result = subprocess.run(args, cwd=str(REPO))
    if result.returncode != 0:
        return result.returncode

    name = "Zaytoon.exe" if sys.platform == "win32" else "Zaytoon"
    exe = DIST / name if not onedir else DIST / "Zaytoon" / name
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
    parser.add_argument(
        "--onedir",
        action="store_true",
        help="build a folder instead of a single file: starts faster, ships as a directory",
    )
    args = parser.parse_args(argv)
    return build(clean=args.clean, console=args.console, onedir=args.onedir)


if __name__ == "__main__":
    raise SystemExit(main())
