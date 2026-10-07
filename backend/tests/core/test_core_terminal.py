"""Streaming terminal sessions: ownership, sandboxing, output.

Commands are built by writing a script into the workspace and running it, not
by typing one out. That is not a workaround: ``split_command`` refuses quotes,
spaces and metacharacters, so ``python -c "print('x')"`` is *correctly* rejected.
Every test that needs quotes has to write a file, which is what a user would do
too.
"""

import asyncio
import os

import pytest

from core.workspace.terminal import (
    MAX_ENV_ENTRIES,
    MAX_INPUT_CHARS,
    MAX_LINES,
    MAX_SESSIONS,
    TerminalError,
    TerminalManager,
)


@pytest.fixture
def manager():
    return TerminalManager()


def script(workspace, name, body):
    """Write a script into the workspace and return its relative path."""
    path = os.path.join(workspace, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)
    return name


async def wait_for(session, timeout=5.0):
    """Wait for a session to exit, with a bound."""
    for _ in range(int(timeout / 0.05)):
        if session.status != "running":
            return session
        await asyncio.sleep(0.05)
    raise AssertionError(f"session {session.id} was still running after {timeout}s")


class TestOpen:
    async def test_opens_and_completes(self, manager, temp_workspace):
        session = await manager.open("python -c pass", name="noop")
        await wait_for(session)
        assert session.status == "exited"
        assert session.exit_code == 0

    async def test_reports_the_pid_and_command(self, manager, temp_workspace):
        session = await manager.open("python -c pass", name="noop")
        await wait_for(session)
        assert session.pid
        assert session.name == "noop"
        assert session.to_dict()["command"] == "python -c pass"

    async def test_an_exit_code_is_recorded(self, manager, temp_workspace):
        name = script(temp_workspace, "exit3.py", "import sys\nsys.exit(3)\n")
        session = await manager.open(f"python {name}")
        await wait_for(session)
        assert session.exit_code == 3
        assert session.status == "exited"

    async def test_stdout_is_captured(self, manager, temp_workspace):
        name = script(temp_workspace, "out.py", "print('hello from the terminal')\n")
        session = await manager.open(f"python {name}")
        await wait_for(session)
        assert any("hello from the terminal" in line["text"] for line in session.tail())

    async def test_stderr_is_captured(self, manager, temp_workspace):
        name = script(temp_workspace, "err.py", "import sys\nsys.stderr.write('a warning')\n")
        session = await manager.open(f"python {name}")
        await wait_for(session)
        assert any("a warning" in line["text"] for line in session.tail())

    async def test_an_unknown_command_is_reported(self, manager, temp_workspace):
        with pytest.raises(TerminalError, match="Command not found"):
            await manager.open("definitely-not-a-real-binary-xyz --version")

    async def test_a_missing_working_directory_is_reported(self, manager, temp_workspace):
        with pytest.raises(TerminalError, match="does not exist"):
            await manager.open("python -c pass", cwd="nope")

    async def test_sessions_run_in_the_workspace(self, manager, temp_workspace):
        """Never the process cwd: the backend runs from backend/, so inheriting
        it would put the terminal in the assistant's own source tree."""
        session = await manager.open("python -c pass")
        await wait_for(session)
        assert session.cwd == os.path.realpath(temp_workspace)


class TestSandbox:
    async def test_command_chaining_is_refused(self, manager, temp_workspace):
        """Same rule as every other command path; approving would not help."""
        with pytest.raises(TerminalError):
            await manager.open("python -c pass & hostname")

    async def test_a_pipe_is_refused(self, manager, temp_workspace):
        with pytest.raises(TerminalError):
            await manager.open("python -c pass | more")

    async def test_a_sandbox_refusal_is_marked_blocked(self, manager, temp_workspace):
        """`blocked` is what makes the API answer 403 rather than "try again"."""
        with pytest.raises(TerminalError) as caught:
            await manager.open("python -c pass & hostname")
        assert caught.value.blocked is True

    async def test_a_missing_binary_is_not_blocked(self, manager, temp_workspace):
        """Not installed is a plain error the user can act on, not a refusal."""
        with pytest.raises(TerminalError) as caught:
            await manager.open("definitely-not-a-real-binary-xyz")
        assert caught.value.blocked is False

    async def test_a_redirect_is_refused(self, manager, temp_workspace):
        with pytest.raises(TerminalError):
            await manager.open("python -c pass > out.txt")

    async def test_quotes_are_refused(self, manager, temp_workspace):
        """No shell quoting, so there is no quoting to get wrong."""
        with pytest.raises(TerminalError):
            await manager.open('python -c "print(1)"')

    async def test_percent_expansion_is_refused(self, manager, temp_workspace):
        with pytest.raises(TerminalError):
            await manager.open("echo %PATH%")

    async def test_an_empty_command_is_refused(self, manager, temp_workspace):
        with pytest.raises(TerminalError):
            await manager.open("   ")

    async def test_a_cwd_outside_the_workspace_is_refused(self, manager, temp_workspace):
        with pytest.raises(TerminalError, match="outside the workspace"):
            await manager.open("python -c pass", cwd=os.path.join(temp_workspace, "..", ".."))


class TestOutput:
    async def test_the_sink_receives_each_line(self, manager, temp_workspace):
        name = script(temp_workspace, "many.py", "for i in range(5):\n    print(i)\n")
        seen = []

        async def sink(session, entry):
            seen.append(entry["text"])

        manager.set_sink(sink)
        session = await manager.open(f"python {name}")
        await wait_for(session)

        assert len([line for line in seen if line.strip()]) >= 5

    async def test_output_is_redacted(self, manager, temp_workspace):
        """A dev server printing its environment must not turn a terminal into
        a credential leak."""
        name = script(temp_workspace, "leak.py", "print('api_key=sk-leakedsecretvalue12345')\n")
        seen = []

        async def sink(session, entry):
            seen.append(entry["text"])

        manager.set_sink(sink)
        session = await manager.open(f"python {name}")
        await wait_for(session)

        assert seen, "the line was produced"
        assert not any("sk-leakedsecretvalue12345" in line for line in seen)

    async def test_two_sinks_both_receive(self, manager, temp_workspace):
        """Two clients watching two terminals must both get their lines; a
        single-slot sink makes the second replace the first."""
        name = script(temp_workspace, "out.py", "print('shared')\n")
        first, second = [], []

        manager.add_sink(lambda s, e: first.append(e["text"]))
        manager.add_sink(lambda s, e: second.append(e["text"]))

        session = await manager.open(f"python {name}")
        await wait_for(session)

        assert first and second

    async def test_a_removed_sink_stops_receiving(self, manager, temp_workspace):
        name = script(temp_workspace, "out.py", "print('x')\n")
        seen = []
        remove = manager.add_sink(lambda s, e: seen.append(e["text"]))
        remove()
        remove()  # idempotent

        session = await manager.open(f"python {name}")
        await wait_for(session)
        assert seen == []

    async def test_a_raising_sink_does_not_kill_the_pump(self, manager, temp_workspace):
        name = script(temp_workspace, "out.py", "print('survives')\n")

        async def bad(session, entry):
            raise RuntimeError("the socket died")

        manager.set_sink(bad)
        session = await manager.open(f"python {name}")
        await wait_for(session)

        assert session.status == "exited"
        assert session.tail(), "output is still recorded even when the sink fails"

    async def test_the_ring_buffer_is_bounded(self, manager, temp_workspace):
        name = script(temp_workspace, "flood.py",
                      f"for i in range({MAX_LINES + 200}):\n    print(i)\n")
        session = await manager.open(f"python {name}")
        await wait_for(session)
        assert len(session.lines) <= MAX_LINES


class TestInput:
    async def test_input_reaches_the_process(self, manager, temp_workspace):
        name = script(
            temp_workspace, "echo.py",
            "import sys\nprint('got:', sys.stdin.readline().strip())\n",
        )
        session = await manager.open(f"python {name}")
        for _ in range(20):
            if session.lines:
                break
            await asyncio.sleep(0.05)

        await manager.write(session.id, "hello\n")
        await wait_for(session)

        assert any("got: hello" in line["text"] for line in session.tail())

    async def test_input_to_an_unknown_session(self, manager, temp_workspace):
        with pytest.raises(TerminalError, match="No such terminal"):
            await manager.write("term_nonexistent", "x")

    async def test_input_to_a_finished_session(self, manager, temp_workspace):
        session = await manager.open("python -c pass")
        await wait_for(session)
        with pytest.raises(TerminalError, match="already exited"):
            await manager.write(session.id, "x")

    async def test_empty_input_is_refused(self, manager, temp_workspace):
        session = await manager.open("python -c pass")
        with pytest.raises(TerminalError, match="Nothing to send"):
            await manager.write(session.id, "")
        await wait_for(session)

    async def test_oversized_input_is_truncated_rather_than_refused(self, manager, temp_workspace):
        """The payload is capped, so an unbounded body cannot reach the pipe.

        The script reads one *line* rather than to EOF: nothing ever closes the
        pipe, so a read-to-EOF script would wait forever and the test would be
        asserting on the wrong thing.
        """
        name = script(temp_workspace, "reader.py", "import sys\nsys.stdin.readline()\n")
        session = await manager.open(f"python {name}")
        result = await manager.write(session.id, "x\n" + "x" * (MAX_INPUT_CHARS + 500))
        assert result["sent"] == MAX_INPUT_CHARS
        await wait_for(session)


class TestClose:
    async def test_close_stops_a_running_process(self, manager, temp_workspace):
        name = script(temp_workspace, "sleep.py", "import time\ntime.sleep(60)\n")
        session = await manager.open(f"python {name}")
        result = await manager.close(session.id)
        assert result is not None
        assert result["status"] == "stopped"

    async def test_closing_an_unknown_session_is_none(self, manager, temp_workspace):
        """Only sessions this manager started exist at all — which is what stops
        it being a remote kill switch."""
        assert await manager.close("term_nonexistent") is None

    async def test_a_closed_session_is_forgotten(self, manager, temp_workspace):
        name = script(temp_workspace, "sleep.py", "import time\ntime.sleep(60)\n")
        session = await manager.open(f"python {name}")
        await manager.close(session.id)
        assert manager.get(session.id) is None

    async def test_close_all(self, manager, temp_workspace):
        name = script(temp_workspace, "sleep.py", "import time\ntime.sleep(60)\n")
        await manager.open(f"python {name}")
        await manager.open(f"python {name}")
        assert await manager.close_all("test") == 2
        assert manager.list() == []

    async def test_the_session_limit_is_enforced(self, manager, temp_workspace):
        name = script(temp_workspace, "sleep.py", "import time\ntime.sleep(60)\n")
        for _ in range(MAX_SESSIONS):
            await manager.open(f"python {name}")
        with pytest.raises(TerminalError, match="already open"):
            await manager.open("python -c pass")
        await manager.close_all("test")

    async def test_a_stopped_session_frees_a_slot(self, manager, temp_workspace):
        name = script(temp_workspace, "sleep.py", "import time\ntime.sleep(60)\n")
        sessions = [await manager.open(f"python {name}") for _ in range(MAX_SESSIONS)]
        await manager.close(sessions[0].id)
        extra = await manager.open("python -c pass")
        assert extra is not None
        await manager.close_all("test")


class TestInspection:
    async def test_list_is_newest_first(self, manager, temp_workspace):
        name = script(temp_workspace, "sleep.py", "import time\ntime.sleep(60)\n")
        first = await manager.open(f"python {name}")
        second = await manager.open(f"python {name}")
        assert manager.list()[0].id == second.id
        assert first.id in {s.id for s in manager.list()}
        await manager.close_all("test")

    async def test_order_is_stable_when_the_clock_ties(self, manager, temp_workspace):
        """Two sessions opened in the same tick must still order.

        `time.monotonic()` on Windows is coarse enough that back-to-back opens
        routinely report the same value. Without a tie-break the order falls to
        dict insertion, which looks correct until a session is closed and
        re-opened — and then the list reads as arbitrary.
        """
        name = script(temp_workspace, "sleep.py", "import time\ntime.sleep(60)\n")
        sessions = [await manager.open(f"python {name}") for _ in range(5)]
        assert [s.id for s in manager.list()] == [s.id for s in reversed(sessions)]

        # Close one and open another: the newest is still the newest.
        await manager.close(sessions[0].id)
        fresh = await manager.open(f"python {name}")
        assert manager.list()[0].id == fresh.id
        await manager.close_all("test")

    async def test_a_listing_omits_output_by_default(self, manager, temp_workspace):
        name = script(temp_workspace, "out.py", "print('x')\n")
        session = await manager.open(f"python {name}")
        await wait_for(session)
        assert "lines" not in manager.snapshot()[0]
        assert "lines" in session.to_dict()

    async def test_duration_is_reported(self, manager, temp_workspace):
        session = await manager.open("python -c pass")
        await wait_for(session)
        assert session.duration_ms() >= 0


class TestEnvOverlay:
    """`env` reaches the child as an overlay — and the values never leave it."""

    async def test_an_overlay_entry_is_visible_to_the_child(self, manager, temp_workspace):
        name = script(
            temp_workspace,
            "envcheck.py",
            "import os\n"
            "print('ZAY_TEST=' + os.environ.get('ZAY_TEST', 'MISSING'))\n"
            "print('HAS_PATH=' + str(bool(os.environ.get('PATH') or os.environ.get('Path'))))\n",
        )
        session = await manager.open(f"python {name}", env={"ZAY_TEST": "hello"})
        await wait_for(session)
        text = "\n".join(line["text"] for line in session.tail())
        assert "ZAY_TEST=hello" in text
        # Overlay, not replacement: PATH must survive or the child cannot
        # resolve anything on the next line.
        assert "HAS_PATH=True" in text

    async def test_key_names_are_reported_but_values_are_not(self, manager, temp_workspace):
        session = await manager.open("python -c pass", env={"ZAY_SECRET": "hunter2"})
        await wait_for(session)
        payload = session.to_dict()
        assert "ZAY_SECRET" in payload["env_keys"]
        assert "hunter2" not in str(payload), "env values must never reach the API"

    async def test_an_invalid_key_is_refused(self, manager, temp_workspace):
        with pytest.raises(TerminalError, match="Invalid environment variable name"):
            await manager.open("python -c pass", env={"NOT A KEY": "x"})

    async def test_too_many_entries_are_refused(self, manager, temp_workspace):
        env = {f"K{i}": "v" for i in range(MAX_ENV_ENTRIES + 1)}
        with pytest.raises(TerminalError, match="at most"):
            await manager.open("python -c pass", env=env)

    async def test_a_non_string_value_is_refused(self, manager, temp_workspace):
        with pytest.raises(TerminalError, match="short string"):
            await manager.open("python -c pass", env={"K": 123})

    async def test_a_non_dict_is_refused(self, manager, temp_workspace):
        with pytest.raises(TerminalError, match="object of name"):
            await manager.open("python -c pass", env=["K=V"])


class TestTimeout:
    async def test_a_session_stops_at_its_limit_with_a_reason(self, manager, temp_workspace):
        name = script(
            temp_workspace, "sleepy.py", "import time\nprint('sleeping', flush=True)\ntime.sleep(30)\n"
        )
        session = await manager.open(f"python {name}", timeout=0.4)
        for _ in range(100):
            if session.status != "running":
                break
            await asyncio.sleep(0.05)
        # The reason lands in the output *before* the stop: "the terminal
        # closed itself" is a mystery, "stopped after 0.4s" is an answer.
        assert session.status == "stopped"
        assert any("timeout" in line["text"] for line in session.tail())

    async def test_without_a_limit_the_session_runs_until_closed(self, manager, temp_workspace):
        name = script(temp_workspace, "sleeper.py", "import time\ntime.sleep(30)\n")
        session = await manager.open(f"python {name}")
        await asyncio.sleep(0.3)
        assert session.status == "running"
        assert session.timeout_s is None
        await manager.close(session.id)

    async def test_the_limit_is_stored_for_display(self, manager, temp_workspace):
        session = await manager.open("python -c pass", timeout=30)
        await wait_for(session)
        assert session.to_dict()["timeout_s"] == 30

    async def test_an_absurd_limit_is_refused(self, manager, temp_workspace):
        for bad in (0, -5, 9_999_999, "soon"):
            with pytest.raises(TerminalError, match="timeout"):
                await manager.open("python -c pass", timeout=bad)