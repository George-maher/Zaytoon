"""`reminder_cmd` over the real WebSocket handler.

The handling used to live inline in the receive loop; a refactor replaced
the call site with `_handle_reminder_cmd(...)` and **never wrote the
function**. Every reminder-panel button then raised NameError, fell through
to the loop's catch-all and dropped the socket — pressing "refresh"
disconnected the chat, and `reminder_list` never arrived, so the panel was
permanently empty. Nothing caught it because the client-side test only
asserted the *outbound* frame shape, never the server's answer.

These tests drive the real handler with a scripted socket, so the call site
and its implementation cannot drift apart again.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.websocket import ConnectionManager, create_websocket_handler
from domain.models import TokenBucketRateLimiter
from tests.test_websocket_confirmation import FakeWebSocket, handler_deps


def _service():
    """A reminder service double with the four calls the handler makes."""
    service = MagicMock()
    service.list = AsyncMock(return_value=[
        {
            "reminder_id": 7,
            "task": "stretch my back",
            "scheduled_time": "2026-10-07 16:01:00",
            "status": "pending",
        },
    ])
    service.format_list_for_display = AsyncMock(return_value="1. stretch my back")
    service.delete = AsyncMock(return_value=True)
    service.mark_completed = AsyncMock(return_value=True)
    return service


async def run(ws, reminder_service, deps):
    """The real handler with one collaborator swapped out per test."""
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
        reminder_service=reminder_service,
        config=deps["config"],
    )


def _cmd(action, **extra):
    return json.dumps({"type": "reminder_cmd", "action": action, **extra})


class TestReminderCmdHandler:
    async def test_list_answers_instead_of_dropping_the_socket(
        self, handler_deps
    ):
        """The regression itself: a missing function disconnected the client.

        With `_handle_reminder_cmd` undefined the NameError was swallowed by
        the loop's catch-all, which *disconnects* — so the assertion that
        matters is that a `reminder_list` frame arrives at all.
        """
        service = _service()
        ws = FakeWebSocket([_cmd("list")])

        await run(ws, service, handler_deps)

        listing = await ws.wait_for("reminder_list")
        assert listing["reminders"][0]["task"] == "stretch my back"
        assert listing["display"], "the transcript display line is missing"
        service.list.assert_awaited_once()

    async def test_list_is_not_scoped_to_this_socket(self, handler_deps):
        """`session_id` is attribution, not a partition.

        The socket id changes on every reconnect, so filtering by it hides a
        reminder from every future view of the panel — the exact bug that
        made a freshly-set reminder invisible after a refresh.
        """
        service = _service()
        ws = FakeWebSocket([_cmd("list")])

        await run(ws, service, handler_deps)

        await ws.wait_for("reminder_list")
        assert "session_id" not in service.list.await_args.kwargs

    async def test_delete_success_tells_the_panel_to_remove_the_row(
        self, handler_deps
    ):
        service = _service()
        ws = FakeWebSocket([_cmd("delete", id=7)])

        await run(ws, service, handler_deps)

        frame = await ws.wait_for("reminder_deleted")
        assert frame["id"] == 7
        assert frame["success"] is True
        service.delete.assert_awaited_once_with(7)

    async def test_a_missing_reminder_is_reported_not_claimed(
        self, handler_deps
    ):
        """Never claim an action that did not happen — the client removes
        the row from its panel on success."""
        service = _service()
        service.delete = AsyncMock(return_value=False)
        ws = FakeWebSocket([_cmd("delete", id=404)])

        await run(ws, service, handler_deps)

        frame = await ws.wait_for("reminder_deleted")
        assert frame["success"] is False
        assert "No such reminder" in frame["message"]

    async def test_done_marks_completed(self, handler_deps):
        service = _service()
        ws = FakeWebSocket([_cmd("done", id=7)])

        await run(ws, service, handler_deps)

        frame = await ws.wait_for("reminder_done")
        assert frame["success"] is True
        service.mark_completed.assert_awaited_once_with(7)

    async def test_unknown_action_is_answered_not_fatal(self, handler_deps):
        """A bad command gets an `error` frame the panel can render; the
        connection stays up either way."""
        service = _service()
        ws = FakeWebSocket([_cmd("explode")])

        await run(ws, service, handler_deps)

        frame = await ws.wait_for("error")
        assert "explode" in frame["content"]

    async def test_a_non_numeric_id_is_a_message_not_a_traceback(
        self, handler_deps
    ):
        service = _service()
        ws = FakeWebSocket([_cmd("delete", id="banana")])

        await run(ws, service, handler_deps)

        frame = await ws.wait_for("error")
        assert "number" in frame["content"]
        service.delete.assert_not_awaited()

    async def test_arabic_commands_answer_in_arabic(self, handler_deps):
        service = _service()
        service.delete = AsyncMock(return_value=False)
        ws = FakeWebSocket(
            [_cmd("delete", id=7, language="ar")]
        )

        await run(ws, service, handler_deps)

        frame = await ws.wait_for("reminder_deleted")
        assert frame["success"] is False
        assert any("\u0600" <= ch <= "\u06FF" for ch in frame["message"])
