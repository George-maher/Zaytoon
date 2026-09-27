"""Voice pipeline: speech-to-text, text-to-speech, and language routing."""

from .stt import SpeechToText, TranscriptionResult
from .tts import TextToSpeech
from . import routing
from .routing import (
    VOICE_PROFILES,
    VoiceProfile,
    detect_text_language,
    get_voice,
    normalize_language,
    select_voice_for_text,
    voice_chain,
)

__all__ = [
    "SpeechToText",
    "TranscriptionResult",
    "TextToSpeech",
    "routing",
    "VOICE_PROFILES",
    "VoiceProfile",
    "detect_text_language",
    "get_voice",
    "normalize_language",
    "select_voice_for_text",
    "voice_chain",
]
