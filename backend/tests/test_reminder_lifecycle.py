"""The reminder lifecycle: claim, deliver, settle — never consume on read.

Found by reading `MemoryAgent.get_due_reminders` against
`SchedulerManager._fire_reminder`. Both believed they owned the recurring
schedule:

    get_due_reminders():  complete this row  +  insert tomorrow's row
    _fire_reminder():                        insert tomorrow's row  (on success)

so a daily reminder grew a second occurrence every day it fired (1 row → 2 →
4 → 8 …), and the extra rows lost `session_id`, which is what made the panel
look empty while reminders still went off. Worse in the other direction: because
the row was completed *at read time*, a notification that failed afterwards
consumed the reminder — `mark_failed` only renamed a row nobody would select
again. A reminder set before closing the laptop was gone, not delayed.

These tests exercise the real store and the real scheduler tick against a
disposable database; the assertions are on rows, not on log lines.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.memory_agent import MemoryAgent  # noqa: E402
from reminder.models import ReminderStatus  # noqa: E402
from reminder.service import ReminderService  # noqa: E402
from scheduler.manager import MAX_RETRIES, SchedulerManager  # noqa: E402


def _rows(db_path: str, where: str = "") -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f"SELECT id, title, status, session_id FROM reminders {where}").fetchall()
    finally:
        conn.close()


def _past(minutes: int = 5, days: int = 0) -> str:
    """A time already past, in the format `add_reminder` stores (UTC, no tz).

    `days` exists so the recurring tests can start far enough back that the
    generated next occurrence is *also* overdue: that keeps the test driving the
    real `handle_recurring` arithmetic instead of a frozen clock.
    """
    return (
        datetime.now(timezone.utc) - timedelta(days=days, minutes=minutes)
    ).strftime("%Y-%m-%d %H:%M:%S")


def _future(days: int = 1) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


@pytest.fixture(autouse=True)
def no_backoff_sleeps(monkeypatch):
    """Zero the retry delays for the whole module.

    `RETRY_DELAYS` is 1+5+15 s of real sleeping, which made the failure cases
    take a minute of wall clock to prove three calls were made. The delays are
    asserted where they matter (see `test_a_persistent_failure_...`, which checks
    the *count*, not the schedule).
    """
    import scheduler.manager as mod

    monkeypatch.setattr(mod, "RETRY_DELAYS", [0, 0, 0])


class _Notify:
    """Notification manager double. `result` may be a value or a callable."""

    def __init__(self, result=True):
        self.result = result
        self.calls: list[dict] = []

    async def notify(self, payload):
        self.calls.append(payload)
        if callable(self.result):
            return await self.result(payload)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
async def store():
    """A `MemoryAgent` on a throwaway database, as the agent itself opens it.

    The path comes from ``DATABASE_URL`` — ``MemoryAgent`` takes no `db_path`
    argument, so passing one would silently leave the agent pointed at the real
    development database and the test would be asserting against user data.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
        path = handle.name
    os.environ["DATABASE_URL"] = f"sqlite:///{path}"
    agent = MemoryAgent()
    await agent._ensure_initialized()  # noqa: SLF001 - schema is the agent's job
    try:
        yield agent, path
    finally:
        await agent.close()
        try:
            os.unlink(path)
        except OSError:  # pragma: no cover - Windows file lock
            pass
        os.environ.pop("DATABASE_URL", None)


class _Tick:
    """Runs one scheduler tick without starting the background loop."""

    def __init__(self, store):
        self.service = ReminderService(store)
        self.notify = _Notify()
        self.manager = SchedulerManager(self.service, self.notify)

    async def once(self) -> int:
        """One real tick, through `deliver_due` — the loop's own body.

        Returns how many reminders were claimed, which is what every assertion
        below wants: not "did a log line appear", but "one row was taken".
        """
        return await self.manager.deliver_due()


class TestClaimDoesNotConsume:
    async def test_reading_due_rows_leaves_them_uncompleted(self, store):
        memory, path = store
        added = await memory.add_reminder("stretch", _past())
        assert "error" not in added

        due = await memory.get_due_reminders()
        assert [r["title"] for r in due] == ["stretch"]
        status = _rows(path, "WHERE id = {}".format(added["id"]))[0][2]
        # 'firing' = claimed, in flight. 'completed' here means the store decided
        # the reminder was delivered before anyone tried.
        assert status == ReminderStatus.FIRING.value
        assert status != "completed"

    async def test_a_second_read_within_the_tick_sees_nothing_new(self, store):
        """Claiming is the exclusion; the second caller must get an empty hand."""
        memory, _ = store
        await memory.add_reminder("one", _past())
        first = await memory.get_due_reminders()
        second = await memory.get_due_reminders()
        assert len(first) == 1
        assert second == []

    async def test_two_overlapping_ticks_cannot_both_claim_one_row(self, store):
        """The claim is one UPDATE, so the predicate does the excluding."""
        memory, _ = store
        await memory.add_reminder("shared", _past())
        a, b = await asyncio.gather(memory.get_due_reminders(), memory.get_due_reminders())
        assert len(a) + len(b) == 1, f"double claim: {a} / {b}"

    async def test_a_delivery_failure_releases_the_row_for_the_next_tick(
        self, store
    ):
        memory, path = store
        added = await memory.add_reminder("call mom", _past())
        tick = _Tick(memory)
        tick.notify.result = False  # every attempt answered "not delivered"

        await tick.once()
        status = _rows(path, f"WHERE id = {added['id']}")[0][2]
        assert status == "pending", (
            "a reminder whose notification failed must be tried again; "
            f"'{status}' means it was consumed by the attempt"
        )

        # And it is actually retried, not merely labelled.
        assert await tick.once() == 1

    async def test_a_persistent_failure_stops_after_the_ceiling(self, store):
        """Retrying forever is how a dead channel becomes a log-spewing loop."""
        memory, path = store
        added = await memory.add_reminder("unreachable", _past())
        tick = _Tick(memory)
        tick.notify.result = RuntimeError("transport down")

        for _ in range(MAX_RETRIES):
            await tick.once()
        status = _rows(path, f"WHERE id = {added['id']}")[0][2]
        assert status == "failed"
        assert await tick.once() == 0, "a failed row must stop being retried"

    async def test_a_recovered_channel_delivers_the_released_reminder(self, store):
        memory, path = store
        added = await memory.add_reminder("after the blip", _past())
        tick = _Tick(memory)
        tick.notify.result = False
        await tick.once()
        assert _rows(path, f"WHERE id = {added['id']}")[0][2] == "pending"

        tick.notify.result = True
        assert await tick.once() == 1
        assert _rows(path, f"WHERE id = {added['id']}")[0][2] == "completed"
        # 3 attempts in the failed tick, then 1 in the delivered tick: the second
        # tick had to *earn* its delivery by re-reading a released row.
        assert len(tick.notify.calls) == MAX_RETRIES + 1


class TestRecurringOwnership:
    async def test_one_delivery_creates_exactly_one_next_occurrence(self, store):
        """The whole bug in one assertion: 2 pending rows was the old result."""
        memory, path = store
        await memory.add_reminder("daily standup", _past(), recurring="daily")
        tick = _Tick(memory)
        tick.notify.result = True

        await tick.once()
        pending = _rows(path, "WHERE status = 'pending'")
        assert len(pending) == 1, (
            "a fired daily reminder must have exactly one future occurrence; "
            f"{len(pending)} rows means the read path and the scheduler both inserted one"
        )
        completed = _rows(path, "WHERE status = 'completed'")
        assert len(completed) == 1, "the fired occurrence is the only completed row"

    async def test_the_count_does_not_double_over_repeated_days(self, store):
        """1 → 2 → 4 → 8 was the growth; it must stay one row per fire."""
        memory, path = store
        # Four days back, so every generated occurrence is itself overdue and the
        # next iteration can fire it without any clock fiddling.
        await memory.add_reminder("water", _past(days=4), recurring="daily")
        tick = _Tick(memory)
        tick.notify.result = True

        for day in range(4):
            fired = await tick.once()
            assert fired == 1, f"day {day}: {fired} reminders fired"
            # Each generated row is itself due now, so the next iteration fires it.
            total = _rows(path)
            assert len(total) == day + 2, f"day {day}: {len(total)} rows in the table"

    async def test_the_next_occurrence_keeps_its_session(self, store):
        """The duplicate used to be inserted without `session_id`."""
        memory, path = store
        await memory.add_reminder(
            "meds", _past(), recurring="daily", session_id="ws-42"
        )
        tick = _Tick(memory)
        tick.notify.result = True
        await tick.once()
        pending = _rows(path, "WHERE status = 'pending'")
        assert [row[3] for row in pending] == ["ws-42"]

    async def test_a_non_recurring_reminder_creates_nothing(self, store):
        memory, path = store
        await memory.add_reminder("one-off", _past())
        tick = _Tick(memory)
        tick.notify.result = True
        await tick.once()
        assert len(_rows(path)) == 1


class TestCrashRecovery:
    async def test_a_claim_left_by_a_dead_process_is_requeued(self, store):
        """Crash between claim and delivery used to lose the reminder entirely."""
        memory, path = store
        added = await memory.add_reminder("was firing", _past())
        await memory.get_due_reminders()  # claim, then "crash" before delivery
        assert _rows(path, f"WHERE id = {added['id']}")[0][2] == "firing"

        recovered = await memory.requeue_stale_claims()
        assert recovered == 1
        assert _rows(path, f"WHERE id = {added['id']}")[0][2] == "pending"
        assert await _Tick(memory).once() == 1

    async def test_startup_requeues_before_the_first_tick(self, store, monkeypatch):
        memory, path = store
        await memory.add_reminder("orphan", _past())
        await memory.get_due_reminders()
        _Tick(memory)  # ensure the helper imports cleanly

        service = ReminderService(memory)

        #: Records whether the background loop ever ran. The override below
        #: replaced the loop but appended to a name that was never defined, so
        #: the test would have raised `NameError: name 'slept' is not defined`
        #: if `start()` ever *did* await the loop — reporting a bug as a broken
        #: test rather than as "the loop ran when it should not have".
        ticks: list[str] = []

        class _NoLoop(SchedulerManager):
            async def _check_loop(self):  # pragma: no cover - replaced
                ticks.append("loop")

        manager = _NoLoop(service, _Notify())
        await manager.start()
        await manager.stop()
        assert ticks == [], (
            "`start()` awaited the tick loop; startup recovery must complete "
            "before the loop is allowed to claim rows"
        )
        assert _rows(path, "WHERE status = 'pending'"), (
            "`start()` must recover rows claimed by the previous run, or "
            "restarting the app silently deletes the reminders it missed"
        )

    async def test_requeue_leaves_completed_and_failed_rows_alone(self, store):
        memory, path = store
        done = await memory.add_reminder("done", _past())
        await memory.get_due_reminders()
        await memory.complete_reminder(done["id"])
        await memory.requeue_stale_claims()
        assert _rows(path, f"WHERE id = {done['id']}")[0][2] == "completed"


class TestClaimsAreBounded:
    async def test_a_backlog_is_claimed_in_batches(self, store, monkeypatch):
        """A machine that slept for a week must not deliver 500 at once."""
        from agents import memory_agent as mod

        monkeypatch.setattr(mod, "MAX_REMINDERS_PER_TICK", 3)
        memory, _ = store
        for i in range(10):
            await memory.add_reminder(f"backlog-{i}", _past(minutes=i + 1))
        assert len(await memory.get_due_reminders()) == 3
        assert len(await memory.get_due_reminders()) == 3

    async def test_rows_in_the_future_are_not_claimed(self, store):
        memory, _ = store
        await memory.add_reminder("later", _future())
        assert await memory.get_due_reminders() == []
