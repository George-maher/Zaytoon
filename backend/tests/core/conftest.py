"""Shared fixtures for the core runtime tests.

Every test in this directory touches the database, the secrets file or the
workspace root. Without isolation they would write into the developer's real
``data/`` directory and — much worse — into the real workspace, which is why the
workspace is redirected to a temp directory per test rather than mocked: the
containment rules are the thing under test, and a mock would be asserting
against a fiction.
"""

import asyncio
import os
import tempfile

import pytest


@pytest.fixture
def temp_core_db(monkeypatch, tmp_path):
    """Point the core database at a fresh temp file.

    Deliberately **synchronous** and deliberately *not* calling
    ``reset_for_tests``. ``tmp_path`` is unique per test, so the file does not
    exist yet and :func:`core.db.connect` creates the schema on first use — the
    database is empty by construction rather than by a cleanup step that could
    itself fail.

    The earlier version was an async fixture awaiting a reset, which pytest-asyncio
    cannot deliver to a **synchronous** test that requests it (the API tests use
    ``TestClient``). The fixture appeared to run and did nothing, so every test
    in the file shared one database and failed in ways that looked like product
    bugs.
    """
    path = tmp_path / "core.db"
    monkeypatch.setenv("CORE_DB_PATH", str(path))
    return str(path)


@pytest.fixture
def temp_secrets(monkeypatch, tmp_path):
    """Point the secrets file at a temp path and clear the process cache."""
    from core import secrets as secrets_module

    path = tmp_path / "secrets.json"
    monkeypatch.setenv("ZAYTOON_SECRETS_PATH", str(path))
    secrets_module.reset_cache()
    yield str(path)
    secrets_module.reset_cache()


@pytest.fixture
def temp_workspace(monkeypatch, tmp_path):
    """Redirect the workspace root to a temp directory.

    Uses the real ``config.workspace_path`` (via ``WORKSPACE_ROOT``), because
    the containment check resolves through it. Patching the sandbox helper
    instead would bypass the code path these tests exist to verify.
    """
    from config import config

    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(root), raising=False)
    return str(root)


@pytest.fixture
def temp_skills_dir(monkeypatch, tmp_path):
    """Redirect the skills root to a temp directory."""
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setenv("ZAYTOON_SKILLS_DIR", str(root))
    return str(root)


@pytest.fixture(autouse=True)
def _clean_core_state():
    """Reset the module-level task manager between tests.

    The manager holds asyncio handles from one test's task graph; a handle left
    behind is a task that outlives its test and fails in the next one.
    """
    from core.tasks.manager import TaskManager

    manager = TaskManager()
    manager._handles.clear()  # noqa: SLF001 - test isolation
    yield
    manager._handles.clear()  # noqa: SLF001