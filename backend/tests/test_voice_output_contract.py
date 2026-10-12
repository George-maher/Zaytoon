"""Two defects in the voice output path, both invisible until production.

**The streaming path accepted an empty prosody value.** `stream_synthesize`
resolved its rate and volume with two steps (argument, then config) where
`synthesize` used three (argument, config, then the locale profile). Those look
equivalent until `TTS_RATE=` appears in a `.env`: `os.getenv("TTS_RATE",
"+0%")` returns `""` for a variable that is present but empty, `"" or ""` is
`""`, and edge-tts then renders `<prosody rate="">` — malformed SSML, no audio,
and a "speaking" state with nothing behind it. Streaming is the default path,
so the branch with the weaker resolution was the one that actually ran.

**Per-frame tracing was logged at WARNING.** Every outbound frame produced a
WARNING line, and a streamed reply emits one `audio_chunk` per TTS chunk, so a
single spoken answer wrote hundreds of WARNINGs — on the responsiveness path,
while burying the warnings that mean something. The inbound line additionally
carried the first 80 characters of whatever the client sent, which for an
`audio` frame is base64 of the user's own recording.
"""

from __future__ import annotations

import logging
import sys
import types

import pytest

from voice.tts import TextToSpeech


class _FakeCommunicate:
    """Captures the prosody it was constructed with, then yields one chunk."""

    last: dict = {}

    def __init__(self, text, voice=None, rate=None, volume=None, **kwargs):
        type(self).last = {"text": text, "voice": voice, "rate": rate, "volume": volume}

    async def stream(self):
        yield {"type": "audio", "data": b"ID3fake"}


@pytest.fixture
def fake_edge_tts(monkeypatch):
    module = types.ModuleType("edge_tts")
    module.Communicate = _FakeCommunicate
    module.list_voices = lambda: []
    _FakeCommunicate.last = {}
    monkeypatch.setitem(sys.modules, "edge_tts", module)
    return _FakeCommunicate


@pytest.fixture
def tts():
    instance = TextToSpeech()
    instance._available = True
    return instance


class TestStreamingProsodyNeverEmpty:
    async def test_empty_config_rate_falls_back_to_the_profile(
        self, tts, fake_edge_tts, monkeypatch
    ):
        """`TTS_RATE=` (present but empty) must not become `rate=""`."""
        from config import config

        monkeypatch.setattr(config, "TTS_RATE", "", raising=False)
        monkeypatch.setattr(config, "TTS_VOLUME", "", raising=False)

        chunks = [c async for c in tts.stream_synthesize("hello there")]

        assert chunks == [b"ID3fake"], "no audio was produced for an empty rate"
        assert fake_edge_tts.last["rate"] == "+0%"
        assert fake_edge_tts.last["volume"] == "+0%"

    async def test_an_explicit_rate_still_wins(self, tts, fake_edge_tts, monkeypatch):
        from config import config

        monkeypatch.setattr(config, "TTS_RATE", "+0%", raising=False)
        [c async for c in tts.stream_synthesize("hello", rate="+40%")]
        assert fake_edge_tts.last["rate"] == "+40%"

    async def test_synthesize_and_stream_agree_on_prosody(
        self, tts, fake_edge_tts, monkeypatch
    ):
        """The two entry points must not resolve the same inputs differently."""
        from config import config

        monkeypatch.setattr(config, "TTS_RATE", "", raising=False)
        monkeypatch.setattr(config, "TTS_VOLUME", "", raising=False)

        [c async for c in tts.stream_synthesize("hello there")]
        streamed = (fake_edge_tts.last["rate"], fake_edge_tts.last["volume"])

        tts._cache.clear()
        await tts.synthesize("hello there")
        one_shot = (fake_edge_tts.last["rate"], fake_edge_tts.last["volume"])

        assert streamed == one_shot, (
            "the streaming and one-shot paths disagreed on prosody, so whether "
            "audio works depended on which TTS mode was configured"
        )


class TestPerFrameTracingIsNotAWarning:
    async def test_an_outbound_frame_logs_below_warning(self, caplog):
        from api.websocket import ConnectionManager

        class _Socket:
            async def send_text(self, raw):
                return None

        manager = ConnectionManager()
        with caplog.at_level(logging.DEBUG, logger="Zaytoon.WebSocket"):
            await manager._send(_Socket(), {"type": "audio_chunk", "data": "AAAA"})

        records = [r for r in caplog.records if "WS >>" in r.getMessage()]
        assert records, "the frame trace disappeared entirely"
        assert max(r.levelno for r in records) < logging.WARNING, (
            "an outgoing frame is logged at WARNING; a streamed reply emits one "
            "audio_chunk per TTS chunk, so this is hundreds of WARNINGs per "
            "spoken answer"
        )

    async def test_one_audio_chunk_cannot_produce_a_warning(self, caplog):
        """The volumetric version: the level, over many frames."""
        from api.websocket import ConnectionManager

        class _Socket:
            async def send_text(self, raw):
                return None

        manager = ConnectionManager()
        with caplog.at_level(logging.DEBUG, logger="Zaytoon.WebSocket"):
            for index in range(50):
                await manager._send(
                    _Socket(), {"type": "audio_chunk", "data": "A" * 512, "index": index}
                )

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == [], [r.getMessage() for r in warnings]
