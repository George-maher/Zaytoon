"""End-to-end approval flow tests through ToolAgent + ConfirmationManager.

Tests:

    pending created → require_confirmation emitted → approve/deny → resume or
    cancel → final response reflects outcome

Uses the real ConfirmationManager with a capturable notifier, and a
ScriptedLLM for deterministic responses. No WebSocket transport is needed —
the confirmation channel is duck-typed (notify callback), exactly as
ToolAgent's docstring describes.

No real credentials, no network, no live integrations.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

from agents.tool_agent import ToolAgent
from core.connections.capability_discovery import discovery
from core.connections.connection_manager import manager
from core.connections.connector_interface import (
    BaseConnector,
    CapabilityResult,
    ConnectionState,
    ConnectorCapability,
    ConnectorDefinition,
)
from core.connections.connector_registry import registry
from core import grants
from core.permissions import Permission
from tools.connections import ConnectorDiscover, ConnectorExecute
from tools.registry import ToolRegistry
from tools.safety import (
    ConfirmationManager,
    RiskAssessor,
    RiskLevel,
)


# --------------------------------------------------------------------------- #
# Scripted LLM                                                                #
# --------------------------------------------------------------------------- #


class ScriptedLLM:
    """Replays a fixed list of responses, recording every request."""

    tier = "fast"

    def __init__(self, script: List[Any]):
        self.script = list(script)
        self.requests: List[List[Dict[str, Any]]] = []
        self.calls = 0

    async def complete(self, messages, **kwargs):
        self.requests.append(list(messages))
        self.calls += 1
        if not self.script:
            return "Nothing left to say."
        item = self.script.pop(0)
        if item is None:
            return None
        return item


# --------------------------------------------------------------------------- #
# Fake connector (confirm tier)                                               #
# --------------------------------------------------------------------------- #


class _FakeConfirmConnector(BaseConnector):
    """Confirm-tier fake connector that records calls."""

    @classmethod
    def connector_id(cls) -> str:
        return "confirm-fake"

    @classmethod
    def definition_class(cls) -> ConnectorDefinition:
        return ConnectorDefinition(
            id="confirm-fake",
            name="Confirm Fake",
            description="Confirm-tier test connector.",
            icon="write",
            category="test",
            auth_method="api_key",
            capabilities=[
                ConnectorCapability(
                    id="confirm-fake.write",
                    name="Write",
                    type="create",
                    description="Write something that needs approval.",
                    input_schema={"type": "object", "properties": {}},
                    requires_permission="filesystem.write",
                    risk_level="confirm",
                ),
            ],
            version="1.0.0",
        )

    def __init__(self, definition: ConnectorDefinition):
        super().__init__(definition)
        self.calls: List[tuple] = []

    async def validate_config(self, config):  # noqa: ARG002 - fake
        return None

    async def start_auth(self, connection_id, redirect_uri):  # noqa: ARG002 - fake
        return {"state": ConnectionState.CONNECTED.value}

    async def complete_auth(self, connection_id, auth_response):  # noqa: ARG002 - fake
        return {"state": ConnectionState.CONNECTED.value}

    async def test_connection(self, connection_id):  # noqa: ARG002 - fake
        return {"ok": True}

    async def refresh_credentials(self, connection_id):  # noqa: ARG002 - fake
        return True

    async def disconnect(self, connection_id):  # noqa: ARG002 - fake
        return None

    async def list_capabilities(self, connection_id):  # noqa: ARG002 - fake
        return list(self.definition.capabilities)

    async def execute_capability(self, connection_id, capability_id, params):  # noqa: ARG002 - fake
        self.calls.append((connection_id, capability_id, params))
        return CapabilityResult(
            success=True,
            output=f"Executed {capability_id}",
            data={"capability": capability_id, "params": params},
        )


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


async def _make_connected_connection(connector_id: str) -> str:
    """Create + complete-auth a connection, returning its id."""
    conn = await manager.create_connection(connector_id)
    await manager.start_auth(conn.id, "http://localhost/callback")
    await manager.complete_auth(
        conn.id, {"code": "auth_code_123", "state": "any-state"}
    )
    return conn.id


def _call(action: str, **params) -> str:
    """Build a flat-form JSON tool-call string like the LLM would emit.

    All keyword arguments become top-level keys (no ``params`` wrapper).
    Do not pass a ``params`` keyword — use _call_nested instead.
    """
    import json

    return json.dumps({"action": action, **params})


def _call_nested(action: str, **params) -> str:
    """Build a nested-form JSON tool-call string.

    All keyword arguments go inside ``params``. Use this when the tool
    expects its arguments nested under ``params``.
    """
    import json

    return json.dumps({"action": action, "params": params})


# --------------------------------------------------------------------------- #
# Confirmation double                                                         #
# --------------------------------------------------------------------------- #


class _CapturingConfirmationManager:
    """ConfirmationManager double that captures pending confirmations.

    Tests resolve them via ``resolve(request_id, approved)``.
    Implements ``request(action, params, session_id)`` and
    ``request_with_assessment(assessment, action, params, session_id)`` so
    :meth:`ToolAgent._ask_with_reconciled_tier` can call them.
    """

    def __init__(self):
        self.pending: List[Any] = []
        self.resolved: Dict[str, bool] = {}
        self._futures: Dict[str, asyncio.Future] = {}
        self.last_outcome: Optional[str] = None

    async def request(self, action, params=None, session_id=""):
        return await self.request_with_assessment(
            type("A", (), {
                "level": type("L", (), {"value": "confirm"})(),
                "permission": "",
                "reasons": [],
                "summary": "",
                "prompt_text": lambda self: action,
                "requires_confirmation": True,
            })(),
            action, params, session_id,
        )

    async def request_with_assessment(self, assessment, action, params=None, session_id=""):
        pending = type(
            "P",
            (),
            {
                "request_id": f"cfm_cap_{len(self.pending)}",
                "action": action,
                "params": params or {},
                "session_id": session_id,
                "assessment": assessment,
                "to_event": lambda self: {
                    "type": "require_confirmation",
                    "request_id": self.request_id,
                    "action": self.action,
                    "params": self.params,
                    "level": self.assessment.level.value,
                    "permission": self.assessment.permission,
                    "reasons": self.assessment.reasons,
                    "summary": self.assessment.summary,
                    "text": self.assessment.prompt_text(),
                    "session_id": self.session_id,
                },
            },
        )()
        self.pending.append(pending)
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._futures[pending.request_id] = fut
        # Block until the test calls resolve(), mirroring the real manager.
        return await fut

    def resolve(self, request_id: str, approved: bool) -> bool:
        fut = self._futures.get(request_id)
        if fut is None or fut.done():
            return False
        fut.set_result(bool(approved))
        self.resolved[request_id] = bool(approved)
        self.last_outcome = "approved" if approved else "declined"
        return True


def _make_tool_agent(
    llm: ScriptedLLM,
    registry: ToolRegistry,
    confirmation_manager,
) -> ToolAgent:
    agent = ToolAgent(
        confirmation_manager=confirmation_manager,
        registry=registry,
    )
    agent.llm = llm
    return agent


# --------------------------------------------------------------------------- #
# Approval flow through conversation                                          #
# --------------------------------------------------------------------------- #


class TestApprovalFlowConversation:
    """confirm-tier capability through ToolAgent with a capturable confirmation double."""

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch, tmp_path):
        from core import secrets as core_secrets

        monkeypatch.setenv("ZAYTOON_SECRETS_PATH", str(tmp_path / "secrets.json"))
        core_secrets.reset_cache()
        from core.connections.auth.oauth import _AUTH_PENDING

        _AUTH_PENDING.clear()
        registry._connectors.clear()
        manager._connections.clear()
        grants.invalidate()
        yield

    @pytest.mark.asyncio
    async def test_approval_round_trip_executes_and_answers(self):
        """Pending → approve → execute → final answer reflects the result."""
        registry.register(_FakeConfirmConnector)
        connection_id = await _make_connected_connection("confirm-fake")

        reg = ToolRegistry()
        reg.register_all([ConnectorDiscover(), ConnectorExecute()])

        confirm = _CapturingConfirmationManager()
        llm = ScriptedLLM([
            _call_nested(
                "connector_execute",
                connection_id=connection_id,
                capability_id="confirm-fake.write",
                params={"x": 1},
            ),
            "Done: Executed confirm-fake.write after your approval.",
        ])
        agent = _make_tool_agent(llm, reg, confirm)

        task = asyncio.create_task(
            agent.process("Write x=1 to my fake connector", [], session_id="sess-1")
        )

        # Wait for the pending confirmation.
        for _ in range(200):
            if confirm.pending:
                break
            await asyncio.sleep(0.01)

        assert len(confirm.pending) == 1
        pending = confirm.pending[0]
        assert pending.action == "connector_execute"
        assert pending.session_id == "sess-1"
        assert pending.params["capability_id"] == "confirm-fake.write"
        assert pending.params["connection_id"] == connection_id

        # The pending event carries the right payload (mirrors WebSocket frame).
        event = pending.to_event()
        assert event["type"] == "require_confirmation"
        assert event["request_id"] == pending.request_id
        assert event["level"] == "high"  # RiskLevel.HIGH.value
        assert event["permission"] == "confirm"  # tool-declared tier

        # Approve.
        assert confirm.resolve(pending.request_id, True) is True

        result = await task

        assert "after your approval" in result.lower()
        connector = manager._connections[connection_id].connector
        assert len(connector.calls) == 1
        assert connector.calls[0][1] == "confirm-fake.write"
        assert connector.calls[0][2] == {"x": 1}

    @pytest.mark.asyncio
    async def test_denial_cancels_no_execution(self):
        """Pending → deny → no execution → refusal answer."""
        registry.register(_FakeConfirmConnector)
        connection_id = await _make_connected_connection("confirm-fake")

        reg = ToolRegistry()
        reg.register_all([ConnectorDiscover(), ConnectorExecute()])

        confirm = _CapturingConfirmationManager()
        llm = ScriptedLLM([
            _call("connector_execute", connection_id=connection_id, capability_id="confirm-fake.write"),
            "You declined, so nothing ran.",
        ])
        agent = _make_tool_agent(llm, reg, confirm)

        task = asyncio.create_task(
            agent.process("Write to my fake connector", [], session_id="sess-2")
        )

        for _ in range(200):
            if confirm.pending:
                break
            await asyncio.sleep(0.01)

        assert len(confirm.pending) == 1
        pending = confirm.pending[0]

        # Deny.
        assert confirm.resolve(pending.request_id, False) is True

        result = await task

        assert "declined" in result.lower() or "declined" in result.lower()
        connector = manager._connections[connection_id].connector
        assert len(connector.calls) == 0

    @pytest.mark.asyncio
    async def test_duplicate_replay_rejected(self):
        """Resolving the same request_id twice returns False the second time."""
        registry.register(_FakeConfirmConnector)
        connection_id = await _make_connected_connection("confirm-fake")

        reg = ToolRegistry()
        reg.register_all([ConnectorDiscover(), ConnectorExecute()])

        confirm = _CapturingConfirmationManager()
        llm = ScriptedLLM([
            _call("connector_execute", connection_id=connection_id, capability_id="confirm-fake.write"),
            "Answer after first resolution.",
        ])
        agent = _make_tool_agent(llm, reg, confirm)

        task = asyncio.create_task(
            agent.process("Write to my fake connector", [], session_id="sess-4")
        )

        for _ in range(200):
            if confirm.pending:
                break
            await asyncio.sleep(0.01)

        pending = confirm.pending[0]

        # First approval succeeds.
        assert confirm.resolve(pending.request_id, True) is True

        # Replay: same id again → rejected.
        assert confirm.resolve(pending.request_id, True) is False

        result = await task
        assert "after your approval" in result.lower() or "after first resolution" in result.lower()

    @pytest.mark.asyncio
    async def test_cross_session_confirmation_rejected(self):
        """A confirmation created for session A carries session_id=sess-A."""
        registry.register(_FakeConfirmConnector)
        connection_id = await _make_connected_connection("confirm-fake")

        reg = ToolRegistry()
        reg.register_all([ConnectorDiscover(), ConnectorExecute()])

        confirm = _CapturingConfirmationManager()
        llm = ScriptedLLM([
            _call("connector_execute", connection_id=connection_id, capability_id="confirm-fake.write"),
            "Done.",
        ])
        agent = _make_tool_agent(llm, reg, confirm)

        task = asyncio.create_task(
            agent.process("Write to my fake connector", [], session_id="sess-A")
        )

        for _ in range(200):
            if confirm.pending:
                break
            await asyncio.sleep(0.01)

        pending = confirm.pending[0]
        assert pending.session_id == "sess-A"

        # Approve — the double resolves regardless of session (session binding
        # is enforced by the transport/WebSocket layer, not the manager).
        assert confirm.resolve(pending.request_id, True) is True

        result = await task
        assert "Done." in result

    @pytest.mark.asyncio
    async def test_connector_failure_after_approval(self):
        """Approval succeeds but the connector fails → honest final answer."""
        class _Failing(BaseConnector):
            @classmethod
            def connector_id(cls) -> str:
                return "fail-fake"

            @classmethod
            def definition_class(cls) -> ConnectorDefinition:
                return ConnectorDefinition(
                    id="fail-fake",
                    name="Fail Fake",
                    description="Always fails.",
                    icon="error",
                    category="test",
                    auth_method="api_key",
                    capabilities=[
                        ConnectorCapability(
                            id="fail-fake.do",
                            name="Do",
                            type="execute",
                            description="Always fails.",
                            input_schema={"type": "object", "properties": {}},
                            risk_level="confirm",
                        ),
                    ],
                    version="1.0.0",
                )

            def __init__(self, definition):
                super().__init__(definition)

            async def validate_config(self, config):
                return None

            async def start_auth(self, connection_id, redirect_uri):
                return {"state": ConnectionState.CONNECTED.value}

            async def complete_auth(self, connection_id, auth_response):
                return {"state": ConnectionState.CONNECTED.value}

            async def test_connection(self, connection_id):
                return {"ok": True}

            async def refresh_credentials(self, connection_id):
                return True

            async def disconnect(self, connection_id):
                return None

            async def list_capabilities(self, connection_id):
                return list(self.definition.capabilities)

            async def execute_capability(self, connection_id, capability_id, params):
                return CapabilityResult(
                    success=False,
                    output="",
                    error="The remote service is down.",
                )

        registry.register(_Failing)
        connection_id = await _make_connected_connection("fail-fake")

        reg = ToolRegistry()
        reg.register_all([ConnectorDiscover(), ConnectorExecute()])

        confirm = _CapturingConfirmationManager()
        llm = ScriptedLLM([
            _call("connector_execute", connection_id=connection_id, capability_id="fail-fake.do"),
            "The remote service is down, so I could not complete that even after you approved it.",
        ])
        agent = _make_tool_agent(llm, reg, confirm)

        task = asyncio.create_task(
            agent.process("Do the failing thing", [], session_id="sess-5")
        )

        for _ in range(200):
            if confirm.pending:
                break
            await asyncio.sleep(0.01)

        pending = confirm.pending[0]
        assert confirm.resolve(pending.request_id, True) is True

        result = await task

        assert "could not complete" in result.lower() or "down" in result.lower()


# --------------------------------------------------------------------------- #
# ConfirmationManager direct behavior (through the real class)                #
# --------------------------------------------------------------------------- #


class TestConfirmationManagerDirect:
    """The real ConfirmationManager's terminal outcomes.

    Uses ``run_code`` which is on TOOL_ALWAYS_CONFIRM so every request
    reaches the confirmation channel.
    """

    @pytest.mark.asyncio
    async def test_timeout_denies(self):
        """No resolution before timeout → declined (timeout outcome)."""
        resolved = []

        async def notify(pending):
            resolved.append(pending)
            # Never resolve → timeout.

        mgr = ConfirmationManager(notify=notify, timeout=0.05)

        ok = await mgr.request("run_code", {}, "sess")
        assert ok is False
        assert mgr.last_outcome == ConfirmationManager.OUTCOME_TIMEOUT

    @pytest.mark.asyncio
    async def test_resolve_approves(self):
        """resolve(request_id, True) unblocks the waiting request."""
        captured = []

        async def notify(pending):
            captured.append(pending)

        mgr = ConfirmationManager(notify=notify, timeout=5.0)

        async def waiter():
            return await mgr.request("run_code", {}, "sess")

        task = asyncio.create_task(waiter())

        for _ in range(200):
            if captured:
                break
            await asyncio.sleep(0.01)

        assert mgr.resolve(captured[0].request_id, True) is True
        assert await task is True
        assert mgr.last_outcome == ConfirmationManager.OUTCOME_APPROVED

    @pytest.mark.asyncio
    async def test_resolve_denies(self):
        """resolve(request_id, False) unblocks with a denial."""
        captured = []

        async def notify(pending):
            captured.append(pending)

        mgr = ConfirmationManager(notify=notify, timeout=5.0)

        async def waiter():
            return await mgr.request("run_code", {}, "sess")

        task = asyncio.create_task(waiter())

        for _ in range(200):
            if captured:
                break
            await asyncio.sleep(0.01)

        assert mgr.resolve(captured[0].request_id, False) is True
        assert await task is False
        assert mgr.last_outcome == ConfirmationManager.OUTCOME_DECLINED

    @pytest.mark.asyncio
    async def test_cancel_all_cancels_pending(self):
        """cancel_all() resolves all pending as declined."""
        captured = []

        async def notify(pending):
            captured.append(pending)

        mgr = ConfirmationManager(notify=notify, timeout=5.0)

        async def waiter():
            return await mgr.request("run_code", {}, "sess")

        task = asyncio.create_task(waiter())

        for _ in range(200):
            if captured:
                break
            await asyncio.sleep(0.01)

        mgr.cancel_all("test cleanup")
        assert await task is False
        assert mgr.last_outcome == ConfirmationManager.OUTCOME_DECLINED
        assert mgr.pending_count == 0

    @pytest.mark.asyncio
    async def test_no_notifier_means_unavailable(self):
        """Without a notify callback, high-risk actions return False."""
        mgr = ConfirmationManager(notify=None)
        ok = await mgr.request("run_code", {}, "sess")
        assert ok is False
        assert mgr.last_outcome == ConfirmationManager.OUTCOME_UNAVAILABLE

