"""Generate an arc-reactor-style source.png for the Zaytoon .ico icon."""

from pathlib import Path
from PIL import Image, ImageDraw
import math

size = 256
img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)

cx, cy = size // 2, size // 2
radius = size // 2 - 4

# Outer circle (dark)
draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=(0, 0, 0, 255))

# Core inner circle (red)
core_radius = 40
draw.ellipse([cx - core_radius, cy - core_radius, cx + core_radius, cy + core_radius], fill=(255, 0, 0, 255))

# Reactor rings
for i in range(8):
    ring_radius = 20 + i * 8
    alpha = int(255 * (1 - i / 8))
    color = (255, 20, 147, alpha)  # Deep pink
    offset = i * 4
    draw.ellipse(
        [cx - ring_radius + offset, cy - ring_radius + offset,
         cx + ring_radius - offset, cy + ring_radius - offset],
        fill=color,
    )

# Glow effect
for glow in range(1, 4):
    glow_radius = radius + 20 + glow * 12
    for angle in range(0, 360, 45):
        ax = cx + int(glow_radius * math.cos(math.radians(angle)))
        ay = cy + int(glow_radius * math.sin(math.radians(angle)))
        if 0 <= ax < size and 0 <= ay < size:
            draw.point((ax, ay), fill=(255, 0, 0, int(100 / glow)))

# MCU-inspired accents
for i in range(4):
    size_i = 20 + i * 12
    draw.ellipse([cx - size_i, cy - size_i, cx + size_i, cy + size_i],
                 fill=(135, 206, 250, int(50 / ((i + 1) * 1.2))))

# Core details
draw.ellipse([cx - 8, cy - 8, cx + 8, cy + 8], fill=(255, 255, 255, 255))
draw.ellipse([cx - 4, cy - 4, cx + 4, cy + 4], fill=(0, 0, 139, 255))

# Save
output = Path("desktop/icons/source_arcreactor.png")
output.parent.mkdir(parents=True, exist_ok=True)
img.save(output)
print(f"Wrote arc reactor source image to {output} ({img.size})")