"""The turn boundary: an escaping exception must become a report, not silence.

``_handle_message`` is scheduled with ``asyncio.create_task``. In that
position an exception is not surfaced to anyone — no traceback reaches the
console, no frame reaches the client, and the UI sits on "thinking" forever
because the ``done`` that would release it never comes. That is the failure
this boundary exists to prevent.

So three things are asserted here, with the real handler driving a fake
socket:

1. the client gets an ``error`` frame made of a fixed sentence — no exception
   text, no path, no ``Traceback``;
2. a terminal ``done`` still arrives, so the session is usable afterwards;
3. ``REQUEST_DONE`` is logged for every outcome, which is what makes a slow
   turn measurable at all.
"""

import asyncio
import json
import logging

from tests.test_websocket_interrupt import FakeWebSocket, run_handler, _empty_audio
from unittest.mock import AsyncMock, MagicMock


async def make_deps(**overrides):
    """Build the handler's dependencies; ``dispatch`` is the seam under test."""
    state = {"outcome": "ok"}

    async def dispatch(intent, message, context, language, session_id, **kwargs):
        if message == "explode":
            # The shape of a real leak: a secret inside an exception message.
            raise RuntimeError("could not reach https://api.example.com with sk-or-v1-leakedvalue123")
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

    return {
        "agent_service": agent_service,
        "router": router,
        "memory": memory,
        "tts": tts,
        "config": config,
        "state": state,
        **overrides,
    }


async def wait_for(predicate, timeout=2.0):
    """Poll until ``predicate`` holds — turns are scheduled, not awaited."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


class TestAnEscapingExceptionIsReported:
    async def test_the_client_gets_a_sentence_not_an_exception(self, caplog):
        ws = FakeWebSocket([json.dumps({"type": "message", "message": "explode"})])

        with caplog.at_level(logging.INFO, logger="Zaytoon.WebSocket"):
            await run_handler(ws, await make_deps())

        errors = ws.events("error")
        assert errors, [e.get("type") for e in ws.sent]
        content = errors[0]["content"]
        assert content == "Something went wrong. Please try again."
        assert "sk-or-v1-leakedvalue123" not in content
        assert "Traceback" not in content
        assert "api.example.com" not in content
        assert "RuntimeError" not in content

    async def test_a_terminal_frame_still_lands(self):
        """Without it the client waits for a `done` that never comes."""
        ws = FakeWebSocket([json.dumps({"type": "message", "message": "explode"})])
        await run_handler(ws, await make_deps())

        statuses = [e.get("content") for e in ws.events("status")]
        assert "done" in statuses, statuses

    async def test_the_failure_is_logged_with_the_real_type(self, caplog):
        ws = FakeWebSocket([json.dumps({"type": "message", "message": "explode"})])

        with caplog.at_level(logging.INFO, logger="Zaytoon.Reporting"):
            await run_handler(ws, await make_deps())

        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "type=RuntimeError" in logged
        # The log carries the detail — redacted, not omitted.
        assert "sk-or-v1-leakedvalue123" not in logged
        assert "op=turn" in logged

    async def test_the_session_still_answers_the_next_message(self):
        async def next_message_once_reported():
            await wait_for(lambda: bool(ws.events("error")))
            return json.dumps({"type": "message", "message": "hello"})

        ws = FakeWebSocket([
            json.dumps({"type": "message", "message": "explode"}),
            next_message_once_reported,
        ])
        await run_handler(ws, await make_deps())

        contents = [e.get("content") for e in ws.events("message")]
        assert "quick result" in contents, contents


class TestRequestDoneIsAlwaysLogged:
    async def test_a_successful_turn_records_its_duration(self, caplog):
        ws = FakeWebSocket([json.dumps({"type": "message", "message": "hi"})])

        with caplog.at_level(logging.INFO, logger="Zaytoon.WebSocket"):
            await run_handler(ws, await make_deps())

        lines = [r.getMessage() for r in caplog.records if "REQUEST_DONE" in r.getMessage()]
        assert lines, [r.getMessage() for r in caplog.records]
        line = lines[0]
        assert "status=ok" in line
        assert "ms=" in line
        assert "trace=" in line

    async def test_a_failed_turn_records_its_duration(self, caplog):
        ws = FakeWebSocket([json.dumps({"type": "message", "message": "explode"})])

        with caplog.at_level(logging.INFO, logger="Zaytoon.WebSocket"):
            await run_handler(ws, await make_deps())

        lines = [r.getMessage() for r in caplog.records if "REQUEST_DONE" in r.getMessage()]
        assert any("status=error" in line for line in lines), lines

    async def test_the_error_frame_is_traceable(self):
        ws = FakeWebSocket([json.dumps({"type": "message", "message": "explode"})])
        await run_handler(ws, await make_deps())

        errors = ws.events("error")
        assert errors
        # Every frame in this protocol carries the trace id, so a client can
        # correlate the failure with the log line that explains it.
        assert "trace_id" in errors[0]
