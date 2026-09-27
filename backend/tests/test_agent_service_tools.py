import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch

from services.agent_service import AgentService, ACTION_JSON_RE


@pytest.fixture
def mock_executor():
    ex = AsyncMock()
    ex.execute = AsyncMock(return_value={
        "success": True,
        "message": "Opened notepad",
        "output": "",
    })
    return ex


@pytest.fixture
def service(mock_executor):
    s = AgentService(
        router_agent=MagicMock(),
        fast_agent=MagicMock(),
        planner_agent=MagicMock(),
        code_agent=MagicMock(),
        tool_agent=MagicMock(),
        memory_agent=MagicMock(),
        scheduler_llm_agent=MagicMock(),
        scheduler_manager=MagicMock(),
        reminder_service=MagicMock(),
        notification_mgr=MagicMock(),
        tool_executor=mock_executor,
        os_toolkit=MagicMock(),
    )
    return s


class TestActionJsonRegex:
    def test_matches_simple_action(self):
        text = 'Doing this: {"action": "open_app", "app": "notepad"}'
        matches = ACTION_JSON_RE.findall(text)
        assert len(matches) > 0

    def test_matches_with_number_value(self):
        text = '{"action": "run_code", "language": "python", "code": "print(1)"}'
        matches = ACTION_JSON_RE.findall(text)
        assert len(matches) > 0

    def test_no_match_for_normal_text(self):
        text = "Hello, how are you?"
        matches = ACTION_JSON_RE.findall(text)
        assert len(matches) == 0

    def test_matches_multiple_actions(self):
        text = 'First: {"action": "open_app", "app": "spotify"} Then: {"action": "close_app", "app": "chrome"}'
        matches = ACTION_JSON_RE.findall(text)
        assert len(matches) >= 2


class TestExecuteToolCommands:
    async def test_no_action_in_text(self, service):
        result = await service._execute_tool_commands("Just some normal text")
        assert result == "Just some normal text"

    async def test_executes_action_and_replaces(self, service, mock_executor):
        text = '{"action": "open_app", "app": "notepad"}'
        result = await service._execute_tool_commands(text)
        assert "OPEN_APP" in result
        assert "Opened" in result or "notepad" in result
        mock_executor.execute.assert_called_once_with("open_app", {"app": "notepad"})

    async def test_executes_with_extra_params(self, service, mock_executor):
        # `run_code` is a high-risk action, so the raw-JSON path is now gated.
        # See TestHighRiskGating below for the approved case.
        text = '{"action": "run_code", "language": "python", "code": "print(1)"}'
        result = await service._execute_tool_commands(text)
        mock_executor.execute.assert_not_called()
        assert "cancelled" in result.lower()

    async def test_executes_with_extra_params_after_approval(self, service, mock_executor):
        async def approve(action, params, session_id=""):
            return True

        manager = MagicMock()
        manager.request = approve
        text = '{"action": "run_code", "language": "python", "code": "print(1)"}'
        result = await service._execute_tool_commands(text, confirmation_manager=manager)
        assert "RUN_CODE" in result
        mock_executor.execute.assert_called_once()

    async def test_handles_json_error_gracefully(self, service):
        text = '{"action": "open_app" "app": "bad json"}'
        result = await service._execute_tool_commands(text)
        # If regex matches but json.loads fails, it should still handle gracefully
        assert result is not None

    async def test_multiple_actions_in_sequence(self, service, mock_executor):
        mock_executor.execute = AsyncMock(return_value={
            "success": True, "message": "Done", "output": "",
        })
        text = (
            '{"action": "open_app", "app": "spotify"}\n'
            '{"action": "search_web", "query": "weather"}'
        )
        result = await service._execute_tool_commands(text)
        assert mock_executor.execute.call_count == 2
        assert "OPEN_APP" in result
        assert "SEARCH_WEB" in result


class TestHighRiskGating:
    """The raw-JSON tool path must honour the same sandbox as ToolAgent."""

    async def test_dangerous_command_is_blocked(self, service, mock_executor):
        text = '{"action": "system_command", "command": "rm -rf /"}'
        result = await service._execute_tool_commands(text)
        mock_executor.execute.assert_not_called()
        assert "cancelled" in result.lower()

    async def test_dangerous_command_runs_after_approval(self, service, mock_executor):
        async def approve(action, params, session_id=""):
            return True

        manager = MagicMock()
        manager.request = approve
        text = '{"action": "system_command", "command": "rm -rf /"}'
        await service._execute_tool_commands(text, confirmation_manager=manager)
        mock_executor.execute.assert_called_once()

    async def test_confirmation_channel_error_fails_closed(self, service, mock_executor):
        async def explode(action, params, session_id=""):
            raise RuntimeError("socket died")

        manager = MagicMock()
        manager.request = explode
        text = '{"action": "system_command", "command": "rm -rf /"}'
        result = await service._execute_tool_commands(text, confirmation_manager=manager)
        mock_executor.execute.assert_not_called()
        assert "cancelled" in result.lower()

    async def test_open_app_is_not_gated(self, service, mock_executor):
        text = '{"action": "open_app", "app": "notepad"}'
        await service._execute_tool_commands(text)
        mock_executor.execute.assert_called_once()


class TestDispatchActionIntent:
    async def test_action_intent_calls_tool_agent(self, service):
        service.tool.process = AsyncMock(return_value='{"action": "open_app", "app": "notepad"}')
        service.fast.process = AsyncMock(return_value="Done, Sir.")

        result = await service.dispatch(
            "action", "open notepad", [], "en", "sess_1", stream_callback=None,
        )
        assert result is not None
        service.tool.process.assert_called_once()

    async def test_action_intent_fast_agent_fallback(self, service):
        service.tool.process = AsyncMock(return_value="Hello")
        result = await service.dispatch(
            "action", "hello", [], "en", "sess_1", stream_callback=None,
        )
        assert result == "Hello"
