"""End-to-end WebSocket tests for the human-in-the-loop confirmation flow.

A fake WebSocket drives the real handler, so this exercises the full path:

    ToolAgent -> ConfirmationManager -> require_confirmation event
              -> client tool_confirm reply -> approval -> execution
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import WebSocketDisconnect

from api.websocket import ConnectionManager, create_websocket_handler, session_states
from domain.models import TokenBucketRateLimiter


class FakeWebSocket:
    """Minimal WebSocket double that records what the server sent.

    When the scripted inbound messages run out, the socket stays connected and
    idles for ``idle_grace`` seconds so background dispatch tasks can finish,
    then raises ``WebSocketDisconnect`` like a real client going away.
    """

    def __init__(self, incoming, idle_grace=1.0):
        # A turn now writes a task record (core.db) before its final frames,
        # so the idle window that lets background dispatches finish had to
        # grow with it — see the same note in test_websocket_interrupt.py.
        self.incoming = list(incoming)
        self.idle_grace = idle_grace
        self.sent: list[dict] = []
        self.client = type("Client", (), {"host": "127.0.0.1"})()
        self._closed = False

    async def accept(self):
        return None

    async def receive_text(self):
        if self.incoming:
            item = self.incoming.pop(0)
            if callable(item):
                return await item()
            return item
        # No more scripted input: idle so in-flight tasks can run, then hang up.
        await asyncio.sleep(self.idle_grace)
        raise WebSocketDisconnect(code=1000)

    async def send_text(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self, code=1000, reason=""):
        self._closed = True
        self._closed_code = code

    # -- test helpers ---------------------------------------------------
    def events(self, event_type):
        return [e for e in self.sent if e.get("type") == event_type]

    async def wait_for(self, event_type, timeout=3.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            found = self.events(event_type)
            if found:
                return found[0]
            await asyncio.sleep(0.01)
        raise AssertionError(
            f"never received '{event_type}'; got {[e.get('type') for e in self.sent]}"
        )


@pytest.fixture
def handler_deps():
    """Real handler collaborators, with the agents stubbed out."""
    tool_agent = MagicMock()

    async def tool_process(message, context, stream_callback=None, **kwargs):
        manager = kwargs.get("confirmation_manager")
        approved = await manager.request(
            "system_command", {"command": "rm -rf /"}, kwargs.get("session_id", "")
        )
        return "executed" if approved else "refused"

    tool_agent.process = tool_process

    agent_service = MagicMock()

    async def dispatch(intent, message, context, language, session_id,
                       stream_callback=None, confirmation_manager=None,
                       event_bus=None, response_id=""):
        # `event_bus` / `response_id` are forwarded so the handler's real
        # progress relay is wired up. The stub deliberately does not emit
        # anything on the bus: these tests are about the confirmation round
        # trip, and `test_websocket_events.py` covers the event mapping.
        return await tool_agent.process(
            message, [], stream_callback=stream_callback, language=language,
            confirmation_manager=confirmation_manager, session_id=session_id,
            event_bus=event_bus, response_id=response_id,
        )

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
    }


def _empty_audio():
    async def gen(*args, **kwargs):
        if False:
            yield b""
    return gen


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


class TestConfirmationRoundTrip:
    async def test_approval_executes_the_tool(self, handler_deps):
        ws = FakeWebSocket([])

        async def approve_when_asked():
            # Wait for the server to publish the request, then reply.
            request = await ws.wait_for("require_confirmation")
            return json.dumps({
                "type": "tool_confirm",
                "request_id": request["request_id"],
                "approved": True,
            })

        ws.incoming = [
            json.dumps({"type": "message", "message": "delete everything"}),
            approve_when_asked,
        ]

        await run_handler(ws, handler_deps)

        request = ws.events("require_confirmation")[0]
        assert request["action"] == "system_command"
        assert request["level"] == "high"
        assert request["reasons"]
        assert request["request_id"]

        results = ws.events("confirmation_result")
        assert results and results[0]["resolved"] is True
        assert results[0]["approved"] is True

        messages = [e for e in ws.events("message") if e.get("content") == "executed"]
        assert messages, [e for e in ws.sent if e.get("type") == "message"]

    async def test_denial_blocks_the_tool(self, handler_deps):
        ws = FakeWebSocket([])

        async def deny_when_asked():
            request = await ws.wait_for("require_confirmation")
            return json.dumps({
                "type": "tool_confirm",
                "request_id": request["request_id"],
                "approved": False,
            })

        ws.incoming = [json.dumps({"type": "message", "message": "nuke it"}),
                       deny_when_asked]

        await run_handler(ws, handler_deps)

        results = ws.events("confirmation_result")
        assert results and results[0]["approved"] is False
        messages = [e for e in ws.events("message") if e.get("content") == "refused"]
        assert messages

    async def test_timeout_denies(self, handler_deps):
        handler_deps["config"].TOOL_CONFIRM_TIMEOUT = 0.05
        ws = FakeWebSocket(
            [json.dumps({"type": "message", "message": "nuke it"})], idle_grace=0.5
        )
        await run_handler(ws, handler_deps)

        assert ws.events("require_confirmation")
        messages = [e for e in ws.events("message") if e.get("content") == "refused"]
        assert messages

    async def test_disconnect_denies_pending(self, handler_deps):
        # Client never replies and the socket drops mid-request. The pending
        # approval must be cancelled and the session torn down, so nothing is
        # left dangling and the tool is never executed.
        handler_deps["config"].TOOL_CONFIRM_TIMEOUT = 30
        ws = FakeWebSocket(
            [json.dumps({"type": "message", "message": "nuke it"})], idle_grace=0.3
        )
        await run_handler(ws, handler_deps)

        assert ws.events("require_confirmation"), "no approval was requested"
        # The session is removed and no result message reaches the dead socket.
        assert not session_states
        assert not [e for e in ws.events("message") if e.get("content") == "executed"]

    async def test_unknown_request_id_is_reported_unresolved(self, handler_deps):
        ws = FakeWebSocket([
            json.dumps({"type": "tool_confirm",
                        "request_id": "cfm_bogus", "approved": True}),
            json.dumps({"type": "ping"}),
        ])
        await run_handler(ws, handler_deps)
        results = ws.events("confirmation_result")
        assert results and results[0]["resolved"] is False
        assert ws.events("pong")

    async def test_session_teardown_removes_state(self, handler_deps):
        ws = FakeWebSocket([json.dumps({"type": "ping"})])
        await run_handler(ws, handler_deps)
        # The handler tears the session down on disconnect.
        assert not session_states

    async def test_ping_pong(self, handler_deps):
        ws = FakeWebSocket([json.dumps({"type": "ping"})])
        await run_handler(ws, handler_deps)
        assert ws.events("pong")
        assert ws.events("status")[0]["content"] == "connected"
