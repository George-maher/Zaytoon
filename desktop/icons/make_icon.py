"""Generate ``zaytoon.ico`` from ``source.png``.

Run once after changing the source artwork:

    backend\\.venv\\Scripts\\python.exe desktop\\icons\\make_icon.py

Both the PyInstaller build (``--icon``) and the Inno Setup installer
(``SetupIconFile``) consume the generated ``.ico``. Inno refuses an icon
source that is not an .ico/.exe/.dll — pointing it at the 18 MB built
``Zaytoon.exe`` fails with "File is too large" — and a windowed exe with
no embedded icon gives every shortcut the PyInstaller default.

Windows .ico files may hold 256x256 entries only as PNG data, which Pillow
writes for us; older sizes are stored as BMP. Sizes below are what Explorer,
the taskbar, shortcuts and the installer actually request.
"""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "source.png"
TARGET = HERE / "zaytoon.ico"
SIZES = (16, 24, 32, 48, 64, 128, 256)


def main() -> int:
    if not SOURCE.is_file():
        print(f"Cannot build icon: {SOURCE} is missing")
        return 1
    from PIL import Image

    image = Image.open(SOURCE).convert("RGBA")
    image.save(TARGET, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"Wrote {TARGET} ({TARGET.stat().st_size / 1024:.0f} KB, sizes {SIZES})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
