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

import pytest

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


class TestTheTurnReachesTheTaskSystem:
    """The whole point of the transport wiring: a live turn is a task.

    Before the orchestrator owned the turn sequence, ``api/websocket.py``
    executed turns with no task record at all — the task system could not see
    live work, so ``/tasks`` showed nothing while the user was being answered.
    """

    @pytest.fixture
    def turn_task_db(self, monkeypatch, tmp_path):
        """A private core.db, so record identity is unambiguous.

        Top-level tests otherwise write rows into the developer's real
        ``data/core.db`` (the suite's historical default); an assertion like
        "a record exists for this message" must not be satisfiable by an older
        row from another run.
        """
        monkeypatch.setenv("CORE_DB_PATH", str(tmp_path / "core.db"))

    async def test_a_live_turn_writes_a_completed_task_record(self, turn_task_db):
        message = "a lineage probe only this test ever sends"
        ws = FakeWebSocket([json.dumps({"type": "message", "message": message})])

        await run_handler(ws, await make_deps())

        from core.tasks import get_manager
        from core.tasks.models import TaskStatus

        # The record is written at turn start and completed at turn end; poll
        # so the assertion does not depend on the loop's scheduling.
        completed = None
        deadline = asyncio.get_running_loop().time() + 3.0
        while asyncio.get_running_loop().time() < deadline:
            records = await get_manager().list(limit=200)
            mine = [
                t for t in records
                if message in (t.request or "") and t.status is TaskStatus.COMPLETED
            ]
            if mine:
                completed = mine[0]
                break
            await asyncio.sleep(0.02)

        assert completed is not None, (
            "the transport's turn never produced a completed task record"
        )
        assert completed.result == "quick result"
        assert completed.intent == "action"
        # And the frame side still worked: the wiring did not swallow the reply.
        contents = [e.get("content") for e in ws.events("message")]
        assert "quick result" in contents, contents
