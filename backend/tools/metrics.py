"""Execution metrics for the tool layer.

Kept separate from ``services.llm_client.LLMMetrics`` on purpose: that one
answers "is the *model* healthy?" (rate limits, invalid completions, latency),
this one answers "is the *tool layer* healthy?" — which tool is slow, how often
the gate declines, how many calls time out. They are exposed side by side on
``GET /health`` (``llm.metrics`` and top-level ``metrics.tools``) rather than
merged, because a single blended number cannot be acted on.

Counters only. Every figure here is derivable from this process's own
observations, so nothing can drift out of sync with what actually ran. The
registry is the chokepoint for *execution*, but the engine owns **timing** — it
is the layer that knows about the timeout, the gate and the batch — so the
engine records into this object.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

#: Terminal states a call can end in. Plain strings so the payload stays
#: JSON-serialisable without the client importing an enum.
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_DECLINED = "declined"
STATUS_TIMED_OUT = "timed_out"

_TERMINAL = (STATUS_OK, STATUS_FAILED, STATUS_DECLINED, STATUS_TIMED_OUT)


def _empty_entry() -> Dict[str, Any]:
    return {
        "calls": 0,       # started
        "done": 0,        # reached a terminal state
        "running": 0,     # in flight right now (a gauge)
        "avg_ms": 0.0,    # running mean over completed calls
        "last_ms": 0.0,
        "last_status": None,
    }


class ToolMetrics:
    """Counters for tool execution.

    Deliberately lock-free: every mutation happens on the event loop, so a lock
    would be a cost with no reader. ``running`` is a gauge rather than a
    counter — a stuck increment shows up immediately as a number that never
    comes back down, which is exactly the signal you want on a hung tool.
    """

    def __init__(self) -> None:
        self._started = 0
        self._running = 0
        self._status: Dict[str, int] = {status: 0 for status in _TERMINAL}
        self._total_ms = 0.0
        self._by_tool: Dict[str, Dict[str, Any]] = {}

    # -- recording -------------------------------------------------------
    def begin(self, name: str) -> None:
        """A call has been dispatched. Pairs with :meth:`end`."""
        self._started += 1
        self._running += 1
        entry = self._by_tool.setdefault(name, _empty_entry())
        entry["calls"] += 1
        entry["running"] += 1

    def end(self, name: str, status: str = STATUS_OK, duration_ms: float = 0.0) -> None:
        """Record the terminal state of a call started with :meth:`begin`."""
        if status not in self._status:
            status = STATUS_FAILED
        self._status[status] += 1
        if self._running:
            self._running -= 1

        duration_ms = max(0.0, float(duration_ms))
        self._total_ms += duration_ms

        entry = self._by_tool.setdefault(name, _empty_entry())
        if entry["running"]:
            entry["running"] -= 1
        done = entry["done"]
        # Running mean: O(1) memory, and honest about which calls it covers.
        entry["avg_ms"] = (entry["avg_ms"] * done + duration_ms) / (done + 1)
        entry["done"] = done + 1
        entry["last_ms"] = duration_ms
        entry["last_status"] = status

    # -- reporting -------------------------------------------------------
    @property
    def running(self) -> int:
        return self._running

    def to_dict(self) -> Dict[str, Any]:
        completed = sum(self._status.values())
        return {
            "started": self._started,
            "completed": completed,
            "running": self._running,
            "succeeded": self._status[STATUS_OK],
            "failed": self._status[STATUS_FAILED],
            "declined": self._status[STATUS_DECLINED],
            "timed_out": self._status[STATUS_TIMED_OUT],
            "avg_ms": round(self._total_ms / completed, 2) if completed else 0.0,
            "by_tool": {
                name: {
                    "calls": entry["calls"],
                    "completed": entry["done"],
                    "running": entry["running"],
                    "avg_ms": round(entry["avg_ms"], 2),
                    "last_ms": round(entry["last_ms"], 2),
                    "last_status": entry["last_status"],
                }
                for name, entry in sorted(self._by_tool.items())
            },
        }

    def reset(self) -> None:
        """Clear everything. Used by tests and by the settings panel."""
        self.__init__()


#: The process-wide instance. One per process is the point: ``/health`` and the
#: settings panel read it without any plumbing, and a restart clearing it is
#: correct — these are process-lifetime observations.
_METRICS = ToolMetrics()


def get_tool_metrics() -> ToolMetrics:
    return _METRICS


def record_tool_call(
    metrics: Optional[ToolMetrics],
    name: str,
    status: str,
    duration_ms: float,
) -> None:
    """Best-effort recorder: metrics must never be able to fail a tool call."""
    if metrics is None:
        return
    try:
        metrics.end(name, status=status, duration_ms=duration_ms)
    except Exception:  # pragma: no cover - defensive
        pass


__all__ = [
    "ToolMetrics",
    "get_tool_metrics",
    "record_tool_call",
    "STATUS_OK",
    "STATUS_FAILED",
    "STATUS_DECLINED",
    "STATUS_TIMED_OUT",
]
