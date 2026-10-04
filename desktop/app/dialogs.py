"""Modal dialogs: the execution approval and the settings view.

The approval dialog is security-critical UI, so its *logic* lives here as plain
data (:class:`ConfirmationState`) rather than inside widget callbacks:

* the payload sent back to the backend is built by :func:`build_payload`,
  which is the same ``protocol.tool_confirm`` builder the rest of the shell
  uses — one shape, tested;
* a decision can be taken only **once**; if the request was already resolved
  (another client answered it, or the backend timed it out), later clicks
  return ``None`` instead of sending a second answer;
* cancel is deny: the buttons, the window's close box and an unresponsive
  dialog all produce ``approved: false`` or nothing at all.

Nothing in this module touches the network; the caller sends what it is given.
"""

from __future__ import annotations

import json
import tkinter as tk

import customtkinter as ctk

from . import protocol, theme

#: The dialog's title, fixed and human-written (never model output).
DIALOG_TITLE = "ZAYTOON LOCAL EXECUTION"

#: Risk level -> accent. Anything unrecognised reads as high.
RISK_ACCENTS: dict[str, str] = {
    "low": theme.GREEN,
    "safe": theme.GREEN,
    "medium": theme.AMBER,
    "confirm": theme.AMBER,
    "high": theme.RED,
    "dangerous": theme.RED,
}


def build_payload(request_id: str, approved: bool) -> dict:
    """The exact frame the dialog sends for a decision."""
    return protocol.tool_confirm(request_id, approved)


def risk_level(payload: dict) -> str:
    """Normalise the backend's ``level`` to ``low`` / ``medium`` / ``high``.

    Unknown levels are treated as high: the dialog must never under-sell a
    request whose risk it does not understand.
    """
    raw = str(payload.get("level") or "").strip().lower()
    if raw in ("low", "safe"):
        return "low"
    if raw in ("medium", "confirm", "moderate", "warning"):
        return "medium"
    return "high"


def risk_accent(level: str) -> str:
    return RISK_ACCENTS.get(str(level).lower(), theme.RED)


def command_text(payload: dict) -> str:
    """The code block: an explicit command when there is one, else the action
    with its parameters rendered as JSON."""
    for key in ("command", "code", "script"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    action = payload.get("action") or payload.get("name") or "tool"
    params = payload.get("params")
    if isinstance(params, dict) and params:
        try:
            rendered = json.dumps(params, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):  # pragma: no cover - non-serialisable
            rendered = str(params)
        return f"{action}\n{rendered}"
    if params not in (None, "", {}):
        return f"{action} {params}"
    return str(action)


def directory_of(payload: dict) -> str:
    """Working directory for the request, when the backend reports one."""
    params = payload.get("params")
    if isinstance(params, dict):
        for key in ("cwd", "directory", "dir", "root", "workspace"):
            value = params.get(key)
            if isinstance(value, str) and value:
                return value
    for key in ("cwd", "directory", "workspace"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def reasons_of(payload: dict) -> list[str]:
    reasons = payload.get("reasons") or []
    if not isinstance(reasons, (list, tuple)):
        reasons = [reasons]
    return [str(r).strip() for r in reasons if str(r).strip()]


def summary_of(payload: dict) -> str:
    return str(payload.get("summary") or payload.get("text") or payload.get("action") or "")


class ConfirmationState:
    """The dialog's whole behaviour, without a widget in sight."""

    def __init__(self, payload: dict) -> None:
        self.payload = dict(payload or {})
        self.request_id: str = str(self.payload.get("request_id") or "")
        self.resolved_elsewhere = False
        self.answered = False

    # -- derived views ----------------------------------------------------
    @property
    def level(self) -> str:
        return risk_level(self.payload)

    @property
    def active(self) -> bool:
        """Still waiting on *this* client's decision."""
        return bool(self.request_id) and not self.answered and not self.resolved_elsewhere

    # -- decisions --------------------------------------------------------
    def allow(self) -> dict | None:
        return self._decide(True)

    def cancel(self) -> dict | None:
        return self._decide(False)

    def _decide(self, approved: bool) -> dict | None:
        if not self.active:
            return None
        self.answered = True
        return build_payload(self.request_id, approved)

    def mark_resolved(self) -> None:
        """Someone else (or a timeout) settled it: no answer may be sent."""
        self.resolved_elsewhere = True


def settings_rows(config) -> list[tuple[str, str]]:
    """Read-only settings view content: env-backed configuration only.

    Pure so the view cannot grow business logic — it renders these pairs and
    nothing else.
    """
    from .hotkeys import format_hotkey

    density_name = theme.density()
    return [
        ("TOGGLE HOTKEY", format_hotkey(getattr(config, "toggle_hotkey", ""))),
        ("EXPAND HOTKEY", format_hotkey(getattr(config, "expand_hotkey", ""))),
        ("BACKEND", str(getattr(config, "backend_addr", ""))),
        ("WEBSOCKET", str(getattr(config, "ws_url", ""))),
        ("THEME DENSITY", density_name),
        ("MANAGES BACKEND", "yes" if getattr(config, "manage_backend", True) else "no"),
        ("WORKSPACE", str(getattr(config, "repo_root", ""))),
    ]


class SettingsDialog(ctk.CTkToplevel):
    """Small read-only view of the env-backed configuration."""

    def __init__(self, master, config) -> None:
        super().__init__(master)
        self.title("Zaytoon settings")
        self.configure(fg_color=theme.BG0)
        self.resizable(False, False)

        body = ctk.CTkFrame(
            self, fg_color=theme.BG1, corner_radius=theme.RADIUS_PANEL,
            border_width=1, border_color=theme.HAIRLINE,
        )
        body.pack(fill="both", expand=True, padx=theme.SPACE_4, pady=theme.SPACE_4)

        ctk.CTkLabel(
            body, text=theme.letterspace("SETTINGS"), anchor="w",
            font=(theme.FONT_MONO, theme.FONT_HEADER_SM, "bold"),
            text_color=theme.CYAN,
        ).pack(fill="x", padx=theme.SPACE_4, pady=(theme.SPACE_4, theme.SPACE_2))

        for key, value in settings_rows(config):
            row = ctk.CTkFrame(body, fg_color="transparent")
            row.pack(fill="x", padx=theme.SPACE_4, pady=2)
            ctk.CTkLabel(
                row, text=key, anchor="w", width=150,
                font=(theme.FONT_MONO, theme.FONT_SECTION, "bold"),
                text_color=theme.TEXT_FAINT,
            ).pack(side="left")
            ctk.CTkLabel(
                row, text=value or "—", anchor="w",
                font=(theme.FONT_MONO, theme.FONT_BODY_SM),
                text_color=theme.TEXT_BASE,
            ).pack(side="left", fill="x", expand=True)

        ctk.CTkButton(
            body, text="CLOSE", width=96, height=28,
            corner_radius=theme.RADIUS_CONTROL,
            fg_color=theme.BG2, hover_color=theme.HAIRLINE,
            border_width=1, border_color=theme.HAIRLINE,
            text_color=theme.TEXT_BASE, font=(theme.FONT_MONO, theme.FONT_BODY_SM, "bold"),
            command=self.destroy,
        ).pack(pady=(theme.SPACE_4, theme.SPACE_4))

        self.transient(master)
        self.after(50, self._focus)

    def _focus(self) -> None:
        try:
            self.grab_set()
            self.focus_force()
        except tk.TclError:  # pragma: no cover - master went away
            pass


class ConfirmationDialog(ctk.CTkToplevel):
    """The ``require_confirmation`` modal.

    A separate window rather than a card inside the panel for one reason: the
    panel is withdrawn while the orb is showing, and a security prompt the user
    cannot see is worse than no prompt at all.
    """

    def __init__(self, master, payload: dict, on_result) -> None:
        super().__init__(master)
        self.state = ConfirmationState(payload)
        self._on_result = on_result

        self.title(DIALOG_TITLE)
        self.configure(fg_color=theme.BG0)
        self.minsize(480, 320)

        accent = risk_accent(self.state.level)
        frame = ctk.CTkFrame(
            self, fg_color=theme.BG1, corner_radius=theme.RADIUS_PANEL,
            border_width=1, border_color=accent,
        )
        frame.pack(fill="both", expand=True, padx=theme.SPACE_4, pady=theme.SPACE_4)
        frame.grid_columnconfigure(0, weight=1)

        # --- title + risk badge ------------------------------------------
        top = ctk.CTkFrame(frame, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=theme.SPACE_4,
                 pady=(theme.SPACE_4, theme.SPACE_2))
        ctk.CTkLabel(
            top, text=theme.letterspace(DIALOG_TITLE), anchor="w",
            font=(theme.FONT_MONO, theme.FONT_HEADER_SM, "bold"),
            text_color=accent,
        ).pack(side="left")
        badge = ctk.CTkFrame(
            top, fg_color=theme.tint(accent, 0.12, theme.BG1),
            corner_radius=theme.RADIUS_CONTROL,
            border_width=1, border_color=theme.HAIRLINE,
        )
        badge.pack(side="right")
        ctk.CTkLabel(
            badge, text=f"RISK: {self.state.level.upper()}",
            font=(theme.FONT_MONO, theme.FONT_SECTION, "bold"),
            text_color=accent, padx=theme.SPACE_2,
        ).pack(padx=2, pady=1)

        # --- what is being asked -----------------------------------------
        ctk.CTkLabel(
            frame, text=summary_of(self.state.payload), anchor="w", justify="left",
            font=(theme.FONT_SANS, theme.FONT_BODY),
            text_color=theme.TEXT_BASE, wraplength=520,
        ).grid(row=1, column=0, sticky="ew", padx=theme.SPACE_4)

        # Command block. Tk draws text left-to-right by default and the caret
        # starts at the left edge, so a shell command cannot be visually
        # reordered by an RTL label — the desktop equivalent of the web
        # dialog's ``dir="ltr"``. (``tk.Text`` has no ``justify`` option;
        # paragraph alignment there is done with tags, and left is the
        # default, which is exactly what an approval prompt needs.)
        self.command = tk.Text(
            frame, height=8, wrap="none", relief="flat",
            bg=theme.BG2, fg=theme.TEXT_BASE,
            insertbackground=theme.TEXT_BASE,
            selectbackground=theme.BORDER_STRONG,
            font=(theme.FONT_MONO, theme.FONT_BODY_SM),
            padx=theme.SPACE_3, pady=theme.SPACE_2,
            state="disabled",
        )
        self.command.grid(row=2, column=0, sticky="ew", padx=theme.SPACE_4,
                          pady=(theme.SPACE_2, 0))
        self._set_command(command_text(self.state.payload))

        directory = directory_of(self.state.payload)
        if directory:
            ctk.CTkLabel(
                frame, text=f"DIR  {directory}", anchor="w",
                font=(theme.FONT_MONO, theme.FONT_BODY_SM),
                text_color=theme.TEXT_MUTE,
            ).grid(row=3, column=0, sticky="ew", padx=theme.SPACE_4,
                   pady=(theme.SPACE_1, 0))

        reasons = reasons_of(self.state.payload)
        if reasons:
            ctk.CTkLabel(
                frame, text="\n".join(f"·  {r}" for r in reasons),
                anchor="w", justify="left",
                font=(theme.FONT_MONO, theme.FONT_BODY_SM),
                text_color=theme.AMBER, wraplength=520,
            ).grid(row=4, column=0, sticky="ew", padx=theme.SPACE_4,
                   pady=(theme.SPACE_2, 0))

        # --- status + decisions ------------------------------------------
        self.status_label = ctk.CTkLabel(
            frame, text="", anchor="w",
            font=(theme.FONT_MONO, theme.FONT_SECTION, "bold"),
            text_color=theme.TEXT_FAINT,
        )
        self.status_label.grid(row=5, column=0, sticky="ew", padx=theme.SPACE_4,
                               pady=(theme.SPACE_2, 0))

        buttons = ctk.CTkFrame(frame, fg_color="transparent")
        buttons.grid(row=6, column=0, sticky="ew", padx=theme.SPACE_4,
                     pady=(theme.SPACE_3, theme.SPACE_4))
        self.cancel_btn = ctk.CTkButton(
            buttons, text="CANCEL", width=160, height=44,
            corner_radius=theme.RADIUS_CONTROL,
            fg_color=theme.tint(theme.RED, 0.12, theme.BG1),
            hover_color=theme.tint(theme.RED, 0.28, theme.BG1),
            border_width=1, border_color=theme.RED,
            text_color=theme.RED,
            font=(theme.FONT_MONO, theme.FONT_BODY_LG, "bold"),
            command=lambda: self.decide(False),
        )
        self.cancel_btn.pack(side="left", expand=True, fill="x", padx=(0, theme.SPACE_2))
        self.allow_btn = ctk.CTkButton(
            buttons, text="ALLOW", width=160, height=44,
            corner_radius=theme.RADIUS_CONTROL,
            fg_color=theme.tint(theme.GREEN, 0.12, theme.BG1),
            hover_color=theme.tint(theme.GREEN, 0.28, theme.BG1),
            border_width=1, border_color=theme.GREEN,
            text_color=theme.GREEN,
            font=(theme.FONT_MONO, theme.FONT_BODY_LG, "bold"),
            command=lambda: self.decide(True),
        )
        self.allow_btn.pack(side="left", expand=True, fill="x", padx=(theme.SPACE_2, 0))

        self.protocol("WM_DELETE_WINDOW", lambda: self.decide(False))  # deny on close
        self._present()

    # -- internals --------------------------------------------------------
    def _set_command(self, text: str) -> None:
        self.command.configure(state="normal")
        self.command.delete("1.0", "end")
        self.command.insert("1.0", text)
        self.command.configure(state="disabled")

    def _present(self) -> None:
        """Center over the parent, or float on top when the panel is hidden."""
        try:
            self.update_idletasks()
            parent = self.master
            visible = bool(parent is not None and parent.winfo_viewable())
            if visible:
                self.transient(parent)
                x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
                y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 3
                self.geometry(f"+{max(0, x)}+{max(0, y)}")
            else:
                # Orb mode: the panel is withdrawn, so do not tie the dialog's
                # visibility to it — a prompt nobody can see always times out.
                self.wm_attributes("-topmost", True)
                self.geometry("+120+120")
            self.grab_set()
            self.focus_force()
        except tk.TclError:  # pragma: no cover - parent destroyed
            pass

    def decide(self, approved: bool) -> None:
        """Send exactly one decision, then close."""
        payload = self.state.allow() if approved else self.state.cancel()
        if payload is None:
            # Already answered or resolved elsewhere: nothing more to send.
            self.destroy()
            return
        callback = self._on_result
        self._on_result = None
        try:
            self.destroy()
        finally:
            if callback is not None:
                callback(payload)

    def mark_resolved(self) -> None:
        """The backend already settled this request: disable both buttons."""
        self.state.mark_resolved()
        if not self.winfo_exists():
            return
        self.allow_btn.configure(state="disabled")
        self.cancel_btn.configure(state="disabled")
        self.status_label.configure(
            text="RESOLVED ELSEWHERE — no further answer will be sent",
            text_color=theme.TEXT_MUTE,
        )
        self.after(400, self._safe_destroy)

    def _safe_destroy(self) -> None:
        try:
            self.destroy()
        except tk.TclError:  # pragma: no cover
            pass
