"""Microphone capture with voice-activity detection, and TTS playback.

Design notes
------------
* Capture and playback are both **push streams with ring buffers**, not
  ``sd.rec``/``sd.play`` calls. The TTS path needs to start the next chunk
  while the previous is still draining, otherwise every sentence boundary
  produces an audible gap.
* The VAD decision is a **pure function** (:func:`should_close_utterance`) so
  it can be unit tested without a microphone. Everything hardware-specific is
  confined to the two classes below.
* Audio is captured as mono 16-bit PCM at the configured rate, which is exactly
  what ``faster-whisper`` wants, so no conversion happens before the upload.
* TTS audio from the backend is **MP3** (from edge-tts) and must be decoded
  to PCM before being fed to the audio output stream.
"""

from __future__ import annotations

import io
import logging
import queue
import threading
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: Set once, the first time a decode fails, so a missing decoder is reported
#: in the log exactly once instead of on every chunk of every reply.
_decoder_error_logged = False

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy is a hard requirement
    np = None  # type: ignore[assignment]

try:
    import sounddevice as sd
except ImportError:  # pragma: no cover
    sd = None  # type: ignore[assignment]

#: PyAV is the MP3 decoder. It matters that this is PyAV and not pydub:
#: pydub is only a wrapper around the *ffmpeg binary*, which is not installed
#: on a default Windows machine, so a pydub-based decoder silently yields zero
#: bytes and the user hears nothing. PyAV links ffmpeg as a library, so it
#: works from a plain `pip install` with no external tool.
try:
    import av  # noqa: F401
    _AV_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without PyAV
    _AV_AVAILABLE = False

# Fallbacks, tried in order, if PyAV is somehow unavailable. Probed with
# find_spec rather than imported: `import pydub` emits a RuntimeWarning about
# the missing ffmpeg binary on every single start, and neither fallback is
# reached while PyAV works.
def _module_present(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):  # pragma: no cover
        return False


_PYDUB_AVAILABLE = _module_present("pydub")
_FFMPEG_AVAILABLE = _module_present("ffmpeg")

#: Bytes per sample for 16-bit mono audio.
BYTES_PER_SAMPLE = 2


def _log_decoder_missing_once() -> None:
    """Report an absent decoder exactly once per process.

    The previous decoder caught every exception and returned ``b""``, which
    turned "no decoder on this machine" into silence: the reply appeared to
    work, the transcript looked fine, and the only symptom was no sound. A
    missing dependency must be loud.
    """
    global _decoder_error_logged
    if not _decoder_error_logged:
        _decoder_error_logged = True
        logger.error(
            "no MP3 decoder available — TTS will be SILENT. Install PyAV "
            "(`pip install av`), which needs no external ffmpeg binary. "
            "pydub/ffmpeg-python are fallbacks but both require ffmpeg on PATH."
        )


def _log_decoder_error_once(message: str) -> None:
    """Report a decode failure once, so a log storm cannot hide it."""
    global _decoder_error_logged
    if not _decoder_error_logged:
        _decoder_error_logged = True
        logger.error("MP3 decode error (further errors suppressed): %s", message)


# ---------------------------------------------------------------------------
# MP3 to PCM Decoder
# ---------------------------------------------------------------------------
class Mp3Decoder:
    """Decodes a streamed MP3 response into 16-bit PCM for playback.

    Why this is not "decode each chunk as it arrives"
    -------------------------------------------------
    MP3 frames are not independently decodable. Each frame's ``main_data_begin``
    points *backwards* into a bit reservoir carried by the preceding frames, so
    a frame decoded without that history decodes to the wrong samples. The
    first decoder here re-opened the container on a growing prefix and emitted
    whatever parsed, discarding the undecoded remainder; on a real edge-tts
    reply fed in small pieces that produced 98% near-silence with a peak of
    5120 instead of 23831 — audible as clicks and gaps, not speech.

    So the compressed stream is kept whole and decoded from frame zero, and
    only the PCM *tail* beyond what has already been emitted is returned. The
    decode is repeated per chunk (O(n^2) in principle, milliseconds in
    practice for reply-sized audio) because correctness here is worth far more
    than the redundant work: a reply is a few hundred kilobytes and the
    alternative is audio nobody can hear.
    """

    #: Hard cap on retained compressed audio. A long reply is well under this;
    #: the cap exists so a missing ``audio_end`` cannot leak memory forever.
    MAX_BUFFER = 16 * 1024 * 1024

    def __init__(self, target_sample_rate: int = 24000, target_channels: int = 1):
        self.target_sample_rate = target_sample_rate
        self.target_channels = target_channels
        self._mp3_buffer = bytearray()
        self._emitted = 0
        self._initialized = False

    @property
    def available(self) -> bool:
        """True when some decoder backend is importable."""
        return _AV_AVAILABLE or _PYDUB_AVAILABLE or _FFMPEG_AVAILABLE

    def _init_decoder(self):
        """Initialize the MP3 decoder."""
        if self._initialized:
            return
        self._initialized = True
        if not self.available:
            _log_decoder_missing_once()

    def decode(self, mp3_chunk: bytes) -> bytes:
        """Add a chunk and return whatever new PCM is now decodable."""
        if mp3_chunk:
            self._init_decoder()
            self._mp3_buffer.extend(mp3_chunk)

        if not self._mp3_buffer:
            return b""

        if _AV_AVAILABLE:
            pcm = self._decode_with_av()
        elif _PYDUB_AVAILABLE:
            pcm = self._decode_with_pydub()
        elif _FFMPEG_AVAILABLE:
            pcm = self._decode_with_ffmpeg(bytes(self._mp3_buffer))
        else:
            _log_decoder_missing_once()
            return b""

        if len(pcm) <= self._emitted:
            # Nothing new yet: the trailing frame is still incomplete. The
            # stream is deliberately *not* reset, because the next chunk needs
            # the bytes already buffered to decode that frame correctly.
            if len(self._mp3_buffer) > self.MAX_BUFFER:
                _log_decoder_error_once(
                    f"compressed buffer reached {len(self._mp3_buffer)} bytes "
                    "without new audio; resetting decoder"
                )
                self.reset()
            return b""

        fresh = pcm[self._emitted :]
        self._emitted = len(pcm)
        return fresh

    def _decode_with_av(self) -> bytes:
        """Decode with PyAV, which links ffmpeg as a library (no binary)."""
        import av

        data = bytes(self._mp3_buffer)
        if not data:
            return b""
        out: list[bytes] = []
        container = None
        try:
            container = av.open(io.BytesIO(data))
            resampler = av.AudioResampler(
                format="s16", layout="mono", rate=self.target_sample_rate
            )
            for frame in container.decode(audio=0):
                for resampled in resampler.resample(frame):
                    out.append(resampled.to_ndarray().tobytes())
            for resampled in resampler.resample(None):
                out.append(resampled.to_ndarray().tobytes())
        except av.FFmpegError as exc:
            # Expected while streaming: the buffer ends mid-frame, so the
            # demuxer hits an incomplete packet. Whatever decoded before that
            # point is still valid and is returned, so this is not a failure
            # and must not be logged as one.
            logger.debug("av decode stopped on a partial frame: %s", exc)
        except Exception as exc:
            _log_decoder_error_once(f"av decode failed: {type(exc).__name__}: {exc}")
        finally:
            if container is not None:
                try:
                    container.close()
                except Exception:  # pragma: no cover
                    pass
        return b"".join(out)

    def _decode_with_pydub(self) -> bytes:
        """Fallback via pydub, which requires the ffmpeg *binary* on PATH."""
        if not _PYDUB_AVAILABLE:
            return b""
        try:
            from pydub import AudioSegment

            audio = AudioSegment.from_file(
                io.BytesIO(bytes(self._mp3_buffer)), format="mp3"
            )
        except Exception as exc:
            _log_decoder_error_once(f"pydub decode failed: {type(exc).__name__}: {exc}")
            return b""
        audio = audio.set_frame_rate(self.target_sample_rate)
        audio = audio.set_channels(self.target_channels)
        audio = audio.set_sample_width(2)
        return audio.raw_data
            
    def _decode_with_ffmpeg(self, mp3_chunk: bytes) -> bytes:
        """Fallback decoder using ffmpeg-python."""
        if not _FFMPEG_AVAILABLE:
            return b""

        import ffmpeg

        try:
            process = (
                ffmpeg
                .input('pipe:0', format='mp3')
                .output('pipe:1', format='s16le', acodec='pcm_s16le', 
                       ar=str(self.target_sample_rate), ac=self.target_channels)
                .run_async(pipe_stdin=True, pipe_stdout=True, pipe_stderr=True)
            )
            pcm_data, _ = process.communicate(input=mp3_chunk, timeout=5)
            return pcm_data
        except Exception:
            return b""
            
    def flush(self) -> bytes:
        """Return any PCM that only became decodable at end-of-stream."""
        return self.decode(b"")

    def reset(self):
        """Reset the decoder state.

        Called on interrupt so the next response starts a fresh MP3 stream
        instead of being appended to the interrupted one.
        """
        self._mp3_buffer = bytearray()
        self._emitted = 0


#: Bytes per sample for 16-bit mono audio.
BYTES_PER_SAMPLE = 2


def rms(chunk) -> float:
    """Root-mean-square level of a 16-bit PCM buffer, normalised to 0..1."""
    if np is None or len(chunk) == 0:
        return 0.0
    samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(np.square(samples))))


def should_close_utterance(
    *,
    speech_detected: bool,
    silence_frames: int,
    threshold_frames: int,
    speech_frames: int,
    min_speech_ms: int,
    frame_ms: float,
    duration_s: float,
    max_utterance_s: float,
) -> bool:
    """Decide whether the current utterance is finished.

    Kept free of hardware and state so the rules are directly testable:

    * never close before any speech was seen (that is the idle noise floor),
    * close after ``threshold_frames`` silent frames,
    * but discard rather than emit an utterance shorter than ``min_speech_ms``,
    * and always close at ``max_utterance_s`` so a stuck VAD cannot record
      forever.
    """
    if not speech_detected:
        return False
    if duration_s >= max_utterance_s:
        return True
    if silence_frames < threshold_frames:
        return False
    return speech_frames * frame_ms >= min_speech_ms


@dataclass
class Utterance:
    """A finished recording, ready to upload."""

    pcm: bytes
    duration_s: float

    def as_wav(self) -> bytes:
        """Wrap the raw PCM in a RIFF header, for saving to disk."""
        import io
        import wave

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(BYTES_PER_SAMPLE)
            handle.setframerate(16000)
            handle.writeframes(self.pcm)
        return buffer.getvalue()


@dataclass
class CaptureStats:
    frames: int = 0
    speech_frames: int = 0
    peak_rms: float = 0.0


class MicrophoneCapture:
    """Streams mic audio and emits an :class:`Utterance` when speech stops.

    The PortAudio callback runs on an audio thread and therefore does the
    minimum: append bytes to a deque. All interpretation happens on
    :meth:`poll`, which the caller drives from the UI thread.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        frame_ms: float = 33.0,
        device: str | None = None,
        vad_threshold: float = 0.012,
        vad_silence_frames: int = 25,
        min_speech_ms: int = 400,
        max_utterance_s: float = 30.0,
        on_utterance=None,
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.device = device or None
        self.vad_threshold = vad_threshold
        self.vad_silence_frames = vad_silence_frames
        self.min_speech_ms = min_speech_ms
        self.max_utterance_s = max_utterance_s
        self.on_utterance = on_utterance

        self._blocks: deque[bytes] = deque()
        self._lock = threading.Lock()
        self._stream = None
        self._running = False

        self.speech_detected = False
        self.silence_frames = 0
        self._speech_frames = 0
        self._utterance = bytearray()
        self._started_at = 0.0
        self.stats = CaptureStats()

    # -- properties -------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._running

    @property
    def speaking(self) -> bool:
        return self.speech_detected

    @property
    def level(self) -> float:
        """Latest RMS, for the orb's reaction animation."""
        return self.stats.peak_rms

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if self._running or sd is None:
            return
        frames = max(1, int(self.sample_rate * self.frame_ms / 1000))

        def callback(indata, frames_count, time_info, status):  # noqa: ARG001
            if status:
                logger.debug("input stream status: %s", status)
            with self._lock:
                self._blocks.append(bytes(indata))

        try:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                blocksize=frames,
                device=self.device,
                channels=1,
                dtype="int16",
                callback=callback,
            )
            self._stream.start()
        except Exception as exc:
            logger.error("could not open the microphone: %s", exc)
            self._stream = None
            self._running = False
            raise
        self._reset()
        self._running = True
        logger.info(
            "microphone open: rate=%s device=%s frames=%s", self.sample_rate, self.device, frames
        )

    def stop(self) -> None:
        self._running = False
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as exc:  # pragma: no cover
                logger.debug("error closing input stream: %s", exc)

    def _reset(self) -> None:
        self.speech_detected = False
        self.silence_frames = 0
        self._speech_frames = 0
        self._utterance = bytearray()
        self._started_at = 0.0

    def discard(self) -> None:
        """Throw away the in-flight utterance (used when the user interrupts)."""
        self._reset()

    # -- the VAD ----------------------------------------------------------
    def poll(self) -> Utterance | None:
        """Consume buffered audio and return an utterance when one closes.

        Call frequently (the UI ticks at ~30ms) so latency stays low.
        """
        if not self._running:
            return None
        with self._lock:
            blocks = list(self._blocks)
            self._blocks.clear()

        finished: Utterance | None = None
        for block in blocks:
            if block is None or len(block) == 0:
                continue
            level = rms(block)
            self.stats.frames += 1
            self.stats.peak_rms = level

            if level >= self.vad_threshold:
                if not self.speech_detected:
                    self.speech_detected = True
                    # The threshold crossing is part of the utterance, so
                    # include the frame that proved speech was present.
                    self._utterance = bytearray(block)
                else:
                    self._utterance.extend(block)
                self.silence_frames = 0
                self._speech_frames += 1
                continue

            # Below the threshold: only meaningful once speech has started.
            if self.speech_detected:
                self._utterance.extend(block)
                self.silence_frames += 1

            if should_close_utterance(
                speech_detected=self.speech_detected,
                silence_frames=self.silence_frames,
                threshold_frames=self.vad_silence_frames,
                speech_frames=self._speech_frames,
                min_speech_ms=self.min_speech_ms,
                frame_ms=self.frame_ms,
                duration_s=len(self._utterance) / (self.sample_rate * BYTES_PER_SAMPLE),
                max_utterance_s=self.max_utterance_s,
            ):
                pcm = bytes(self._utterance)
                duration = len(pcm) / (self.sample_rate * BYTES_PER_SAMPLE)
                if self.on_utterance is not None:
                    try:
                        self.on_utterance(Utterance(pcm, duration))
                    except Exception as exc:  # pragma: no cover
                        logger.error("utterance callback failed: %s", exc)
                finished = Utterance(pcm, duration)
                self._reset()

        return finished


class AudioPlayer:
    """Gapless-ish playback of streamed TTS chunks.

    Chunks are pushed with :meth:`play` and drained by a PortAudio callback
    reading from a byte ring, so the next chunk can be enqueued while the
    current one is still playing.
    
    The backend sends MP3 audio chunks (from edge-tts) which are decoded
    to 16-bit PCM on-the-fly before being fed to the audio output stream.
    """

    def __init__(self, *, sample_rate: int = 24000, device: str | None = None) -> None:
        self.sample_rate = sample_rate
        self.device = device or None
        self._pending: deque[bytes] = deque()
        self._lock = threading.Lock()
        self._stream = None
        self._playing = threading.Event()
        self._current = b""
        # MP3 decoder for edge-tts output (which is MP3)
        self._decoder = Mp3Decoder(target_sample_rate=sample_rate, target_channels=1)

    @property
    def playing(self) -> bool:
        return self._playing.is_set()

    def start(self) -> None:
        if self._stream is not None or sd is None:
            return
        block = 1024

        def callback(outdata, frames_count, time_info, status):  # noqa: ARG001
            if status:
                logger.debug("output stream status: %s", status)
            with self._lock:
                if not self._current:
                    self._current = self._pending.popleft() if self._pending else b""
                needed = frames_count * BYTES_PER_SAMPLE
                data = self._current[:needed]
                self._current = self._current[needed:]
                if not data:
                    self._playing.clear()
            # outdata is (frames, channels); with one channel it must be
            # reshaped rather than assigned flat.
            samples = np.frombuffer(data.ljust(needed, b"\x00"), dtype=np.int16)
            outdata[:] = samples.reshape(outdata.shape)
            if not self._pending and not self._current:
                self._playing.clear()

        try:
            self._stream = sd.OutputStream(
                samplerate=self.sample_rate,
                blocksize=block,
                device=self.device,
                channels=1,
                dtype="int16",
                callback=callback,
            )
            self._stream.start()
        except Exception as exc:
            logger.error("could not open the audio output: %s", exc)
            self._stream = None
            raise

    def stop(self) -> None:
        self.clear()
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as exc:  # pragma: no cover
                logger.debug("error closing output stream: %s", exc)

    def play(self, mp3_chunk: bytes) -> None:
        """Queue an MP3 chunk for playback. Decodes to PCM internally."""
        if not mp3_chunk:
            return
        # Decode MP3 to PCM
        pcm = self._decoder.decode(mp3_chunk)
        if pcm:
            with self._lock:
                self._pending.append(pcm)
            self._playing.set()

    def clear(self) -> None:
        """Drop everything queued and start the next response from scratch.

        This is the interrupt path, so it *resets* rather than flushes: a
        flush would emit whatever partial MP3 the interrupted response had
        buffered (audible as a truncated fragment after the user hit stop) and
        would leave the decoder mid-stream, so the next response's MP3 would be
        appended to this one's and both would be corrupt.
        """
        with self._lock:
            self._pending.clear()
            self._current = b""
        self._decoder.reset()
        self._playing.clear()

    def wait_idle(self, timeout: float = 0.05, max_wait: float = 30.0) -> bool:
        """Block until playback drains. Returns False on timeout."""
        import time

        deadline = time.monotonic() + max_wait
        while self._playing.is_set() and time.monotonic() < deadline:
            time.sleep(timeout)
        return not self._playing.is_set()


def list_devices() -> tuple[list[str], list[str]]:
    """Return ``(input_device_names, output_device_names)`` for diagnostics."""
    if sd is None:
        return [], []
    inputs, outputs = [], []
    for index, device in enumerate(sd.query_devices()):
        if device.get("max_input_channels", 0) > 0:
            inputs.append(f"{index}: {device['name']}")
        if device.get("max_output_channels", 0) > 0:
            outputs.append(f"{index}: {device['name']}")
    return inputs, outputs
