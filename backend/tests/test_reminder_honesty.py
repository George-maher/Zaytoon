"""A reminder operation must report whether it actually happened.

`delete_reminder` and `complete_reminder` both ended with an unconditional
`return True`, so asking to delete an id that did not exist reported success.
That value is relayed to the UI as "Deleted." and the desktop removes the row
on it, so a stale or already-fired id made the app assert an action it never
performed -- exactly the failure mode the rest of this codebase is careful to
avoid (see the anti-hallucination rules in agents/prompts.py and the
"never claim an action was performed" line in AGENTS.md).
"""

from __future__ import annotations

import os
import tempfile

import pytest

from agents.memory_agent import MemoryAgent


@pytest.fixture
async def agent():
    """A MemoryAgent on a throwaway database.

    The path comes from ``DATABASE_URL`` because that is what the agent reads;
    setting a ``db_path`` attribute silently does nothing and the agent would
    then open -- and leave locked -- the real development database.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    mem = MemoryAgent()
    # The schema is created by the agent, not by the test, and _get_db() alone
    # hands back a connection to a database with no tables in it.
    await mem._ensure_initialized()
    try:
        yield mem
    finally:
        # The aiosqlite handle must close before unlink, or Windows raises
        # WinError 32 and the temp file leaks.
        await mem.close()
        try:
            os.unlink(db_path)
        except OSError:
            pass
        os.environ.pop("DATABASE_URL", None)


async def _add(mem, title="stand up") -> int:
    """Insert a pending reminder. Columns match the agent's own schema."""
    db = await mem._get_db()
    await db.execute(
        "INSERT INTO reminders (title, remind_time, status) VALUES (?, ?, 'pending')",
        (title, "2026-01-01 00:00:00"),
    )
    await db.commit()
    cur = await db.execute("SELECT id FROM reminders ORDER BY id DESC LIMIT 1")
    row = await cur.fetchone()
    return int(row["id"])


class TestDeleteIsHonest:
    @pytest.mark.asyncio
    async def test_missing_id_reports_failure(self, agent):
        assert await agent.delete_reminder(999999) is False

    @pytest.mark.asyncio
    async def test_existing_id_reports_success(self, agent):
        rid = await _add(agent)
        assert await agent.delete_reminder(rid) is True

    @pytest.mark.asyncio
    async def test_deleting_twice_fails_the_second_time(self, agent):
        rid = await _add(agent)
        assert await agent.delete_reminder(rid) is True
        assert await agent.delete_reminder(rid) is False


class TestCompleteIsHonest:
    @pytest.mark.asyncio
    async def test_missing_id_reports_failure(self, agent):
        assert await agent.complete_reminder(999999) is False

    @pytest.mark.asyncio
    async def test_existing_id_reports_success(self, agent):
        rid = await _add(agent)
        assert await agent.complete_reminder(rid) is True
        db = await agent._get_db()
        cur = await db.execute("SELECT status FROM reminders WHERE id = ?", (rid,))
        row = await cur.fetchone()
        assert row["status"] == "completed"
