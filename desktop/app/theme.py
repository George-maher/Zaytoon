"""Shared visual language for the desktop shell.

Values mirror the web frontend's CSS variables so the orb looks the same
whether it is running in a browser or as a desktop window.
"""

from __future__ import annotations

# Core palette
BG = "#0a0e14"
BG_PANEL = "#111823"
BG_ELEVATED = "#1a2330"
BORDER = "#243040"

ACCENT = "#00d4ff"
ACCENT_DIM = "#0a7d99"
SUCCESS = "#00ff88"
WARNING = "#f0b000"
DANGER = "#ff4d6d"

TEXT = "#e6edf3"
TEXT_MUTED = "#8b98a5"

# Orb glow per state: (inner, outer)
ORB_STATES: dict[str, tuple[str, str]] = {
    "disconnected": ("#2a3442", "#16202c"),
    "connected": ("#0e5f73", "#0a2d38"),
    "listening": ("#0d7a5a", "#053528"),
    "awaiting_command": ("#7a5c0d", "#3a2c05"),
    "thinking": ("#0d5f8a", "#052a3d"),
    "speaking": ("#0d8a7a", "#04382f"),
    "awaiting_confirmation": ("#8a3a0d", "#3d1a05"),
    "error": ("#8a0d2a", "#3d0512"),
}

#: Statuses that should read as "the assistant is doing something", used to
#: decide whether the orb animates.
ACTIVE_STATES = frozenset({"listening", "thinking", "speaking", "awaiting_command"})


def orb_colors(status: str) -> tuple[str, str]:
    """Glow colours for a status, falling back to ``disconnected``."""
    return ORB_STATES.get(status, ORB_STATES["disconnected"])


def status_text(status: str, language: str = "en") -> str:
    """Human-readable status label, bilingual like the rest of the app."""
    arabic = language == "ar"
    table = {
        "disconnected": ("Disconnected", "غير متصل"),
        "connected": ("Ready", "جاهز"),
        "listening": ("Listening...", "بستمع..."),
        "awaiting_command": ("Say your command...", "قول الأمر..."),
        "thinking": ("Thinking...", "بفكر..."),
        "speaking": ("Speaking", "بتكلم"),
        "awaiting_confirmation": ("Waiting for your approval", "في انتظار موافقتك"),
        "done": ("Done", "خلاص"),
        "error": ("Something went wrong", "حصل خطأ"),
    }
    label = table.get(status, ("Idle", "خامل"))
    return label[1] if arabic else label[0]
