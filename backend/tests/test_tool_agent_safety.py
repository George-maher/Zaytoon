"""ToolAgent integration with the human-in-the-loop sandbox.

Verifies that a high-risk tool call pauses, emits a `require_confirmation`
event, and only executes after the user approves.

The "did the tool actually run?" spies used to monkeypatch
``ToolAgent._toolkit_cmd`` and ``._run_code_tool``. Those were private
duplicates of implementations that already existed in ``ToolExecutor``; they
were deleted when the two stacks were collapsed into
:class:`tools.registry.ToolRegistry`. The property being tested is unchanged —
so the spy now replaces the *registered tool*, which is the real seam.
"""

import pytest

from agents.tool_agent import ToolAgent
from config import config
from tools.base import BaseTool, ToolResult
from tools.safety import ConfirmationManager, RiskAssessor


@pytest.fixture
def assessor():
    return RiskAssessor(always_confirm=[])


class _Spy(BaseTool):
    """Records invocations instead of performing them."""

    permission = "safe"

    def __init__(self, name: str, calls: list, result: str = "done") -> None:
        self._name = name
        self._calls = calls
        self._result = result

    def name(self) -> str:
        return self._name

    def description(self) -> str:
        return "Test double."

    def schema(self):
        return {"type": "object", "properties": {}, "additionalProperties": True}

    async def execute(self, **kwargs):
        self._calls.append(kwargs)
        return ToolResult.ok(self._result)


def spy_tool(agent, calls, tool_name="system_command", result="done", tier="safe"):
    """Swap a registered tool for a recording double.

    Returns a callable that yields the recorded invocations, so a test can
    assert the tool never ran.
    """
    # The spy's tier must match the real tool's, otherwise the gate would be
    # exercised against the wrong classification and the test would prove
    # nothing.
    real = agent.registry.get(tool_name)
    if real is not None:
        tier = real.permission
    agent.registry.register(_Spy(tool_name, calls, result), replace=True)
    agent.registry.get(tool_name).permission = tier
    return lambda: list(calls)


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
        spy_tool(agent, executed, "system_command")

        result = await agent._execute_single_tool(
            "system_command", {"command": "rm -rf /"}
        )
        assert executed == []
        assert "Blocked" in result
        assert manager.last_outcome == ConfirmationManager.OUTCOME_UNAVAILABLE

    async def test_no_manager_at_all_is_reported(self, assessor):
        agent = ToolAgent(confirmation_manager=None, risk_assessor=assessor)
        executed = []
        spy_tool(agent, executed, "system_command")

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
        spy_tool(agent, executed, "system_command")

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
        spy_tool(agent, executed, "system_command")

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
        spy_tool(agent, executed, "system_command")

        result = await agent._execute_single_tool(
            "system_command", {"command": "rm -rf /"}, language="en"
        )

        # The spy records the kwargs the tool actually received.
        assert [c["command"] for c in executed] == ["rm -rf /"]
        assert result == "done"
        assert len(requests) == 1
        assert requests[0].action == "system_command"

    async def test_run_code_blocked_by_default_always_confirm(self):
        # Default assessor: run_code is in TOOL_ALWAYS_CONFIRM.
        agent = ToolAgent(confirmation_manager=None)
        executed = []
        spy_tool(agent, executed, "run_code")

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
        spy_tool(agent, [], "system_command")
        result = await agent._execute_single_tool(
            "system_command", {"command": "rm -rf /"}, language="ar"
        )
        assert "اتلغت" in result

    async def test_arabic_unavailable_message(self, assessor):
        manager = ConfirmationManager(notify=None, timeout=1)
        agent = ToolAgent(confirmation_manager=manager, risk_assessor=assessor)
        spy_tool(agent, [], "system_command")
        result = await agent._execute_single_tool(
            "system_command", {"command": "rm -rf /"}, language="ar"
        )
        assert "متوقف" in result

    async def test_cached_result_cannot_bypass_confirmation(self, assessor):
        manager = ConfirmationManager(notify=None, timeout=1)
        agent = ToolAgent(confirmation_manager=manager, risk_assessor=assessor)
        executed = []
        spy_tool(agent, executed, "system_command")

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
        spy_tool(agent, executed, "system_command")

        result = await agent._execute_single_tool(
            "system_command", {"command": "rm -rf /"}
        )
        assert [c["command"] for c in executed] == ["rm -rf /"]
        assert result == "done"


class TestExecuteToolsIntegration:
    async def test_json_tool_call_is_gated(self, assessor):
        manager = ConfirmationManager(notify=None, timeout=1)
        agent = ToolAgent(confirmation_manager=manager, risk_assessor=assessor)
        executed = []
        spy_tool(agent, executed, "system_command")

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
        spy_tool(agent, executed, "system_command")

        result = await agent.execute_tools(
            '{"action": "system_command", "command": "shutdown /s"}'
        )
        assert executed == []
        assert "Cancelled" in result

    async def test_nested_params_form_is_gated(self, assessor):
        manager = ConfirmationManager(notify=None, timeout=1)
        agent = ToolAgent(confirmation_manager=manager, risk_assessor=assessor)
        executed = []
        spy_tool(agent, executed, "system_command")

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


class TestApprovalCallbackNotices:
    """Every non-approval branch of `_make_approval` must return its notice.

    The four notice dicts (`_DECLINED`, `_TIMEOUT`, `_UNAVAILABLE`,
    `_ERROR`) are **module-level**; the closure once wrote `self._DECLINED`,
    so every branch except "approved" raised `AttributeError` — the engine
    logged `op=approval type=AttributeError` and fed an error string back to
    the model instead of the bilingual refusal. The live E2E decline
    scenario found it; no unit test had ever walked these branches, because
    the WS suite stubs `ToolAgent` entirely and the engine suite supplies
    its own callbacks.
    """

    @staticmethod
    def _manager(outcome=None, raises=None):
        class Manager:
            last_outcome = outcome

            async def request(self, *args, **kwargs):
                if raises is not None:
                    raise raises
                return False

        return Manager()

    async def test_no_manager_returns_the_decline_notice(self, assessor):
        agent = ToolAgent(confirmation_manager=None, risk_assessor=assessor)
        approve = agent._make_approval(None, "sess_1", "en")
        result = await approve("system_command", {"command": "rm -rf /"})
        assert result == "Cancelled: user denied a high-risk action."

    async def test_a_decline_returns_the_decline_notice(self, assessor):
        agent = ToolAgent(confirmation_manager=None, risk_assessor=assessor)
        manager = self._manager(ConfirmationManager.OUTCOME_DECLINED)
        approve = agent._make_approval(manager, "sess_1", "en")
        assert await approve("file_delete", {}) == (
            "Cancelled: user denied a high-risk action."
        )

    async def test_a_timeout_returns_the_timeout_notice(self, assessor):
        agent = ToolAgent(confirmation_manager=None, risk_assessor=assessor)
        manager = self._manager(ConfirmationManager.OUTCOME_TIMEOUT)
        approve = agent._make_approval(manager, "sess_1", "en")
        assert await approve("file_delete", {}) == (
            "Cancelled: no confirmation received in time."
        )

    async def test_no_channel_returns_the_unavailable_notice(self, assessor):
        agent = ToolAgent(confirmation_manager=None, risk_assessor=assessor)
        manager = self._manager(ConfirmationManager.OUTCOME_UNAVAILABLE)
        approve = agent._make_approval(manager, "sess_1", "en")
        assert "no approval channel" in await approve("file_delete", {})

    async def test_a_failed_request_returns_the_error_notice(self, assessor):
        agent = ToolAgent(confirmation_manager=None, risk_assessor=assessor)
        manager = self._manager(raises=RuntimeError("bus is gone"))
        approve = agent._make_approval(manager, "sess_1", "en")
        assert await approve("file_delete", {}) == (
            "Cancelled: the confirmation request could not be sent."
        )

    async def test_an_approval_still_returns_true(self, assessor):
        agent = ToolAgent(confirmation_manager=None, risk_assessor=assessor)

        class Yes:
            async def request(self, *args, **kwargs):
                return True

        approve = agent._make_approval(Yes(), "sess_1", "en")
        assert await approve("file_delete", {}) is True

    async def test_arabic_gets_the_arabic_notice(self, assessor):
        agent = ToolAgent(confirmation_manager=None, risk_assessor=assessor)
        approve = agent._make_approval(None, "sess_1", "ar")
        result = await approve("file_delete", {})
        assert any("\u0600" <= ch <= "\u06FF" for ch in result)
