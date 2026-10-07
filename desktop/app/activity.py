"""Per-turn activity: plan steps, tool rows, and the typing indicator.

The *model* here is plain data with no Tk in it — what the plan was, which
tools ran, what happened to them — so the rules that decide a row's glyph and
colour are unit-testable. The widget only renders the model the app hands it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import customtkinter as ctk

from . import theme
from .core import AnimationLoop

#: One status vocabulary for a tool row, and its (glyph, accent) rendering.
TOOL_GLYPHS: dict[str, tuple[str, str]] = {
    "running": ("●", "cyan"),
    "done": ("✓", "green"),
    "failed": ("✕", "red"),
}

#: Plan steps get their own glyph so a plan is never mistaken for a result.
PLAN_GLYPH = ("◇", "violet")

#: Never let the feed grow past this many rows in one turn.
MAX_TOOL_ROWS = 5


@dataclass
class ToolRow:
    name: str
    label: str
    status: str = "running"
    summary: str = ""


@dataclass
class ActivityModel:
    """Everything the current turn is doing, as data."""

    steps: list[str] = field(default_factory=list)
    tools: list[ToolRow] = field(default_factory=list)
    typing: bool = False

    def add_plan(self, steps: list[str]) -> None:
        self.steps = [str(s).strip() for s in steps if str(s).strip()]
        # A new plan means a new turn's worth of work; stale tool rows would
        # otherwise sit next to steps that come after them.
        self.tools = []

    def start_tool(self, name: str, label: str = "") -> ToolRow:
        row = ToolRow(name=name or "tool", label=(label or name or "tool"))
        # Same tool twice in a turn replaces the old row rather than duplicating.
        self.tools = [t for t in self.tools if t.name != row.name]
        self.tools.append(row)
        if len(self.tools) > MAX_TOOL_ROWS:
            self.tools = self.tools[-MAX_TOOL_ROWS:]
        return row

    def progress_tool(self, name: str, message: str = "") -> None:
        for row in self.tools:
            if row.name == name:
                if message:
                    row.summary = message
                return
        # A progress frame before its started frame: keep the row honest, and
        # keep the message too — dropping it would hide what the tool said.
        row = self.start_tool(name)
        if message:
            row.summary = message

    def finish_tool(self, name: str, success: bool, summary: str = "") -> ToolRow | None:
        for row in reversed(self.tools):
            if row.name == name:
                row.status = "done" if success else "failed"
                row.summary = summary or row.summary
                return row
        # Finished without a started frame (missed/queued): record it anyway.
        row = self.start_tool(name)
        row.status = "done" if success else "failed"
        row.summary = summary
        return row

    @property
    def active_tool(self) -> str | None:
        """The name of the tool currently running, if any."""
        for row in reversed(self.tools):
            if row.status == "running":
                return row.name
        return None

    def set_typing(self, on: bool) -> None:
        self.typing = bool(on)

    def reset(self) -> None:
        self.steps = []
        self.tools = []
        self.typing = False

    # -- rendering data ---------------------------------------------------
    def plan_rows(self) -> list[tuple[str, str, str]]:
        """``(glyph, accent_name, text)`` for the plan steps."""
        return [
            (PLAN_GLYPH[0], PLAN_GLYPH[1], f"{index:02d}  {step}")
            for index, step in enumerate(self.steps, start=1)
        ]

    def tool_rows(self) -> list[tuple[str, str, str]]:
        """``(glyph, accent_name, text)`` for the tool rows."""
        out: list[tuple[str, str, str]] = []
        for tool in self.tools:
            glyph, accent = TOOL_GLYPHS.get(tool.status, TOOL_GLYPHS["running"])
            text = tool.label or tool.name
            if tool.status != "running" and tool.summary:
                text = f"{text}  ·  {tool.summary}"
            out.append((glyph, accent, text))
        return out

    def rows(self) -> list[tuple[str, str, str]]:
        """``(glyph, accent_name, text)`` in display order."""
        return self.plan_rows() + self.tool_rows()

    @property
    def empty(self) -> bool:
        return not self.steps and not self.tools


def accent_hex(accent_name: str) -> str:
    return theme.ACCENTS.get(accent_name, theme.TEXT_MUTE)


class ActivityFeed(ctk.CTkFrame):
    """The strip between the header and the transcript.

    Hidden entirely when the turn has nothing to report, so an idle shell is
    one clean transcript rather than an empty grey band.
    """

    def __init__(self, master, **kwargs) -> None:
        super().__init__(master, fg_color="transparent", **kwargs)
        self.model = ActivityModel()

        self._body = ctk.CTkFrame(self, fg_color="transparent", height=1)
        self._body.pack(fill="x")
        self._body.grid_columnconfigure(0, weight=1)

        self._plan_label = ctk.CTkLabel(
            self._body, text="", anchor="w", justify="left",
            font=(theme.FONT_MONO, theme.FONT_BODY_SM),
            text_color=theme.TEXT_MUTE,
        )
        self._tools: list[ctk.CTkLabel] = []
        self._typing_label = ctk.CTkLabel(
            self._body, text="", anchor="w",
            font=(theme.FONT_MONO, theme.FONT_BODY_SM, "bold"),
            text_color=theme.CYAN,
        )

        # The typing dots share the animation discipline of the core: one
        # cancellable chain, stopped the moment the indicator goes off.
        self._dots = 0
        self._loop = AnimationLoop(
            self.after, self.after_cancel, self._tick, interval_ms=360
        )
        # NOTE: the shell grids this widget and then ``grid_remove()``s it —
        # ``grid remove`` on a never-gridded widget has no options to remember,
        # so the hide/show pair belongs to the caller that knows the layout.

    # -- model passthrough (widgets dispatch, they do not decide) ---------
    def show_plan(self, steps: list[str]) -> None:
        self.model.add_plan(steps)
        self._render()

    def tool_started(self, name: str, label: str = "") -> None:
        self.model.start_tool(name, label)
        self._render()

    def tool_progress(self, name: str, message: str = "") -> None:
        self.model.progress_tool(name, message)
        self._render()

    def tool_finished(self, name: str, success: bool, summary: str = "") -> None:
        self.model.finish_tool(name, success, summary)
        self._render()

    def set_typing(self, on: bool) -> None:
        on = bool(on)
        if on == self.model.typing:
            return  # tokens arrive per frame; do not re-render for each one
        self.model.set_typing(on)
        self._loop.set_mode("working" if on else "idle")
        if not on:
            self._typing_label.configure(text="")
        self._render()

    def reset(self) -> None:
        self.model.reset()
        self._loop.stop()
        self._typing_label.configure(text="")
        self._render()

    @property
    def active_tool(self) -> str | None:
        return self.model.active_tool

    # -- rendering --------------------------------------------------------
    def _tick(self) -> None:
        self._dots = (self._dots + 1) % 4
        self._typing_label.configure(text="·" * self._dots + "   " if self._dots else "   ")

    def _render(self) -> None:
        if not self.winfo_exists():
            return
        plan = self.model.plan_rows()
        steps_text = "\n".join(text for _g, _a, text in plan)
        self._plan_label.configure(text=steps_text)
        if steps_text:
            if not self._plan_label.winfo_ismapped():
                self._plan_label.pack(fill="x", pady=(0, 2))
        else:
            self._plan_label.pack_forget()

        tool_rows = self.model.tool_rows()
        # Recycle labels: rebuilding widgets each frame flickers.
        while len(self._tools) < len(tool_rows):
            label = ctk.CTkLabel(
                self._body, text="", anchor="w", justify="left",
                font=(theme.FONT_MONO, theme.FONT_BODY_SM),
                text_color=theme.TEXT_BASE,
            )
            self._tools.append(label)
        for index, label in enumerate(self._tools):
            if index < len(tool_rows):
                glyph, accent, text = tool_rows[index]
                label.configure(text=f"{glyph}  {text}", text_color=accent_hex(accent))
                if not label.winfo_ismapped():
                    label.pack(fill="x")
            else:
                label.pack_forget()

        if self.model.typing:
            if not self._typing_label.winfo_ismapped():
                self._typing_label.pack(fill="x")
        else:
            self._typing_label.pack_forget()

        hidden = self.model.empty and not self.model.typing
        if hidden:
            self.grid_remove()
        else:
            self.grid()

    def destroy(self) -> None:
        self._loop.stop()
        super().destroy()
