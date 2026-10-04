"""The STOP path: ``interrupt`` must cancel the in-flight turn.

The web UI's STOP button and the desktop shell's interrupt hotkey both send
``{"type": "interrupt"}``. This drives the real handler with a dispatch stub
that blocks like a real turn, so the assertions are about observable wire
behaviour — an ``interrupted`` ack, a genuinely cancelled task, no final
``message`` frame for the cancelled turn, and a session that still works
afterwards.

The handler side had no coverage at all: the desktop tests assert the client
* sends* the frame, and nothing asserted the server does anything with it.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.websocket import ConnectionManager, create_websocket_handler
from domain.models import TokenBucketRateLimiter


class FakeWebSocket:
    """Minimal WebSocket double that records what the server sent."""

    def __init__(self, incoming, idle_grace=0.3):
        self.incoming = list(incoming)
        self.idle_grace = idle_grace
        self.sent: list[dict] = []
        self.client = type("Client", (), {"host": "127.0.0.1"})()

    async def accept(self):
        return None

    async def receive_text(self):
        if self.incoming:
            item = self.incoming.pop(0)
            if callable(item):
                return await item()
            return item
        await asyncio.sleep(self.idle_grace)
        from fastapi import WebSocketDisconnect

        raise WebSocketDisconnect(code=1000)

    async def send_text(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self, code=1000, reason=""):
        return None

    # -- test helpers ---------------------------------------------------
    def events(self, event_type):
        return [e for e in self.sent if e.get("type") == event_type]


def _empty_audio():
    async def gen(*args, **kwargs):
        if False:
            yield b""

    return gen


@pytest.fixture
def turn():
    """Dispatch stub + the signals to sequence a turn around it.

    ``long job`` blocks like a real CPU-bound turn and records whether it was
    cancelled; anything else returns immediately, so a *second* turn can prove
    the session survives an interrupt.
    """
    state = {
        "started": asyncio.Event(),
        "cancelled": asyncio.Event(),
        "completed": False,
    }

    async def dispatch(intent, message, context, language, session_id, **kwargs):
        if message == "long job":
            state["started"].set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                state["cancelled"].set()
                raise
            state["completed"] = True
            return "long job done"  # must never reach the wire
        return "quick result"

    agent_service = MagicMock()
    agent_service.dispatch = dispatch

    router = MagicMock()
    router.route = AsyncMock(return_value={"intent": "action"})

    memory = MagicMock()
    memory.store_message = AsyncMock()
    memory.get_recent_context = AsyncMock(return_value=[])
    memory.summarize_and_prune = AsyncMock()

    tts = MagicMock()
    tts.split_sentences = lambda text: [text]
    tts.stream_synthesize = _empty_audio()

    config = MagicMock()
    config.MAX_MESSAGE_LENGTH = 8000
    config.TTS_STREAM_EARLY = False
    config.TTS_RATE = "+0%"
    config.TTS_VOLUME = "+0%"
    config.TOOL_CONFIRM_TIMEOUT = 5.0
    config.WAKE_SESSION_TIMEOUT = 10.0

    deps = {
        "agent_service": agent_service,
        "router": router,
        "memory": memory,
        "tts": tts,
        "config": config,
        "state": state,
    }
    return deps


async def run_handler(ws, deps):
    await create_websocket_handler(
        websocket=ws,
        rate_limiter=TokenBucketRateLimiter(max_requests=100, window=60),
        connection_manager=ConnectionManager(),
        stt=MagicMock(loading=False, available=False),
        tts=deps["tts"],
        memory_agent=deps["memory"],
        short_term_memory=MagicMock(),
        agent_service=deps["agent_service"],
        router_agent=deps["router"],
        scheduler=MagicMock(),
        reminder_service=MagicMock(),
        config=deps["config"],
    )


class TestInterruptCancelsTheTurn:
    async def test_interrupt_cancels_an_in_flight_turn(self, turn):
        state = turn["state"]

        async def interrupt_once_started():
            await asyncio.wait_for(state["started"].wait(), 2.0)
            return json.dumps({"type": "interrupt"})

        ws = FakeWebSocket([
            json.dumps({"type": "message", "message": "long job"}),
            interrupt_once_started,
        ])
        await run_handler(ws, turn)

        # The ack the STOP button's UI resets on.
        assert ws.events("interrupted"), [
            e.get("type") for e in ws.sent
        ]
        # The task was really cancelled — not left running in the background.
        assert state["cancelled"].is_set(), "dispatch kept running after interrupt"
        assert state["completed"] is False
        # And the interrupted turn produced no deliverable answer.
        contents = [e.get("content") for e in ws.events("message")]
        assert "long job done" not in contents

    async def test_interrupt_without_an_active_turn_is_harmless(self, turn):
        # Pressing STOP while idle must ack, not crash and not wedge the
        # session: users hit the button whenever they feel like it.
        ws = FakeWebSocket([json.dumps({"type": "interrupt"})])
        await run_handler(ws, turn)
        assert ws.events("interrupted")

    async def test_the_session_works_after_an_interrupt(self, turn):
        # STOP is not a disconnect: the next message must still be answered.
        state = turn["state"]

        async def interrupt_once_started():
            await asyncio.wait_for(state["started"].wait(), 2.0)
            return json.dumps({"type": "interrupt"})

        async def next_turn_once_cancelled():
            await asyncio.wait_for(state["cancelled"].wait(), 2.0)
            return json.dumps({"type": "message", "message": "quick one"})

        ws = FakeWebSocket([
            json.dumps({"type": "message", "message": "long job"}),
            interrupt_once_started,
            next_turn_once_cancelled,
        ])
        await run_handler(ws, turn)

        assert ws.events("interrupted")
        contents = [e.get("content") for e in ws.events("message")]
        assert "quick result" in contents, contents
        assert "long job done" not in contents


class TestMalformedFramesDoNotEndTheSession:
    """The server owes its clients the tolerance it asks of them.

    Both clients already survive a bad frame from the server — the browser's
    ``onmessage`` swallows an unparseable frame and the desktop's
    ``ServerEvent.parse`` returns ``None``. The server did not reciprocate:
    ``json.loads`` sat *outside* the ``while True``, so one non-JSON line fell
    through to the outer ``except`` and ended the receive loop. The socket
    closed, the in-flight turn was cancelled by ``finally``, and the user's
    next message went nowhere with no error on screen.

    A bad frame is reported and skipped; the session keeps working.
    """

    async def test_a_malformed_frame_is_reported_and_skipped(self, turn):
        ws = FakeWebSocket([
            "THIS IS NOT JSON {{{",
            json.dumps({"type": "message", "message": "quick one"}),
        ])
        await run_handler(ws, turn)

        errors = [e.get("content") for e in ws.events("error")]
        assert errors, "a malformed frame must be reported, not dropped silently"
        # A fixed sentence — the frame itself is client data and must not be
        # echoed back as if it were ours.
        assert errors == ["Malformed message ignored."]
        contents = [e.get("content") for e in ws.events("message")]
        assert "quick result" in contents, "the session must still answer"

    async def test_a_non_object_frame_is_tolerated(self, turn):
        """JSON that parses to a scalar has no ``type`` to read — previously an
        ``AttributeError`` on ``.get``, which killed the loop the same way."""
        ws = FakeWebSocket([
            '"just a string"',
            json.dumps({"type": "message", "message": "quick one"}),
        ])
        await run_handler(ws, turn)

        contents = [e.get("content") for e in ws.events("message")]
        assert "quick result" in contents, contents

    async def test_a_malformed_frame_mid_turn_does_not_cancel_it(self, turn):
        """The frame arrives while a turn is in flight; the turn must finish.

        This is the shape that made the bug expensive: the session did not just
        lose the next message, it lost the answer already being produced. The
        receive loop's exit path cancels the current task, so leaving the loop
        early cancelled work that had nothing to do with the bad frame."""
        finished = {"done": False}

        async def dispatch(intent, message, context, language, session_id, **kwargs):
            await asyncio.sleep(0.15)  # long enough to be in flight, not 30s
            finished["done"] = True
            return "slow but finite"

        agent_service = MagicMock()
        agent_service.dispatch = dispatch
        turn["agent_service"] = agent_service

        async def bad_frame_once_started():
            # Arrive while dispatch is sleeping, i.e. mid-turn.
            await asyncio.sleep(0.03)
            return "not json at all"

        ws = FakeWebSocket([
            json.dumps({"type": "message", "message": "slow one"}),
            bad_frame_once_started,
        ])
        await run_handler(ws, turn)

        errors = [e.get("content") for e in ws.events("error")]
        assert errors == ["Malformed message ignored."]
        assert finished["done"] is True, "the in-flight turn was cancelled"
        contents = [e.get("content") for e in ws.events("message")]
        assert "slow but finite" in contents, contents
