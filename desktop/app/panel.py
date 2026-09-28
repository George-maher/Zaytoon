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
    """One chat bubble, aligned by speaker."""

    def __init__(self, master, role: str, text: str = "") -> None:
        super().__init__(master, fg_color="transparent")
        self.role = role

        if role == "user":
            fg, anchor, justify = theme.ACCENT_DIM, "e", "right"
        elif role == "assistant":
            fg, anchor, justify = theme.BG_ELEVATED, "w", "left"
        elif role == "system":
            fg, anchor, justify = theme.BG_PANEL, "w", "left"
        else:
            fg, anchor, justify = theme.DANGER, "w", "left"

        self.bubble = ctk.CTkLabel(
            self,
            text=text,
            fg_color=fg,
            text_color=theme.TEXT,
            corner_radius=12,
            wraplength=560,
            justify=justify,
            anchor=anchor,
            padx=12,
            pady=8,
        )
        self.bubble.pack(anchor=anchor, padx=(56 if role == "user" else 0, 56 if role != "user" else 0))
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
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 8))
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header, text="Zaytoon AI", font=("Segoe UI", 17, "bold"),
            text_color=theme.TEXT, anchor="w",
        ).grid(row=0, column=0, sticky="w")

        self.collapse_btn = ctk.CTkButton(
            header, text="—", width=34, height=28, corner_radius=8,
            fg_color=theme.BG_ELEVATED, hover_color=theme.BORDER,
            text_color=theme.TEXT_MUTED, command=self._emit(self._on_collapse),
        )
        self.collapse_btn.grid(row=0, column=1, padx=(8, 6))

        self.quit_btn = ctk.CTkButton(
            header, text="✕", width=34, height=28, corner_radius=8,
            fg_color=theme.BG_ELEVATED, hover_color=theme.DANGER,
            text_color=theme.TEXT_MUTED, command=self._emit(self._on_quit),
        )
        self.quit_btn.grid(row=0, column=2)

    def _build_composer(self) -> None:
        composer = ctk.CTkFrame(self, fg_color="transparent")
        composer.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 8))
        composer.grid_columnconfigure(0, weight=1)

        self.entry = ctk.CTkEntry(
            composer,
            placeholder_text="Ask Zaytoon…  (Enter to send, Shift+Enter for a new line)",
            fg_color=theme.BG_ELEVATED,
            border_color=theme.BORDER,
            text_color=theme.TEXT,
            corner_radius=14,
            height=40,
        )
        self.entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.entry.bind("<Return>", self._handle_return)
        self.entry.bind("<Shift-Return>", lambda _e: "break")

        self.send_btn = ctk.CTkButton(
            composer, text="Send", width=72, height=40, corner_radius=14,
            fg_color=theme.ACCENT_DIM, hover_color=theme.ACCENT,
            text_color=theme.TEXT, command=self._submit,
        )
        self.send_btn.grid(row=0, column=1)

        self.mic_btn = ctk.CTkButton(
            composer, text="🎤", width=44, height=40, corner_radius=14,
            fg_color=theme.BG_ELEVATED, hover_color=theme.BORDER,
            text_color=theme.TEXT, command=self._emit(self._on_mic_toggle),
        )
        self.mic_btn.grid(row=0, column=2, padx=(8, 0))

        self.stop_btn = ctk.CTkButton(
            composer, text="■", width=44, height=40, corner_radius=14,
            fg_color=theme.BG_ELEVATED, hover_color=theme.DANGER,
            text_color=theme.TEXT, command=self._emit(self._on_interrupt),
        )
        self.stop_btn.grid(row=0, column=3, padx=(8, 0))

    def _build_status_bar(self) -> None:
        bar = ctk.CTkFrame(self, fg_color="transparent", height=26)
        bar.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 10))
        bar.grid_columnconfigure(1, weight=1)

        self.status_dot = ctk.CTkLabel(bar, text="●", width=14, text_color=theme.TEXT_MUTED)
        self.status_dot.grid(row=0, column=0, sticky="w")

        self.status_label = ctk.CTkLabel(
            bar, text="Starting…", anchor="w", text_color=theme.TEXT_MUTED
        )
        self.status_label.grid(row=0, column=1, sticky="w")

        self.hint_label = ctk.CTkLabel(
            bar, text="", anchor="e", text_color=theme.TEXT_MUTED
        )
        self.hint_label.grid(row=0, column=2, sticky="e")

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
        self.mic_on = on
        self.mic_btn.configure(
            text="🔴" if on else "🎤",
            fg_color=theme.DANGER if on else theme.BG_ELEVATED,
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
        self.status_label.configure(text=theme.status_text(status, language))
        color = theme.WARNING if status == "awaiting_confirmation" else theme.ACCENT
        if status == "disconnected":
            color = theme.TEXT_MUTED
        elif status == "error":
            color = theme.DANGER
        self.status_dot.configure(text_color=color)

    def set_hint(self, text: str) -> None:
        self.hint_label.configure(text=text)

    # -- human-in-the-loop ------------------------------------------------
    def show_confirmation(self, payload: dict) -> None:
        """Render a ``require_confirmation`` request and wait for a click."""
        self._pending_confirmation = payload.get("request_id")
        self.dismiss_confirmation()

        card = ctk.CTkFrame(self, fg_color=theme.BG_ELEVATED, corner_radius=12)
        card.grid(row=4, column=0, sticky="ew", padx=16, pady=(0, 8))
        card.grid_columnconfigure(0, weight=1)
        self._confirmation_card = card

        action = payload.get("action", "tool")
        summary = payload.get("summary") or payload.get("text") or action
        reasons = payload.get("reasons") or []
        level = payload.get("level", "high")

        ctk.CTkLabel(
            card, text="Approval required",
            font=("Segoe UI", 13, "bold"), text_color=theme.WARNING, anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=14, pady=(12, 2))

        ctk.CTkLabel(
            card, text=str(summary), text_color=theme.TEXT, anchor="w",
            wraplength=640, justify="left",
        ).grid(row=1, column=0, sticky="w", padx=14)

        if reasons:
            ctk.CTkLabel(
                card, text="Risk: " + ", ".join(str(r) for r in reasons),
                text_color=theme.TEXT_MUTED, anchor="w", wraplength=640, justify="left",
            ).grid(row=2, column=0, sticky="w", padx=14, pady=(2, 0))

        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.grid(row=3, column=0, sticky="e", padx=12, pady=(8, 12))
        ctk.CTkButton(
            buttons, text="Deny", width=88, height=32, corner_radius=8,
            fg_color=theme.DANGER, hover_color="#d63a58", text_color=theme.TEXT,
            command=lambda: self._resolve_confirmation(False),
        ).grid(row=0, column=0, padx=(0, 8))
        ctk.CTkButton(
            buttons, text=f"Approve ({level})", width=120, height=32, corner_radius=8,
            fg_color=theme.SUCCESS, hover_color="#00cc6e", text_color="#04241a",
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
