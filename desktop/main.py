#!/usr/bin/env python
"""Entry point for the Zaytoon desktop application.

    python desktop/main.py                # normal launch
    python desktop/main.py --diagnose     # print what it can see, then exit
    python desktop/main.py --no-backend   # never spawn FastAPI
    python desktop/main.py --dev          # connect to Vite dev server

The desktop application hosts the React/Vite UI through a native WebView
(pywebview + WebView2 on Windows). The React app is the primary interface;
this host provides native window management, backend lifecycle, and OS
integration through a secure bridge.
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
    backend_is_up,
    resolve_backend_command,
    wait_for_backend,
)
from app.config import DesktopConfig, default_repo_root  # noqa: E402
from app.host import WEBVIEW2_DOWNLOAD_URL, webview2_runtime_present  # noqa: E402


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _diagnose(config: DesktopConfig) -> int:
    """Print everything a first-run problem could be, then report a verdict."""
    print(f"Zaytoon desktop {__version__}")
    print(f"  python     : {sys.version.split()[0]}  ({sys.executable})")
    print(f"  repo root  : {config.repo_root}")
    print(f"  config     : {config.describe()}")
    print()

    ok = True

    # 1. backend
    print("backend")
    up = backend_is_up(config.host, config.port)
    print(f"  {'UP  ' if up else 'DOWN'} {config.backend_addr}")
    if not up:
        resolved = resolve_backend_command(config)
        if resolved is None:
            ok = False
            print("  FAIL no interpreter or backend/main.py found")
            print(f"       expected {config.repo_root / 'backend' / 'main.py'}")
        else:
            python_exe, main_script, cwd = resolved
            print(f"  would run : {python_exe} {main_script}")
            print(f"  cwd        : {cwd}")
    print()

    # 2. dependencies
    print("dependencies")
    for module, why in (
        ("webview", "the WebView runtime (pywebview)"),
        ("websockets", "the backend connection"),
    ):
        try:
            __import__(module)
            print(f"  OK   {module}  ({why})")
        except Exception as exc:
            print(f"  FAIL {module}  ({why}) - {type(exc).__name__}: {exc}")
    print()

    # 3. frontend build
    print("frontend")
    dist = config.frontend_dist
    index = dist / "index.html"
    if index.exists():
        print(f"  OK   production build at {dist}")
    else:
        print(f"  WARN no production build at {dist}")
        print(f"       run `npm run build` in frontend/ or use --dev mode")
    print()

    # 4. WebView2 (Windows)
    if sys.platform == "win32":
        print("webview2")
        try:
            import webview

            # pywebview 6 no longer exposes __version__; do not let its
            # absence be reported as a failed import.
            version = getattr(webview, "__version__", None) or getattr(
                webview, "VERSION", "unknown"
            )
            print(f"  OK   pywebview ({version})")
        except Exception as exc:
            ok = False
            print(f"  FAIL pywebview - {type(exc).__name__}: {exc}")
        if webview2_runtime_present():
            print("  OK   WebView2 runtime installed")
        else:
            ok = False
            print("  FAIL WebView2 runtime missing - the window cannot open.")
            print(f"       Install it once: {WEBVIEW2_DOWNLOAD_URL}")
        print()

    # 5. dev mode
    if config.dev_mode:
        print("dev mode")
        dev_url = f"http://localhost:{config.dev_port}"
        print(f"  URL        : {dev_url}")
        print(f"  (ensure Vite dev server is running)")
        print()

    print("verdict:", "ready" if ok else "problems above must be fixed first")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zaytoon-desktop", description="Zaytoon AI desktop environment"
    )
    parser.add_argument("--diagnose", action="store_true", help="check the environment and exit")
    parser.add_argument("--no-backend", action="store_true", help="never spawn FastAPI")
    parser.add_argument("--dev", action="store_true", help="connect to Vite dev server")
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)

    config = DesktopConfig()
    if args.no_backend:
        config.manage_backend = False
    if args.dev:
        config.dev_mode = True

    if args.diagnose:
        return _diagnose(config)

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
