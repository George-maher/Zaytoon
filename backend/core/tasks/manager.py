"""The task manager: lifecycle, cancellation and progress, in one place.

Every state change in the product goes through :meth:`TaskManager.transition`.
Not for tidiness — for two reasons that turned out to matter:

* **The event fires exactly once.** If the WebSocket relay, the database write
  and the client notification each lived at the call sites, a caller that
  forgot one would produce a task whose UI and database disagree. A test asserts
  a transition emits exactly one event.
* **Cancellation is reachable.** A task holds the handle to its own work, so
  ``POST /tasks/{id}/cancel`` works on a task created by a WebSocket turn, not
  only on one a REST caller started. The manager's ``register_handle`` is what
  connects an ``asyncio.Task`` to a task row.

The manager is also where the **progress floor** lives. A status change sets a
minimum progress, and steps refine it upwards, so the bar always moves forward
and never rewinds when a plan is revised.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from common.reporting import safe_error
from core.events import CoreEventType, bus
from core.tasks import store
from core.tasks.models import (
    IllegalTransition,
    Task,
    TaskStatus,
    TaskStepStatus,
)

logger = logging.getLogger("Zaytoon.Core.Tasks.Manager")

#: Log levels accepted by :meth:`TaskManager.log`, mapped for display.
LOG_LEVELS = ("debug", "info", "warning", "error")


class TaskManager:
    """Owns the live task table and every transition into it."""

    def __init__(self, event_bus=None) -> None:
        self._bus = event_bus or bus
        #: task id → the asyncio task driving it, so cancel() can reach it.
        self._handles: Dict[int, "asyncio.Task"] = {}

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------
    async def create(
        self,
        title: str,
        request: str = "",
        *,
        intent: Optional[str] = None,
        language: Optional[str] = None,
        project_id: Optional[str] = None,
        session_id: Optional[str] = None,
        parent_task_id: Optional[int] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Task:
        """Create and persist a task in ``RECEIVED``."""
        task = Task(
            title=(title or "Task").strip()[:200],
            request=(request or "")[:4000],
            intent=intent,
            language=language,
            project_id=project_id,
            session_id=session_id,
            parent_task_id=parent_task_id,
            provider=provider,
            model=model,
        )
        task.id = await store.create(task)
        if task.id is None:
            logger.warning("Task row could not be created; continuing in memory")
        await self._bus.emit(CoreEventType.TASK_CREATED, task=task.to_dict())
        return task

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def transition(self, task: Task, target: TaskStatus, **extra: Any) -> Task:
        """Move ``task`` to ``target``.

        Raises :class:`IllegalTransition` on an illegal move. The alternative —
        writing the status anyway — was rejected because a task log that
        misreports its own history is worse than an error.
        """
        previous = task.status
        task.transition(target)

        if task.status is TaskStatus.COMPLETED:
            task.progress = 1.0
        elif task.status.is_terminal:
            # Failed/cancelled still *finished*; the bar reaches the end and the
            # status says why. Freezing it at the failure point reads as a hang.
            task.progress = 1.0
        else:
            task.refresh_progress()

        for key, value in extra.items():
            if hasattr(task, key) and value is not None:
                setattr(task, key, value)

        if task.id is not None:
            await store.update(task)
            if task.steps:
                await store.replace_steps(task.id, task.steps)
            # update() no longer carries `paused` (see store.update), so this
            # copy's flag may be stale — pause() could have landed through REST
            # mid-turn. Refresh before emitting, or the event would tell the
            # client the pause had been lifted when only this copy forgot it.
            try:
                task.paused = await store.is_paused(task.id)
            except Exception as e:  # noqa: BLE001 - payload accuracy, not correctness
                safe_error(e, operation="tasks.paused_refresh", component="TaskManager")

        await self._bus.emit(
            CoreEventType.TASK_UPDATED,
            task=task.to_dict(),
            previous=previous.value,
        )
        logger.info(
            "Task %s %s -> %s (%d%%)",
            task.id, previous.value, task.status.value, int(task.progress * 100),
        )
        return task

    async def set_plan(self, task: Task, steps: List[str]) -> Task:
        """Record the plan as steps. Labels only — never model reasoning."""
        task.set_plan(steps)
        if task.id is not None:
            await store.replace_steps(task.id, task.steps)
            await store.update(task)
        await self._bus.emit(CoreEventType.TASK_STEP, task_id=task.id, step=None,
                             plan=list(task.plan))
        return task

    async def start_step(
        self, task: Task, seq: int, tool: Optional[str] = None, detail: Optional[str] = None
    ) -> Optional[Any]:
        """Mark one step running. Returns the step, or ``None`` if out of range."""
        step = task.step(seq)
        if step is None:
            return None
        step.status = TaskStepStatus.RUNNING
        step.tool = tool
        step.detail = detail
        step.started_at = _now()
        task.refresh_progress()
        if task.id is not None:
            await store.upsert_step(task.id, step)
            await store.update(task)
        await self._bus.emit(CoreEventType.TASK_STEP, task_id=task.id, step=step.to_dict())
        return step

    async def finish_step(
        self,
        task: Task,
        seq: int,
        success: bool = True,
        detail: Optional[str] = None,
        tool: Optional[str] = None,
    ) -> Optional[Any]:
        """Mark one step done or failed."""
        step = task.step(seq)
        if step is None:
            return None
        step.status = TaskStepStatus.DONE if success else TaskStepStatus.FAILED
        step.ended_at = _now()
        if detail:
            step.detail = detail
        if tool:
            step.tool = tool
        task.refresh_progress()
        if task.id is not None:
            await store.upsert_step(task.id, step)
            await store.update(task)
            if tool:
                await store.record_tool(task.id, tool)
        await self._bus.emit(CoreEventType.TASK_STEP, task_id=task.id, step=step.to_dict())
        return step

    async def skip_step(self, task: Task, seq: int, reason: str) -> Optional[Any]:
        """Mark a step skipped — used when a dependency failed."""
        step = task.step(seq)
        if step is None:
            return None
        step.status = TaskStepStatus.SKIPPED
        step.detail = reason
        step.ended_at = _now()
        task.refresh_progress()
        if task.id is not None:
            await store.upsert_step(task.id, step)
            await store.update(task)
        await self._bus.emit(CoreEventType.TASK_STEP, task_id=task.id, step=step.to_dict())
        return step

    # ------------------------------------------------------------------
    # Instrumentation
    # ------------------------------------------------------------------
    async def log(self, task: Task, message: str, level: str = "info") -> None:
        """Append a log line and emit the event. Never raises."""
        text = str(message or "")[:2000]
        if not text.strip():
            return
        normalised = level if level in LOG_LEVELS else "info"
        if task.id is not None:
            await store.append_log(task.id, normalised, text)
        task.logs.append({"level": normalised, "message": text})
        await self._bus.emit(
            CoreEventType.TASK_LOG,
            task_id=task.id,
            level=normalised,
            message=text,
        )

    async def note_file_change(self, task: Task, path: str) -> None:
        """Record that a file changed as part of this task."""
        if not path:
            return
        if path not in task.files_changed:
            task.files_changed.append(path)
        if task.id is not None:
            await store.record_file_change(task.id, path)
        await self._bus.emit(
            CoreEventType.FILE_CHANGED, path=path, task_id=task.id, action="changed"
        )

    async def note_tool(self, task: Task, tool: str) -> None:
        """Record a tool invocation against the task."""
        if not tool:
            return
        if tool not in task.tools_used:
            task.tools_used.append(tool)
        if task.id is not None:
            await store.record_tool(task.id, tool)
        await self._bus.emit(CoreEventType.TOOL_STARTED, name=tool, task_id=task.id)

    # ------------------------------------------------------------------
    # Cancellation and pausing
    # ------------------------------------------------------------------
    def register_handle(self, task_id: Optional[int], handle: "asyncio.Task") -> None:
        """Associate the asyncio task driving ``task_id`` with the record.

        This is what makes ``cancel`` reach work that a WebSocket turn started:
        without it the manager holds a row and no way to stop the thing running.
        """
        if task_id is not None:
            self._handles[int(task_id)] = handle

    def unregister_handle(self, task_id: Optional[int]) -> None:
        if task_id is not None:
            self._handles.pop(int(task_id), None)

    async def cancel(self, task: Task, reason: str = "cancelled by user") -> Task:
        """Cancel the work, then mark the task cancelled.

        Order matters: the handle is cancelled first so the work stops, and only
        then is the record updated. A record marked cancelled while the work is
        still running is a lie the user can catch.
        """
        handle = self._handles.get(task.id) if task.id is not None else None
        if handle is not None and not handle.done():
            handle.cancel()
            # Give the cancellation a moment to land so the record and the work
            # do not disagree about what happened.
            try:
                await asyncio.wait_for(asyncio.shield(handle), timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception as exc:  # noqa: BLE001
                safe_error(exc, operation="task.cancel", component="core.tasks")
        if task.id is not None:
            self._handles.pop(int(task.id), None)

        await self.log(task, reason, level="warning")
        if task.status.is_terminal:
            return task
        try:
            return await self.transition(task, TaskStatus.CANCELLED)
        except IllegalTransition:
            # Already terminal by the time we got here. The work stopped, which
            # is what the user asked for; refusing to write the record would be
            # less accurate, not more.
            logger.info("Task %s already terminal; cancellation noted in the log", task.id)
            return task

    async def pause(self, task: Task) -> Task:
        """Suspend progress without changing the recorded status."""
        task.paused = True
        if task.id is not None:
            await store.set_paused(task.id, True)
        await self._bus.emit(CoreEventType.TASK_UPDATED, task=task.to_dict())
        return task

    async def resume(self, task: Task) -> Task:
        task.paused = False
        if task.id is not None:
            await store.set_paused(task.id, False)
        await self._bus.emit(CoreEventType.TASK_UPDATED, task=task.to_dict())
        return task

    def is_running(self, task_id: Optional[int]) -> bool:
        """Whether work for this task is in flight right now.

        In-memory by design: a handle only exists while this process is
        running it, which is exactly the question — a row left ``EXECUTING``
        by a crash has no handle and is *not* running, and asking the
        database would answer the wrong question with confidence.
        """
        if task_id is None:
            return False
        handle = self._handles.get(int(task_id))
        return handle is not None and not handle.done()

    async def is_paused(self, task_id: Optional[int]) -> bool:
        """Whether the task is paused, as recorded in the row.

        Delegates to the store rather than trusting an in-memory copy:
        pause() may have been called through a different manager instance
        (the REST singleton against a row this orchestrator is running), and
        the row is the only place both instances can agree.
        """
        return await store.is_paused(task_id)

    # ------------------------------------------------------------------
    # Running a coroutine as a task
    # ------------------------------------------------------------------
    async def run(
        self,
        task: Task,
        coroutine: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Run ``coroutine`` as the work of ``task``, registered so it can be
        cancelled.

        Exceptions are recorded on the task and re-raised, so the caller's own
        error handling still runs — a manager that swallowed the failure would
        report ``FAILED`` and leave the caller thinking it succeeded.
        """
        handle = asyncio.ensure_future(coroutine())
        self.register_handle(task.id, handle)
        try:
            result = await handle
        except asyncio.CancelledError:
            if not task.status.is_terminal:
                task.status = TaskStatus.CANCELLED
                task.ended_at = _now()
                if task.id is not None:
                    await store.update(task)
            raise
        except Exception as exc:  # noqa: BLE001 - recorded then re-raised
            await self.log(task, safe_error(
                exc, operation="task.run", component="core.tasks"
            ), level="error")
            if not task.status.is_terminal:
                try:
                    await self.transition(task, TaskStatus.FAILED)
                except IllegalTransition:
                    pass
            raise
        finally:
            self.unregister_handle(task.id)
        return result

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    async def get(self, task_id: int) -> Optional[Task]:
        return await store.get(task_id)

    async def list(self, **filters: Any) -> List[Task]:
        return await store.list_tasks(**filters)

    async def active_for_session(self, session_id: str) -> Optional[Task]:
        return await store.active_task_for_session(session_id)

    async def delete(self, task_id: int) -> bool:
        self.unregister_handle(task_id)
        return await store.delete(task_id)


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


#: The process-wide manager, like the event bus.
_manager: Optional[TaskManager] = None


def get_manager() -> TaskManager:
    global _manager
    if _manager is None:
        _manager = TaskManager()
    return _manager


__all__ = ["LOG_LEVELS", "TaskManager", "get_manager"]