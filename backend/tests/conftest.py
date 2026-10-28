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
import sys
import tempfile
from pathlib import Path

import pytest

from config import PROVIDER_OPENROUTER, config

#: Removed wholesale in `pytest_unconfigure`; a run leaves no state behind.
_ISOLATED_HOME = Path(tempfile.mkdtemp(prefix="zaytoon-tests-"))
os.environ["ZAYTOON_DATA_DIR"] = str(_ISOLATED_HOME)
os.environ["DATABASE_URL"] = f"sqlite:///{_ISOLATED_HOME / 'zaytoon.db'}"


def write_fake_executable(target: Path, body: str) -> Path:
    """Write ``body`` as an executable stand-in at ``target``; return its path.

    ``body`` is a ``str.format`` template (``{{``/``}}`` for literal braces, an
    optional ``{python}`` placeholder for the interpreter). POSIX gets the
    classic extensionless shebang file. **Windows cannot execute a shebang
    script at all**: ``CreateProcess`` raises ``WinError 193`` ("%1 is not a
    valid Win32 application"), which is why every guest-ssh and vmrun fake
    failed on the development platform while looking perfectly correct. There
    the same body goes to ``<name>_runner.py`` and a ``<name>.cmd`` wrapper
    runs it through the interpreter. Both paths were verified on this
    machine: the shebang form fails with exactly that error, the wrapper
    runs, and argv arrives intact — spaces included, which is what the
    argv-recording fakes assert on.
    """
    rendered = body.format(python=sys.executable)
    if os.name != "nt":
        target.write_text(f"#!{sys.executable}\n{rendered}", encoding="utf-8")
        target.chmod(0o755)
        return target
    runner = target.with_name(f"{target.name}_runner.py")
    runner.write_text(rendered, encoding="utf-8")
    wrapper = target.with_name(f"{target.name}.cmd")
    wrapper.write_text(
        f'@echo off\r\n"{sys.executable}" "{runner}" %*\r\n',
        encoding="utf-8",
    )
    return wrapper


@pytest.fixture
def fake_exe():
    """Factory fixture: ``fake_exe(path, template) -> Path``.

    Exposed as a fixture so both suites that need a fake executable (the
    guest-ssh fakes in ``tests/`` and the vmrun fakes in ``tests/core/``) use
    the same platform logic instead of growing two shebang templates that
    disagree about what Windows can run.
    """
    return write_fake_executable


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

# `tests/` is not reliably on `sys.path` at conftest-import time (it depends on
# which special root pytest derived), so insert this directory explicitly —
# already-present-first, so an already-inserted path stays in front.
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

_HERE = str(_Path(__file__).resolve().parent)
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)
from _sqlite_sweeper import release_stray_threads  # noqa: E402


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


def pytest_sessionfinish(session, exitstatus):
    """Last resort for connections a test left referenced ``after`` teardown.

    The autouse fixture above covers the common case; this one catches whatever
    pytest itself retains past fixture teardown (traceback frames hold the
    objects they refer to). Without it the interpreter waits for the leaked
    worker in ``threading._shutdown`` after the summary has printed.
    """
    release_stray_threads()
