"""Bilingual voice routing: language detection and dynamic voice selection."""

import pytest

from voice import routing
from voice.routing import (
    LANG_AR,
    LANG_EN,
    VoiceProfile,
    detect_text_language,
    get_profile,
    get_voice,
    normalize_language,
    select_voice_for_text,
    voice_chain,
)
from voice.tts import TextToSpeech


ARABIC = "مرحبا، إزيك؟ عايز أعرف الساعة"
ENGLISH = "Hello, how are you? I would like the time please"


# ---------------------------------------------------------------------------
# Language normalisation
# ---------------------------------------------------------------------------
class TestNormalizeLanguage:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("ar", "ar"), ("AR", "ar"), ("en", "en"), ("EN", "en"),
            ("ar-EG", "ar"), ("ar_EG", "ar"), ("ar-eg", "ar"),
            ("en-GB", "en"), ("en_GB", "en"), ("en-US", "en"),
            ("eg", "ar"), ("EG", "ar"),
            ("fr", "en"), ("de", "en"), ("zz", "en"),
        ],
    )
    def test_locales_map_to_two_languages(self, value, expected):
        assert normalize_language(value) == expected

    @pytest.mark.parametrize("value", [None, "", "   ", 123, []])
    def test_falsy_and_garbage_default_to_english(self, value):
        assert normalize_language(value) == "en"

    def test_any_arabic_locale_is_arabic(self):
        for code in ("ar", "ar-SA", "ar-MA", "ar-DZ", "ar-001"):
            assert normalize_language(code) == "ar"


# ---------------------------------------------------------------------------
# Text-based detection
# ---------------------------------------------------------------------------
class TestTextLanguageDetection:
    def test_arabic_detected(self):
        assert detect_text_language(ARABIC) == "ar"

    def test_english_detected(self):
        assert detect_text_language(ENGLISH) == "en"

    def test_single_arabic_word_in_english_does_not_flip(self):
        # A stray Arabic name inside mostly-English text must not switch voices.
        assert detect_text_language("Please open the ملف folder") == "en"
        assert detect_text_language("What is the ip address") == "en"

    def test_arabic_dominant_mixed_text_does_flip(self):
        # Same words, but Arabic-dominant: now it should be Arabic.
        assert detect_text_language("ايه الـ ip address بتاعك") == "ar"

    def test_egyptian_marker_flips_to_arabic(self):
        # Egyptian markers are Arabic-script, so a transliterated line stays
        # English while genuine Egyptian Arabic flips.
        assert detect_text_language("kdeh el file ya zaytoon") == "en"
        assert detect_text_language("ا��ه الملف يا زيتون") == "ar"

    def test_empty_defaults(self):
        assert detect_text_language("") == "en"
        assert detect_text_language(None) == "en"
        assert detect_text_language("   ") == "en"

    def test_default_is_configurable(self):
        assert detect_text_language("", default="ar") == "ar"

    def test_pure_emoji_or_numbers(self):
        assert detect_text_language("123 456 !!!") == "en"

    def test_mixed_heavy_arabic_uses_transcript_ratio(self):
        assert detect_text_language("Hi مرحبا بكم في البيت") == "ar"


# ---------------------------------------------------------------------------
# Voice profile mapping
# ---------------------------------------------------------------------------
class TestVoiceMapping:
    def test_arabic_voice_is_egyptian(self):
        assert get_voice("ar") == "ar-EG-ShakirNeural"
        profile = get_profile("ar")
        assert profile.locale == "ar-EG"
        assert profile.ssml_lang == "ar-EG"

    def test_arabic_fallback_includes_salma(self):
        chain = voice_chain("ar")
        assert chain[0] == "ar-EG-ShakirNeural"
        assert "ar-EG-SalmaNeural" in chain

    def test_english_voice_is_british(self):
        assert get_voice("en") == "en-GB-RyanNeural"
        assert get_profile("en").locale == "en-GB"

    def test_english_fallback_includes_guy(self):
        chain = voice_chain("en")
        assert chain[0] == "en-GB-RyanNeural"
        assert "en-US-GuyNeural" in chain

    def test_voice_chain_is_ordered_and_unique(self):
        for lang in ("ar", "en"):
            chain = voice_chain(lang)
            assert chain == list(dict.fromkeys(chain))
            assert chain[0] == get_voice(lang)

    def test_unknown_language_uses_english(self):
        assert get_voice("fr") == get_voice("en")

    def test_all_configured_voices_are_neural(self):
        for lang in ("ar", "en"):
            for voice in voice_chain(lang):
                assert voice.endswith("Neural"), voice

    def test_primary_voice_matches_declared_locale(self):
        for lang in ("ar", "en"):
            profile = get_profile(lang)
            assert profile.voice.startswith(profile.locale)

    def test_fallbacks_are_allowed_to_cross_locale(self):
        # Cross-locale fallbacks (en-US inside the en-GB chain) are intentional:
        # they keep speech working if a locale's voices are retired.
        chain = voice_chain("en")
        assert chain[0].startswith("en-GB")
        assert any(v.startswith("en-US") for v in chain)

    def test_every_fallback_voice_is_neural(self):
        for lang in ("ar", "en"):
            for voice in voice_chain(lang):
                assert voice.endswith("Neural")

    def test_profiles_are_distinct(self):
        assert get_profile("ar").voice != get_profile("en").voice

    def test_describe_is_serialisable(self):
        described = routing.describe()
        assert set(described) == {"ar", "en"}
        assert described["ar"]["voice"] == "ar-EG-ShakirNeural"
        assert described["ar"]["fallbacks"]


# ---------------------------------------------------------------------------
# Dynamic selection
# ---------------------------------------------------------------------------
class TestDynamicSelection:
    def test_selects_arabic_voice_for_arabic_text(self):
        profile = select_voice_for_text(ARABIC)
        assert profile.language == "ar"
        assert profile.voice == "ar-EG-ShakirNeural"

    def test_selects_english_voice_for_english_text(self):
        profile = select_voice_for_text(ENGLISH)
        assert profile.language == "en"
        assert profile.voice == "en-GB-RyanNeural"

    def test_explicit_override_beats_text(self):
        # English text, but the caller insists on Arabic.
        assert select_voice_for_text(ENGLISH, override="ar").language == "ar"
        # Arabic text, but the caller insists on English.
        assert select_voice_for_text(ARABIC, override="en").language == "en"

    def test_locale_override_accepted(self):
        assert select_voice_for_text("x", override="ar-EG").language == "ar"

    def test_auto_switch_between_languages(self):
        """The same session can flip language per message."""
        picks = [select_voice_for_text(t).voice for t in (ENGLISH, ARABIC, ENGLISH)]
        assert picks == [
            "en-GB-RyanNeural",
            "ar-EG-ShakirNeural",
            "en-GB-RyanNeural",
        ]

    def test_rate_and_volume_helpers(self):
        assert routing.rate_for("ar") is not None
        assert routing.rate_for("en", override="+10%") == "+10%"
        assert routing.volume_for("en", override="-5%") == "-5%"


# ---------------------------------------------------------------------------
# TextToSpeech integration
# ---------------------------------------------------------------------------
class TestTTSVoiceResolution:
    @pytest.fixture
    def tts(self):
        return TextToSpeech()

    def test_resolve_voice_by_language(self, tts):
        assert tts.resolve_voice("ar") == "ar-EG-ShakirNeural"
        assert tts.resolve_voice("en") == "en-GB-RyanNeural"

    def test_resolve_voice_by_locale(self, tts):
        assert tts.resolve_voice("ar-EG") == "ar-EG-ShakirNeural"
        assert tts.resolve_voice("en-GB") == "en-GB-RyanNeural"

    def test_voice_for_text_switches_language(self, tts):
        assert tts.voice_for_text(ENGLISH) == "en-GB-RyanNeural"
        assert tts.voice_for_text(ARABIC) == "ar-EG-ShakirNeural"

    def test_config_override_wins(self, tts, monkeypatch):
        from config import config
        monkeypatch.setattr(config, "TTS_VOICE_AR", "ar-EG-SalmaNeural", raising=False)
        assert tts.resolve_voice("ar") == "ar-EG-SalmaNeural"

    def test_falls_back_when_configured_voice_missing(self, tts, monkeypatch):
        from config import config
        monkeypatch.setattr(config, "TTS_VOICE_AR", "ar-EG-RetiredNeural", raising=False)
        # Pretend edge-tts reports its real voice list.
        tts._available_voices = {"ar-EG-ShakirNeural", "ar-EG-SalmaNeural"}
        assert tts.resolve_voice("ar") == "ar-EG-ShakirNeural"

    def test_supported_voices_lists_both_languages(self, tts):
        voices = tts.supported_voices()
        assert "ar-EG-ShakirNeural" in voices
        assert "en-GB-RyanNeural" in voices

    def test_cache_key_includes_voice(self, tts):
        a = tts._get_cache_key("hello", "ar-EG-ShakirNeural")
        b = tts._get_cache_key("hello", "en-GB-RyanNeural")
        assert a != b

    def test_clear_cache(self, tts):
        tts._cache_put("k", b"x")
        tts.clear_cache()
        assert tts._cache_get("k") is None

    def test_large_audio_is_not_cached(self, tts):
        tts._cache_put("big", b"0" * 100_001)
        assert tts._cache_get("big") is None


# ---------------------------------------------------------------------------
# STT language reporting
# ---------------------------------------------------------------------------
class TestSTTLanguageReporting:
    def test_result_carries_voice_hint(self):
        from voice.stt import TranscriptionResult

        result = TranscriptionResult(
            text="مرحبا", language="ar", detected=True, voice_hint="ar-EG-ShakirNeural"
        )
        assert result.to_dict()["language"] == "ar"
        assert result.to_dict()["voice"] == "ar-EG-ShakirNeural"

    def test_unavailable_stt_returns_empty_with_default(self):
        from voice.stt import SpeechToText

        stt = SpeechToText()
        stt._load_error = "not installed"
        stt._model = None
        stt._loading = False
        result = stt.transcribe_detailed(b"x" * 500)
        assert result.text == ""
        assert result.language in ("ar", "en")

    def test_tiny_audio_discarded(self):
        from voice.stt import SpeechToText

        stt = SpeechToText()
        stt._model = object()  # loaded
        stt._loading = False
        result = stt.transcribe_detailed(b"\x00" * 10)
        assert result.text == ""

    def test_hint_is_normalised(self):
        from voice.stt import SpeechToText

        stt = SpeechToText()
        stt._load_error = "x"
        stt._model = None
        stt._loading = False
        result = stt.transcribe_detailed(b"x" * 500, language="ar-EG")
        assert result.language == "ar"

    def test_backwards_compatible_transcribe_returns_text(self):
        from voice.stt import SpeechToText

        stt = SpeechToText()
        stt._load_error = "x"
        stt._model = None
        stt._loading = False
        assert stt.transcribe(b"x" * 500) == ""


# ---------------------------------------------------------------------------
# audio_format / clean_stt_output unchanged
# ---------------------------------------------------------------------------
class TestSTTHelpers:
    def test_detect_webm(self):
        from voice.stt import detect_audio_format

        assert detect_audio_format(b"\x1a\x45\xdf\xa3" + b"\x00" * 40) == ".webm"

    def test_detect_wav(self):
        from voice.stt import detect_audio_format

        assert detect_audio_format(b"RIFF" + b"\x00" * 40) == ".wav"

    def test_clean_stt_uppercases_english_only(self):
        from voice.stt import clean_stt_output

        assert clean_stt_output("hello there", "en") == "Hello there"
        assert clean_stt_output("مرحبا", "ar") == "مرحبا"

    def test_clean_stt_collapses_whitespace(self):
        from voice.stt import clean_stt_output

        # English transcripts are sentence-cased; Arabic is left as-is.
        assert clean_stt_output("a   b\n\nc", "en") == "A b c"
        assert clean_stt_output("a   b\n\nc", "ar") == "a b c"
