import asyncio
import logging
import re
from collections import OrderedDict
from typing import Optional, AsyncGenerator, List

from voice import routing
from common.reporting import safe_error

logger = logging.getLogger("Zaytoon.Voice.TTS")

# Split on sentence-ending punctuation followed by whitespace.
# Supports Arabic (؟،؛) and English (.!?,;)
SENTENCE_BOUNDARY = re.compile(
    r"(?<=[.!?\u061F\u060C\u061B;])\s+|(?<=\n)\s*(?=\n)"
)

# TTS cache for frequently spoken short phrases (LRU via OrderedDict)
_TTS_CACHE: OrderedDict = OrderedDict()
_MAX_CACHE_SIZE = 50


class TextToSpeech:
    """edge-tts wrapper with dynamic bilingual voice selection.

    Every public method accepts an optional ``language``. When it is omitted
    the voice is chosen from the *text* itself, so an English reply and an
    Egyptian-Arabic reply in the same session are read in the right voice
    without the caller tracking language state.
    """

    def __init__(self):
        self._available = False
        self._available_voices: set = set()
        self._check_available()
        self._cache = _TTS_CACHE

    def _check_available(self):
        try:
            import edge_tts
            self._available = True
        except ImportError:
            logger.warning("edge-tts not installed. Install with: pip install edge-tts")
            self._available = False

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        return self._available

    async def probe_voices(self) -> set:
        """Best-effort fetch of the voices edge-tts actually offers.

        Used to fall back gracefully when a configured voice has been retired.
        Never raises.
        """
        if not self._available:
            return set()
        try:
            import edge_tts

            voices = await edge_tts.list_voices()
            self._available_voices = {v.get("ShortName", "") for v in voices if v.get("ShortName")}
            logger.info("edge-tts exposes %d voices", len(self._available_voices))
        except Exception as e:
            logger.debug("voice probe failed: %s", e)
        return self._available_voices

    def resolve_voice(self, language: Optional[str] = None) -> str:
        """Pick the best available voice for a language.

        Honours an override in config, then the registry default, then the
        registry's fallback list when we know the primary is unavailable.
        """
        from config import config as app_config

        configured = {
            routing.LANG_AR: app_config.TTS_VOICE_AR,
            routing.LANG_EN: app_config.TTS_VOICE_EN,
        }.get(routing.normalize_language(language))

        chain = routing.voice_chain(language)
        if configured:
            chain = [configured] + [v for v in chain if v != configured]

        if self._available_voices:
            for candidate in chain:
                if candidate in self._available_voices:
                    return candidate
            logger.warning(
                "no configured voice available for %s; falling back to %s",
                language, chain[-1],
            )
        return chain[0]

    @staticmethod
    def split_sentences(text: str) -> list:
        parts = SENTENCE_BOUNDARY.split(text)
        return [p.strip() for p in parts if p.strip()]

    def _get_cache_key(self, text: str, voice: str) -> str:
        return f"{voice}:{text.strip()[:100]}"

    def _cache_get(self, key: str) -> Optional[bytes]:
        if key in self._cache:
            logger.debug("TTS cache hit")
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def _cache_put(self, key: str, value: bytes):
        # Only cache small responses, otherwise a long reply evicts everything.
        if len(value) >= 100_000:
            return
        if key in self._cache:
            self._cache.move_to_end(key)
        else:
            self._cache[key] = value
        while len(self._cache) > _MAX_CACHE_SIZE:
            self._cache.popitem(last=False)

    # ------------------------------------------------------------------
    # Synthesis
    # ------------------------------------------------------------------
    async def synthesize(
        self,
        text: str,
        voice: Optional[str] = None,
        rate: Optional[str] = None,
        volume: Optional[str] = None,
        language: Optional[str] = None,
    ) -> Optional[bytes]:
        from config import config as app_config

        if not self._available or not (text or "").strip():
            return None

        # Dynamic routing: an explicit `voice` wins, then an explicit
        # `language`, then inference from the text itself.
        if voice:
            resolved_voice = voice
        else:
            profile = routing.select_voice_for_text(text, override=language)
            resolved_voice = self.resolve_voice(profile.language)
            if profile is not None and voice is None:
                logger.debug(
                    "TTS auto-selected %s (%s)", resolved_voice, profile.language
                )

        resolved_rate = rate or app_config.TTS_RATE or routing.rate_for(language)
        resolved_volume = volume or app_config.TTS_VOLUME or routing.volume_for(language)

        cache_key = self._get_cache_key(text, resolved_voice)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        try:
            import edge_tts

            communicate = edge_tts.Communicate(
                text, resolved_voice, rate=resolved_rate, volume=resolved_volume
            )
            audio_data = bytearray()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_data.extend(chunk["data"])
            if not audio_data:
                return None
            result = bytes(audio_data)
            self._cache_put(cache_key, result)
            logger.info(
                "TTS: %d bytes with voice '%s'", len(audio_data), resolved_voice
            )
            return result
        except asyncio.CancelledError:
            raise
        except Exception as e:
            safe_error(e, operation="tts.synthesize", component="voice.tts")
            return None

    async def stream_synthesize(
        self,
        text: str,
        voice: Optional[str] = None,
        rate: Optional[str] = None,
        volume: Optional[str] = None,
        language: Optional[str] = None,
    ) -> AsyncGenerator[bytes, None]:
        """Stream audio chunks. This is what keeps early TTS low-latency."""
        from config import config as app_config

        if not (text or "").strip():
            return
        if not self._available:
            logger.error("TTS unavailable: edge-tts not installed")
            return

        if voice:
            resolved_voice = voice
        else:
            profile = routing.select_voice_for_text(text, override=language)
            resolved_voice = self.resolve_voice(profile.language)

        resolved_rate = rate or app_config.TTS_RATE
        resolved_volume = volume or app_config.TTS_VOLUME

        try:
            import edge_tts

            communicate = edge_tts.Communicate(
                text, resolved_voice, rate=resolved_rate, volume=resolved_volume
            )
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    yield chunk["data"]
        except asyncio.CancelledError:
            raise
        except Exception as e:
            safe_error(e, operation="tts.stream", component="voice.tts")

    async def synthesize_ssml(
        self,
        ssml: str,
        voice: Optional[str] = None,
        rate: Optional[str] = None,
        language: Optional[str] = None,
    ) -> Optional[bytes]:
        if not self._available or not (ssml or "").strip():
            return None
        try:
            import edge_tts

            resolved_voice = self.resolve_voice(language) if not voice else voice
            ssml_lang = routing.ssml_lang_for(language) if not voice else resolved_voice[:5]
            wrapped = (
                f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
                f'xml:lang="{ssml_lang}">'
                f'<voice name="{resolved_voice}">'
                f'<prosody rate="{rate or "+0%"}">{ssml}</prosody>'
                f"</voice>"
                f"</speak>"
            )
            communicate = edge_tts.Communicate(wrapped, resolved_voice)
            audio_data = bytearray()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_data.extend(chunk["data"])
            return bytes(audio_data) if audio_data else None
        except asyncio.CancelledError:
            raise
        except Exception as e:
            safe_error(e, operation="tts.ssml", component="voice.tts")
            return None

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    def voice_for_text(self, text: str) -> str:
        """Voice that would be used for ``text`` — used by tests and /health."""
        return self.resolve_voice(
            routing.select_voice_for_text(text).language
        )

    def supported_voices(self) -> List[str]:
        seen: List[str] = []
        for language in routing.VOICE_PROFILES:
            for voice in routing.voice_chain(language):
                if voice not in seen:
                    seen.append(voice)
        return seen

    def clear_cache(self):
        self._cache.clear()
        logger.info("TTS cache cleared")
