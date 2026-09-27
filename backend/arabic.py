import re

ARABIC_PATTERN = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")

ARABIC_NORMALIZE_MAP = {
    "\u0622": "\u0627", "\u0623": "\u0627", "\u0625": "\u0627",
    "\u0649": "\u064a",
    "\u0629": "\u0647",
    "\u0624": "\u0648",
    "\u0640": "",
    "\u06a9": "\u0643",
    "\u06cc": "\u064a",
}

ARABIC_TO_INDIC = {
    "\u0660": "0", "\u0661": "1", "\u0662": "2", "\u0663": "3",
    "\u0664": "4", "\u0665": "5", "\u0666": "6", "\u0667": "7",
    "\u0668": "8", "\u0669": "9",
}

EGYPTIAN_MARKERS = [
    "\u0625\u064A\u0647", "\u0627\u064A\u0647",
    "\u0643\u062F\u0647", "\u0643\u062F\u0629", "\u0643\u062F\u0627",
    "\u062F\u0644\u0648\u0642\u062A\u064A",
    "\u0639\u0627\u064A\u0632", "\u0639\u0627\u0648\u0632", "\u0639\u0627\u064A\u0632\u0629", "\u0639\u0627\u0648\u0632\u0629",
    "\u0639\u0627\u064A\u0632\u064A\u0646", "\u0639\u0627\u0648\u0632\u064A\u0646",
    "\u0645\u0634",
    "\u0627\u062D\u0646\u0627", "\u0625\u062D\u0646\u0627",
    "\u0627\u0646\u062A\u0648", "\u0625\u0646\u062A\u0645",
    "\u0627\u0646\u062A\u064A", "\u0625\u0646\u062A\u064A",
    "\u0647\u0645\u0627", "\u0647\u0645\u0647", "\u0647\u0645\u0629",
    "\u0628\u0633", "\u062E\u0644\u0627\u0635", "\u0637\u064A\u0628",
    "\u0643\u0645\u0627\u0646", "\u0628\u0631\u0636\u0648", "\u0628\u0631\u0636\u0647",
    "\u0623\u0647\u0648", "\u0627\u0647\u0648", "\u0623\u0647\u064A", "\u0627\u0647\u064A",
    "\u0641\u064A\u0646", "\u0627\u0632\u0627\u064A", "\u0625\u0632\u0627\u064A",
    "\u0643\u0627\u0645", "\u0642\u062F \u0627\u064A\u0647",
    "\u0628\u062A\u0627\u0639", "\u0628\u062A\u0627\u0639\u0629", "\u0628\u062A\u0648\u0639",
    "\u0628\u062A\u0627\u0639\u0643", "\u0628\u062A\u0627\u0639\u0643\u0645",
    "\u062F\u0627", "\u062F\u064A", "\u062F\u0648\u0644",
    "\u0645\u0627\u0634\u064A", "\u062D\u0627\u0636\u0631",
    "\u0628\u0642\u0627\u0644\u064A", "\u0628\u0642\u0627\u0644\u0647", "\u0628\u0642\u0627\u0644\u0643",
    "\u0644\u0633\u0647", "\u0644\u0633\u0627",
    "\u064A\u0639\u0646\u064A", "\u0639\u0634\u0627\u0646", "\u0639\u0644\u0634\u0627\u0646",
    "\u0623\u0635\u0644", "\u064A\u0627 \u0641\u0646\u062F\u0645", "\u0627\u0641\u0646\u062F\u0645",
    "\u0625\u0632\u0627\u064A\u0643", "\u0627\u0632\u0627\u064A\u0643",
    "\u0639\u0627\u064A\u0634", "\u0645\u0628\u0631\u0648\u0643",
    "\u0628\u0627\u0644\u0630\u0628\u0637", "\u0645\u0636\u0628\u0648\u0637",
    "\u062E\u0644\u064A\u0643", "\u0633\u064A\u0628\u0643",
    "\u064A\u0628\u0642\u0649", "\u064A\u0635\u062F\u0642",
    "\u0623\u0643\u064A\u062F", "\u0637\u0628\u0639\u0627",
    "\u064A\u0627 \u0633\u064A\u062F\u064A", "\u064A\u0627 \u0628\u0648",
    "\u0639\u0645\u0644\u062A \u0627\u064A\u0647",
    "\u0627\u0646\u062A \u0639\u0627\u0631\u0641",
    "\u0627\u0644\u0635\u0628\u062D", "\u0627\u0644\u0638\u0647\u0631",
    "\u0627\u0644\u0645\u063A\u0631\u0628", "\u0627\u0644\u0639\u0634\u0627",
    "\u0642\u0648\u0644\u062A \u0627\u064A\u0647",
    "\u062E\u0644\u064A\u0647", "\u062E\u0644\u064A\u0647\u0627",
    "\u062A\u0639\u0627\u0644", "\u062A\u0639\u0627\u0644\u0649",
    "\u0631\u0648\u062D", "\u0631\u0648\u062D\u0649",
    "\u0627\u0644\u0641\u0643\u0631\u0647", "\u064A\u0627 \u0633\u062A\u064A",
    "\u0643\u062F\u0647 \u0643\u062F\u0647",
    "\u0645\u0646\u0641\u0633", "\u0648\u0633\u0637",
    "\u0628\u0635\u0631\u0627\u062D\u0629",
]


def normalize_arabic(text: str) -> str:
    """Normalise Arabic orthography to a canonical form.

    Returns ``""`` for empty/None input rather than propagating the falsy
    value, so the ``-> str`` contract holds and callers can concatenate
    the result unconditionally.
    """
    if not text:
        return ""
    result = []
    for ch in text:
        normalized_char = ARABIC_NORMALIZE_MAP.get(ch, ch)
        result.append(ARABIC_TO_INDIC.get(normalized_char, normalized_char))
    normalized = "".join(result)
    normalized = re.sub(r"[\u064B-\u065F\u0670]", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def detect_language(text: str) -> str:
    if not text or not text.strip():
        return "en"
    text_stripped = text.strip()
    arabic_chars = len(ARABIC_PATTERN.findall(text_stripped))
    total_chars = len(text_stripped)
    if total_chars == 0:
        return "en"
    ratio = arabic_chars / total_chars
    if ratio > 0.15:
        return "ar"
    if is_egyptian(text_stripped):
        return "ar"
    return "en"


def is_egyptian(text: str) -> bool:
    if not text:
        return False
    matches = sum(1 for m in EGYPTIAN_MARKERS if m in text)
    return matches >= 1


def clean_and_detect(text: str) -> dict:
    if not text:
        return {"text": "", "language": "en"}
    language = detect_language(text)
    return {"text": text, "language": language}


def get_tts_voice(language: str) -> str:
    """Voice id for a language.

    Delegates to :mod:`voice.routing` so there is a single registry, but a
    configured override in ``config`` still wins.
    """
    from config import config
    from voice import routing

    normalized = routing.normalize_language(language)
    override = {
        "ar": config.TTS_VOICE_AR,
        "en": config.TTS_VOICE_EN,
    }.get(normalized)
    if override:
        return override
    return routing.get_voice(normalized)


def get_stt_language(language: str) -> str:
    from voice import routing
    return routing.normalize_language(language)


def language_name(code: str) -> str:
    return {"ar": "\u0627\u0644\u0639\u0631\u0628\u064A\u0629", "en": "English"}.get(code, code)
