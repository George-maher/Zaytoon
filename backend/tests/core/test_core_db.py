"""The core database: schema, migration and the never-raise contract."""

import asyncio

import pytest

from core import db


class TestSchema:
    async def test_connect_creates_every_table(self, temp_core_db):
        conn = await db.connect()
        try:
            rows = await conn.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            names = {row[0] for row in rows}
        finally:
            await conn.close()

        for expected in (
            "projects", "tasks", "task_steps", "task_logs", "providers",
            "provider_secrets", "models", "skills", "permission_grants", "kv",
        ):
            assert expected in names, f"{expected} table missing"

    async def test_migrate_is_idempotent(self, temp_core_db):
        conn = await db.connect()
        try:
            first = await db.migrate(conn)
            second = await db.migrate(conn)
            rows = await conn.execute_fetchall("SELECT version FROM schema_version")
        finally:
            await conn.close()

        assert first == second == db.SCHEMA_VERSION
        # One row, not one per connect. A row per open would make the stored
        # version ambiguous on the next read.
        assert len(rows) == 1

    async def test_journal_mode_is_wal(self, temp_core_db):
        # WAL is what lets the settings page read while a task writes.
        conn = await db.connect()
        try:
            rows = await conn.execute_fetchall("PRAGMA journal_mode")
        finally:
            await conn.close()
        assert rows[0][0].lower() == "wal"

    async def test_uses_its_own_file(self, temp_core_db):
        """The core database must not share a handle with memory or audit.

        `common/audit.py` records the reason: sharing zaytoon.db with the memory
        agent made every clear fail with "database is locked".
        """
        assert "audit" not in db.db_path()
        assert "zaytoon.db" not in db.db_path()

    async def test_db_path_honours_the_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CORE_DB_PATH", str(tmp_path / "custom.db"))
        assert db.db_path() == str(tmp_path / "custom.db")


class TestHelpers:
    async def test_fetch_all_returns_empty_on_a_bad_query(self, temp_core_db):
        # Never raises: a route reading a missing column must not 500 the API.
        assert await db.fetch_all("SELECT * FROM no_such_table") == []

    async def test_execute_reports_failure_without_raising(self, temp_core_db):
        assert await db.execute("INSERT INTO no_such_table VALUES (1)") is False

    async def test_execute_reports_success(self, temp_core_db):
        assert await db.execute(
            "INSERT INTO kv (key, value, updated_at) VALUES (?,?,?)",
            ("k", "v", "2026-01-01T00:00:00+00:00"),
        ) is True

    async def test_kv_round_trip_and_upsert(self, temp_core_db):
        await db.kv_set("greeting", "hello")
        assert await db.kv_get("greeting") == "hello"
        await db.kv_set("greeting", "goodbye")
        assert await db.kv_get("greeting") == "goodbye"

    async def test_kv_get_missing_returns_the_default(self, temp_core_db):
        assert await db.kv_get("absent") is None
        assert await db.kv_get("absent", "fallback") == "fallback"

    async def test_execute_many_is_atomic(self, temp_core_db):
        stamp = "2026-01-01T00:00:00+00:00"
        ok = await db.execute_many([
            ("INSERT INTO kv (key, value, updated_at) VALUES (?,?,?)", ("a", "1", stamp)),
            ("INSERT INTO kv (key, value, updated_at) VALUES (?,?,?)", ("b", "2", stamp)),
        ])
        assert ok is True
        assert await db.kv_get("a") == "1"
        assert await db.kv_get("b") == "2"

    async def test_execute_many_with_nothing_is_a_no_op(self, temp_core_db):
        assert await db.execute_many([]) is True

    async def test_execute_many_with_a_bad_statement_does_not_raise(self, temp_core_db):
        assert await db.execute_many([
            ("INSERT INTO kv (key, value, updated_at) VALUES (?,?,?)",
             ("ok", "1", "2026-01-01T00:00:00+00:00")),
            ("INSERT INTO nope VALUES (1)", ()),
        ]) is False

    async def test_fetch_one_returns_none_for_no_rows(self, temp_core_db):
        assert await db.fetch_one("SELECT * FROM kv WHERE key = ?", ("absent",)) is None


class TestReset:
    async def test_reset_drops_the_data(self, temp_core_db):
        await db.kv_set("temp", "value")
        await db.reset_for_tests()
        assert await db.kv_get("temp") is None

    async def test_reset_keeps_the_schema(self, temp_core_db):
        """After a reset the schema must still be usable."""
        await db.reset_for_tests()
        assert await db.kv_set("again", "value") is True

    async def test_reset_is_idempotent(self, temp_core_db):
        await db.reset_for_tests()
        await db.reset_for_tests()
        assert await db.kv_set("value", "1") is True