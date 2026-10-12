"""Probe: does a spoken Arabic utterance ever reach the UI as Arabic?

Drives the *real* WebSocket handler with an ``audio`` frame the way the real
browser client sends it — including the ``language`` hint it always attaches —
and reports which frames came back.

The question being asked: the frontend's only path to ``setLanguage("ar")`` is
the ``stt_result`` frame, and ``setLanguage`` is what flips the whole app to RTL
and the TTS voice to Egyptian Arabic. If the frame never arrives when the client
supplied a hint, spoken Arabic can never switch the app to Arabic.

    cd backend && python scripts/probe_voice_language.py
"""

import asyncio
import base64
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.websocket import (  # noqa: E402
    ConnectionManager,
    create_websocket_handler,
    session_states,
)
from domain.models import TokenBucketRateLimiter  # noqa: E402
from voice.stt import TranscriptionResult  # noqa: E402


class FakeWebSocket:
    def __init__(self, incoming, idle_grace=0.6):
        self.incoming = list(incoming)
        self.idle_grace = idle_grace
        self.sent = []
        self.client = type("Client", (), {"host": "127.0.0.1"})()

    async def accept(self):
        return None

    async def receive_text(self):
        if self.incoming:
            return self.incoming.pop(0)
        await asyncio.sleep(self.idle_grace)
        from fastapi import WebSocketDisconnect

        raise WebSocketDisconnect(code=1000)

    async def send_text(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self, code=1000, reason=""):
        return None

    def types(self):
        return [frame.get("type") for frame in self.sent]


def _deps(captured):
    agent_service = MagicMock()

    async def dispatch(intent, message, context, language, session_id, **kwargs):
        captured["language"] = language
        return "تم"

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
        if False:
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


async def run(hint):
    """One turn. ``hint`` is the ``language`` the client attaches to the frame."""
    captured = {}
    agent_service, router, memory, tts, config = _deps(captured)

    # What Whisper returns for Egyptian Arabic speech. `language` is re-derived
    # from the transcript in every case; `detected` is exactly
    # `not <the hint the client sent>`, which is what `transcribe_detailed`
    # computes.
    stt = MagicMock(loading=False, available=True)
    stt.transcribe_detailed = MagicMock(
        return_value=TranscriptionResult(
            text="عامل إيه يا زيتون",
            language="ar",
            detected=(hint is None),
            duration=1.4,
            voice_hint="ar-EG-ShakirNeural",
        )
    )

    frame = {"type": "audio", "audio": base64.b64encode(b"x" * 400).decode()}
    if hint is not None:
        frame["language"] = hint
    frame["isManualVoice"] = True

    ws = FakeWebSocket([json.dumps(frame)])
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


async def main():
    ok = True
    for hint in (None, "en", "ar"):
        ws, captured = await run(hint)
        reported = [f for f in ws.sent if f.get("type") == "stt_result"]
        print(f"client hint={hint!r}")
        print(f"  frames      : {ws.types()}")
        print(f"  stt_result  : {reported}")
        print(f"  agent lang  : {captured.get('language')!r}")
        if hint is not None and not reported:
            print("  -> DEFECT: Whisper said 'ar' and the UI was never told.")
            ok = False
        print()
    print("RESULT:", "PASS" if ok else "DEFECT REPRODUCED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
