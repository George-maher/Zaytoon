"""Increment B — how one round's tool calls are executed.

The engine used to run every call in a batch strictly one after another. Three
safe reads in a request therefore cost three reads' worth of wall time, which
is the difference between an answer and a spinner on a local model.

Two properties have to survive that change, and both are load-bearing:

* **Order.** The observations handed back to the model are folded in the order
  the model wrote the calls, so a `create_folder` that precedes a `file_read`
  must actually have finished first. Running everything concurrently would let
  a read overtake the folder it is supposed to see — the model would then
  reason about a sequence that never happened.
* **The gate.** A confirm/dangerous call is a barrier that runs alone, and it
  still goes through approval before it runs.

So: consecutive safe calls run together under a cap; every gated call runs on
its own, in place.
"""

import asyncio
import json

from agents.engine import PARALLEL_SAFE_TOOLS, AgentEngine
from agents.events import EventBus
from tools.base import BaseTool, ToolResult
from tools.registry import ToolRegistry


class ScriptedLLM:
    tier = "fast"

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.calls = 0

    async def complete(self, messages, **kwargs):
        self.requests.append(list(messages))
        self.calls += 1
        if not self.script:
            return "Nothing left to say."
        return self.script.pop(0)


def call(action, **params):
    return json.dumps({"action": action, **params})


class _Probe(BaseTool):
    """A tool that reports when it started and finished.

    ``events`` is shared by every probe in a batch, so the *interleaving* is
    the assertion — not just which ones ran.
    """

    def __init__(self, name, events, tier="safe", delay=0.0, fail=False):
        self._name = name
        self.events = events
        self.permission = tier
        self.delay = delay
        self.fail = fail
        self.runs = 0

    def name(self):
        return self._name

    def description(self):
        return "Records its own execution window."

    def schema(self):
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        self.events.append(("start", self._name))
        self.runs += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        self.events.append(("end", self._name))
        if self.fail:
            return ToolResult.fail("it did not work")
        return ToolResult.ok(f"{self._name} ran")


class _ConcurrencyProbe(BaseTool):
    """Counts how many siblings are in flight at once."""

    permission = "safe"

    def __init__(self, name, counter, delay=0.1):
        self._name = name
        self.counter = counter
        self.delay = delay

    def name(self):
        return self._name

    def description(self):
        return "Holds the door open briefly."

    def schema(self):
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        self.counter["active"] += 1
        self.counter["peak"] = max(self.counter["peak"], self.counter["active"])
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.counter["active"] -= 1
        return ToolResult.ok(f"{self._name} ran")


def build(*tools):
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def _grant(name, params):
    """An approval channel that approves — a channel must exist to proceed."""
    return True


async def run_round(registry, first_reply, approval=None):
    """Run exactly one tool round; returns (result, llm).

    The channel defaults to one that grants. A confirm-tier call with **no**
    channel is refused by design (see
    ``test_agent_engine.py::TestLoopApproval::test_no_approval_callback_means_a_blocked_tool_is_refused``),
    so a test that wants a gated call to be a *barrier* has to satisfy the gate
    it is asserting rather than sidestep it.
    """
    llm = ScriptedLLM([first_reply, "finished"])
    result = await AgentEngine(llm, registry).run(
        "do the thing", approval=approval or _grant
    )
    return result, llm


# ---------------------------------------------------------------------------
class TestSafeCallsRunTogether:
    async def test_three_safe_calls_cost_one_calls_worth_of_time(self):
        """Three safe calls overlap; the wall time proves it *and* so does the order.

        The original form of this test asserted an absolute `elapsed < 0.45`
        for three 0.2 s sleeps. That is a claim about the machine as much as
        about the engine: on a loaded box the same correct batch measured
        0.47 s and failed, which is a test reporting its own host's load. Two
        changes make it honest without weakening it.

        * **The interleaving is the assertion.** Every probe appends to one
          shared list, so "they ran together" is literally `start, start,
          start, end, end, end`. That is deterministic, needs no clock, and is
          a stronger statement than a stopwatch: a fast sequential run could
          beat a wall-clock bound, but it can never produce that sequence.
        * **The timing is calibrated, not guessed.** The baseline is the same
          three probes with the same delay run through the *same engine* as
          confirm-tier calls, which are barriers by design — so both numbers
          carry the same per-round overhead and only the difference is being
          judged. Overhead cancels; a loaded machine moves both figures.
        """
        # Baseline: identical work, forced sequential by the tier gate.
        sequential_events = []
        baseline_tools = [
            _Probe(f"s{i}", sequential_events, tier="confirm", delay=0.2)
            for i in range(3)
        ]
        sequential_started = asyncio.get_running_loop().time()
        await run_round(
            build(*baseline_tools),
            "\n".join(call(t.name()) for t in baseline_tools),
        )
        sequential = asyncio.get_running_loop().time() - sequential_started
        # Sanity: the baseline really is sequential, or the comparison is void.
        assert sequential_events == [
            ("start", "s0"), ("end", "s0"),
            ("start", "s1"), ("end", "s1"),
            ("start", "s2"), ("end", "s2"),
        ]

        events = []
        tools = [_Probe(f"p{i}", events, delay=0.2) for i in range(3)]
        registry = build(*tools)

        started = asyncio.get_running_loop().time()
        result, _ = await run_round(
            registry, "\n".join(call(t.name()) for t in tools)
        )
        elapsed = asyncio.get_running_loop().time() - started

        assert result.tool_calls == ["p0", "p1", "p2"]
        # Deterministic proof of overlap: no probe finished before its siblings
        # started, so the three windows are one window.
        assert events == [
            ("start", "p0"), ("start", "p1"), ("start", "p2"),
            ("end", "p0"), ("end", "p1"), ("end", "p2"),
        ], f"probes did not overlap: {events}"

        # Calibrated timing: three 0.2 s sleeps overlapped must save roughly two
        # of them (0.4 s). A sequential batch saves none, so the margin is wide
        # on both sides and does not depend on how fast this host is today.
        saved = sequential - elapsed
        assert saved > 0.25, (
            f"batch saved only {saved:.2f}s of {sequential:.2f}s — "
            f"elapsed {elapsed:.2f}s looks sequential"
        )

    async def test_the_number_of_simultaneous_tools_is_capped(self):
        counter = {"active": 0, "peak": 0}
        tools = [_ConcurrencyProbe(f"c{i}", counter) for i in range(6)]
        registry = build(*tools)

        result, _ = await run_round(
            registry, "\n".join(call(t.name()) for t in tools)
        )

        assert sorted(result.tool_calls) == sorted(t.name() for t in tools)
        # Parallel, but bounded: a model that emits twenty reads must not open
        # twenty subprocesses at once.
        assert 2 <= counter["peak"] <= PARALLEL_SAFE_TOOLS


class TestOrderingIsPreserved:
    async def test_a_gated_call_is_a_barrier(self):
        """Nothing after a confirm/dangerous call starts until it finishes."""
        events = []
        slow = _Probe("safe_first", events, delay=0.1)
        gated = _Probe("gated_second", events, tier="confirm")
        after = _Probe("safe_third", events)
        registry = build(slow, gated, after)

        result, _ = await run_round(
            registry,
            "\n".join([call("safe_first"), call("gated_second"), call("safe_third")]),
        )

        assert result.tool_calls == ["safe_first", "gated_second", "safe_third"]
        assert events == [
            ("start", "safe_first"), ("end", "safe_first"),
            ("start", "gated_second"), ("end", "gated_second"),
            ("start", "safe_third"), ("end", "safe_third"),
        ]

    async def test_a_later_fast_call_cannot_outrun_an_earlier_slow_one(self):
        """The fold claims an order; execution has to back that claim up."""
        events = []
        slow = _Probe("slow_one", events, delay=0.15)
        fast = _Probe("fast_one", events)
        registry = build(slow, fast)

        result, llm = await run_round(
            registry, "\n".join([call("slow_one"), call("fast_one")])
        )

        assert result.tool_calls == ["slow_one", "fast_one"]
        # The observations the model reads back are in request order too.
        blob = json.dumps(llm.requests[1])
        assert blob.index("slow_one ran") < blob.index("fast_one ran")

    async def test_declined_calls_keep_their_position(self):
        events = []
        gated = _Probe("gated", events, tier="confirm")
        safe = _Probe("safe", events)
        registry = build(gated, safe)

        async def decline(name, params):
            return False

        llm = ScriptedLLM([
            "\n".join([call("gated"), call("safe")]),
            "understood",
        ])
        result = await AgentEngine(llm, registry).run(
            "do both", approval=decline
        )

        # The refusal is fed back to the model as an observation, and the safe
        # call still ran.
        blob = json.dumps(llm.requests[1])
        assert "did not approve" in blob
        assert "safe ran" in blob
        assert gated.runs == 0
        assert safe.runs == 1
        # A declined call never became a dispatched tool.
        assert result.tool_calls == ["safe"]


class TestOneFailureNeverBlocksTheOthers:
    async def test_a_failing_sibling_still_reports_its_successes(self):
        events = []
        bad = _Probe("bad", events, fail=True)
        good = _Probe("good", events)
        registry = build(bad, good)

        result, llm = await run_round(
            registry, "\n".join([call("bad"), call("good")])
        )

        assert result.tool_calls == ["bad", "good"]
        assert result.failed_calls == ["bad"]
        assert result.errors
        blob = json.dumps(llm.requests[1])
        assert "it did not work" in blob
        assert "good ran" in blob

    async def test_an_unknown_tool_does_not_stop_the_batch(self):
        events = []
        good = _Probe("good", events)
        registry = build(good)

        result, llm = await run_round(
            registry, "\n".join([call("no_such_tool"), call("good")])
        )

        assert "good" in result.tool_calls
        assert good.runs == 1
        assert "Unknown tool" in json.dumps(llm.requests[1])


class TestTheProgressChannelStillReportsLive:
    async def test_start_and_finish_frames_are_paired_per_tool(self):
        events = []
        tools = [_Probe(f"p{i}", events, delay=0.05) for i in range(3)]
        registry = build(*tools)

        bus = EventBus()
        seen = []
        bus.subscribe(lambda event: seen.append(event))

        llm = ScriptedLLM([
            "\n".join(call(t.name()) for t in tools),
            "finished",
        ])
        await AgentEngine(llm, registry).run("do the thing", bus=bus)

        from agents.events import AgentEventType

        starts = [e for e in seen if e.type is AgentEventType.TOOL_STARTED]
        finishes = [e for e in seen if e.type is AgentEventType.TOOL_FINISHED]
        assert {e.data["name"] for e in starts} == {"p0", "p1", "p2"}
        assert {e.data["name"] for e in finishes} == {"p0", "p1", "p2"}
        for name in ("p0", "p1", "p2"):
            first_start = next(
                i for i, e in enumerate(seen)
                if e.type is AgentEventType.TOOL_STARTED and e.data["name"] == name
            )
            matching_end = next(
                i for i, e in enumerate(seen)
                if e.type is AgentEventType.TOOL_FINISHED and e.data["name"] == name
            )
            assert first_start < matching_end, name
