"""One schema, one connection discipline.

The existing codebase already learned this lesson twice, and both lessons are
encoded here rather than repeated:

* **The audit log has its own database file.** It originally shared
  ``zaytoon.db`` with the memory agent, which holds that connection open for the
  life of the process, so clearing the audit page failed with ``database is
  locked`` after a 3s busy timeout *and* three retries. The contention was
  structural, not transient. The core database is therefore **its own file**,
  ``data/core.db``, and never shares a handle with the memory agent or the audit
  log.
* **A failing write must never break a turn.** Every public function here
  swallows its own errors and returns a safe value, because losing a task row is
  annoying and refusing to answer the user over one is not acceptable.

WAL is enabled so the settings page (a reader) never blocks a running task (a
writer), and ``busy_timeout`` is set so a concurrent write waits instead of
raising ``SQLITE_BUSY`` at the one moment the assistant needed the machine.

The schema is versioned. :func:`migrate` is idempotent and is called on every
:func:`connect`, so a fresh install and an upgraded one take the same path.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

import aiosqlite

from common.reporting import safe_error
from core import paths

logger = logging.getLogger("Zaytoon.Core.DB")

BUSY_TIMEOUT_MS = 3000
BUSY_RETRIES = 3
BUSY_BACKOFF_S = 0.2


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
# Written as numbered migrations rather than one ``CREATE TABLE IF NOT EXISTS``
# blob so an upgraded database gets new columns instead of silently keeping the
# old shape. Index 0 is the initial schema.

#: The data half of migration 5, kept as a named constant because it is the
#: part worth testing: the ``ALTER`` runs once on a schema where the column is
#: missing and can never observe its own effect. A provider that already has
#: recorded models has served this user before, so its setup counts as proven;
#: a provider without models is a fresh configuration and must still pass a
#: real chat test.
SETUP_STAMP_SQL = """
    UPDATE providers
       SET chat_verified_at = COALESCE(updated_at, created_at)
     WHERE chat_verified_at IS NULL
       AND EXISTS (SELECT 1 FROM models m WHERE m.provider_id = providers.id)
"""

_MIGRATIONS: List[str] = [
    # -- 0: initial ------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS projects (
        id             TEXT PRIMARY KEY,
        name           TEXT NOT NULL,
        path           TEXT NOT NULL UNIQUE,
        kind           TEXT,
        language       TEXT,
        package_manager TEXT,
        framework      TEXT,
        run_command    TEXT,
        test_command   TEXT,
        build_command  TEXT,
        entry_points   TEXT,
        is_git         INTEGER DEFAULT 0,
        branch         TEXT,
        pinned         INTEGER DEFAULT 0,
        last_opened_at TEXT,
        created_at     TEXT
    );

    CREATE TABLE IF NOT EXISTS tasks (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        title        TEXT NOT NULL,
        request      TEXT,
        status       TEXT NOT NULL,
        progress     REAL DEFAULT 0,
        intent       TEXT,
        language     TEXT,
        project_id   TEXT,
        session_id   TEXT,
        provider     TEXT,
        model        TEXT,
        plan         TEXT,
        result       TEXT,
        error        TEXT,
        files_changed TEXT,
        tools_used   TEXT,
        created_at   TEXT,
        started_at   TEXT,
        ended_at     TEXT,
        duration_ms  INTEGER
    );

    CREATE TABLE IF NOT EXISTS task_steps (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id    INTEGER NOT NULL,
        seq        INTEGER NOT NULL,
        title      TEXT NOT NULL,
        status     TEXT NOT NULL,
        tool       TEXT,
        detail     TEXT,
        started_at TEXT,
        ended_at   TEXT,
        FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS task_logs (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id    INTEGER NOT NULL,
        ts         TEXT NOT NULL,
        level      TEXT NOT NULL,
        message    TEXT NOT NULL,
        FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS providers (
        id           TEXT PRIMARY KEY,
        name         TEXT NOT NULL,
        kind         TEXT NOT NULL,
        base_url     TEXT NOT NULL,
        requires_key INTEGER NOT NULL DEFAULT 0,
        enabled      INTEGER NOT NULL DEFAULT 1,
        is_default   INTEGER NOT NULL DEFAULT 0,
        created_at   TEXT,
        updated_at   TEXT
    );

    CREATE TABLE IF NOT EXISTS provider_secrets (
        provider_id TEXT PRIMARY KEY,
        api_key     TEXT NOT NULL,
        updated_at  TEXT,
        FOREIGN KEY (provider_id) REFERENCES providers(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS models (
        provider_id    TEXT NOT NULL,
        model_id       TEXT NOT NULL,
        display_name   TEXT,
        capabilities   TEXT,
        context_length INTEGER,
        enabled        INTEGER NOT NULL DEFAULT 1,
        priority       INTEGER NOT NULL DEFAULT 0,
        discovered_at  TEXT,
        PRIMARY KEY (provider_id, model_id),
        FOREIGN KEY (provider_id) REFERENCES providers(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS skills (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        version     TEXT,
        description TEXT,
        path        TEXT NOT NULL,
        source      TEXT,
        enabled     INTEGER NOT NULL DEFAULT 1,
        tools       TEXT,
        created_at  TEXT,
        updated_at  TEXT
    );

    CREATE TABLE IF NOT EXISTS permission_grants (
        permission TEXT PRIMARY KEY,
        decision   TEXT NOT NULL,
        updated_at TEXT
    );

    CREATE TABLE IF NOT EXISTS kv (
        key        TEXT PRIMARY KEY,
        value      TEXT,
        updated_at TEXT
    );

    -- Target authorizations for active security assessment.
    --
    -- A migration, not an edit to an earlier one: `_MIGRATIONS` is applied in
    -- order and the schema_version row records how many have run, so appending
    -- is the only change an existing database can absorb. Adding this to an
    -- earlier entry would leave every already-migrated database without the
    -- table, because that entry is not re-run.
    CREATE TABLE IF NOT EXISTS security_authorizations (
        id          TEXT PRIMARY KEY,
        target      TEXT NOT NULL,
        operations  TEXT NOT NULL,
        note        TEXT,
        created_at  TEXT,
        expires_at  TEXT,
        revoked_at  TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_security_auth_target
        ON security_authorizations(target);

    CREATE INDEX IF NOT EXISTS idx_task_steps_task ON task_steps(task_id, seq);
    CREATE INDEX IF NOT EXISTS idx_task_logs_task  ON task_logs(task_id, id);
    CREATE INDEX IF NOT EXISTS idx_tasks_status    ON tasks(status, id DESC);
    CREATE INDEX IF NOT EXISTS idx_tasks_session   ON tasks(session_id, id DESC);
    """,

    # -- 1: uniqueness for step upserts --------------------------------
    # `store.upsert_step` writes with ON CONFLICT(task_id, seq), which SQLite
    # refuses unless that pair is *declared* unique — an index is not enough.
    # Without it every step update raised OperationalError and was swallowed by
    # the never-raise helper, so a task's steps silently stayed "pending" while
    # the in-memory object showed them done. A silent write failure that leaves
    # a record wrong is worse than a loud one.
    #
    # The duplicates an index has to collapse are only possible if a plan was
    # rewritten with a different length; delete_task_rows runs first in that path.
    """
    DELETE FROM task_steps
     WHERE id NOT IN (SELECT MIN(id) FROM task_steps GROUP BY task_id, seq);

    CREATE UNIQUE INDEX IF NOT EXISTS idx_task_steps_unique
        ON task_steps(task_id, seq);
    """,

    # -- 2: task lineage and a persisted pause flag ----------------------
    # Two columns, for two reasons:
    #
    # * ``parent_task_id`` is how a retry relates to the task it retries.
    #   Terminal states are refused transitions, so a retry cannot reuse the
    #   old record without erasing the history that explains why it exists;
    #   a new record with a lineage edge keeps both.
    # * ``paused`` was a field on the in-memory Task only. It survived in the
    #   object ``pause()`` returned and nowhere else — ``store.update`` never
    #   wrote it and ``from_row`` never read it — so a pause evaporated on the
    #   next read, and a runner checking its own copy of the row could never
    #   see a pause requested through another manager instance. A flag the
    #   rest of the process cannot observe is not a flag.
    #
    # Plain ``ALTER`` (no FK on parent): lineage is informational, and a
    # foreign key here would start refusing ``DELETE /tasks/{id}`` on any
    # task that has ever been retried. The script runs once per file —
    # ``migrate`` only executes entries past the recorded version — and a
    # re-run after the version ledger is manually wiped fails loudly on the
    # duplicate column, which is the right failure for a corrupted history:
    # a silent half-apply would not be.
    """
    ALTER TABLE tasks ADD COLUMN parent_task_id INTEGER;
    ALTER TABLE tasks ADD COLUMN paused INTEGER NOT NULL DEFAULT 0;
    """,

    # -- 3: per-provider timeout, org identifier, verification, catalog ----
    # Provider options the settings form can set without a code change:
    #
    # * ``timeout_s`` — a request timeout per provider (0/NULL = the client
    #   default): a slow local daemon and a fast hosted API do not want one
    #   number. §2A asks for it explicitly.
    # * ``org_id`` — an OpenAI ``OpenAI-Organization`` header for endpoints
    #   that bill or scope by organisation. Not a credential: it is returned
    #   by ``GET /providers`` like every other non-secret field.
    # * ``verified_at`` — when a connectivity test last *passed*. The UI must
    #   never show a success it did not measure, and "verified 2 minutes ago"
    #   is a measurement; a provider row without one is unverified.
    # * ``models.metadata_json`` — catalog metadata that has no natural
    #   column (pricing, supported_parameters): kept as JSON rather than
    #   flattened into columns that would need another migration per vendor.
    """
    ALTER TABLE providers ADD COLUMN timeout_s REAL;
    ALTER TABLE providers ADD COLUMN org_id TEXT;
    ALTER TABLE providers ADD COLUMN verified_at TEXT;
    ALTER TABLE models ADD COLUMN metadata_json TEXT;
    """,

    # -- 4: why a catalog row is disabled ---------------------------------
    # Refresh records *every* model an endpoint reports, and a model the
    # free-only policy refuses is stored disabled with the refusal as its
    # reason instead of being silently dropped — a catalog where the refusals
    # are invisible is one the user cannot audit. The column stores the
    # refusal text; the enforcement stays in the write path (a hand-save
    # still raises), so a row cannot be enabled around it.
    """
    ALTER TABLE models ADD COLUMN policy_reason TEXT;
    """,

    # -- 5: first-run setup proof -----------------------------------------
    # The wizard and the setup banner read this column: setup completes only
    # after a *real* chat test (POST /providers/{id}/test-chat). A
    # reachability probe proves the endpoint answers and says nothing about
    # whether the key may generate or the model exists — every failure this
    # guards against shows up only when a completion is attempted. Existing
    # installs are stamped from prior use (see SETUP_STAMP_SQL); fresh ones
    # stay unverified until a completion actually happens.
    "ALTER TABLE providers ADD COLUMN chat_verified_at TEXT;\n"
    + SETUP_STAMP_SQL,

    # -- 6: evidence — what the assessment tools actually observed --------
    # A finding that lives only in a transcript is a finding nobody can
    # review later: the report (§11C) needs the observation, its evidence
    # and its authorization to outlive the turn that produced it. Written
    # by the security tools themselves, after a *successful* probe — a
    # refused or failed probe records nothing, because there was no
    # observation to keep.
    #
    # `confidence` is a closed vocabulary enforced in findings.py, not by a
    # CHECK constraint: the tools are the only writers, they construct the
    # value, and a constraint would turn a future vocabulary change into a
    # migration that silently drops rows the moment an unknown value is
    # written by a never-raise helper. The index is on (target, created_at)
    # because the report reads per-target in time order.
    """
    CREATE TABLE IF NOT EXISTS security_findings (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        authorization_id TEXT,
        tool         TEXT NOT NULL,
        operation    TEXT,
        target       TEXT NOT NULL,
        finding_id   TEXT,
        title        TEXT NOT NULL,
        severity     TEXT NOT NULL,
        confidence   TEXT NOT NULL,
        category     TEXT,
        evidence     TEXT,
        consequence  TEXT,
        remediation  TEXT,
        data_json    TEXT,
        created_at   TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_security_findings_target
        ON security_findings(target, created_at);
    """,
]


def db_path() -> str:
    """Absolute path of the core database file.

    ``CORE_DB_PATH`` overrides it. The default lives beside the backend's
    ``data/`` directory, which is where the memory and audit databases already
    are — but in a **separate file**, for the reason documented at the top of
    this module.
    """
    override = os.getenv("CORE_DB_PATH")
    if override:
        path = os.path.abspath(override)
    else:
        # `paths.data_dir()` rather than `os.getcwd()/data`: a portable install
        # puts Data/ beside Application/, and a backend launched by the desktop
        # shell has a cwd the user did not choose. See core/paths.py.
        path = os.path.join(str(paths.data_dir()), "core.db")
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_busy(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


async def _with_retry(operation, *args, **kwargs):
    """Run a database operation, retrying briefly while the file is locked."""
    delay = BUSY_BACKOFF_S
    for attempt in range(BUSY_RETRIES):
        try:
            return await operation(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - narrowed by _is_busy
            if not _is_busy(exc) or attempt == BUSY_RETRIES - 1:
                raise
            await asyncio.sleep(delay)
            delay *= 2
    return None  # pragma: no cover - unreachable


#: Bumped whenever :data:`_MIGRATIONS` grows an entry. Declared after the list
#: so it cannot drift from it.
SCHEMA_VERSION = len(_MIGRATIONS)


async def migrate(conn: aiosqlite.Connection) -> int:
    """Bring ``conn`` up to :data:`SCHEMA_VERSION`. Idempotent.

    Returns the version now in force. Applied on every connect so a fresh file
    and an upgraded one converge on the same shape without a separate migration
    step that someone can forget.
    """
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
    )
    row = await conn.execute_fetchall("SELECT version FROM schema_version LIMIT 1")
    current = int(row[0][0]) if row else 0

    for index in range(current, len(_MIGRATIONS)):
        await conn.executescript(_MIGRATIONS[index])
        # Replaced rather than appended: the table holds exactly one row, and a
        # second INSERT would make `current` ambiguous on the next open.
        await conn.execute("DELETE FROM schema_version")
        await conn.execute("INSERT INTO schema_version (version) VALUES (?)", (index + 1,))
    await conn.commit()
    return max(current, len(_MIGRATIONS))


async def connect() -> aiosqlite.Connection:
    """Open the core database with the schema applied."""
    conn = await aiosqlite.connect(db_path())
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    # Referential cleanup is explicit rather than automatic: a task's steps and
    # logs are meaningless without it, and ON DELETE CASCADE only fires when the
    # pragma is on for every connection that writes.
    await conn.execute("PRAGMA foreign_keys=ON")
    await migrate(conn)
    return conn


# ---------------------------------------------------------------------------
# Small helpers shared by the stores
# ---------------------------------------------------------------------------
async def fetch_all(sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
    """All rows as dicts. Returns ``[]`` on failure."""
    async def _run():
        conn = await connect()
        try:
            async with conn.execute(sql, tuple(params)) as cur:
                rows = await cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            await conn.close()

    try:
        return await _with_retry(_run)
    except Exception as exc:  # noqa: BLE001
        logger.warning(safe_error(exc, operation="db.fetch_all", component="core.db"))
        return []


async def fetch_one(sql: str, params: Iterable[Any] = ()) -> Optional[Dict[str, Any]]:
    """First row as a dict, or ``None``."""
    rows = await fetch_all(sql, params)
    return rows[0] if rows else None


async def execute(sql: str, params: Iterable[Any] = ()) -> bool:
    """Run a write. Returns whether the statement *succeeded* — it never raises.

    "Succeeded" is not "changed anything": an ``UPDATE`` matching no rows is a
    successful statement. Use :func:`execute_affected` when the difference
    matters — a caller that reports "deleted" for a row that was never there is
    reporting something that did not happen.
    """
    async def _run():
        conn = await connect()
        try:
            await conn.execute(sql, tuple(params))
            await conn.commit()
            return True
        finally:
            await conn.close()

    try:
        return bool(await _with_retry(_run))
    except Exception as exc:  # noqa: BLE001
        logger.warning(safe_error(exc, operation="db.execute", component="core.db"))
        return False


async def execute_affected(sql: str, params: Iterable[Any] = ()) -> int:
    """Run a write and return the number of rows it changed. Never raises.

    ``-1`` means the statement failed, which is distinguishable from ``0``
    ("matched nothing") — a distinction several callers genuinely need.
    """
    async def _run():
        conn = await connect()
        try:
            cursor = await conn.execute(sql, tuple(params))
            changed = cursor.rowcount
            await conn.commit()
            return max(0, int(changed))
        finally:
            await conn.close()

    try:
        return await _with_retry(_run)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            safe_error(exc, operation="db.execute_affected", component="core.db")
        )
        return -1


async def execute_many(statements: Iterable[tuple]) -> bool:
    """Run several writes in one transaction. Never raises."""
    items = list(statements)
    if not items:
        return True

    async def _run():
        conn = await connect()
        try:
            for sql, params in items:
                await conn.execute(sql, tuple(params))
            await conn.commit()
            return True
        finally:
            await conn.close()

    try:
        return bool(await _with_retry(_run))
    except Exception as exc:  # noqa: BLE001
        logger.warning(safe_error(exc, operation="db.execute_many", component="core.db"))
        return False


async def kv_get(key: str, default: Optional[str] = None) -> Optional[str]:
    row = await fetch_one("SELECT value FROM kv WHERE key = ?", (key,))
    return row["value"] if row else default


async def kv_set(key: str, value: Optional[str]) -> bool:
    return await execute(
        "INSERT INTO kv (key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, _now()),
    )


async def reset_for_tests() -> None:
    """Drop every table, **including the version marker**.

    ``Tests only`` — never call this from a request path.

    The version marker has to go too, and that is the non-obvious part: leaving
    it behind makes :func:`migrate` believe the schema is current and skip
    re-creating the tables, so a "reset" database has no tables at all. The
    reset then looks like it worked and every later test fails on a missing
    table.
    """
    async def _run():
        conn = await connect()
        try:
            rows = await conn.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            for row in rows:
                name = row[0]
                # `sqlite_*` covers SQLite's own bookkeeping tables
                # (`sqlite_sequence`, `sqlite_stat1`). Dropping one is an
                # error, and a partial reset that dies on the third table
                # leaves the "empty" database partly populated.
                if name.startswith("sqlite_"):
                    continue
                await conn.execute(f"DROP TABLE IF EXISTS {name}")
            await conn.commit()
        finally:
            await conn.close()

    try:
        await _with_retry(_run)
    except Exception as exc:  # noqa: BLE001
        logger.warning(safe_error(exc, operation="db.reset", component="core.db"))


__all__ = [
    "SCHEMA_VERSION",
    "connect",
    "db_path",
    "execute",
    "execute_many",
    "fetch_all",
    "fetch_one",
    "kv_get",
    "kv_set",
    "migrate",
    "reset_for_tests",
]