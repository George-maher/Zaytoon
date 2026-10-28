"""Generate zaytoon.ico from the arc-reactor source PNG."""
from pathlib import Path
from PIL import Image

SOURCE = Path("desktop/icons/source_arcreactor.png")
TARGET = Path("desktop/icons/zaytoon.ico")
SIZES = (16, 24, 32, 48, 64, 128, 256)

image = Image.open(SOURCE).convert("RGBA")
image.save(TARGET, format="ICO", sizes=[(s, s) for s in SIZES])
print(f"Wrote {TARGET} ({TARGET.stat().st_size / 1024:.0f} KB, sizes {SIZES})")