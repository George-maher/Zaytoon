"""Before/after numbers for the deterministic fast path and the concurrency gate.

Run it: ``python scripts/measure_fast_path.py`` from `backend/`.

Both measurements use a **simulated provider** on purpose. The machine this is
written on has no `ollama` binary and no model, so any number claiming "the local
model took N ms" would be invented; what can be measured honestly is the *cost the
code adds* — model round trips, and the queueing behaviour when more requests
exist than slots. So the stub answers in a fixed `PROVIDER_LATENCY_S`, and every
figure below is expressed in those units as well as in seconds.

What the fast path actually removes: a `get_time`-class request otherwise costs
two generations (the call, then the narration of the result) plus one tool
execution. The tool execution stays; the generations go.
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.engine import AgentEngine  # noqa: E402
from common.concurrency import BoundedGate, QueueTimeout  # noqa: E402
from config import config  # noqa: E402
from tools.base import BaseTool, ToolResult  # noqa: E402
from tools.registry import ToolRegistry  # noqa: E402
from tools.safety import PERMISSION_SAFE  # noqa: E402

#: What one model round trip costs in this simulation. Deliberately in the range a
#: 3B model on laptop hardware occupies, so the *ratio* is meaningful even though
#: the absolute milliseconds belong to this machine and not to yours.
PROVIDER_LATENCY_S = 0.9

SAMPLES = 8

PROMPTS = [
    "what time is it",
    "take a screenshot",
    "system info",
    "open spotify",
]


class _ClockTool(BaseTool):
    """A safe tool whose shape matches the real ones (`get_time`, `screenshot`)."""

    permission = PERMISSION_SAFE

    def __init__(self, name: str):
        super().__init__()
        self._name = name
        self.calls = 0

    def name(self) -> str:
        return self._name

    def description(self) -> str:
        return "Measurement stub."

    def schema(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **params) -> ToolResult:
        self.calls += 1
        await asyncio.sleep(0.004)  # a real tool is not free either
        return ToolResult(success=True, output="09:41:07 (Saturday)")


def _stub_engine(tools: dict):
    registry = ToolRegistry()
    for tool in tools.values():
        registry.register(tool)
    engine = AgentEngine(llm=None, registry=registry, metrics=None)

    state = {"calls": 0}

    async def _ask(messages, *args, **kwargs):
        state["calls"] += 1
        await asyncio.sleep(PROVIDER_LATENCY_S)
        # Alternating, not "first call only". A stub that returns a tool call
        # exactly once makes every later turn a one-generation prose answer and
        # quietly measures the wrong thing — the number this script exists to
        # produce. Odd calls are a turn's opening move (the call), even ones are
        # the narration after the observation, which is what a tool turn costs
        # two generations of.
        if state["calls"] % 2 == 1:
            return '{"action": "get_time", "params": {}}'
        return "It's 09:41:07 (Saturday)."

    engine._ask = _ask  # noqa: SLF001
    engine._measure_state = state  # noqa: SLF001
    return engine


async def measure_fast_path() -> None:
    print("=" * 74)
    print(f"Fast path: {SAMPLES} turns x {len(PROMPTS)} prompts, "
          f"simulated provider latency {PROVIDER_LATENCY_S * 1000:.0f} ms "
          f"(a tool turn = 2 generations: the call, then the narration)")
    print("=" * 74)

    rows = []
    for enabled in (False, True):
        original = config.FAST_PATH_ENABLED
        config.FAST_PATH_ENABLED = enabled
        try:
            tools = {
                name: _ClockTool(name) for name in
                ("get_time", "screenshot", "system_info", "open_app")
            }
            engine = _stub_engine(tools)
            latencies = []
            for _ in range(SAMPLES):
                for prompt in PROMPTS:
                    started = time.perf_counter()
                    result = await engine.run(prompt, language="en")
                    latencies.append(time.perf_counter() - started)
            rows.append({
                "label": "fast path ON" if enabled else "fast path OFF (today)",
                "p50": statistics.median(latencies),
                "p95": sorted(latencies)[int(len(latencies) * 0.95) - 1],
                "calls": engine._measure_state["calls"],  # noqa: SLF001
                "turns": len(latencies),
            })
        finally:
            config.FAST_PATH_ENABLED = original

    for row in rows:
        print(f"  {row['label']:<24} turns={row['turns']:>3}  "
              f"model calls={row['calls']:>3}  "
              f"p50={row['p50'] * 1000:6.0f} ms  p95={row['p95'] * 1000:6.0f} ms")
    if rows[0]["calls"]:
        print(f"\n  model calls avoided: {rows[0]['calls']} -> {rows[1]['calls']} "
              f"({100 * (1 - rows[1]['calls'] / rows[0]['calls']):.0f}% fewer)")
        print(f"  p50 latency:        {rows[0]['p50'] * 1000:.0f} ms -> "
              f"{rows[1]['p50'] * 1000:.0f} ms "
              f"({rows[0]['p50'] / max(rows[1]['p50'], 1e-9):.1f}x faster)")


async def measure_gate() -> None:
    print()
    print("=" * 74)
    print("Concurrency gate: arrivals beyond the limit queue, they do not pile in")
    print("=" * 74)

    for limit in (1, 2, 4):
        for concurrency in (1, 4, 12):
            gate = BoundedGate(limit=limit, queue_timeout=30)
            peak = 0
            live = 0
            started = time.perf_counter()

            async def worker():
                nonlocal peak, live
                async with gate:
                    live += 1
                    peak = max(peak, live)
                    await asyncio.sleep(0.02)
                    live -= 1

            await asyncio.gather(*(worker() for _ in range(concurrency)))
            elapsed = time.perf_counter() - started
            waves = -(-concurrency // limit)
            expected = waves * 0.02
            print(f"  limit={limit}  requests={concurrency:>2}  "
                  f"peak in-flight={peak}  wall={elapsed * 1000:6.0f} ms  "
                  f"(queueing floor {expected * 1000:.0f} ms)")
            assert peak <= limit, f"limit {limit} exceeded: peak {peak}"

    print("\n  refused instead of hung, when the queue never drains:")
    gate = BoundedGate(limit=1, queue_timeout=0.05)
    stuck = asyncio.Event()

    async def holder():
        async with gate:
            await stuck.wait()

    task = asyncio.create_task(holder())
    await asyncio.sleep(0.01)
    refused_in = None
    try:
        started = time.perf_counter()
        async with gate:
            pass  # pragma: no cover - unreachable while the holder is stuck
    except QueueTimeout as exc:
        refused_in = time.perf_counter() - started
        print(f"    QueueTimeout after {refused_in * 1000:.0f} ms: {exc}")
    finally:
        stuck.set()
        await task
    print("    -> the caller gets an explanation instead of waiting forever, and "
          "the slot count stayed honest.")


def main() -> int:
    asyncio.run(measure_fast_path())
    asyncio.run(measure_gate())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
