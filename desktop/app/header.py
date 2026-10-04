"""The header bar: identity, telemetry pills, settings, window controls.

Everything the header shows arrives through ``set_*`` calls — it holds no
connection, model or audio state of its own, so it can never disagree with the
rest of the shell. The pills use one recipe (hairline border + a 12% accent
tint + accent text) defined by ``theme.pill_colors``.
"""

from __future__ import annotations

import customtkinter as ctk

from . import theme, states
from .states import UiState


class Pill(ctk.CTkFrame):
    """Status pill: hairline border, 12% tinted accent fill, accent text."""

    def __init__(self, master, *, text: str = "", accent: str = theme.CYAN, **kwargs) -> None:
        background, border, _text_color = theme.pill_colors(accent)
        super().__init__(
            master,
            fg_color=background,
            border_width=1,
            border_color=border,
            corner_radius=theme.RADIUS_CONTROL,
            **kwargs,
        )
        self.accent = accent
        self.label = ctk.CTkLabel(
            self, text=text, anchor="w",
            font=(theme.FONT_MONO, theme.FONT_SECTION, "bold"),
            text_color=accent,
        )
        self.label.pack(fill="x", padx=theme.SPACE_2, pady=1)

    def set(self, text: str, accent: str | None = None) -> None:
        if accent is not None and accent != self.accent:
            self.accent = accent
            background, border, _ = theme.pill_colors(accent)
            self.configure(fg_color=background, border_color=border)
            self.label.configure(text_color=accent)
        self.label.configure(text=text)


class HeaderBar(ctk.CTkFrame):
    """Wordmark + pills + settings + the window's own controls."""

    def __init__(
        self,
        master,
        *,
        on_settings=None,
        on_minimize=None,
        on_maximize=None,
        on_close=None,
        on_toggle_side=None,
        **kwargs,
    ) -> None:
        super().__init__(master, fg_color=theme.BG1, **kwargs)
        self._on_settings = on_settings
        self._on_minimize = on_minimize
        self._on_maximize = on_maximize
        self._on_close = on_close
        self._on_toggle_side = on_toggle_side
        #: Tracked here rather than read back from ``winfo_ismapped``, which
        #: lies while the window is withdrawn.
        self._compact = False

        # -- identity ------------------------------------------------------
        identity = ctk.CTkFrame(self, fg_color="transparent")
        identity.pack(side="left", padx=(theme.SPACE_3, theme.SPACE_4))

        # A cyan accent dot is the whole logo mark: no asset, no emoji.
        ctk.CTkLabel(
            identity, text="●", width=14,
            font=(theme.FONT_MONO, 9), text_color=theme.CYAN,
        ).pack(side="left")
        ctk.CTkLabel(
            identity, text=theme.letterspace("ZAYTOON"),
            font=(theme.FONT_MONO, theme.FONT_BODY, "bold"),
            text_color=theme.TEXT_BASE,
        ).pack(side="left")

        # -- telemetry pills ------------------------------------------------
        self.state_pill = Pill(self, text="● READY", accent=theme.CYAN)
        self.state_pill.pack(side="left", padx=(0, theme.SPACE_2), pady=theme.SPACE_2)

        self.connection_pill = Pill(self, text="OFFLINE", accent=theme.RED)
        self.connection_pill.pack(side="left", padx=(0, theme.SPACE_2), pady=theme.SPACE_2)

        self.model_pill = Pill(self, text="MODEL: —", accent=theme.VIOLET)
        self.model_pill.pack(side="left", padx=(0, theme.SPACE_2), pady=theme.SPACE_2)

        self.mic_pill = Pill(self, text="MIC: OFF", accent=theme.TEXT_FAINT)
        self.mic_pill.pack(side="left", padx=(0, theme.SPACE_2), pady=theme.SPACE_2)

        # -- right-hand controls -------------------------------------------
        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.pack(side="right", padx=(theme.SPACE_2, theme.SPACE_2))
        self._controls = controls

        if self._on_toggle_side is not None:
            self._button(controls, "SIDE", self._call(self._on_toggle_side), width=44).pack(
                side="left", padx=2
            )
        self._button(controls, "SETTINGS", self._call(self._on_settings), width=76).pack(
            side="left", padx=2
        )

        separator = ctk.CTkFrame(controls, fg_color=theme.HAIRLINE, width=1)
        separator.pack(side="left", fill="y", padx=theme.SPACE_2, pady=4)

        # Native chrome stays (the window keeps its OS title bar); these are
        # the same three operations expressed in the shell's own visual
        # language, so both paths work.
        self._button(controls, "─", self._call(self._on_minimize), width=28).pack(
            side="left", padx=2
        )
        self._button(controls, "□", self._call(self._on_maximize), width=28).pack(
            side="left", padx=2
        )
        self._button(controls, "✕", self._call(self._on_close), width=28,
                     danger=True).pack(side="left", padx=2)

    # -- construction helpers ---------------------------------------------
    def _button(self, master, text, command, *, width: int = 28, danger: bool = False):
        return ctk.CTkButton(
            master, text=text, width=width, height=22,
            corner_radius=theme.RADIUS_CONTROL,
            fg_color=theme.BG2, hover_color=theme.HAIRLINE if not danger else "#3a1420",
            border_width=1, border_color=theme.HAIRLINE,
            text_color=theme.RED if danger else theme.TEXT_MUTE,
            font=(theme.FONT_MONO, theme.FONT_SECTION),
            command=command,
        )

    @staticmethod
    def _call(callback):
        """A ``None`` callback must still be a callable Tk command."""
        if callback is None:
            return lambda: None

        def wrapper(*_args, **_kwargs):
            callback()

        return wrapper

    # -- state -------------------------------------------------------------
    def set_state(self, state: UiState, text: str = "") -> None:
        accent = theme.ACCENTS[states.STATE_ACCENT[state]]
        glyph = states.STATE_GLYPH[state]
        label = text or states.label(state)
        self.state_pill.set(f"{glyph} {label.upper()}", accent)

    def set_connection(self, value: str) -> None:
        """``connected`` / ``reconnecting`` / ``offline`` -> the pill's colour."""
        if value == states.CONNECTION_CONNECTED:
            self.connection_pill.set("CONNECTED", theme.GREEN)
        elif value == states.CONNECTION_RECONNECTING:
            self.connection_pill.set("RECONNECTING", theme.AMBER)
        else:
            self.connection_pill.set("OFFLINE", theme.RED)

    def set_model(self, name: str | None) -> None:
        self.model_pill.set(f"MODEL: {name or '—'}", theme.VIOLET)

    def set_mic(self, on: bool) -> None:
        if on:
            self.mic_pill.set("MIC: LIVE", theme.GREEN)
        else:
            self.mic_pill.set("MIC: OFF", theme.TEXT_FAINT)

    def set_compact(self, compact: bool) -> None:
        """Below a comfortable width the pills yield to the transcript.

        The connection pill is exempt: "are we talking to the backend?" is the
        one thing a narrow window must still answer.
        """
        compact = bool(compact)
        if compact == self._compact:
            return
        self._compact = compact
        pills = (self.state_pill, self.connection_pill, self.model_pill, self.mic_pill)
        for pill in pills:
            pill.pack_forget()
        for pill in pills:
            if compact and pill is not self.connection_pill:
                continue
            # ``before=`` restores the build order, so the header looks the
            # same after a collapse/expand cycle as it did before one.
            pill.pack(side="left", before=self._controls,
                      padx=(0, theme.SPACE_2), pady=theme.SPACE_2)

    @property
    def compact(self) -> bool:
        return self._compact
