"""The core event vocabulary.

``agents/events.py`` already has an ``EventBus`` for one agent's progress, and
it is good. This does **not** replace it — that would be a second bus, and the
codebase is explicit that two event systems is how UI logic gets duplicated.

What this adds is a *second vocabulary* on top of the same mechanism: the
system-level events that are not about one agent turn. A task changing state, a
tool starting, a model being selected, a permission being requested — these
happen whether or not an agent is driving, and both the desktop shell and the
web client want them.

So: one bus (:class:`EventBus`, re-exported), two enumerations. The agent
vocabulary keeps its own transport relay in ``api/websocket.py``; the core
vocabulary gets its own. Neither knows about the other, and both are
transport-agnostic.

The same two rules as the agent bus apply, because they are what makes a
progress display safe to have:

* A subscriber that raises is logged and skipped. It must never be able to abort
  the work that produced the event.
* Events carry **labels and identifiers**, never model reasoning. ``task_log``
  can carry a tool's rendered output because that output is already the thing
  the user is allowed to see.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from common.reporting import safe_error

logger = logging.getLogger("Zaytoon.Core.Events")


class CoreEventType(str, Enum):
    """System-level events.

    Names are dotted and stable because they are a wire contract: the desktop
    shell and the browser both switch on these strings, so renaming one is a
    breaking change to two clients.

    There is a plain :attr:`ERROR` alongside the specific ``*.failed`` events on
    purpose: a client that wants "something went wrong, show a banner" should not
    have to enumerate every way it can go wrong, and a client that wants the
    detail subscribes to the specific one.
    """

    # -- task lifecycle ---------------------------------------------------
    TASK_CREATED = "task.created"
    TASK_UPDATED = "task.updated"
    TASK_STEP = "task.step"
    TASK_LOG = "task.log"

    # -- execution --------------------------------------------------------
    TOOL_STARTED = "tool.started"
    TOOL_OUTPUT = "tool.output"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"

    # -- model ------------------------------------------------------------
    MODEL_STARTED = "model.started"
    MODEL_TOKEN = "model.token"
    MODEL_COMPLETED = "model.completed"
    MODEL_FAILED = "model.failed"

    # -- human in the loop ------------------------------------------------
    PERMISSION_REQUESTED = "permission.requested"
    PERMISSION_RESOLVED = "permission.resolved"

    # -- workspace / registry --------------------------------------------
    FILE_CHANGED = "file.changed"
    PROJECT_CHANGED = "project.changed"
    SKILL_CHANGED = "skill.changed"
    PROVIDER_CHANGED = "provider.changed"

    # -- terminal ---------------------------------------------------------
    TERMINAL_OUTPUT = "terminal.output"

    #: Something failed. The catch-all for a client that only shows a banner.
    ERROR = "error"


#: Events that are lifecycle markers rather than payloads. A client that only
#: wants a task's current phase can subscribe to this subset and ignore
#: everything carrying content.
#:
#: Defined as a module constant rather than an enum member because Python
#: forbids assigning to a member of an Enum class after it is created — and a
#: member *inside* the enum would become a third enumeration value rather than a
#: frozenset, which is a much worse thing to have to explain.
STATUS_EVENTS = frozenset({
    CoreEventType.TASK_CREATED,
    CoreEventType.TASK_UPDATED,
    CoreEventType.TASK_STEP,
    CoreEventType.TOOL_STARTED,
    CoreEventType.TOOL_COMPLETED,
    CoreEventType.TOOL_FAILED,
    CoreEventType.MODEL_STARTED,
    CoreEventType.MODEL_COMPLETED,
    CoreEventType.MODEL_FAILED,
    CoreEventType.PERMISSION_REQUESTED,
    CoreEventType.PERMISSION_RESOLVED,
    CoreEventType.ERROR,
})


@dataclass
class CoreEvent:
    """One system-level observation.

    ``event_id`` and ``ts`` are assigned here, not derived from the payload:
    a client deduplicating a replay needs an id that survives payload edits,
    and a client ordering events it received out of band needs the observation
    time rather than its arrival order. Both travel on the wire (see
    :meth:`to_dict`) so every transport can offer the same envelope without
    knowing the payload's shape.
    """

    type: CoreEventType
    data: Dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    ts: float = field(default_factory=time.time)

    @property
    def is_status(self) -> bool:
        return self.type in STATUS_EVENTS

    def to_dict(self) -> Dict[str, Any]:
        # The envelope wins a key collision: a payload that happened to carry
        # `event_id` or `ts` of its own must not shadow the observation's own
        # identity and time — that would give one event two winners for the
        # same field depending on which the client read first.
        return {
            **self.data,
            "type": self.type.value,
            "event_id": self.event_id,
            "ts": self.ts,
        }


Subscriber = Callable[[CoreEvent], Optional[Any]]


class EventBus:
    """Fan-out for core events.

    Deliberately the same shape as :class:`agents.events.EventBus` — subscribe,
    emit, unsubscribe — so a caller that already knows one knows this. The
    difference is that it is safe to share: :meth:`publish` is fire-and-forget
    and catches everything, so a producer that emits from a synchronous context
    cannot be blocked by a slow socket.
    """

    def __init__(self) -> None:
        self._subscribers: List[Subscriber] = []
        self._recent: List[CoreEvent] = []
        self._recent_limit = 100

    # -- registration -----------------------------------------------------
    def subscribe(self, callback: Subscriber) -> Callable[[], None]:
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass

        return unsubscribe

    def unsubscribe_all(self) -> None:
        self._subscribers.clear()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # -- emission ---------------------------------------------------------
    async def emit(self, event_type: CoreEventType, **data: Any) -> None:
        """Deliver to every subscriber, in registration order."""
        event = CoreEvent(event_type, data)
        self._recent.append(event)
        if len(self._recent) > self._recent_limit:
            self._recent = self._recent[-self._recent_limit :]
        for callback in list(self._subscribers):
            try:
                result = callback(event)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                # A progress display is not the turn. Never let it abort it.
                safe_error(
                    exc,
                    operation="core.events.emit",
                    component="core.events",
                    detail=f"event={event_type.value}",
                )

    def publish(self, event_type: CoreEventType, **data: Any) -> None:
        """Fire-and-forget emit, safe from synchronous code.

        Schedules onto the running loop when there is one and drops the event
        otherwise, so a store update during startup does not raise. Dropping is
        the right call: the durable record is the database row, and this is the
        notification that something changed.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("No running loop; dropping %s", event_type.value)
            return
        loop.create_task(self.emit(event_type, **data))

    # -- inspection -------------------------------------------------------
    def recent(self, limit: int = 20) -> List[CoreEvent]:
        """The last few events. Lets a newly connected client catch up."""
        count = max(1, min(int(limit or 20), self._recent_limit))
        return self._recent[-count:]

    # -- convenience ------------------------------------------------------
    async def task_created(self, task: Dict[str, Any]) -> None:
        await self.emit(CoreEventType.TASK_CREATED, task=task)

    async def task_updated(self, task: Dict[str, Any]) -> None:
        await self.emit(CoreEventType.TASK_UPDATED, task=task)

    async def task_step(self, task_id: int, step: Dict[str, Any]) -> None:
        await self.emit(CoreEventType.TASK_STEP, task_id=task_id, step=step)

    async def task_log(self, task_id: int, level: str, message: str) -> None:
        await self.emit(CoreEventType.TASK_LOG, task_id=task_id, level=level, message=message)

    async def tool_started(self, name: str, task_id: Optional[int] = None, **extra: Any) -> None:
        await self.emit(CoreEventType.TOOL_STARTED, name=name, task_id=task_id, **extra)

    async def tool_completed(
        self, name: str, success: bool, summary: str = "", task_id: Optional[int] = None
    ) -> None:
        await self.emit(
            CoreEventType.TOOL_COMPLETED,
            name=name, success=success, summary=summary, task_id=task_id,
        )

    async def tool_failed(self, name: str, error: str, task_id: Optional[int] = None) -> None:
        await self.emit(CoreEventType.TOOL_FAILED, name=name, error=error, task_id=task_id)

    async def permission_requested(self, **data: Any) -> None:
        await self.emit(CoreEventType.PERMISSION_REQUESTED, **data)

    async def file_changed(self, path: str, action: str) -> None:
        await self.emit(CoreEventType.FILE_CHANGED, path=path, action=action)


class NullBus(EventBus):
    """A bus with no subscribers, for callers that do not want progress."""

    async def emit(self, event_type: CoreEventType, **data: Any) -> None:
        return None

    def publish(self, event_type: CoreEventType, **data: Any) -> None:
        return None


#: The process-wide bus. A module-level singleton so the API layer, the task
#: manager and a WebSocket session all address the same one without threading a
#: bus through every constructor — which is how two clients end up listening to
#: different buses.
bus = EventBus()


def get_bus() -> EventBus:
    return bus


__all__ = [
    "STATUS_EVENTS",
    "CoreEvent",
    "CoreEventType",
    "EventBus",
    "NullBus",
    "bus",
    "get_bus",
]