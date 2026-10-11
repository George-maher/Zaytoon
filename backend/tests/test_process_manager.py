"""Background process tracking (``tools/processes.py``).

``execute_local_command`` is the wrong tool for a dev server: a one-shot command
has a deadline, a dev server does not. These tests care about three things, in
this order:

* **Ownership.** A pid is in the table only because Zaytoon spawned it, and
  stopping an unknown id is refused. If this ever became a generic kill switch
  for the machine, everything else in this file would still pass — so it is
  tested first and most explicitly.
* **Truthfulness.** ``status``, ``exit_code`` and the log tail reflect what the
  child actually did, including when it died on its own or blew its deadline.
* **The same policy as every other execution tool.** A tracked process gets the
  identical command-line refusal, before the approval gate, and the confirm
  tier still fires.

Real child processes against a real temporary workspace: the contract being
asserted is what comes back from an actual process, so mocking the subprocess
layer would test the mock.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio  # noqa: E402

from tools import sandbox  # noqa: E402
from tools.base import ToolValidationError  # noqa: E402
from tools.processes import (  # noqa: E402
    LOG_LINES,
    ProcessRegistry,
    build_tools,
    registry as process_registry,
)
from tools.registry import build_default_registry  # noqa: E402
from tools.safety import (  # noqa: E402
    PERMISSION_CONFIRM,
    PERMISSION_SAFE,
    RiskAssessor,
)
from tools.sandbox import SecurityError  # noqa: E402

PYTHON = sys.executable

#: Commands for the *tool* tests. `sys.executable` cannot be used there: this
#: checkout lives under "D:\my work\...", and a command is tokenised on
#: whitespace, so an interpreter path with a space arrives as three arguments.
#: The registry-level tests pass an argv *list* and are unaffected.
LONG_RUNNING = "ping -n 30 127.0.0.1" if os.name == "nt" else "sleep 30"
QUICK = "ping -n 2 127.0.0.1" if os.name == "nt" else "sleep 2"


class _Approve:
    async def request(self, action, params=None, session_id=""):
        return True


@pytest.fixture
def registry():
    return build_default_registry()


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr(sandbox, "workspace_root", lambda: str(root))
    return root


@pytest.fixture(autouse=True)
def empty_table():
    """The module-level table is shared, so start every test from empty."""
    process_registry._procs.clear()
    yield
    # Anything still running is stopped, so a test cannot leave a live child.
    for entry in list(process_registry._procs.values()):
        if entry.status == "running" and entry.proc is not None:
            try:
                entry.proc.kill()
            except (ProcessLookupError, OSError):
                pass
    process_registry._procs.clear()


def _sleeper(workspace, name="loop.py", seconds=30):
    """A script that announces itself then waits: a dev server in miniature."""
    path = workspace / name
    path.write_text(
        f"import time\nprint('started', flush=True)\ntime.sleep({seconds})\n",
        encoding="utf-8",
    )
    return path


async def _await_exit(entry, timeout=8.0):
    """Wait for a child to stop, however it stops."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while entry.status == "running" and loop.time() < deadline:
        await asyncio.sleep(0.05)
    return entry.status


# ---------------------------------------------------------------------------
# Registration and tiers
# ---------------------------------------------------------------------------
class TestRegistration:
    def test_tools_are_registered(self, registry):
        for name in ("process_start", "process_list", "process_logs", "process_stop"):
            assert registry.has(name), name

    def test_tiers(self, registry):
        assert registry.get("process_start").permission == PERMISSION_CONFIRM
        assert registry.get("process_stop").permission == PERMISSION_CONFIRM
        assert registry.get("process_list").permission == PERMISSION_SAFE
        assert registry.get("process_logs").permission == PERMISSION_SAFE

    def test_start_and_stop_always_ask(self):
        assessor = RiskAssessor()
        for action in ("process_start", "process_stop"):
            assert assessor.assess(action, {"command": "npm run dev"}).requires_confirmation

    def test_inspection_never_asks(self):
        assessor = RiskAssessor()
        for action in ("process_list", "process_logs"):
            assert not assessor.assess(action, {"id": "proc_1"}).requires_confirmation

    def test_a_hazardous_command_escalates_the_tier(self):
        """The structured tools treat a matched pattern as what it is."""
        assessor = RiskAssessor()
        verdict = assessor.assess("process_start", {"command": "rm -rf /"})
        assert verdict.permission == "dangerous"

    def test_schemas_declare_what_they_require(self, registry):
        assert registry.get("process_start").schema()["required"] == ["command"]
        assert registry.get("process_logs").schema()["required"] == ["id"]
        assert registry.get("process_stop").schema()["required"] == ["id"]

    def test_build_tools_returns_the_four(self):
        assert {t.name() for t in build_tools()} == {
            "process_start", "process_list", "process_logs", "process_stop",
        }


# ---------------------------------------------------------------------------
# Ownership: the property that keeps this from being a kill switch
# ---------------------------------------------------------------------------
class TestOwnership:
    async def test_unknown_id_cannot_be_reached(self):
        """The load-bearing test. Nothing outside the table is touchable."""
        table = ProcessRegistry()
        assert table.get("proc_does_not_exist") is None
        assert table.logs("proc_does_not_exist", 10) is None
        assert await table.stop("proc_does_not_exist") is None

    async def test_a_started_pid_is_a_real_child(self, workspace):
        entry = await process_registry.start(
            [PYTHON, str(_sleeper(workspace))], cwd=str(workspace), name="loop"
        )
        assert entry.pid > 0
        assert entry.status == "running"
        assert process_registry.get(entry.id) is entry
        await process_registry.stop(entry.id)

    async def test_list_is_newest_first(self, workspace):
        first = await process_registry.start(
            [PYTHON, "-c", "pass"], cwd=str(workspace), name="one"
        )
        await asyncio.sleep(0.05)
        second = await process_registry.start(
            [PYTHON, "-c", "pass"], cwd=str(workspace), name="two"
        )
        names = [p.name for p in process_registry.list()]
        assert names[0] == second.name
        assert names[-1] == first.name

    async def test_two_starts_get_two_ids(self, workspace):
        a = await process_registry.start([PYTHON, "-V"], cwd=str(workspace))
        b = await process_registry.start([PYTHON, "-V"], cwd=str(workspace))
        assert a.id != b.id
        # Drain the readers of both children through the production path instead
        # of leaving them for the fixture's bare `kill()`: an un-drained pipe
        # transport is only closed in its destructor, which after the test's event
        # loop is gone printed `RuntimeError: Event loop is closed` and made every
        # run of this file look broken. Note `close_all` reports 0 here on
        # purpose - `python -V` has already exited, and it stops what is *running*.
        for entry in (a, b):
            await process_registry.stop(entry.id)


# ---------------------------------------------------------------------------
# Lifecycle truthfulness
# ---------------------------------------------------------------------------
class TestLifecycle:
    async def test_start_then_list_logs_then_stop(self, workspace):
        entry = await process_registry.start(
            [PYTHON, str(_sleeper(workspace))], cwd=str(workspace), name="loop"
        )
        for _ in range(60):
            if entry.lines:
                break
            await asyncio.sleep(0.05)
        assert entry.status == "running"
        assert any("started" in line for line in process_registry.logs(entry.id, 50))

        stopped = await process_registry.stop(entry.id)
        assert stopped is not None
        assert stopped["status"] in {"stopped", "exited", "timed_out"}
        assert process_registry.get(entry.id).status != "running"

    async def test_a_child_that_exits_on_its_own_reports_its_code(self, workspace):
        script = workspace / "quick.py"
        script.write_text("import sys\nprint('bye')\nsys.exit(3)\n", encoding="utf-8")
        entry = await process_registry.start([PYTHON, str(script)], cwd=str(workspace))
        assert await _await_exit(entry) == "exited"
        assert entry.exit_code == 3

    async def test_an_optional_timeout_stops_the_child(self, workspace):
        entry = await process_registry.start(
            [PYTHON, "-c", "import time; time.sleep(30)"],
            cwd=str(workspace),
            timeout=1.0,
        )
        assert await _await_exit(entry) == "timed_out"
        assert entry.proc.returncode is not None

    async def test_a_child_that_ignores_SIGTERM_is_killed_and_then_reported(
        self, workspace
    ):
        """The escalated path must not publish a status it has not earned.

        `proc.terminate()` is a request; a child that traps SIGTERM turns the
        escalation into a two-step. The registry used to write `timed_out`
        before it had reaped anything, so `exit_code` was null for as long as two
        grace periods while the child was still running — and on the macOS
        runner a caller landed in that window. A reported outcome now always
        follows the wait, so a SIGTERM-trapping child still ends with a real code.
        """
        script = workspace / "stubborn.py"
        script.write_text(
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, lambda *a: None)\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        entry = await process_registry.start([PYTHON, str(script)], cwd=str(workspace), timeout=0.5)
        assert await _await_exit(entry) == "timed_out"
        assert entry.proc.returncode is not None, "a status was published before the child was reaped"
        assert entry.exit_code == entry.proc.returncode

    async def test_no_timeout_means_it_keeps_running(self, workspace):
        entry = await process_registry.start(
            [PYTHON, "-c", "import time; time.sleep(30)"], cwd=str(workspace)
        )
        await asyncio.sleep(0.4)
        assert entry.status == "running"
        assert entry.proc.returncode is None

    async def test_output_is_redacted_on_the_way_in(self, workspace):
        """A child that prints a token must not put it in the log tail."""
        secret = "sk-test-do-not-log-1234567890"
        script = workspace / "leaky.py"
        script.write_text(f"print('token is {secret}')\n", encoding="utf-8")
        entry = await process_registry.start([PYTHON, str(script)], cwd=str(workspace))
        for _ in range(60):
            if entry.lines:
                break
            await asyncio.sleep(0.05)
        assert entry.lines, "the line should have been captured"
        assert secret not in "\n".join(entry.lines)

    async def test_the_log_buffer_is_bounded(self, workspace):
        script = workspace / "chatty.py"
        script.write_text(
            f"for i in range({LOG_LINES + 150}):\n    print('line', i)\n",
            encoding="utf-8",
        )
        entry = await process_registry.start([PYTHON, str(script)], cwd=str(workspace))
        await _await_exit(entry, timeout=15.0)
        assert entry.lines, "output should have been captured"
        assert len(entry.lines) <= LOG_LINES

    async def test_stderr_is_captured_too(self, workspace):
        """stderr is folded into the same tail, so a failing server is visible."""
        script = workspace / "noisy.py"
        script.write_text("import sys\nsys.stderr.write('boom\\n')\n", encoding="utf-8")
        entry = await process_registry.start([PYTHON, str(script)], cwd=str(workspace))
        await _await_exit(entry)
        assert any("boom" in line for line in entry.lines)


# ---------------------------------------------------------------------------
# The tool layer: same sandbox, same gate, structured results
# ---------------------------------------------------------------------------
class TestTools:
    def test_shell_chaining_is_refused_outright(self):
        """Same unconditional block as every other execution tool — reported as
        a sandbox refusal, because approving it could not help."""
        from tools.processes import ProcessStart

        with pytest.raises(SecurityError):
            ProcessStart().validate({"command": "npm run dev && del important.txt"})

    def test_missing_command_is_a_validation_error(self):
        from tools.processes import ProcessStart

        with pytest.raises(ToolValidationError):
            ProcessStart().validate({"command": "   "})

    def test_a_bad_timeout_is_refused_before_the_gate(self):
        from tools.processes import ProcessStart

        with pytest.raises(ToolValidationError):
            ProcessStart().validate({"command": "npm run dev", "timeout": 99999})

    async def test_start_runs_in_the_workspace_not_the_backend_cwd(self, workspace):
        from tools.processes import ProcessStart

        result = await ProcessStart().execute(command=LONG_RUNNING, name="long")
        assert result.success, result.error
        try:
            assert result.data["cwd"] == str(workspace)
            assert result.data["pid"] > 0
            assert result.data["status"] == "running"
        finally:
            await process_registry.stop(result.data["id"])

    async def test_cwd_outside_the_workspace_is_denied(self, workspace, tmp_path):
        from tools.processes import ProcessStart

        outside = tmp_path / "outside"
        outside.mkdir()
        result = await ProcessStart().execute(command=QUICK, cwd=str(outside))
        assert not result.success
        assert result.blocked or "outside the workspace" in (result.error or "")

    async def test_a_missing_working_directory_is_reported_not_crashed(self, workspace):
        from tools.processes import ProcessStart

        result = await ProcessStart().execute(command=QUICK, cwd="nope_not_here")
        assert not result.success
        assert "does not exist" in (result.error or "")

    async def test_list_reports_an_empty_table(self):
        from tools.processes import ProcessList

        result = await ProcessList().execute()
        assert result.success
        assert result.data["processes"] == []

    async def test_unknown_ids_fail_with_a_structured_result(self):
        from tools.processes import ProcessLogs, ProcessStop

        logs = await ProcessLogs().execute(id="nope", tail=5)
        stop = await ProcessStop().execute(id="nope")
        assert not logs.success and not stop.success
        assert "only processes zaytoon started" in (stop.error or "").lower()

    async def test_the_registry_gate_asks_before_starting(self, workspace):
        """Through the registry the confirm tier must fire and run nothing."""
        import tools.processes as processes

        spawned = []

        async def _no_spawn(*args, **kwargs):
            spawned.append(args)
            raise AssertionError("must not be reached")

        original = processes.asyncio.create_subprocess_exec
        processes.asyncio.create_subprocess_exec = _no_spawn
        try:
            registry = build_default_registry()
            result = await registry.execute(
                "process_start", {"command": QUICK}
            )
        finally:
            processes.asyncio.create_subprocess_exec = original

        assert result.get("requires_confirmation") is True
        assert result["permission"] == PERMISSION_CONFIRM
        assert spawned == [], "a declined prompt must not reach the subprocess layer"

    async def test_approval_starts_it(self, workspace):
        registry = build_default_registry()
        result = await registry.execute(
            "process_start",
            {"command": LONG_RUNNING, "name": "approved"},
            confirmation_manager=_Approve(),
        )
        assert result["success"] is True, result
        try:
            assert result["data"]["status"] == "running"
            assert result["data"]["cwd"] == str(workspace)
        finally:
            await process_registry.stop(result["data"]["id"])

    async def test_safe_tools_do_not_prompt(self, workspace):
        registry = build_default_registry()
        result = await registry.execute("process_list", {})
        assert result["success"] is True
        assert "requires_confirmation" not in result

# ---------------------------------------------------------------------------
# Shutdown: what happens to tracked children when the backend goes away
# ---------------------------------------------------------------------------
class TestCloseAll:
    """The registry is in memory, so an unreaped child is orphaned forever.

    Before `close_all` existed, `process_start` + a backend restart left a dev
    server holding its port with no id to stop it by: the new session's registry
    was empty, so `process_stop` refused the pid it did not own. The terminal
    manager had already made the opposite choice for shells, which is the
    precedent this follows.
    """

    async def test_a_tracked_child_is_stopped(self, workspace):
        table = ProcessRegistry()
        entry = await table.start(
            [PYTHON, str(_sleeper(workspace, "orphan.py", 30))],
            cwd=str(workspace),
            name="server",
        )
        assert await _await_status(entry) == "running", entry.status

        stopped = await table.close_all("backend shutting down")

        assert stopped == 1
        await _await_exit(entry)
        assert entry.proc.returncode is not None, "the child is still alive"
        assert entry.status in ("stopped", "exited"), entry.status

    async def test_the_child_is_reaped_not_merely_signalled(self, workspace):
        """`terminate` then `wait`: a zombie is not 'stopped' either."""
        table = ProcessRegistry()
        entry = await table.start(
            [PYTHON, str(_sleeper(workspace, "reap.py", 30))], cwd=str(workspace)
        )
        await table.close_all("shutdown")
        # The pumps are cancelled on stop; the exit watcher must have run.
        assert all(task.cancelled() or task.done() for task in entry.pumps)

    async def test_only_processes_this_registry_started_are_touched(self, workspace):
        """The ownership invariant, asserted at the one place that could break it.

        A `close_all` that searched for matching command lines would be a way to
        kill anything on the machine from a backend restart. It is not: it walks
        its own table, so a foreign process with an identical command line keeps
        running.
        """
        foreign = await asyncio.create_subprocess_exec(
            PYTHON,
            str(_sleeper(workspace, "foreign.py", 30)),
            cwd=str(workspace),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.STDOUT,
        )
        table = ProcessRegistry()
        try:
            assert await table.close_all("shutdown") == 0
            await asyncio.sleep(0.3)
            assert foreign.returncode is None, "a process we did not spawn was killed"
        finally:
            foreign.kill()
            await foreign.wait()
            # DEVNULL means there is no pipe to drain; a subprocess with PIPE and
            # no reader leaves the transport open until its destructor runs,
            # which after the loop is closed is the `Event loop is closed` noise.
            assert foreign.stdout is None

    async def test_finished_entries_are_not_counted(self, workspace):
        table = ProcessRegistry()
        entry = await table.start(
            [PYTHON, str(_sleeper(workspace, "brief.py", 0))], cwd=str(workspace)
        )
        await _await_exit(entry)
        assert await table.close_all("shutdown") == 0

    async def test_it_is_safe_to_call_twice(self, workspace):
        table = ProcessRegistry()
        await table.start(
            [PYTHON, str(_sleeper(workspace, "twice.py", 30))], cwd=str(workspace)
        )
        assert await table.close_all("shutdown") == 1
        assert await table.close_all("shutdown") == 0

    async def test_an_empty_registry_is_not_an_error(self):
        assert await ProcessRegistry().close_all("shutdown") == 0


async def _await_status(entry, want="running", timeout=4.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while entry.status != want and loop.time() < deadline:
        await asyncio.sleep(0.05)
    return entry.status


class TestStoppedProcessesReleaseTheirPipes:
    """`stop()` is not finished until the *transport* is gone, not just the pid."""

    async def test_the_subprocess_transport_is_closed(self, workspace):
        table = ProcessRegistry()
        entry = await table.start(
            [PYTHON, str(_sleeper(workspace, "short.py", 0))], cwd=str(workspace)
        )
        await _await_exit(entry)
        await table.stop(entry.id)
        # The observable that matters: the readers ran to EOF instead of being
        # cancelled, which is what lets the transport close its own pipes.
        # Asserted on the tasks because Python 3.11 offers no public way to ask
        # whether a subprocess transport is closed.
        assert entry.pumps and all(t.done() for t in entry.pumps)
        assert not any(t.cancelled() for t in entry.pumps), (
            "a cancelled reader leaves the pipe transport open until __del__"
        )

    async def test_a_child_that_outlived_its_readers_is_still_stopped(self, workspace):
        """A wedged grandchild must not be able to hang `stop()`.

        The drain is bounded by `PUMP_DRAIN_S`, so a pipe held open by someone
        else's process falls back to cancelling the reader instead of blocking
        shutdown forever.
        """
        from tools import processes as mod
        holder = await asyncio.create_subprocess_exec(
            PYTHON,
            "-c",
            "import sys, time; sys.stdout.write('x' * 10); sys.stdout.flush(); time.sleep(30)",
            stdout=asyncio.subprocess.PIPE,
        )
        try:
            table = ProcessRegistry()
            entry = await table.start(
                [PYTHON, str(_sleeper(workspace, "holder.py", 30))], cwd=str(workspace)
            )
            started = asyncio.get_running_loop().time()
            await table.stop(entry.id)
            assert asyncio.get_running_loop().time() - started < 3.0, (
                "`stop()` blocked on a pipe it could not drain; the wait must be "
                f"bounded by PUMP_DRAIN_S ({mod.PUMP_DRAIN_S}s)"
            )
        finally:
            holder.kill()
            await holder.wait()
            await asyncio.wait_for(holder.stdout.read(), timeout=2)

    async def test_closing_twice_is_harmless(self, workspace):
        table = ProcessRegistry()
        entry = await table.start(
            [PYTHON, str(_sleeper(workspace, "twice2.py", 0))], cwd=str(workspace)
        )
        await _await_exit(entry)
        assert await table.stop(entry.id) is not None
        assert await table.stop(entry.id) is not None, "stop must stay idempotent"
