import os
import json
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple

import aiosqlite

logger = logging.getLogger("Zaytoon.MemoryAgent")


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
        self._initialized = False

    async def _get_db(self) -> aiosqlite.Connection:
        if self._db is None:
            self._db = await aiosqlite.connect(self.db_path)
            self._db.row_factory = aiosqlite.Row
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
        except Exception:
            try:
                await db.execute("ALTER TABLE context_summary ADD COLUMN session_id TEXT DEFAULT 'default'")
            except Exception:
                pass

    async def store_message(
        self, role: str, content: str, language: str = "en", session_id: str = "default"
    ) -> None:
        await self._ensure_initialized()
        db = await self._get_db()
        await db.execute(
            "INSERT INTO conversations (role, content, language, session_id) VALUES (?, ?, ?, ?)",
            (role, content, language, session_id),
        )
        await db.commit()

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

    async def summarize_and_prune(self, session_id: str = "default"):
        await self._ensure_initialized()
        db = await self._get_db()
        async with db.execute(
            "SELECT COUNT(*) as cnt FROM conversations WHERE session_id = ?",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row or row["cnt"] < 20:
                return

        async with db.execute(
            "SELECT role, content FROM conversations WHERE session_id = ? ORDER BY timestamp ASC LIMIT ?",
            (session_id, 10),
        ) as cursor:
            old = await cursor.fetchall()

        lines = [f"{r['role']}: {r['content'][:100]}" for r in old]
        text = " | ".join(lines)

        summary = await self._summarize(text)
        logger.info("Summarised %d messages for %s: %r", len(old), session_id, summary)

        await db.execute(
            "INSERT INTO context_summary (summary, session_id) VALUES (?, ?)",
            (summary, session_id),
        )
        async with db.execute(
            "SELECT id FROM conversations WHERE session_id = ? ORDER BY timestamp ASC LIMIT ?",
            (session_id, 10),
        ) as cursor:
            old_ids = [r["id"] for r in await cursor.fetchall()]
        if old_ids:
            placeholders = ",".join("?" for _ in old_ids)
            await db.execute(
                f"DELETE FROM conversations WHERE id IN ({placeholders})",
                old_ids,
            )
        await db.commit()

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
            logger.info("Summariser returned nothing usable: %r", summary[:80])
        except Exception as e:
            logger.warning("Summariser unavailable (%s); using extract fallback", e)

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
        db = await self._get_db()
        await db.execute(
            "DELETE FROM conversations WHERE session_id = ?", (session_id,)
        )
        await db.commit()

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

        db = await self._get_db()
        cursor = await db.execute(
            "INSERT INTO reminders (title, description, remind_time, recurring, session_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (title, description, utc_time, recurring, session_id),
        )
        await db.commit()
        return {
            "id": cursor.lastrowid,
            "title": title,
            "remind_time": remind_time,
            "utc_time": utc_time,
            "recurring": recurring,
        }

    async def list_reminders(
        self, status: str = "pending", session_id: str = "default"
    ) -> List[Dict[str, Any]]:
        await self._ensure_initialized()
        db = await self._get_db()
        async with db.execute(
            "SELECT * FROM reminders WHERE status = ? AND session_id = ? ORDER BY remind_time",
            (status, session_id),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def delete_reminder(self, reminder_id: int) -> bool:
        await self._ensure_initialized()
        db = await self._get_db()
        await db.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        await db.commit()
        return True

    async def complete_reminder(self, reminder_id: int) -> bool:
        await self._ensure_initialized()
        db = await self._get_db()
        await db.execute(
            "UPDATE reminders SET status = 'completed' WHERE id = ?",
            (reminder_id,),
        )
        await db.commit()
        return True

    async def get_due_reminders(self) -> List[Dict[str, Any]]:
        await self._ensure_initialized()
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        db = await self._get_db()
        async with db.execute(
            "SELECT id, title, description, remind_time, recurring FROM reminders "
            "WHERE status = 'pending' AND remind_time <= ?",
            (now_utc,),
        ) as cursor:
            rows = await cursor.fetchall()
            result = [dict(row) for row in rows]
            for r in result:
                await self.complete_reminder(r["id"])
                if r.get("recurring") and r["recurring"] != "none":
                    intervals = {
                        "daily": timedelta(days=1),
                        "weekly": timedelta(weeks=1),
                        "monthly": timedelta(days=30),
                    }
                    delta = intervals.get(r["recurring"], timedelta(days=1))
                    old_time = datetime.strptime(
                        r["remind_time"][:19], "%Y-%m-%d %H:%M:%S"
                    )
                    new_time = old_time + delta
                    await self.add_reminder(
                        title=r["title"],
                        remind_time=new_time.strftime("%Y-%m-%d %H:%M:%S"),
                        description=r.get("description", ""),
                        recurring=r["recurring"],
                    )
            return result

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
