#!/usr/bin/env python
"""Entry point for the Zaytoon desktop application.

    python desktop/main.py                # normal launch
    python desktop/main.py --diagnose     # print what it can see, then exit
    python desktop/main.py --diagnose --start-backend
                                          # diagnose, and spawn the backend so
                                          # the health answer is a real one
    python desktop/main.py --no-backend   # never spawn FastAPI
    python desktop/main.py --dev          # connect to Vite dev server
    Zaytoon.exe --serve                   # packaged: run ONLY the backend

The desktop application hosts the React/Vite UI through a native WebView
(pywebview + WebView2 on Windows). The React app is the primary interface;
this host provides native window management, backend lifecycle, and OS
integration through a secure bridge.

A packaged build is one executable with two roles: launching it normally opens
the window, and ``--serve`` — the mode the shell spawns for itself — runs the
FastAPI backend in-process. There is no second artifact whose path could go
stale, and an installed machine needs no checkout, no virtualenv and no system
Python.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Allow `python desktop/main.py` (script dir is desktop/, not the repo root).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import __version__  # noqa: E402
from app.backend import (  # noqa: E402
    BackendProcess,
    backend_health,
    backend_is_up,
    resolve_backend_command,
)
from app.config import (  # noqa: E402
    DesktopConfig,
    bundle_dir,
    default_repo_root,
    default_state_dir,
    is_frozen,
)
from app.host import WEBVIEW2_DOWNLOAD_URL, webview2_runtime_present  # noqa: E402


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


# ---------------------------------------------------------------------------
# Frozen-build bootstrap and the backend-worker role
# ---------------------------------------------------------------------------
def _bootstrap_frozen_home() -> None:
    """Point every backend subsystem at per-user state before they read env.

    A frozen build has no writable checkout: ``backend/data`` would resolve to
    ``<install>/_internal/backend/data``, which is read-only under Program
    Files and wiped by a repair. ``ZAYTOON_HOME`` selects the portable layout
    (``Data/ Config/ Logs/ …``) under the platform state directory instead, and
    ``DATABASE_URL`` is set to an **absolute** path because backend config
    resolves a relative SQLite URL against the process cwd — which differs
    between this process and the backend worker it spawns.

    Idempotent (``setdefault`` everywhere) and a no-op outside frozen builds, so
    a development checkout keeps its ``backend/data`` behaviour exactly.
    """
    if not is_frozen():
        return
    home = default_state_dir()  # honours an explicit ZAYTOON_HOME, else platform dir
    os.environ.setdefault("ZAYTOON_HOME", str(home))
    data_dir = home / "Data"
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{data_dir / 'zaytoon.db'}")


def _serve_backend() -> int:
    """Backend-worker mode: run the FastAPI app in this process, then block.

    Frozen builds ship the backend's own ``.py`` files as *data* and import
    them straight from the file tree. Baking the backend into the frozen module
    graph instead would require PyInstaller to predict the entire backend
    import closure, and every module it missed would be an import error on a
    user's machine that no test in this repository — which all import from
    source — could see.

    The import is by **path**, not by name: when the shell runs as a script this
    module is already called ``main``, so a plain ``import main`` would hand
    back this very file.
    """
    _bootstrap_frozen_home()  # also when `Zaytoon.exe --serve` is run standalone

    if is_frozen():
        backend_cwd = (bundle_dir() or Path(sys.executable).resolve().parent) / "backend"
    else:
        backend_cwd = default_repo_root() / "backend"
    main_py = backend_cwd / "main.py"
    if not main_py.exists():
        print(f"backend worker: no backend entry point at {main_py}", file=sys.stderr)
        return 3

    # The shell may be configured on a non-default port; the backend reads
    # BACKEND_HOST/BACKEND_PORT, so translate before its config is imported.
    # An explicit BACKEND_PORT still wins — setdefault, not assignment.
    addr = os.environ.get("ZAYTOON_BACKEND_ADDR", "")
    host_part, _, port_part = addr.rpartition(":")
    if host_part and port_part.isdigit():
        os.environ.setdefault("BACKEND_HOST", host_part)
        os.environ.setdefault("BACKEND_PORT", port_part)

    # The backend assumes it is launched from its own directory (sys.path and
    # relative data paths), exactly as `cd backend && python main.py` does.
    os.chdir(backend_cwd)
    sys.path.insert(0, str(backend_cwd))

    import importlib.util

    spec = importlib.util.spec_from_file_location("zaytoon_backend_main", main_py)
    if spec is None or spec.loader is None:
        print(f"backend worker: cannot load {main_py}", file=sys.stderr)
        return 3
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - report, do not traceback into a log file
        print(f"backend worker: importing the backend failed: {exc!r}", file=sys.stderr)
        return 4

    # backend/main.py binds these itself when run directly; here uvicorn is
    # handed the app *object* so nothing re-imports by name.
    configure = getattr(module, "configure_logging", None)
    if callable(configure):
        configure()

    backend_config = getattr(module, "config", None)
    host = getattr(backend_config, "BACKEND_HOST", None) or "127.0.0.1"
    port = getattr(backend_config, "BACKEND_PORT", None) or 8000

    try:
        import uvicorn
    except ImportError as exc:
        print(f"backend worker: uvicorn is not bundled ({exc})", file=sys.stderr)
        return 4

    uvicorn.run(
        module.app,
        host=host,
        port=int(port),
        log_level=(os.environ.get("LOG_LEVEL", "info") or "info").lower(),
        reload=False,
    )
    return 0


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------
def _probe_health(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> dict | None:
    """The parsed ``GET /health`` body, or ``None`` if nothing healthy answers.

    A listener is not health. The backend can hold the port while its app is
    still importing modules, its lifespan has not run, or a *different* process
    (an old install, an unrelated service) is answering. Uses only the standard
    library: this module is the desktop shell, whose bundle must not gain an
    HTTP-client dependency for one GET request.
    """
    import json
    from urllib.request import urlopen

    try:
        with urlopen(f"http://{host}:{port}/health", timeout=timeout) as response:  # noqa: S310 - loopback from config
            if response.status != 200:
                return None
            body = response.read(64 * 1024)
        parsed = json.loads(body.decode("utf-8", "replace"))
        return parsed if isinstance(parsed, dict) else {"status": "ok"}
    except Exception:  # noqa: BLE001 - every failure class means "not healthy"
        return None


def _check_frontend_assets(dist, frozen: bool) -> tuple[str, str]:
    """Check the production build, including that its own assets resolve.

    An ``index.html`` whose ``src=/assets/app-abc.js`` is missing renders a
    blank window with no error anywhere, so the referenced files are checked
    too — not merely that ``index.html`` exists.
    """
    import re

    if not dist:
        return ("FAIL" if frozen else "WARN", "frontend dist: not configured")
    dist = Path(dist)
    if not dist.exists():
        return ("FAIL" if frozen else "WARN",
                f"frontend dist: not found at {dist} (run `npm run build` in frontend/)")
    index = dist / "index.html"
    if not index.exists():
        return ("FAIL" if frozen else "WARN", f"frontend dist: no index.html at {dist}")
    try:
        text = index.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return "FAIL", f"frontend dist: index.html is unreadable ({exc})"
    missing = []
    for ref in re.findall(r'(?:src|href)=["\']([^"\']+)["\']', text):
        if ref.startswith(("http://", "https://", "data:", "#", "mailto:")):
            continue
        if not (dist / ref.lstrip("/")).exists():
            missing.append(ref)
    if missing:
        return ("FAIL" if frozen else "WARN",
                f"frontend dist: {len(missing)} referenced file(s) missing, "
                f"e.g. {missing[:3]} — this is a blank window")
    size_kb = sum(f.stat().st_size for f in dist.rglob("*") if f.is_file()) // 1024
    return "PASS", f"frontend dist: production build at {dist} ({size_kb} KB)"


def _check_writable(path, label: str) -> tuple[str, str]:
    """Report whether ``path`` could be created and written — without creating it.

    The probe writes (and removes) a file in the nearest existing ancestor:
    that is exactly the question ``mkdir -p`` asks a moment later, and answering
    it here leaves no directory behind on a machine that only ran a diagnosis.
    """
    target = Path(path)
    probe_in = target
    while not probe_in.exists():
        parent = probe_in.parent
        if parent == probe_in:
            return "FAIL", f"{label}: no existing parent for {target}"
        probe_in = parent
    if not probe_in.is_dir():
        return "FAIL", f"{label}: {probe_in} exists but is not a directory"
    probe = probe_in / f".zaytoon-write-probe-{os.getpid()}"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return "FAIL", f"{label}: not writable ({probe_in}): {exc}"
    return "PASS", f"{label}: writable ({target})"


def _diagnose(config: DesktopConfig, start_backend: bool = False) -> int:
    """Print everything a first-run problem could be, then report a verdict.

    Each section reports its own status and the verdict fails **only** when a
    section actually failed: the previous version printed ``verdict: ready``
    while the backend was DOWN and the production build was missing, because
    two booleans were folded into one judgement.
    """
    frozen = is_frozen()
    checks: list[tuple[str, str]] = []  # status, text; status in PASS/WARN/FAIL/INFO

    print(f"Zaytoon desktop {__version__}")
    print(f"  python     : {sys.version.split()[0]}  ({sys.executable})")
    print(f"  platform   : {sys.platform}")
    print(f"  frozen     : {frozen}")
    print(f"  repo root  : {config.repo_root}")
    print(f"  config     : {config.describe()}")
    print()

    # 1. bundled backend -------------------------------------------------
    if frozen:
        bdir = bundle_dir() or Path(sys.executable).resolve().parent
        backend_main = bdir / "backend" / "main.py"
        if backend_main.exists():
            checks.append(("PASS", f"backend bundle: {backend_main}"))
        else:
            checks.append(("FAIL", "backend bundle: backend/main.py is missing from "
                                   "this install — the app cannot start a backend"))
        marker = bdir / "zaytoon_bundle.json"
        if marker.exists():
            try:
                import json

                meta = json.loads(marker.read_text(encoding="utf-8"))
                checks.append(("INFO", f"bundle built: {meta.get('created', '?')} "
                                       f"with {meta.get('python', '?')}"))
            except (OSError, ValueError):
                checks.append(("WARN", f"bundle marker unreadable: {marker}"))
        else:
            checks.append(("WARN", f"bundle marker missing: {marker}"))
    else:
        backend_main = config.repo_root / "backend" / "main.py"
        checks.append(("PASS" if backend_main.exists() else "FAIL",
                       f"backend source: {backend_main}"))

    # 2. frontend assets -------------------------------------------------
    checks.append(_check_frontend_assets(config.frontend_dist, frozen))

    # 3. backend process and health --------------------------------------
    listener = backend_is_up(config.host, config.port)
    health = _probe_health(config.port, config.host)
    if health:
        checks.append(("PASS", f"backend: UP on {config.backend_addr}, /health answered "
                               f"(status={health.get('status', 'ok')}, "
                               f"tools={health.get('tools_loaded', '?')})"))
    elif listener:
        checks.append(("FAIL", f"backend: something answers TCP on {config.backend_addr} "
                               "but /health does not — another application owns this "
                               "port, or the backend is still starting"))
    else:
        resolved = resolve_backend_command(config)
        if resolved is None:
            checks.append(("FAIL", f"backend: DOWN on {config.backend_addr}, and no "
                                   "backend could be resolved to start"))
        else:
            checks.append(("WARN", f"backend: DOWN on {config.backend_addr}; it would "
                                   f"start as [{resolved.kind}]: {resolved.describe()}"))

    # 4. windowing -------------------------------------------------------
    try:
        import webview  # noqa: F401

        checks.append(("PASS", "pywebview: importable"))
    except Exception as exc:  # noqa: BLE001 - any import failure is fatal for the window
        checks.append(("FAIL", f"pywebview: NOT importable ({type(exc).__name__}: {exc})"))
    if sys.platform == "win32":
        if webview2_runtime_present():
            checks.append(("PASS", "WebView2 runtime: installed"))
        else:
            checks.append(("FAIL", "WebView2 runtime missing - the window cannot open. "
                                   f"Install it once from {WEBVIEW2_DOWNLOAD_URL}"))
    else:
        checks.append(("INFO", "WebView2: not required on this platform"))

    # 5. configuration ---------------------------------------------------
    overrides = {
        name: os.environ[name]
        for name in (
            "ZAYTOON_BACKEND_ADDR", "ZAYTOON_BACKEND_PYTHON", "ZAYTOON_BACKEND_MAIN",
            "ZAYTOON_BACKEND_CWD", "ZAYTOON_DEV_MODE", "ZAYTOON_HOME",
            "ZAYTOON_REPO_ROOT", "ZAYTOON_FRONTEND_DIST", "BACKEND_PORT",
        )
        if os.environ.get(name)
    }
    checks.append(("INFO", f"env overrides: {overrides or '(none)'}"))

    # 6. writable state locations ----------------------------------------
    checks.append(_check_writable(config.state_dir, "state dir"))
    if frozen:
        checks.append(_check_writable(Path(os.environ.get("ZAYTOON_HOME", default_state_dir())) / "Data",
                                      "backend data dir"))
    else:
        checks.append(_check_writable(config.repo_root / "backend" / "data", "backend data dir"))
    checks.append(_check_writable(Path(config.state_dir) / "logs", "log dir"))

    # 7. optional integrations (never fatal) -----------------------------
    import socket

    try:
        with socket.create_connection(("127.0.0.1", 11434), timeout=0.25):
            checks.append(("INFO", "ollama: reachable on 127.0.0.1:11434"))
    except OSError:
        checks.append(("INFO", "ollama: not reachable on 127.0.0.1:11434 (optional)"))
    checks.append(("INFO", "openrouter key: "
                           f"{'set' if os.environ.get('OPENROUTER_API_KEY') else 'not set'} (optional)"))

    # 8. optional: actually bring the backend up --------------------------
    if start_backend and not health:
        print("starting the backend so /health can be answered for real…")
        proc = BackendProcess(config)
        status = proc.ensure_running()
        try:
            health = _probe_health(config.port, config.host)
            if status.running and health:
                checks = [c for c in checks if not c[1].startswith(
                    ("backend: DOWN", "backend: something answers"))]
                checks.append(("PASS", f"backend: started by this diagnose run "
                                       f"(logs: {proc.log_path})"))
            else:
                checks = [c for c in checks if not c[1].startswith(
                    ("backend: DOWN", "backend: something answers"))]
                checks.append(("FAIL", f"backend: failed to start — {status.hint}"))
        finally:
            # Only ever stops what this run spawned (BackendProcess ownership).
            proc.shutdown()

    print()
    for status, text in checks:
        print(f"  [{status:4}] {text}")
    print()

    fails = [c for c in checks if c[0] == "FAIL"]
    warns = [c for c in checks if c[0] == "WARN"]
    if fails:
        print(f"verdict: NOT READY — {len(fails)} failed check(s) above")
        return 1
    if warns:
        print(f"verdict: ready with {len(warns)} warning(s)")
        return 0
    print("verdict: ready")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zaytoon-desktop", description="Zaytoon AI desktop environment"
    )
    parser.add_argument("--diagnose", action="store_true", help="check the environment and exit")
    parser.add_argument(
        "--start-backend",
        action="store_true",
        help="with --diagnose: spawn the backend and wait for /health before judging",
    )
    parser.add_argument("--no-backend", action="store_true", help="never spawn FastAPI")
    parser.add_argument("--dev", action="store_true", help="connect to Vite dev server")
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    parser.add_argument(
        "--serve",
        action="store_true",
        help="run only the backend (backend-worker mode; the shell spawns this)",
    )
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)
    # Per-user state layout first: everything below reads env, and the backend
    # child inherits whatever this process sets.
    _bootstrap_frozen_home()

    if args.serve:
        return _serve_backend()

    config = DesktopConfig()
    if args.no_backend:
        config.manage_backend = False
    if args.dev:
        config.dev_mode = True

    if args.diagnose:
        return _diagnose(config, start_backend=args.start_backend)

    if not backend_is_up(config.host, config.port) and config.manage_backend:
        print("Starting the Zaytoon backend, this takes a few seconds…")

    # Single instance guard — one window, one backend handle
    from app.instance import claim_or_report

    guard = claim_or_report()
    if guard is None:
        return 0

    from app.host import WebViewHost

    host = WebViewHost(config)
    try:
        return host.run()
    finally:
        guard.release()


if __name__ == "__main__":
    raise SystemExit(main())
