"""Structured local execution (Increment D): ``execute_local_command`` + ``execute_code``.

These two tools exist to give the agent a result it can *branch on* — exit code,
both streams, duration, timeout — instead of a rendered paragraph. What they must
not do is become a second, looser way to reach a shell.

So the tests come in two halves, and the second half matters more:

* the structured contract itself (what the payload contains, what a timeout looks
  like, that partial output survives the kill);
* the policy half: the sandbox's unconditional blocks still apply, the gate still
  fires, the tiers are what the tables say, and ``system_command``'s classification
  is untouched by the escalation added for the new tool.

Everything runs against a real temporary workspace and real processes — no
mocking of the subprocess layer, because the point of the contract is what comes
back from an actual child.
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import sandbox  # noqa: E402
from tools.base import ToolValidationError  # noqa: E402
from tools.local_exec import (  # noqa: E402
    MAX_TIMEOUT_S,
    ExecOutcome,
    check_timeout,
    build_tools,
)
from tools.registry import build_default_registry  # noqa: E402
from tools.safety import (  # noqa: E402
    PERMISSION_CONFIRM,
    PERMISSION_DANGEROUS,
    PendingConfirmation,
    RiskAssessor,
)
from tools.sandbox import SecurityError  # noqa: E402

#: A command that produces no output but runs for several seconds, used to prove
#: the deadline is enforced. Chosen to contain no shell syntax at all, because
#: the whole point is that it reaches ``create_subprocess_exec`` untouched.
SLOW_COMMAND = "ping -n 6 127.0.0.1" if os.name == "nt" else "sleep 6"

#: A command that exits non-zero without needing quotes or metacharacters.
FAILING_COMMAND = "dir nope_zaytoon_not_here" if os.name == "nt" else "ls nope_zaytoon_not_here"

#: The tool's own name in listing output, portable across the two listings.
LISTING_COMMAND = "dir" if os.name == "nt" else "ls"


class _Approve:
    async def request(self, action, params=None, session_id=""):
        return True


@pytest.fixture
def registry():
    return build_default_registry()


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    """Point the containment layer at a temporary directory."""
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr(sandbox, "workspace_root", lambda: str(root))
    return root


# ---------------------------------------------------------------------------
# Registration and tiers
# ---------------------------------------------------------------------------
class TestRegistration:
    def test_both_tools_are_registered(self, registry):
        assert registry.has("execute_local_command")
        assert registry.has("execute_code")

    def test_tiers(self, registry):
        assert registry.get("execute_local_command").permission == PERMISSION_CONFIRM
        assert registry.get("execute_code").permission == PERMISSION_DANGEROUS

    def test_aliases_resolve_to_the_same_tool(self, registry):
        assert registry.get("run_local_command") is registry.get("execute_local_command")
        assert registry.get("exec_local") is registry.get("execute_local_command")
        assert registry.get("run_snippet") is registry.get("execute_code")

    def test_schemas_declare_what_they_require(self, registry):
        for name, required in (
            ("execute_local_command", ["command"]),
            ("execute_code", ["code", "language"]),
        ):
            schema = registry.get(name).schema()
            assert schema["required"] == required, name
            for field in required:
                assert field in schema["properties"], (name, field)

    def test_no_shell_switch_is_offered(self, registry):
        """A deliberate omission, pinned so it cannot appear unreviewed.

        ``tools.sandbox`` refuses separators, metacharacters and ``%VAR%``
        expansion before the gate, and every process is started from an argv.
        Under those constraints a shell flag adds no capability — only a second
        thing to get wrong.
        """
        schema = registry.get("execute_local_command").schema()
        assert "shell" not in schema["properties"]
        assert schema["additionalProperties"] is False

    def test_build_tools_returns_one_of_each(self):
        names = [tool.name() for tool in build_tools()]
        assert names == ["execute_local_command", "execute_code"]


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------
class TestGate:
    def test_command_tool_asks_before_running(self, registry):
        result = asyncio.run(
            registry.execute("execute_local_command", {"command": "echo hello"})
        )
        assert result.get("requires_confirmation") is True
        assert result["permission"] == PERMISSION_CONFIRM

    def test_code_tool_asks_before_running(self, registry):
        result = asyncio.run(
            registry.execute(
                "execute_code", {"code": "print(1)", "language": "python"}
            )
        )
        assert result.get("requires_confirmation") is True
        assert result["permission"] == PERMISSION_DANGEROUS

    def test_approval_runs_it(self, registry, workspace):
        result = asyncio.run(
            registry.execute(
                "execute_local_command",
                {"command": "echo hello"},
                confirmation_manager=_Approve(),
            )
        )
        assert result["success"] is True, result
        assert "hello" in result["output"]

    def test_denial_runs_nothing(self, registry, monkeypatch):
        """A declined prompt must not reach the subprocess layer."""
        spawned = []
        import tools.local_exec as local_exec

        async def _no_spawn(*args, **kwargs):
            spawned.append(args)
            raise AssertionError("must not be reached")

        monkeypatch.setattr(local_exec.asyncio, "create_subprocess_exec", _no_spawn)
        result = asyncio.run(
            registry.execute("execute_local_command", {"command": "echo hello"})
        )
        assert result.get("requires_confirmation") is True
        assert spawned == []

    def test_validation_still_precedes_the_gate(self, registry):
        result = asyncio.run(
            registry.execute("execute_local_command", {"command": ""})
        )
        assert result["success"] is False
        assert not result.get("requires_confirmation")
        assert "required" in result["error"]


# ---------------------------------------------------------------------------
# The structured contract
# ---------------------------------------------------------------------------
class TestStructuredResult:
    def test_success_carries_the_whole_envelope(self, registry, workspace):
        result = asyncio.run(
            registry.execute(
                "execute_local_command",
                {"command": "echo hello"},
                approval=True,
            )
        )
        assert result["success"] is True
        data = result["data"]
        for key in (
            "argv", "exit_code", "stdout", "stderr", "duration_ms",
            "timed_out", "truncated", "timeout_s", "cwd", "success",
        ):
            assert key in data, key
        assert data["exit_code"] == 0
        assert data["timed_out"] is False
        assert data["truncated"] is False
        assert data["success"] is True
        assert data["duration_ms"] >= 0
        assert "hello" in data["stdout"]
        assert data["argv"], "the argv actually run must be reported"
        # The verdict line is part of the human-readable rendering, so the model
        # can see the exit code and duration without parsing a JSON payload.
        assert result["output"].startswith("exit=0 in")

    def test_a_non_zero_exit_keeps_both_streams(self, registry, workspace):
        result = asyncio.run(
            registry.execute(
                "execute_local_command",
                {"command": FAILING_COMMAND},
                approval=True,
            )
        )
        assert result["success"] is False
        # The contract is "the exit code the process actually returned is echoed
        # into the error text", not "that code is 1": `dir nope…` exits 1 in
        # cmd.exe while GNU `ls` exits 2. Pinning the number tested the OS.
        code = result["data"]["exit_code"]
        assert code != 0
        assert f"exit={code} in" in result["error"]
        # The model only sees `error` on failure, so the diagnostic has to live
        # there — a failing test whose output stayed in `output` would be
        # invisible to the agent.
        assert result["error"] == result["output"]

    def test_timeout_is_reported_and_output_is_kept(self, registry, workspace):
        result = asyncio.run(
            registry.execute(
                "execute_local_command",
                {"command": SLOW_COMMAND, "timeout": 1},
                approval=True,
            )
        )
        data = result["data"]
        assert result["success"] is False
        assert data["timed_out"] is True
        assert data["exit_code"] is None, "a killed process has no exit code"
        assert "TIMED OUT" in result["error"]
        assert "limit 1s" in result["error"], "the deadline that fired must be named"
        if os.name == "nt":
            # Partial output must survive the kill: the readers are detached
            # from `communicate()` precisely so the deadline does not discard
            # everything the process managed to say.
            assert data["stdout"], "ping prints before the deadline"

    def test_default_cwd_is_the_workspace_root(self, registry, workspace):
        (workspace / "note.txt").write_text("hi", encoding="utf-8")
        result = asyncio.run(
            registry.execute(
                "execute_local_command", {"command": LISTING_COMMAND}, approval=True
            )
        )
        assert result["success"] is True, result
        assert "note.txt" in result["data"]["stdout"]
        assert result["data"]["cwd"] == str(workspace)

    def test_a_relative_cwd_resolves_inside_the_workspace(self, registry, workspace):
        sub = workspace / "project"
        sub.mkdir()
        (sub / "inner.txt").write_text("x", encoding="utf-8")
        result = asyncio.run(
            registry.execute(
                "execute_local_command",
                {"command": LISTING_COMMAND, "cwd": "project"},
                approval=True,
            )
        )
        assert result["success"] is True, result
        assert "inner.txt" in result["data"]["stdout"]

    def test_code_reports_the_language_it_used(self, registry):
        result = asyncio.run(
            registry.execute(
                "execute_code",
                {"code": "print(40 + 2)", "language": "python"},
                approval=True,
            )
        )
        assert result["success"] is True, result
        assert result["data"]["language"] == "python"
        assert "42" in result["data"]["stdout"]
        assert result["data"]["exit_code"] == 0

    def test_code_failure_is_not_reported_as_success(self, registry):
        result = asyncio.run(
            registry.execute(
                "execute_code",
                {"code": "raise SystemExit(3)", "language": "python"},
                approval=True,
            )
        )
        assert result["success"] is False
        assert result["data"]["exit_code"] == 3
        assert "exit=3" in result["error"]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
class TestValidation:
    @pytest.mark.parametrize("timeout", [0, -5, 5000, "soon", True])
    def test_a_bad_timeout_is_rejected_before_the_gate(self, registry, timeout):
        result = asyncio.run(
            registry.execute(
                "execute_local_command",
                {"command": "echo hi", "timeout": timeout},
            )
        )
        assert result["success"] is False
        assert not result.get("requires_confirmation")
        assert "timeout" in result["error"]

    @pytest.mark.parametrize("language", ["powershell", "bash", "cmd"])
    def test_shell_languages_are_blocked_not_rejected(self, registry, language):
        """A policy refusal is `blocked`, because retrying cannot fix it."""
        result = asyncio.run(
            registry.execute(
                "execute_code",
                {"code": "whoami", "language": language},
                approval=True,
            )
        )
        assert result["success"] is False
        assert result.get("blocked") is True, result
        assert not result.get("requires_confirmation")

    def test_an_unknown_language_is_a_validation_error(self, registry):
        result = asyncio.run(
            registry.execute(
                "execute_code",
                {"code": "x", "language": "cobol"},
                approval=True,
            )
        )
        assert result["success"] is False
        assert not result.get("blocked")
        assert "Unsupported language" in result["error"]

    def test_check_timeout_is_the_single_definition(self):
        assert check_timeout(None) == 30.0
        assert check_timeout(5) == 5.0
        with pytest.raises(ToolValidationError):
            check_timeout(MAX_TIMEOUT_S + 1)


# ---------------------------------------------------------------------------
# The unconditional sandbox blocks
# ---------------------------------------------------------------------------
class TestSandboxUnchanged:
    @pytest.mark.parametrize(
        "command",
        [
            "echo hi & hostname",
            "dir & whoami",
            "echo a | findstr b",
            "type nul > out.txt",
            "echo `whoami`",
            "echo $(whoami)",
            "echo %USERPROFILE%",
            "echo !PATH!",
        ],
    )
    def test_shell_syntax_is_refused_before_the_gate(self, registry, command):
        result = asyncio.run(
            registry.execute("execute_local_command", {"command": command})
        )
        assert result["success"] is False, command
        assert result.get("blocked") is True, (command, result)
        assert not result.get("requires_confirmation"), command

    def test_the_refusal_comes_from_the_shared_primitive(self):
        """One definition of 'this has shell syntax' — not a second copy."""
        with pytest.raises(SecurityError):
            sandbox.split_command("echo hi & hostname")

    def test_an_absolute_cwd_outside_the_workspace_is_denied(self, registry, workspace):
        result = asyncio.run(
            registry.execute(
                "execute_local_command",
                {"command": "echo hi", "cwd": os.path.dirname(str(workspace))},
                approval=True,
            )
        )
        assert result["success"] is False
        assert result.get("blocked") is True
        assert "outside the workspace" in result["error"]

    def test_a_cwd_that_does_not_exist_says_so(self, registry, workspace):
        result = asyncio.run(
            registry.execute(
                "execute_local_command",
                {"command": "echo hi", "cwd": "not_here_yet"},
                approval=True,
            )
        )
        assert result["success"] is False
        assert "does not exist" in result["error"]


# ---------------------------------------------------------------------------
# Tiers: the new escalation must not loosen anything
# ---------------------------------------------------------------------------
class TestTiers:
    def setup_method(self):
        self.assessor = RiskAssessor()

    def test_a_clean_command_stays_confirm(self):
        result = self.assessor.assess("execute_local_command", {"command": "echo hi"})
        assert result.permission == PERMISSION_CONFIRM
        assert result.requires_confirmation is True

    def test_a_hazardous_command_escalates(self):
        result = self.assessor.assess(
            "execute_local_command", {"command": "rm -rf /"}
        )
        assert result.permission == PERMISSION_DANGEROUS
        assert result.requires_confirmation is True
        assert any("recursive force delete" in r for r in result.reasons), result.reasons

    def test_the_escalation_is_scoped_to_the_structured_tool(self):
        """``system_command``'s classification is pinned elsewhere and must not
        move: adding a tool must never re-label an existing one."""
        result = self.assessor.assess("system_command", {"command": "rm -rf /"})
        assert result.permission == PERMISSION_CONFIRM
        assert result.requires_confirmation is True
        assert any("recursive force delete" in r for r in result.reasons), result.reasons

    def test_the_registry_keeps_the_stricter_tier(self, registry):
        """Declared ``confirm`` + assessor ``dangerous`` → dangerous."""
        assessment = registry.assess(
            "execute_local_command", {"command": "rm -rf /"}
        )
        assert assessment.permission == PERMISSION_DANGEROUS
        assert assessment.requires_confirmation is True

    def test_the_confirmation_event_carries_the_escalated_tier(self):
        """The dialog the user actually sees must say ``dangerous``, not
        ``confirm``. The escalation lives in the assessor, so a regression that
        drops it before the event is built would silently weaken the prompt
        while every ``assess()`` test stayed green."""
        assessment = RiskAssessor().assess(
            "execute_local_command", {"command": "rm -rf /"}
        )
        pending = PendingConfirmation(
            request_id="cfm_local_1",
            action="execute_local_command",
            params={"command": "rm -rf /"},
            assessment=assessment,
            session_id="s1",
        )
        event = pending.to_event()
        assert event["permission"] == PERMISSION_DANGEROUS
        assert event["level"] == "high"
        assert event["reasons"], event
        assert "rm -rf /" in str(event.get("params"))

    def test_code_execution_is_dangerous(self):
        result = self.assessor.assess(
            "execute_code", {"code": "print(1)", "language": "python"}
        )
        assert result.permission == PERMISSION_DANGEROUS
        assert result.requires_confirmation is True

    def test_every_alias_is_classified(self):
        for action in (
            "execute_local_command", "run_local_command", "exec_local",
            "execute_code", "exec_code", "run_snippet",
        ):
            result = self.assessor.assess(action, {})
            assert result.permission in (
                PERMISSION_CONFIRM, PERMISSION_DANGEROUS,
            ), (action, result.permission)
            assert result.requires_confirmation is True, action

    def test_risk_params_hide_the_timeout_from_the_pattern_rules(self, registry):
        """Only the source and its language are judged, not the envelope."""
        tool = registry.get("execute_code")
        params = {"code": "print(1)", "language": "python", "timeout": 30}
        assert set(tool.risk_params(params)) == {"code", "language"}


# ---------------------------------------------------------------------------
# The result object in isolation (no process needed)
# ---------------------------------------------------------------------------
class TestExecOutcome:
    def _outcome(self, **overrides):
        base = dict(
            argv=["echo", "hi"],
            exit_code=0,
            stdout="",
            stderr="",
            duration_ms=12,
            timed_out=False,
            truncated=False,
            timeout_s=30.0,
        )
        base.update(overrides)
        return ExecOutcome(**base)

    def test_render_reports_no_output_honestly(self):
        assert self._outcome().render() == "exit=0 in 12ms\n(no output)"

    def test_render_labels_stderr(self):
        text = self._outcome(stdout="fine", stderr="warning: deprecated").render()
        assert "fine" in text
        assert "--- stderr ---" in text
        assert "warning: deprecated" in text

    def test_render_names_a_timeout_as_a_timeout(self):
        text = self._outcome(timed_out=True, exit_code=None, duration_ms=5000).render()
        assert text.startswith("TIMED OUT after 5000ms (limit 30s)")

    def test_to_dict_never_confuses_none_with_zero(self):
        ok = self._outcome().to_dict()
        killed = self._outcome(exit_code=None, timed_out=True).to_dict()
        assert ok["exit_code"] == 0 and ok["success"] is True
        assert killed["exit_code"] is None and killed["success"] is False

    def test_truncation_is_announced_rather_than_silent(self):
        from tools.local_exec import _clip

        assert _clip("short") == "short"
        clipped = _clip("y" * 5_000, limit=10)
        assert clipped.startswith("y" * 10)
        assert "[truncated, 5000 chars total]" in clipped
        assert len(clipped) < 5_000
