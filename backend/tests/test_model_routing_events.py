"""Model routing events carry the turn they belong to.

`model.completed` is a report about routing, and both clients put it in front of
the user: the browser stamps it on the answer that turn produced, the desktop
shell has the same vocabulary. An event that names a model but not *which turn*
forced every client to guess — "the attempt that arrived last" — and a guess is
the one thing this frame must never be, because a badge on the wrong answer is a
false claim about how the reply was produced.

The join key is the trace id, which the transport binds per turn and which the
event payload now carries. These tests pin that down on both sides of the join:
the client publishes it, and it is the *turn's* id rather than a fresh one.
"""

from __future__ import annotations

import asyncio

import pytest

from common.tracing import reset_trace_id, set_trace_id
from core.events import CoreEventType, bus
from services.llm_client import AttemptRecord, LLMClient


async def _settle() -> None:
    """Let scheduled deliveries run.

    `EventBus.publish` is fire-and-forget: it schedules `emit` on the running
    loop rather than awaiting it, so that a slow socket cannot block the request
    path. Nothing is observable until the loop turns.
    """
    for _ in range(3):
        await asyncio.sleep(0)


class _Recorder:
    """Collects core bus events; the bus is fire-and-forget."""

    def __init__(self):
        self.events = []
        self._remove = None

    async def __call__(self, event):
        self.events.append(event)

    def __enter__(self):
        self._remove = bus.subscribe(self)
        return self

    def __exit__(self, *exc):
        if self._remove:
            self._remove()
        return False

    def of(self, kind):
        return [e for e in self.events if e.type == kind]


def _record(outcome="ok", **kwargs) -> AttemptRecord:
    base = dict(
        label="primary",
        provider="openrouter",
        model="qwen/qwen3-30b-a3b:free",
        outcome=outcome,
        latency_ms=812,
        attempt=2,
        request_id="req_1",
    )
    base.update(kwargs)
    return AttemptRecord(**base)


@pytest.fixture
def client():
    return LLMClient()


class TestModelEventsCarryTheTurn:
    async def test_a_completed_attempt_names_the_turn_it_served(self, client):
        with _Recorder() as seen:
            token = set_trace_id("trace_turn_1")
            try:
                client._emit_model_event(_record())
            finally:
                reset_trace_id(token)
            await _settle()

        [event] = seen.of(CoreEventType.MODEL_COMPLETED)
        assert event.data["trace_id"] == "trace_turn_1"
        # The routing facts are unchanged; the trace is additive.
        assert event.data["model"] == "qwen/qwen3-30b-a3b:free"
        assert event.data["provider"] == "openrouter"
        assert event.data["attempt"] == 2
        assert event.data["latency_ms"] == 812
        assert event.data["request_id"] == "req_1"

    async def test_a_failed_attempt_names_it_too(self, client):
        """A fallback chain's failures are what explain a slow, odd answer."""
        with _Recorder() as seen:
            token = set_trace_id("trace_turn_2")
            try:
                client._emit_model_event(_record(outcome="rate_limited"))
            finally:
                reset_trace_id(token)
            await _settle()

        [event] = seen.of(CoreEventType.MODEL_FAILED)
        assert event.data["trace_id"] == "trace_turn_2"
        assert event.data["outcome"] == "rate_limited"

    async def test_outside_a_turn_the_trace_is_empty_not_invented(self, client):
        """A REST call or a health probe belongs to no turn.

        Empty rather than a fresh id or a reused one: a client must be able to
        tell "this attempt is not part of the answer on screen" from "this
        attempt is part of it", and reusing the last turn's trace would put the
        badge on the wrong message.
        """
        with _Recorder() as seen:
            client._emit_model_event(_record())
            await _settle()

        [event] = seen.of(CoreEventType.MODEL_COMPLETED)
        assert event.data["trace_id"] == ""

    async def test_the_payload_still_carries_no_content(self, client):
        """The rule the trace field must not have broken.

        `record.detail` can hold a response body, so it is deliberately absent
        from the event. A field added later is exactly how that rule erodes, so
        it is asserted rather than assumed.
        """
        with _Recorder() as seen:
            client._emit_model_event(
                _record(detail="HTTP 500: {'error': 'secret-looking body'}")
            )
            await _settle()

        [event] = seen.of(CoreEventType.MODEL_COMPLETED)
        assert "detail" not in event.data
        assert "secret-looking body" not in str(event.data)

    async def test_two_turns_are_distinguishable(self, client):
        with _Recorder() as seen:
            for trace in ("trace_a", "trace_b"):
                token = set_trace_id(trace)
                try:
                    client._emit_model_event(_record(request_id=f"req_{trace}"))
                finally:
                    reset_trace_id(token)
                await _settle()

        traces = [e.data["trace_id"] for e in seen.of(CoreEventType.MODEL_COMPLETED)]
        assert traces == ["trace_a", "trace_b"]
