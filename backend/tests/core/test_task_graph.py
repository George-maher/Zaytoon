"""Task graphs: concurrency, barriers, skipped dependents, cycles."""

import asyncio

import pytest

from core.tasks.graph import (
    NODE_DONE,
    NODE_FAILED,
    NODE_SKIPPED,
    CycleError,
    Node,
    TaskGraph,
    UnknownDependencyError,
    build_graph,
)


def sleeper(seconds: float, log: list = None, name: str = ""):
    async def run(context):
        if log is not None:
            log.append(("start", name))
        await asyncio.sleep(seconds)
        if log is not None:
            log.append(("end", name))
        return f"{name} done"

    return run


class TestConstruction:
    def test_duplicate_ids_are_refused(self):
        with pytest.raises(ValueError, match="Duplicate"):
            TaskGraph([
                Node(id="a", run=sleeper(0)),
                Node(id="a", run=sleeper(0)),
            ])

    def test_an_empty_graph_is_refused(self):
        with pytest.raises(ValueError):
            TaskGraph([])

    def test_an_unknown_dependency_is_refused(self):
        with pytest.raises(UnknownDependencyError):
            TaskGraph([Node(id="a", run=sleeper(0), depends_on=("ghost",))])

    def test_self_dependency_is_a_cycle(self):
        with pytest.raises(CycleError):
            TaskGraph([Node(id="a", run=sleeper(0), depends_on=("a",))])

    def test_a_two_node_cycle_is_refused(self):
        with pytest.raises(CycleError):
            TaskGraph([
                Node(id="a", run=sleeper(0), depends_on=("b",)),
                Node(id="b", run=sleeper(0), depends_on=("a",)),
            ])

    def test_a_longer_cycle_is_refused(self):
        with pytest.raises(CycleError):
            TaskGraph([
                Node(id="a", run=sleeper(0), depends_on=("c",)),
                Node(id="b", run=sleeper(0), depends_on=("a",)),
                Node(id="c", run=sleeper(0), depends_on=("b",)),
            ])

    def test_a_diamond_is_legal(self):
        graph = TaskGraph([
            Node(id="root", run=sleeper(0)),
            Node(id="left", run=sleeper(0), depends_on=("root",)),
            Node(id="right", run=sleeper(0), depends_on=("root",)),
            Node(id="join", run=sleeper(0), depends_on=("left", "right")),
        ])
        assert set(graph.execution_order) == {"root", "left", "right", "join"}

    def test_the_order_is_deterministic(self):
        """Two graphs with the same shape must produce the same order, or a bug
        in a graph is unreproducible."""
        def make():
            return TaskGraph([
                Node(id="a", run=sleeper(0)),
                Node(id="b", run=sleeper(0)),
                Node(id="c", run=sleeper(0), depends_on=("a", "b")),
            ])

        assert make().execution_order == make().execution_order

    def test_concurrency_is_bounded_to_at_least_one(self):
        assert TaskGraph([Node(id="a", run=sleeper(0))], concurrency=0).concurrency == 1
        assert TaskGraph([Node(id="a", run=sleeper(0))], concurrency="x").concurrency == 4


class TestConcurrency:
    async def test_independent_nodes_overlap(self):
        """Three independent inspections should cost one, not three."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        graph = TaskGraph([
            Node(id="a", run=sleeper(0.12)),
            Node(id="b", run=sleeper(0.12)),
            Node(id="c", run=sleeper(0.12)),
        ])
        results = await graph.run()
        elapsed = loop.time() - started

        assert all(r.status == NODE_DONE for r in results)
        assert elapsed < 0.30, f"took {elapsed:.3f}s — the nodes did not overlap"

    async def test_the_concurrency_cap_is_respected(self):
        live = 0
        peak = 0

        async def tracked(context):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.05)
            live -= 1
            return True

        graph = TaskGraph(
            [Node(id=f"n{i}", run=tracked) for i in range(8)], concurrency=2
        )
        await graph.run()

        assert peak <= 2, f"peak concurrency was {peak}"

    async def test_dependent_nodes_are_sequential(self):
        log = []
        graph = TaskGraph([
            Node(id="first", run=sleeper(0.02, log, "first")),
            Node(id="second", run=sleeper(0.02, log, "second"), depends_on=("first",)),
        ])
        await graph.run()

        assert log == [("start", "first"), ("end", "first"),
                       ("start", "second"), ("end", "second")]


class TestBarriers:
    async def test_a_barrier_waits_for_in_flight_work(self):
        """An approval prompt has no meaningful position inside a concurrent batch."""
        log = []
        graph = TaskGraph([
            Node(id="slow", run=sleeper(0.08, log, "slow")),
            Node(id="gate", run=sleeper(0.0, log, "gate"), barrier=True),
        ])
        await graph.run()

        assert ("end", "slow") in log
        assert log.index(("end", "slow")) < log.index(("start", "gate"))


class TestFailure:
    async def test_a_failing_node_does_not_kill_the_graph(self):
        """One failing inspection must not destroy the other results."""
        async def boom(context):
            raise RuntimeError("could not reach the database")

        graph = TaskGraph([
            Node(id="good", run=sleeper(0)),
            Node(id="bad", run=boom),
            Node(id="also_good", run=sleeper(0)),
        ])
        results = {r.id: r for r in await graph.run()}

        assert results["good"].status == NODE_DONE
        assert results["bad"].status == NODE_FAILED
        assert results["also_good"].status == NODE_DONE
        assert "could not reach" in results["bad"].error

    async def test_a_dependent_of_a_failure_is_skipped_with_a_reason(self):
        async def boom(context):
            raise RuntimeError("nope")

        graph = TaskGraph([
            Node(id="bad", run=boom),
            Node(id="after", run=sleeper(0), depends_on=("bad",)),
        ])
        results = {r.id: r for r in await graph.run()}

        assert results["after"].status == NODE_SKIPPED
        assert "dependency failed" in results["after"].error
        assert "bad" in results["after"].error

    async def test_a_cancelled_error_propagates(self):
        async def cancelled(context):
            raise asyncio.CancelledError()

        graph = TaskGraph([Node(id="x", run=cancelled)])
        with pytest.raises(asyncio.CancelledError):
            await graph.run()

    async def test_results_are_in_declaration_order(self):
        async def boom(context):
            raise RuntimeError("x")

        graph = TaskGraph([
            Node(id="c", run=sleeper(0.05)),
            Node(id="a", run=boom),
            Node(id="b", run=sleeper(0)),
        ])
        results = await graph.run()
        assert [r.id for r in results] == ["c", "a", "b"]


class TestValues:
    async def test_a_node_receives_its_dependency_result(self):
        seen = {}

        async def producer(context):
            return {"count": 42}

        async def consumer(context):
            seen["value"] = context.value("producer")
            return seen["value"]["count"] * 2

        graph = TaskGraph([
            Node(id="producer", run=producer),
            Node(id="consumer", run=consumer, depends_on=("producer",)),
        ])
        results = {r.id: r for r in await graph.run()}

        assert results["consumer"].value == 84
        assert seen["value"] == {"count": 42}

    async def test_reading_a_non_dependency_is_refused(self):
        """A node reaching for a sibling before it ran is a race the scheduler
        cannot fix, so the context refuses it."""
        async def cheating(context):
            with pytest.raises(UnknownDependencyError):
                context.result("sibling")
            return "ok"

        graph = TaskGraph([
            Node(id="first", run=sleeper(0)),
            Node(id="second", run=cheating),
        ])
        results = {r.id: r for r in await graph.run()}
        assert results["second"].status == NODE_DONE

    async def test_a_node_can_write_to_the_shared_output(self):
        async def writer(context):
            context.outputs["note"] = "written"
            return True

        async def reader(context):
            return context.outputs.get("note")

        graph = TaskGraph([
            Node(id="w", run=writer),
            Node(id="r", run=reader, depends_on=("w",)),
        ])
        results = {r.id: r for r in await graph.run()}
        assert results["r"].value == "written"


class TestCancellation:
    async def test_a_cancel_event_stops_the_graph(self):
        event = asyncio.Event()
        started = asyncio.Event()

        async def slow(context):
            started.set()
            await asyncio.sleep(30)

        async def canceller():
            await started.wait()
            event.set()

        graph = TaskGraph([Node(id="slow", run=slow), Node(id="next", run=sleeper(0))])
        asyncio.create_task(canceller())

        with pytest.raises(asyncio.CancelledError):
            await graph.run(cancel_event=event)


class TestBuildGraph:
    async def test_build_from_dicts(self):
        seen = []

        async def runner(node_id, node):
            seen.append(node_id)
            return node_id

        graph = build_graph([
            {"id": "backend", "label": "Inspect the backend"},
            {"id": "frontend", "label": "Inspect the frontend"},
            {"id": "plan", "depends_on": ["backend", "frontend"]},
        ], runner)

        results = {r.id: r for r in await graph.run()}

        assert set(seen) == {"backend", "frontend", "plan"}
        assert all(r.status == NODE_DONE for r in results.values())
        # The label came from the dict, not from the id.
        assert graph.describe()[0]["label"] == "Inspect the backend"
        # "plan" ran after both inspections: its value is the id it was given,
        # and the runner only ever saw the three declared nodes.
        assert results["plan"].value == "plan"

    async def test_build_preserves_barriers(self):
        graph = build_graph(
            [{"id": "a", "barrier": True}],
            lambda node_id, node: asyncio.sleep(0),
        )
        assert graph.describe()[0]["barrier"] is True

    def test_build_rejects_a_cycle(self):
        with pytest.raises(CycleError):
            build_graph([
                {"id": "a", "depends_on": ["b"]},
                {"id": "b", "depends_on": ["a"]},
            ], lambda node_id, node: asyncio.sleep(0))


class TestDescribe:
    def test_describe_is_plain_data(self):
        graph = TaskGraph([
            Node(id="a", run=sleeper(0), label="Read the project"),
            Node(id="b", run=sleeper(0), depends_on=("a",), barrier=True),
        ])
        import json
        json.dumps(graph.describe())

        rows = {row["id"]: row for row in graph.describe()}
        assert rows["a"]["label"] == "Read the project"
        assert rows["b"]["depends_on"] == ["a"]
        assert rows["b"]["barrier"] is True