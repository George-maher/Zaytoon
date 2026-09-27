import logging

from arabic import detect_language, get_tts_voice, get_stt_language, language_name

logger = logging.getLogger("Zaytoon.Voice.Detector")


class LanguageDetector:
    @staticmethod
    def detect(text: str) -> str:
        return detect_language(text)

    @staticmethod
    def is_egyptian(text: str) -> bool:
        if not text:
            return False
        from arabic import is_egyptian as _is_egyptian
        return _is_egyptian(text)

    @staticmethod
    def language_name(code: str) -> str:
        return language_name(code)

    @staticmethod
    def get_tts_voice(language: str) -> str:
        return get_tts_voice(language)

    @staticmethod
    def get_stt_language(language: str) -> str:
        return get_stt_language(language)
