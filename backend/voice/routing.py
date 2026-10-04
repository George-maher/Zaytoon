"""Dynamic bilingual voice routing.

Central registry mapping a language code to the right ``edge-tts`` voice, with
a documented preference order and graceful degradation when a configured voice
is unavailable. Both the TTS output path and the STT input path route through
this module so a single switch governs both directions.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger("Zaytoon.Voice.Routing")

# Language codes used across the app.
LANG_AR = "ar"
LANG_EN = "en"


@dataclass(frozen=True)
class VoiceProfile:
    """A voice plus the edge-tts settings that suit its locale."""

    voice: str
    language: str
    locale: str
    #: BCP-47 tag for the SSML wrapper.
    ssml_lang: str
    #: Slightly slower delivery reads better for Arabic script.
    default_rate: str = "+0%"
    default_volume: str = "+0%"
    description: str = ""
    #: Alternative voices tried in order if the primary is unavailable.
    fallbacks: List[str] = field(default_factory=list)


# Egyptian Arabic is the product's primary dialect; English is British to match
# the existing configuration.
VOICE_PROFILES: Dict[str, VoiceProfile] = {
    LANG_AR: VoiceProfile(
        voice="ar-EG-ShakirNeural",
        language=LANG_AR,
        locale="ar-EG",
        ssml_lang="ar-EG",
        default_rate="+0%",
        default_volume="+0%",
        description="Egyptian Arabic, male",
        # Only voices verified to exist in the edge-tts catalogue. A retired
        # id would make synthesis fail with "No audio was received".
        fallbacks=["ar-EG-SalmaNeural"],
    ),
    LANG_EN: VoiceProfile(
        voice="en-GB-RyanNeural",
        language=LANG_EN,
        locale="en-GB",
        ssml_lang="en-GB",
        default_rate="+0%",
        default_volume="+0%",
        description="British English, male",
        fallbacks=["en-GB-ThomasNeural", "en-US-GuyNeural", "en-US-AriaNeural"],
    ),
}

DEFAULT_LANGUAGE = LANG_EN

# Locales that should resolve to Egyptian Arabic rather than being treated as
# generic Arabic.
_ARABIC_LOCALES = ("ar-eg", "ar_eg", "ar-ara", "eg", "egypt")
_ENGLISH_LOCALES = ("en-gb", "en_gb", "en-us", "en_us", "en-au", "en-ca", "en")


def normalize_language(language: Optional[str]) -> str:
    """Map any locale-ish string onto ``"ar"`` or ``"en"``.

    Accepts bare codes (``"ar"``), locales (``"ar-EG"``), and ``None``.
    Anything unrecognised falls back to English, matching
    :func:`arabic.detect_language`'s default.
    """
    if not language or not isinstance(language, str):
        return DEFAULT_LANGUAGE
    code = language.strip().lower().replace("_", "-")
    if not code:
        return DEFAULT_LANGUAGE
    if code in _ARABIC_LOCALES or code.split("-")[0] == LANG_AR:
        return LANG_AR
    if code in _ENGLISH_LOCALES or code.split("-")[0] == LANG_EN:
        return LANG_EN
    return DEFAULT_LANGUAGE


def get_profile(language: Optional[str]) -> VoiceProfile:
    """Return the :class:`VoiceProfile` for a language, defaulting to English."""
    return VOICE_PROFILES[normalize_language(language)]


def get_voice(language: Optional[str]) -> str:
    """The primary edge-tts voice id for a language."""
    return get_profile(language).voice


def voice_chain(language: Optional[str]) -> List[str]:
    """Primary voice followed by its fallbacks, de-duplicated."""
    profile = get_profile(language)
    chain = [profile.voice]
    for candidate in profile.fallbacks:
        if candidate not in chain:
            chain.append(candidate)
    return chain


def ssml_lang_for(language: Optional[str]) -> str:
    return get_profile(language).ssml_lang


def rate_for(language: Optional[str], override: Optional[str] = None) -> str:
    """Resolve the speaking rate, letting config override the profile default."""
    if override:
        return override
    return get_profile(language).default_rate


def volume_for(language: Optional[str], override: Optional[str] = None) -> str:
    if override:
        return override
    return get_profile(language).default_volume


# ---------------------------------------------------------------------------
# Text-based language selection
# ---------------------------------------------------------------------------
#: Shared with arabic.py so text routing and STT routing agree.
_ARABIC_PATTERN = re.compile(
    r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]"
)

#: Both conditions must hold before a mostly-English sentence is treated as
#: Arabic: enough Arabic characters *and* a meaningful share of the text. This
#: stops a stray Arabic word (a name, a unit) from flipping the voice.
_MIN_ARABIC_CHARS_FOR_FLIP = 3
_MIN_ARABIC_RATIO = 0.15


def detect_text_language(text: Optional[str], default: str = DEFAULT_LANGUAGE) -> str:
    """Choose a voice for ``text``.

    Mirrors :func:`arabic.detect_language` (Arabic-character ratio plus
    Egyptian markers) but is safe to call from the voice layer without
    importing the full arabic module, and it needs the *output* text only.
    """
    if not text or not str(text).strip():
        return default
    value = str(text)
    arabic_chars = len(_ARABIC_PATTERN.findall(value))
    if arabic_chars:
        total = len(value.strip())
        if total and arabic_chars >= _MIN_ARABIC_CHARS_FOR_FLIP and (
            arabic_chars / total
        ) > _MIN_ARABIC_RATIO:
            return LANG_AR
    try:
        from arabic import is_egyptian

        if is_egyptian(value):
            return LANG_AR
    except Exception:  # pragma: no cover - defensive
        pass
    return default


def select_voice_for_text(
    text: Optional[str],
    override: Optional[str] = None,
    default: str = DEFAULT_LANGUAGE,
) -> VoiceProfile:
    """Pick a voice for ``text``, honouring an explicit language override.

    This is the single entry point the TTS layer uses, so dynamic
    language-switching is decided in exactly one place.
    """
    language = normalize_language(override) if override else detect_text_language(text, default)
    return get_profile(language)


def describe() -> Dict[str, dict]:
    """Serializable summary for ``GET /health`` and diagnostics."""
    return {
        code: {
            "voice": profile.voice,
            "locale": profile.locale,
            "fallbacks": list(profile.fallbacks),
            "description": profile.description,
        }
        for code, profile in VOICE_PROFILES.items()
    }
