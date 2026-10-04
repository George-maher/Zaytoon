"""The agent's event vocabulary, and a bus to carry it.

The agent used to reach the transport directly: ``ToolAgent.process`` took a
``stream_callback`` and pushed whatever the model produced at the user. Since
the model is asked for JSON, that meant the **raw tool call was streamed to the
user as if it were the answer** — ``{"action":"open_app","app":"spotify"}``
typed out character by character, then overwritten by the real reply. It also
meant there was no way to express "planning" or "running tool X" at all.

This module fixes that structurally:

* The agent emits **events**, never raw deltas. A caller that does not care
  about progress subscribes to nothing and gets the final string.
* Only :attr:`AgentEventType.RESPONSE_CHUNK` is user-facing prose. Everything
  else is a status. That is what keeps private reasoning off the wire: the
  model's intermediate output is a tool call, and a tool call is not a chunk.
* The transport decides the wire format. The agent does not know whether it is
  speaking to a browser or the desktop shell.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional

from common.reporting import safe_error

logger = logging.getLogger("Zaytoon.Events")


class AgentEventType(str, Enum):
    """Everything the agent can report.

    These map onto the WebSocket protocol in ``api/websocket.py``; the transport
    is the only place that knows the wire names.
    """

    #: The request was received and is being classified.
    THINKING = "thinking"
    #: A multi-step approach was formed. Carries a user-facing label only.
    PLANNING = "planning"
    #: A tool is about to run. Carries the tool name and a display label.
    TOOL_STARTED = "tool_started"
    #: Progress within a long-running tool, e.g. a subprocess line.
    TOOL_PROGRESS = "tool_progress"
    #: A tool finished, successfully or not.
    TOOL_FINISHED = "tool_finished"
    #: **User-facing prose.** The only event that becomes visible text.
    RESPONSE_CHUNK = "response_chunk"
    #: A risky action is waiting on a human.
    CONFIRMATION_REQUIRED = "confirmation_required"
    #: Something failed.
    ERROR = "error"
    #: The turn is over.
    COMPLETED = "completed"


#: Events that represent *status*, not content. A transport that only wants to
#: show a spinner can listen for these and ignore everything else.
STATUS_EVENTS = frozenset({
    AgentEventType.THINKING,
    AgentEventType.PLANNING,
    AgentEventType.TOOL_STARTED,
    AgentEventType.TOOL_PROGRESS,
    AgentEventType.TOOL_FINISHED,
    AgentEventType.ERROR,
    AgentEventType.COMPLETED,
})


@dataclass
class AgentEvent:
    """One observation from the agent."""

    type: AgentEventType
    data: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_user_facing(self) -> bool:
        return self.type is AgentEventType.RESPONSE_CHUNK

    def to_dict(self) -> Dict[str, Any]:
        return {"type": self.type.value, **self.data}


Subscriber = Callable[[AgentEvent], Optional[Awaitable[None]]]


class EventBus:
    """Fan-out for agent events.

    Deliberately tiny and deliberately forgiving:

    * A subscriber that raises is logged and skipped. A progress display must
      never be able to abort the turn that produced it.
    * Subscribers are called in registration order and awaited, so a transport
      can rely on ordering when writing to a single socket.
    * :meth:`emit` never blocks the agent on a slow consumer beyond the await
      itself; there is no queue to overflow.
    """

    def __init__(self) -> None:
        self._subscribers: List[Subscriber] = []

    def subscribe(self, callback: Subscriber) -> Callable[[], None]:
        """Register ``callback``. Returns a function that unsubscribes it."""
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

    async def emit(self, event_type: AgentEventType, **data: Any) -> None:
        """Deliver an event to every subscriber."""
        if not self._subscribers:
            return
        event = AgentEvent(event_type, data)
        for callback in list(self._subscribers):
            try:
                result = callback(event)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # A subscriber is a display, not the turn: it must never be
                # able to abort the work that produced the event, so the
                # failure is reported and swallowed.
                safe_error(
                    e, operation="emit", component="EventBus",
                    detail=f"event={event_type.value}",
                )

    async def emit_many(self, events: List[AgentEvent]) -> None:
        for event in events:
            await self.emit(event.type, **event.data)

    # -- convenience -----------------------------------------------------
    async def thinking(self, **data: Any) -> None:
        await self.emit(AgentEventType.THINKING, **data)

    async def planning(self, steps: Optional[List[str]] = None, **data: Any) -> None:
        """Announce a plan.

        ``steps`` must be **user-facing labels** ("Create the project folder"),
        never the model's reasoning. There is a test asserting that.
        """
        await self.emit(AgentEventType.PLANNING, steps=list(steps or []), **data)

    async def tool_started(self, name: str, label: str = "", **data: Any) -> None:
        await self.emit(AgentEventType.TOOL_STARTED, name=name, label=label, **data)

    async def tool_progress(self, name: str, message: str = "", **data: Any) -> None:
        await self.emit(AgentEventType.TOOL_PROGRESS, name=name, message=message, **data)

    async def tool_finished(
        self, name: str, success: bool, summary: str = "", **data: Any
    ) -> None:
        await self.emit(
            AgentEventType.TOOL_FINISHED,
            name=name, success=success, summary=summary, **data,
        )

    async def response_chunk(self, text: str, **data: Any) -> None:
        """User-facing prose. The only event that becomes visible text."""
        if text:
            await self.emit(AgentEventType.RESPONSE_CHUNK, content=text, **data)

    async def error(self, message: str, **data: Any) -> None:
        await self.emit(AgentEventType.ERROR, content=message, **data)

    async def completed(self, **data: Any) -> None:
        await self.emit(AgentEventType.COMPLETED, **data)


class NullBus(EventBus):
    """A bus with no subscribers. Used when a caller does not want progress.

    Cheaper than branching on ``if bus is not None`` at every call site.
    """

    def __init__(self) -> None:
        super().__init__()

    async def emit(self, event_type: AgentEventType, **data: Any) -> None:
        return None
