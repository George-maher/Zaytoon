"""ToolAgent integration with the human-in-the-loop sandbox.

Verifies that a high-risk tool call pauses, emits a `require_confirmation`
event, and only executes after the user approves.
"""

import pytest

from agents.tool_agent import ToolAgent
from config import config
from tools.safety import ConfirmationManager, RiskAssessor


@pytest.fixture
def assessor():
    return RiskAssessor(always_confirm=[])


def spy_cmd(calls):
    """Async replacement for ToolAgent._toolkit_cmd that records invocations."""

    async def _cmd(command, shell="cmd"):
        calls.append(command)
        return "done"

    return _cmd


class TestSafetyGate:
    async def test_safe_tool_runs_without_confirmation(self, assessor):
        agent = ToolAgent(confirmation_manager=None, risk_assessor=assessor)
        result = await agent._execute_single_tool("get_time", {})
        assert "Error" not in result
        assert ":" in result

    async def test_no_channel_available_is_reported(self, assessor):
        """A manager with no notifier cannot ask anyone, so nothing runs."""
        manager = ConfirmationManager(notify=None, timeout=1)
        agent = ToolAgent(confirmation_manager=manager, risk_assessor=assessor)

        executed = []
        agent._toolkit_cmd = spy_cmd(executed)

        result = await agent._execute_single_tool(
            "system_command", {"command": "rm -rf /"}
        )
        assert executed == []
        assert "Blocked" in result
        assert manager.last_outcome == ConfirmationManager.OUTCOME_UNAVAILABLE

    async def test_no_manager_at_all_is_reported(self, assessor):
        agent = ToolAgent(confirmation_manager=None, risk_assessor=assessor)
        executed = []
        agent._toolkit_cmd = spy_cmd(executed)

        result = await agent._execute_single_tool(
            "system_command", {"command": "rm -rf /"}
        )
        assert executed == []
        assert "Blocked" in result

    async def test_explicit_decline_is_reported_as_cancelled(self, assessor):
        async def deny(pending):
            manager.resolve(pending.request_id, False)

        manager = ConfirmationManager(notify=deny, timeout=2)
        agent = ToolAgent(confirmation_manager=manager, risk_assessor=assessor)

        executed = []
        agent._toolkit_cmd = spy_cmd(executed)

        result = await agent._execute_single_tool(
            "system_command", {"command": "rm -rf /"}
        )
        assert executed == []
        assert "Cancelled" in result
        assert manager.last_outcome == ConfirmationManager.OUTCOME_DECLINED

    async def test_timeout_is_distinguished_from_decline(self, assessor):
        async def never_answers(pending):
            return None

        manager = ConfirmationManager(notify=never_answers, timeout=0.05)
        agent = ToolAgent(confirmation_manager=manager, risk_assessor=assessor)

        executed = []
        agent._toolkit_cmd = spy_cmd(executed)

        result = await agent._execute_single_tool(
            "system_command", {"command": "rm -rf /"}
        )
        assert executed == []
        assert "no confirmation received in time" in result
        assert manager.last_outcome == ConfirmationManager.OUTCOME_TIMEOUT

    async def test_dangerous_command_runs_after_approval(self, assessor):
        requests = []

        async def notify(pending):
            requests.append(pending)
            manager.resolve(pending.request_id, True)

        manager = ConfirmationManager(notify=notify, timeout=2)
        agent = ToolAgent(confirmation_manager=manager, risk_assessor=assessor)

        executed = []
        agent._toolkit_cmd = spy_cmd(executed)

        result = await agent._execute_single_tool(
            "system_command", {"command": "rm -rf /"}, language="en"
        )

        assert executed == ["rm -rf /"]
        assert result == "done"
        assert len(requests) == 1
        assert requests[0].action == "system_command"

    async def test_run_code_blocked_by_default_always_confirm(self):
        # Default assessor: run_code is in TOOL_ALWAYS_CONFIRM.
        agent = ToolAgent(confirmation_manager=None)
        executed = []
        agent._run_code_tool = spy_cmd(executed)

        result = await agent._execute_single_tool(
            "run_code", {"code": "print(1)", "language": "python"}
        )
        assert executed == []
        assert "Blocked" in result

    async def test_arabic_cancellation_message(self, assessor):
        async def deny(pending):
            manager.resolve(pending.request_id, False)

        manager = ConfirmationManager(notify=deny, timeout=2)
        agent = ToolAgent(confirmation_manager=manager, risk_assessor=assessor)
        agent._toolkit_cmd = spy_cmd([])
        result = await agent._execute_single_tool(
            "system_command", {"command": "rm -rf /"}, language="ar"
        )
        assert "اتلغت" in result

    async def test_arabic_unavailable_message(self, assessor):
        manager = ConfirmationManager(notify=None, timeout=1)
        agent = ToolAgent(confirmation_manager=manager, risk_assessor=assessor)
        agent._toolkit_cmd = spy_cmd([])
        result = await agent._execute_single_tool(
            "system_command", {"command": "rm -rf /"}, language="ar"
        )
        assert "متوقف" in result

    async def test_cached_result_cannot_bypass_confirmation(self, assessor):
        manager = ConfirmationManager(notify=None, timeout=1)
        agent = ToolAgent(confirmation_manager=manager, risk_assessor=assessor)

        executed = []
        agent._toolkit_cmd = spy_cmd(executed)

        first = await agent._execute_single_tool("system_command", {"command": "rm -rf /x"})
        second = await agent._execute_single_tool("system_command", {"command": "rm -rf /x"})

        # A naive implementation would cache "done" and skip the gate on the
        # second call; the gate must run before the cache lookup.
        assert executed == []
        assert "Cached" not in first
        assert "Cached" not in second

    async def test_safety_disabled_allows_execution(self, monkeypatch):
        monkeypatch.setattr(config, "TOOL_SAFETY_ENABLED", False, raising=False)
        agent = ToolAgent(
            confirmation_manager=ConfirmationManager(notify=None, timeout=1),
            risk_assessor=RiskAssessor(),
        )
        executed = []
        agent._toolkit_cmd = spy_cmd(executed)

        result = await agent._execute_single_tool(
            "system_command", {"command": "rm -rf /"}
        )
        assert executed == ["rm -rf /"]
        assert result == "done"


class TestExecuteToolsIntegration:
    async def test_json_tool_call_is_gated(self, assessor):
        manager = ConfirmationManager(notify=None, timeout=1)
        agent = ToolAgent(confirmation_manager=manager, risk_assessor=assessor)
        executed = []
        agent._toolkit_cmd = spy_cmd(executed)

        result = await agent.execute_tools(
            '{"action": "system_command", "command": "shutdown /s"}'
        )
        assert executed == []
        assert "Blocked" in result

    async def test_json_tool_call_denied_by_user(self, assessor):
        async def deny(pending):
            manager.resolve(pending.request_id, False)

        manager = ConfirmationManager(notify=deny, timeout=2)
        agent = ToolAgent(confirmation_manager=manager, risk_assessor=assessor)
        executed = []
        agent._toolkit_cmd = spy_cmd(executed)

        result = await agent.execute_tools(
            '{"action": "system_command", "command": "shutdown /s"}'
        )
        assert executed == []
        assert "Cancelled" in result

    async def test_nested_params_form_is_gated(self, assessor):
        manager = ConfirmationManager(notify=None, timeout=1)
        agent = ToolAgent(confirmation_manager=manager, risk_assessor=assessor)
        executed = []
        agent._toolkit_cmd = spy_cmd(executed)

        result = await agent.execute_tools(
            '{"action": "system_command", "params": {"command": "format C:"}}'
        )
        assert executed == []
        assert "Blocked" in result

    async def test_safe_json_tool_call_still_executes(self, assessor):
        agent = ToolAgent(confirmation_manager=None, risk_assessor=assessor)
        result = await agent.execute_tools(
            '{"action": "get_time", "params": {}}'
        )
        assert "Error" not in result

    async def test_plain_text_passes_through(self, assessor):
        agent = ToolAgent(confirmation_manager=None, risk_assessor=assessor)
        text = "Just a normal sentence, no tools here."
        assert await agent.execute_tools(text) == text

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ('{"action": "get_time", "params": {}}', {}),
            ('{"action": "run_code", "language": "python", "code": "x"}',
             {"language": "python", "code": "x"}),
            ('{"action": "open_app", "params": {"app": "spotify"}}',
             {"app": "spotify"}),
        ],
    )
    def test_parse_params_handles_both_shapes(self, raw, expected):
        import json
        import re
        match = re.search(
            r'\{\s*"action"\s*:\s*"([^"]+)"((?:\s*,\s*"[^"]+"\s*:\s*'
            r'(?:"[^"]*"|-?\d+(?:\.\d+)?|true|false|null|\{[^}]*\}|\[[^\]]*\]))*)\s*\}',
            raw,
        )
        assert match
        assert ToolAgent._parse_params(match.group(2)) == expected


class TestAgentServiceGating:
    async def test_raw_json_path_is_gated(self):
        from services.agent_service import AgentService
        from unittest.mock import AsyncMock, MagicMock

        executor = AsyncMock()
        executor.execute = AsyncMock(return_value={"success": True, "message": "ran"})
        service = AgentService(
            router_agent=MagicMock(), fast_agent=MagicMock(), planner_agent=MagicMock(),
            code_agent=MagicMock(), tool_agent=MagicMock(), memory_agent=MagicMock(),
            scheduler_llm_agent=MagicMock(), scheduler_manager=MagicMock(),
            reminder_service=MagicMock(), notification_mgr=MagicMock(),
            tool_executor=executor, os_toolkit=MagicMock(),
        )
        service.tool.confirmation_manager = None

        text = '{"action": "system_command", "command": "rm -rf /"}'
        result = await service._execute_tool_commands(text)

        executor.execute.assert_not_called()
        assert "cancelled" in result.lower()

    async def test_safe_json_path_executes(self):
        from services.agent_service import AgentService
        from unittest.mock import AsyncMock, MagicMock

        executor = AsyncMock()
        executor.execute = AsyncMock(
            return_value={"success": True, "message": "Opened notepad"}
        )
        service = AgentService(
            router_agent=MagicMock(), fast_agent=MagicMock(), planner_agent=MagicMock(),
            code_agent=MagicMock(), tool_agent=MagicMock(), memory_agent=MagicMock(),
            scheduler_llm_agent=MagicMock(), scheduler_manager=MagicMock(),
            reminder_service=MagicMock(), notification_mgr=MagicMock(),
            tool_executor=executor, os_toolkit=MagicMock(),
        )
        service.tool.confirmation_manager = None

        text = '{"action": "open_app", "app": "notepad"}'
        result = await service._execute_tool_commands(text)
        executor.execute.assert_called_once()
        assert "OPEN_APP" in result
