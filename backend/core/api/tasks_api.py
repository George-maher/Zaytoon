"""``/tasks`` — agent tasks, and ``/reminders`` — the scheduled ones.

``/tasks`` used to be the reminder list. It now means what the name says, and
reminders moved to ``/reminders``. This is a deliberate break rather than a new
name like ``/agent-tasks``: a task system cannot coexist with a different
meaning at the path every client already calls, and the frontend change is
mechanical.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query

from common.reporting import safe_error
from core.tasks import TaskStatus, get_manager
from core.tasks.models import IllegalTransition, Task
from core.tasks.store import MAX_LOGS_PER_TASK

logger = logging.getLogger("Zaytoon.Core.Api.Tasks")

router = APIRouter()

VALID_STATUSES = {s.value for s in TaskStatus}


@router.get("/tasks")
async def list_tasks(
    status: Optional[str] = Query(None),
    session_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    logs: bool = Query(False, description="Include per-task log lines"),
) -> Dict[str, Any]:
    """Newest first."""
    if status and status not in VALID_STATUSES:
        raise HTTPException(
            400,
            f"Unknown status '{status}'. Valid: {', '.join(sorted(VALID_STATUSES))}",
        )
    tasks = await get_manager().list(
        status=status,
        session_id=session_id,
        project_id=project_id,
        limit=limit,
        include_logs=logs,
    )
    return {
        "tasks": [t.to_dict() for t in tasks],
        "count": len(tasks),
        "active": sum(1 for t in tasks if t.status.is_active),
    }


# Declared **before** `/tasks/{task_id}` on purpose. FastAPI matches in
# declaration order, so a `/tasks/stats` route added afterwards would be
# captured by the parameterised one and answered with a 422 — "stats" is not an
# integer. The same trap applies to any future literal path under a
# parameterised prefix, which is why this comment exists.
@router.get("/tasks/stats")
async def task_stats() -> Dict[str, Any]:
    """Counts by status, for the task panel header."""
    tasks = await get_manager().list(limit=500)
    counts: Dict[str, int] = {s.value: 0 for s in TaskStatus}
    for task in tasks:
        counts[task.status.value] += 1
    return {
        "total": len(tasks),
        "by_status": counts,
        "active": sum(1 for t in tasks if t.status.is_active),
    }


@router.get("/tasks/{task_id}")
async def get_task(task_id: int) -> Dict[str, Any]:
    """One task with its steps and recent log lines."""
    task = await get_manager().get(task_id)
    if task is None:
        raise HTTPException(404, f"No such task: {task_id}")
    payload = task.to_dict()
    payload["log_count"] = len(task.logs)
    return payload


@router.post("/tasks", status_code=201)
async def create_task(data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Create a task without running it.

    Useful for a long plan the user wants to review before anything happens, and
    for a client that drives execution itself.
    """
    title = (data.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "'title' is required")
    task = await get_manager().create(
        title=title,
        request=(data.get("request") or data.get("description") or ""),
        intent=data.get("intent"),
        language=data.get("language"),
        project_id=data.get("project_id"),
        session_id=data.get("session_id"),
        provider=data.get("provider"),
        model=data.get("model"),
    )
    return task.to_dict()


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: int) -> Dict[str, Any]:
    """Cancel a running task.

    Cancels the work first, then the record, so the two cannot disagree about
    whether it is still running.
    """
    manager = get_manager()
    task = await manager.get(task_id)
    if task is None:
        raise HTTPException(404, f"No such task: {task_id}")
    await manager.cancel(task)
    return task.to_dict()


@router.post("/tasks/{task_id}/pause")
async def pause_task(task_id: int) -> Dict[str, Any]:
    manager = get_manager()
    task = await manager.get(task_id)
    if task is None:
        raise HTTPException(404, f"No such task: {task_id}")
    return (await manager.pause(task)).to_dict()


@router.post("/tasks/{task_id}/resume")
async def resume_task(task_id: int) -> Dict[str, Any]:
    manager = get_manager()
    task = await manager.get(task_id)
    if task is None:
        raise HTTPException(404, f"No such task: {task_id}")
    return (await manager.resume(task)).to_dict()


# -- background execution ------------------------------------------------
async def _run_task_turn(task: Task) -> None:
    """The turn that executes ``task`` — the seam tests replace.

    Imported lazily: ``api.main`` includes this router, so a module-level
    import of it here would be circular. The orchestrator *binds* the record
    (``task=``), so this run advances the task the route fetched rather than
    creating a second one. ``session_id`` falls back to a per-task id when the
    task never had a session: two background runs sharing a session id would
    share the orchestrator's cancel registry, and cancelling one would then
    report on the other.
    """
    from api.main import orchestrator

    await orchestrator.run_turn(
        task.request or task.title,
        task.session_id or f"task:{task.id}",
        language_hint=task.language if task.language in ("ar", "en") else None,
        project_id=task.project_id,
        task=task,
    )


def _run_finished(manager, task_id: Optional[int], handle: "asyncio.Task") -> None:
    """Unbind a finished background run and surface anything that escaped.

    ``run_turn`` reports its own failures into the record and returns them,
    so an exception reaching here is the case that escaped *it* — logged,
    never re-raised: this runs in a done-callback, where a raised error goes
    to the loop's exception handler and to no one else.
    """
    manager.unregister_handle(task_id)
    if handle.cancelled():
        return
    exc = handle.exception()
    if exc is not None:
        safe_error(exc, operation="task.run", component="core.api.tasks")


def _start_run(manager, task: Task) -> None:
    """Run ``task`` in the background, registered so ``/cancel`` can reach it.

    Registered *before* the first await can interleave with another request:
    ``ensure_future`` schedules, ``register_handle`` binds — both synchronous,
    so a second ``/run`` for the same id cannot pass the ``is_running`` check
    in between.
    """
    handle = asyncio.ensure_future(_run_task_turn(task))
    manager.register_handle(task.id, handle)
    handle.add_done_callback(lambda h, tid=task.id: _run_finished(manager, tid, h))


@router.post("/tasks/{task_id}/run", status_code=202)
async def run_task(task_id: int) -> Dict[str, Any]:
    """Execute a non-terminal task's request, in the background.

    The turn runs through the same orchestrator a live session uses — task
    record, project preamble, routing, transitions — so the only difference
    from a chat turn is that nobody is listening for tokens. **A background
    run has no approval channel**: a gated tool is *refused* rather than
    executed (the gate fails closed), so work that may need your approval
    belongs in a live session, not here.

    ``POST /tasks/{id}/pause`` stops it at the next checkpoint;
    ``POST /tasks/{id}/cancel`` stops it outright. An ``EXECUTING`` row left
    behind by a crashed process has no live handle, so it is *not* "already
    running" and ``/run`` legitimately re-enters it.
    """
    manager = get_manager()
    task = await manager.get(task_id)
    if task is None:
        raise HTTPException(404, f"No such task: {task_id}")
    if task.status.is_terminal:
        raise HTTPException(
            409, "This task has already finished. Use POST /tasks/{task_id}/retry."
        )
    if task.paused:
        raise HTTPException(
            409, "This task is paused. Use POST /tasks/{task_id}/resume first."
        )
    if manager.is_running(task.id):
        raise HTTPException(409, "This task is already running.")

    _start_run(manager, task)
    return {"status": "running", "task": task.to_dict()}


@router.post("/tasks/{task_id}/retry", status_code=202)
async def retry_task(task_id: int) -> Dict[str, Any]:
    """Run a finished task's request again — as a **new** task.

    Terminal states are refused transitions on purpose: re-opening the old
    record would erase the history that says why a retry is needed at all.
    The new record carries ``parent_task_id`` back to the original, copies the
    request and its settings, and runs exactly as ``/run`` does — same
    approval caveat, same pause and cancel handles.
    """
    manager = get_manager()
    task = await manager.get(task_id)
    if task is None:
        raise HTTPException(404, f"No such task: {task_id}")
    if not task.status.is_terminal:
        raise HTTPException(
            409, "This task has not finished. Use POST /tasks/{task_id}/run."
        )

    child = await manager.create(
        title=task.title,
        request=task.request,
        intent=task.intent,
        language=task.language,
        project_id=task.project_id,
        session_id=task.session_id,
        parent_task_id=task.id,
        provider=task.provider,
        model=task.model,
    )
    _start_run(manager, child)
    return {"status": "running", "task": child.to_dict(), "retry_of": task.id}


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: int) -> Dict[str, Any]:
    if not await get_manager().delete(task_id):
        raise HTTPException(404, f"No such task: {task_id}")
    return {"status": "deleted", "id": task_id}