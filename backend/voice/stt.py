import os
import re
import logging
import tempfile
import threading
from dataclasses import dataclass
from typing import Optional

from voice import routing
from common.reporting import safe_error

logger = logging.getLogger("Zaytoon.Voice.STT")


def detect_audio_format(audio_data: bytes) -> str:
    if len(audio_data) >= 4:
        if audio_data[:4] == b"\x1a\x45\xdf\xa3":
            return ".webm"
        if audio_data[:4] == b"RIFF":
            return ".wav"
        if audio_data[:3] == b"ID3" or audio_data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xfa"):
            return ".mp3"
        if audio_data[:4] == b"OggS":
            return ".ogg"
        logger.warning(f"Unknown audio header: {audio_data[:8].hex()} ({len(audio_data)} bytes)")
    return ".webm"


def clean_stt_output(text: str, detected_language: str = "en") -> str:
    if not text:
        return ""
    text = text.strip().strip(".,!?;\u061C\u060C\u061F\u061B ")
    text = re.sub(r"\s+", " ", text).strip()
    if text and text[0].isalpha() and detected_language == "en":
        text = text[0].upper() + text[1:] if len(text) > 1 else text.upper()
    return text


@dataclass
class TranscriptionResult:
    """STT output plus the language metadata the rest of the pipeline needs.

    The language detected by Whisper is more trustworthy than re-sniffing the
    transcript text, so it is carried explicitly rather than being re-guessed
    downstream.
    """

    text: str
    language: str
    detected: bool = True
    duration: float = 0.0
    voice_hint: str = ""

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "language": self.language,
            "detected": self.detected,
            "duration": self.duration,
            "voice": self.voice_hint,
        }


class SpeechToText:
    def __init__(self, model_name: str = "tiny", device: str = "cpu", compute_type: str = "int8"):
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self._model = None
        self._load_error = None
        self._load_lock = threading.Lock()
        self._loading = False

    def _load_model(self):
        if self._model is not None:
            return True
        if self._loading:
            return False
        with self._load_lock:
            if self._model is not None:
                return True
            if self._loading:
                return False
            self._loading = True
            try:
                from faster_whisper import WhisperModel
                logger.info(f"Loading Whisper model '{self.model_name}' on {self.device}...")
                self._model = WhisperModel(
                    self.model_name,
                    device=self.device,
                    compute_type=self.compute_type,
                    download_root=os.getenv("WHISPER_DOWNLOAD_ROOT", None),
                    cpu_threads=os.cpu_count() or 4,
                    num_workers=2,
                )
                logger.info("Whisper model loaded successfully.")
                self._loading = False
                return True
            except ImportError:
                msg = "faster-whisper not installed. Install with: pip install faster-whisper"
                logger.warning(msg)
                self._load_error = msg
                self._model = None
                self._loading = False
                return False
            except Exception as e:
                # `_load_error` is surfaced by `GET /health`, so it goes
                # through the reporting layer rather than carrying `str(e)`.
                msg = safe_error(
                    e, operation="whisper.load", component="voice.stt",
                    detail=f"model '{self.model_name}'",
                )
                self._load_error = msg
                self._model = None
                self._loading = False
                return False

    @property
    def available(self) -> bool:
        return self._load_model()

    @property
    def loading(self) -> bool:
        return self._loading

    def transcribe(self, audio_data: bytes, language: Optional[str] = None) -> str:
        """Backwards-compatible transcript-only API."""
        return self.transcribe_detailed(audio_data, language).text

    def transcribe_detailed(
        self, audio_data: bytes, language: Optional[str] = None
    ) -> TranscriptionResult:
        """Transcribe and report the detected language.

        ``language`` is a hint: it is forwarded as the decode language (faster
        and more accurate for a known locale) but the returned language is
        re-derived from the transcript so a wrong hint cannot misroute the TTS
        voice.
        """
        auto_detected = not language
        hint = routing.normalize_language(language) if language else None

        loaded = self._load_model()
        if not loaded:
            if not self._loading:
                err = self._load_error or "STT model not loaded"
                logger.warning(f"transcribe() skipped: {err}")
            return TranscriptionResult(
                "", hint or routing.DEFAULT_LANGUAGE, False
            )

        if len(audio_data) < 100:
            logger.warning(f"Audio too small ({len(audio_data)} bytes), discarding")
            return TranscriptionResult(
                "", hint or routing.DEFAULT_LANGUAGE, False
            )

        audio_format = detect_audio_format(audio_data)
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=audio_format, delete=False) as tmp:
                tmp.write(audio_data)
                tmp_path = tmp.name

            logger.info(
                f"STT transcribing {len(audio_data)} bytes as '{audio_format}'"
                + (f" (lang hint: {hint})" if hint else " (auto-detect)")
            )
            segments, info = self._model.transcribe(
                tmp_path,
                language=hint,
                beam_size=5,
                vad_filter=True,
                vad_parameters=dict(
                    min_silence_duration_ms=300,
                    threshold=0.35,
                    min_speech_duration_ms=100,
                    max_speech_duration_s=30,
                ),
                condition_on_previous_text=False,
                no_speech_threshold=0.7,
                compression_ratio_threshold=2.4,
            )

            result = " ".join(segment.text for segment in segments)
            raw_lang = getattr(info, "language", None) if info else None
            # Trust the transcript over the hint.
            detected_lang = routing.detect_text_language(
                result, default=routing.normalize_language(raw_lang or hint)
            )
            cleaned = clean_stt_output(result.strip(), detected_lang)
            logger.info(
                f"STT: lang={detected_lang} (raw={raw_lang}), text='{cleaned[:80]}'"
            )
            return TranscriptionResult(
                text=cleaned,
                language=detected_lang,
                detected=auto_detected,
                duration=float(getattr(info, "duration", 0.0) or 0.0),
                voice_hint=routing.get_voice(detected_lang),
            )

        except Exception as e:
            safe_error(e, operation="transcribe", component="voice.stt")
            return TranscriptionResult(
                "", hint or routing.DEFAULT_LANGUAGE, False
            )
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
