"""The floating holographic orb.

Tk has no alpha channel on Windows, so transparency is done with a colour key:
the window background is set to a single sentinel colour and
``-transparentcolor`` makes exactly that colour invisible. Everything drawn on
the canvas therefore has to avoid the sentinel.

The orb reacts to two inputs: the backend ``status`` (colour and pulse) and the
microphone RMS level (a reactive ring), so it visibly answers both the
assistant's state and whether it is hearing the user.
"""

from __future__ import annotations

import math
import tkinter as tk

import customtkinter as ctk

from . import theme

#: Sentinel colour made fully transparent by ``-transparentcolor``.
TRANSPARENT_KEY = "#000001"


class OrbWindow(ctk.CTkToplevel):
    """A frameless, always-on-top, click-through-nowhere orb."""

    SIZE = 320

    def __init__(self, master=None, *, size: int = SIZE, on_click=None) -> None:
        super().__init__(master)
        self._size = size
        self._on_click = on_click

        self.title("Zaytoon")
        self.overrideredirect(True)
        self.configure(bg=TRANSPARENT_KEY)
        self.geometry(f"{size}x{size}")
        self.resizable(False, False)

        # Colour-key transparency plus always-on-top, applied after the window
        # exists because both are no-ops on an unmapped window.
        self.after(10, self._apply_window_attributes)

        self.canvas = tk.Canvas(
            self,
            width=size,
            height=size,
            bg=TRANSPARENT_KEY,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self._handle_click)
        self.canvas.bind("<Configure>", lambda _e: self.render())
        self._apply_window_icon()

        self.status = "disconnected"
        self.level = 0.0
        self.hovered = False
        self._pulse = 0.0
        self._pulse_speed = 0.0
        self._running = False
        self._visible = True

    # -- window management ------------------------------------------------
    def _apply_window_icon(self) -> None:
        """Give the taskbar/tray the orb asset, if Pillow is available."""
        try:
            from PIL import Image, ImageTk

            from .tray import ICON_PATH

            if not ICON_PATH.exists():
                return
            with Image.open(ICON_PATH) as handle:
                photo = ImageTk.PhotoImage(handle.convert("RGBA").resize((64, 64)))
            self._icon_ref = photo  # keep a reference or Tk garbage-collects it
            self.iconphoto(True, photo)
        except Exception as exc:  # pragma: no cover - cosmetic only
            print(f"[orb] no window icon: {exc}")

    def _apply_window_attributes(self) -> None:
        try:
            self.wm_attributes("-transparentcolor", TRANSPARENT_KEY)
            self.wm_attributes("-topmost", True)
            self.wm_attributes("-toolwindow", True)
        except tk.TclError as exc:  # pragma: no cover - platform dependent
            print(f"[orb] could not set window attributes: {exc}")

    def set_always_on_top(self, on_top: bool) -> None:
        try:
            self.wm_attributes("-topmost", bool(on_top))
        except tk.TclError:  # pragma: no cover
            pass

    def recenter(self, margin: int = 24) -> None:
        """Move the orb to the bottom-right of the monitor it is on."""
        self.update_idletasks()
        width, height = self.winfo_width(), self.winfo_height()
        if width <= 1:
            width = height = self._size
        try:
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
        except tk.TclError:  # pragma: no cover
            return
        x = max(margin, sw - width - margin)
        y = max(margin, sh - height - margin)
        self.geometry(f"+{x}+{y}")

    def move_to(self, x: int, y: int) -> None:
        self.geometry(f"+{int(x)}+{int(y)}")

    @property
    def position(self) -> tuple[int, int]:
        return self.winfo_x(), self.winfo_y()

    def hide(self) -> None:
        self._visible = False
        self.withdraw()

    def show(self) -> None:
        if self._visible:
            return
        self._visible = True
        self.deiconify()
        self.lift()
        self.set_always_on_top(True)
        self.focus_force()

    @property
    def visible(self) -> bool:
        return self._visible

    def toggle_visible(self) -> None:
        self.hide() if self._visible else self.show()

    def destroy_safely(self) -> None:
        self._running = False
        try:
            self.destroy()
        except tk.TclError:  # pragma: no cover
            pass

    # -- input ------------------------------------------------------------
    def _handle_click(self, _event=None) -> None:
        if self._on_click is not None:
            self._on_click()

    def set_status(self, status: str) -> None:
        if status != self.status:
            self.status = status
            # Thinking and speaking pulse; idle states hold still.
            self._pulse_speed = 0.09 if status in theme.ACTIVE_STATES else 0.0
            self.render()

    def set_level(self, level: float) -> None:
        """Feed the microphone RMS so the orb reacts to the user's voice."""
        self.level = max(0.0, min(1.0, level * 8.0))

    def set_hovered(self, hovered: bool) -> None:
        if hovered != self.hovered:
            self.hovered = hovered
            self.render()

    # -- animation --------------------------------------------------------
    def start(self, fps: int = 30) -> None:
        if self._running:
            return
        self._running = True
        self._tick(fps)

    def stop(self) -> None:
        self._running = False

    def _tick(self, fps: int) -> None:
        if not self._running:
            return
        self._pulse = (self._pulse + self._pulse_speed) % (math.pi * 2)
        self.render()
        try:
            self.after(max(16, 1000 // fps), self._tick, fps)
        except tk.TclError:  # pragma: no cover - window destroyed
            self._running = False

    # -- drawing ----------------------------------------------------------
    def render(self) -> None:
        if not self.winfo_exists():
            return
        c = self.canvas
        c.delete("all")
        size = self._size
        cx = cy = size / 2
        inner, outer = theme.orb_colors(self.status)

        breathe = 0.5 + 0.5 * math.sin(self._pulse)
        base = size * 0.30 * (1.0 + 0.035 * breathe + 0.05 * self.level)

        # Outer halo, brightening with input level.
        halo = c.create_oval(
            cx - base * 1.34, cy - base * 1.34,
            cx + base * 1.34, cy + base * 1.34,
            fill=outer, outline="",
        )
        c.itemconfig(halo, stipple="gray50")

        # Mid ring.
        c.create_oval(
            cx - base * 1.12, cy - base * 1.12,
            cx + base * 1.12, cy + base * 1.12,
            fill=inner, outline=theme.ACCENT_DIM, width=1,
        )

        # Core.
        core = base * (0.86 + 0.10 * self.level)
        c.create_oval(
            cx - core, cy - core, cx + core, cy + core,
            fill=inner, outline=theme.ACCENT if self.status in theme.ACTIVE_STATES else theme.BORDER,
            width=2,
        )

        # A crisp highlight sells the "holographic" read.
        gloss = base * 0.34
        c.create_oval(
            cx - gloss * 0.9, cy - gloss * 1.25,
            cx + gloss * 0.9, cy - gloss * 0.2,
            fill=theme.TEXT, outline="",
        )
        c.itemconfig(c.find_withtag("all")[-1], stipple="gray75")

        if self.hovered:
            ring = base * 1.5
            c.create_oval(
                cx - ring, cy - ring, cx + ring, cy + ring,
                outline=theme.ACCENT, width=1,
            )

        if self.status == "disconnected":
            label = c.create_text(
                cx, cy + base * 1.9, text="offline",
                fill=theme.TEXT_MUTED, font=("Segoe UI", 9),
            )
            c.itemconfig(label, stipple="gray50")
