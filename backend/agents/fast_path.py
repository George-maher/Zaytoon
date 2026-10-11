"""Answer the handful of requests whose entire meaning is one safe tool call.

A local 3B model asked "what time is it" must: read the prompt, decide to call a
tool, emit JSON, have that JSON parsed, run the tool, then be asked to narrate the
result — two generations for a `datetime.now()`. On constrained hardware that is
seconds of work, and the failure modes are unkind: a small model sometimes answers
from memory and invents a time, which is the one outcome worse than being slow.

So this module matches a *closed set* of phrasings to *exactly one* tool. What it
is not:

* **Not an intent platform.** No fuzzy scoring, no embeddings, no "closest tool".
  A miss falls through to the model, which is what happens today.
* **Not a permission shortcut.** The rule set is limited to tools the registry
  already classifies ``safe``, and the engine re-checks that at the call site.
  A confirmation-required tool cannot be reached from here even if someone adds a
  pattern for it — and the call still goes through the same approval gate, audit
  record and metrics as a model-initiated one.
* **Not a partial match.** Every pattern is anchored at both ends, after
  punctuation and case are normalised, so "what time is it **and** close my app"
  is not answered by the first clause. Half-understanding a request is exactly
  what the model is for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

#: The only tools this module may ever name. Kept explicit rather than derived
#: from the registry: "it is tier=safe today" is a fact about tomorrow's file,
#: and a fast path to a mutating tool should be a deliberate edit, not the
#: consequence of someone re-tiering something.
ALLOWED_TOOLS = frozenset({
    "get_time",
    "get_volume",
    "system_info",
    "screenshot",
    "process_list",
    "open_app",
})

#: Words that mean the message carries a second instruction. Checked against the
#: whole utterance rather than the captured slot, because "open spotify and x"
#: fails the anchor anyway while "open spotify, then mute" could otherwise be
#: read as one request by a looser pattern.
_CONJUNCTIONS = re.compile(
    r"""(?:\b(?:and|then|plus|afterwards|also)\b|،|\sو\s|ثم|وبعد)""",
    re.IGNORECASE,
)

_TRAILING_PUNCT = " \t\r\n.!?؟،,؛:"


@dataclass(frozen=True)
class Intent:
    """One phrasing that maps to exactly one tool call."""

    tool: str
    patterns: Tuple[str, ...]
    #: Static arguments. A captured group named after a parameter overrides it.
    params: Dict[str, Any]
    #: Bilingual framing for the answer, as a template over the tool's own
    #: output. Templated rather than "label + colon + value" because the
    #: connector differs per sentence in both languages — `It's 18:04` reads
    #: fine and `It's: 18:04` reads like a log line.
    label: Dict[str, str]

    def compiled(self) -> List[re.Pattern]:
        return [re.compile(p, re.IGNORECASE) for p in self.patterns]


def _capture(name: str) -> str:
    return f"(?P<{name}>[^.,؛;]+?)"


INTENTS: Tuple[Intent, ...] = (
    Intent(
        tool="get_time",
        patterns=(
            r"(?:what(?:'s| is)|tell me|remind me)? ?(?:me )?the time(?: please)?",
            r"what time is it(?: now)?(?: please)?",
            r"time(?: please)?",
            # Transliterated Egyptian, several spellings of the same phrase. The
            # prefix group is `(?: )?`-style repetition rather than one optional
            # word because "hi el saa kam" carries both.
            r"(?:(?:hi|el|il|de|the) +)*(?:saa'aht|saa'a|saa3a|sa3a|saa|sa'a) +kam"
            r"(?: +(?:دلوقتي|en|الآن))?",
            r"(?:كم|ايش وش شنو) الساعة(?: (?:الآن|دلوقتي|انهاردة))?",
            r"(?:الساعة|الوقت) كام(?: (?:الآن|دلوقتي))?",
            r"(?:متى|ما) الوقت(?: الآن)?",
        ),
        params={},
        label={"en": "It's {value}", "ar": "الساعة دلوقتي {value}"},
    ),
    Intent(
        tool="get_volume",
        patterns=(
            r"(?:what(?:'s| is) )?(?:my |the )?volume(?: level)?(?: now)?",
            r"(?:check|tell me) (?:the )?volume",
            r"(?:مستوى|إيه|ما) الصوت(?: الحالي| دلوقتي)?",
            r"(?:الصوت|مستوى الصوت) كام(?: دلوقتي)?",
        ),
        params={},
        label={"en": "Volume: {value}", "ar": "مستوى الصوت: {value}"},
    ),
    Intent(
        tool="system_info",
        patterns=(
            r"(?:system|machine) info(?:rmation)?",
            r"(?:what|show) (?:are|is) my (?:specs|spec|system info(?:rmation)?)",
            r"(?:how much )?(?:ram|memory)(?: (?:do i have|usage|used))?(?:\?)?",
            r"(?:cpu|memory) (?:usage|info)",
            r"مواصفات(?: الجهاز| جهازك)?",
            r"معلومات(?: النظام| الجهاز)",
            r"(?:الرام|الذاكرة)(?: بتاعتك)? كام",
        ),
        params={},
        label={"en": "System: {value}", "ar": "معلومات الجهاز: {value}"},
    ),
    Intent(
        tool="screenshot",
        patterns=(
            r"(?:take|grab|capture|get|make) (?:a|the) (?:screen ?shot|screenshot)",
            r"(?:screen ?shot|screenshot)(?: please)?",
            r"(?:التقط|خد|اعمل|صور) (?:ليا |لي )?(?:صورة |سكرين ?شوت)(?:ل|لل)?(?:الشاشة)?",
            r"(?:صورة|سكرينشوت) (?:لل)?شاشة?",
        ),
        params={},
        label={"en": "Saved {value}", "ar": "اتصور: {value}"},
    ),
    Intent(
        tool="process_list",
        patterns=(
            r"(?:list|show)(?: me)? (?:my |the )?(?:tracked )?(?:background )?processes",
            r"(?:what|which) (?:processes|apps) (?:are|do i) running(?: now)?",
            r"(?:my )?running processes",
            r"اعرض(?: لي)? العمليات(?: الخلفية)?",
            r"(?:العمليات|المهام) (?:الخلفية|الشغالة)(?: إيه)?",
        ),
        params={},
        label={"en": "Tracked processes: {value}", "ar": "العمليات: {value}"},
    ),
    Intent(
        # `open_app` takes the name; the whole rest of the sentence is the value,
        # which is why the conjunction guard above is not optional.
        tool="open_app",
        patterns=(
            rf"(?:open|launch|start)(?: up| the)? {_capture('app')}",
            rf"(?:please )?open (?:the )?{_capture('app')} (?:app|application)",
            rf"(?:افتح|شغل|قم بتشغيل|تشغيل)(?: ليا| لي)? {_capture('app')}",
        ),
        params={},
        label={"en": "Opening {value}", "ar": "أفتحت {value}"},
    ),
)


def normalise(text: str) -> str:
    """Lower-case, squeeze spaces, drop trailing punctuation.

    Nothing else. Deliberately no stemming, no stop-word removal, no unicode
    folding of Arabic letters: each of those would widen what a pattern matches,
    and the cost of matching too broadly is answering a different question than
    the one that was asked.
    """
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    return cleaned.strip(_TRAILING_PUNCT).lower()


def match(text: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """``(tool, params)`` when the whole message *is* one of the known requests.

    ``None`` — the normal answer for anything else — means "ask the model".
    """
    value = normalise(text)
    if not value or len(value) > 120:
        # A message that long is not "what time is it", and the length ceiling
        # is also what keeps an anchored regex from being handed a paragraph to
        # backtrack over.
        return None
    if _CONJUNCTIONS.search(value):
        return None

    for intent in INTENTS:
        if intent.tool not in ALLOWED_TOOLS:  # pragma: no cover - guarded at source
            continue
        for pattern in intent.compiled():
            found = pattern.fullmatch(value)
            if not found:
                continue
            params: Dict[str, Any] = dict(intent.params)
            for key, captured in (found.groupdict() or {}).items():
                if captured and captured.strip():
                    params[key] = captured.strip(_TRAILING_PUNCT).strip()
            if intent.tool == "open_app":
                app = str(params.get("app", "")).strip()
                # An empty or absurd "app" is a pattern artefact, not a request;
                # `open_app` would then resolve nothing and the answer would be a
                # tool error the model could have avoided.
                if not app or len(app) > 60 or " " in app and len(app.split()) > 3:
                    return None
                params["app"] = app
            return intent.tool, params
    return None


def render(intent: Optional[Intent], result: Dict[str, Any], language: str) -> str:
    """The tool's own output, framed in the user's language.

    Framed rather than raw, because a bare ``2026-10-10 18:02:52 (Saturday)``
    reads like a log line in a chat, and the whole point of this path is that the
    answer was *obtained* deterministically — not that it should sound like one.
    """
    body = str((result or {}).get("output") or "").strip()
    if not body:
        return ""
    if intent is None:
        return body
    template = intent.label.get(language) or intent.label["en"]
    if "{value}" in template:
        return template.format(value=body)
    return template or body


def intent_for(tool: str) -> Optional[Intent]:
    """The :class:`Intent` whose tool is ``tool`` — for the caller that renders."""
    return next((i for i in INTENTS if i.tool == tool), None)


__all__ = ["ALLOWED_TOOLS", "Intent", "INTENTS", "match", "normalise",
           "render", "intent_for"]
