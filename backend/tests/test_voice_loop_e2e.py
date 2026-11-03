"""End-to-end voice loop with TTS failure and recovery.

Covers the full pipeline: STT transcript -> conversation -> TTS synthesis
failure -> honest unavailability notice -> TTS retry -> success -> availability
restored. Uses a fake edge-tts so no network to Microsoft's service is needed.

The TTS failure case is the production reality in this sandbox (edge-tts
cannot reach speech.platform.bing.com:443). These tests prove the system
responds honestly: TTS reports not-available, the UI gets a notice, and a
retry can restore service when the network path returns.
"""

from __future__ import annotations

import asyncio
import sys
import types

import edge_tts
import pytest

from voice.tts import TextToSpeech


class _FakeCommunicate:
    """Stand-in for edge_tts.Communicate. Modeled by the test per case."""

    def __init__(self, text, voice=None, rate=None, volume=None, **kwargs):
        self.text = text
        self.voice = voice
        self.rate = rate
        self.volume = volume

    async def stream(self):
        if False:  # pragma: no cover - makes this an async generator
            yield b""


class _Worker(_FakeCommunicate):
    async def stream(self):
        yield {"type": "audio", "data": b"\x00\x01"}


class _Refuser(_FakeCommunicate):
    async def stream(self):
        raise RuntimeError("WSServerHandshakeError")
        yield b""  # pragma: no cover - makes this an async generator


class _Silent(_FakeCommunicate):
    """Runs to completion but yields no audio (service returns empty body)."""

    async def stream(self):
        return  # pragma: no cover - makes this an async generator
        yield b""


def _make_tts():
    instance = TextToSpeech()
    instance._available = True
    instance.clear_cache()
    return instance


# -- Availability tracks real synthesis -------------------------------------


async def test_tts_starts_unavailable_untested(monkeypatch):
    """Fresh instance: package installed, service untested -> not available."""
    monkeypatch.setattr(edge_tts, "Communicate", _Worker)
    tts = _make_tts()
    assert tts.available is False
    assert tts._last_synthesis_succeeded is None


async def test_tts_success_marks_available(monkeypatch):
    monkeypatch.setattr(edge_tts, "Communicate", _Worker)
    tts = _make_tts()
    result = await tts.synthesize("hello")
    assert result == b"\x00\x01"
    assert tts.available is True
    assert tts._last_synthesis_succeeded is True


async def test_tts_failure_marks_unavailable(monkeypatch):
    monkeypatch.setattr(edge_tts, "Communicate", _Refuser)
    tts = _make_tts()
    result = await tts.synthesize("hello")
    assert result is None
    assert tts.available is False
    assert tts._last_synthesis_succeeded is False


async def test_tts_silent_return_marks_unavailable(monkeypatch):
    """Service runs but yields no audio -> also a failure."""
    monkeypatch.setattr(edge_tts, "Communicate", _Silent)
    tts = _make_tts()
    result = await tts.synthesize("hello")
    assert result is None
    assert tts.available is False


async def test_tts_recovery_after_failure(monkeypatch):
    """A failure followed by a success restores availability."""
    monkeypatch.setattr(edge_tts, "Communicate", _Refuser)
    tts = _make_tts()
    await tts.synthesize("hello")
    assert tts.available is False

    monkeypatch.setattr(edge_tts, "Communicate", _Worker)
    result = await tts.synthesize("hello again")
    assert result == b"\x00\x01"
    assert tts.available is True
    assert tts.last_stream_error is None


async def test_streaming_failure_marks_unavailable(monkeypatch):
    monkeypatch.setattr(edge_tts, "Communicate", _Refuser)
    tts = _make_tts()
    chunks = [c async for c in tts.stream_synthesize("hello")]
    assert chunks == []
    assert tts.available is False
    assert tts.last_stream_error == "RuntimeError"


async def test_streaming_success_marks_available(monkeypatch):
    monkeypatch.setattr(edge_tts, "Communicate", _Worker)
    tts = _make_tts()
    chunks = [c async for c in tts.stream_synthesize("hello")]
    assert chunks == [b"\x00\x01"]
    assert tts.available is True


# -- Voice loop integration: TTS silence notice path -----------------------


class _FakeSocket:
    def __init__(self):
        self.sent = []

    async def send_text(self, raw):
        import json

        self.sent.append(json.loads(raw))


class _FakeManager:
    def __init__(self):
        self.sent = []

    async def send(self, websocket, frame):
        self.sent.append(frame)


async def test_voice_loop_tts_failure_sends_notice_once(monkeypatch):
    """Full loop: TTS fails -> one honest notice -> audio envelope still closes."""
    monkeypatch.setattr(edge_tts, "Communicate", _Refuser)

    from api.websocket import _TTS_SILENT_SESSIONS, _stream_tts_chunks

    _TTS_SILENT_SESSIONS.clear()
    try:
        tts = _make_tts()
        tts._available = True
        socket = _FakeSocket()
        manager = _FakeManager()
        config = type("C", (), {"TTS_RATE": "+0%", "TTS_VOLUME": "+0%"})()

        await _stream_tts_chunks(
            socket,
            "hello there",
            "en",
            tts,
            manager,
            config=config,
        )

        # TTS marked unavailable.
        assert tts.available is False

        # One honest error notice (fixed sentence, no exception detail).
        errors = [f for f in manager.sent if f["type"] == "error"]
        assert len(errors) == 1
        assert "voice service" in errors[0]["content"]
        assert "WSServerHandshakeError" not in errors[0]["content"]

        # Audio envelope still closes so the client unlocks.
        assert any(f["type"] == "audio_start" for f in manager.sent)
        assert any(f["type"] == "audio_end" for f in manager.sent)

        # Second sentence: notice not repeated (once per session).
        manager.sent = []
        await _stream_tts_chunks(
            socket, "another sentence", "en", tts, manager, config=config
        )
        errors = [f for f in manager.sent if f["type"] == "error"]
        assert len(errors) == 0
    finally:
        _TTS_SILENT_SESSIONS.clear()


async def test_voice_loop_tts_success_rearms_notice(monkeypatch):
    """After a failure, a success re-arms the one-shot notice."""
    from api.websocket import _TTS_SILENT_SESSIONS, _stream_tts_chunks

    _TTS_SILENT_SESSIONS.clear()
    try:
        tts = _make_tts()
        tts._available = True
        socket = _FakeSocket()
        manager = _FakeManager()
        config = type("C", (), {"TTS_RATE": "+0%", "TTS_VOLUME": "+0%"})()

        # Failure first.
        monkeypatch.setattr(edge_tts, "Communicate", _Refuser)
        await _stream_tts_chunks(socket, "hello", "en", tts, manager, config=config)
        errors = [f for f in manager.sent if f["type"] == "error"]
        assert len(errors) == 1
        assert tts.available is False

        # Success restores availability.
        monkeypatch.setattr(edge_tts, "Communicate", _Worker)
        manager.sent = []
        await _stream_tts_chunks(socket, "hello again", "en", tts, manager, config=config)
        assert tts.available is True
        errors = [f for f in manager.sent if f["type"] == "error"]
        assert len(errors) == 0
    finally:
        _TTS_SILENT_SESSIONS.clear()


# -- /tts/retry endpoint logic ---------------------------------------------


class _RetryApp:
    """Exercise the /tts/retry handler logic without FastAPI."""

    def __init__(self, tts):
        self.tts = tts

    async def retry(self):
        from config import config as app_config

        try:
            audio = await asyncio.wait_for(
                self.tts.synthesize(
                    "Voice check.",
                    voice=self.tts.resolve_voice("en"),
                    rate=app_config.TTS_RATE,
                    volume=app_config.TTS_VOLUME,
                ),
                timeout=60.0,
            )
        except asyncio.TimeoutError:
            return {
                "ready": False,
                "reason": "synthesis did not finish within 60s",
                "last_stream_error": getattr(self.tts, "last_stream_error", None),
            }
        if audio:
            return {"ready": True, "reason": "", "last_stream_error": None}
        return {
            "ready": False,
            "reason": "synthesis returned no audio",
            "last_stream_error": getattr(self.tts, "last_stream_error", None),
        }


async def test_tts_retry_endpoint_success(monkeypatch):
    monkeypatch.setattr(edge_tts, "Communicate", _Worker)
    tts = _make_tts()
    app = _RetryApp(tts)

    # Initially unavailable (untested).
    assert tts.available is False

    result = await app.retry()
    assert result == {"ready": True, "reason": "", "last_stream_error": None}
    assert tts.available is True


async def test_tts_retry_endpoint_failure(monkeypatch):
    monkeypatch.setattr(edge_tts, "Communicate", _Refuser)
    tts = _make_tts()
    app = _RetryApp(tts)

    result = await app.retry()
    assert result["ready"] is False
    assert result["last_stream_error"] == "RuntimeError"
    assert tts.available is False
