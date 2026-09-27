"""System tray icon and menu.

The tray is how the shell stays reachable when the orb is hidden, and it is the
only place the backend can be restarted without a console.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import pystray
    from PIL import Image, ImageDraw
except Exception:  # pragma: no cover
    pystray = None  # type: ignore[assignment]
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]

ICON_PATH = Path(__file__).resolve().parent.parent / "icons" / "source.png"


def make_image(size: int = 64, color: str = "#00d4ff"):
    """Load ``icons/source.png``, falling back to a drawn orb.

    The asset is preferred so the tray matches the orb exactly; the drawn
    version keeps the app working if the PNG is missing (e.g. a trimmed
    checkout) rather than showing a blank tray slot.
    """
    if Image is None:
        return None
    if ICON_PATH.exists():
        try:
            with Image.open(ICON_PATH) as handle:
                return handle.convert("RGBA").resize((size, size), Image.LANCZOS)
        except Exception as exc:  # pragma: no cover
            logger.debug("could not load %s: %s", ICON_PATH, exc)

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    inset = size * 0.08
    draw.ellipse(
        (inset, inset, size - inset, size - inset),
        fill=(10, 22, 32, 235),
        outline=color,
        width=max(1, int(size * 0.05)),
    )
    core = size * 0.26
    middle = size / 2
    draw.ellipse(
        (middle - core, middle - core, middle + core, middle + core),
        fill=color,
    )
    return image


class TrayIcon:
    """Wraps ``pystray`` so the rest of the app does not care if it is absent."""

    def __init__(self, name: str, app_name: str = "Zaytoon AI") -> None:
        self.name = name
        self.app_name = app_name
        self._icon = None
        self.available = pystray is not None

    def start(self, menu_items) -> None:
        if pystray is None or self._icon is not None:
            return
        image = make_image() or Image.new("RGBA", (64, 64), (10, 22, 32, 255))
        try:
            import threading

            self._icon = pystray.Icon(
                "zaytoon",
                image,
                self.app_name,
                menu=menu_items,
            )
            threading.Thread(target=self._icon.run, name="zaytoon-tray", daemon=True).start()
        except Exception as exc:  # pragma: no cover - Explorer unavailable
            logger.warning("tray icon unavailable: %s", exc)
            self._icon = None

    def notify(self, message: str, title: str = "Zaytoon AI") -> None:
        if self._icon is None:
            return
        try:
            self._icon.notify(message, title)
        except Exception as exc:  # pragma: no cover
            logger.debug("tray notify failed: %s", exc)

    def stop(self) -> None:
        icon, self._icon = self._icon, None
        if icon is None:
            return
        try:
            icon.stop()
        except Exception as exc:  # pragma: no cover
            logger.debug("tray stop failed: %s", exc)
