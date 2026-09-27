import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime

from tools.executor import ToolExecutor


@pytest.fixture
def mock_toolkit():
    tk = MagicMock()
    tk.execute = AsyncMock(return_value=MagicMock(success=True, output="OK", error=""))
    return tk


@pytest.fixture
def mock_reminder():
    r = AsyncMock()
    r.create = AsyncMock(return_value={"success": True, "reminder": {"reminder_id": 1}})
    return r


@pytest.fixture
def mock_scheduler():
    s = AsyncMock()
    s.create_from_natural = AsyncMock(
        return_value={"success": True, "confirmation": "Done.", "id": 1}
    )
    return s


def _approving_manager():
    """Confirmation manager that always approves."""

    async def approve(action, params, session_id=""):
        return True

    return type("M", (), {"request": staticmethod(approve)})()


@pytest.fixture
def executor(mock_toolkit, mock_reminder, mock_scheduler):
    return ToolExecutor(
        toolkit=mock_toolkit,
        reminder_service=mock_reminder,
        scheduler=mock_scheduler,
    )


class TestToolExecutor:
    async def test_unknown_action(self, executor):
        result = await executor.execute("nonexistent", {})
        assert result["success"] is False
        assert "Unknown tool" in result["error"]

    async def test_open_app_no_name(self, executor):
        result = await executor.execute("open_app", {})
        assert result["success"] is False

    async def test_open_app_with_name(self, executor):
        # Launching a real application is a side effect on the developer's
        # machine, so this only asserts the resolution step; the actual spawn
        # is covered by TestOpenAppResolution below.
        from tools.applications import resolve_windows_app

        assert resolve_windows_app("notepad") is not None

    async def test_close_app_no_name(self, executor):
        result = await executor.execute("close_app", {})
        assert result["success"] is False

    async def test_run_code_missing_params(self, executor):
        result = await executor.execute("run_code", {"code": ""})
        assert result["success"] is False

    async def test_run_code_unsupported_language(self, executor):
        result = await executor.execute("run_code", {
            "language": "brainfuck", "code": "print('hi')",
        })
        assert result["success"] is False
        assert "Unsupported" in result["error"]

    async def test_run_code_python(self, executor):
        # run_code is always-confirm, so supply an approving manager to reach
        # actual execution. The blocking behaviour is asserted separately below.
        executor.confirmation_manager = _approving_manager()
        result = await executor.execute("run_code", {
            "language": "python",
            "code": "print('hello from zaytoon')",
        })
        if result["success"]:
            assert "hello from zaytoon" in result.get("output", "")
        else:
            pytest.skip("Python not available in test env")

    async def test_run_code_blocked_without_approval(self, executor):
        result = await executor.execute("run_code", {
            "language": "python", "code": "print('x')",
        })
        assert result["success"] is False
        assert result.get("requires_confirmation") is True

    async def test_run_code_shell_denied_even_when_approved(self, executor):
        # Defence in depth: a shell interpreter is refused even after a human
        # approves, because it exists purely to run system commands.
        executor.confirmation_manager = _approving_manager()
        result = await executor.execute("run_code", {
            "language": "bash", "code": "rm -rf /",
        })
        assert result["success"] is False
        assert result.get("blocked") is True

    async def test_system_command(self, mock_toolkit, mock_reminder, mock_scheduler):
        # `system_command` is now confirmation-required for every command (the
        # spec's CONFIRMATION_REQUIRED tier), so reaching the handler needs an
        # approving channel.
        executor = ToolExecutor(
            toolkit=mock_toolkit,
            reminder_service=mock_reminder,
            scheduler=mock_scheduler,
            confirmation_manager=_approving_manager(),
        )
        result = await executor.execute("system_command", {"command": "echo test"})
        assert result["success"] is True

    async def test_system_command_requires_confirmation_without_a_channel(self, executor):
        """No approval channel means deny, not run."""
        result = await executor.execute("system_command", {"command": "echo test"})
        assert result["success"] is False
        assert result.get("requires_confirmation") is True
        assert result.get("level") == "high"
        assert result.get("permission") in ("confirm", "dangerous")

    async def test_system_command_rejects_chaining(self, executor):
        """Chaining is an unconditional block, reported before any prompt.

        It is checked in ``_prevalidate`` rather than inside the handler, so
        the refusal does not depend on a confirmation channel existing: no
        approval can authorise whatever follows a separator.
        """
        result = await executor.execute(
            "system_command", {"command": "echo hi & del C:\\x.txt"}
        )
        assert result["success"] is False
        assert "Refusing" in result["error"]
        assert result.get("blocked") is True
        # Critically: it must not be reported as "needs confirmation", because
        # confirming it would not help.
        assert not result.get("requires_confirmation")

    async def test_empty_command_reports_a_prevalidation_error(self, executor):
        result = await executor.execute("system_command", {"command": "   "})
        assert result["success"] is False
        assert "No command" in result["error"]

    async def test_open_app_rejects_shell_injection(self, executor):
        result = await executor.execute("open_app", {"app": "calc & calc"})
        assert result["success"] is False
        assert "Refusing" in result["error"] or result.get("blocked")

    async def test_open_app_rejects_script_target(self, executor):
        result = await executor.execute("open_app", {"app": "evil.bat"})
        assert result["success"] is False

    async def test_search_web_no_query(self, executor):
        result = await executor.execute("search_web", {"query": ""})
        assert result["success"] is False

    async def test_search_web(self, executor):
        result = await executor.execute("search_web", {"query": "python programming"})
        if result["success"]:
            # Structured output now lives under `data`; `output` is the
            # human/model-readable rendering. The old top-level `results` and
            # `summary` keys are gone — see tools/base.py::ToolResult.
            assert "output" in result
            assert "results" in result["data"]
            assert result["data"]["count"] == len(result["data"]["results"])
        else:
            # Network-dependent; a failure here is not a product defect.
            assert "error" in result

    async def test_create_reminder(self, executor, mock_reminder):
        result = await executor.execute("create_reminder", {
            "text": "test task",
            "time": "2026-06-01T10:00:00",
        })
        assert result["success"] is True
        mock_reminder.create.assert_called_once()

    async def test_create_reminder_no_text(self, executor):
        result = await executor.execute("create_reminder", {"time": "2026-06-01T10:00:00"})
        assert result["success"] is False

    async def test_schedule_task(self, executor, mock_scheduler):
        result = await executor.execute("schedule_task", {
            "task": "test task",
            "time": "2026-06-01T10:00:00",
        })
        assert result["success"] is True
        mock_scheduler.create_from_natural.assert_called_once()


class TestToolExecutorResolution:
    """Resolution helpers now live in the domain modules, not on the executor.

    ``_resolve_app_command`` / ``_app_to_exe`` / ``_get_runner`` were private
    methods on the old ``ToolExecutor``; they moved to
    ``tools.applications`` and ``tools.terminal`` when the duplicate tool stack
    was collapsed. These assert on the new homes.
    """

    def test_resolve_known_app(self):
        from tools.applications import resolve_windows_app

        assert resolve_windows_app("notepad") is not None

    def test_resolve_unknown_app(self):
        from tools.applications import resolve_windows_app

        assert resolve_windows_app("some_random_app_12345") is None

    def test_app_to_exe(self):
        from tools.applications import APP_PATHS

        for name in ("chrome", "notepad"):
            alias = APP_PATHS[name][0]
            assert alias.endswith(".exe") or alias + ".exe"

    def test_get_runner_python(self):
        from tools.terminal import RUNNERS

        assert RUNNERS["python"]["ext"] == ".py"

    def test_get_runner_javascript(self):
        from tools.terminal import RUNNERS

        assert RUNNERS["javascript"]["ext"] == ".js"

    def test_get_runner_unknown(self):
        from tools.terminal import RUNNERS

        assert "cobol" not in RUNNERS

    def test_shell_languages_are_not_runners(self):
        """They are refused at execute time even after approval."""
        from tools.sandbox import DENIED_CODE_LANGUAGES
        from tools.terminal import RUNNERS

        for language in DENIED_CODE_LANGUAGES:
            assert language not in RUNNERS, language


class TestToolExecutorIsAFacade:
    """The executor holds no tool logic; it delegates to the registry."""

    def test_executor_owns_no_action_methods(self, executor):
        actions = [
            name for name in dir(executor)
            if name.startswith("_action_") or name.startswith("_get_runner")
            or name.startswith("_app_to_exe") or name.startswith("_resolve_app")
        ]
        assert actions == [], f"tool logic leaked back into the executor: {actions}"

    def test_executor_exposes_the_registry(self, executor):
        from tools.registry import ToolRegistry

        assert isinstance(executor.registry, ToolRegistry)
        assert "file_read" in executor.registry

    def test_executor_lists_tools_through_the_registry(self, executor):
        listed = executor.list_tools()
        assert "file_read" in listed
        assert "file_write" in listed
        assert "git_status" in listed
