"""The application shell: header, core, transcript, side panel, composer.

Layout contract (the row constants below name every grid position, because a
transposed index renders as an invisible row):

* the window is an ordinary toplevel — OS title bar, OS resize borders,
  maximize; no ``overrideredirect``;
* the **transcript is the only weighted cell**, so growth goes to the
  conversation while the header, composer and status line keep their height at
  any window size;
* the core column and the side panel have fixed widths and are dropped (along
  with their column ``minsize``) when the window is too narrow for them — see
  :func:`layout_plan`, which is pure and therefore testable;
* ``minsize`` (``MIN_WIDTH``/``MIN_HEIGHT``) is the floor, ``<Configure>``
  re-applies the layout plan and rewraps the transcript, so text follows the
  window instead of clipping.

Widgets here dispatch: every action reaches ``app.py`` through the callbacks
passed to ``__init__``, and every display value is pushed in from outside. No
connection, protocol or business logic lives in this file.
"""

from __future__ import annotations

import time
import tkinter as tk
from dataclasses import dataclass

import customtkinter as ctk

from . import dialogs, states, theme
from .activity import ActivityFeed
from .core import CoreCanvas
from .header import HeaderBar
from .sidepanel import PANEL_WIDTH, SidePanel
from .states import UiState

#: Smallest window the shell accepts: header, one confirmation, the composer
#: and the status bar all still fit at 420x380.
MIN_WIDTH = 420
MIN_HEIGHT = 380

#: Grid rows/columns of the shell itself.
HEADER_ROW = 0
BODY_ROW = 1
QUICK_ROW = 2
COMPOSER_ROW = 3
STATUS_ROW = 4
BODY_COLUMN = 0

#: Below these window widths the core / side panel yield their space back to
#: the transcript (and their column minsize goes with them).
CORE_MIN_WIDTH = 640
SIDE_MIN_WIDTH = 900
#: Header pills collapse a little earlier than the core does.
COMPACT_WIDTH = 700

CORE_WIDTH = 150

#: Quick actions are fixed phrases the router already recognises.
QUICK_ACTIONS: tuple[tuple[str, str], ...] = (
    ("RUN TESTS", "run tests"),
    ("GIT STATUS", "git status"),
    ("SYSTEM INFO", "system info"),
)


def compute_wrap(
    width: int,
    *,
    base: int = theme.WRAP_BASE,
    margin: int = theme.WRAP_MARGIN,
    floor: int = theme.WRAP_MIN,
) -> int:
    """Transcript wraplength for a container of ``width`` pixels.

    Bounded above so a very wide window still produces readable line lengths,
    and below so a narrow one cannot collapse the text into a column.
    """
    return max(floor, min(base, int(width) - margin))


@dataclass(frozen=True)
class LayoutPlan:
    """Which optional columns this window width can afford."""

    core: bool
    side: bool
    core_width: int = 0
    side_width: int = 0


def layout_plan(width: int, side_enabled: bool = True) -> LayoutPlan:
    """Decide the shell's column set from the window width.

    Pure, so the resize behaviour is testable without moving a real window:
    at the 420px floor it is transcript-only, at the default 820 it gains the
    core, and only a genuinely wide window gets the side panel as well.
    """
    core = int(width) >= CORE_MIN_WIDTH
    side = bool(side_enabled) and int(width) >= SIDE_MIN_WIDTH
    return LayoutPlan(
        core=core,
        side=side,
        core_width=CORE_WIDTH if core else 0,
        side_width=PANEL_WIDTH if side else 0,
    )


@dataclass(frozen=True)
class RowStyle:
    """How a transcript row of a given role is drawn."""

    align: str          # "left" | "right"
    anchor: str         # tk anchor for the bubble
    spine: str          # accent edge colour
    background: str     # bubble fill
    text: str           # bubble text colour
    justify: str


#: One table, so the transcript cannot render the same role two ways.
_ROW_STYLES: dict[str, RowStyle] = {
    "user": RowStyle(
        align="right", anchor="e", spine=theme.CYAN,
        background=theme.tint(theme.CYAN, 0.08, theme.BG1),
        text=theme.TEXT_BASE, justify="right",
    ),
    "assistant": RowStyle(
        align="left", anchor="w", spine=theme.VIOLET,
        background=theme.BG2, text=theme.TEXT_BASE, justify="left",
    ),
    "system": RowStyle(
        align="left", anchor="w", spine=theme.TEXT_FAINT,
        background=theme.BG1, text=theme.TEXT_MUTE, justify="left",
    ),
    "error": RowStyle(
        align="left", anchor="w", spine=theme.RED,
        background=theme.tint(theme.RED, 0.08, theme.BG1),
        text=theme.TEXT_BASE, justify="left",
    ),
}


def row_style(role: str) -> RowStyle:
    """Unknown roles read as system rather than raising mid-stream."""
    return _ROW_STYLES.get(role, _ROW_STYLES["system"])


class MessageRow(ctk.CTkFrame):
    """One transcript entry: bubble, accent edge, monospace timestamp chip."""

    def __init__(self, master, role: str, text: str = "", trace_id: str | None = None) -> None:
        super().__init__(master, fg_color="transparent")
        self.role = role
        self.trace_id = trace_id
        self.style = row_style(role)
        self.timestamp = time.strftime("%H:%M:%S")

        outer = ctk.CTkFrame(self, fg_color="transparent", height=1)
        outer.pack(fill="x")

        bubble = ctk.CTkFrame(
            outer, fg_color=self.style.background,
            corner_radius=theme.RADIUS_CARD,
        )
        if self.style.align == "right":
            bubble.pack(anchor="e", padx=(theme.SPACE_6, 0))
        else:
            bubble.pack(anchor="w", padx=(0, theme.SPACE_6))

        # A plain Tk frame on purpose: ctk.CTkFrame defaults to height=200,
        # which is inherited even with fill="y" and forces every row to 300
        # physical px of empty space. Tk.Frame with no -height has no opinion.
        if self.style.align == "right":
            tk.Frame(bubble, width=theme.SPINE_W, bg=self.style.spine).pack(
                side="right", fill="y"
            )
        else:
            tk.Frame(bubble, width=theme.SPINE_W, bg=self.style.spine).pack(
                side="left", fill="y"
            )

        self._label = ctk.CTkLabel(
            bubble, text=text, fg_color="transparent",
            text_color=self.style.text,
            wraplength=theme.WRAP_BASE,
            justify=self.style.justify,
            anchor=self.style.anchor,
            padx=theme.SPACE_3, pady=theme.SPACE_2,
            font=(theme.FONT_SANS, theme.FONT_BODY),
        )
        self._label.pack(fill="x")
        self.bubble = self._label

        # Timestamp chip: monospace, faint, never competing with the text.
        tag = "YOU" if role == "user" else ("ZAYTOON" if role == "assistant" else role.upper())
        chip = ctk.CTkLabel(
            outer, text=f"{tag}  {self.timestamp}",
            font=(theme.FONT_MONO, 9),
            text_color=theme.TEXT_FAINT,
            anchor="e" if self.style.align == "right" else "w",
        )
        chip.pack(anchor="e" if self.style.align == "right" else "w",
                  padx=(theme.SPACE_6, 0) if self.style.align == "right" else (theme.SPACE_5, 0))

    def set_text(self, text: str) -> None:
        self._label.configure(text=text)

    def get_text(self) -> str:
        return str(self._label.cget("text"))

    def set_wraplength(self, wrap: int) -> None:
        """Re-flow the text to the container's current width."""
        self._label.configure(wraplength=wrap)


class ChatPanel(ctk.CTkToplevel):
    """The expanded window: header, core, transcript, side panel, composer."""

    def __init__(
        self,
        master=None,
        *,
        width: int = 820,
        height: int = 620,
        on_send=None,
        on_mic_toggle=None,
        on_interrupt=None,
        on_collapse=None,
        on_confirm=None,
        on_quit=None,
        on_settings=None,
        config=None,
    ) -> None:
        super().__init__(master)
        self.title("Zaytoon AI")
        self.geometry(f"{width}x{height}")
        self.configure(fg_color=theme.BG0)
        self.minsize(MIN_WIDTH, MIN_HEIGHT)

        self._on_send = on_send
        self._on_mic_toggle = on_mic_toggle
        self._on_interrupt = on_interrupt
        self._on_collapse = on_collapse
        self._on_confirm = on_confirm
        self._on_quit = on_quit
        self._on_settings = on_settings
        self._config = config
        self._initial_width = int(width)

        self._rows: list[MessageRow] = []
        self._streaming: MessageRow | None = None
        self._pending_confirmation: dialogs.ConfirmationState | None = None
        self._dialog: dialogs.ConfirmationDialog | None = None
        self._settings_dialog = None
        self._wrap = 0
        self._plan: LayoutPlan | None = None
        self._side_enabled = True
        self.mic_on = False
        self.busy = False

        self._build()
        self._bind_keys()

    # -- construction -----------------------------------------------------
    def _build(self) -> None:
        self.grid_columnconfigure(BODY_COLUMN, weight=1)
        self.grid_rowconfigure(BODY_ROW, weight=1)

        self._build_header()

        # Body: activity strip over the three-column content row.
        body = ctk.CTkFrame(self, fg_color="transparent", height=1)
        body.grid(row=BODY_ROW, column=BODY_COLUMN, sticky="nsew",
                  padx=theme.SPACE_3, pady=(0, 0))
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(1, weight=1)
        self._body = body

        self.activity = ActivityFeed(body)
        self.activity.grid(row=0, column=0, sticky="ew", pady=(0, theme.SPACE_2))
        # Gridded once (so its options are remembered) and then hidden: the
        # feed appears only when the turn has something to report.
        self.activity.grid_remove()

        content = ctk.CTkFrame(body, fg_color="transparent", height=1)
        content.grid(row=1, column=0, sticky="nsew")
        self._content = content
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(1, weight=1)  # the transcript grows

        # col 0: the reactive core (dropped when narrow)
        self.core = CoreCanvas(content)
        self.core.grid(row=0, column=0, sticky="n", padx=(0, theme.SPACE_3))

        # col 1: the transcript
        self.transcript = ctk.CTkScrollableFrame(
            content, fg_color=theme.BG1, corner_radius=theme.RADIUS_PANEL,
            border_width=1, border_color=theme.HAIRLINE,
        )
        self.transcript.grid(row=0, column=1, sticky="nsew")
        self.transcript.grid_columnconfigure(0, weight=1)
        self.transcript.bind("<Configure>", self._on_transcript_configure)

        # col 2: the telemetry side panel
        self.side = SidePanel(content)
        self.side.grid(row=0, column=2, sticky="nsew", padx=(theme.SPACE_3, 0))

        self._build_quick_actions()
        self._build_composer()
        self._build_status_bar()
        self._apply_layout(self._window_width())

    def _build_header(self) -> None:
        # Wrapped so the hairline sits *under* the bar at any window height
        # instead of at one magic pady (two widgets in one grid cell overlap).
        wrap = ctk.CTkFrame(self, fg_color="transparent", height=1)
        wrap.grid(row=HEADER_ROW, column=BODY_COLUMN, sticky="ew",
                  padx=theme.SPACE_3, pady=(theme.SPACE_2, 0))
        wrap.grid_columnconfigure(0, weight=1)

        self.header = HeaderBar(
            wrap,
            on_settings=self.open_settings,
            on_minimize=self._win_minimize,
            on_maximize=self._win_maximize,
            on_close=self._emit(self._on_collapse) if self._on_collapse else self._emit(self._on_quit),
            on_toggle_side=self.toggle_side,
        )
        self.header.pack(fill="x")
        tk.Frame(wrap, height=1, bg=theme.HAIRLINE).pack(fill="x", pady=(theme.SPACE_2, 0))

    def _build_quick_actions(self) -> None:
        quick = ctk.CTkFrame(self, fg_color="transparent", height=1)
        quick.grid(row=QUICK_ROW, column=BODY_COLUMN, sticky="ew",
                   padx=theme.SPACE_3, pady=(theme.SPACE_2, 0))
        ctk.CTkLabel(
            quick, text=theme.letterspace("QUICK"), anchor="w",
            font=(theme.FONT_MONO, theme.FONT_SECTION, "bold"),
            text_color=theme.TEXT_FAINT,
        ).pack(side="left", padx=(0, theme.SPACE_2))
        for text, message in QUICK_ACTIONS:
            ctk.CTkButton(
                quick, text=text, height=22,
                corner_radius=theme.RADIUS_CONTROL,
                fg_color=theme.BG2, hover_color=theme.HAIRLINE,
                border_width=1, border_color=theme.HAIRLINE,
                text_color=theme.TEXT_MUTE,
                font=(theme.FONT_MONO, theme.FONT_SECTION),
                command=lambda m=message: self._quick(m),
            ).pack(side="left", padx=(0, theme.SPACE_2))

    def _build_composer(self) -> None:
        console = ctk.CTkFrame(
            self, fg_color=theme.BG1, corner_radius=theme.RADIUS_PANEL,
            border_width=1, border_color=theme.HAIRLINE,
        )
        console.grid(row=COMPOSER_ROW, column=BODY_COLUMN, sticky="ew",
                     padx=theme.SPACE_3, pady=(theme.SPACE_2, 0))
        console.grid_columnconfigure(1, weight=1)
        self._console = console

        ctk.CTkLabel(
            console, text=">", width=16,
            font=(theme.FONT_MONO, theme.FONT_BODY, "bold"),
            text_color=theme.CYAN,
        ).grid(row=0, column=0, padx=(theme.SPACE_2, 0))

        self.entry = ctk.CTkEntry(
            console,
            placeholder_text="ENTER COMMAND…",
            fg_color=theme.BG2,
            border_width=1, border_color=theme.HAIRLINE,
            text_color=theme.TEXT_BASE,
            placeholder_text_color=theme.TEXT_FAINT,
            font=(theme.FONT_MONO, theme.FONT_BODY),
            height=theme.ENTRY_H,
            corner_radius=theme.RADIUS_CONTROL,
        )
        self.entry.grid(row=0, column=1, sticky="ew", padx=(theme.SPACE_2, 0),
                        pady=theme.SPACE_2)
        self.entry.bind("<Return>", self._handle_return)
        self.entry.bind("<Shift-Return>", lambda _e: "break")

        self.mic_btn = self._control("MIC", self._emit(self._on_mic_toggle), column=2)
        self.send_btn = self._control("SEND", self._submit, column=3, primary=True)
        self.stop_btn = self._control("STOP", self._emit(self._on_interrupt), column=4)
        self._paint_stop()

    def _control(self, text, command, *, column: int, primary: bool = False):
        button = ctk.CTkButton(
            self._console, text=text, width=64 if primary else 56,
            height=theme.ENTRY_H,
            corner_radius=theme.RADIUS_CONTROL,
            fg_color=theme.tint(theme.CYAN, 0.12, theme.BG1) if primary else theme.BG2,
            hover_color=theme.HAIRLINE,
            border_width=1,
            border_color=theme.BORDER_STRONG if primary else theme.HAIRLINE,
            text_color=theme.CYAN if primary else theme.TEXT_MUTE,
            font=(theme.FONT_MONO, theme.FONT_SECTION, "bold"),
            command=command,
        )
        button.grid(row=0, column=column, padx=(theme.SPACE_2, theme.SPACE_2),
                    pady=theme.SPACE_2)
        return button

    def _build_status_bar(self) -> None:
        bar = ctk.CTkFrame(self, fg_color="transparent", height=theme.STATUS_HEIGHT)
        bar.grid(row=STATUS_ROW, column=BODY_COLUMN, sticky="ew",
                 padx=theme.SPACE_3, pady=(theme.SPACE_2, theme.SPACE_2))
        bar.grid_columnconfigure(1, weight=1)
        tk.Frame(bar, height=1, bg=theme.HAIRLINE).grid(
            row=0, column=0, columnspan=3, sticky="ew", pady=(0, 3)
        )

        self.status_dot = ctk.CTkLabel(
            bar, text="●", width=14, font=(theme.FONT_MONO, 9),
            text_color=theme.TEXT_FAINT,
        )
        self.status_dot.grid(row=1, column=0, sticky="w")
        # The glyph is a second channel so state is not colour-only.
        self.status_label = ctk.CTkLabel(
            bar, text="READY", anchor="w",
            font=(theme.FONT_MONO, theme.FONT_SECTION, "bold"),
            text_color=theme.TEXT_BASE,
        )
        self.status_label.grid(row=1, column=1, sticky="w", padx=(theme.SPACE_1, 0))
        self.hint_label = ctk.CTkLabel(
            bar, text="", anchor="e",
            font=(theme.FONT_MONO, theme.FONT_SECTION),
            text_color=theme.TEXT_FAINT,
        )
        self.hint_label.grid(row=1, column=2, sticky="e")

    def _bind_keys(self) -> None:
        self.bind("<Escape>", lambda _e: self._emit(self._on_collapse)())
        self.bind("<Configure>", self._on_resize)

    # -- layout -----------------------------------------------------------
    def _on_resize(self, event) -> None:
        """Re-plan the column set when the *window* width changes.

        Bound on the toplevel, so it also fires for every child's
        ``<Configure>`` (the toplevel is in each child's bindtags); those are
        filtered out here.
        """
        if event.widget is not self:
            return
        self._apply_layout(event.width)

    def _on_transcript_configure(self, event) -> None:
        wrap = compute_wrap(event.width)
        if wrap == self._wrap:
            return
        self._wrap = wrap
        for row in self._rows:
            row.set_wraplength(wrap)
        if self._streaming is not None:
            self._streaming.set_wraplength(wrap)

    def _window_width(self) -> int:
        """Current width, falling back to the configured one while unmapped."""
        width = self.winfo_width()
        return int(width) if width > 1 else self._initial_width

    def _apply_layout(self, width: int) -> None:
        if width <= 1:
            width = self._initial_width
        plan = layout_plan(width, self._side_enabled)
        compact = int(width) < COMPACT_WIDTH
        if plan == self._plan and compact == self.header.compact:
            return
        self._plan = plan
        self.header.set_compact(compact)

        # Core column: widget and its minsize appear/disappear together, since
        # a grid column with a minsize reserves space even when empty.
        self._content.grid_columnconfigure(0, minsize=plan.core_width, weight=0)
        if plan.core:
            self.core.grid()
        else:
            self.core.grid_remove()

        self._content.grid_columnconfigure(2, minsize=plan.side_width, weight=0)
        if plan.side:
            self.side.grid()
        else:
            self.side.grid_remove()

    def toggle_side(self) -> None:
        """Collapse/expand the side panel, remembering the choice."""
        self._side_enabled = not self._side_enabled
        self._plan = None  # force re-plan
        self._apply_layout(self._window_width())

    @property
    def side_enabled(self) -> bool:
        return self._side_enabled

    # -- callbacks --------------------------------------------------------
    @staticmethod
    def _emit(callback):
        """Wrap a callback so a ``None`` return is harmless in Tk commands."""
        if callback is None:
            return lambda: None

        def wrapper(*_args, **_kwargs):
            callback()

        return wrapper

    # -- window controls --------------------------------------------------
    def _win_minimize(self) -> None:
        try:
            self.iconify()
        except tk.TclError:  # pragma: no cover
            pass

    def _win_maximize(self) -> None:
        """Toggle the window manager's zoom; refuse politely if it will not."""
        try:
            self.state("normal" if self.state() == "zoomed" else "zoomed")
        except tk.TclError:  # pragma: no cover - WM without zoom
            pass

    def show(self) -> None:
        """Present the window (the ``--panel`` start path calls this)."""
        self.deiconify()
        self.lift()

    # -- input ------------------------------------------------------------
    def _handle_return(self, _event=None) -> str:
        self._submit()
        return "break"

    def _submit(self) -> None:
        text = self.entry.get().strip()
        if not text or self._on_send is None:
            return
        self.entry.delete(0, "end")
        self._on_send(text)

    def _quick(self, message: str) -> None:
        if self._on_send is not None:
            self._on_send(message)

    def set_busy(self, busy: bool) -> None:
        """Disable input while a turn is in flight — but never the STOP key."""
        self.busy = bool(busy)
        state = "disabled" if busy else "normal"
        try:
            self.entry.configure(state=state)
            self.send_btn.configure(state=state)
        except tk.TclError:  # pragma: no cover - destroyed mid-turn
            pass
        self._paint_stop()

    def _paint_stop(self) -> None:
        """STOP reads as *available* when idle and *urgent* during a turn."""
        active = self.busy
        self.stop_btn.configure(
            fg_color=theme.tint(theme.RED, 0.24, theme.BG1) if active else theme.BG2,
            border_color=theme.RED if active else theme.HAIRLINE,
            text_color=theme.RED if active else theme.TEXT_FAINT,
        )

    def set_mic(self, on: bool) -> None:
        """Record the mic state with glyph, colour and frame — three channels."""
        self.mic_on = on
        self.mic_btn.configure(
            text="MIC" if not on else "REC",
            text_color=theme.GREEN if on else theme.TEXT_MUTE,
            fg_color=theme.tint(theme.GREEN, 0.12, theme.BG1) if on else theme.BG2,
            border_color=theme.GREEN if on else theme.HAIRLINE,
        )
        self.header.set_mic(on)

    # -- header / side panel ---------------------------------------------
    def set_connection(self, value: str) -> None:
        self.header.set_connection(value)

    def set_model(self, name: str | None) -> None:
        self.header.set_model(name)
        self.side.set_model(name or "")

    def set_task(self, intent: str, status: str) -> None:
        self.side.set_task(intent, status)

    def set_tools(self, names: list[str]) -> None:
        self.side.set_tools(names)

    def set_system(self, cpu: str, ram: str) -> None:
        self.side.set_system(cpu, ram)

    def set_actions(self, items: list[str]) -> None:
        self.side.set_actions(items)

    def set_processes(self, items: list[str]) -> None:
        self.side.set_processes(items)

    def open_settings(self) -> None:
        """Env-backed configuration view. One instance at a time."""
        if self._settings_dialog is not None:
            try:
                if self._settings_dialog.winfo_exists():
                    self._settings_dialog.lift()
                    return
            except tk.TclError:  # pragma: no cover
                pass
        self._settings_dialog = dialogs.SettingsDialog(self, self._config)

    # -- activity ---------------------------------------------------------
    def show_plan(self, steps: list[str]) -> None:
        self.activity.show_plan(steps)

    def tool_started(self, name: str, label: str = "") -> None:
        self.activity.tool_started(name, label)

    def tool_progress(self, name: str, message: str = "") -> None:
        self.activity.tool_progress(name, message)

    def tool_finished(self, name: str, success: bool, summary: str = "") -> None:
        self.activity.tool_finished(name, success, summary)

    def set_typing(self, on: bool) -> None:
        self.activity.set_typing(on)

    # -- transcript -------------------------------------------------------
    def add_message(self, role: str, text: str) -> MessageRow:
        follow = self._at_bottom()
        row = MessageRow(self.transcript, role, text)
        row.grid(
            row=len(self._rows), column=0, sticky="ew",
            pady=(theme.SPACE_1, theme.SPACE_2),
        )
        if self._wrap:
            row.set_wraplength(self._wrap)
        self._rows.append(row)
        if follow:
            self._scroll_to_end()
        return row

    def append_token(self, token: str, trace_id: str | None = None) -> None:
        follow = self._at_bottom()
        if self._streaming is None:
            self._streaming = self.add_message("assistant", token)
            if trace_id:
                self._streaming.trace_id = trace_id
        else:
            self._streaming.set_text(self._streaming.get_text() + token)
            if trace_id:
                self._streaming.trace_id = trace_id
            if follow:
                self._scroll_to_end()
        self.set_typing(True)

    def set_transcript(self, text: str, trace_id: str | None = None) -> None:
        """Replace the streaming bubble with the authoritative final text."""
        if self._streaming is None:
            if text:
                self._streaming = self.add_message("assistant", text)
                if trace_id:
                    self._streaming.trace_id = trace_id
        else:
            self._streaming.set_text(text or self._streaming.get_text())
            if trace_id:
                self._streaming.trace_id = trace_id
        self._streaming = None
        self.set_typing(False)
        self._scroll_to_end()

    def clear_transcript(self) -> None:
        for row in self._rows:
            row.destroy()
        self._rows.clear()
        self._streaming = None

    def _at_bottom(self) -> bool:
        """Should the view follow new content?

        Only when the reader is already at the bottom: yanking the view back
        down while someone is reading back through history is the classic
        auto-scroll bug.
        """
        try:
            canvas = getattr(self.transcript, "_parent_canvas", None)
            if canvas is None:
                return True
            _start, end = canvas.yview()
            return float(end) >= 0.98
        except (AttributeError, tk.TclError, TypeError):  # pragma: no cover
            return True

    def _scroll_to_end(self) -> None:
        # The canvas only exists after the widget has been laid out once.
        self.after(30, self._force_scroll)

    def _force_scroll(self) -> None:
        try:
            canvas = self.transcript._parent_canvas
            if canvas is not None:
                canvas.yview_moveto(1.0)
        except (AttributeError, tk.TclError):  # pragma: no cover
            pass

    # -- status -----------------------------------------------------------
    def set_status(self, status: str, language: str = "en") -> None:
        """Legacy entry point: a raw HUD status string."""
        self.set_ui_state(states.status_to_state(status), language)

    def set_ui_state(self, state: UiState, language: str = "en", tool: str | None = None,
                     text: str | None = None) -> None:
        """Update the status line, header pill and core from one state.

        Word, glyph and colour are set together: the glyph is the channel that
        keeps state legible when colour alone would not be enough. ``text``
        lets the caller pass the label the state machine already resolved
        (some states have a bespoke one — "Generating response…" while tokens
        stream) instead of re-deriving a generic label here.
        """
        text = text or states.label(state, tool, language)
        color = states.accent(state)
        self.status_label.configure(text=text.upper(), text_color=color)
        self.status_dot.configure(text=states.STATE_GLYPH[state], text_color=color)
        self.header.set_state(state, text)
        self.core.set_state(state)

    def set_hint(self, text: str) -> None:
        self.hint_label.configure(text=text)

    # -- human-in-the-loop ------------------------------------------------
    def show_confirmation(self, payload: dict) -> None:
        """Open the approval modal for a ``require_confirmation`` request."""
        self.dismiss_confirmation()
        state = dialogs.ConfirmationState(payload)
        if not state.request_id:
            # A request with no id cannot be answered; refuse to pretend.
            return
        self._pending_confirmation = state
        self._dialog = dialogs.ConfirmationDialog(self, payload, self._deliver)
        self.set_ui_state(UiState.AWAITING_CONFIRM, tool=None)

    def _deliver(self, payload: dict) -> None:
        """Hand one built ``tool_confirm`` payload to the app layer."""
        self._pending_confirmation = None
        self._dialog = None
        if self._on_confirm is None:
            return
        request_id = str(payload.get("request_id") or "")
        approved = bool(payload.get("approved"))
        if request_id:
            self._on_confirm(request_id, approved)

    def resolve_confirmation(self, resolved: bool) -> None:
        """The backend reported the outcome of this request."""
        if self._pending_confirmation is not None:
            self._pending_confirmation.mark_resolved()
        dialog = self._dialog
        if dialog is not None:
            try:
                if resolved:
                    dialog.mark_resolved()
                else:
                    self.dismiss_confirmation()
            except tk.TclError:  # pragma: no cover - already gone
                pass

    def dismiss_confirmation(self) -> None:
        self._pending_confirmation = None
        dialog, self._dialog = self._dialog, None
        if dialog is not None:
            try:
                dialog.destroy()
            except tk.TclError:  # pragma: no cover
                pass

    @property
    def awaiting_confirmation(self) -> bool:
        if self._pending_confirmation is None:
            return False
        return self._pending_confirmation.active

    # -- reminders ----------------------------------------------------------
    def show_reminders(self, reminders: list[dict]) -> None:
        """Display a list of reminders in the transcript."""
        if not reminders:
            self.add_message("system", "No pending reminders.")
            return
        for r in reminders:
            rid = r.get("reminder_id") or r.get("id")
            task = r.get("task") or r.get("title", "?")
            time_raw = r.get("scheduled_time") or r.get("remind_time", "")
            rec = r.get("recurring", "none")
            rec_label = f" ({rec})" if rec and rec != "none" else ""
            self.add_message("system", f"#{rid}: {task} @ {time_raw}{rec_label}")

    def remove_reminder(self, reminder_id: int) -> None:
        """Remove a reminder from the transcript by its ID."""
        # Since reminders are added as system messages, we can't easily remove
        # a specific one without tracking. For now, add a confirmation message.
        self.add_message("system", f"Reminder #{reminder_id} completed.")
