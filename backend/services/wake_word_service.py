"""Wake-word gate — discard transcripts not addressed to Zaytoon.

The wake word is an *activation gate*, not an authorisation: it decides
whether an utterance becomes a command at all. Everything that can actually
change the machine is still behind the tool registry's permission tiers and the
``ConfirmationManager``, and this module never sees either.

Matching rules that matter:

* The wake word must be the first meaningful word (optionally preceded by a
  greeting — "hey Zaytoon", "يا زيتون"). Speech that merely mentions Zaytoon
  later in a sentence is not an activation, which is what stops "I asked
  Zaytoon yesterday…" from executing its remainder.
* Both spellings are recognised, with the transliterations Whisper actually
  emits ("zaytoun", "zayton", "zay toon", "ziton") and Arabic diacritics /
  tatweel tolerated.
* A *partial* match is not a match. The boundary after the token rejects
  "zaytoonish" and, importantly, "زيتونة" (an olive) — the common Arabic word
  that would otherwise fire the gate on grocery lists.
"""

import re
import time

#: Optional vocative / greeting before the name. Deliberately short: an
#: arbitrary prefix would turn "could you ask Zaytoon…" into an activation.
_PREFIX = r"(?:(?:hey|hi|ok|okay|yo)\s*[,،:]?\s*|يا\s*)?"

#: English transliterations. `zay[ -]?toon` covers the split form some STT
#: engines produce; the rest are observed variants, not invented spellings.
_WAKE_EN = (
    r"zay[\s\-']?(?:toon|toun|toone|tune|ton|tan)"
    r"|zaitoon|zayton|zaytan|ziton|zaytoun"
)

#: Arabic, with optional harakat/tatweel *between* letters so "زيتـون" and
#: "زَيْتُون" both match while the surrounding boundary still applies.
_WAKE_AR = r"ز[\u064B-\u065F\u0670\u0640]*ي[\u064B-\u065F\u0670\u0640]*ت[\u064B-\u065F\u0670\u0640]*و[\u064B-\u065F\u0670\u0640]*و?[\u064B-\u065F\u0670\u0640]*ن"

#: The boundary rejects a longer word on either side. Arabic letters are word
#: characters to Python's `\b`, so "زيتونة" fails here as intended. The class is
#: deliberately *letters only*: an earlier range (\u0600-\u06FF) also swallowed
#: Arabic punctuation, so the ordinary sentence "يا زيتون، إيه الوقت" failed the
#: gate because the comma after the name read as part of the word.
_BOUNDARY = r"(?![a-z\u0621-\u064A\u0660-\u0669])"

WAKE_PATTERN = re.compile(
    rf"^{_PREFIX}(?:{_WAKE_EN}|{_WAKE_AR}){_BOUNDARY}",
    re.IGNORECASE,
)

WAKE_ONLY = re.compile(
    rf"^{_PREFIX}(?:{_WAKE_EN}|{_WAKE_AR}){_BOUNDARY}\s*[,،:.\-!?]*\s*$",
    re.IGNORECASE,
)

_STRIP_TRAILING = " ,،:-."


def strip_wake_word(transcript: str) -> str | None:
    """Return command after wake word, '' if wake-only, None if not addressed."""
    cleaned = (transcript or "").strip().strip(_STRIP_TRAILING)
    if not cleaned:
        return None
    if WAKE_ONLY.match(cleaned):
        return ""
    match = WAKE_PATTERN.match(cleaned)
    if not match:
        return None
    remainder = cleaned[match.end():].strip(_STRIP_TRAILING)
    return remainder


def requires_wake_word(awaiting_command: bool, last_active_ts: float, timeout_secs: float) -> bool:
    """True when the next utterance must include the wake word.

    ``awaiting_command`` is the explicit arming flag (wake word spoken alone).
    ``last_active_ts`` is the timestamp of that activation, retained as a
    time bound on how long the follow-up window stays open. It is deliberately
    **not** "when any turn last finished": using that made a typed answer arm
    the microphone for ten seconds.
    """
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
