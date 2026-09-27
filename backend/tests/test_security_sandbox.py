"""Security sandbox tests.

Covers the three defects found in the audit plus the standing sandbox policy:

1. ``WindowsCmdTool`` used ``subprocess.run(command, shell=True)`` for
   ``echo``/``dir``/``date``/``time``, so ``echo hi & del file`` passed the
   allow-list and executed the chained command.
2. Path containment used ``str.startswith`` on absolute paths, so a sibling
   directory sharing a name prefix (``app`` vs ``app_evil``) escaped the
   workspace check.
3. ``ToolExecutor._action_system_command`` ran arbitrary shell strings with no
   allow-list, and app targets were interpolated into ``start "<target>"``.
"""

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.sandbox import (  # noqa: E402
    DENIED_CODE_LANGUAGES,
    SecurityError,
    has_shell_metachars,
    is_blocked_executable,
    is_safe_path,
    sanitize_app_name,
    split_command,
    validate_app_target,
)
from tools.executor import resolve_windows_app  # noqa: E402
from tools.windows import WindowsCmdTool  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Shell injection
# ---------------------------------------------------------------------------
INJECTION_PAYLOADS = [
    "echo pwned & hostname",
    "echo hi & del C:\\secret.txt",
    "echo hi && whoami",
    "echo hi | hostname",
    "echo hi || whoami",
    "dir > C:\\out.txt",
    "echo hi >> C:\\out.txt",
    "echo $(hostname)",
    "echo `hostname`",
    "echo a`hostname`b",
    "echo hi ; whoami",
    "echo hi\nwhoami",
    "echo hi & shutdown /s /t 0",
    "dir & format C:",
    "echo %USERNAME%",
    "echo hi & net user add hacker",
]


class TestSplitCommand:
    @pytest.mark.parametrize("payload", INJECTION_PAYLOADS)
    def test_rejects_shell_syntax(self, payload):
        with pytest.raises(SecurityError):
            split_command(payload)

    @pytest.mark.parametrize(
        "command,expected",
        [
            ("ls", ["ls"]),
            ("echo hi", ["echo", "hi"]),
            ("hostname", ["hostname"]),
            ("dir C:\\Users", ["dir", "C:\\Users"]),
        ],
    )
    def test_accepts_plain_commands(self, command, expected):
        assert split_command(command) == expected

    @pytest.mark.parametrize("command", ["", "   ", None])
    def test_rejects_empty(self, command):
        with pytest.raises(SecurityError):
            split_command(command)

    def test_semicolon_in_path_is_rejected(self):
        # Conservative: a legitimate filename with ';' is refused rather than
        # risking a shell reinterpretation.
        with pytest.raises(SecurityError):
            split_command("cat /tmp/a;rm")


class TestMetacharDetection:
    @pytest.mark.parametrize("value", ["a&b", "a|b", "a>b", "a<b", "a;b", "a`b", "a$b", "a*b", "a?b"])
    def test_detects_metachars(self, value):
        assert has_shell_metachars(value) is True

    @pytest.mark.parametrize("value", ["notepad", "C:\\Program Files\\app.exe", ""])
    def test_plain_values_are_clean(self, value):
        assert has_shell_metachars(value) is False


class TestWindowsCmdToolInjection:
    @pytest.fixture
    def tool(self):
        return WindowsCmdTool()

    @pytest.mark.parametrize("payload", INJECTION_PAYLOADS)
    async def test_all_injection_payloads_blocked(self, tool, payload):
        result = await tool.execute(payload)
        assert result.success is False
        assert result.error

    async def test_exploit_from_audit_is_closed(self, tool):
        """The exact proof-of-concept that previously leaked the hostname."""
        result = await tool.execute("echo pwned & hostname")
        assert result.success is False
        assert "DESKTOP" not in (result.output or "").upper()

    async def test_chained_delete_is_blocked(self, tool):
        result = await tool.execute("echo hi & del C:\\Windows\\System32\\config /s")
        assert result.success is False
        assert "Refusing" in result.error

    async def test_redirection_is_blocked(self, tool):
        result = await tool.execute("dir > C:\\evil.txt")
        assert result.success is False

    async def test_benign_commands_still_work(self, tool):
        """The fix must not break legitimate use of the allow-list."""
        result = await tool.execute("echo hello")
        assert result.success is True
        assert "hello" in result.output

    async def test_allowlist_still_enforced(self, tool):
        result = await tool.execute("format C:")
        assert result.success is False
        assert "not allowed" in result.error.lower()

    async def test_destructive_allowlist_lookalike_blocked(self, tool):
        # `format` is not in the allow-list; a lookalike must not slip through.
        assert (await tool.execute("cmd")).success is False
        assert (await tool.execute("del")).success is False

    async def test_unknown_command_reports_cleanly(self, tool):
        result = await tool.execute("definitely_not_a_command_xyz")
        assert result.success is False
        assert "not allowed" in result.error.lower()


# ---------------------------------------------------------------------------
# 2. Path containment
# ---------------------------------------------------------------------------
class TestPathContainment:
    def test_path_inside_root_allowed(self, tmp_path):
        root = tmp_path / "app"
        root.mkdir()
        inside = root / "file.txt"
        inside.write_text("ok")
        assert is_safe_path(str(inside), str(root)) is True

    def test_sibling_prefix_directory_rejected(self, tmp_path):
        """The bug class `str.startswith` cannot detect."""
        root = tmp_path / "app"
        root.mkdir()
        sibling = tmp_path / "app_evil"
        sibling.mkdir()
        assert is_safe_path(str(sibling), str(root)) is False
        # Show the old, broken check would have passed:
        assert str(sibling).startswith(str(root)) is True

    def test_parent_traversal_rejected(self, tmp_path):
        root = tmp_path / "app"
        root.mkdir()
        assert is_safe_path(str(tmp_path / "secret.txt"), str(root)) is False
        assert is_safe_path("../../etc/passwd", str(root)) is False

    def test_root_itself_allowed(self, tmp_path):
        assert is_safe_path(str(tmp_path), str(tmp_path)) is True

    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_empty_rejected(self, value):
        assert is_safe_path(value) is False

    def test_different_drive_rejected(self):
        assert is_safe_path("Z:\\etc\\passwd", "C:\\Users\\x") is False


class TestFileToolContainment:
    """The tools confine paths to the *workspace root*, so the attack is a
    *sibling of the workspace* whose name merely starts with it.

    Note the root used to be ``os.getcwd()``, which silently meant ``backend/``
    because the server must be launched from there. It is now the explicit
    ``config.workspace_path``; these tests point that at a temp directory so the
    assertion is about containment rather than about the developer's machine.
    """

    @staticmethod
    def _sibling_attack(monkeypatch, tmp_path):
        """workspace = <tmp>/app ; attacker dir = <tmp>/app_evil."""
        from tools import sandbox

        root = tmp_path / "app"
        root.mkdir()
        (root / "ok.txt").write_text("fine")
        evil = tmp_path / "app_evil"
        evil.mkdir()
        (evil / "secret.txt").write_text("stolen")
        monkeypatch.setattr(sandbox, "workspace_root", lambda: str(root))
        return root, evil

    async def test_windows_file_tool_rejects_sibling_prefix(self, monkeypatch, tmp_path):
        from tools.windows import WindowsFileTool

        root, evil = self._sibling_attack(monkeypatch, tmp_path)
        tool = WindowsFileTool()

        allowed = await tool.execute("ok.txt", action="read")
        assert allowed.success is True
        assert "fine" in allowed.output

        denied = await tool.execute(str(evil / "secret.txt"), action="read")
        assert denied.success is False
        assert "denied" in denied.error.lower()

    async def test_linux_file_tool_rejects_sibling_prefix(self, monkeypatch, tmp_path):
        from tools.linux import LinuxFileTool

        _root, evil = self._sibling_attack(monkeypatch, tmp_path)
        result = await LinuxFileTool().execute(str(evil / "secret.txt"), action="read")
        assert result.success is False
        assert "denied" in result.error.lower()

    async def test_tool_agent_file_write_rejects_sibling_prefix(self, monkeypatch, tmp_path):
        """The LLM path must enforce containment too.

        This used to call ``ToolAgent._toolkit_file_write``, one of ten private
        duplicates of the executor's tools. It is now asserted through the
        registry, which is what the agent actually uses.
        """
        from agents.tool_agent import ToolAgent

        _root, evil = self._sibling_attack(monkeypatch, tmp_path)
        target = evil / "written.txt"

        class _Approve:
            async def request(self, *a, **k):
                return True

        agent = ToolAgent(confirmation_manager=_Approve())
        result = await agent._execute_single_tool(
            "file_write", {"path": str(target), "content": "data"}
        )
        assert "Access denied" in result or "Blocked" in result
        assert not target.exists()

    async def test_tool_agent_file_write_allows_inside(self, monkeypatch, tmp_path):
        from agents.tool_agent import ToolAgent

        root, _evil = self._sibling_attack(monkeypatch, tmp_path)
        target = root / "sub" / "written.txt"

        class _Approve:
            async def request(self, *a, **k):
                return True

        agent = ToolAgent(confirmation_manager=_Approve())
        result = await agent._execute_single_tool(
            "file_write", {"path": str(target), "content": "data"}
        )
        assert "Wrote" in result
        assert target.read_text() == "data"


class TestWorkspaceRootIsNotCwd:
    """The containment root must be the explicit workspace, not the cwd.

    This is the regression guard for the change that made file operations
    possible at all: the backend is launched from ``backend/``, so a cwd-based
    root confined the assistant to its own source tree.
    """

    def test_workspace_root_defaults_outside_the_backend(self, monkeypatch, tmp_path):
        """The default workspace is a dedicated folder, not the repo/backend."""
        from config import config

        default = config.DEFAULT_WORKSPACE_DIRNAME
        assert default, "a default workspace directory name must be defined"
        assert default.lower() not in ("backend", ".", "app", "src")
        assert (tmp_path / default) != (tmp_path / "backend")

    def test_workspace_root_resolves_and_creates_itself(self, monkeypatch, tmp_path):
        from config import config

        target = tmp_path / "ws"
        monkeypatch.setattr(config, "WORKSPACE_ROOT", str(target), raising=False)
        assert config.workspace_path == target.resolve()
        assert target.is_dir()

    def test_unresolvable_workspace_falls_back_instead_of_raising(self, monkeypatch, tmp_path):
        """A bad override degrades to a safe default, it does not break startup."""
        from config import config

        monkeypatch.setattr(
            config, "WORKSPACE_ROOT", "\0invalid\0path", raising=False
        )
        resolved = config.workspace_path
        assert resolved.is_dir()
        assert config.DEFAULT_WORKSPACE_DIRNAME in str(resolved)

    def test_sandbox_uses_the_workspace_not_the_cwd(self, monkeypatch, tmp_path):
        from tools import sandbox

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.setattr(sandbox, "workspace_root", lambda: str(workspace))
        monkeypatch.chdir(elsewhere)

        assert sandbox.is_safe_path(str(workspace / "file.txt")) is True
        # Previously allowed, because it was under the cwd.
        assert sandbox.is_safe_path(str(elsewhere / "file.txt")) is False

    def test_resolve_workspace_returns_the_concrete_absolute_path(self, monkeypatch, tmp_path):
        from tools import sandbox

        workspace = tmp_path / "workspace"
        (workspace / "sub").mkdir(parents=True)
        monkeypatch.setattr(sandbox, "workspace_root", lambda: str(workspace))

        resolved = sandbox.resolve_workspace("sub/file.txt")
        assert resolved
        assert Path(resolved) == (workspace / "sub" / "file.txt").resolve()
        assert Path(resolved).is_absolute()

    def test_resolve_workspace_returns_empty_for_an_escape(self, monkeypatch, tmp_path):
        from tools import sandbox

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        monkeypatch.setattr(sandbox, "workspace_root", lambda: str(workspace))
        assert sandbox.resolve_workspace("../escape.txt") == ""

    def test_resolve_workspace_rejects_empty_input(self, monkeypatch, tmp_path):
        from tools import sandbox

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        monkeypatch.setattr(sandbox, "workspace_root", lambda: str(workspace))
        assert sandbox.resolve_workspace("") == ""
        assert sandbox.resolve_workspace("   ") == ""

    def test_assert_safe_path_error_names_the_workspace(self, monkeypatch, tmp_path):
        from tools import sandbox

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        monkeypatch.setattr(sandbox, "workspace_root", lambda: str(workspace))
        with pytest.raises(sandbox.SecurityError) as exc:
            sandbox.assert_safe_path(str(tmp_path / "outside.txt"))
        assert str(workspace) in str(exc.value)


# ---------------------------------------------------------------------------
# 3. ToolExecutor gates and app-target validation
# ---------------------------------------------------------------------------
class TestAppNameSanitisation:
    @pytest.mark.parametrize(
        "value", ["calc & calc", "a|b", "a;calc", "a`calc`", "x$(calc)", "a&&b"]
    )
    def test_rejects_shell_syntax(self, value):
        with pytest.raises(SecurityError):
            sanitize_app_name(value)

    @pytest.mark.parametrize("value", ["notepad", "chrome.exe", "Visual Studio Code"])
    def test_accepts_plain_names(self, value):
        assert sanitize_app_name(value).lower() == value.lower()

    def test_strips_path_traversal(self):
        assert sanitize_app_name("../../evil.exe") == "evil.exe"
        assert sanitize_app_name(r"C:\Windows\System32\notepad.exe") == (
            "notepad.exe"
        )

    @pytest.mark.parametrize("value", ["evil.bat", "evil.cmd", "evil.ps1", "x.vbs"])
    def test_rejects_script_targets(self, value):
        with pytest.raises(SecurityError):
            sanitize_app_name(value)

    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_rejects_empty(self, value):
        with pytest.raises(SecurityError):
            sanitize_app_name(value)

    def test_is_blocked_executable(self):
        assert is_blocked_executable("a.bat") is True
        assert is_blocked_executable("a.exe") is False

    def test_validate_app_target(self):
        assert validate_app_target("C:\\Program Files\\app.exe") is not None
        with pytest.raises(SecurityError):
            validate_app_target("calc & calc")
        with pytest.raises(SecurityError):
            validate_app_target("payload.ps1")
        assert validate_app_target(None) is None


class TestResolveWindowsApp:
    def test_unsafe_name_returns_none(self):
        assert resolve_windows_app("calc & calc") is None
        assert resolve_windows_app("a|calc") is None

    def test_script_name_returns_none(self):
        assert resolve_windows_app("evil.bat") is None

    def test_nonexistent_path_returns_none(self):
        assert resolve_windows_app("C:\\definitely\\not\\here.exe") is None

    def test_known_alias_resolves(self):
        target = resolve_windows_app("notepad")
        assert target is not None
        assert "notepad" in target.lower()


class TestDeniedCodeLanguages:
    @pytest.mark.parametrize(
        "lang", ["bash", "sh", "shell", "cmd", "batch", "powershell", "ps1", "zsh"]
    )
    def test_shells_are_denied(self, lang):
        assert lang in DENIED_CODE_LANGUAGES

    @pytest.mark.parametrize("lang", ["python", "javascript", "js", "typescript", "go"])
    def test_sandboxed_languages_allowed(self, lang):
        assert lang not in DENIED_CODE_LANGUAGES

    async def test_tool_agent_refuses_shell_code(self):
        """A shell interpreter is refused even when the user approves.

        Asserted through the registry rather than the deleted
        ``ToolAgent._run_code_tool``.
        """
        from agents.tool_agent import ToolAgent

        class _Approve:
            async def request(self, *a, **k):
                return True

        agent = ToolAgent(confirmation_manager=_Approve())
        result = await agent._execute_single_tool(
            "run_code", {"code": "rm -rf /", "language": "bash"}
        )
        assert "not permitted" in result

    async def test_executor_refuses_shell_code_even_when_approved(self):
        from unittest.mock import AsyncMock, MagicMock

        from tools.executor import ToolExecutor

        async def approve(action, params, session_id=""):
            return True

        ex = ToolExecutor(
            toolkit=MagicMock(execute=AsyncMock()),
            confirmation_manager=type("M", (), {"request": staticmethod(approve)})(),
        )
        result = await ex.execute("run_code", {"code": "rm -rf /", "language": "bash"})
        assert result["success"] is False
        assert result.get("blocked") is True


class TestToolExecutorSystemCommand:
    @pytest.fixture
    def executor(self):
        from unittest.mock import AsyncMock, MagicMock

        from tools.executor import ToolExecutor

        return ToolExecutor(toolkit=MagicMock(execute=AsyncMock()))

    @pytest.mark.parametrize("payload", INJECTION_PAYLOADS)
    async def test_injection_blocked(self, executor, payload):
        result = await executor.execute("system_command", {"command": payload})
        assert result["success"] is False

    async def test_destructive_requires_confirmation(self, executor):
        result = await executor.execute(
            "system_command", {"command": "del C:\\Windows\\System32\\config /s"}
        )
        assert result["success"] is False
        assert result.get("requires_confirmation") is True

    async def test_unknown_command_reported(self, executor):
        """A command that does not exist still needs approval to be attempted.

        ``system_command`` is confirmation-required, so without a channel the
        call stops at the gate. Attach an approving manager so the test reaches
        the "command not found" resolution the sandbox performs.
        """

        class _Approve:
            async def request(self, action, params=None, session_id=""):
                return True

        executor.confirmation_manager = _Approve()
        result = await executor.execute(
            "system_command", {"command": "definitely_not_real_xyz"}
        )
        assert result["success"] is False
        assert "not found" in result["error"].lower()

    async def test_unknown_command_is_gated_without_a_channel(self, executor):
        """Without approval the unknown command never even reaches resolution."""
        result = await executor.execute(
            "system_command", {"command": "definitely_not_real_xyz"}
        )
        assert result["success"] is False
        assert result.get("requires_confirmation") is True

    async def test_empty_command_reported(self, executor):
        result = await executor.execute("system_command", {"command": "  "})
        assert result["success"] is False
        assert "No command" in result["error"]


class TestToolExecutorRiskGate:
    @pytest.fixture
    def executor(self):
        from unittest.mock import AsyncMock, MagicMock

        from tools.executor import ToolExecutor

        return ToolExecutor(toolkit=MagicMock(execute=AsyncMock()))

    async def test_high_risk_blocked_without_manager(self, executor):
        result = await executor.execute(
            "system_command", {"command": "rm -rf /"}
        )
        assert result["success"] is False
        assert result.get("requires_confirmation") is True

    async def test_high_risk_runs_after_approval(self):
        from unittest.mock import AsyncMock, MagicMock

        from tools.executor import ToolExecutor

        async def approve(action, params, session_id=""):
            return True

        ex = ToolExecutor(
            toolkit=MagicMock(execute=AsyncMock()),
            confirmation_manager=type("M", (), {"request": staticmethod(approve)})(),
        )
        result = await ex.execute("system_command", {"command": "echo approved"})
        assert result["success"] is True

    async def test_denied_returns_cancelled(self):
        from unittest.mock import AsyncMock, MagicMock

        from tools.executor import ToolExecutor

        async def deny(action, params, session_id=""):
            return False

        ex = ToolExecutor(
            toolkit=MagicMock(execute=AsyncMock()),
            confirmation_manager=type("M", (), {"request": staticmethod(approve := deny)})(),
        )
        result = await ex.execute("system_command", {"command": "rm -rf /"})
        assert result["success"] is False
        assert result.get("cancelled") is True

    async def test_confirmation_error_fails_closed(self):
        from unittest.mock import AsyncMock, MagicMock

        from tools.executor import ToolExecutor

        async def boom(action, params, session_id=""):
            raise RuntimeError("socket died")

        ex = ToolExecutor(
            toolkit=MagicMock(execute=AsyncMock()),
            confirmation_manager=type("M", (), {"request": staticmethod(boom)})(),
        )
        result = await ex.execute("system_command", {"command": "rm -rf /"})
        assert result["success"] is False

    async def test_validation_precedes_the_gate(self, executor):
        """A malformed request gets a precise error, not an approval prompt."""
        result = await executor.execute("run_code", {"code": "x", "language": "cobol"})
        assert result["success"] is False
        assert "Unsupported" in result["error"]
        assert "requires_confirmation" not in result

    async def test_read_only_actions_never_ask(self, executor):
        for action, params in (
            ("get_time", {}),
            ("system_info", {}),
            ("search_web", {"query": "weather"}),
        ):
            result = await executor.execute(action, params)
            assert "requires_confirmation" not in result
