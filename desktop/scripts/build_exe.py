#!/usr/bin/env python
"""Build the standalone Zaytoon desktop executable with PyInstaller.

    python desktop/scripts/build_exe.py              # onedir build (the default)
    python desktop/scripts/build_exe.py --clean      # rebuild from scratch
    python desktop/scripts/build_exe.py --onefile    # single file (slower start)
    python desktop/scripts/build_exe.py --skip-frontend   # reuse frontend/dist

The result is ONE directory (``desktop/dist/Zaytoon/``) that is fully
self-contained: the frozen shell, the FastAPI backend's own source tree
(``_internal/backend``), every Python dependency the backend needs, and the
production frontend (``_internal/frontend``). It runs on a machine with no
checkout, no virtualenv and no system Python. An installed application never
needs ``ZAYTOON_REPO_ROOT`` — that variable remains only as a support override
read by ``app/config.py`` if someone wants the frozen shell to point at a
checkout on purpose.

Packaging decisions, and why:

* **onedir by default.** A onefile build extracts the whole bundle to ``%TEMP%``
  on every launch; the backend import tree alone (Whisper, ctranslate2, onnx)
  is hundreds of MB, which turns startup into a minute-long silent extraction.
  A directory starts in seconds and is what a real install ships.
* **The backend travels as DATA, not as frozen modules.** ``main._serve_backend``
  imports ``backend/main.py`` by *path* at runtime, so PyInstaller never has to
  predict the backend's import closure — the entire tree lands verbatim under
  ``_internal/backend``. Only its third-party dependencies are frozen, declared
  in BACKEND_COLLECT_ALL / BACKEND_HIDDEN_IMPORTS below (the backend's imports
  are invisible to static analysis precisely because its code is data).
* **The checkout's ``.venv`` / ``.env`` / ``data`` never ship.** The previous
  build copied the whole backend directory wholesale, which put a 0.4 GB
  virtualenv and the user's OpenRouter key (``backend/.env``) inside the
  distributable. ``stage_tree`` excludes them by name before PyInstaller ever
  sees the tree, and ``verify_bundle`` fails the build if one reappears.
* **The frontend is built by this script.** Shipping whatever happened to be in
  ``frontend/dist`` produced the blank-window bug: an ``index.html`` whose
  assets were never built. ``npm ci`` (when needed) + ``npm run build`` run
  first, and the bundle carries a ``zaytoon_bundle.json`` marker with the build
  stamp so ``--diagnose`` can tell a real bundle from a stale folder.

Frozen data layout (what ``app/config.py`` resolves at runtime):

    Zaytoon/
    ├── Zaytoon.exe               # the shell; `--serve` runs the backend in-process
    └── _internal/
        ├── backend/…             # backend source (imported by path at runtime)
        ├── frontend/…            # production build (index.html + assets)
        └── zaytoon_bundle.json   # build stamp
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

#: ``ZAYTOON_REPO_ROOT`` names a checkout to build FROM (a support/debugging
#: override, the same variable ``app/config.py`` reads at runtime). When it is
#: unset — the normal case — the build uses the repository this script lives
#: in. A stale value degrades to a clear "missing entry point" from
#: ``check_prerequisites`` rather than a confusing failure halfway through.
_REPO_OVERRIDE = os.environ.get("ZAYTOON_REPO_ROOT")
REPO = (
    Path(_REPO_OVERRIDE).expanduser().resolve()
    if _REPO_OVERRIDE
    else Path(__file__).resolve().parents[2]
)
DESKTOP = REPO / "desktop"
ENTRY = DESKTOP / "main.py"
DIST = DESKTOP / "dist"
BUILD = DESKTOP / "build"
BACKEND_SRC = REPO / "backend"
FRONTEND_SRC = REPO / "frontend"
FRONTEND_DIST = FRONTEND_SRC / "dist"

#: Bundle/app name; also the exe name and the onedir folder name.
BUNDLE_NAME = "Zaytoon"

#: Hidden imports of the *desktop shell's own* package. PyInstaller's analysis
#: cannot see them because main.py imports `app.*` dynamically in places, and a
#: missing one is a ModuleNotFoundError that only ever appears on a user's
#: machine. Third-party modules live in THIRD_PARTY_HIDDEN_IMPORTS — the
#: packaging tests assert this list is exactly the `app.*` closure.
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

#: Third-party modules PyInstaller must bundle explicitly (dynamically imported
#: or reachable only through optional code paths in the shell's own modules).
THIRD_PARTY_HIDDEN_IMPORTS = [
    "websockets",
    # uvicorn resolves its loop/protocol/lifespan implementations by name at
    # runtime ("auto"), so the frozen build must contain each candidate —
    # without them `uvicorn.run(app)` dies with "Cannot import auto".
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.websockets_impl",
]

#: Backend packages collected WITH their data/binary payloads. The backend's
#: code is imported by path at runtime (see module docstring), so nothing in it
#: is visible to static analysis — every dependency it needs is declared here.
#: Entries with native libraries (ctranslate2, onnxruntime, av, tokenizers)
#: cannot be plain hidden imports: their .pyd/.dll files must be copied too.
#: `dateparser` is included because its locale modules are imported by name.
BACKEND_COLLECT_ALL = [
    "faster_whisper",
    "ctranslate2",
    "tokenizers",
    "onnxruntime",
    "av",
    "huggingface_hub",
    "edge_tts",
    "ddgs",
    "dateparser",
]

#: Backend packages that are pure Python and statically analyzable once named.
BACKEND_HIDDEN_IMPORTS = [
    "fastapi",
    "starlette",
    "uvicorn",
    "httpx",
    "httpcore",
    "h11",
    "aiosqlite",
    "dotenv",
    "multipart",
    "python_multipart",  # newer python-multipart namespace; a miss is a warning
]

#: Directory names never staged from the backend tree, at its top level:
#: the checkout's interpreter (.venv), mutable state (data — its own SQLite
#: files and secrets.json), dev-only trees (tests, scripts), and VCS metadata.
BACKEND_SKIP_TOP = {".venv", "data", "tests", "scripts", ".git", "node_modules"}

#: Directory names never staged at ANY depth.
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}

#: File names never staged at ANY depth — ``.env`` is the user's API key.
SKIP_FILES = {".env", ".env.local", "secrets.json"}

#: Excluded suffixes (bytecode and databases never belong in a distributable).
SKIP_SUFFIXES = {".pyc", ".pyo", ".db", ".sqlite", ".log"}


def check_prerequisites(skip_frontend: bool = False) -> list[str]:
    """Return human-readable problems; an empty list means we can build."""
    problems: list[str] = []

    if sys.version_info < (3, 9):
        problems.append(f"Python 3.9+ required, found {sys.version.split()[0]}")

    if not ENTRY.exists():
        problems.append(f"missing entry point: {ENTRY}")
    if not (BACKEND_SRC / "main.py").exists():
        problems.append(f"missing backend entry point: {BACKEND_SRC / 'main.py'}")

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        problems.append("PyInstaller is not installed (pip install pyinstaller)")

    for module in ("webview", "websockets"):
        try:
            __import__(module)
        except ImportError:
            problems.append(f"missing runtime dependency: {module}")

    if not skip_frontend:
        if not (FRONTEND_SRC / "package.json").exists():
            problems.append(f"missing frontend: {FRONTEND_SRC / 'package.json'}")
        elif shutil.which("npm") is None:
            problems.append(
                "npm is not on PATH (install Node.js, or pass --skip-frontend "
                "to reuse an existing frontend/dist)"
            )

    return problems


def build_frontend() -> None:
    """``npm ci`` (when node_modules is absent) + ``npm run build``.

    Fails loudly on error: a half-built frontend is the blank-window bug this
    step exists to prevent, so a failed npm build must stop the packaging run
    rather than ship stale assets.
    """
    npm = shutil.which("npm")
    if npm is None:  # pragma: no cover - guarded by check_prerequisites
        raise RuntimeError("npm is not on PATH")

    if not (FRONTEND_SRC / "node_modules").is_dir():
        print("frontend: installing dependencies (npm ci)…")
        result = subprocess.run([npm, "ci"], cwd=str(FRONTEND_SRC))
        if result.returncode != 0:
            raise RuntimeError(f"npm ci failed in {FRONTEND_SRC} (exit {result.returncode})")

    print("frontend: building the production bundle (npm run build)…")
    result = subprocess.run([npm, "run", "build"], cwd=str(FRONTEND_SRC))
    if result.returncode != 0:
        raise RuntimeError(f"npm run build failed in {FRONTEND_SRC} (exit {result.returncode})")

    if not (FRONTEND_DIST / "index.html").is_file():
        raise RuntimeError(
            f"npm reported success but {FRONTEND_DIST / 'index.html'} is missing"
        )


def stage_tree(
    src: Path,
    dst: Path,
    *,
    skip_top: frozenset[str] | set[str] = frozenset(),
) -> int:
    """Copy ``src`` into ``dst`` without secrets, caches or mutable state.

    Returns the number of files staged. Exclusions are applied BEFORE the copy,
    so nothing unwanted ever reaches the staging directory PyInstaller reads —
    filtering afterwards would mean the sensitive file was already copied once.
    """
    if dst.exists():
        shutil.rmtree(dst)
    count = 0
    for path in src.rglob("*"):
        rel = path.relative_to(src)
        if rel.parts and rel.parts[0] in skip_top:
            continue
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if path.name in SKIP_FILES or path.suffix.lower() in SKIP_SUFFIXES:
            continue
        target = dst / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            count += 1
    return count


def write_bundle_marker(path: Path, *, onedir: bool) -> None:
    """Write the build stamp ``--diagnose`` reads to identify a real bundle."""
    commit = ""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(REPO), capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            commit = result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass  # a bundle without git metadata is still a valid bundle

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "name": BUNDLE_NAME,
                "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "python": sys.version.split()[0],
                "commit": commit,
                "layout": "onedir" if onedir else "onefile",
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def pyinstaller_args(*, onedir: bool, console: bool, stage: Path) -> list[str]:
    """The complete PyInstaller invocation as an argument vector (no shell)."""
    separator = ";" if sys.platform == "win32" else ":"
    stage_root = stage / "root"
    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onedir" if onedir else "--onefile",
        "--name",
        BUNDLE_NAME,
        "--distpath",
        str(DIST),
        "--workpath",
        str(BUILD / "work"),
        "--specpath",
        str(BUILD / "spec"),
        "--paths",
        str(DESKTOP),
    ]
    args.append("--console" if console else "--windowed")

    # The app icon. Without it every shortcut and the taskbar show PyInstaller's
    # default; the same .ico is what SetupIconFile uses in the installer, so the
    # two artefacts cannot drift. Generated by desktop/icons/make_icon.py.
    icon = DESKTOP / "icons" / "zaytoon.ico"
    if sys.platform == "win32" and icon.is_file():
        args += ["--icon", str(icon)]

    for module in HIDDEN_IMPORTS + THIRD_PARTY_HIDDEN_IMPORTS:
        args += ["--hidden-import", module]
    for package in BACKEND_COLLECT_ALL:
        args += ["--collect-all", package]
    for package in BACKEND_HIDDEN_IMPORTS:
        args += ["--hidden-import", package]

    # Data payloads, resolved from the STAGED copies (already free of .venv,
    # .env and data/). Destinations match what app/config.py and main.py
    # resolve at runtime: _internal/backend and _internal/frontend.
    for source, destination in (
        (stage / "backend", "backend"),
        (stage / "frontend", "frontend"),
        (stage_root, "."),
    ):
        args += ["--add-data", f"{source}{separator}{destination}"]

    args.append(str(ENTRY))
    return args


def verify_bundle(*, onedir: bool) -> list[str]:
    """Post-build checks on the artifact itself; empty list means it is sound.

    These are the failure classes that used to ship unnoticed: a missing
    backend tree (the app cannot start itself), a missing/broken frontend
    (blank window), and the checkout's .venv or .env leaking into the
    distributable (0.4 GB of bloat and a leaked API key).
    """
    problems: list[str] = []
    name = f"{BUNDLE_NAME}.exe" if sys.platform == "win32" else BUNDLE_NAME

    if onedir:
        root = DIST / BUNDLE_NAME
        exe = root / name
        internal = root / "_internal"
        if not exe.is_file():
            return [f"expected {exe} after the build, but it is missing"]
        if not (internal / "backend" / "main.py").is_file():
            problems.append(
                f"backend source missing from the bundle ({internal / 'backend' / 'main.py'})"
            )
        index = internal / "frontend" / "index.html"
        if not index.is_file():
            problems.append(f"frontend build missing from the bundle ({index})")
        else:
            problems.extend(_missing_frontend_assets(index.parent, index))
        if not (internal / "zaytoon_bundle.json").is_file():
            problems.append("bundle marker (zaytoon_bundle.json) missing from the bundle")
        # The leaks: never ship the checkout's interpreter, key or databases.
        if (internal / "backend" / ".venv").exists():
            problems.append("LEAK: backend/.venv was copied into the bundle")
        for leak in internal.rglob(".env"):
            problems.append(f"LEAK: {leak} was copied into the bundle")
        for leak in internal.rglob("secrets.json"):
            problems.append(f"LEAK: {leak} was copied into the bundle")
        backend_data = internal / "backend" / "data"
        if backend_data.is_dir():
            for leak in backend_data.rglob("*.db"):
                problems.append(f"LEAK: database {leak} was copied into the bundle")
    else:
        exe = DIST / name
        if not exe.is_file():
            return [f"expected {exe} after the build, but it is missing"]

    return problems


def _missing_frontend_assets(dist: Path, index: Path) -> list[str]:
    """Files the built ``index.html`` references but that do not exist."""
    import re

    try:
        text = index.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"frontend index.html is unreadable ({exc})"]
    missing = []
    for ref in re.findall(r'(?:src|href)=["\']([^"\']+)["\']', text):
        if ref.startswith(("http://", "https://", "data:", "#", "mailto:")):
            continue
        if not (dist / ref.lstrip("/")).exists():
            missing.append(ref)
    return [
        f"frontend index.html references {len(missing)} missing file(s), e.g. "
        f"{missing[:3]} — this is a blank window"
    ] if missing else []


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def build(clean: bool = False, console: bool = False, onedir: bool = True,
          skip_frontend: bool = False) -> int:
    problems = check_prerequisites(skip_frontend=skip_frontend)
    if problems:
        print("Cannot build:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    if clean:
        for path in (DIST, BUILD):
            shutil.rmtree(path, ignore_errors=True)

    try:
        if not skip_frontend:
            build_frontend()
    except RuntimeError as exc:
        print(f"Cannot build: {exc}")
        return 1

    # Stage the trees PyInstaller will read: filtered copies, so the checkout's
    # .venv/.env/data never enter the bundle in the first place.
    stage = BUILD / "stage"
    backend_files = stage_tree(BACKEND_SRC, stage / "backend", skip_top=BACKEND_SKIP_TOP)
    frontend_files = stage_tree(FRONTEND_DIST, stage / "frontend")
    if backend_files == 0:
        print(f"Cannot build: nothing staged from {BACKEND_SRC}")
        return 1
    if not (stage / "frontend" / "index.html").is_file():
        print(f"Cannot build: {FRONTEND_DIST / 'index.html'} is missing — run npm run build")
        return 1
    write_bundle_marker(stage / "root" / "zaytoon_bundle.json", onedir=onedir)
    print(f"staged: {backend_files} backend files, {frontend_files} frontend files")

    (BUILD / "work").mkdir(parents=True, exist_ok=True)
    (BUILD / "spec").mkdir(parents=True, exist_ok=True)
    args = pyinstaller_args(onedir=onedir, console=console, stage=stage)

    print("Running PyInstaller…")
    print("  " + " ".join(args[2:]))
    result = subprocess.run(args, cwd=str(REPO))
    if result.returncode != 0:
        return result.returncode

    problems = verify_bundle(onedir=onedir)
    if problems:
        print()
        print("Bundle verification FAILED:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    name = f"{BUNDLE_NAME}.exe" if sys.platform == "win32" else BUNDLE_NAME
    exe = (DIST / BUNDLE_NAME / name) if onedir else (DIST / name)
    print()
    print(f"Built {exe}  ({exe.stat().st_size / (1024 * 1024):.1f} MB executable)")
    if onedir:
        total = _dir_size(DIST / BUNDLE_NAME)
        print(f"Bundle: {DIST / BUNDLE_NAME}  ({total / (1024 * 1024):.1f} MB total)")
    print()
    print("The bundle contains the shell, the backend source tree, every backend")
    print("dependency and the production frontend. It starts its own backend;")
    print("no checkout, virtualenv, system Python or environment variable is")
    print("needed on the target machine.")
    print()
    print(f"Verify it with:  {exe} --diagnose")
    print(f"                  {exe} --diagnose --start-backend   (spawn + probe /health)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Zaytoon desktop executable")
    parser.add_argument("--clean", action="store_true", help="remove dist/ and build/ first")
    parser.add_argument("--console", action="store_true", help="keep a console window (debugging)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--onedir",
        action="store_true",
        default=True,
        help="build a folder (default): starts fast, ships as a directory",
    )
    mode.add_argument(
        "--onefile",
        action="store_true",
        help="build a single executable (extracts the whole bundle on every launch)",
    )
    parser.add_argument(
        "--skip-frontend",
        action="store_true",
        help="do not run npm; reuse the existing frontend/dist as-is",
    )
    args = parser.parse_args(argv)
    return build(
        clean=args.clean,
        console=args.console,
        onedir=not args.onefile,
        skip_frontend=args.skip_frontend,
    )


if __name__ == "__main__":
    raise SystemExit(main())
