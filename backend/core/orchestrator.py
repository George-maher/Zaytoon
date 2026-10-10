"""The orchestrator: one entry point for "do this work", whatever it is.

Today, a turn is driven from ``api/websocket.py``: it detects language, routes
the intent, calls an agent, streams tokens, synthesises speech. That sequence
is correct but it lives in the transport, which means three things it should not:

* **The task system cannot see it.** There was no place to record which task a
  turn belonged to, so ``TaskManager`` would have to be bolted on from outside.
* **A second entry point would be a second copy.** A REST ``POST /tasks`` and a
  WebSocket message both wanting to run a turn means the sequence gets written
  twice, and the two copies drift.
* **Progress reporting is per-call-site.** The caller passes a callback; a
  caller that forgets one gets silence rather than a visible defect.

So the sequence moves here, and the transport becomes a thin adapter: it supplies
the socket, this module owns the work. Both entry points call the same function,
which is what makes the desktop shell and the browser genuinely two views of
one system rather than two clients with a shared backend.

**Nothing here decides what to do.** Routing, planning and execution stay in the
agents and the tool registry. This module sequences them and keeps the record.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from arabic import clean_and_detect
from common.reporting import client_error, safe_error
from core.events import CoreEventType, bus
from core.tasks.manager import TaskManager, get_manager
from core.tasks.models import Task, TaskStatus

logger = logging.getLogger("Zaytoon.Core.Orchestrator")

#: How a caller receives user-facing text as it is produced.
StreamCallback = Callable[[str], Awaitable[None]]

#: How a caller receives a semantic progress event.
EventCallback = Callable[[str, Dict[str, Any]], Awaitable[None]]


class Orchestrator:
    """Runs a request end to end and keeps the task record."""

    def __init__(
        self,
        agent_service,
        router_agent,
        memory_agent=None,
        task_manager: Optional[TaskManager] = None,
        event_bus=None,
    ) -> None:
        # Injectable, and not always the module singleton: the task manager is
        # constructed with it, so a caller that supplied a different bus would
        # otherwise find task events going to a bus it is not listening to.
        self._bus = event_bus or bus
        self.agents = agent_service
        self.router = router_agent
        self.memory = memory_agent
        # The manager is constructed **with this orchestrator's bus**. Handing it
        # the module singleton instead is a real bug, not a tidiness point: task
        # events are emitted by the manager, so a different bus would mean the
        # task lifecycle is invisible to anyone listening on the orchestrator's
        # — including a transport that subscribed correctly.
        self.tasks = task_manager or TaskManager(event_bus=self._bus)
        self._active: Dict[str, asyncio.Event] = {}
        self._handles: Dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------
    def cancel_session(self, session_id: str) -> bool:
        """Cancel whatever this session is doing.

        Three things happen, and each covers a case the other two miss:

        1. the session's cancel event is set, which is what the work checks
           between steps and what `is_cancelled` reports;
        2. the asyncio handle is cancelled, which is what actually interrupts a
           long await;
        Returns whether anything was cancelled.

        Note what this does **not** do: it does not touch the task record.
        Cancelling the handle makes the turn raise ``CancelledError``, and the
        turn's own handler is what marks the task cancelled — so the record and
        the work cannot disagree about what happened, which is what cancelling
        the record from here would risk.
        """
        # The event is **created** if absent, so a cancel that races a turn's
        # first await still records the intent rather than being dropped — the
        # user would otherwise watch it keep going.
        #
        # `existed` is what the return value is built from, not "is there an
        # event now": an event *this call* created says nothing about whether
        # anything was running, and reporting success for it would mean the
        # transport answers "stopped" about a session that never existed.
        existed = session_id in self._active
        event = self._active.get(session_id)
        if event is None:
            event = asyncio.Event()
            self._active[session_id] = event
        event.set()

        handle = self._handles.get(session_id)
        cancelled = handle is not None and not handle.done()
        if cancelled:
            handle.cancel()

        return cancelled or existed

    def cancel_event_for(self, session_id: str) -> asyncio.Event:
        """The session's cancel event, created on first use."""
        event = self._active.get(session_id)
        if event is None:
            event = asyncio.Event()
            self._active[session_id] = event
        return event

    def is_cancelled(self, session_id: str) -> bool:
        event = self._active.get(session_id)
        return bool(event and event.is_set())

    def _reset(self, session_id: str) -> asyncio.Event:
        """Start a new turn for a session with a fresh cancel event."""
        self._active.pop(session_id, None)
        return self.cancel_event_for(session_id)

    # ------------------------------------------------------------------
    # The turn
    # ------------------------------------------------------------------
    async def run_turn(
        self,
        message: str,
        session_id: str,
        *,
        language_hint: Optional[str] = None,
        project_id: Optional[str] = None,
        active_file: Optional[str] = None,
        stream_callback: Optional[StreamCallback] = None,
        event_callback: Optional[EventCallback] = None,
        confirmation_manager: Optional[Any] = None,
        event_bus: Optional[Any] = None,
        response_id: Optional[str] = None,
        cancel_event: Optional[asyncio.Event] = None,
        create_task: bool = True,
        task: Optional[Task] = None,
    ) -> "TurnResult":
        """Run one request: understand, route, act, record.

        ``create_task=False`` runs it without a task record — used for a greeting
        or a chit-chat, where a "task" would be noise in the task list. The
        parameter is explicit rather than inferred from the intent, because the
        caller knows whether the user asked for *work* or *conversation*, and a
        heuristic here would be wrong often enough to be annoying.

        ``task=`` runs against an **existing** record instead of creating one —
        ``POST /tasks/{id}/run`` re-executes the request the record already
        holds, and a retry's freshly created child is bound the same way. The
        record must be non-terminal (a finished one is refused with an
        ``error`` the caller can surface, so a second completion can never be
        stamped over the first). Its status advances only through moves the
        transition table allows: a record parked mid-flight — ``EXECUTING``
        left by a process that died, ``WAITING_*`` for someone who never came
        — is re-entered where the rest of the sequence is legal from, rather
        than forced backwards. When ``task`` is given, ``create_task`` is
        ignored; the record already exists.

        Transport-facing parameters, each because a transport has something the
        core does not:

        * ``confirmation_manager`` — the human-in-the-loop gate lives with the
          socket (one per session, resolved by its ``tool_confirm`` reply), so
          the turn must hand it to the dispatcher or every gated tool stalls.
        * ``event_bus`` — a transport's bus already owns its wire format (the
          WebSocket's ``_ProgressRelay`` emits ``tool_started`` frames). When
          given, it *replaces* the default agent→core bridge, so one event
          class has exactly one path to the client and nothing renders twice.
          When not given, the bridge is used, as before.
        * ``response_id`` — the id every frame of this turn is keyed by. The
          WebSocket allocates it in its session state *before* the task exists;
          defaulting to the task id keeps other callers unchanged.
        * ``cancel_event`` — a transport-owned event (the session state's) is
          **adopted**: it replaces this session's event in the orchestrator, so
          ``interrupt``, ``cancel_session``, ``is_cancelled`` and this turn's
          own checks all read one source of truth. Two events would mean an
          interrupt that stopped the rendering while the task record happily
          completed — a record that disagrees with what the user saw.
        """
        started = time.perf_counter()
        internal_event = self._reset(session_id)
        if cancel_event is None:
            cancel_event = internal_event
        else:
            # Synchronous on purpose: no ``await`` sits between ``_reset``'s
            # pop and this assignment, so a cancel racing the turn's first
            # await cannot land in the gap and be overwritten.
            self._active[session_id] = cancel_event

        cleaning = clean_and_detect(message)
        text = cleaning["text"]
        language = language_hint or cleaning["language"]
        # Whisper's verdict beats the text-based sniff, matching the WebSocket
        # handler's existing precedence.
        if language_hint in ("ar", "en"):
            language = language_hint

        if project_id is None:
            project_id = await self._default_project_id()

        if task is not None and task.status.is_terminal:
            # Refused here as well as at the route, so a caller that forgets
            # the check gets an error result instead of a second completion
            # stamped over the first — terminal states are refused
            # transitions precisely because the history they hold is the
            # thing a retry must not erase.
            return TurnResult(
                intent="chat",
                language=language,
                task=task,
                error="This task has already finished. Retry it to run it again.",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        if task is None and create_task:
            try:
                task = await self.tasks.create(
                    title=_title_for(text),
                    request=text,
                    language=language,
                    session_id=session_id,
                    project_id=project_id,
                )
                await self._notify(event_callback, "task_created", task.to_dict())
            except Exception as exc:  # noqa: BLE001 - a record is bookkeeping
                # Same class as a failing audit write: a turn must not die
                # because its own paper trail could not be written. The
                # failure is logged and the turn runs without a record.
                safe_error(exc, operation="orchestrator.task.create",
                           component="core.orchestrator")
                task = None
        if task is not None:
            try:
                # A bound record may sit parked mid-flight — EXECUTING left
                # by a process that died, WAITING_* for someone who never
                # came. Every non-terminal parked state can reach EXECUTING,
                # and EXECUTING can continue the normal chain, so the re-run
                # starts where the rest of the sequence is legal from.
                # RECEIVED and ANALYZING are the chain's own starting points
                # and are left alone.
                if task.status in (
                    TaskStatus.EXECUTING, TaskStatus.VERIFYING,
                    TaskStatus.WAITING_FOR_PERMISSION, TaskStatus.WAITING_FOR_USER,
                ):
                    await self._advance(task, TaskStatus.EXECUTING)
                await self._advance(task, TaskStatus.ANALYZING)
            except Exception as exc:  # noqa: BLE001 - a record is bookkeeping
                safe_error(exc, operation="orchestrator.task.begin",
                           component="core.orchestrator")
                task = None

        if cancel_event.is_set():
            return await self._cancelled(task, session_id, started)

        # Pause checkpoint 1: before the turn invests anything in routing.
        # The flag is read from the row, not from this copy — see
        # `_paused_here` for why an in-memory read would miss the request.
        if await self._paused_here(task):
            return TurnResult(
                intent="chat",
                language=language,
                task=task,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        try:
            # The preamble is assembled before routing, not after: "open my
            # project" and "run the tests" are routing decisions that depend on
            # what the project *is*. An agent that routes first and is told
            # afterwards would have to answer before it had been told.
            #
            # It is assembled per turn and never stored, so a stale project
            # profile or a since-disabled skill cannot leak into a later turn.
            preamble = await self.build_preamble(project_id, active_file)
            if preamble:
                text = f"{preamble}\n\n{text}"

            context = await self._context(session_id)
            route_result = await self.router.route(text, context)
            intent = route_result.get("intent", "chat")
            if task is not None:
                task.intent = intent
                await self._advance(task, TaskStatus.PLANNING)
            await self._notify(event_callback, "intent", {"intent": intent})

            if cancel_event.is_set():
                return await self._cancelled(task, session_id, started)

            # Pause checkpoint 2: routing is done and nothing has run yet —
            # the meaningful place for a background run to be stopped. A
            # pause that lands *during* dispatch cannot un-run what dispatch
            # has already done, so the turn then finishes truthfully; the
            # flag means "suspend progress", not "roll back".
            if await self._paused_here(task):
                return TurnResult(
                    intent=intent,
                    language=language,
                    task=task,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )

            async def _stream(token: str) -> None:
                if stream_callback is not None:
                    await stream_callback(token)

            # The recorder rides **whichever** bus the dispatch is given: a
            # transport that supplied its own relay (the WebSocket does) must
            # not lose the task's own bookkeeping, and subscribing here rather
            # than inside the default bridge is what makes one path cover both
            # cases. It is unsubscribed in the same `finally` — a transport bus
            # outlives this turn, and a stale recorder would attribute the
            # *next* turn's tools to this task.
            agent_bus = event_bus if event_bus is not None else _bus_with_bus()
            off_record = agent_bus.subscribe(self._tool_recorder(task))
            try:
                reply = await self.agents.dispatch(
                    intent,
                    text,
                    # The same list the router saw. Fetched twice would mean two
                    # database reads per turn, and — worse — two chances for the
                    # memory agent to have pruned between them, so the agent could
                    # be given a *different* history than the one the route was
                    # chosen from.
                    context,
                    language,
                    session_id,
                    stream_callback=_stream,
                    event_bus=agent_bus,
                    response_id=(
                        response_id if response_id is not None
                        else (task.id if task else "")
                    ),
                    confirmation_manager=confirmation_manager,
                )
            finally:
                off_record()
            reply = reply or ""

            if cancel_event.is_set():
                # The reply arrived after the user interrupted. The transport
                # discards it — and the record must not claim completion for
                # something nobody saw. This is the only place that can set the
                # record straight: by here the dispatch has already returned.
                logger.info("Turn for %s cancelled after dispatch", session_id)
                if task is not None and not task.status.is_terminal:
                    await self.tasks.transition(task, TaskStatus.CANCELLED)
                    await self._notify(event_callback, "task_updated", task.to_dict())
                return TurnResult(
                    reply=reply,
                    intent=intent,
                    language=language,
                    task=task,
                    cancelled=True,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )

            if task is not None:
                if task.status.is_active or task.status is TaskStatus.PLANNING:
                    await self._advance(task, TaskStatus.VERIFYING)
                task.result = reply
                await self._advance(task, TaskStatus.COMPLETED, result=reply)
                await self._notify(event_callback, "task_updated", task.to_dict())

            return TurnResult(
                reply=reply,
                intent=intent,
                language=language,
                task=task,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        except asyncio.CancelledError:
            if task is not None and not task.status.is_terminal:
                await self.tasks.transition(task, TaskStatus.CANCELLED)
            raise
        except Exception as exc:  # noqa: BLE001 - the turn boundary
            safe_error(exc, operation="orchestrator.turn", component="core.orchestrator")
            if task is not None and not task.status.is_terminal:
                task.error = client_error(exc, operation="turn", component="core.orchestrator",
                                         language=language)
                await self.tasks.transition(task, TaskStatus.FAILED)
                await self._notify(event_callback, "task_updated", task.to_dict())
            return TurnResult(
                reply="",
                intent="chat",
                language=language,
                task=task,
                error=task.error if task else "Something went wrong.",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        finally:
            self._active.pop(session_id, None)

    async def run_turn_in_session(
        self, session_id: str, message: str, **kwargs
    ) -> "asyncio.Task":
        """Schedule a turn and register its handle so it can be cancelled.

        **This is the entry point a transport should use.** :meth:`cancel_session`
        reaches work by cancelling a registered handle, so a turn started any
        other way has nothing for it to cancel — the event is set but a long
        await inside the agent keeps running.
        """
        handle = asyncio.create_task(self.run_turn(message, session_id, **kwargs))
        self._handles[session_id] = handle
        handle.add_done_callback(
            lambda _: self._handles.pop(session_id, None)
            if self._handles.get(session_id) is handle
            else None
        )
        return handle

    async def cancel_session_task(self, session_id: str) -> bool:
        """Cancel the session's work **and** its active task record.

        What the WebSocket ``task`` message calls. The two halves cover
        different realities: :meth:`cancel_session` stops a turn that is running
        *now*, while a task can outlive its turn — a test suite can still be
        running after the answer was delivered — and stopping the turn alone
        would orphan it.
        """
        from core.tasks import get_manager as task_manager

        stopped = self.cancel_session(session_id)
        task = await task_manager().active_for_session(session_id)
        if task is not None:
            await task_manager().cancel(task, "cancelled by the user")
            stopped = True
        return stopped

    def _tool_recorder(self, task: Optional[Task]):
        """An observer for what this turn's tools actually did.

        Subscribed to the agent bus the dispatch is given, so it sees every
        tool execution whether the transport supplied its own relay or the
        default bridge is used. Two jobs, in order:

        * **Every completed call is recorded against the task**
          (``tools_used``) — without it the record stays empty for ad-hoc
          tool turns, which are the common case.
        * **A tool that declares ``mutates_files`` also records each
          path-shaped argument**, which is what emits ``file.changed`` — the
          event behind the file panel's "changed by the assistant" marker.
          The declaration travels with the event (see the engine), so this
          side never needs the registry and never re-derives a name list
          that could disagree with the registered aliases.

        Bookkeeping, both halves: a failure is logged and swallowed, and the
        two are guarded separately so one failing cannot skip the other. (The
        bus already promises not to let a subscriber abort the turn — this
        keeps the *second* phase running when the first one had a bad day.)
        """

        async def _record(event) -> None:
            from agents.events import AgentEventType

            if event.type is not AgentEventType.TOOL_FINISHED:
                return
            data = event.data or {}
            name = str(data.get("name") or "")
            if task is not None and name:
                # Every completed call counts as *used*, success or not —
                # `finish_step` already records a failed step's tool, and
                # "the model asked, the user declined" is part of what
                # happened. The file change below is the part that must not
                # be a lie, so it alone demands success.
                try:
                    await self.tasks.note_tool(task, name)
                except Exception as exc:  # noqa: BLE001 - bookkeeping
                    safe_error(
                        exc, operation="orchestrator.note_tool",
                        component="core.orchestrator",
                    )
            if not data.get("success") or not data.get("mutates"):
                return
            for path in _changed_paths(data.get("params")):
                try:
                    if task is not None:
                        await self.tasks.note_file_change(task, path)
                    else:
                        # No record to attach it to, but the UI's indicator
                        # still has a right to know.
                        await self._bus.file_changed(path, "changed")
                except Exception as exc:  # noqa: BLE001 - bookkeeping
                    safe_error(
                        exc, operation="orchestrator.note_file_change",
                        component="core.orchestrator",
                    )

        return _record

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _advance(self, task: Task, target: TaskStatus, **extra: Any) -> bool:
        """Move ``task`` toward ``target`` when the transition table allows it.

        Returns whether the move happened. The table is the authority: this
        never forces a move and never raises on one that cannot happen,
        because a record being *re-run* legitimately sits at a stage the
        fresh-task sequence would not — ``X → X`` after a pause, or
        ``PLANNING → ANALYZING`` for a record that already planned — and
        crashing the turn for the record's own history would punish the
        caller, not the bug. Illegal moves are logged at debug with both
        endpoints so a genuine table mistake is still visible.
        """
        if task.status is target:
            return False
        if not task.status.can_transition_to(target):
            logger.debug(
                "Task %s stays %s; %s is not a legal move",
                task.id, task.status.value, target.value,
            )
            return False
        await self.tasks.transition(task, target, **extra)
        return True

    async def _paused_here(self, task: Optional[Task]) -> bool:
        """Whether the record is paused — read from the row, not from the object.

        ``task.paused`` on *this* copy would miss the request: ``pause()`` may
        have been called through the REST singleton on a different Python
        object for the same row, and neither copy's assignment reaches the
        other. The row is the one place both instances can agree. A failing
        read returns False — pause is a progress policy, not a safety gate,
        and a database hiccup must not abort the turn that merely asked whether
        it should stop.
        """
        if task is None or task.id is None:
            return False
        try:
            paused = await self.tasks.is_paused(task.id)
        except Exception as exc:  # noqa: BLE001 - see the docstring
            logger.debug(
                "Pause check failed for task %s: %s", task.id, type(exc).__name__
            )
            return False
        if not paused:
            return False
        try:
            await self.tasks.log(
                task, "Paused: the turn stopped here before dispatching.",
                level="warning",
            )
        except Exception as e:  # noqa: BLE001 - the log explains, it does not decide
            safe_error(e, operation="orchestrator.pause_log", component="Orchestrator")
        return True

    async def _default_project_id(self) -> Optional[str]:
        """The project the user opened last — the one they are working in.

        Not a guess about intent: ``projects.recent(1)`` is ordered by
        ``last_opened_at``, which only *opening* a project updates, so a
        project merely detected still sits behind one the user chose. The
        preamble is labelled with the project's name and path, so a turn about
        something else can be told it is looking at the wrong context rather
        than being silently wrong. Callers with a better answer pass
        ``project_id`` explicitly.
        """
        try:
            from core.projects import recent

            found = await recent(1)
        except Exception as exc:  # noqa: BLE001 - context is an optimisation
            logger.debug("Could not resolve the default project: %s", type(exc).__name__)
            return None
        return found[0]["id"] if found else None

    async def _context(self, session_id: str) -> List[Dict[str, Any]]:
        if self.memory is None:
            return []
        try:
            return await self.memory.get_recent_context(session_id=session_id) or []
        except Exception as exc:  # noqa: BLE001 - context is an optimisation
            logger.debug("Could not load context: %s", type(exc).__name__)
            return []

    async def build_preamble(
        self, project_id: Optional[str] = None, active_file: Optional[str] = None
    ) -> str:
        """The context block plus enabled skill instructions.

        Assembled fresh every turn, in that order: what this project *is*
        (detected facts) before what the user has *installed* (opinions). Both
        are labelled so the model can tell a detected default from an
        instruction, which is the difference between correcting a wrong test
        command and obeying a wrong one.
        """
        from core.projects import build_context, get
        from core.skills import build_skill_context

        blocks: List[str] = []
        if project_id:
            project = await get(project_id)
            if project:
                block = build_context(project, active_file=active_file)
                if block:
                    blocks.append(block)
        if active_file and not project_id:
            blocks.append(f"Currently open file: {active_file}")

        try:
            skills = await build_skill_context()
        except Exception as exc:  # noqa: BLE001 - skills must not break a turn
            logger.warning("Could not build the skill context: %s", type(exc).__name__)
            skills = ""
        if skills:
            blocks.append(skills)
        return "\n\n".join(blocks)

    async def _notify(self, callback: Optional[EventCallback], kind: str, payload: Dict[str, Any]) -> None:
        if callback is None:
            return
        try:
            await callback(kind, payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a display must not break a turn
            logger.warning("Progress callback failed: %s", type(exc).__name__)

    async def _cancelled(self, task: Optional[Task], session_id: str, started: float) -> "TurnResult":
        logger.info("Turn for %s cancelled before dispatch", session_id)
        if task is not None and not task.status.is_terminal:
            # The record must not sit in ANALYZING forever: a cancel that
            # arrives before dispatch is still a cancelled turn, and a task
            # panel showing "Understanding…" for a stopped request is a lie
            # the user cannot act on.
            await self.tasks.transition(task, TaskStatus.CANCELLED)
        return TurnResult(
            reply="",
            intent="chat",
            language="en",
            task=task,
            cancelled=True,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )


@dataclass
class TurnResult:
    """What one turn produced."""

    reply: str = ""
    intent: str = "chat"
    language: str = "en"
    task: Optional[Task] = None
    error: Optional[str] = None
    cancelled: bool = False
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return not self.error and not self.cancelled

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reply": self.reply,
            "intent": self.intent,
            "language": self.language,
            "error": self.error,
            "cancelled": self.cancelled,
            "duration_ms": self.duration_ms,
            "task_id": self.task.id if self.task else None,
        }


#: Argument names that carry a filesystem path. A mutating tool's change is
#: read from the call's own arguments rather than from a per-tool rule, so a
#: tool that never takes a path cannot announce one and ``file_move``'s
#: destination is recorded alongside its source.
_PATH_ARGS = ("path", "file", "target", "source", "destination")


def _changed_paths(params: Any) -> List[str]:
    """Distinct path-shaped strings from a tool call's arguments."""
    if not isinstance(params, dict):
        return []
    out: List[str] = []
    for key in _PATH_ARGS:
        value = params.get(key)
        if isinstance(value, str) and value.strip() and value not in out:
            out.append(value)
    return out


def _bus_with_bus():
    """The agent-facing event bus.

    The agent stack emits through ``agents.events.EventBus``, and the core bus
    is a different object with a different vocabulary. This bridges them so a
    tool call the agent performs reaches the core bus as a ``tool.started`` /
    ``tool.completed`` pair — which is what the task view renders — without the
    agent having to know the core exists.
    """
    from agents.events import AgentEventType, EventBus

    relay = EventBus()

    async def _forward(event) -> None:
        kind = event.type
        data = event.data or {}
        if kind is AgentEventType.TOOL_STARTED:
            await bus.emit(
                CoreEventType.TOOL_STARTED,
                name=data.get("name", ""),
                label=data.get("label", ""),
            )
        elif kind is AgentEventType.TOOL_FINISHED:
            await bus.emit(
                CoreEventType.TOOL_COMPLETED,
                name=data.get("name", ""),
                success=bool(data.get("success")),
                summary=str(data.get("summary", ""))[:200],
            )
        elif kind is AgentEventType.TOOL_PROGRESS:
            await bus.emit(
                CoreEventType.TOOL_OUTPUT,
                name=data.get("name", ""),
                message=str(data.get("message", ""))[:400],
            )
        elif kind is AgentEventType.ERROR:
            await bus.emit(
                CoreEventType.MODEL_FAILED,
                error=str(data.get("content", ""))[:300],
            )

    relay.subscribe(_forward)
    return relay


def _title_for(text: str) -> str:
    """A short, human title for a task row.

    The first line, trimmed. A task titled with a truncated sentence is ugly; a
    task titled with an LLM-generated summary is a second model call for
    something the user's own words already answer.
    """
    first = (text or "").strip().splitlines()[0] if (text or "").strip() else "Task"
    return first[:80] + ("…" if len(first) > 80 else "")


__all__ = ["EventCallback", "Orchestrator", "StreamCallback", "TurnResult"]