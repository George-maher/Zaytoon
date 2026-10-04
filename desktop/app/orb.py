"""The floating holographic orb.

Tk has no alpha channel on Windows, so transparency is done with a colour key:
the window background is set to a single sentinel colour and
``-transparentcolor`` makes exactly that colour invisible. Everything drawn on
the canvas therefore has to avoid the sentinel.

The orb is the *same* assistant as the in-panel core, not a second one:

* it resolves its state through ``states.status_to_state``, so both views name
  the current state the same way;
* it draws with ``core.draw_core``, the shared renderer;
* it drives itself with ``core.AnimationLoop`` — **one** cancellable ``after()``
  chain, started for an active state and cancelled outright for idle/offline,
  so an idle orb costs no timer at all.
"""

from __future__ import annotations

import math
import tkinter as tk

import customtkinter as ctk

from . import states, theme
from .core import AnimationLoop, draw_core
from .states import UiState

#: Sentinel colour made fully transparent by ``-transparentcolor``.
TRANSPARENT_KEY = "#000001"


def _rate_to_speed(rate: float) -> float:
    """Legacy helper kept for callers outside the shell.

    A state's sweep period (seconds/rev) becomes a pulse increment at 30fps:
    ``speed = 2*pi / (p * 30)``. A ``rate`` of 0 means "no sweep".
    """
    if rate <= 0:
        return 0.0
    return (2.0 * math.pi) / (rate * 30.0)


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
        # exists because both are a no-op on an unmapped window.
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
        self._state = states.status_to_state(self.status)
        self._phase = 0.0
        self._visible = True
        self._interval_ms = theme.FRAME_MS
        self._loop = AnimationLoop(
            self.after, self.after_cancel, self._on_frame,
            interval_ms=self._interval_ms,
        )

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
        self._loop.stop()
        try:
            self.destroy()
        except tk.TclError:  # pragma: no cover
            pass

    # -- input ------------------------------------------------------------
    def _handle_click(self, _event=None) -> None:
        if self._on_click is not None:
            self._on_click()

    # -- state ------------------------------------------------------------
    @property
    def ui_state(self) -> UiState:
        """The state the orb is showing, resolved through the shared mapper."""
        return self._state

    def set_status(self, status) -> None:
        """Accepts a HUD status string or a :class:`UiState`.

        The transition drives the animation: an active state starts the one
        loop, idle/offline **cancels** it. That is deliberate — a permanently
        scheduled 30fps callback is exactly the cost the redesign removes.
        """
        if isinstance(status, UiState):
            state = status
            raw = states.orb_status(state)
        else:
            raw = str(status)
            state = states.status_to_state(raw)
        changed = state is not self._state or raw != self.status
        self.status = raw
        if state is not self._state:
            self._state = state
            self._phase = 0.0
        if changed:
            self._loop.set_mode(self._state)
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
        """Enable animation for the current state.

        For an idle/offline orb this schedules **nothing** — ``set_mode``
        refuses to start a loop for a static state, which is the point.
        """
        self._interval_ms = max(16, 1000 // max(1, fps))
        self._loop.interval_ms = self._interval_ms
        self._loop.set_mode(self._state)

    def stop(self) -> None:
        self._loop.stop()

    @property
    def animating(self) -> bool:
        return self._loop.running

    def _on_frame(self) -> None:
        # Phase advances on wall time, so the sweep keeps its period even if
        # frames are dropped.
        period = theme.state_rate(self.status) or 1.0
        self._phase = (self._phase + (2 * math.pi) * (self._interval_ms / 1000.0) / period) % (2 * math.pi)
        self.render()

    # -- drawing ----------------------------------------------------------
    def render(self) -> None:
        if not self.winfo_exists():
            return
        canvas = self.canvas
        try:
            width = canvas.winfo_width() or self._size
            height = canvas.winfo_height() or self._size
        except tk.TclError:  # pragma: no cover
            return

        # The orb is the one element allowed a bloom: a stippled halo behind
        # the shared core renderer, in this state's glow colours.
        canvas.delete("all")
        inner, outer = theme.orb_colors(self.status)
        cx, cy = width / 2.0, height / 2.0
        radius = min(width, height) * 0.42
        halo = canvas.create_oval(
            cx - radius, cy - radius, cx + radius, cy + radius,
            fill=outer, outline="",
        )
        canvas.itemconfig(halo, stipple="gray50")

        draw_core(
            canvas, width, height,
            state=self._state, phase=self._phase, level=self.level,
            hovered=self.hovered,
        )

        # The inner glow tints the core without changing the shared geometry.
        core = radius * 0.52
        glow = canvas.create_oval(
            cx - core, cy - core, cx + core, cy + core,
            fill=inner, outline="",
        )
        canvas.itemconfig(glow, stipple="gray50")
        # Re-draw the centre marker over the glow so it stays crisp.
        arm = core * 0.28
        color = states.accent(self._state)
        canvas.create_line(cx - arm, cy, cx + arm, cy, fill=color, width=1)
        canvas.create_line(cx, cy - arm, cx, cy + arm, fill=color, width=1)

        if self._state is UiState.OFFLINE:
            label = canvas.create_text(
                cx, cy + radius * 1.05, text="offline",
                fill=theme.TEXT_FAINT, font=(theme.FONT_MONO, 9),
            )
            canvas.itemconfig(label, stipple="gray50")
