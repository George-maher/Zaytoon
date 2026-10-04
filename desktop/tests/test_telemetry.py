"""The telemetry worker: network and OS access, never on the Tk thread.

``TelemetryWorker`` is the only thing in the shell that performs blocking I/O,
so the contract that matters is not speed but *behaviour*: it pushes results
through the app's own event queue (one path into widget code), it never raises
out of a tick, and it degrades to "no data" for endpoints the backend does not
have — ``/audit`` and ``/processes`` do not exist, and a 404 body is HTML.
"""

from __future__ import annotations

import queue
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import app as app_mod  # noqa: E402
from app.sidepanel import SystemSampler  # noqa: E402


class FakeSampler(SystemSampler):
    """A sampler that never touches the OS: fixed numbers, injectable clock."""

    def __init__(self) -> None:
        super().__init__(
            times_reader=lambda: None,
            memory_reader=lambda: 41,
            clock=lambda: 10_000.0,
        )

    def sample(self, now=None) -> dict:
        self._last_sample = self._clock() if now is None else now
        return {"cpu": 12.5, "ram": 41.0}


@pytest.fixture
def responses(monkeypatch):
    """Route every REST call the worker makes through one dict."""
    table: dict[str, object] = {}

    def fake_fetch(url: str, timeout: float = 3.0, opener=None):
        for path, payload in table.items():
            if url.endswith(path):
                return payload
        return None

    monkeypatch.setattr(app_mod, "fetch_json", fake_fetch)
    return table


def make_worker(responses) -> tuple[app_mod.TelemetryWorker, queue.Queue]:
    events: queue.Queue = queue.Queue()
    worker = app_mod.TelemetryWorker("http://127.0.0.1:9", events, sampler=FakeSampler())
    return worker, events


def drain(events: queue.Queue) -> list[dict]:
    out: list[dict] = []
    while True:
        try:
            kind, value = events.get_nowait()
        except queue.Empty:
            return out
        assert kind == "telemetry", f"worker must speak the app's one vocabulary, got {kind!r}"
        out.append(value)


class TestSnapshotOnDemand:
    def test_no_snapshot_until_asked(self, responses):
        worker, events = make_worker(responses)
        worker._tick()
        # The OS sample and the optional poll are due, but no *snapshot* was
        # requested, so neither /tools nor /health was consulted.
        payloads = drain(events)
        assert all("tools" not in p and "model" not in p for p in payloads)
        assert {"system": {"cpu": 12.5, "ram": 41.0}} in payloads

    def test_requested_snapshot_fetches_tools_and_health(self, responses):
        responses["/tools"] = {"tools": ["file_read", "file_write"]}
        responses["/health"] = {"llm": {"chain": [{"model": "m1"}]}}
        worker, events = make_worker(responses)
        worker.request_snapshot()
        worker._tick()

        payloads = drain(events)
        assert {"tools": ["file_read", "file_write"]} in payloads
        assert {"model": "m1"} in payloads

    def test_a_successful_snapshot_is_not_refetched_forever(self, responses):
        responses["/tools"] = {"tools": ["a"]}
        worker, events = make_worker(responses)
        worker.request_snapshot()
        worker._tick()
        drain(events)
        worker._tick()
        assert drain(events) == []

    def test_a_failed_snapshot_is_retried(self, responses):
        # /tools answers None (backend still booting): keep asking next tick.
        worker, events = make_worker(responses)
        worker.request_snapshot()
        worker._tick()
        worker.request_snapshot()
        worker._tick()
        # No tools payload was produced, but nothing raised either.
        assert all("tools" not in p for p in drain(events))


class TestOptionalEndpoints:
    def test_missing_audit_and_processes_emit_empty_lists(self, responses):
        worker, events = make_worker(responses)
        worker._next_optional = 0.0
        worker._tick()
        payloads = drain(events)
        assert {"audit": [], "processes": []} in payloads

    def test_present_endpoints_are_parsed(self, responses):
        responses["/audit"] = {"audit": [{"action": "file_write", "time": "12:00"}]}
        responses["/processes"] = {"processes": [{"name": "python", "pid": 1}]}
        worker, events = make_worker(responses)
        worker._tick()
        payloads = drain(events)
        optional = next(p for p in payloads if "audit" in p)
        assert optional["audit"] == ["12:00 · file_write"]
        assert optional["processes"] == ["python · pid 1"]

    def test_optional_endpoints_are_rate_limited(self, responses):
        worker, events = make_worker(responses)
        worker._tick()
        assert drain(events) != []  # first tick schedules the interval
        events.queue.clear()
        worker._tick()
        # Nothing due: the 10s floor has not elapsed.
        assert drain(events) == []


class TestSystemSampling:
    def test_a_due_sampler_emits_system_numbers(self, responses):
        worker, events = make_worker(responses)
        worker._tick()
        payloads = drain(events)
        assert {"system": {"cpu": 12.5, "ram": 41.0}} in payloads

    def test_a_raising_sampler_does_not_kill_the_worker(self, responses):
        # The containment lives in run(), not in _tick(): one bad tick is
        # logged and the loop carries on instead of the thread dying silently.
        class Boom(SystemSampler):
            def due(self, now=None) -> bool:
                raise RuntimeError("no such API")

        events: queue.Queue = queue.Queue()
        worker = app_mod.TelemetryWorker("http://x", events, sampler=Boom())
        ticks = {"n": 0}

        original = worker._tick

        def counting_tick():
            ticks["n"] += 1
            if ticks["n"] >= 2:
                worker.stop()
            original()

        worker._tick = counting_tick
        worker.run()  # must return normally, having swallowed the failure
        assert ticks["n"] == 2
        assert worker._stop.is_set()


class TestWorkerLifecycle:
    def test_stop_lets_the_loop_finish(self, responses):
        worker, _events = make_worker(responses)
        worker.stop()
        assert worker._stop.is_set()

    def test_the_worker_is_a_daemon_thread(self, responses):
        # It must never hold the process open on shutdown.
        worker, _events = make_worker(responses)
        assert worker.daemon is True
        assert worker.name == "zaytoon-telemetry"
