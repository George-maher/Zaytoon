"""The reactive core — layered rings and an inner glow drawn on a plain Canvas.

Two things live here:

* :class:`AnimationLoop` — the *only* animation scheduler in the shell. One
  cancellable ``after()`` chain per widget, guarded so a second ``start()`'
  cannot spawn an overlapping loop, and **self-stopping**: the moment the state
  is static (idle / offline) it cancels itself and schedules nothing further.
  That is what keeps an idle assistant from costing a permanent 30fps timer.
* :func:`draw_core` — a pure drawing routine shared by the in-panel core and
  the floating orb, so both render the same state the same way. Everything is
  scaled from the widget's real pixel size, so resizing is free of fixed-pixel
  art, and a missed frame budget degrades to a static ring instead of a
  stutter.
"""

from __future__ import annotations

import math
import time
import tkinter as tk
from typing import Callable

import customtkinter as ctk

from . import states, theme
from .states import UiState

#: Consecutive over-budget frames before the core stops animating and draws
#: static rings instead. Two is enough to tell a hiccup from a stall.
MAX_MISSES = 2

#: Statuses that must not animate when the caller passes a raw HUD status
#: string instead of a :class:`UiState`.
STATIC_STATUSES = frozenset({"idle", "connected", "done", "offline", "disconnected"})


def is_static_mode(mode) -> bool:
    """True when ``mode`` (a :class:`UiState` or a HUD status string) should
    draw once and schedule no further frames."""
    if isinstance(mode, UiState):
        return states.is_static(mode)
    return str(mode) in STATIC_STATUSES


def budget_verdict(elapsed: float, interval: float, streak: int) -> tuple[int, bool]:
    """Judge one frame against the budget.

    Returns ``(new_streak, degrade)``. ``degrade`` turns True once
    :data:`MAX_MISSES` frames in a row overrun, which is the signal for the
    widget to stop the loop and draw a static ring.
    """
    if elapsed <= interval:
        return 0, False
    streak = int(streak) + 1
    return streak, streak >= MAX_MISSES


class AnimationLoop:
    """A single cancellable ``after()`` chain.

    ``schedule(delay_ms, callback) -> token`` and ``cancel(token)`` are injected
    so the loop is testable without Tk (``widget.after`` / ``widget.after_cancel``
    in production, a recording fake in the tests).
    """

    def __init__(
        self,
        schedule: Callable[[int, Callable[[], None]], object],
        cancel: Callable[[object], None],
        on_frame: Callable[[], None],
        *,
        interval_ms: int = theme.FRAME_MS,
    ) -> None:
        self._schedule = schedule
        self._cancel = cancel
        self._on_frame = on_frame
        self.interval_ms = max(1, int(interval_ms))
        self._token: object | None = None
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def token(self) -> object | None:
        """The id of the currently scheduled frame, if any (test/diagnostic)."""
        return self._token

    def set_mode(self, mode) -> None:
        """Start for an animated mode; stop dead for a static one."""
        if is_static_mode(mode):
            self.stop()
        else:
            self.start()

    def start(self) -> None:
        if self._running:
            return  # the guard: never two chains for the same widget
        self._running = True
        self._schedule_next()

    def stop(self) -> None:
        self._running = False
        token, self._token = self._token, None
        if token is not None:
            try:
                self._cancel(token)
            except Exception:  # pragma: no cover - already fired / Tk gone
                pass

    def _schedule_next(self) -> None:
        if not self._running:
            return
        try:
            self._token = self._schedule(self.interval_ms, self._frame)
        except Exception:  # pragma: no cover - widget destroyed mid-frame
            self._running = False
            self._token = None

    def _frame(self) -> None:
        self._token = None
        if not self._running:
            return  # cancelled while queued: do not reschedule
        try:
            self._on_frame()
        finally:
            self._schedule_next()


# ---------------------------------------------------------------------------
# drawing
# ---------------------------------------------------------------------------

def _mix(color: str, other: str, t: float) -> str:
    return theme.mix(color, other, t)


def _base_rings(canvas, cx, cy, radius, accent, animated) -> None:
    """The static lattice every mode is drawn on: two rings + four ticks."""
    canvas.create_oval(
        cx - radius, cy - radius, cx + radius, cy + radius,
        outline=theme.HAIRLINE, width=1, fill="",
    )
    inner = radius * 0.72
    canvas.create_oval(
        cx - inner, cy - inner, cx + inner, cy + inner,
        outline=_mix(theme.HAIRLINE, accent, 0.35 if animated else 0.12),
        width=1, fill="",
    )
    # Instrument ticks at the cardinals: the "technical" read, for free.
    for angle in (0, 90, 180, 270):
        rad = math.radians(angle)
        x1, y1 = cx + math.cos(rad) * radius, cy + math.sin(rad) * radius
        x2, y2 = cx + math.cos(rad) * (radius + 6), cy + math.sin(rad) * (radius + 6)
        canvas.create_line(x1, y1, x2, y2, fill=theme.HAIRLINE, width=1)


def _core_disc(canvas, cx, cy, radius, accent, animated) -> None:
    canvas.create_oval(
        cx - radius, cy - radius, cx + radius, cy + radius,
        fill=theme.tint(accent, 0.18, theme.BG1),
        outline=accent if animated else _mix(theme.HAIRLINE, accent, 0.4),
        width=2 if animated else 1,
    )


def draw_core(
    canvas,
    width: int,
    height: int,
    *,
    state: UiState = UiState.IDLE,
    phase: float = 0.0,
    level: float = 0.0,
    hovered: bool = False,
    degraded: bool = False,
    settle: float = 1.0,
) -> None:
    """Render one frame of the core into ``canvas``.

    Pure with respect to the widget: it only reads ``width``/``height`` (so the
    geometry scales with the container) and the arguments, and it clears the
    canvas itself. ``degraded=True`` draws the static ring only — the fallback
    when the frame budget is being missed.
    """
    try:
        canvas.delete("all")
    except tk.TclError:  # pragma: no cover - destroyed mid-frame
        return
    if width < 12 or height < 12:
        return

    cx, cy = width / 2.0, height / 2.0
    radius = min(width, height) * 0.36
    accent = states.accent(state)
    animated = not states.is_static(state) and not degraded

    _base_rings(canvas, cx, cy, radius, accent, animated)

    if degraded or states.is_static(state):
        _core_disc(canvas, cx, cy, radius * 0.52, accent, animated=False)
        _crosshair(canvas, cx, cy, radius, theme.HAIRLINE)
    elif state is UiState.LISTENING:
        _waves(canvas, cx, cy, radius, accent, phase, level)
    elif state in (UiState.UNDERSTANDING, UiState.PLANNING):
        _orbit(canvas, cx, cy, radius, accent, phase, violet=state is UiState.PLANNING)
    elif state is UiState.EXECUTING:
        _segments(canvas, cx, cy, radius, accent, phase)
    elif state is UiState.SPEAKING:
        _amplitude(canvas, cx, cy, radius, accent, phase, level)
    elif state is UiState.AWAITING_CONFIRM:
        _awaiting(canvas, cx, cy, radius, accent, phase)
    elif state is UiState.ERROR:
        _error(canvas, cx, cy, radius, accent, phase, settle)
    else:  # pragma: no cover - exhaustive above
        _core_disc(canvas, cx, cy, radius * 0.52, accent, animated=False)

    if hovered:
        ring = radius * 1.18
        canvas.create_oval(
            cx - ring, cy - ring, cx + ring, cy + ring,
            outline=accent, width=1,
        )


def _crosshair(canvas, cx, cy, radius, color) -> None:
    """Small centre cross — reads as 'parked', not 'dead'."""
    arm = radius * 0.14
    canvas.create_line(cx - arm, cy, cx + arm, cy, fill=color, width=1)
    canvas.create_line(cx, cy - arm, cx, cy + arm, fill=color, width=1)


def _waves(canvas, cx, cy, radius, accent, phase, level) -> None:
    """Listening: rings expanding outward from the core, level-reactive."""
    cycles = (phase / (2 * math.pi)) % 1.0
    for i in range(3):
        t = (cycles + i / 3.0) % 1.0
        r = radius * (0.5 + 0.62 * t)
        canvas.create_oval(
            cx - r, cy - r, cx + r, cy + r,
            outline=_mix(accent, theme.BG0, 1.0 - t),
            width=1,
        )
    _core_disc(canvas, cx, cy, radius * (0.42 + 0.16 * level), accent, True)


def _orbit(canvas, cx, cy, radius, accent, phase, *, violet: bool) -> None:
    """Understanding: orbiting particles. Planning: a violet node lattice."""
    nodes = 4 if violet else 7
    path = radius * 0.88
    canvas.create_oval(
        cx - path, cy - path, cx + path, cy + path,
        outline=_mix(accent, theme.BG0, 0.7), width=1,
    )
    points = []
    for i in range(nodes):
        a = phase + i * (2 * math.pi / nodes)
        x, y = cx + math.cos(a) * path, cy + math.sin(a) * path
        points.append((x, y))
    if violet:
        # Chords turn the nodes into a lattice rather than a ring of dots.
        for i in range(len(points)):
            x1, y1 = points[i]
            x2, y2 = points[(i + 1) % len(points)]
            canvas.create_line(x1, y1, x2, y2, fill=_mix(accent, theme.BG0, 0.55), width=1)
    for x, y in points:
        r = 3
        canvas.create_oval(x - r, y - r, x + r, y + r, fill=accent, outline="")
    _core_disc(canvas, cx, cy, radius * 0.44, accent, True)


def _segments(canvas, cx, cy, radius, accent, phase) -> None:
    """Executing / tool: a rotating segmented ring (the 'working' glyph)."""
    segments, extent, gap = 9, 22.0, 16.0
    start_deg = math.degrees(phase)
    for i in range(segments):
        start = start_deg + i * (360.0 / segments)
        canvas.create_arc(
            cx - radius, cy - radius, cx + radius, cy + radius,
            start=-start, extent=extent, style="arc",
            outline=accent, width=3,
        )
        # Hairline continuation makes the ring read as machined, not dotted.
        canvas.create_arc(
            cx - radius, cy - radius, cx + radius, cy + radius,
            start=-(start + extent + 2), extent=gap - 2, style="arc",
            outline=_mix(accent, theme.BG0, 0.7), width=1,
        )
    _core_disc(canvas, cx, cy, radius * 0.46, accent, True)


def _amplitude(canvas, cx, cy, radius, accent, phase, level) -> None:
    """Speaking: pulsing amplitude-style rings."""
    for i in range(3):
        wave = 0.5 + 0.5 * math.sin(phase + i * 2.1)
        r = radius * (0.46 + 0.16 * wave + 0.20 * level)
        canvas.create_oval(
            cx - r, cy - r, cx + r, cy + r,
            outline=_mix(accent, theme.BG0, 0.35 * i),
            width=2 if i < 2 else 1,
        )
    _core_disc(canvas, cx, cy, radius * (0.40 + 0.14 * level), accent, True)


def _awaiting(canvas, cx, cy, radius, accent, phase) -> None:
    """Awaiting confirmation: an amber ring with a slow travelling highlight."""
    canvas.create_arc(
        cx - radius, cy - radius, cx + radius, cy + radius,
        start=-math.degrees(phase), extent=70, style="arc",
        outline=accent, width=3,
    )
    _core_disc(canvas, cx, cy, radius * 0.44, accent, True)
    canvas.create_text(cx, cy, text="!", fill=accent, font=(theme.FONT_MONO, 16, "bold"))


def _error(canvas, cx, cy, radius, accent, phase, settle) -> None:
    """Error: flickers for a moment, then settles into a steady red ring."""
    flicker = max(0.0, min(1.0, 1.0 - settle))  # 1 = just happened, 0 = settled
    pulse = 0.5 + 0.5 * math.sin(phase * 6.0)
    bright = 1.0 - flicker * pulse * 0.75
    canvas.create_oval(
        cx - radius, cy - radius, cx + radius, cy + radius,
        outline=_mix(theme.BG0, accent, bright), width=2,
    )
    _core_disc(canvas, cx, cy, radius * 0.46, accent, animated=settle < 1.0)
    canvas.create_text(
        cx, cy, text="✕", fill=accent, font=(theme.FONT_MONO, 14, "bold")
    )


# ---------------------------------------------------------------------------
# the widget
# ---------------------------------------------------------------------------

class CoreCanvas(ctk.CTkFrame):
    """In-panel core. Geometry follows the widget; the loop self-stops on idle."""

    def __init__(self, master, **kwargs) -> None:
        super().__init__(master, fg_color="transparent", **kwargs)
        self.canvas = tk.Canvas(
            self, bg=theme.BG0, highlightthickness=0, bd=0, relief="flat"
        )
        self.canvas.pack(fill="both", expand=True)

        self._state = UiState.IDLE
        self._phase = 0.0
        self._level = 0.0
        self._streak = 0
        self._degraded = False
        self._hovered = False
        self._mode_since = time.monotonic()
        self._last_frame = 0.0

        self._loop = AnimationLoop(
            self.after, self.after_cancel, self._on_frame,
            interval_ms=theme.FRAME_MS,
        )
        self.canvas.bind("<Configure>", self._on_configure)
        self._redraw()

    # -- public API -------------------------------------------------------
    @property
    def state(self) -> UiState:
        return self._state

    def set_state(self, state: UiState) -> None:
        if state is not self._state:
            self._state = state
            self._mode_since = time.monotonic()
            self._streak = 0
            self._degraded = False
            self._phase = 0.0
        # set_mode is idempotent: an animated state keeps one chain, a static
        # state cancels it outright.
        self._loop.set_mode(state)
        self._redraw()

    def set_level(self, level: float) -> None:
        """Microphone / amplitude input, 0..1."""
        self._level = max(0.0, min(1.0, float(level)))

    def set_hovered(self, hovered: bool) -> None:
        if hovered != self._hovered:
            self._hovered = hovered
            self._redraw()

    def stop(self) -> None:
        """Cancel the loop (shutdown). Safe to call twice."""
        self._loop.stop()

    # -- internals --------------------------------------------------------
    def _rate(self) -> float:
        """Seconds per revolution for the current state (shared HUD table).

        A rate of 0 means "no sweep" (the offline table entry); fall back to a
        one-second period so the phase still advances if that ever changes.
        """
        return theme.state_rate(states.orb_status(self._state)) or 1.0

    def _on_configure(self, _event=None) -> None:
        self._redraw()

    def _on_frame(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_frame if self._last_frame else 0.0
        self._last_frame = now
        self._phase = (self._phase + (2 * math.pi) * (elapsed or theme.FRAME_MS / 1000.0) / self._rate()) % (2 * math.pi)
        self._redraw()
        if self._degraded:
            # Draw one static frame, then stop scheduling: a stalled machine
            # must not be pushed harder by an animation timer.
            self._loop.stop()
            return
        self._streak, degrade = budget_verdict(elapsed, self._loop.interval_ms, self._streak)
        if degrade:
            self._degraded = True

    def _redraw(self) -> None:
        if not self.winfo_exists():
            return
        try:
            width = self.canvas.winfo_width()
            height = self.canvas.winfo_height()
        except tk.TclError:  # pragma: no cover
            return
        settle = 1.0
        if self._state is UiState.ERROR:
            settle = min(1.0, (time.monotonic() - self._mode_since) / 1.5)
        draw_core(
            self.canvas, width, height,
            state=self._state, phase=self._phase, level=self._level,
            hovered=self._hovered, degraded=self._degraded, settle=settle,
        )

    def destroy(self) -> None:
        self._loop.stop()
        super().destroy()
