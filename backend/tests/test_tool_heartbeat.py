"""Increment C — liveness while a tool runs, and numbers after it does.

Two observability gaps, fixed together because they answer the same question
from opposite ends:

* **before**: a tool that takes ten seconds produced no frames at all, so the
  client could not tell "working" from "wedged". A heartbeat emits
  ``tool_progress`` on a fixed cadence until the call ends, and is cancelled in
  the ``finally`` so it can never outlive the work it describes.
* **after**: nothing counted what the tool layer did. ``ToolMetrics`` records
  begin/end/status per call and is read back through ``GET /health``.
"""

import asyncio
import json

import agents.engine as engine_mod
from agents.engine import AgentEngine
from agents.events import AgentEventType, EventBus
from tools.base import BaseTool, ToolResult
from tools.metrics import (
    STATUS_OK,
    ToolMetrics,
    get_tool_metrics,
    record_tool_call,
)
from tools.registry import ToolRegistry


class ScriptedLLM:
    tier = "fast"

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    async def complete(self, messages, **kwargs):
        self.requests.append(list(messages))
        if not self.script:
            return "Nothing left to say."
        return self.script.pop(0)


def call(action, **params):
    return json.dumps({"action": action, **params})


class _Sleepy(BaseTool):
    permission = "safe"

    def __init__(self, name="sleepy", delay=0.0):
        self._name = name
        self.delay = delay
        #: How long this tool's own body took. Recorded so a test can say
        #: "this call was fast" as a fact rather than assuming it.
        self.elapsed = None

    def name(self):
        return self._name

    def description(self):
        return "Takes its time."

    def schema(self):
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        started = asyncio.get_running_loop().time()
        if self.delay:
            await asyncio.sleep(self.delay)
        self.elapsed = asyncio.get_running_loop().time() - started
        return ToolResult.ok(f"{self._name} ran")


class _NeverReturns(BaseTool):
    permission = "safe"

    def name(self):
        return "hang"

    def description(self):
        return "Never returns."

    def schema(self):
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        await asyncio.sleep(3600)


class _Refused(BaseTool):
    """Declares `confirm`, so the gate has something to decline."""

    permission = "confirm"

    def name(self):
        return "refused"

    def description(self):
        return "Needs approval."

    def schema(self):
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        return ToolResult.ok("should not run")


def collector():
    bus = EventBus()
    seen = []
    bus.subscribe(lambda event: seen.append(event))
    return bus, seen


def progress_events(seen, name=None):
    events = [
        e for e in seen if e.type is AgentEventType.TOOL_PROGRESS
    ]
    if name is not None:
        events = [e for e in events if e.data.get("name") == name]
    return events


async def run_one_round(registry, reply, *, bus=None, metrics=None, approval=None):
    llm = ScriptedLLM([reply, "finished"])
    engine = AgentEngine(llm, registry, metrics=metrics)
    result = await engine.run("go", bus=bus, approval=approval)
    return result, llm


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------
class TestHeartbeat:
    async def test_a_slow_tool_says_it_is_still_working(self, monkeypatch):
        monkeypatch.setattr(engine_mod, "TOOL_HEARTBEAT_S", 0.05)
        registry = ToolRegistry()
        registry.register(_Sleepy("sleepy", delay=0.25))
        bus, seen = collector()

        await run_one_round(registry, call("sleepy"), bus=bus)

        beats = progress_events(seen, "sleepy")
        assert len(beats) >= 2, f"expected periodic beats, got {len(beats)}"
        # Fixed, human-written text — never the model's words.
        assert all(e.data["message"] == "Still working…" for e in beats)

    async def test_a_fast_tool_produces_none(self, monkeypatch):
        """A call that never runs long emits no beat.

        The interval here used to be 0.05 s — copied from the *slow*-tool test
        above — which made this assertion a measurement of the host rather than
        of the engine: on a loaded machine the engine's own bookkeeping around
        a zero-delay call took longer than 50 ms, a beat fired for a tool that
        had already returned, and a correct implementation failed. The interval
        is now an order of magnitude above any plausible per-round overhead, and
        the tool's own duration is asserted, so the test still says exactly what
        it claims: this call really was fast, and it produced nothing.
        """
        monkeypatch.setattr(engine_mod, "TOOL_HEARTBEAT_S", 0.5)
        registry = ToolRegistry()
        fast = _Sleepy("sleepy", delay=0.0)
        registry.register(fast)
        bus, seen = collector()

        await run_one_round(registry, call("sleepy"), bus=bus)

        assert fast.elapsed is not None and fast.elapsed < 0.1, (
            f"the probe was not fast ({fast.elapsed}s) — the assertion below "
            f"would not mean anything"
        )
        assert progress_events(seen) == []

    async def test_the_beats_stop_the_moment_the_call_ends(self, monkeypatch):
        """A heartbeat that outlives its tool is a permanent lie on the wire."""
        monkeypatch.setattr(engine_mod, "TOOL_HEARTBEAT_S", 0.05)
        registry = ToolRegistry()
        registry.register(_Sleepy("sleepy", delay=0.15))
        bus, seen = collector()

        await run_one_round(registry, call("sleepy"), bus=bus)
        before = len(progress_events(seen))
        await asyncio.sleep(0.25)
        assert len(progress_events(seen)) == before, "heartbeat task leaked"

    async def test_the_finish_frame_follows_the_beats(self, monkeypatch):
        monkeypatch.setattr(engine_mod, "TOOL_HEARTBEAT_S", 0.05)
        registry = ToolRegistry()
        registry.register(_Sleepy("sleepy", delay=0.15))
        bus, seen = collector()

        await run_one_round(registry, call("sleepy"), bus=bus)

        start = next(
            i for i, e in enumerate(seen)
            if e.type is AgentEventType.TOOL_STARTED
        )
        finish = next(
            i for i, e in enumerate(seen)
            if e.type is AgentEventType.TOOL_FINISHED
        )
        beats = [
            i for i, e in enumerate(seen)
            if e.type is AgentEventType.TOOL_PROGRESS
        ]
        assert beats, "no beats to order"
        assert all(start < i < finish for i in beats), (start, beats, finish)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
class TestToolMetrics:
    async def test_a_successful_call_is_counted(self):
        metrics = ToolMetrics()
        registry = ToolRegistry()
        registry.register(_Sleepy("sleepy"))

        await run_one_round(registry, call("sleepy"), metrics=metrics)

        snapshot = metrics.to_dict()
        assert snapshot["started"] == 1
        assert snapshot["succeeded"] == 1
        assert snapshot["completed"] == 1
        # Nothing left in flight — a stuck increment would show up here.
        assert snapshot["running"] == 0
        tool = snapshot["by_tool"]["sleepy"]
        assert tool["calls"] == 1
        assert tool["completed"] == 1
        assert tool["last_status"] == STATUS_OK

    async def test_a_declined_call_is_counted_separately(self):
        metrics = ToolMetrics()
        registry = ToolRegistry()
        registry.register(_Refused())

        async def decline(name, params):
            return False

        await run_one_round(
            registry, call("refused"), metrics=metrics, approval=decline
        )

        snapshot = metrics.to_dict()
        assert snapshot["declined"] == 1
        assert snapshot["succeeded"] == 0
        assert snapshot["running"] == 0
        assert snapshot["by_tool"]["refused"]["last_status"] == "declined"

    async def test_a_timeout_is_counted_separately(self, monkeypatch):
        monkeypatch.setattr(engine_mod, "TOOL_TIMEOUT_S", 0.05)
        metrics = ToolMetrics()
        registry = ToolRegistry()
        registry.register(_NeverReturns())

        await run_one_round(registry, call("hang"), metrics=metrics)

        snapshot = metrics.to_dict()
        assert snapshot["timed_out"] == 1
        assert snapshot["failed"] == 0
        assert snapshot["running"] == 0

    async def test_concurrent_calls_do_not_leave_the_gauge_stuck(self):
        metrics = ToolMetrics()
        registry = ToolRegistry()
        for i in range(4):
            registry.register(_Sleepy(f"s{i}", delay=0.05))

        await run_one_round(
            registry,
            "\n".join(call(f"s{i}") for i in range(4)),
            metrics=metrics,
        )

        snapshot = metrics.to_dict()
        assert snapshot["started"] == 4
        assert snapshot["succeeded"] == 4
        assert snapshot["running"] == 0
        assert all(t["running"] == 0 for t in snapshot["by_tool"].values())

    async def test_the_default_is_the_process_wide_instance(self):
        engine = AgentEngine(ScriptedLLM([]), ToolRegistry())
        assert engine.metrics is get_tool_metrics()

    def test_recording_never_raises(self):
        class Exploding:
            def end(self, *args, **kwargs):
                raise RuntimeError("metrics exploded")

        # Instrumentation failing must not fail the call it measures.
        record_tool_call(Exploding(), "x", STATUS_OK, 1.0)

    def test_an_unknown_status_falls_back_to_failed(self):
        metrics = ToolMetrics()
        metrics.begin("x")
        metrics.end("x", status="nonsense", duration_ms=5)
        snapshot = metrics.to_dict()
        assert snapshot["failed"] == 1
        assert snapshot["running"] == 0

    def test_zero_completed_calls_report_a_zero_average(self):
        assert ToolMetrics().to_dict()["avg_ms"] == 0.0

    async def test_a_broken_metrics_object_cannot_break_a_call(self):
        """Instrumentation that fails must not fail the thing it measures."""

        class Broken:
            def begin(self, name):
                raise RuntimeError("metrics exploded")

            def end(self, *args, **kwargs):
                raise RuntimeError("metrics exploded")

        registry = ToolRegistry()
        registry.register(_Sleepy("sleepy"))

        result, _ = await run_one_round(
            registry, call("sleepy"), metrics=Broken()
        )

        assert result.tool_calls == ["sleepy"]
        assert result.stopped_reason == "answered"
