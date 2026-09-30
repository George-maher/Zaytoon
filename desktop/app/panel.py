"""The expanded chat panel.

A frameless, always-on-top window holding the transcript, the composer, the
human-in-the-loop approval dialog, and a live status bar. Every widget is
created once and updated in place — rebuilding the message list on each token
would flicker and leak widgets.
"""

from __future__ import annotations

import tkinter as tk

import customtkinter as ctk

from . import theme


class MessageRow(ctk.CTkFrame):
    """One transcript entry, framed as a system transmission.

    Square corners and a 2px accent spine on the anchored side, rather than a
    rounded bubble. The spine is cyan for the human and blue for the assistant,
    so the two remain distinguishable in greyscale as well as by colour.
    """

    def __init__(self, master, role: str, text: str = "") -> None:
        super().__init__(master, fg_color="transparent")
        self.role = role

        if role == "user":
            spine, justify, anchor = theme.CYAN, "right", "e"
        elif role == "assistant":
            spine, justify, anchor = theme.BLUE, "left", "w"
        elif role == "system":
            spine, justify, anchor = theme.TEXT_MUTED, "left", "w"
        else:
            spine, justify, anchor = theme.DANGER, "left", "w"

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=(48 if role != "user" else 0, 48 if role == "user" else 0))

        marker = ctk.CTkFrame(row, width=2, fg_color=spine, corner_radius=0)
        marker.pack(side="left", fill="y", padx=(0, 8))

        self.bubble = ctk.CTkLabel(
            row,
            text=text,
            fg_color=theme.DEEP,
            text_color=theme.TEXT,
            corner_radius=theme.RADIUS,
            wraplength=560,
            justify=justify,
            anchor=anchor,
            padx=12,
            pady=8,
            font=("Segoe UI", 12),
        )
        self.bubble.pack(side="left", fill="x", expand=True)
        self._label = self.bubble

    def set_text(self, text: str) -> None:
        self._label.configure(text=text)


class ChatPanel(ctk.CTkToplevel):
    """Expanded window: transcript, composer, approvals, status bar."""

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
    ) -> None:
        super().__init__(master)
        self.title("Zaytoon AI")
        self.geometry(f"{width}x{height}")
        self.configure(fg_color=theme.BG)
        self.minsize(420, 380)

        self._on_send = on_send
        self._on_mic_toggle = on_mic_toggle
        self._on_interrupt = on_interrupt
        self._on_collapse = on_collapse
        self._on_confirm = on_confirm
        self._on_quit = on_quit

        self._rows: list[MessageRow] = []
        self._streaming: MessageRow | None = None
        self._pending_confirmation: str | None = None
        self.mic_on = False

        self._build()
        self._bind_keys()

    # -- construction -----------------------------------------------------
    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_header()

        # Transcript
        self.transcript = ctk.CTkScrollableFrame(self, fg_color=theme.BG, corner_radius=0)
        self.transcript.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 8))
        self.transcript.grid_columnconfigure(0, weight=1)

        self._build_composer()
        self._build_status_bar()

    def _build_header(self) -> None:
        """Identity block plus window controls, on a hairline-ruled header."""
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 8))
        header.grid_columnconfigure(0, weight=1)

        # Identity mark: a framed "Z" plus the wordmark, matching the web
        # header's square badge + letterspaced identity.
        badge = ctk.CTkFrame(header, width=28, height=28, fg_color=theme.RAISED,
                             border_width=1, border_color=theme.EDGE_STRONG,
                             corner_radius=theme.RADIUS)
        badge.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            badge, text="Z", width=28, height=28, font=("Consolas", 15, "bold"),
            text_color=theme.CYAN_CORE,
        ).place(relx=0.5, rely=0.5, anchor="center")

        titles = ctk.CTkFrame(header, fg_color="transparent")
        titles.grid(row=0, column=1, sticky="w", padx=(10, 0))
        ctk.CTkLabel(
            titles, text="Z A Y T O O N", anchor="w",
            font=("Consolas", 13, "bold"), text_color=theme.CYAN_CORE,
        ).pack(anchor="w")
        ctk.CTkLabel(
            titles, text="TACTICAL COMMAND INTERFACE", anchor="w",
            font=("Consolas", 8), text_color=theme.TEXT_MUTED,
        ).pack(anchor="w")

        # 1px rule under the header, the web's panel divider.
        ctk.CTkFrame(self, height=1, fg_color=theme.EDGE_FAINT,
                     corner_radius=0).grid(row=0, column=0, sticky="sew", padx=16,
                                           pady=(52, 0))

        self.collapse_btn = ctk.CTkButton(
            header, text="—", width=32, height=26, corner_radius=theme.RADIUS,
            fg_color=theme.RAISED, hover_color=theme.EDGE,
            border_width=1, border_color=theme.EDGE,
            text_color=theme.TEXT_DIM, command=self._emit(self._on_collapse),
        )
        self.collapse_btn.grid(row=0, column=2, padx=(8, 6))

        self.quit_btn = ctk.CTkButton(
            header, text="✕", width=32, height=26, corner_radius=theme.RADIUS,
            fg_color=theme.RAISED, hover_color=theme.DANGER,
            border_width=1, border_color=theme.EDGE,
            text_color=theme.TEXT_DIM, command=self._emit(self._on_quit),
        )
        self.quit_btn.grid(row=0, column=3)

    def _build_composer(self) -> None:
        """Command console: prompt glyph, input, mic, stop, send."""
        console = ctk.CTkFrame(self, fg_color=theme.DEEP, corner_radius=theme.RADIUS,
                               border_width=1, border_color=theme.EDGE)
        console.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 8))
        console.grid_columnconfigure(1, weight=1)

        # Prompt glyph: the desktop equivalent of the web console's ">".
        ctk.CTkLabel(
            console, text=">", width=18, font=("Consolas", 14, "bold"),
            text_color=theme.CYAN,
        ).grid(row=0, column=0, padx=(10, 0))

        self.entry = ctk.CTkEntry(
            console,
            placeholder_text="ENTER COMMAND…",
            fg_color="transparent",
            border_width=0,
            text_color=theme.TEXT,
            font=("Consolas", 12),
            height=42,
        )
        self.entry.grid(row=0, column=1, sticky="ew", padx=(6, 0), pady=4)
        self.entry.bind("<Return>", self._handle_return)
        self.entry.bind("<Shift-Return>", lambda _e: "break")

        # Square hairline-framed controls. The mic is a drawn glyph rather than
        # an emoji so it matches the web's outline icon set and renders the
        # same on every machine.
        self.send_btn = ctk.CTkButton(
            console, text="SEND", width=76, height=42, corner_radius=theme.RADIUS,
            fg_color="#0A2A3A", hover_color="#12455E",
            border_width=1, border_color=theme.EDGE,
            text_color=theme.CYAN, font=("Consolas", 10, "bold"),
            command=self._submit,
        )
        self.send_btn.grid(row=0, column=2, padx=(8, 0), pady=4)

        self.mic_btn = ctk.CTkButton(
            console, text="●", width=42, height=42, corner_radius=theme.RADIUS,
            fg_color=theme.RAISED, hover_color=theme.EDGE,
            border_width=1, border_color=theme.EDGE,
            text_color=theme.TEXT_MUTED, font=("Consolas", 13),
            command=self._emit(self._on_mic_toggle),
        )
        self.mic_btn.grid(row=0, column=3, padx=(8, 0), pady=4)

        self.stop_btn = ctk.CTkButton(
            console, text="■", width=42, height=42, corner_radius=theme.RADIUS,
            fg_color=theme.RAISED, hover_color=theme.DANGER,
            border_width=1, border_color=theme.EDGE,
            text_color=theme.TEXT_MUTED, font=("Consolas", 11),
            command=self._emit(self._on_interrupt),
        )
        self.stop_btn.grid(row=0, column=4, padx=(6, 4), pady=4)

    def _build_status_bar(self) -> None:
        """Telemetry strip: LED, state word, and a hint field."""
        bar = ctk.CTkFrame(self, fg_color="transparent", height=26)
        bar.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 10))
        bar.grid_columnconfigure(1, weight=1)

        ctk.CTkFrame(bar, height=1, fg_color=theme.EDGE_FAINT,
                     corner_radius=0).grid(row=0, column=0, columnspan=3,
                                           sticky="ew", pady=(0, 6))

        self.status_dot = ctk.CTkLabel(
            bar, text="■", width=14, font=("Consolas", 9), text_color=theme.TEXT_MUTED
        )
        self.status_dot.grid(row=1, column=0, sticky="w")

        # The glyph is a second channel so state is not colour-only.
        self.status_label = ctk.CTkLabel(
            bar, text="READY", anchor="w", font=("Consolas", 10, "bold"),
            text_color=theme.TEXT_DIM,
        )
        self.status_label.grid(row=1, column=1, sticky="w")

        self.hint_label = ctk.CTkLabel(
            bar, text="", anchor="e", font=("Consolas", 9), text_color=theme.TEXT_MUTED
        )
        self.hint_label.grid(row=1, column=2, sticky="e")

    def _bind_keys(self) -> None:
        self.bind("<Escape>", lambda _e: self._emit(self._on_collapse)())

    @staticmethod
    def _emit(callback):
        """Wrap a callback so a ``False`` return is harmless in Tk commands."""
        if callback is None:
            return lambda: None

        def wrapper(*_args, **_kwargs):
            callback()

        return wrapper

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

    def set_busy(self, busy: bool) -> None:
        """Disable input while a turn is in flight, so replies cannot interleave."""
        self.entry.configure(state="disabled" if busy else "normal")
        self.send_btn.configure(state="disabled" if busy else "normal")

    def set_mic(self, on: bool) -> None:
        """Record the mic state with glyph, colour and frame -- three channels.

        The button previously swapped between two emoji. An emoji is a
        pictograph, not an instrument glyph, it renders differently per
        machine, and it was the only thing indicating that a live microphone
        was open. The recording state is now a filled dot in the success hue
        inside a lit frame, matching the web console's REC indicator.
        """
        self.mic_on = on
        self.mic_btn.configure(
            text="●" if on else "○",
            text_color=theme.GREEN if on else theme.TEXT_MUTED,
            fg_color="#062A1C" if on else theme.RAISED,
            border_color=theme.SUCCESS if on else theme.EDGE,
        )

    # -- transcript -------------------------------------------------------
    def add_message(self, role: str, text: str) -> MessageRow:
        row = MessageRow(self.transcript, role, text)
        row.grid(row=len(self._rows), column=0, sticky="ew", pady=4)
        self._rows.append(row)
        self._scroll_to_end()
        return row

    def append_token(self, token: str) -> None:
        if self._streaming is None:
            self._streaming = self.add_message("assistant", token)
        else:
            self._streaming.set_text(self._streaming._label.cget("text") + token)
            self._scroll_to_end()

    def set_transcript(self, text: str) -> None:
        """Replace the streaming bubble with the authoritative final text."""
        if self._streaming is None:
            if text:
                self._streaming = self.add_message("assistant", text)
        else:
            self._streaming.set_text(text or self._streaming._label.cget("text"))
        self._streaming = None
        self._scroll_to_end()

    def clear_transcript(self) -> None:
        for row in self._rows:
            row.destroy()
        self._rows.clear()
        self._streaming = None

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
        """Update the telemetry strip from the shared state vocabulary.

        Word, glyph and colour are set together. The glyph is not decoration:
        it is the channel that keeps the state legible when colour alone would
        not be enough.
        """
        word = theme.status_text(status, language)
        color = theme.state_hue(status)
        glyph = theme.state_glyph(status)

        self.status_label.configure(text=word, text_color=color)
        self.status_dot.configure(text=glyph, text_color=color)

    def set_hint(self, text: str) -> None:
        self.hint_label.configure(text=text)

    # -- human-in-the-loop ------------------------------------------------
    def show_confirmation(self, payload: dict) -> None:
        """Render a ``require_confirmation`` request and wait for a click."""
        self._pending_confirmation = payload.get("request_id")
        self.dismiss_confirmation()

        card = ctk.CTkFrame(self, fg_color=theme.RAISED, corner_radius=theme.RADIUS,
                            border_width=1, border_color=theme.WARNING)
        card.grid(row=4, column=0, sticky="ew", padx=16, pady=(0, 8))
        card.grid_columnconfigure(0, weight=1)
        self._confirmation_card = card

        action = payload.get("action", "tool")
        summary = payload.get("summary") or payload.get("text") or action
        reasons = payload.get("reasons") or []
        level = payload.get("level", "high")

        ctk.CTkLabel(
            card, text="▲  AWAIT CONFIRM",
            font=("Consolas", 12, "bold"), text_color=theme.WARNING, anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=14, pady=(12, 2))

        ctk.CTkLabel(
            card, text=str(summary), text_color=theme.TEXT, anchor="w",
            font=("Segoe UI", 12), wraplength=640, justify="left",
        ).grid(row=1, column=0, sticky="w", padx=14)

        if reasons:
            ctk.CTkLabel(
                card, text="RISK: " + ", ".join(str(r) for r in reasons),
                text_color=theme.TEXT_MUTED, anchor="w", font=("Consolas", 9),
                wraplength=640, justify="left",
            ).grid(row=2, column=0, sticky="w", padx=14, pady=(2, 0))

        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.grid(row=3, column=0, sticky="e", padx=12, pady=(8, 12))
        ctk.CTkButton(
            buttons, text="DENY", width=92, height=32, corner_radius=theme.RADIUS,
            fg_color="#2A0C14", hover_color=theme.DANGER,
            border_width=1, border_color=theme.DANGER,
            text_color=theme.DANGER, font=("Consolas", 10, "bold"),
            command=lambda: self._resolve_confirmation(False),
        ).grid(row=0, column=0, padx=(0, 8))
        ctk.CTkButton(
            buttons, text=f"APPROVE ({str(level).upper()})", width=150, height=32,
            corner_radius=theme.RADIUS,
            fg_color="#062A1C", hover_color=theme.SUCCESS,
            border_width=1, border_color=theme.SUCCESS,
            text_color=theme.SUCCESS, font=("Consolas", 10, "bold"),
            command=lambda: self._resolve_confirmation(True),
        ).grid(row=0, column=1)

        self.set_status("awaiting_confirmation")
        card.focus_set()
        card.lift()
        card.after(50, lambda: card.focus_force())

    def _resolve_confirmation(self, approved: bool) -> None:
        request_id, self._pending_confirmation = self._pending_confirmation, None
        self.dismiss_confirmation()
        if request_id and self._on_confirm is not None:
            self._on_confirm(request_id, approved)

    def dismiss_confirmation(self) -> None:
        card = getattr(self, "_confirmation_card", None)
        if card is not None:
            card.destroy()
            self._confirmation_card = None

    @property
    def awaiting_confirmation(self) -> bool:
        return self._pending_confirmation is not None

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
        self.add_message("system", f"✅ Reminder #{reminder_id} completed.")
