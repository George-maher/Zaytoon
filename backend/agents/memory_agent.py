import os
import asyncio
import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional

import aiosqlite

from common.concurrency import ReentrantAsyncLock
from common.reporting import redact, safe_error

logger = logging.getLogger("Zaytoon.MemoryAgent")


#: Rows claimed per tick. Unbounded claiming is how a machine that slept for a
#: week delivers 400 notifications at once: the app becomes the thing the user
#: notices instead of the reminders they asked for.
MAX_REMINDERS_PER_TICK = 20


class MemoryAgent:
    def __init__(self):
        raw = os.getenv("DATABASE_URL", "sqlite:///./data/zaytoon.db")
        if raw.startswith("sqlite:///"):
            self.db_path = raw[len("sqlite:///"):]
        else:
            from urllib.parse import urlparse
            parsed = urlparse(raw)
            self.db_path = parsed.path.lstrip("/")
        db_dir = os.path.dirname(os.path.abspath(self.db_path))
        os.makedirs(db_dir, exist_ok=True)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        #: Sessions with a compaction in flight — see `summarize_and_prune`.
        self._compacting: set = set()
        #: Serialises writes. aiosqlite runs each connection on its own thread,
        #: and two coroutines interleaving `execute`/`commit` on one connection
        #: can commit a half-written pair. Every mutation takes this lock. It
        #: is re-entrant so a writer may call another writer
        #: (`get_due_reminders` completes *and* re-schedules) without
        #: deadlocking against itself.
        self._write_lock = ReentrantAsyncLock()
        #: Serialises *connection creation*. Two coroutines could both observe
        #: `self._db is None`, both call `aiosqlite.connect`, and one handle
        #: would silently replace the other — a leaked connection and a split
        #: view of the schema. Batched tool calls made that race real.
        self._connect_lock = asyncio.Lock()
        self._initialized = False

    async def _get_db(self) -> aiosqlite.Connection:
        if self._db is not None:
            return self._db
        async with self._connect_lock:
            if self._db is None:
                db = await aiosqlite.connect(self.db_path)
                db.row_factory = aiosqlite.Row
                # Increment G: WAL lets the reader (context lookup) and the
                # writer (store_message) stop fighting over the single file
                # lock, and busy_timeout turns "database is locked" from an
                # immediate crash into a bounded wait. Both are no-ops on a
                # database that cannot take them, so this never makes an
                # existing setup worse.
                try:
                    await db.execute("PRAGMA journal_mode=WAL")
                    await db.execute("PRAGMA busy_timeout=5000")
                    await db.execute("PRAGMA synchronous=NORMAL")
                except aiosqlite.Error as e:
                    safe_error(e, operation="pragma", component="MemoryAgent")
                self._db = db
        return self._db

    async def _ensure_initialized(self):
        if self._initialized:
            return
        async with self._lock:
            if self._initialized:
                return
            db = await self._get_db()
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    language TEXT DEFAULT 'en',
                    session_id TEXT DEFAULT 'default',
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    remind_time DATETIME NOT NULL,
                    recurring TEXT DEFAULT 'none',
                    status TEXT DEFAULT 'pending',
                    session_id TEXT DEFAULT 'default',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS preferences (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS context_summary (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    summary TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_conversations_session
                    ON conversations(session_id, timestamp);
                CREATE INDEX IF NOT EXISTS idx_reminders_time
                    ON reminders(status, remind_time);
            """)
            await self._migrate(db)
            await db.commit()
            self._initialized = True

    async def _migrate(self, db):
        try:
            await db.execute("SELECT session_id FROM context_summary LIMIT 1")
        except Exception:  # noqa: BLE001 - this read *is* the "column missing" test
            try:
                await db.execute(
                    "ALTER TABLE context_summary ADD COLUMN session_id TEXT DEFAULT 'default'"
                )
            except Exception as exc:  # noqa: BLE001 - re-run of a done migration
                # The failing `SELECT` above is the expected path and stays quiet.
                # The failing `ALTER` does not: it left `context_summary` without
                # the column every later query assumes, so the turn blew up
                # somewhere far away with nothing pointing back to the migration
                # that never ran. Same guard-and-report shape as the reminder
                # columns below, because "a re-run is a no-op" and "we could not
                # migrate" are different events that used to log identically (that
                # is to say: not at all).
                if "duplicate column" not in str(exc).lower():
                    safe_error(
                        exc,
                        operation="migrate.context_summary.session_id",
                        component="MemoryAgent",
                    )

        # Delivered-or-not has to be countable *in the row*: `notify_attempts` on
        # the data class was recomputed from zero on every tick and never
        # persisted, so nothing could tell a first attempt from a third.
        # The `duplicate column name` guard is this file's existing idiom for an
        # idempotent ALTER, so a re-run is a no-op rather than an error.
        for column, ddl in (
            (
                "attempts",
                "ALTER TABLE reminders ADD COLUMN attempts INTEGER DEFAULT 0",
            ),
            # The claim token: *which* batch took a row. `status = 'firing'` on
            # its own only says somebody is working on it, so the next read would
            # hand the same reminder out again while the first delivery was still
            # in flight — a slow notification is enough. See `get_due_reminders`.
            (
                "claimed_at",
                "ALTER TABLE reminders ADD COLUMN claimed_at TEXT",
            ),
        ):
            try:
                await db.execute(ddl)
            except Exception as exc:  # noqa: BLE001 - re-run of a done migration
                if "duplicate column" not in str(exc).lower():
                    safe_error(
                        exc,
                        operation=f"migrate.reminders.{column}",
                        component="MemoryAgent",
                    )

    async def store_message(
        self, role: str, content: str, language: str = "en", session_id: str = "default"
    ) -> None:
        """Persist one turn. Best-effort: annotating a conversation must never
        be the reason the conversation fails, so a write error is reported as
        one structured line and swallowed.
        """
        try:
            await self._ensure_initialized()
            async with self._write_lock:
                db = await self._get_db()
                await db.execute(
                    "INSERT INTO conversations (role, content, language, session_id) VALUES (?, ?, ?, ?)",
                    (role, content, language, session_id),
                )
                await db.commit()
        except Exception as e:  # noqa: BLE001 - memory is best-effort
            safe_error(e, operation="store_message", component="MemoryAgent")

    async def get_recent_context(
        self, limit: int = 6, session_id: str = "default"
    ) -> List[Dict[str, Any]]:
        await self._ensure_initialized()
        db = await self._get_db()
        async with db.execute(
            "SELECT role, content, language, timestamp FROM conversations "
            "WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?",
            (session_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            summary = await self._get_summary(session_id)
            ctx = [dict(row) for row in reversed(rows)]
            if summary and len(ctx) < limit:
                ctx.insert(0, {"role": "system", "content": f"[Previous context: {summary}]"})
            return ctx

    async def _get_summary(self, session_id: str) -> Optional[str]:
        await self._ensure_initialized()
        db = await self._get_db()
        async with db.execute(
            "SELECT summary FROM context_summary WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return row["summary"] if row else None

    #: Sessions with a compaction in flight. The transport fires this as a
    #: bare `asyncio.create_task` on *every* message, so two quick messages
    #: started two concurrent runs over the same ten rows: both summarised the
    #: same batches, both inserted a summary row (the table grew by two
    #: identical "Previous conversation" lines), and the delete in the second
    #: run re-read "the ten oldest" *after* the first had removed them — so it
    #: deleted ten rows that had never been summarised. That is silent history
    #: loss, which is exactly what a compaction step must not do. Single-flight
    #: per session fixes both halves: one run at a time, and the delete is by
    #: the id list the read actually produced.
    async def summarize_and_prune(self, session_id: str = "default"):
        """Roll old turns into one line, then delete them.

        The read and the (potentially slow) summariser deliberately run
        *outside* the write lock — holding a lock across an LLM call would
        stall every other writer for seconds. Only the INSERT/DELETE pair is
        serialised, which is the part that must not interleave.

        Single-flight per session, and the delete names the exact rows the
        summary was built from. Both halves were absent, and together they made
        concurrent compaction destructive rather than merely wasteful.
        """
        if session_id in self._compacting:
            logger.debug("Compaction already running for %s; skipping", session_id)
            return
        self._compacting.add(session_id)
        try:
            await self._summarize_and_prune_once(session_id)
        finally:
            self._compacting.discard(session_id)

    async def _summarize_and_prune_once(self, session_id: str = "default") -> None:
        try:
            await self._ensure_initialized()
            db = await self._get_db()
            async with db.execute(
                "SELECT COUNT(*) as cnt FROM conversations WHERE session_id = ?",
                (session_id,),
            ) as cursor:
                row = await cursor.fetchone()
                if not row or row["cnt"] < 20:
                    return

            # The ids are captured with the rows they belong to, and the delete
            # below uses *these* ids. It used to re-run "the ten oldest" at
            # delete time, which is a different set whenever another writer (or
            # the concurrent compaction described above) removed rows in
            # between — the summary would then describe ten messages while ten
            # *other* messages were deleted.
            async with db.execute(
                "SELECT id, role, content FROM conversations WHERE session_id = ? "
                "ORDER BY timestamp ASC, id ASC LIMIT ?",
                (session_id, 10),
            ) as cursor:
                old = await cursor.fetchall()

            lines = [f"{r['role']}: {r['content'][:100]}" for r in old]
            text = " | ".join(lines)
            old_ids = [r["id"] for r in old]

            summary = await self._summarize(text)
            # The summary *is* conversation content (it is a paraphrase of ten
            # user and assistant turns), so it must not reach a log line at
            # INFO — that is the same leak as logging a message body, one step
            # removed. Length is the part the operator can act on.
            logger.info(
                "Summarised %d messages for %s (%d chars)",
                len(old), session_id, len(summary or ""),
            )

            async with self._write_lock:
                db = await self._get_db()
                await db.execute(
                    "INSERT INTO context_summary (summary, session_id) VALUES (?, ?)",
                    (summary, session_id),
                )
                if old_ids:
                    placeholders = ",".join("?" for _ in old_ids)
                    await db.execute(
                        f"DELETE FROM conversations WHERE id IN ({placeholders})",
                        old_ids,
                    )
                await db.commit()
        except Exception as e:  # noqa: BLE001 - fire-and-forget by design
            # This runs as a bare `asyncio.create_task`, where an escaping
            # exception is reported to nobody. Catch it here or it is lost.
            safe_error(e, operation="summarize_and_prune", component="MemoryAgent")

    async def _summarize(self, text: str) -> str:
        """Compress old turns into one line, best-effort.

        This used to do ``BaseAgent("Summarizer", ...)``. ``BaseAgent`` is an
        ABC with an abstract ``process`` method, so that call raised
        ``TypeError: Can't instantiate abstract class`` on *every* invocation —
        and a bare ``except Exception`` swallowed it, so every stored summary was
        the literal string ``"Previous conversation"`` forever. Silent, permanent,
        and untested.

        The real summariser is a proper agent now. When it cannot run (no API
        key, provider down) or returns a failure report, we fall back to a
        mechanical extract, which is still more useful than a constant.
        """
        try:
            from agents.summarizer_agent import SummarizerAgent

            summary = await SummarizerAgent().process(text, [])
            if summary and not SummarizerAgent.looks_like_an_error(summary):
                return summary
            # `summary` here is model output that failed validation, so it can
            # carry anything the model was shown — including the conversation
            # being summarised. Redacted, shortened, and still enough to tell
            # "returned an error object" from "returned nothing".
            logger.info(
                "Summariser returned nothing usable: %r",
                redact(str(summary)[:80]),
            )
        except Exception as e:
            # `component` carries the word "Summariser" because the regression
            # this reports was a *silent* failure — `test_fallback_is_logged_
            # not_silent` asserts the name appears in the log.
            safe_error(
                e, operation="summarize", component="MemoryAgent.Summariser",
                detail="using the extract fallback",
            )

        return self._extract_summary(text)

    @staticmethod
    def _extract_summary(text: str, limit: int = 200) -> str:
        """Deterministic fallback: the first meaningful user request.

        Used when the LLM is unreachable, so long-running sessions still get a
        real (if crude) rolling summary instead of a constant string.
        """
        for part in text.split("|"):
            chunk = part.strip()
            if chunk.lower().startswith("user:"):
                body = chunk[5:].strip()
                if body:
                    return body[:limit]
        from agents.summarizer_agent import FALLBACK_SUMMARY

        return text.strip()[:limit] or FALLBACK_SUMMARY

    async def get_conversation_summary(
        self, session_id: str = "default", limit: int = 30
    ) -> str:
        await self._ensure_initialized()
        db = await self._get_db()
        async with db.execute(
            "SELECT role, content FROM conversations WHERE session_id = ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (session_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            lines = []
            for row in reversed(rows):
                label = "User" if row["role"] == "user" else "Zaytoon"
                lines.append(f"{label}: {row['content']}")
            return "\n".join(lines)

    async def search_memory(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        await self._ensure_initialized()
        db = await self._get_db()
        async with db.execute(
            "SELECT role, content, timestamp FROM conversations "
            "WHERE content LIKE ? ORDER BY timestamp DESC LIMIT ?",
            (f"%{query}%", limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def clear_memory(self, session_id: str = "default") -> None:
        await self._ensure_initialized()
        try:
            async with self._write_lock:
                db = await self._get_db()
                await db.execute(
                    "DELETE FROM conversations WHERE session_id = ?", (session_id,)
                )
                await db.commit()
        except Exception as e:  # noqa: BLE001 - reported, never leaked
            safe_error(e, operation="clear_memory", component="MemoryAgent")

    async def add_reminder(
        self,
        title: str,
        remind_time: str,
        description: str = "",
        recurring: str = "none",
        timezone_offset: int = 0,
        session_id: str = "default",
    ) -> Dict[str, Any]:
        await self._ensure_initialized()
        try:
            local_dt = datetime.strptime(remind_time, "%Y-%m-%d %H:%M:%S")
            utc_dt = local_dt - timedelta(hours=timezone_offset)
            utc_time = utc_dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return {"error": "Invalid date/time format. Use YYYY-MM-DD HH:MM:SS"}

        try:
            async with self._write_lock:
                db = await self._get_db()
                cursor = await db.execute(
                    "INSERT INTO reminders (title, description, remind_time, recurring, session_id) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (title, description, utc_time, recurring, session_id),
                )
                await db.commit()
        except Exception as e:  # noqa: BLE001 - reported, never leaked
            # Returning a failure is the honest answer: the caller (and the
            # panel built on it) must not be told a reminder exists when the
            # insert never committed.
            safe_error(e, operation="add_reminder", component="MemoryAgent")
            return {"error": "The reminder could not be saved."}
        return {
            "id": cursor.lastrowid,
            "title": title,
            "remind_time": remind_time,
            "utc_time": utc_time,
            "recurring": recurring,
        }

    async def list_reminders(
        self, status: str = "pending", session_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Rows for ``status`` in due-first order.

        ``session_id`` is **attribution, not a partition**: a reminder set from
        a conversation must outlive the socket that created it — the WS session
        id changes on every reconnect, so filtering by it hides a reminder from
        every future view of the panel. The default therefore lists every
        session; an explicit id still filters for callers that want one
        session's rows.

        The firing path (`get_due_reminders`) never filtered on session, which
        is why this showed up as "the reminder says it was set but the panel is
        empty" rather than as a reminder that never fired.
        """
        await self._ensure_initialized()
        db = await self._get_db()
        if session_id:
            query = (
                "SELECT * FROM reminders WHERE status = ? AND session_id = ? "
                "ORDER BY remind_time"
            )
            params: tuple = (status, session_id)
        else:
            query = "SELECT * FROM reminders WHERE status = ? ORDER BY remind_time"
            params = (status,)
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def delete_reminder(self, reminder_id: int) -> bool:
        """Delete a reminder. Returns whether a row was actually removed.

        These used to return ``True`` unconditionally, so deleting an id that
        did not exist reported success. The WebSocket handler relays that flag
        to the UI as "Deleted.", and the desktop removes the row from its panel
        on it -- so a stale or already-fired id made the app assert an action it
        never performed. The row count is the only honest answer.
        """
        await self._ensure_initialized()
        try:
            async with self._write_lock:
                db = await self._get_db()
                cursor = await db.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
                await db.commit()
        except Exception as e:  # noqa: BLE001 - reported, never leaked
            safe_error(e, operation="delete_reminder", component="MemoryAgent")
            return False
        return cursor.rowcount > 0

    async def complete_reminder(self, reminder_id: int) -> bool:
        """Mark a reminder completed. Returns whether a row was updated."""
        await self._ensure_initialized()
        try:
            async with self._write_lock:
                db = await self._get_db()
                cursor = await db.execute(
                    "UPDATE reminders SET status = 'completed' WHERE id = ?",
                    (reminder_id,),
                )
                await db.commit()
        except Exception as e:  # noqa: BLE001 - reported, never leaked
            safe_error(e, operation="complete_reminder", component="MemoryAgent")
            return False
        return cursor.rowcount > 0

    async def get_due_reminders(self) -> List[Dict[str, Any]]:
        """**Claim** every pending reminder whose time has come; deliver none.

        This used to *complete* each row it read and *insert the next
        occurrence* of a recurring one. Both were wrong, and in opposite
        directions:

        * a notification that then failed left the row completed, so the
          reminder was consumed without being delivered — nothing would read it
          again, and `mark_failed` only changed the status of a dead row;
        * the scheduler *also* reschedules after a verified delivery
          (`ReminderService.handle_recurring`), so a daily reminder got **two**
          next occurrences per fire, doubling every day. The extra copy was
          also inserted through `add_reminder` without `session_id` or
          `timezone_offset`, so it appeared in the wrong session at the wrong
          hour.

        A read must not mutate. The only thing owed at claim time is that nobody
        else takes the same row, which one UPDATE does atomically — the
        `status = 'pending'` predicate is evaluated by the statement itself, so
        two overlapping ticks cannot both claim it. Completion and rescheduling
        now have exactly one owner: ``SchedulerManager._fire_reminder``, after a
        verified delivery.
        """
        await self._ensure_initialized()
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        async with self._write_lock:
            db = await self._get_db()
            token = uuid.uuid4().hex
            await db.execute(
                "UPDATE reminders SET status = 'firing', attempts = attempts + 1, "
                "claimed_at = ? "
                "WHERE id IN ("
                "  SELECT id FROM reminders"
                "  WHERE status = 'pending' AND remind_time <= ?"
                "  ORDER BY remind_time"
                "  LIMIT ?"
                ")",
                (token, now_utc, MAX_REMINDERS_PER_TICK),
            )
            await db.commit()
            async with db.execute(
                # Only *this* batch. `status = 'firing' AND remind_time <= now`
                # would also match rows another caller claimed and has not
                # delivered yet, which is precisely the duplicate being prevented;
                # `claimed_at` is the token written by the UPDATE above.
                "SELECT id, title, description, remind_time, recurring, session_id, "
                "       status, attempts, created_at "
                "FROM reminders WHERE claimed_at = ? AND status = 'firing' "
                "ORDER BY remind_time",
                (token,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def release_reminder(self, reminder_id: int, max_attempts: int = 3) -> str:
        """Give a failed delivery back, or stop trying. Returns the new status.

        ``pending`` lets the next tick retry; ``failed`` is reached only once the
        persisted ``attempts`` count says we have tried ``max_attempts`` times —
        which, being in the row, stays true across a restart too.
        """
        await self._ensure_initialized()
        async with self._write_lock:
            db = await self._get_db()
            cursor = await db.execute(
                "UPDATE reminders SET status = CASE WHEN attempts >= ? "
                "THEN 'failed' ELSE 'pending' END, claimed_at = NULL "
                "WHERE id = ? AND status = 'firing'",
                (max_attempts, reminder_id),
            )
            await db.commit()
            async with db.execute(
                "SELECT status FROM reminders WHERE id = ?", (reminder_id,)
            ) as check:
                row = await check.fetchone()
        # The status is read back rather than computed from `rowcount`, because
        # `rowcount == 0` means "this row was not in `firing`" — already
        # released, deleted, or never claimed. Reporting `pending` in that case
        # would promise a retry that the next tick will not perform.
        return row["status"] if row else "gone"

    async def requeue_stale_claims(self) -> int:
        """Return rows left ``firing`` by a previous process.

        A claim must eventually be released or the reminder is neither delivered
        nor visible: the panel lists `pending`, so a row stranded in `firing` by
        a crash or a `kill` between claim and delivery simply vanishes. Nothing
        can be legitimately firing at startup, so every such row is due again.
        Re-notifying after a crash is the lesser harm beside losing it.
        """
        await self._ensure_initialized()
        async with self._write_lock:
            db = await self._get_db()
            cursor = await db.execute(
                "UPDATE reminders SET status = 'pending', claimed_at = NULL "
                "WHERE status = 'firing'"
            )
            await db.commit()
        count = cursor.rowcount
        if count:
            logger.info("Requeued %d reminder(s) claimed by a previous run", count)
        return count

    async def mark_reminder_done(self, reminder_id: int) -> None:
        await self.complete_reminder(reminder_id)

    async def fail_reminder(self, reminder_id: int) -> None:
        await self._ensure_initialized()
        db = await self._get_db()
        await db.execute(
            "UPDATE reminders SET status = 'failed' WHERE id = ?",
            (reminder_id,),
        )
        await db.commit()

    async def get_preference(self, key: str, default: str = "") -> str:
        await self._ensure_initialized()
        db = await self._get_db()
        async with db.execute(
            "SELECT value FROM preferences WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
            return row["value"] if row else default

    async def set_preference(self, key: str, value: str) -> None:
        await self._ensure_initialized()
        db = await self._get_db()
        await db.execute(
            "INSERT OR REPLACE INTO preferences (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (key, value),
        )
        await db.commit()

    async def close(self):
        if self._db:
            await self._db.close()
            self._db = None
