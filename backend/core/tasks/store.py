"""Task persistence.

Thin by design: every function is a SQL statement against ``core.db`` and every
one of them swallows its own errors, because the pattern established by
``common/audit.py`` is that storage must never be able to break a turn. A task
that fails to record its last log line has still run correctly; a turn that
raises because a log line did not fit has not.

Steps and logs are **not** loaded with the task on every read. A busy task
accumulates hundreds of log lines and the task list view needs none of them, so
they are fetched on demand by :func:`load_steps` / :func:`load_logs`.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core import db
from core.tasks.models import Task, TaskStatus, TaskStep, TaskStepStatus

logger = logging.getLogger("Zaytoon.Core.Tasks.Store")

#: Log lines kept per task. A runaway loop is bounded by the engine's
#: iteration cap, but a chatty command can print thousands of lines and the
#: table should not grow without limit.
MAX_LOGS_PER_TASK = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dump(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return None


def _undump(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, dict):
        return [str(k) for k in value]
    return [str(value)]


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
async def create(task: Task) -> Optional[int]:
    """Insert a new task. Returns its row id, or ``None`` if the write failed."""
    from core.events import CoreEventType, bus

    task.created_at = task.created_at or _now()
    ok = await db.execute(
        """
        INSERT INTO tasks
            (title, request, status, progress, intent, language, project_id,
             session_id, parent_task_id, provider, model, plan, files_changed,
             tools_used, result, error, paused, created_at, started_at,
             ended_at, duration_ms)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            task.title,
            task.request,
            task.status.value,
            task.progress,
            task.intent,
            task.language,
            task.project_id,
            task.session_id,
            task.parent_task_id,
            task.provider,
            task.model,
            _dump(task.plan),
            _dump(task.files_changed),
            _dump(task.tools_used),
            task.result,
            task.error,
            1 if task.paused else 0,
            task.created_at,
            task.started_at,
            task.ended_at,
            task.duration_ms,
        ),
    )
    if not ok:
        return None
    row = await db.fetch_one(
        "SELECT id FROM tasks WHERE title = ? AND created_at = ? "
        "ORDER BY id DESC LIMIT 1",
        (task.title, task.created_at),
    )
    task.id = row["id"] if row else None
    bus.publish(CoreEventType.TASK_CREATED, task=task.to_dict())
    return task.id


async def update(task: Task) -> bool:
    """Write the mutable columns of an existing task.

    ``paused`` is deliberately absent. It is a *cross-request* flag owned by
    :func:`set_paused` alone: a running copy carries a stale value for it, so
    writing the whole row from that copy would silently un-pause a task that
    was paused through REST a moment earlier — which is exactly how a pause
    used to evaporate between the request that set it and the checkpoint that
    was meant to honour it.
    """
    return await db.execute(
        """
        UPDATE tasks SET
            title = ?, request = ?, status = ?, progress = ?, intent = ?,
            language = ?, project_id = ?, session_id = ?, parent_task_id = ?,
            provider = ?, model = ?, plan = ?, files_changed = ?, tools_used = ?,
            result = ?, error = ?, started_at = ?, ended_at = ?,
            duration_ms = ?
        WHERE id = ?
        """,
        (
            task.title,
            task.request,
            task.status.value,
            task.progress,
            task.intent,
            task.language,
            task.project_id,
            task.session_id,
            task.parent_task_id,
            task.provider,
            task.model,
            _dump(task.plan),
            _dump(task.files_changed),
            _dump(task.tools_used),
            task.result,
            task.error,
            task.started_at,
            task.ended_at,
            task.duration_ms,
            task.id,
        ),
    )


async def set_paused(task_id: Optional[int], paused: bool) -> bool:
    """Persist the pause flag — the only writer of that column.

    One column on purpose: pause() must not race a lifecycle write for the
    same row, and no lifecycle write may answer the pause question on
    pause()'s behalf. Returns whether a row was found.
    """
    if task_id is None:
        return False
    return await db.execute(
        "UPDATE tasks SET paused = ? WHERE id = ?",
        (1 if paused else 0, task_id),
    )


async def replace_steps(task_id: int, steps: List[TaskStep]) -> bool:
    """Rewrite a task's steps. Called when the plan changes, not per step."""
    statements: List[tuple] = [
        ("DELETE FROM task_steps WHERE task_id = ?", (task_id,))
    ]
    for step in steps:
        statements.append((
            """
            INSERT INTO task_steps
                (task_id, seq, title, status, tool, detail, started_at, ended_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                task_id,
                step.seq,
                step.title,
                step.status.value,
                step.tool,
                step.detail,
                step.started_at,
                step.ended_at,
            ),
        ))
    return await db.execute_many(statements)


async def upsert_step(task_id: int, step: TaskStep) -> bool:
    """Write one step's current state."""
    return await db.execute(
        """
        INSERT INTO task_steps
            (task_id, seq, title, status, tool, detail, started_at, ended_at)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(task_id, seq) DO UPDATE SET
            title = excluded.title,
            status = excluded.status,
            tool = excluded.tool,
            detail = excluded.detail,
            started_at = excluded.started_at,
            ended_at = excluded.ended_at
        """,
        (
            task_id,
            step.seq,
            step.title,
            step.status.value,
            step.tool,
            step.detail,
            step.started_at,
            step.ended_at,
        ),
    )


async def append_log(task_id: int, level: str, message: str) -> bool:
    """Append one log line, trimming the oldest beyond :data:`MAX_LOGS_PER_TASK`."""
    stored = await db.execute(
        "INSERT INTO task_logs (task_id, ts, level, message) VALUES (?,?,?,?)",
        (task_id, _now(), level, message),
    )
    if stored:
        # Trim only occasionally: doing it on every line would be a second write
        # per log entry for no benefit at these volumes.
        count = await count_logs(task_id)
        if count > MAX_LOGS_PER_TASK:
            excess = count - MAX_LOGS_PER_TASK
            await db.execute(
                "DELETE FROM task_logs WHERE id IN ("
                "  SELECT id FROM task_logs WHERE task_id = ? ORDER BY id ASC LIMIT ?"
                ")",
                (task_id, excess),
            )
    return stored


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
async def get(task_id: int, include_logs: bool = True) -> Optional[Task]:
    """One task, with its steps and (by default) its log lines.

    ``include_logs`` defaults to True here but to False for the list endpoint:
    a single task's log is the point of opening it, while the task *list* view
    needs none of them and there can be hundreds of tasks.
    """
    row = await db.fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not row:
        return None
    task = Task.from_row(row)
    task.steps = await load_steps(task_id)
    if include_logs:
        task.logs = await load_logs(task_id)
    task.refresh_progress()
    return task


async def list_tasks(
    status: Optional[str] = None,
    session_id: Optional[str] = None,
    project_id: Optional[str] = None,
    limit: int = 50,
    include_logs: bool = False,
) -> List[Task]:
    """Newest first. Filtered by status/session/project when given."""
    clauses: List[str] = []
    params: List[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    if project_id:
        clauses.append("project_id = ?")
        params.append(project_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        bound = max(1, min(int(limit or 50), 500))
    except (TypeError, ValueError):
        bound = 50
    params.append(bound)

    rows = await db.fetch_all(
        f"SELECT * FROM tasks {where} ORDER BY id DESC LIMIT ?", params
    )
    tasks = []
    for row in rows:
        task = Task.from_row(row)
        task.refresh_progress()
        if include_logs:
            task.logs = await load_logs(task.id)
        tasks.append(task)
    return tasks


async def load_steps(task_id: int) -> List[TaskStep]:
    rows = await db.fetch_all(
        "SELECT * FROM task_steps WHERE task_id = ? ORDER BY seq ASC", (task_id,)
    )
    return [
        TaskStep(
            title=row.get("title") or "",
            status=TaskStepStatus(row.get("status") or "pending"),
            seq=int(row.get("seq") or 0),
            tool=row.get("tool"),
            detail=row.get("detail"),
            started_at=row.get("started_at"),
            ended_at=row.get("ended_at"),
        )
        for row in rows
    ]


async def load_logs(task_id: int, limit: int = MAX_LOGS_PER_TASK) -> List[Dict[str, Any]]:
    try:
        bound = max(1, min(int(limit or MAX_LOGS_PER_TASK), MAX_LOGS_PER_TASK))
    except (TypeError, ValueError):
        bound = MAX_LOGS_PER_TASK
    # Oldest first, and the most recent `bound` of them: a task log read top to
    # bottom should read in the order the events happened. Ordering DESC here and
    # reversing in Python would be the same data with an extra step, and would
    # be wrong the moment someone forgot the reverse.
    rows = await db.fetch_all(
        "SELECT ts, level, message FROM task_logs WHERE task_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (task_id, bound),
    )
    return rows[::-1]


async def count_logs(task_id: int) -> int:
    row = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM task_logs WHERE task_id = ?", (task_id,)
    )
    return int(row["n"]) if row else 0


async def active_task_for_session(session_id: str) -> Optional[Task]:
    """The newest non-terminal task of a session, if any.

    This is what makes ``interrupt`` able to say which task it stopped, rather
    than just cancelling a socket-level task and hoping.
    """
    rows = await db.fetch_all(
        "SELECT * FROM tasks WHERE session_id = ? AND status NOT IN (?,?,?) "
        "ORDER BY id DESC LIMIT 1",
        (session_id, TaskStatus.COMPLETED.value, TaskStatus.FAILED.value,
         TaskStatus.CANCELLED.value),
    )
    if not rows:
        return None
    task = Task.from_row(rows[0])
    task.steps = await load_steps(task.id)
    task.refresh_progress()
    return task


async def is_paused(task_id: Optional[int]) -> bool:
    """The persisted pause flag — one column, one query.

    Read fresh rather than trusting an in-memory ``Task.paused``: pause() may
    have been called through a *different* ``TaskManager`` instance (the REST
    singleton against a row an orchestrator is running) on a different Python
    object, and that copy never sees the assignment. The row is the only
    place both can agree on.
    """
    if task_id is None:
        return False
    row = await db.fetch_one("SELECT paused FROM tasks WHERE id = ?", (task_id,))
    return bool(row and row["paused"])


async def delete(task_id: int) -> bool:
    """Remove a task and its children.

    Reports whether a row was actually removed, so "deleted" is never claimed
    for a task that was not there.
    """
    removed = await db.execute_affected("DELETE FROM tasks WHERE id = ?", (task_id,)) > 0
    # Explicit child cleanup: foreign_keys is a per-connection pragma and a
    # connection opened elsewhere may not have it on.
    await db.execute_many([
        ("DELETE FROM task_steps WHERE task_id = ?", (task_id,)),
        ("DELETE FROM task_logs WHERE task_id = ?", (task_id,)),
    ])
    return removed


async def clear_terminal(keep: int = 100) -> int:
    """Delete the oldest terminal tasks, keeping the newest ``keep``."""
    rows = await db.fetch_all(
        "SELECT id FROM tasks WHERE status IN (?,?,?) ORDER BY id DESC",
        (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value),
    )
    stale = [r["id"] for r in rows[max(0, int(keep or 0)) :]]
    for task_id in stale:
        await delete(task_id)
    return len(stale)


async def record_file_change(task_id: int, path: str) -> bool:
    """Add a path to a task's ``files_changed`` list, de-duplicated."""
    task = await get(task_id)
    if task is None:
        return False
    if path not in task.files_changed:
        task.files_changed.append(path)
    return await update(task)


async def record_tool(task_id: int, tool: str) -> bool:
    """Add a tool name to a task's ``tools_used`` list, de-duplicated."""
    task = await get(task_id)
    if task is None:
        return False
    if tool not in task.tools_used:
        task.tools_used.append(tool)
    return await update(task)


__all__ = [
    "MAX_LOGS_PER_TASK",
    "active_task_for_session",
    "append_log",
    "clear_terminal",
    "create",
    "delete",
    "get",
    "list_tasks",
    "load_logs",
    "load_steps",
    "record_file_change",
    "record_tool",
    "replace_steps",
    "update",
    "upsert_step",
]