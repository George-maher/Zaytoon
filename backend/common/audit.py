"""The local execution audit log.

Every tool invocation is recorded here: when, which tool, what was asked, which
directory, which risk tier, whether a human approved it, and how it ended.

Three properties are load-bearing:

* **It cannot break a turn.** :func:`record` is fire-and-forget: a locked
  database, a full disk or a schema error is logged once and forgotten. Losing
  an audit line is bad; refusing to answer the user because of one is worse.
* **It cannot become a secret store.** Every text field goes through
  :func:`common.reporting.redact` before it is stored, so a command line
  containing a token is recorded as ``***``.
* **It is inspectable.** The desktop shell and the web settings page read it
  through :func:`recent` / :func:`count` and clear it through :func:`clear`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiosqlite

from common.reporting import redact, safe_error

logger = logging.getLogger("Zaytoon.Audit")

#: Longest stored value for any free-text field. An audit row is a record that
#: something happened, not a transcript of everything the user ever said.
MAX_TEXT = 500

#: `PRAGMA busy_timeout` covers a *short* wait. A concurrent writer holding the
#: lock for longer — the memory agent storing a message while the audit page is
#: being cleared — still raises `database is locked`, so writes and deletes are
#: retried briefly. Bounded on purpose: an audit write is never worth turning
#: into a long stall, and the caller is already protected by the swallow-all
#: wrapper around every function here.
BUSY_TIMEOUT_MS = 3000
BUSY_RETRIES = 3
BUSY_BACKOFF_S = 0.2

#: Ring of recent failures. An audit table that fails to write must be
#: noticeable in the log, not invisible.
_WARNED = False


def _is_busy(exc: BaseException) -> bool:
    return "locked" in str(exc).lower() or "busy" in str(exc).lower()


async def _with_retry(operation, *args, **kwargs):
    """Run a database operation, retrying briefly while the file is locked."""
    delay = BUSY_BACKOFF_S
    for attempt in range(BUSY_RETRIES):
        try:
            return await operation(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - narrowed by _is_busy below
            if not _is_busy(exc) or attempt == BUSY_RETRIES - 1:
                raise
            await asyncio.sleep(delay)
            delay *= 2
    return None  # pragma: no cover - unreachable

_SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    trace_id    TEXT,
    user_request TEXT,
    tool        TEXT    NOT NULL,
    command     TEXT,
    cwd         TEXT,
    risk        TEXT,
    approved    INTEGER,
    success     INTEGER,
    exit_code   INTEGER,
    duration_ms INTEGER,
    error       TEXT
)
"""


def _db_path() -> str:
    """The audit database file — **deliberately not** the memory database.

    The first version shared ``zaytoon.db`` with the memory agent, on the
    reasonable-sounding assumption that "one SQLite file" was simpler. It is not:
    the memory agent holds that connection open for the life of the process, so
    clearing the audit page from the UI failed with ``database is locked`` after
    a 3s busy timeout *and* three retries — the contention was structural, not
    transient. A separate file removes the shared lock entirely, and an audit
    trail is worth being able to delete, archive or ship on its own.

    ``AUDIT_DB_PATH`` overrides it; the data directory is honoured so a
    relocated deployment keeps its history.
    """
    override = os.getenv("AUDIT_DB_PATH")
    if override:
        path = override
    else:
        data_dir = os.path.join(os.getcwd(), "data")
        path = os.path.join(data_dir, "audit.db")
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    return path


def _clip(value: Any, limit: int = MAX_TEXT) -> str:
    text = redact("" if value is None else str(value)).strip()
    return text if len(text) <= limit else text[:limit] + "…"


async def _connect() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(_db_path())
    conn.row_factory = aiosqlite.Row
    # WAL so a reader (the audit page) never blocks a writer (a running tool),
    # and a busy timeout so a concurrent write waits instead of raising
    # SQLITE_BUSY at the one moment the assistant needed the machine.
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    await conn.execute(_SCHEMA)
    await conn.commit()
    return conn


async def _write(
    tool, trace_id, user_request, command, cwd, risk, approved, success,
    exit_code, duration_ms, error,
) -> None:
    """The INSERT itself, separated so the retry wrapper can re-run it whole."""
    conn = await _connect()
    try:
        await conn.execute(
            """
            INSERT INTO execution_audit
                (ts, trace_id, user_request, tool, command, cwd, risk,
                 approved, success, exit_code, duration_ms, error)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                _clip(trace_id, 64),
                _clip(user_request),
                _clip(tool, 64),
                _clip(command),
                _clip(cwd, 300),
                _clip(risk, 32),
                None if approved is None else int(bool(approved)),
                int(bool(success)),
                exit_code if isinstance(exit_code, int) else None,
                int(duration_ms) if duration_ms is not None else None,
                _clip(error),
            ),
        )
        await conn.commit()
    finally:
        await conn.close()


async def record(
    *,
    tool: str,
    trace_id: Optional[str] = None,
    user_request: Optional[str] = None,
    command: Any = None,
    cwd: Optional[str] = None,
    risk: Optional[str] = None,
    approved: Optional[bool] = None,
    success: bool = False,
    exit_code: Optional[int] = None,
    duration_ms: Optional[float] = None,
    error: Any = None,
) -> None:
    """Append one execution record. Never raises."""
    global _WARNED
    try:
        await _with_retry(_write, tool, trace_id, user_request, command, cwd,
                          risk, approved, success, exit_code, duration_ms, error)
    except Exception as exc:  # noqa: BLE001 - auditing must never break a turn
        if not _WARNED:
            _WARNED = True
            logger.warning(
                safe_error(
                    exc,
                    operation="audit.record",
                    component="common.audit",
                    detail="further audit failures will be debug-log only",
                )
            )
        else:
            logger.debug("audit write failed", exc_info=True)


async def recent(limit: int = 100) -> List[Dict[str, Any]]:
    """Newest-first execution records. Returns ``[]`` if the table is unreadable."""
    try:
        limit = max(1, min(int(limit or 100), 1000))
    except (TypeError, ValueError):
        limit = 100
    try:
        conn = await _connect()
        try:
            async with conn.execute(
                "SELECT * FROM execution_audit ORDER BY id DESC LIMIT ?", (limit,)
            ) as cur:
                rows = await cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            await conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            safe_error(
                exc, operation="audit.recent", component="common.audit"
            )
        )
        return []


async def count() -> int:
    """Total rows, or 0 when the table cannot be read."""
    try:
        conn = await _connect()
        try:
            async with conn.execute("SELECT COUNT(*) AS n FROM execution_audit") as cur:
                row = await cur.fetchone()
            return int(row["n"]) if row else 0
        finally:
            await conn.close()
    except Exception:  # noqa: BLE001
        return 0


async def clear() -> int:
    """Delete every record; returns how many were removed."""
    try:
        return await _with_retry(_delete_all)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            safe_error(exc, operation="audit.clear", component="common.audit")
        )
        return 0


async def _delete_all() -> int:
    """The DELETE itself, separated so the retry wrapper can re-run it whole."""
    conn = await _connect()
    try:
        async with conn.execute("SELECT COUNT(*) AS n FROM execution_audit") as cur:
            row = await cur.fetchone()
        total = int(row["n"]) if row else 0
        await conn.execute("DELETE FROM execution_audit")
        await conn.commit()
        return total
    finally:
        await conn.close()