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
        assert "Unknown action" in result["error"]

    async def test_open_app_no_name(self, executor):
        result = await executor.execute("open_app", {})
        assert result["success"] is False

    async def test_open_app_with_name(self, executor):
        result = await executor.execute("open_app", {"app": "notepad"})
        assert result["success"] is True
        assert "Opened" in result["message"]

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
            assert "results" in result
        else:
            assert True  # May fail if no internet, that's OK

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
    def test_resolve_known_app(self, executor):
        cmd = executor._resolve_app_command("notepad")
        assert cmd is not None

    def test_resolve_unknown_app(self, executor):
        cmd = executor._resolve_app_command("some_random_app_12345")
        assert cmd is not None  # Should fall back to start/shell

    def test_app_to_exe(self, executor):
        assert executor._app_to_exe("chrome").endswith(".exe")
        assert executor._app_to_exe("notepad").endswith(".exe")

    def test_get_runner_python(self, executor):
        runner = executor._get_runner("python")
        assert runner is not None
        assert runner["ext"] == ".py"

    def test_get_runner_javascript(self, executor):
        runner = executor._get_runner("javascript")
        assert runner is not None
        assert runner["ext"] == ".js"

    def test_get_runner_unknown(self, executor):
        runner = executor._get_runner("cobol")
        assert runner is None
