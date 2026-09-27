"""Wake-word gate — discard transcripts not addressed to Zaytoon."""

import re
import time

WAKE_PATTERNS = [
    re.compile(r"^(يا\s*)?زيتون\b", re.IGNORECASE),
    re.compile(r"^\s*zaytoon\b", re.IGNORECASE),
    re.compile(r"^\s*zaitoon\b", re.IGNORECASE),
    re.compile(r"^\s*zayton\b", re.IGNORECASE),
    re.compile(r"^\s*zaitan\b", re.IGNORECASE),
    re.compile(r"^\s*zaytan\b", re.IGNORECASE),
    re.compile(r"^\s*ziton\b", re.IGNORECASE),
]

WAKE_ONLY = re.compile(
    r"^(يا\s*)?(زيتون|zaytoon|zaitoon|zayton|zaitan|zaytan|ziton)\s*$",
    re.IGNORECASE,
)

_STRIP_TRAILING = " ,،:-."


def strip_wake_word(transcript: str) -> str | None:
    """Return command after wake word, '' if wake-only, None if not addressed."""
    cleaned = transcript.strip().strip(_STRIP_TRAILING)
    if not cleaned:
        return None
    if WAKE_ONLY.match(cleaned):
        return ""
    for pattern in WAKE_PATTERNS:
        match = pattern.match(cleaned)
        if match:
            remainder = cleaned[match.end():].strip(_STRIP_TRAILING)
            return remainder
    return None


def requires_wake_word(awaiting_command: bool, last_active_ts: float, timeout_secs: float) -> bool:
    """True when the next utterance must include the wake word."""
    if awaiting_command:
        return False
    if last_active_ts and (time.time() - last_active_ts) < timeout_secs:
        return False
    return True


class WakeResult:
    """Outcome of running the gate over one transcript.

    acted    - the transcript should be handled as a normal message
    text     - the command with the wake word stripped (only when acted)
    detected - True when this utterance itself carried the wake word
    dropped  - the transcript was not addressed to Zaytoon and was discarded
    """

    __slots__ = ("acted", "text", "detected", "dropped")

    def __init__(self, acted: bool = False, text: str = "", detected: bool = False, dropped: bool = False):
        self.acted = acted
        self.text = text
        self.detected = detected
        self.dropped = dropped

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"WakeResult(acted={self.acted}, detected={self.detected}, "
            f"dropped={self.dropped}, text={self.text!r})"
        )


def apply_wake_gate(
    text: str,
    *,
    awaiting_command: bool,
    last_active_ts: float,
    timeout_secs: float,
    enabled: bool = True,
) -> WakeResult:
    """Run the wake-word gate over `text` and say what should happen.

    `enabled=False` disables the gate entirely, so every transcript is handled.
    Keeping the decision here (rather than inline in the WebSocket handler)
    means the three outcomes — handled, wake-word-only, discarded — are
    testable without a socket.
    """
    if not enabled:
        return WakeResult(acted=True, text=text)
    if not requires_wake_word(awaiting_command, last_active_ts, timeout_secs):
        return WakeResult(acted=True, text=text)
    command = strip_wake_word(text)
    if command is None:
        return WakeResult(dropped=True)
    if command == "":
        return WakeResult(acted=False, detected=True)
    return WakeResult(acted=True, text=command, detected=True)
