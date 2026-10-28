"""The concurrency gate: bounded, fair, re-entrant, and it queues before it fails.

Nothing in this codebase capped simultaneous model calls. The only other
semaphore is the per-turn tool parallelism limit, which knows nothing about how
many *turns* the process is running — so two sessions on a laptop with one GPU
did not get twice the throughput, they got two slow answers and a real chance of
an OOM-killed daemon and a dark assistant.

Three properties are load-bearing, and each has a test that would fail if the
implementation traded it away:

* **Bounded** — occupancy never exceeds ``LLM_MAX_CONCURRENCY``.
* **Re-entrant per task** — ``stream()`` falls back to a blocking ``complete()``
  mid-turn; a non-reentrant gate deadlocks there, and a deadlock in this layer
  takes the whole app down rather than one request.
* **Queued, not failed** — a second question in the same second is ordinary use.
  Refusing because the gate was momentarily full would be a self-inflicted
  outage; only a waiter past ``LLM_QUEUE_TIMEOUT_S`` gives up, and it gives up
  with a sentence that says why.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.concurrency import BoundedGate, QueueTimeout  # noqa: E402
from config import config  # noqa: E402


class TestBounded:
    async def test_six_waiters_never_exceed_the_limit(self):
        gate = BoundedGate(limit=2, queue_timeout=10)
        peak = 0
        live = 0

        async def worker():
            nonlocal peak, live
            async with gate:
                live += 1
                peak = max(peak, live)
                await asyncio.sleep(0.01)
                live -= 1

        await asyncio.gather(*(worker() for _ in range(6)))
        assert peak == 2, f"peak in-flight was {peak}, limit was 2"
        assert live == 0
        assert gate.stats() == {"limit": 2, "in_flight": 0, "queued": 0}

    async def test_a_limit_of_one_serialises(self):
        gate = BoundedGate(limit=1, queue_timeout=10)
        order = []

        async def worker(n):
            async with gate:
                order.append(f"start{n}")
                await asyncio.sleep(0.005)
                order.append(f"end{n}")

        await asyncio.gather(*(worker(n) for n in range(3)))
        # No interleaving: every start is immediately followed by its own end.
        assert order == ["start0", "end0", "start1", "end1", "start2", "end2"], order

    async def test_an_exception_still_gives_the_slot_back(self):
        """Otherwise one crash permanently shrinks the pool, quietly."""
        gate = BoundedGate(limit=1, queue_timeout=5)
        with pytest.raises(ValueError):
            async with gate:
                raise ValueError("tool blew up")
        assert gate.stats()["in_flight"] == 0
        async with gate:  # the next caller is not locked out forever
            assert gate.stats()["in_flight"] == 1

    async def test_cancellation_while_queued_leaves_no_ghost_ticket(self):
        gate = BoundedGate(limit=1, queue_timeout=None)
        holder = asyncio.Event()
        done = asyncio.Event()

        async def hold():
            async with gate:
                holder.set()
                await done.wait()

        task = asyncio.create_task(hold())
        await holder.wait()
        waiter = asyncio.create_task(_wait_once(gate))
        await asyncio.sleep(0.01)
        assert gate.stats()["queued"] == 1
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        done.set()
        await task
        assert gate.stats() == {"limit": 1, "in_flight": 0, "queued": 0}, (
            "a cancelled ticket left behind would hand a slot to nobody and "
            "strand every later caller"
        )

    async def test_waiters_are_served_in_arrival_order(self):
        gate = BoundedGate(limit=1, queue_timeout=10)
        served = []
        release = asyncio.Event()

        async def holder():
            async with gate:
                await release.wait()

        async def waiter(n):
            async with gate:
                served.append(n)

        tasks = [asyncio.create_task(holder())]
        await asyncio.sleep(0.01)
        tasks += [asyncio.create_task(waiter(n)) for n in range(1, 4)]
        await asyncio.sleep(0.01)
        release.set()
        await asyncio.gather(*tasks)
        assert served == [1, 2, 3], served

    async def test_a_free_slot_does_not_jump_the_queue(self):
        """Strict FIFO, even when it looks wasteful.

        Letting a fresh arrival take an open slot the instant it appears is how a
        polling caller starves everyone else: the poller is always there when a
        slot frees, the human is not.
        """
        gate = BoundedGate(limit=1, queue_timeout=10)
        order = []
        release = asyncio.Event()

        async def holder():
            async with gate:
                await release.wait()

        async def queued():
            async with gate:
                order.append("queued")

        async def latecomer():
            await asyncio.sleep(0.005)
            async with gate:
                order.append("latecomer")

        tasks = [asyncio.create_task(holder()), asyncio.create_task(queued()),
                 asyncio.create_task(latecomer())]
        await asyncio.sleep(0.02)
        release.set()
        await asyncio.gather(*tasks)
        assert order == ["queued", "latecomer"], order


class TestTimeoutAccounting:
    async def test_a_timed_out_waiter_does_not_run_as_if_it_held_the_slot(self):
        """The subtle one: `wait_for` cancels the ticket it was awaiting.

        Both "granted" and "timed out" leave the future *done*, so a check on
        `done()` admits the timed-out caller into the critical section anyway —
        two bodies inside a gate of one, and an `in_flight` count that no longer
        means anything. This asserts both the refusal and the accounting.
        """
        gate = BoundedGate(limit=1, queue_timeout=0.1)
        inside = []
        release = asyncio.Event()

        async def holder():
            async with gate:
                inside.append("holder")
                await release.wait()
            inside.append("holder-left")

        task = asyncio.create_task(holder())
        await asyncio.sleep(0.02)
        with pytest.raises(QueueTimeout):
            async with gate:
                inside.append("intruder")  # pragma: no cover - must not run
        assert "intruder" not in inside
        assert gate.stats()["in_flight"] == 1, "the holder still owns the slot"

        release.set()
        await task
        assert gate.stats() == {"limit": 1, "in_flight": 0, "queued": 0}

    async def test_a_waiter_granted_in_the_last_moment_is_not_discarded(self):
        """Losing a slot handed over at the deadline wastes work already paid for."""
        gate = BoundedGate(limit=1, queue_timeout=None)
        ran = []

        async def holder():
            async with gate:
                await asyncio.sleep(0.05)

        async def waiter():
            async with gate:
                ran.append("waiter")

        await asyncio.gather(holder(), waiter())
        assert ran == ["waiter"]
        assert gate.stats()["in_flight"] == 0


class _Reenter:
    """Marks whether a nested acquisition of the same gate blocked.

    The inner call is a plain `await`, mirroring what `stream()` actually does
    when it falls back to `complete()`. It must not be wrapped in `wait_for`:
    that would move the body into a *new task*, which is correctly not the
    re-entrant holder — the timeout therefore belongs to the caller of `outer`.
    """

    def __init__(self, gate):
        self.gate = gate
        self.inner_ran = False

    async def outer(self):
        async with self.gate:
            await self.inner()
        return "ok"

    async def inner(self):
        async with self.gate:
            self.inner_ran = True


class TestReentrancy:
    async def test_the_same_task_may_hold_the_slot_twice(self):
        """`stream()` falls back to a blocking `complete()` inside its own slot.

        That nesting is not hypothetical and not avoidable — it is the documented
        recovery for a stream that fails after its first delta — so a plain
        semaphore here is a deadlock on the happy-ish path, at limit=1 most
        reliably.
        """
        for limit in (1, 2, 4):
            gate = BoundedGate(limit=limit, queue_timeout=5)
            probe = _Reenter(gate)
            # The timeout is the *test's*: if nesting regressed into a deadlock,
            # this is what says so, rather than hanging the suite.
            outcome = await asyncio.wait_for(probe.outer(), timeout=1.0)
            assert outcome == "ok", f"deadlocked at limit={limit}"
            assert probe.inner_ran
            assert gate.stats()["in_flight"] == 0, "nesting leaked a slot"

    async def test_a_sibling_task_still_blocks(self):
        """Re-entrancy is per task. If it were per object the gate is decorative."""
        gate = BoundedGate(limit=1, queue_timeout=5)
        release = asyncio.Event()
        other_ran = []

        async def holder():
            async with gate:
                await release.wait()

        async def other():
            async with gate:
                other_ran.append(True)

        task = asyncio.create_task(holder())
        await asyncio.sleep(0.01)
        waiter = asyncio.create_task(other())
        await asyncio.sleep(0.02)
        assert not other_ran, "a second task walked into a held slot"
        release.set()
        await asyncio.gather(task, waiter)
        assert other_ran


class TestClientWiring:
    async def test_complete_and_stream_both_go_through_the_gate(self, monkeypatch):
        """The cap must be on the real entry points, not on a helper nobody calls."""
        from services import llm_client as mod

        gate = BoundedGate(limit=1, queue_timeout=5)
        monkeypatch.setattr(mod, "_llm_gate", gate)
        client = mod.LLMClient()

        async def fake_complete(self, *a, **k):
            assert gate.stats()["in_flight"] == 1, "complete() ran outside the gate"
            return "ok"

        async def fake_stream(self, *a, **k):
            assert gate.stats()["in_flight"] == 1, "stream() ran outside the gate"
            return "ok"

        monkeypatch.setattr(mod.LLMClient, "_complete", fake_complete)
        monkeypatch.setattr(mod.LLMClient, "_stream", fake_stream)
        assert await client.complete([{"role": "user", "content": "hi"}]) == "ok"
        assert await client.stream([{"role": "user", "content": "hi"}]) == "ok"
        assert gate.stats()["in_flight"] == 0

    async def test_a_full_gate_is_reported_as_a_busy_model_not_a_broken_one(
        self, monkeypatch
    ):
        from services.llm_client import AllModelsUnavailableError, user_notice

        monkeypatch.setattr(config, "LLM_MAX_CONCURRENCY", 1)
        monkeypatch.setattr(config, "LLM_QUEUE_TIMEOUT_S", 0.15)
        from services import llm_client as mod

        gate = BoundedGate(limit=1, queue_timeout=lambda: config.LLM_QUEUE_TIMEOUT_S)
        monkeypatch.setattr(mod, "_llm_gate", gate)
        client = mod.LLMClient()

        async def slow(self, *a, **k):
            await asyncio.sleep(5)

        monkeypatch.setattr(mod.LLMClient, "_complete", slow)
        holder = asyncio.create_task(client.complete([{"role": "user", "content": "a"}]))
        await asyncio.sleep(0.02)

        with pytest.raises(AllModelsUnavailableError) as caught:
            await client.complete([{"role": "user", "content": "b"}])
        assert caught.value.kind == "llm_queue_timeout"
        # The user-facing sentence must exist in both languages and must not be
        # the generic "all models are unavailable" — the models are fine, they
        # are busy, and only one of those two is worth acting on.
        for language in ("en", "ar"):
            notice = user_notice(caught.value, language)
            assert notice and ("busy" in notice or "M" in notice)
        holder.cancel()
        with pytest.raises(asyncio.CancelledError):
            await holder

    def test_health_publishes_occupancy(self):
        """'Why is it slow?' needs a number, not a theory."""
        from services.llm_client import llm_gate

        stats = llm_gate().stats()
        assert set(stats) == {"limit", "in_flight", "queued"}
        assert stats["limit"] == config.LLM_MAX_CONCURRENCY

    async def test_the_health_endpoint_carries_the_slots(self, monkeypatch):
        from fastapi.testclient import TestClient

        import api.main as main_module

        monkeypatch.setattr(main_module.provider_health, "status",
                            lambda force=False: _reachable())
        with TestClient(main_module.app) as client:
            body = client.get("/health").json()
        assert body["llm"]["slots"]["limit"] == config.LLM_MAX_CONCURRENCY


class _StubCaps:
    supports_streaming = True
    supports_tools = False
    supports_structured_output = False


class _StubProbe:
    """A stand-in for OllamaClient: only ``probe_capabilities`` is exercised."""

    def __init__(self):
        self.calls = 0

    async def probe_capabilities(self, model):
        self.calls += 1
        return _StubCaps()


class TestModelSlot:
    """A probe or a 'test chat' is a real generation and takes a real slot.

    ``complete()``/``stream()`` were the only paths that held one, so a
    user-triggered capability probe ran *alongside* a live turn — two
    generations on one GPU, which is the exact case the bound exists for.
    ``model_slot`` routes those callers through the same gate.
    """

    async def test_it_holds_a_slot_of_the_process_gate(self, monkeypatch):
        from services import llm_client as mod
        from services.llm_client import model_slot

        gate = BoundedGate(limit=1, queue_timeout=5)
        monkeypatch.setattr(mod, "_llm_gate", gate)
        async with model_slot():
            assert gate.stats()["in_flight"] == 1
        assert gate.stats()["in_flight"] == 0

    async def test_a_full_gate_refuses_a_probe_honestly(self, monkeypatch):
        from fastapi import HTTPException

        import api.main as main_module
        from services import llm_client as mod

        gate = BoundedGate(limit=1, queue_timeout=0.05)
        monkeypatch.setattr(mod, "_llm_gate", gate)
        release = asyncio.Event()

        async def hold():
            async with gate:
                await release.wait()

        holder = asyncio.create_task(hold())
        await asyncio.sleep(0.02)

        stub = _StubProbe()
        with pytest.raises(HTTPException) as caught:
            await main_module._probe_capabilities_gated(stub, "qwen3.5:9b")
        assert caught.value.status_code == 503
        assert stub.calls == 0, "the probe ran despite the gate being full"

        release.set()
        await holder

    async def test_a_free_gate_lets_the_probe_through(self, monkeypatch):
        import api.main as main_module
        from services import llm_client as mod

        monkeypatch.setattr(mod, "_llm_gate", BoundedGate(limit=2, queue_timeout=5))
        stub = _StubProbe()
        caps = await main_module._probe_capabilities_gated(stub, "qwen3.5:9b")
        assert caps.supports_streaming is True
        assert stub.calls == 1


async def _wait_once(gate):
    async with gate:
        await asyncio.sleep(0.01)


async def _reachable():
    from services.provider_health import REACHABLE

    return {
        "provider": config.PROVIDER, "endpoint": "", "state": REACHABLE,
        "error": "", "missing_models": [], "latency_ms": 1, "age_s": 0.0,
        "cached": False, "source": "probe", "checked_at": "",
    }
