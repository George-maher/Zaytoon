"""Shared test fixtures.

**Nothing here touches a real Zaytoon install.** Three SQLite files sit under
``backend/data`` and each one is live state, not scratch: ``zaytoon.db`` holds the
memory and *preference* rows the app reads back, ``audit.db`` is the execution
history the UI displays, and ``core.db`` is the task/project/skill/provider
registry. Importing ``api.main`` constructs a module-level ``MemoryAgent`` that
binds to ``./data/zaytoon.db``, so a test posting a preference wrote into the
developer's running assistant — and a ``pytest`` run against a live backend
lands in ``database is locked`` while the user is using it.

The redirect happens at *import* time rather than in a fixture because that
module-level agent is built when ``api.main`` is first imported, which is before
any fixture runs. ``tests/core/test_core_paths.py`` deletes these variables for
its own assertions, so the layout rules stay testable on their real defaults.

The suite predates multi-provider support: the routing, free-model and
contract tests all exercise the *OpenRouter* chain, so they pin the provider
explicitly rather than depending on the ambient default (Ollama). Tests that
cover the local path opt in by setting ``config.PROVIDER = "ollama"``
themselves (see ``test_ollama_provider.py``).
"""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from config import PROVIDER_OPENROUTER, config

#: Removed wholesale in `pytest_unconfigure`; a run leaves no state behind.
_ISOLATED_HOME = Path(tempfile.mkdtemp(prefix="zaytoon-tests-"))
os.environ["ZAYTOON_DATA_DIR"] = str(_ISOLATED_HOME)
os.environ["DATABASE_URL"] = f"sqlite:///{_ISOLATED_HOME / 'zaytoon.db'}"


def pytest_unconfigure(*_args):
    shutil.rmtree(_ISOLATED_HOME, ignore_errors=True)


@pytest.fixture(autouse=True)
def _openrouter_provider(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER", PROVIDER_OPENROUTER)
    yield


@pytest.fixture(autouse=True)
def _isolated_core_db(monkeypatch, tmp_path):
    """Point the core database at a fresh temp file for every test.

    The LLM client consults the provider registry (``core.db``) before it
    falls back to the configured chain, so a registry populated on a
    developer's machine would steer — and a stale row could break — tests
    that are about the chain itself. Isolation here is what keeps
    "which model answered?" a question the test controls.

    Deeper fixtures override this: ``tests/core``'s ``temp_core_db`` sets the
    same variable later (conftest depth orders parent before child), so the
    core suite keeps its own explicit database.
    """
    monkeypatch.setenv("CORE_DB_PATH", str(tmp_path / "core.db"))


#: Every `MemoryAgent` built during a test run, weakly. Populated by the
#: `__init__` wrapper below rather than by scanning `gc.get_objects()` per test —
#: that version worked, and also nearly doubled the suite (92 s → 174 s), which is
#: how a hygiene fixture becomes the slowest thing in the build.
import weakref  # noqa: E402

_LIVE_AGENTS: "weakref.WeakSet" = weakref.WeakSet()


def _track_memory_agents() -> None:
    """Record agents as they are constructed, without touching production code.

    Only `__init__` is wrapped: the class is created at import time by
    ``api.main`` and by some tests directly, and every one of them owns an
    aiosqlite connection plus its worker thread.
    """
    from agents.memory_agent import MemoryAgent

    if getattr(MemoryAgent.__init__, "_zaytoon_tracked", False):
        return
    original = MemoryAgent.__init__

    def __init__(self, *args, **kwargs):  # noqa: ANN001, N805
        original(self, *args, **kwargs)
        _LIVE_AGENTS.add(self)

    __init__._zaytoon_tracked = True  # noqa: SLF001
    MemoryAgent.__init__ = __init__


_track_memory_agents()


@pytest.fixture(autouse=True)
async def _close_import_time_memory_agent():
    """Close every live `MemoryAgent` before its event loop dies.

    ``api.main`` constructs one at module scope, so an aiosqlite connection and
    its worker thread outlive whichever test happened to use it. When that thread
    finishes a statement after the owning loop is closed, aiosqlite reports
    ``RuntimeError: Event loop is closed`` *inside the worker thread* — attributed
    to whatever test the collector happened to run in, and immune to any fix made
    in the code that actually leaked. The app's own lifespan closes this agent
    too; this fixture keeps the *suite's* loops from tripping over it.

    Safe to run unconditionally: `_get_db()` reconnects on demand, so closing
    here can never break a later test.
    """
    yield
    for agent in list(_LIVE_AGENTS):
        if getattr(agent, "_db", None) is not None:
            await agent.close()
