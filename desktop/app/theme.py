"""Shared visual language for the desktop shell.

This is the customTkinter rendering of ``docs/DESIGN_SYSTEM.md``. The values
mirror ``frontend/src/styles/theme.css`` so the desktop orb and the browser
are recognisably the same product rather than two apps that happen to be
dark.

The previous palette here was a warmer, greyer dark theme with pill-shaped
chrome; it drifted from the web and read as a consumer chat window. The
differences that matter for parity:

* surfaces are flat and blue-black, not neutral grey;
* the primary accent is the reference's teal-leaning cyan;
* chrome is square (corner radius 0-2), not rounded pills;
* the state vocabulary matches ``hudState.js`` exactly, including the glyphs,
  so the two clients name the same state the same way.
"""

from __future__ import annotations

# --- surfaces: flat, never blurred -----------------------------------------
VOID = "#03070D"
DEEP = "#06111A"
RAISED = "#081722"
LINE = "#0C1E2C"

# --- accents ---------------------------------------------------------------
CYAN = "#00E5FF"
CYAN_CORE = "#00CFFF"
BLUE = "#168DFF"
INDIGO = "#2878FF"
AMBER = "#FFB020"
GREEN = "#00FF9C"
RED = "#FF3B5C"

ACCENT = CYAN
ACCENT_DIM = "#0A7D99"
SUCCESS = GREEN
WARNING = AMBER
DANGER = RED

# Compatibility aliases for the names the rest of the shell already uses.
BG = VOID
BG_PANEL = DEEP
BG_ELEVATED = RAISED
BORDER = "#12283A"
TEXT = "#E8F7FF"
TEXT_MUTED = "#63879A"
TEXT_DIM = "#A8C7D8"

#: Corner radius for instrument chrome. Square is the point: a rounded panel
#: reads as a card, and the design language is an instrument bay, not a
#: dashboard. Only the mic and orb affordances use a fully round shape.
RADIUS = 0
RADIUS_SM = 2

#: Hex colour plus the 1px edge tints used for frames, borders and focus.
EDGE = "#123246"
EDGE_STRONG = "#1D5C7D"
EDGE_FAINT = "#0C1E2C"
GLOW = "#0E4A63"


# --- state vocabulary ------------------------------------------------------
# Mirrors STATES in frontend/src/components/hud/hudState.js:
#   name -> (hue token, glyph, ring seconds, pulse?)
# `rate` is the seconds per revolution the core/orb sweep takes, which is how
# "busy" is communicated without relying on colour.
STATES: dict[str, tuple[str, str, float, bool]] = {
    "offline":        (RED,    "○", 0.0,  True),
    "idle":           (CYAN,   "●", 12.0, False),
    "connected":      (CYAN,   "●", 12.0, False),
    "listening":      (GREEN,  "◉", 4.0,  True),
    "transcribing":   (CYAN,   "◌", 2.0,  True),
    "thinking":       (CYAN,   "◐", 1.5,  True),
    "planning":       (BLUE,   "◇", 2.0,  True),
    "executing":      (AMBER,  "▲", 2.0,  True),
    "speaking":       (CYAN,   "▶", 3.0,  True),
    "awaiting_command": (AMBER, "?", 6.0, False),
    # `awaiting` is the web's name for the same state (hudState.js). Both are
    # accepted so the two clients cannot disagree about it.
    "awaiting": (AMBER, "?", 6.0, False),
    "awaiting_confirmation": (AMBER, "!", 2.0, True),
    "confirming":     (AMBER,  "!", 2.0,  True),
    "error":          (RED,    "✕", 1.0,  True),
    "done":           (GREEN,  "✓", 12.0, False),
}

#: Statuses that should read as "the assistant is doing something", used to
#: decide whether the orb animates.
ACTIVE_STATES = frozenset(
    {"listening", "thinking", "speaking", "awaiting_command", "executing", "planning", "transcribing"}
)

#: Orb glow per state: (inner, outer). Kept separate from the flat palette
#: because the orb is the one element allowed a bloom.
ORB_STATES: dict[str, tuple[str, str]] = {
    "disconnected": ("#2A3442", "#16202C"),
    "connected": ("#0E5F73", "#0A2D38"),
    "idle": ("#0E5F73", "#0A2D38"),
    "listening": ("#0D7A5A", "#053528"),
    "transcribing": ("#0E5F73", "#0A2D38"),
    "thinking": ("#0D5F8A", "#052A3D"),
    "planning": ("#12407A", "#08203F"),
    "executing": ("#7A5C0D", "#3A2C05"),
    "speaking": ("#0D8A7A", "#04382F"),
    "awaiting_command": ("#7A5C0D", "#3A2C05"),
    "awaiting_confirmation": ("#8A3A0D", "#3D1A05"),
    "confirming": ("#8A3A0D", "#3D1A05"),
    "error": ("#8A0D2A", "#3D0512"),
    "done": ("#0D7A5A", "#053528"),
}

#: Bilingual labels. The words match hudState.js so the two clients agree.
_LABELS: dict[str, tuple[str, str]] = {
    "offline": ("OFFLINE", "غير متصل"),
    "connected": ("READY", "جاهز"),
    "idle": ("READY", "جاهز"),
    "listening": ("LISTENING", "بستمع"),
    "transcribing": ("TRANSCRIBING", "بفهم"),
    "thinking": ("PROCESSING", "بفكر"),
    "planning": ("PLANNING", "يخطط"),
    "executing": ("RUNNING TOOL", "بينفّذ"),
    "speaking": ("SPEAKING", "بتكلم"),
    "awaiting_command": ("AWAIT COMMAND", "قول الأمر"),
    "awaiting": ("AWAIT COMMAND", "قول الأمر"),
    "awaiting_confirmation": ("AWAIT CONFIRM", "في انتظار موافقتك"),
    "confirming": ("AWAIT CONFIRM", "في انتظار موافقتك"),
    "error": ("ERROR", "خطأ"),
    "done": ("READY", "جاهز"),
}


def resolve(status: str) -> str:
    """Map a transport status onto the shared HUD state vocabulary.

    The WebSocket protocol uses a couple of names the HUD does not
    (``connected`` vs ``idle``, ``awaiting_confirmation`` vs ``confirming``).
    Normalising here means the orb, the status bar and the web core cannot
    drift apart on naming.
    """
    return status if status in STATES else "idle"


def state_hue(status: str) -> str:
    return STATES[resolve(status)][0]


def state_glyph(status: str) -> str:
    return STATES[resolve(status)][1]


def state_rate(status: str) -> float:
    """Seconds per sweep revolution. 0 means the sweep is stopped."""
    return STATES[resolve(status)][2]


def state_pulses(status: str) -> bool:
    return STATES[resolve(status)][3]


def orb_colors(status: str) -> tuple[str, str]:
    """Glow colours for a status, falling back to ``disconnected``."""
    return ORB_STATES.get(status, ORB_STATES["disconnected"])


def status_text(status: str, language: str = "en") -> str:
    """Hud-style state word, bilingual like the rest of the app."""
    arabic = language == "ar"
    label = _LABELS.get(resolve(status), _LABELS["idle"])
    return label[1] if arabic else label[0]
