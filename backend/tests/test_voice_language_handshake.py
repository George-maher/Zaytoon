"""The STT language handshake reaches the client on every audio turn.

The defect these tests exist for: `TranscriptionResult.detected` is False
whenever the *caller* supplied a language hint — it records whether a hint was
sent, not whether the language was inferred. The WebSocket handler read it as
"the language is trustworthy" and gated its `stt_result` frame on it. The
browser client attaches its current `language` to every audio frame, so
`detected` was always False in production and the frame was never sent at all.

`stt_result` is the only frame that reaches `setLanguage()` on the client, and
`setLanguage` is what flips the application to RTL and the read-aloud voice to
Egyptian Arabic. So the gate made it impossible for a spoken Egyptian-Arabic
request to switch the app to Arabic — the reported "voice does not work"
symptom, with no error anywhere to explain it.

These drive the real handler with a real `audio` frame, because the bug lived
in the seam between the transport and the STT result rather than in either one:
`transcribe_detailed` was correct, and the client's `stt_result` case was
correct, and the wire between them was dead.
"""

from __future__ import annotations

import asyncio
import base64
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import WebSocketDisconnect

from api.websocket import (
    ConnectionManager,
    create_websocket_handler,
    session_states,
)
from domain.models import TokenBucketRateLimiter
from voice.stt import TranscriptionResult


class FakeWebSocket:
    """Scripted inbound frames; everything sent is recorded."""

    def __init__(self, incoming, idle_grace=0.6):
        self.incoming = list(incoming)
        self.idle_grace = idle_grace
        self.sent: list[dict] = []
        self.client = type("Client", (), {"host": "127.0.0.1"})()

    async def accept(self):
        return None

    async def receive_text(self):
        if self.incoming:
            return self.incoming.pop(0)
        await asyncio.sleep(self.idle_grace)
        raise WebSocketDisconnect(code=1000)

    async def send_text(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self, code=1000, reason=""):
        return None

    def events(self, event_type):
        return [e for e in self.sent if e.get("type") == event_type]


def _deps(captured: dict):
    agent_service = MagicMock()

    async def dispatch(intent, message, context, language, session_id, **kwargs):
        captured["language"] = language
        captured["message"] = message
        return "تمام"

    agent_service.dispatch = dispatch

    router = MagicMock()
    router.route = AsyncMock(return_value={"intent": "chat"})

    memory = MagicMock()
    memory.store_message = AsyncMock()
    memory.get_recent_context = AsyncMock(return_value=[])
    memory.summarize_and_prune = AsyncMock()

    tts = MagicMock()
    tts.split_sentences = lambda text: [text]

    async def no_audio(*_a, **_k):
        if False:  # pragma: no cover - empty async generator
            yield b""

    tts.stream_synthesize = no_audio

    config = MagicMock()
    config.MAX_MESSAGE_LENGTH = 8000
    config.TTS_STREAM_EARLY = False
    config.TTS_RATE = "+0%"
    config.TTS_VOLUME = "+0%"
    config.TOOL_CONFIRM_TIMEOUT = 5.0
    config.WAKE_SESSION_TIMEOUT = 10.0
    config.WAKE_WORD_REQUIRED = True
    config.WAKE_WORD_HINT = True
    return agent_service, router, memory, tts, config


def _audio_frame(hint):
    """Exactly the frame the browser client builds (`useWebSocket.js`)."""
    frame = {
        "type": "audio",
        "audio": base64.b64encode(b"x" * 400).decode(),
        "isManualVoice": True,
    }
    if hint is not None:
        frame["language"] = hint
    return json.dumps(frame)


async def _run_audio_turn(*, hint, stt_result):
    """Drive one microphone turn through the real handler."""
    captured: dict = {}
    agent_service, router, memory, tts, config = _deps(captured)
    stt = MagicMock(loading=False, available=True)
    stt.transcribe_detailed = MagicMock(return_value=stt_result)

    ws = FakeWebSocket([_audio_frame(hint)])
    await create_websocket_handler(
        websocket=ws,
        rate_limiter=TokenBucketRateLimiter(max_requests=100, window=60),
        connection_manager=ConnectionManager(),
        stt=stt,
        tts=tts,
        memory_agent=memory,
        short_term_memory=MagicMock(),
        agent_service=agent_service,
        router_agent=router,
        scheduler=MagicMock(),
        reminder_service=MagicMock(),
        config=config,
    )
    session_states.clear()
    return ws, captured


ARABIC = TranscriptionResult(
    text="عامل إيه يا زيتون",
    language="ar",
    detected=False,  # the client sent a hint, so this is False by definition
    duration=1.4,
    voice_hint="ar-EG-ShakirNeural",
)


class TestSttLanguageReachesTheClient:
    @pytest.mark.parametrize("hint", ["en", "ar", "ar-EG"])
    async def test_spoken_arabic_is_reported_when_the_client_sent_a_hint(self, hint):
        """The regression. Every one of these hints produced no frame before."""
        ws, _ = await _run_audio_turn(hint=hint, stt_result=ARABIC)

        reported = ws.events("stt_result")
        assert reported, (
            "Whisper's language never reached the client, so `setLanguage()` "
            f"could not run and the UI stayed in the old language. hint={hint!r}, "
            f"frames={[e.get('type') for e in ws.sent]}"
        )
        assert reported[0]["language"] == "ar"
        assert reported[0]["voice"] == "ar-EG-ShakirNeural"
        assert reported[0]["duration"] == 1.4

    async def test_no_hint_still_reports_the_language(self):
        """The path that did work, kept working.

        Without a hint ``detected`` is True, which is the only combination the
        old gate accepted — so this is the case that hid the bug.
        """
        auto = TranscriptionResult(
            text=ARABIC.text, language="ar", detected=True,
            duration=1.4, voice_hint="ar-EG-ShakirNeural",
        )
        ws, _ = await _run_audio_turn(hint=None, stt_result=auto)
        reported = ws.events("stt_result")
        assert reported and reported[0]["language"] == "ar"

    async def test_english_speech_after_arabic_switches_back(self):
        """A hint must not pin the language either: it follows the audio."""
        english = TranscriptionResult(
            text="what time is it", language="en", detected=False,
            duration=1.0, voice_hint="en-GB-RyanNeural",
        )
        ws, _ = await _run_audio_turn(hint="ar", stt_result=english)
        reported = ws.events("stt_result")
        assert reported and reported[0]["language"] == "en"

    async def test_the_turn_uses_the_language_that_was_spoken(self):
        """The agent is told the spoken language, not the client's guess."""
        _, captured = await _run_audio_turn(hint="en", stt_result=ARABIC)
        assert captured["language"] == "ar", (
            "the turn ran in the client's hint language, so the reply and the "
            "read-aloud voice would both have been English"
        )

    async def test_empty_transcript_still_sends_nothing_and_returns_to_listening(self):
        """Silence must not produce a language frame or a turn."""
        empty = TranscriptionResult(text="", language="ar", detected=False)
        ws, captured = await _run_audio_turn(hint="en", stt_result=empty)

        assert ws.events("stt_result") == []
        assert "language" not in captured
        assert any(
            e.get("type") == "status" and e.get("content") == "listening"
            for e in ws.sent
        ), [e for e in ws.sent if e.get("type") == "status"]


class TestDetectedFlagIsNotAConfidenceScore:
    """Pin the meaning of the field the transport used to misread."""

    def test_detected_is_false_when_a_hint_was_supplied(self, monkeypatch):
        from voice.stt import SpeechToText

        stt = SpeechToText()
        monkeypatch.setattr(stt, "_load_model", lambda: True)
        stt._loading = False
        # A model double that reports Arabic while the caller hinted English.
        class _Info:
            language = "ar"
            duration = 1.0

        class _Model:
            def transcribe(self, path, **kwargs):
                return iter([]), _Info()

        stt._model = _Model()
        result = stt.transcribe_detailed(b"RIFF" + b"\x00" * 500, language="en")

        assert result.detected is False, (
            "a hint was supplied, so `detected` must be False — it describes "
            "the request, not the language"
        )
        assert result.language == "ar", (
            "the language must still come from the audio, so the transport can "
            "report it without consulting `detected`"
        )
