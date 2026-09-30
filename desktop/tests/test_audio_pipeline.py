"""Regression tests for the TTS decode/playback path.

These exist because the previous suite passed 78/78 while
``desktop/app/audio.py`` was **unimportable** — it had a ``from __future__``
statement after other imports, so the whole desktop app raised SyntaxError on
startup and nothing noticed, because no test imported the module. Every test
here therefore begins by importing the real module, and the decode assertions
run against a genuine edge-tts recording rather than a stub, for the same
reason: a double can make a broken thing look fine.

The fixture is real edge-tts output ("Hello, this is Zaytoon."), 24000 Hz
mono MP3, which is exactly what the backend streams.
"""

from __future__ import annotations

import array
import struct
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The import itself is an assertion. If this line moves below a docstring-free
# `from __future__` mistake again, the collection error fails the suite.
from app import audio as A  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "edge_tts_sample.mp3"
RATE = 24000


def _pcm_stats(pcm: bytes) -> dict:
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    peak = max(abs(s) for s in samples)
    rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
    quiet = sum(1 for s in samples if abs(s) < 64) / len(samples)
    return {
        "peak": peak,
        "rms": rms,
        "crest": peak / rms if rms else 0.0,
        "quiet": quiet,
        "duration": len(pcm) / 2 / RATE,
    }


@pytest.fixture(scope="module")
def mp3() -> bytes:
    assert FIXTURE.exists(), f"missing audio fixture: {FIXTURE}"
    return FIXTURE.read_bytes()


class TestModuleImports:
    """The gap that let a SyntaxError reach a 'passing' suite."""

    def test_audio_module_is_importable(self):
        assert hasattr(A, "AudioPlayer")
        assert hasattr(A, "MicrophoneCapture")

    def test_a_decoder_backend_exists(self):
        assert A.Mp3Decoder().available, (
            "no MP3 decoder importable; TTS would be completely silent"
        )


class TestMp3Decode:
    def test_whole_file_decodes_to_pcm(self, mp3):
        dec = A.Mp3Decoder(RATE, 1)
        pcm = dec.decode(mp3) + dec.flush()
        assert pcm, "decoded to zero bytes -- playback would be silence"
        assert len(pcm) % 2 == 0, "PCM must be whole 16-bit samples"
        assert len(pcm) / 2 / RATE > 1.0, "a 1s+ phrase should yield over a second"

    def test_output_is_speech_not_static(self, mp3):
        """The original bug was noise, so amplitude shape is the real assertion.

        White or pink static has a crest factor near 1.0-1.8 because every
        sample is comparable in magnitude. Voiced speech has peaks well above
        its RMS and a substantial fraction of near-silence between words.
        """
        dec = A.Mp3Decoder(RATE, 1)
        stats = _pcm_stats(dec.decode(mp3) + dec.flush())
        assert stats["peak"] > 8000, f"output too quiet to be speech: {stats}"
        assert 2.0 < stats["crest"] < 14.0, f"crest {stats['crest']:.1f} is not speech"
        assert 0.10 < stats["quiet"] < 0.90, f"silence ratio {stats['quiet']:.0%} is not speech"

    def test_energy_sits_in_the_speech_band(self, mp3):
        dec = A.Mp3Decoder(RATE, 1)
        pcm = dec.decode(mp3) + dec.flush()
        x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        win = np.hanning(len(x))
        spec = np.abs(np.fft.rfft(x * win))
        freqs = np.fft.rfftfreq(len(x), 1.0 / RATE)
        band = spec[(freqs >= 300) & (freqs < 4000)].sum() / spec.sum()
        assert band > 0.4, f"only {band:.0%} of energy is in 300Hz-4kHz; not speech-like"

    @pytest.mark.parametrize("chunk_size", [1, 7, 997, 4096, 15552])
    def test_arbitrary_chunk_boundaries_are_equivalent(self, mp3, chunk_size):
        """One WebSocket message is NOT one audio frame.

        MP3 frames share a backwards-pointing bit reservoir, so a frame decoded
        without its history yields the wrong samples. Decoding chunk-by-chunk
        must produce the same audio as decoding the whole file, otherwise
        streaming is clicks and gaps while the whole-file path sounds fine.
        """
        dec = A.Mp3Decoder(RATE, 1)
        out = bytearray()
        for i in range(0, len(mp3), chunk_size):
            out += dec.decode(mp3[i : i + chunk_size])
        out += dec.flush()

        whole = A.Mp3Decoder(RATE, 1)
        reference = whole.decode(mp3) + whole.flush()

        stats = _pcm_stats(bytes(out))
        ref = _pcm_stats(reference)
        assert abs(len(out) - len(reference)) <= 4096, (
            f"chunk={chunk_size} produced {len(out)} bytes vs {len(reference)} whole-file"
        )
        assert stats["crest"] > 2.0, f"chunk={chunk_size} crest {stats['crest']:.1f} -> clicks"
        assert abs(stats["rms"] - ref["rms"]) < ref["rms"] * 0.25, (
            f"chunk={chunk_size} RMS {stats['rms']:.0f} vs {ref['rms']:.0f}: amplitude collapsed"
        )

    def test_no_audio_is_dropped_across_chunks(self, mp3):
        dec = A.Mp3Decoder(RATE, 1)
        out = bytearray()
        for i in range(0, len(mp3), 4096):
            out += dec.decode(mp3[i : i + 4096])
        out += dec.flush()
        # Every sample should be delivered exactly once: no gaps (too short)
        # and no duplication (too long).
        whole = A.Mp3Decoder(RATE, 1)
        reference = whole.decode(mp3) + whole.flush()
        assert abs(len(out) - len(reference)) <= 4096


class TestInterrupt:
    def test_clear_discards_buffered_audio(self, mp3):
        player = A.AudioPlayer(sample_rate=RATE)
        player.play(mp3[:3000])
        player.clear()
        assert not player._pending, "queued audio survived the interrupt"
        assert player._current == b"", "partially played audio survived"
        assert not player.playing

    def test_reset_makes_the_next_response_independent(self, mp3):
        """A's bytes must not be prepended to B's, or both are corrupt."""
        dec = A.Mp3Decoder(RATE, 1)
        dec.decode(mp3[:3000])       # A, interrupted mid-stream
        dec.reset()
        pcm = dec.decode(mp3)        # B
        stats = _pcm_stats(pcm + dec.flush())
        assert abs(stats["duration"] - 2.6) < 0.4, (
            f"B came out {stats['duration']:.2f}s; A's partial stream leaked in"
        )
        assert stats["crest"] > 2.0

    def test_flush_after_reset_yields_nothing(self, mp3):
        dec = A.Mp3Decoder(RATE, 1)
        dec.decode(mp3)
        dec.reset()
        assert dec.flush() == b"", "flushing a reset decoder replayed old audio"


class TestPlayerWritesPcmToDevice:
    """Drive the production callback; only libportaudio is substituted."""

    def _player_with_recorder(self, player, rate=RATE):
        import types

        captured = {}

        class Recorder:
            def __init__(self):
                self.written = bytearray()
                self.callback = None
                self.closed = False

            def start(self):
                pass

            def stop(self):
                pass

            def close(self):
                self.closed = True

            def pump(self, frames=1024):
                out = np.zeros((frames, 1), dtype=np.int16)
                self.callback(out, frames, None, None)
                self.written.extend(out.astype("<i2").tobytes())

        rec = Recorder()
        captured["rec"] = rec
        real = A.sd
        A.sd = types.SimpleNamespace(OutputStream=lambda **kw: (setattr(rec, "callback", kw["callback"]), rec)[1])
        try:
            player._stream = None
            player.start()
        finally:
            A.sd = real
        assert player._stream is rec
        return rec

    def test_device_receives_speech_shaped_pcm(self, mp3):
        player = A.AudioPlayer(sample_rate=RATE)
        rec = self._player_with_recorder(player)
        player.play(mp3)
        for _ in range(400):
            rec.pump()
            if not player.playing:
                break
        stats = _pcm_stats(bytes(rec.written))
        assert stats["duration"] > 1.5
        assert stats["peak"] > 8000
        assert 2.0 < stats["crest"] < 14.0, "device received noise, not speech"

    def test_streamed_chunks_reach_the_device(self, mp3):
        player = A.AudioPlayer(sample_rate=RATE)
        rec = self._player_with_recorder(player)
        for i in range(0, len(mp3), 4096):
            player.play(mp3[i : i + 4096])
        player._decoder.flush()
        for _ in range(400):
            rec.pump()
            if not player.playing:
                break
        stats = _pcm_stats(bytes(rec.written))
        assert stats["duration"] > 1.5, "streamed audio never reached the device"
        assert stats["crest"] > 2.0

    def test_output_stream_is_opened_once_and_reused(self, mp3):
        """A stable device is the point of the ring buffer.

        Opening/closing the output per chunk is what produces clicks, so the
        stream must survive across chunks.
        """
        player = A.AudioPlayer(sample_rate=RATE)
        rec = self._player_with_recorder(player)
        for i in range(0, len(mp3), 4096):
            player.play(mp3[i : i + 4096])
        assert player._stream is rec, "the device was reopened mid-response"
        assert not rec.closed

    def test_interrupt_while_speaking_silences_the_device(self, mp3):
        player = A.AudioPlayer(sample_rate=RATE)
        rec = self._player_with_recorder(player)
        player.play(mp3[:3000])
        player.clear()
        before = len(rec.written)
        for _ in range(30):
            rec.pump()
        # Only zero-padding may be written after the interrupt.
        tail = bytes(rec.written[before:])
        assert set(struct.unpack(f"<{len(tail)//2}h", tail)) <= {0}
        assert not player.playing
