"""The execution audit log, and the one rule that keeps it honest.

An audit log that can fail a turn is not a log, it is a liability: the assistant
would refuse to answer because SQLite was locked. So most of this file is about
the failure path — a broken table, a bad path, a value that cannot be stored —
and the guarantee is that the turn continues and the write is dropped.

The other guarantee is that the log is not a secret store. Every text field is
redacted before it lands, which is asserted with a realistic token rather than a
truncated one.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import audit  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch, tmp_path):
    """Point the audit at its own database file per test."""
    db = tmp_path / "audit_test.db"
    monkeypatch.setattr(audit, "_db_path", lambda: str(db))
    monkeypatch.setattr(audit, "_WARNED", False)
    return db


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
class TestRecord:
    async def test_a_record_is_stored_with_its_fields(self):
        await audit.record(
            tool="execute_local_command",
            trace_id="trace-1",
            user_request="run the frontend tests",
            command="npm run test",
            cwd="C:/ws/frontend",
            risk="confirm",
            approved=True,
            success=True,
            exit_code=0,
            duration_ms=1234.5,
        )
        entries = await audit.recent(10)
        assert len(entries) == 1
        row = entries[0]
        assert row["tool"] == "execute_local_command"
        assert row["trace_id"] == "trace-1"
        assert row["user_request"] == "run the frontend tests"
        assert row["command"] == "npm run test"
        assert row["cwd"] == "C:/ws/frontend"
        assert row["risk"] == "confirm"
        assert row["approved"] == 1
        assert row["success"] == 1
        assert row["exit_code"] == 0
        assert row["duration_ms"] == 1234
        assert row["ts"], "a record without a timestamp is not a record"

    async def test_newest_first(self):
        for i in range(5):
            await audit.record(tool=f"tool_{i}")
        entries = await audit.recent(10)
        assert [e["tool"] for e in entries] == [
            "tool_4", "tool_3", "tool_2", "tool_1", "tool_0",
        ]

    async def test_a_declined_call_is_recorded_as_declined(self):
        await audit.record(tool="process_start", approved=False, success=False)
        row = (await audit.recent(1))[0]
        assert row["approved"] == 0
        assert row["success"] == 0

    async def test_a_safe_call_records_no_decision(self):
        """`None` means "never asked", which is not the same as "refused"."""
        await audit.record(tool="get_time", success=True)
        row = (await audit.recent(1))[0]
        assert row["approved"] is None

    async def test_a_failed_call_keeps_its_error(self):
        await audit.record(tool="execute_code", success=False, error="exit code 1")
        assert (await audit.recent(1))[0]["error"] == "exit code 1"


# ---------------------------------------------------------------------------
# The log must not become a secret store
# ---------------------------------------------------------------------------
class TestRedaction:
    SECRET = "sk-or-v1-abcdef0123456789abcdef0123456789"

    async def test_a_token_in_a_command_is_masked(self):
        await audit.record(tool="execute_local_command", command=f"curl -H auth {self.SECRET}")
        row = (await audit.recent(1))[0]
        assert self.SECRET not in row["command"]
        assert "***" in row["command"]

    async def test_a_token_in_an_error_is_masked(self):
        await audit.record(tool="execute_local_command", error=f"failed with key={self.SECRET}")
        assert self.SECRET not in (await audit.recent(1))[0]["error"]

    async def test_a_token_in_the_user_request_is_masked(self):
        await audit.record(
            tool="x", user_request=f"my api_key={self.SECRET} is broken"
        )
        assert self.SECRET not in (await audit.recent(1))[0]["user_request"]

    async def test_long_text_is_clipped(self):
        await audit.record(tool="x", user_request="a" * 5000)
        row = (await audit.recent(1))[0]
        assert len(row["user_request"]) <= 501, "a huge request must not become a huge row"

    async def test_a_non_string_field_does_not_crash_the_write(self):
        await audit.record(tool="x", command={"unexpected": object()})
        assert len(await audit.recent(1)) == 1


# ---------------------------------------------------------------------------
# Reading and clearing
# ---------------------------------------------------------------------------
class TestReadAndClear:
    async def test_limit_is_honoured_and_bounded(self):
        for i in range(10):
            await audit.record(tool=f"t{i}")
        assert len(await audit.recent(3)) == 3
        # Nonsense limits fall back rather than raising.
        assert len(await audit.recent("not a number")) == 10
        assert len(await audit.recent(99999)) == 10

    async def test_count_matches(self):
        for i in range(4):
            await audit.record(tool=f"t{i}")
        assert await audit.count() == 4

    async def test_clear_empties_the_table_and_reports_how_many(self):
        for i in range(3):
            await audit.record(tool=f"t{i}")
        assert await audit.clear() == 3
        assert await audit.recent(10) == []
        assert await audit.count() == 0

    async def test_clearing_an_empty_table_is_zero_not_an_error(self):
        assert await audit.clear() == 0


# ---------------------------------------------------------------------------
# The failure path: this is the actual point of the module
# ---------------------------------------------------------------------------
class TestFailuresAreSwallowed:
    async def test_a_write_to_an_unwritable_path_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(audit, "_db_path", lambda: "Z:/no/such/drive/audit.db")
        await audit.record(tool="execute_code", command="print(1)")  # must not raise

    async def test_reading_a_broken_table_returns_an_empty_list(self, monkeypatch):
        monkeypatch.setattr(audit, "_db_path", lambda: "Z:/no/such/drive/audit.db")
        assert await audit.recent(10) == []
        assert await audit.count() == 0
        assert await audit.clear() == 0

    async def test_a_dropped_table_is_recreated_on_the_next_write(self, isolated_db):
        """`CREATE TABLE IF NOT EXISTS` runs on every connect, so a table lost
        to a reset database comes back by itself rather than failing forever."""
        import aiosqlite

        conn = await aiosqlite.connect(str(isolated_db))
        await conn.execute("DROP TABLE IF EXISTS execution_audit")
        await conn.commit()
        await conn.close()

        await audit.record(tool="x")          # must not raise
        assert len(await audit.recent(5)) == 1