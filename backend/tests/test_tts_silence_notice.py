"""One honest notice when speech produces nothing — and only then.

`stream_synthesize` swallows its errors so a dead voice can never eat a reply
that already streamed as text. That same swallowing makes the silence
invisible to the user, so `_stream_tts_chunks` reads `tts.last_stream_error`
and names the problem: once per session, re-armed by a success, silent for an
interrupt, and never carrying the raw exception text.

The `isinstance(reason, str)` gate in the transport is pinned here too: the
flow tests drive it with a MagicMock tts, whose auto-generated attribute is a
Mock — truthy, but not a recorded failure.
"""

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.websocket import _TTS_SILENT_SESSIONS, _stream_tts_chunks  # noqa: E402


class FakeWebSocket:
    pass


class FakeManager:
    """Records every frame the transport would have sent."""

    def __init__(self):
        self.sent = []

    async def send(self, websocket, frame):
        self.sent.append(frame)

    def frames_of(self, kind):
        return [f for f in self.sent if f["type"] == kind]


def make_tts(failing: bool):
    tts = SimpleNamespace(
        resolve_voice=lambda language: "en-GB-RyanNeural",
        split_sentences=lambda text: [text],
        last_stream_error=None,
    )

    async def stream(text, voice=None, language=None, rate=None, volume=None):
        if failing:
            tts.last_stream_error = "WSServerHandshakeError"
            return
        tts.last_stream_error = None
        yield b"\x00\x01"

    tts.stream_synthesize = stream
    return tts


async def run_once(
    tts, websocket=None, manager=None, language="en", cancel_event=None
):
    manager = manager or FakeManager()
    websocket = websocket or FakeWebSocket()
    config = SimpleNamespace(TTS_RATE="+0%", TTS_VOLUME="+0%")
    await _stream_tts_chunks(
        websocket,
        "hello there",
        language,
        tts,
        manager,
        cancel_event=cancel_event,
        config=config,
    )
    return manager, websocket


@pytest.fixture(autouse=True)
def _isolated_sessions():
    """Socket identity is only unique among live objects; keep the registry
    per-test so an id() reuse can never leak a 'silent' flag between tests."""
    _TTS_SILENT_SESSIONS.clear()
    yield
    _TTS_SILENT_SESSIONS.clear()


async def test_a_recorded_failure_produces_exactly_one_notice():
    tts = make_tts(failing=True)
    manager, _ = await run_once(tts)

    notices = manager.frames_of("error")
    assert len(notices) == 1, notices
    # The fixed human-written sentence — the recorded reason never crosses.
    assert "voice service" in notices[0]["content"]
    assert "WSServerHandshakeError" not in notices[0]["content"]
    # The turn still completed its audio envelope, so the client unlocks.
    assert manager.frames_of("audio_start")
    assert manager.frames_of("audio_end")


async def test_the_notice_is_arabic_when_the_turn_is():
    tts = make_tts(failing=True)
    manager, _ = await run_once(tts, language="ar")

    notices = manager.frames_of("error")
    assert len(notices) == 1
    assert "\u0627\u0644\u0631\u062f" in notices[0]["content"]  # الرد


async def test_the_notice_is_once_per_session_not_per_sentence():
    tts = make_tts(failing=True)
    websocket = FakeWebSocket()
    manager = FakeManager()

    await run_once(tts, websocket=websocket, manager=manager)  # first sentence
    await run_once(tts, websocket=websocket, manager=manager)  # next sentence

    assert len(manager.frames_of("error")) == 1


async def test_a_success_re_arms_the_notice():
    tts = make_tts(failing=True)
    websocket = FakeWebSocket()
    manager = FakeManager()
    await run_once(tts, websocket=websocket, manager=manager)
    assert len(manager.frames_of("error")) == 1

    await run_once(make_tts(failing=False), websocket=websocket, manager=manager)
    await run_once(tts, websocket=websocket, manager=manager)

    assert len(manager.frames_of("error")) == 2


async def test_an_interrupted_turn_says_nothing():
    """The user stopped it; a notice about a stop they chose would be noise."""
    cancel = asyncio.Event()
    tts = make_tts(failing=True)

    async def stream_and_interrupt(text, **kwargs):
        tts.last_stream_error = "WSServerHandshakeError"
        cancel.set()  # the interrupt lands while synthesising
        return  # pragma: no cover - makes this an async generator
        yield b""

    tts.stream_synthesize = stream_and_interrupt
    manager, _ = await run_once(tts, cancel_event=cancel)

    assert manager.frames_of("audio_end")  # the envelope still closes
    assert manager.frames_of("error") == []


async def test_a_mock_tts_with_no_recorded_reason_says_nothing():
    """The flow tests' MagicMock reports a Mock attribute, not a str — the
    gate must read that as 'no recorded failure' or their frame assertions
    would grow a surprise error frame."""
    tts = MagicMock()
    tts.split_sentences = lambda text: [text]

    async def silent_stream(text, **kwargs):
        return  # pragma: no cover - makes this an async generator
        yield b""

    tts.stream_synthesize = silent_stream
    manager, _ = await run_once(tts)

    assert manager.frames_of("error") == []
