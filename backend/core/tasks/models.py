"""Task records: what a task *is*, and which state changes are legal.

The lifecycle in the spec is a straight line with a few exits:

    RECEIVED → ANALYZING → PLANNING → WAITING_FOR_PERMISSION → EXECUTING
                                ↓                                ↓
                            VERIFYING ←──────────────────────────┘
                                ↓
                            COMPLETED

plus FAILED, CANCELLED, PAUSED and WAITING_FOR_USER.

Transitions are validated in one place, :meth:`TaskStatus.can_transition_to`.
Not because ceremony is valuable, but because the alternative is a task log that
lies: a task that jumps from ``RECEIVED`` to ``COMPLETED`` because some code
path forgot a guard is worse than a task that raises, because the raise is
visible and the log is not.

``PAUSED`` is the one genuinely interesting case. Pausing is not a state change
in the usual sense — a paused task is still the one running, it just is not
advancing — so ``PAUSED`` is a **suspension flag**, not a status. Modelling it
as a status would have required every transition table to enumerate
``PAUSED → X`` for all seven X, and would have made "what was this task doing
before the pause" unrecoverable from the status column alone.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class TaskStatus(str, Enum):
    """Every state a task can be in."""

    RECEIVED = "received"
    ANALYZING = "analyzing"
    PLANNING = "planning"
    WAITING_FOR_PERMISSION = "waiting_for_permission"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    WAITING_FOR_USER = "waiting_for_user"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL

    @property
    def is_active(self) -> bool:
        """True while the task is doing something (or waiting to be allowed to)."""
        return self in _ACTIVE

    def can_transition_to(self, target: "TaskStatus") -> bool:
        """Whether this → target is a legal move."""
        return target in _TRANSITIONS.get(self, frozenset())

    def describe(self) -> str:
        return _STATUS_DESCRIPTIONS.get(self, self.value)


_TERMINAL = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED})

_ACTIVE = frozenset({
    TaskStatus.ANALYZING,
    TaskStatus.PLANNING,
    TaskStatus.EXECUTING,
    TaskStatus.VERIFYING,
})

#: The legal moves, in one table. Anything not listed is refused.
#:
#: ``FAILED`` and ``CANCELLED`` are reachable from every non-terminal state
#: because any of them can fail or be cancelled. ``VERIFYING`` is skipped by
#: every route that never executed anything: a conversational turn, or a plan
#: that turned out to need no tools, has nothing to verify, and forcing a
#: synthetic ``EXECUTING → VERIFYING`` pass over it would put a lie in the
#: record — two statuses implying work that did not happen. ``EXECUTING`` may go
#: straight to ``COMPLETED`` for the same reason.
_TRANSITIONS: Dict[TaskStatus, frozenset] = {
    TaskStatus.RECEIVED: frozenset({
        TaskStatus.ANALYZING, TaskStatus.PLANNING, TaskStatus.EXECUTING,
        TaskStatus.FAILED, TaskStatus.CANCELLED,
    }),
    TaskStatus.ANALYZING: frozenset({
        TaskStatus.PLANNING, TaskStatus.EXECUTING, TaskStatus.WAITING_FOR_USER,
        TaskStatus.VERIFYING,
        # A question answered from context alone.
        TaskStatus.COMPLETED,
        TaskStatus.FAILED, TaskStatus.CANCELLED,
    }),
    TaskStatus.PLANNING: frozenset({
        TaskStatus.WAITING_FOR_PERMISSION, TaskStatus.EXECUTING,
        TaskStatus.WAITING_FOR_USER,
        # Reachable because a turn can be routed, planned and then finished by
        # the agent *without* the orchestrator moving it to EXECUTING — the
        # agent stack owns execution, and a conversational turn never crosses
        # into it. Without these the task system deadlocks on the ordinary case.
        TaskStatus.VERIFYING, TaskStatus.COMPLETED,
        TaskStatus.FAILED, TaskStatus.CANCELLED,
    }),
    TaskStatus.WAITING_FOR_PERMISSION: frozenset({
        TaskStatus.EXECUTING, TaskStatus.WAITING_FOR_USER,
        TaskStatus.FAILED, TaskStatus.CANCELLED,
    }),
    TaskStatus.WAITING_FOR_USER: frozenset({
        TaskStatus.ANALYZING, TaskStatus.PLANNING, TaskStatus.EXECUTING,
        TaskStatus.WAITING_FOR_PERMISSION, TaskStatus.FAILED, TaskStatus.CANCELLED,
    }),
    TaskStatus.EXECUTING: frozenset({
        TaskStatus.VERIFYING, TaskStatus.COMPLETED, TaskStatus.PLANNING,
        TaskStatus.WAITING_FOR_PERMISSION, TaskStatus.WAITING_FOR_USER,
        TaskStatus.FAILED, TaskStatus.CANCELLED,
    }),
    TaskStatus.VERIFYING: frozenset({
        TaskStatus.COMPLETED, TaskStatus.EXECUTING,
        TaskStatus.WAITING_FOR_USER, TaskStatus.FAILED, TaskStatus.CANCELLED,
    }),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}

_STATUS_DESCRIPTIONS: Dict[TaskStatus, str] = {
    TaskStatus.RECEIVED: "Request received",
    TaskStatus.ANALYZING: "Understanding the request",
    TaskStatus.PLANNING: "Forming a plan",
    TaskStatus.WAITING_FOR_PERMISSION: "Waiting for your approval",
    TaskStatus.EXECUTING: "Running tools",
    TaskStatus.VERIFYING: "Checking the result",
    TaskStatus.COMPLETED: "Done",
    TaskStatus.FAILED: "Failed",
    TaskStatus.CANCELLED: "Cancelled",
    TaskStatus.WAITING_FOR_USER: "Waiting for you",
}

#: Progress implied by each status when no step information is available, so a
#: bar always moves. A task in ``EXECUTING`` with three of five steps done
#: reports its real progress; this is the floor.
_STATUS_PROGRESS: Dict[TaskStatus, float] = {
    TaskStatus.RECEIVED: 0.0,
    TaskStatus.ANALYZING: 0.1,
    TaskStatus.PLANNING: 0.2,
    TaskStatus.WAITING_FOR_PERMISSION: 0.3,
    TaskStatus.WAITING_FOR_USER: 0.3,
    TaskStatus.EXECUTING: 0.5,
    TaskStatus.VERIFYING: 0.9,
    TaskStatus.COMPLETED: 1.0,
    TaskStatus.FAILED: 1.0,
    TaskStatus.CANCELLED: 1.0,
}


def progress_for_status(status: TaskStatus) -> float:
    return _STATUS_PROGRESS.get(status, 0.0)


class IllegalTransition(ValueError):
    """Raised when a task is asked to move somewhere it cannot go.

    A ``ValueError`` subclass so existing broad handlers still catch it, but a
    distinct type so the task manager can report *which* move was refused.
    """

    def __init__(self, current: TaskStatus, target: TaskStatus):
        self.current = current
        self.target = target
        super().__init__(
            f"A {current.value} task cannot become {target.value}"
        )


class TaskStepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class TaskStep:
    """One step of a task's plan."""

    title: str
    status: TaskStepStatus = TaskStepStatus.PENDING
    seq: int = 0
    tool: Optional[str] = None
    detail: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "status": self.status.value,
            "seq": self.seq,
            "tool": self.tool,
            "detail": self.detail,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }


@dataclass
class Task:
    """A unit of work with a durable identity.

    Every meaningful user request becomes one of these, so "what did Zaytoon do
    to my project last night" is answerable after a restart — which it is not
    today, because a turn only exists inside one WebSocket message.
    """

    title: str
    request: str = ""
    status: TaskStatus = TaskStatus.RECEIVED
    id: Optional[int] = None
    progress: float = 0.0
    intent: Optional[str] = None
    language: Optional[str] = None
    project_id: Optional[str] = None
    session_id: Optional[str] = None
    #: The task this one retries. Set when a terminal task is retried: the
    #: old record stays terminal (the history of *why* a retry was needed is
    #: the point) and the new one carries the edge back to it.
    parent_task_id: Optional[int] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    plan: List[str] = field(default_factory=list)
    steps: List[TaskStep] = field(default_factory=list)
    files_changed: List[str] = field(default_factory=list)
    tools_used: List[str] = field(default_factory=list)
    logs: List[Dict[str, Any]] = field(default_factory=list)
    result: Optional[str] = None
    error: Optional[str] = None
    paused: bool = False
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    duration_ms: Optional[int] = None
    _monotonic_start: float = field(default_factory=time.monotonic, repr=False)

    # -- transitions ------------------------------------------------------
    def transition(self, target: TaskStatus) -> None:
        """Move to ``target``, or raise :class:`IllegalTransition`.

        Stamps ``started_at`` on the first non-terminal move and ``ended_at`` on
        the terminal one, so duration is a property of the record rather than
        something a caller has to remember to measure.
        """
        if not isinstance(target, TaskStatus):
            target = TaskStatus(str(target))
        if target is self.status:
            return
        if not self.status.can_transition_to(target):
            raise IllegalTransition(self.status, target)

        self.status = target
        now = _now()
        if self.started_at is None and target.is_active:
            self.started_at = now
        if target.is_terminal:
            self.ended_at = now
            self.duration_ms = int((time.monotonic() - self._monotonic_start) * 1000)
        self.progress = max(self.progress, progress_for_status(target))
        if target is not TaskStatus.COMPLETED and target.is_terminal:
            # A failed or cancelled task that somehow reports 100% success is a
            # lie a progress bar cannot show.
            self.progress = 1.0

    # -- progress ---------------------------------------------------------
    def refresh_progress(self) -> float:
        """Recompute progress from the steps, never moving it backwards.

        Falls back to the status floor when there are no steps, so a task that
        has one always shows *something* moving.
        """
        if not self.steps:
            self.progress = max(self.progress, progress_for_status(self.status))
            return self.progress

        weights = {
            TaskStepStatus.PENDING: 0.0,
            TaskStepStatus.RUNNING: 0.5,
            TaskStepStatus.DONE: 1.0,
            # A failed or skipped step is finished work. Reporting it as 0 would
            # make the bar freeze at the point things went wrong.
            TaskStepStatus.FAILED: 1.0,
            TaskStepStatus.SKIPPED: 1.0,
        }
        total = sum(weights.get(s.status, 0.0) for s in self.steps)
        computed = round(total / len(self.steps), 3)
        self.progress = max(self.progress, computed)
        return self.progress

    # -- steps ------------------------------------------------------------
    def set_plan(self, steps: List[str]) -> None:
        """Record the plan. Human-readable labels only."""
        self.plan = [str(s) for s in steps if str(s).strip()]
        self.steps = [
            TaskStep(title=label, seq=index)
            for index, label in enumerate(self.plan)
        ]

    def step(self, seq: int) -> Optional[TaskStep]:
        for candidate in self.steps:
            if candidate.seq == seq:
                return candidate
        return None

    # -- serialisation ----------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "request": self.request,
            "status": self.status.value,
            "status_label": self.status.describe(),
            "progress": round(self.progress, 3),
            "paused": self.paused,
            "intent": self.intent,
            "language": self.language,
            "project_id": self.project_id,
            "session_id": self.session_id,
            "parent_task_id": self.parent_task_id,
            "provider": self.provider,
            "model": self.model,
            "plan": list(self.plan),
            "steps": [s.to_dict() for s in self.steps],
            "files_changed": list(self.files_changed),
            "tools_used": list(self.tools_used),
            "logs": list(self.logs),
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
            "is_terminal": self.status.is_terminal,
        }

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "Task":
        """Rebuild from a database row."""
        return cls(
            id=row.get("id"),
            title=row.get("title") or "",
            request=row.get("request") or "",
            status=TaskStatus(row.get("status") or TaskStatus.RECEIVED.value),
            progress=float(row.get("progress") or 0.0),
            intent=row.get("intent"),
            language=row.get("language"),
            project_id=row.get("project_id"),
            session_id=row.get("session_id"),
            parent_task_id=row.get("parent_task_id"),
            provider=row.get("provider"),
            model=row.get("model"),
            plan=_load_json_list(row.get("plan")),
            files_changed=_load_json_list(row.get("files_changed")),
            tools_used=_load_json_list(row.get("tools_used")),
            result=row.get("result"),
            error=row.get("error"),
            # Persisted since migration 2; a row written before it reads
            # ``None`` here and an unset flag is still ``False``.
            paused=bool(int(row.get("paused") or 0)),
            created_at=row.get("created_at"),
            started_at=row.get("started_at"),
            ended_at=row.get("ended_at"),
            duration_ms=row.get("duration_ms"),
        )


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_json_list(raw: Any) -> List[str]:
    """Tolerant JSON-list decoding.

    A corrupt cell returns ``[]`` rather than raising: a task row with an
    unreadable plan is still a task row the user needs to see, and losing the
    plan text is a smaller failure than losing the task.
    """
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(v) for v in raw]
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, dict):
        return [str(k) for k in value]
    return [str(value)]


def new_task_id() -> str:
    """A client-visible task reference, independent of the row id."""
    return f"task_{uuid.uuid4().hex[:12]}"


__all__ = [
    "IllegalTransition",
    "Task",
    "TaskStatus",
    "TaskStep",
    "TaskStepStatus",
    "new_task_id",
    "progress_for_status",
]