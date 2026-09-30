#!/usr/bin/env python
"""Entry point for the Zaytoon desktop shell.

    python desktop/main.py                # normal launch
    python desktop/main.py --diagnose     # print what it can see, then exit
    python desktop/main.py --panel        # start in the expanded panel
    python desktop/main.py --no-backend   # never spawn FastAPI

``--diagnose`` exists because the most common first-run failure is
environmental (no interpreter, no PortAudio, hotkeys already taken). It reports
all of that without opening a window, so the answer is one command instead of a
log file.
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
from app.hotkeys import format_hotkey  # noqa: E402


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
        ("customtkinter", "the UI"),
        ("sounddevice", "microphone and TTS playback"),
        ("numpy", "audio maths"),
        ("websockets.sync.client", "the backend connection"),
        ("keyboard", "global hotkeys"),
        ("pystray", "the tray icon"),
    ):
        try:
            __import__(module)
            print(f"  OK   {module}  ({why})")
        except Exception as exc:
            print(f"  FAIL {module}  ({why}) - {type(exc).__name__}: {exc}")
    print()

    # 3. audio devices
    print("audio")
    try:
        from app.audio import list_devices

        inputs, outputs = list_devices()
        print(f"  {len(inputs)} input device(s), {len(outputs)} output device(s)")
        for name in inputs[:4]:
            print(f"    in  {name}")
        for name in outputs[:2]:
            print(f"    out {name}")
        if not inputs:
            ok = False
            print("  FAIL no microphone detected")
    except Exception as exc:
        ok = False
        print(f"  FAIL {type(exc).__name__}: {exc}")
    print()

    # 4. display
    print("display")
    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        print(f"  OK   tkinter ({root.tk.call('tk', 'windowingsystem')})")
        root.destroy()
    except Exception as exc:
        ok = False
        print(f"  FAIL tkinter - {type(exc).__name__}: {exc}")
    print()

    # 5. hotkeys
    print("hotkeys")
    try:
        from app.hotkeys import HotkeyManager

        manager = HotkeyManager()
        results = {}
        for name, key in (("toggle", config.toggle_hotkey), ("expand", config.expand_hotkey)):
            results[name] = manager.register(key, lambda: None)
            print(f"  {'OK  ' if results[name] else 'FAIL'} {format_hotkey(key)}")
        if not all(results.values()):
            print(f"  note: {manager.error}")
            print("        these are optional; the tray menu still works")
        manager.unregister_all()
    except Exception as exc:
        print(f"  FAIL {type(exc).__name__}: {exc}")
    print()

    print("verdict:", "ready" if ok else "problems above must be fixed first")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zaytoon-desktop", description="Zaytoon AI floating assistant"
    )
    parser.add_argument("--diagnose", action="store_true", help="check the environment and exit")
    parser.add_argument("--panel", action="store_true", help="start in the expanded panel")
    parser.add_argument("--no-backend", action="store_true", help="never spawn FastAPI")
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)

    config = DesktopConfig()
    if args.no_backend:
        config.manage_backend = False

    if args.diagnose:
        return _diagnose(config)

    if not backend_is_up(config.host, config.port) and config.manage_backend:
        print("Starting the Zaytoon backend, this takes a few seconds…")

    # One orb, one keyboard hook. Claimed before anything global is created,
    # so a second launch reports the conflict instead of contending for the
    # hotkey -- which, depending on timing and elevation, surfaced as a crash
    # inside the `keyboard` package rather than as a message.
    from app.instance import claim_or_report

    guard = claim_or_report()
    if guard is None:
        return 0

    from app.app import ZaytoonApp

    application = ZaytoonApp(config)
    if args.panel:
        application._enter_panel()
    try:
        return application.run()
    finally:
        guard.release()


if __name__ == "__main__":
    raise SystemExit(main())
