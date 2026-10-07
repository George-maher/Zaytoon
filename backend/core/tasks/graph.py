"""Task graphs: independent work runs together, dependent work waits.

The shape this models:

    Analyze Project
           │
           ├── Inspect Backend ──┐
           │                     │
           ├── Inspect Frontend ─┤
           │                     ▼
           └── Inspect Database → Planning
                                    │
                                    ▼
                            Implementation
                                    │
                                    ▼
                                  Tests
                                    │
                                    ▼
                              Verification

Four properties, each of which exists because of a way the naive version is
wrong:

* **Independent nodes run concurrently**, under a semaphore. "Run backend tests,
  frontend tests, and check git status at once" is the common case, and running
  them in sequence is a three-times-slower answer to the same question.
* **Gated nodes are barriers.** A node whose permission tier is not ``safe``
  runs alone, in declaration order — the same rule the agent engine already
  applies, for the same reason: an approval prompt has no meaningful position
  inside a concurrent batch.
* **A failed dependency skips its dependents** rather than running them against
  a broken precondition, and says why.
* **Cycles are refused at construction time.** Discovering one at run time means
  discovering it while the user watches.

Outcomes are stored **positionally**, so ``results[i]`` is always node *i*,
whatever order things actually finished in.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from common.reporting import safe_error

logger = logging.getLogger("Zaytoon.Core.TaskGraph")

#: Node status values. Mirrors ``TaskStepStatus`` but is independent of it: a
#: graph node may exist without a task record behind it (the tests drive it
#: directly), and coupling them would mean the graph could not be exercised in
#: isolation.
NODE_PENDING = "pending"
NODE_RUNNING = "running"
NODE_DONE = "done"
NODE_FAILED = "failed"
NODE_SKIPPED = "skipped"

#: Default cap on simultaneous nodes. Four is chosen from the agent engine's own
#: limit: enough that four independent inspections genuinely overlap, low enough
#: that a test suite running four test suites at once does not thrash the disk.
DEFAULT_CONCURRENCY = 4


class CycleError(ValueError):
    """The dependency graph contains a cycle."""


class UnknownDependencyError(ValueError):
    """A node depends on something that is not in the graph."""


@dataclass
class Node:
    """One unit of work in a graph."""

    id: str
    run: Callable[["GraphContext"], Awaitable[Any]]
    depends_on: Sequence[str] = field(default_factory=tuple)
    #: ``safe`` runs concurrently; anything else is a barrier.
    barrier: bool = False
    #: Human-readable label. Fixed text, never model reasoning.
    label: str = ""


@dataclass
class NodeResult:
    """What one node did. Stored positionally, so order is the declaration's."""

    id: str
    status: str
    value: Any = None
    error: Optional[str] = None
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status == NODE_DONE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


@dataclass
class GraphContext:
    """What a node receives when it runs.

    Read access to the results of its *completed* dependencies, and write access
    to its own. Deliberately not a dict: a node that reaches for a sibling's
    result before that sibling has run is a race the scheduler cannot fix.
    """

    _results: Dict[str, NodeResult]
    outputs: Dict[str, Any] = field(default_factory=dict)

    def result(self, node_id: str) -> NodeResult:
        found = self._results.get(node_id)
        if found is None:
            raise UnknownDependencyError(
                f"'{node_id}' has not run; only declared dependencies are readable"
            )
        return found

    def value(self, node_id: str, default: Any = None) -> Any:
        found = self._results.get(node_id)
        return default if found is None else found.value

    @property
    def all_results(self) -> Dict[str, NodeResult]:
        return dict(self._results)


class TaskGraph:
    """A validated DAG of :class:`Node` objects."""

    def __init__(self, nodes: Sequence[Node], concurrency: int = DEFAULT_CONCURRENCY) -> None:
        if not nodes:
            raise ValueError("A task graph needs at least one node")
        self._nodes: Dict[str, Node] = {}
        for node in nodes:
            if node.id in self._nodes:
                raise ValueError(f"Duplicate node id: {node.id!r}")
            self._nodes[node.id] = node
        try:
            self.concurrency = max(1, int(concurrency))
        except (TypeError, ValueError):
            self.concurrency = DEFAULT_CONCURRENCY
        #: Bumped every time a node actually starts. :meth:`run` asserts on it, so
        #: a scheduling change that starts one node twice fails loudly instead of
        #: quietly re-running a side effect.
        self._starts = 0

        self._validate_dependencies()
        self._order = self._topological_order()

    # ------------------------------------------------------------------
    # Construction-time validation
    # ------------------------------------------------------------------
    def _validate_dependencies(self) -> None:
        for node in self._nodes.values():
            for dependency in node.depends_on:
                if dependency not in self._nodes:
                    raise UnknownDependencyError(
                        f"Node {node.id!r} depends on unknown node {dependency!r}"
                    )
                if dependency == node.id:
                    raise CycleError(f"Node {node.id!r} depends on itself")

    def _topological_order(self) -> List[str]:
        """Kahn's algorithm. Also proves acyclicity — a leftover node means a
        cycle, and it is reported here rather than hanging at run time."""
        indegree = {nid: 0 for nid in self._nodes}
        dependents: Dict[str, List[str]] = {nid: [] for nid in self._nodes}
        for node in self._nodes.values():
            for dependency in node.depends_on:
                indegree[node.id] += 1
                dependents[dependency].append(node.id)

        # Declaration order as the tie-break, so the output is deterministic:
        # two graphs with the same shape must produce the same order, or a bug
        # in a graph is unreproducible.
        ready = [nid for nid in self._nodes if indegree[nid] == 0]
        order: List[str] = []
        while ready:
            current = ready.pop(0)
            order.append(current)
            for dependent in dependents[current]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    ready.append(dependent)
            ready.sort(key=lambda nid: list(self._nodes).index(nid))

        if len(order) != len(self._nodes):
            unresolved = [nid for nid in self._nodes if nid not in order]
            raise CycleError(
                f"Dependency cycle involving: {', '.join(sorted(unresolved))}"
            )
        return order

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @property
    def node_ids(self) -> List[str]:
        """Declaration order."""
        return list(self._nodes)

    @property
    def execution_order(self) -> List[str]:
        """A valid sequential order. Useful for display and for tests."""
        return list(self._order)

    def describe(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": node.id,
                "label": node.label or node.id,
                "depends_on": list(node.depends_on),
                "barrier": node.barrier,
            }
            for node in self._nodes.values()
        ]

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    async def run(self, cancel_event: Optional[asyncio.Event] = None) -> List[NodeResult]:
        """Run every node. Returns results **in declaration order**.

        Never raises for a node failure — that is reported as a ``failed``
        result, because one failing inspection must not destroy the other three
        results the user asked for. A cancellation raises, because there is
        nothing left to report.

        The scheduler is a single loop with three steps, and the ordering of
        those steps is the whole implementation:

        1. **Harvest** anything that finished.
        2. **Start** every ready node whose concurrency allows it.
        3. **Wait** for at least one completion (or a barrier gap).
        """
        context = GraphContext(_results={})
        results: Dict[str, NodeResult] = {}
        completed: set = set()
        semaphore = asyncio.Semaphore(self.concurrency)
        in_flight: Dict[str, "asyncio.Task"] = {}
        self._starts = 0

        async def _run_node(node: Node) -> NodeResult:
            self._starts += 1
            started = time.monotonic()
            try:
                async with semaphore:
                    if cancel_event is not None and cancel_event.is_set():
                        return NodeResult(node.id, NODE_SKIPPED, error="cancelled")
                    value = await node.run(context)
                return NodeResult(
                    node.id, NODE_DONE, value=value,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one node must not kill the graph
                logger.warning(
                    safe_error(exc, operation="graph.node",
                               component="core.tasks.graph", detail=f"node={node.id}")
                )
                return NodeResult(
                    node.id, NODE_FAILED, error=str(exc)[:500],
                    duration_ms=int((time.monotonic() - started) * 1000),
                )

        def _record(node_id: str, outcome: NodeResult) -> None:
            results[node_id] = outcome
            context._results[node_id] = outcome
            completed.add(node_id)

        while len(completed) < len(self._nodes):
            if cancel_event is not None and cancel_event.is_set():
                for handle in in_flight.values():
                    handle.cancel()
                if in_flight:
                    await asyncio.gather(*in_flight.values(), return_exceptions=True)
                raise asyncio.CancelledError()

            # -- 2a: nodes whose dependency failed become skipped, straight away.
            #    Resolving these first is what keeps "ready" a clean question: a
            #    skipped node is finished, so it can never be mistaken for one
            #    still waiting to run.
            for node_id in self._order:
                if node_id in completed or node_id in in_flight:
                    continue
                node = self._nodes[node_id]
                if any(dep not in completed for dep in node.depends_on):
                    continue
                broken = [d for d in node.depends_on if results[d].status != NODE_DONE]
                if broken:
                    _record(node_id, NodeResult(
                        node_id, NODE_SKIPPED,
                        error=f"dependency failed: {', '.join(broken)}",
                    ))

            def _ready(node_id: str) -> bool:
                node = self._nodes[node_id]
                return all(dep in completed for dep in node.depends_on)

            # -- 2c: fill the deck with every ready *non-barrier* node first.
            #    Barriers are considered afterwards on purpose: a barrier
            #    declared after independent work must not preempt it, or
            #    "Inspect the backend, then confirm the install" would show the
            #    prompt before the inspection that justified it.
            for node_id in self._order:
                node = self._nodes[node_id]
                if node_id in completed or node_id in in_flight:
                    continue
                if node.barrier or not _ready(node_id):
                    continue
                in_flight[node_id] = asyncio.create_task(_run_node(node))

            # -- 2b: now a barrier. It runs only on an empty deck, so an
            #    approval prompt is never concurrent with other work — a prompt
            #    fired in the middle of a batch has no meaningful position for
            #    the user to answer.
            ready_barrier = next(
                (nid for nid in self._order
                 if nid not in completed and nid not in in_flight
                 and self._nodes[nid].barrier and _ready(nid)),
                None,
            )
            if ready_barrier is not None:
                if in_flight:
                    # Drain, then let the next pass start the barrier alone.
                    await asyncio.gather(*in_flight.values(), return_exceptions=True)
                    for finished_id, handle in list(in_flight.items()):
                        _record(finished_id, handle.result())
                        in_flight.pop(finished_id)
                    continue
                in_flight[ready_barrier] = asyncio.create_task(
                    _run_node(self._nodes[ready_barrier])
                )

            if not in_flight:
                # Nothing running and nothing startable. With cycles refused at
                # construction this can only mean every remaining node has an
                # unmet dependency that was never resolved — recorded as skipped
                # rather than left dangling, so the caller gets a full report.
                for node_id in self._order:
                    if node_id not in completed:
                        _record(node_id, NodeResult(
                            node_id, NODE_SKIPPED,
                            error="unreachable in this run",
                        ))
                break

            # -- 3: wait for progress.
            done, _pending = await asyncio.wait(
                in_flight.values(), return_when=asyncio.FIRST_COMPLETED
            )
            for handle in done:
                node_id = next(k for k, v in in_flight.items() if v is handle)
                _record(node_id, handle.result())
                in_flight.pop(node_id)

        # Every node ran exactly once. Asserted rather than assumed: a scheduling
        # change that starts one node twice would re-run a side effect (a write,
        # a command) invisibly, and the tests cannot see that from the results
        # alone.
        if self._starts > len(self._nodes):  # pragma: no cover - a bug guard
            raise RuntimeError(
                f"{self._starts} node starts for {len(self._nodes)} nodes — "
                "the scheduler ran something twice"
            )

        # Declaration order, whatever actually happened.
        return [results[nid] for nid in self.node_ids]


def build_graph(
    steps: Sequence[Dict[str, Any]],
    runner: Callable[[str, Dict[str, Any]], Awaitable[Any]],
    concurrency: int = DEFAULT_CONCURRENCY,
) -> TaskGraph:
    """Build a graph from plain dicts.

    The shape is what a JSON plan or a task-graph definition would use, so a
    planner can produce one without importing this module::

        [
          {"id": "backend",  "depends_on": []},
          {"id": "frontend", "depends_on": []},
          {"id": "plan",     "depends_on": ["backend", "frontend"]},
        ]

    ``runner`` is called with ``(node_id, node)`` for each node.
    """

    # The lambda ignores the GraphContext it is handed: the contract is
    # `runner(node_id, node_dict)`, so a node built from a plan is addressed by
    # name rather than by context. Binding the step as a default argument is
    # what keeps the late-binding bug out of the list comprehension.
    nodes = [
        Node(
            id=str(step["id"]),
            run=lambda context, step=step: runner(step["id"], step),
            depends_on=tuple(step.get("depends_on") or ()),
            barrier=bool(step.get("barrier", False)),
            label=str(step.get("label") or step["id"]),
        )
        for step in steps
    ]
    return TaskGraph(nodes, concurrency=concurrency)


__all__ = [
    "CycleError",
    "DEFAULT_CONCURRENCY",
    "GraphContext",
    "Node",
    "NodeResult",
    "NODE_DONE",
    "NODE_FAILED",
    "NODE_PENDING",
    "NODE_RUNNING",
    "NODE_SKIPPED",
    "TaskGraph",
    "UnknownDependencyError",
    "build_graph",
]