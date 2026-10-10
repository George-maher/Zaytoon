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

The module has **two deliberate layers**:

1. the web-shared HUD palette (``VOID``/``DEEP``/``RAISED``/...), pinned
   token-for-token against ``frontend/src/styles/theme.css`` by
   ``tests/test_design_parity.py``;
2. the command-center palette (``BG0``/``BG1``/``BG2``/``HAIRLINE``/
   ``TEXT_BASE``/...), which is what the redesigned shell actually draws with.

Layer 2 is the single source for every widget in ``app/``: colours, radii,
spacing, type and the status-pill recipe all come from it, so re-theming the
shell is an edit to this file and not a hunt through the widgets.
"""

from __future__ import annotations

import os

# --- surfaces: flat, never blurred -----------------------------------------
# Measured from the reference image: its ground is #000000 across ~64% of
# pixels, lifted only enough that a panel reads as a surface rather than a
# hole. These names are pinned token-for-token against
# frontend/src/styles/theme.css by tests/test_design_parity.py.
VOID = "#05070A"
DEEP = "#080B11"
RAISED = "#0D1219"
LINE = "#121924"

# --- accents ---------------------------------------------------------------
# The accent is the reference's own cyan: 96% of its saturated pixels are in
# the blue/cyan family and its brightest measured accent is #e879f9. The name
# stays ``CYAN`` because test_design_parity pins it against the stylesheet;
# renaming it would move the drift, not remove it.
CYAN = "#D946EF"
CYAN_CORE = "#67E8F9"
BLUE = "#8B2A9E"
INDIGO = "#2563EB"
# Amber is the reference's only warm element (its two orange bars) and is kept
# deliberately scarce so it still reads as an alert rather than as decoration.
AMBER = "#F2B84B"
GREEN = "#35D399"
RED = "#FF6B6B"

ACCENT = CYAN
ACCENT_DIM = "#0E7490"
SUCCESS = GREEN
WARNING = AMBER
DANGER = RED

# Compatibility aliases for the names the rest of the shell already uses.
BG = VOID
BG_PANEL = DEEP
BG_ELEVATED = RAISED
BORDER = "#182230"
TEXT = "#E8F6FF"
TEXT_MUTED = "#6D8296"
TEXT_DIM = "#A3B8CC"

#: Corner radius for instrument chrome. Square is the point: a rounded panel
#: reads as a card, and the design language is an instrument bay, not a
#: dashboard. Only the mic and orb affordances use a fully round shape.
#:
#: .. note::
#:    ``RADIUS`` is the **legacy** square token and is pinned by
#:    ``tests/test_design_parity.py`` together with the palette above. The
#:    command-center redesign draws with the radius *scale* declared below
#:    (``RADIUS_PANEL`` / ``RADIUS_CARD`` / ``RADIUS_CONTROL``), which is the
#:    value set the redesign specifies. Do not delete either one without
#:    updating the parity test and ``frontend/src/styles/theme.css`` together.
RADIUS = 0
RADIUS_SM = 2

#: Hex colour plus the 1px edge tints used for frames, borders and focus.
EDGE = "#182230"
EDGE_STRONG = "#24344A"
EDGE_FAINT = "#121924"
GLOW = "#164E5C"

# --- density: the compact HUD ----------------------------------------------
# Every spacing / font / thickness decision in the chat panel lives here, so
# the whole shell can be re-densified by editing this block alone. Values are
# Tk logical pixels; customtkinter scales them by the detected DPI factor.
PAD_X = 10                     # horizontal padding of the panel content column
PAD_Y = 6                      # vertical padding around the content column

ROW_PAD_Y = 2                  # vertical padding under a transcript row
ROW_PAD_TOP = 4                # spacing above a transcript row
ROW_INDENT = 36                # transcript side margin (0 on the speaker side)
BUBBLE_PAD_X = 10              # transcript bubble inner horizontal padding
BUBBLE_PAD_Y = 3               # transcript bubble inner vertical padding

CARD_PAD = 10                  # confirmation card inner padding
CARD_PAD_Y = 6

STATUS_HEIGHT = 20             # status strip height
STATUS_PAD_Y = 5               # status strip vertical padding
LINE_PAD_Y = 4                 # padding around the header / footer hairlines

BADGE_SIZE = 24                # header identity badge box
BTN_W, BTN_H = 28, 22          # header collapse / quit buttons
COMPOSER_PAD = 4               # composer inner vertical padding
COMPOSER_PAD_BOTTOM = 8        # gap under the composer
ENTRY_H = 32                   # composer entry + control height
SEND_W = 64                    # SEND button width
MIC_W = 34                     # mic / stop button width

FONT_LABEL_SM = 11             # header label / status strip
FONT_TITLE = 12                # header wordmark
FONT_MSG = 12                  # transcript body
FONT_ENTRY = 12                # composer entry + SEND label
FONT_HINT = 11                 # composer placeholder / disabled hint
FONT_CARD_TITLE = 11           # confirmation card title
FONT_CARD_BODY = 11            # confirmation card summary / risk / buttons

SPINE_W = 2                    # message spine column width
SPINE_GAP = 8                  # gap between the spine and the message

WRAP_BASE = 560                # transcript wraplength at the default width
WRAP_MARGIN = 36               # wraplength tracks the window width minus this
WRAP_MIN = 180                 # never wrap narrower than this


# ===========================================================================
# Command-center palette — THE rendering source for the redesigned shell
# ===========================================================================
#
# Two layers, on purpose, and the difference matters:
#
# * Layer 1 (above) is the **web-shared HUD palette**. Those names are compared
#   token-for-token against ``frontend/src/styles/theme.css`` by
#   ``tests/test_design_parity.py``, so they cannot move until the CSS moves.
# * Layer 2 (here) is the **AI command-center palette**: the surfaces, text,
#   accents, radii, spacing and type the redesigned shell draws with. These are
#   the values the redesign specifies, and every widget in the shell reads them
#   from here rather than hard-coding a colour.
#
# The accents already agree across both layers (cyan/green/amber/red are the
# same hex values); only the surfaces and text tones differ, which is why the
# two layers can coexist without the shell looking like two themes stitched
# together.

# --- surfaces ---------------------------------------------------------------
# Same reference-derived scale as the HUD palette above, so the shell and the
# browser are one product rather than two dark themes.
BG0 = "#05070a"                # app background
BG1 = "#080b11"                # panels
BG2 = "#0d1219"                # raised cards

# --- edges ------------------------------------------------------------------
HAIRLINE = "#182230"           # 1px separators, card borders
BORDER_STRONG = "#24344a"      # focused / emphasized frames

# --- text -------------------------------------------------------------------
TEXT_BASE = "#e8f6ff"          # body copy
TEXT_MUTE = "#6d8296"          # secondary labels
TEXT_FAINT = "#55697d"         # timestamps, disabled, placeholders

# --- accents ----------------------------------------------------------------
#: The assistant hue. In the reference the "hotter" tone is the near
#: white-cyan rim on its circular dial rather than a second hue, so the AI
#: state reads as *brighter* — which also survives greyscale.
VIOLET = "#e879f9"

#: Every accent the shell may colour something with, in one place.
ACCENTS: dict[str, str] = {
    "cyan": CYAN,
    "green": GREEN,
    "amber": AMBER,
    "red": RED,
    "violet": VIOLET,
}

#: Status pill recipe: hairline border + a 12% tinted accent background +
#: accent text. ``pill_colors`` is the only place that recipe lives.
PILL_TINT = 0.12


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")
    if len(value) != 6:
        raise ValueError(f"not a #rrggbb colour: {color!r}")
    try:
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ValueError(f"not a #rrggbb colour: {color!r}") from exc


def _rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, round(v))) for v in rgb))


def mix(color_a: str, color_b: str, t: float) -> str:
    """Blend two colours: ``t=0`` is ``color_a``, ``t=1`` is ``color_b``.

    Used for the cheap ~160-220ms state transitions (Tk has no tweening, so a
    few ticks of interpolation stand in for it) and for tinting.
    """
    a, b = _hex_to_rgb(color_a), _hex_to_rgb(color_b)
    t = max(0.0, min(1.0, float(t)))
    return _rgb_to_hex(tuple(a[i] + (b[i] - a[i]) * t for i in range(3)))


def tint(color: str, alpha: float = PILL_TINT, over: str = BG0) -> str:
    """``color`` composited over ``over`` at ``alpha`` (0..1).

    Tk has no alpha channel, so translucent fills are pre-composited here.
    """
    return mix(over, color, alpha)


def pill_colors(accent: str) -> tuple[str, str, str]:
    """The status-pill recipe: ``(background, border, text)``."""
    return tint(accent, PILL_TINT, BG1), HAIRLINE, accent


# --- radius scale -----------------------------------------------------------
RADIUS_PANEL = 8               # window-level panels
RADIUS_CARD = 6                # raised cards (confirmation, side cards)
RADIUS_CONTROL = 2             # buttons, pills, inputs — sharp, not pill

# --- spacing scale ----------------------------------------------------------
SPACE_1, SPACE_2, SPACE_3 = 4, 8, 12
SPACE_4, SPACE_5, SPACE_6 = 16, 24, 32
SPACING = (SPACE_1, SPACE_2, SPACE_3, SPACE_4, SPACE_5, SPACE_6)

# --- type -------------------------------------------------------------------
FONT_MONO = "Consolas"         # technical text, code, timestamps, telemetry
FONT_SANS = "Segoe UI"         # body copy

FONT_BODY_SM = 11              # 11-13 body
FONT_BODY = 12
FONT_BODY_LG = 13

FONT_HEADER_SM = 18            # 18-22 headers
FONT_HEADER = 20
FONT_HEADER_LG = 22

FONT_SECTION = 10              # letter-spaced uppercase section labels
FONT_SECTION_LG = 11

#: Letter-spacing for uppercase section labels. Tk has no tracking attribute,
#: so the label text is spaced manually with ``letterspace()``.
LETTER_SPACING = 1


def letterspace(text: str, amount: int = 1) -> str:
    """Space out an uppercase label: ``SECTION`` -> ``S E C T I O N``."""
    if amount <= 0:
        return text
    return (" " * amount).join(str(text))


# --- density ----------------------------------------------------------------
#: ``ZAYTOON_THEME_DENSITY=compact|normal`` — the single knob for how tight the
#: shell reads. Read at build time by the shell and shown in the settings view.
DENSITY_ENV = "ZAYTOON_THEME_DENSITY"
DENSITIES = ("compact", "normal")


def density() -> str:
    """The configured density, normalised to a member of :data:`DENSITIES`."""
    raw = (os.getenv(DENSITY_ENV) or "").strip().lower()
    return raw if raw in DENSITIES else "normal"


def density_scale(density_name: str | None = None) -> float:
    """Padding multiplier for the configured density."""
    return 0.75 if (density_name or density()) == "compact" else 1.0


# --- motion -----------------------------------------------------------------
#: Transitions read as ~160-220ms. Tk has no easing, so state changes
#: interpolate a colour over a handful of ticks at these frame budgets.
T_FAST_MS = 160
T_SLOW_MS = 220
FRAME_MS = 30                 # ~33fps ceiling for the core/orb
ANIM_FPS_MAX = 30


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
