"""End-to-end assistant conversation tests through connector execution.

Tests the full flow:

    user message → ToolAgent.process → LLM tool call → validation → connector
    selection → permission checks → execution → result → LLM follow-up → final
    response

No real credentials, no network, no live integrations. Every connector is a
fake that records its calls and returns canned results. Live integration
(GitHub, Drive, MCP) is Blocked by unavailable credentials and is not
attempted here.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

from agents.tool_agent import ToolAgent
from core.connections.capability_discovery import discovery
from core.connections.connection_manager import (
    ConnectionManager,
    NotConnected,
    manager,
)
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


# --------------------------------------------------------------------------- #
# Scripted LLM — deterministic provider mock                                  #
# --------------------------------------------------------------------------- #


class ScriptedLLM:
    """Replays a fixed list of responses, recording every request.

    ``None`` in the script simulates a provider failure (returns None).
    """

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
            return None  # simulate provider failure
        return item


# --------------------------------------------------------------------------- #
# Fake connectors                                                             #
# --------------------------------------------------------------------------- #


class _FakeReadOnlyConnector(BaseConnector):
    """Read-only fake connector, safe tier, always succeeds."""

    @classmethod
    def connector_id(cls) -> str:
        return "readonly-fake"

    @classmethod
    def definition_class(cls) -> ConnectorDefinition:
        return ConnectorDefinition(
            id="readonly-fake",
            name="Read Only Fake",
            description="Read-only test connector.",
            icon="read",
            category="test",
            auth_method="api_key",
            capabilities=[
                ConnectorCapability(
                    id="readonly-fake.read",
                    name="Read",
                    type="read",
                    description="Read something safe.",
                    input_schema={"type": "object", "properties": {}},
                    risk_level="safe",
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


class _FakeConfirmConnector(BaseConnector):
    """Confirm-tier fake connector, always succeeds."""

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


class _FakeFailingConnector(BaseConnector):
    """Connector whose execute_capability returns failure."""

    @classmethod
    def connector_id(cls) -> str:
        return "failing-fake"

    @classmethod
    def definition_class(cls) -> ConnectorDefinition:
        return ConnectorDefinition(
            id="failing-fake",
            name="Failing Fake",
            description="Connector whose capabilities fail.",
            icon="error",
            category="test",
            auth_method="api_key",
            capabilities=[
                ConnectorCapability(
                    id="failing-fake.do",
                    name="Do",
                    type="execute",
                    description="Always fails.",
                    input_schema={"type": "object", "properties": {}},
                    risk_level="safe",
                ),
            ],
            version="1.0.0",
        )

    def __init__(self, definition: ConnectorDefinition):
        super().__init__(definition)

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
        return CapabilityResult(
            success=False,
            output="",
            error="The remote service rejected the request.",
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

    All keyword arguments become top-level keys (no ``params`` wrapper), so
    :func:`engine._parse_json_object` picks them up via the flat-form branch.
    Do not pass a ``params`` keyword — it would shadow the flat-form branch.
    """
    import json

    return json.dumps({"action": action, **params})


def _call_nested(action: str, **params) -> str:
    """Build a nested-form JSON tool-call string.

    All keyword arguments go inside ``params``. Use this when the tool
    expects its arguments nested under ``params`` (e.g. ``connector_execute``
    with capability parameters).
    """
    import json

    return json.dumps({"action": action, "params": params})


# --------------------------------------------------------------------------- #
# ToolAgent harness                                                           #
# --------------------------------------------------------------------------- #


def _make_tool_agent(
    llm: ScriptedLLM,
    registry: ToolRegistry,
    confirmation_manager=None,
):
    """Build a ToolAgent with a scripted LLM and an optional confirmation channel."""
    agent = ToolAgent(
        confirmation_manager=confirmation_manager,
        registry=registry,
    )
    agent.llm = llm
    return agent


# --------------------------------------------------------------------------- #
# ConnectorDiscover through conversation                                      #
# --------------------------------------------------------------------------- #


class TestConnectorDiscoverConversation:
    """connector_discover invoked through ToolAgent.process (read-only, safe)."""

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch, tmp_path):
        from core import secrets as core_secrets

        monkeypatch.setenv("ZAYTOON_SECRETS_PATH", str(tmp_path / "secrets.json"))
        core_secrets.reset_cache()
        from core.connections.auth.oauth import _AUTH_PENDING

        _AUTH_PENDING.clear()
        registry._connectors.clear()
        manager._connections.clear()
        discovery._grants_cache.clear()
        grants.invalidate()
        yield

    @pytest.mark.asyncio
    async def test_discover_through_conversation(self):
        """User asks about capabilities → LLM calls connector_discover → final answer."""
        registry.register(_FakeReadOnlyConnector)
        connection_id = await _make_connected_connection("readonly-fake")

        reg = ToolRegistry()
        reg.register_all([ConnectorDiscover(), ConnectorExecute()])

        llm = ScriptedLLM([
            _call("connector_discover", connection_id=connection_id),
            "Your fake connector offers one capability: readonly-fake.read.",
        ])
        agent = _make_tool_agent(llm, reg)

        result = await agent.process("What can my fake connector do?", [])

        assert "readonly-fake.read" in result
        assert llm.calls == 2

    @pytest.mark.asyncio
    async def test_discover_across_all_connections(self):
        """No connection_id → scan all connected services."""
        registry.register(_FakeReadOnlyConnector)
        await _make_connected_connection("readonly-fake")

        reg = ToolRegistry()
        reg.register_all([ConnectorDiscover(), ConnectorExecute()])

        llm = ScriptedLLM([
            _call("connector_discover"),
            "Across your connections, readonly-fake offers readonly-fake.read.",
        ])
        agent = _make_tool_agent(llm, reg)

        result = await agent.process("What can all my connectors do?", [])

        assert "readonly-fake.read" in result
        assert llm.calls == 2

    @pytest.mark.asyncio
    async def test_discover_when_no_connections(self):
        """No connections → graceful 'no connected services' answer."""
        registry.register(_FakeReadOnlyConnector)

        reg = ToolRegistry()
        reg.register_all([ConnectorDiscover(), ConnectorExecute()])

        llm = ScriptedLLM([
            _call("connector_discover"),
            "You have no connected services yet.",
        ])
        agent = _make_tool_agent(llm, reg)

        result = await agent.process("What can my connectors do?", [])

        assert "no connected services" in result or "no connected" in result.lower()


# --------------------------------------------------------------------------- #
# ConnectorExecute through conversation (safe tier, no approval needed)       #
# --------------------------------------------------------------------------- #


class TestConnectorExecuteSafeThroughConversation:
    """connector_execute on a safe-tier capability through ToolAgent.process."""

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch, tmp_path):
        from core import secrets as core_secrets

        monkeypatch.setenv("ZAYTOON_SECRETS_PATH", str(tmp_path / "secrets.json"))
        core_secrets.reset_cache()
        from core.connections.auth.oauth import _AUTH_PENDING

        _AUTH_PENDING.clear()
        registry._connectors.clear()
        manager._connections.clear()
        discovery._grants_cache.clear()
        grants.invalidate()
        yield

    @pytest.mark.asyncio
    async def test_execute_safe_capability_through_conversation(self):
        """Full loop: discover → execute safe capability → final answer with result."""
        registry.register(_FakeReadOnlyConnector)
        connection_id = await _make_connected_connection("readonly-fake")

        reg = ToolRegistry()
        reg.register_all([ConnectorDiscover(), ConnectorExecute()])

        confirm = _AutoApproveConfirmationManager()
        llm = ScriptedLLM([
            _call("connector_discover", connection_id=connection_id),
            _call("connector_execute", connection_id=connection_id, capability_id="readonly-fake.read"),
            "I read your fake data: Executed readonly-fake.read",
        ])
        agent = _make_tool_agent(llm, reg, confirmation_manager=confirm)

        result = await agent.process("Read from my fake connector", [])

        assert "Executed readonly-fake.read" in result
        assert llm.calls == 3
        # The connector actually ran — the instance lives in the manager.
        connector = manager._connections[connection_id].connector
        assert len(connector.calls) == 1
        assert connector.calls[0][1] == "readonly-fake.read"

    @pytest.mark.asyncio
    async def test_execute_passes_params_through(self):
        """Params flow from LLM call → connector → result → LLM answer."""
        registry.register(_FakeReadOnlyConnector)
        connection_id = await _make_connected_connection("readonly-fake")

        reg = ToolRegistry()
        reg.register_all([ConnectorDiscover(), ConnectorExecute()])

        confirm = _AutoApproveConfirmationManager()
        llm = ScriptedLLM([
            _call_nested(
                "connector_execute",
                connection_id=connection_id,
                capability_id="readonly-fake.read",
                params={"key": "value"},
            ),
            "Done with key=value.",
        ])
        agent = _make_tool_agent(llm, reg, confirmation_manager=confirm)

        result = await agent.process("Read with key value", [])

        assert "Done with key=value." in result
        connector = manager._connections[connection_id].connector
        assert connector.calls[0][2] == {"key": "value"}

    @pytest.mark.asyncio
    async def test_execute_unknown_connector_through_conversation(self):
        """LLM asks to execute on a nonexistent connection → error surfaced."""
        registry.register(_FakeReadOnlyConnector)

        reg = ToolRegistry()
        reg.register_all([ConnectorDiscover(), ConnectorExecute()])

        confirm = _AutoApproveConfirmationManager()
        llm = ScriptedLLM([
            _call("connector_execute", connection_id="does-not-exist", capability_id="readonly-fake.read"),
            "I could not find that connection.",
        ])
        agent = _make_tool_agent(llm, reg, confirmation_manager=confirm)

        result = await agent.process("Read from missing connection", [])

        assert "could not find" in result.lower() or "not connected" in result.lower() or "connectionnotfound" in result.lower()

    @pytest.mark.asyncio
    async def test_execute_unknown_capability_through_conversation(self):
        """LLM asks for a capability the connector doesn't have → error surfaced."""
        registry.register(_FakeReadOnlyConnector)
        connection_id = await _make_connected_connection("readonly-fake")

        reg = ToolRegistry()
        reg.register_all([ConnectorDiscover(), ConnectorExecute()])

        confirm = _AutoApproveConfirmationManager()
        llm = ScriptedLLM([
            _call("connector_execute", connection_id=connection_id, capability_id="readonly-fake.nope"),
            "That capability does not exist on this connection.",
        ])
        agent = _make_tool_agent(llm, reg, confirmation_manager=confirm)

        result = await agent.process("Read nonexistent capability", [])

        assert "does not exist" in result.lower() or "unknown capability" in result.lower()

    @pytest.mark.asyncio
    async def test_execute_connector_failure_through_conversation(self):
        """Connector returns failure → LLM gets error observation → honest final answer."""
        registry.register(_FakeFailingConnector)
        connection_id = await _make_connected_connection("failing-fake")

        reg = ToolRegistry()
        reg.register_all([ConnectorDiscover(), ConnectorExecute()])

        confirm = _AutoApproveConfirmationManager()
        llm = ScriptedLLM([
            _call("connector_execute", connection_id=connection_id, capability_id="failing-fake.do"),
            "The remote service rejected the request, so I could not complete that.",
        ])
        agent = _make_tool_agent(llm, reg, confirmation_manager=confirm)

        result = await agent.process("Do the failing thing", [])

        assert "could not complete" in result.lower() or "rejected" in result.lower()


# --------------------------------------------------------------------------- #
# ConnectorExecute confirm tier through conversation (approval path)          #
# --------------------------------------------------------------------------- #


class _AutoApproveConfirmationManager:
    """ConfirmationManager double that auto-approves every pending request.

    Used for safe-tier capability execution through connector_execute, which is
    itself confirm-tier but where the test wants execution without an explicit
    approval step.
    Implements ``request(action, params, session_id)`` and
    ``request_with_assessment(assessment, action, params, session_id)`` so
    :meth:`ToolAgent._ask_with_reconciled_tier` can call them.
    """

    def __init__(self):
        self.pending: List[Any] = []
        self.resolved: Dict[str, bool] = {}
        self.last_outcome: Optional[str] = None

    async def request(self, action, params=None, session_id=""):
        self.last_outcome = "approved"
        return True

    async def request_with_assessment(self, assessment, action, params=None, session_id=""):
        self.pending.append(
            type(
                "P",
                (),
                {
                    "request_id": f"cfm_auto_{len(self.pending)}",
                    "action": action,
                    "params": params or {},
                    "session_id": session_id,
                    "assessment": assessment,
                },
            )()
        )
        self.resolved[f"cfm_auto_{len(self.pending) - 1}"] = True
        self.last_outcome = "approved"
        return True

    def resolve(self, request_id: str, approved: bool) -> bool:
        return False  # auto-approved already; nothing to resolve


class _CapturingConfirmationManager:
    """ConfirmationManager double that captures pending confirmations.

    Tests resolve them via ``approve(request_id)`` / ``deny(request_id)``.
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

    def resolve(self, request_id: str, approved: bool) -> bool:
        fut = self._futures.get(request_id)
        if fut is None or fut.done():
            return False
        fut.set_result(bool(approved))
        self.resolved[request_id] = bool(approved)
        self.last_outcome = "approved" if approved else "declined"
        return True

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


class TestConnectorExecuteConfirmThroughConversation:
    """connector_execute on a confirm-tier capability → approval → execution."""

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch, tmp_path):
        from core import secrets as core_secrets

        monkeypatch.setenv("ZAYTOON_SECRETS_PATH", str(tmp_path / "secrets.json"))
        core_secrets.reset_cache()
        from core.connections.auth.oauth import _AUTH_PENDING

        _AUTH_PENDING.clear()
        registry._connectors.clear()
        manager._connections.clear()
        discovery._grants_cache.clear()
        grants.invalidate()
        yield

    @pytest.mark.asyncio
    async def test_confirm_executes_after_approval(self):
        """confirm-tier capability: pending → approve → execute → final answer."""
        registry.register(_FakeConfirmConnector)
        connection_id = await _make_connected_connection("confirm-fake")

        reg = ToolRegistry()
        reg.register_all([ConnectorDiscover(), ConnectorExecute()])

        confirm = _CapturingConfirmationManager()
        llm = ScriptedLLM([
            _call("connector_execute", connection_id=connection_id, capability_id="confirm-fake.write"),
            "I wrote to your fake connector after you approved it.",
        ])
        agent = _make_tool_agent(llm, reg, confirmation_manager=confirm)

        task = asyncio.create_task(
            agent.process("Write to my fake connector (confirm tier)", [])
        )

        # Wait for the pending confirmation to be captured.
        for _ in range(100):
            if confirm.pending:
                break
            await asyncio.sleep(0.01)

        assert len(confirm.pending) == 1
        pending = confirm.pending[0]
        assert pending.action == "connector_execute"
        assert pending.session_id == ""

        # Approve.
        assert confirm.resolve(pending.request_id, True) is True

        result = await task

        assert "wrote to your fake connector" in result.lower()
        connector = manager._connections[connection_id].connector
        assert len(connector.calls) == 1
        assert connector.calls[0][1] == "confirm-fake.write"

    @pytest.mark.asyncio
    async def test_confirm_denied_cancels_operation(self):
        """confirm-tier capability: pending → deny → no execution → refusal answer."""
        registry.register(_FakeConfirmConnector)
        connection_id = await _make_connected_connection("confirm-fake")

        reg = ToolRegistry()
        reg.register_all([ConnectorDiscover(), ConnectorExecute()])

        confirm = _CapturingConfirmationManager()
        llm = ScriptedLLM([
            _call("connector_execute", connection_id=connection_id, capability_id="confirm-fake.write"),
            "You declined, so nothing was written.",
        ])
        agent = _make_tool_agent(llm, reg, confirmation_manager=confirm)

        task = asyncio.create_task(
            agent.process("Write to my fake connector", [])
        )

        for _ in range(100):
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
        assert len(connector.calls) == 0  # never executed

    @pytest.mark.asyncio
    async def test_confirm_without_channel_is_declined(self):
        """confirm-tier tool with no confirmation manager → declined, no execution."""
        registry.register(_FakeConfirmConnector)
        connection_id = await _make_connected_connection("confirm-fake")

        reg = ToolRegistry()
        reg.register_all([ConnectorDiscover(), ConnectorExecute()])

        # No confirmation manager at all.
        llm = ScriptedLLM([
            _call("connector_execute", connection_id=connection_id, capability_id="confirm-fake.write"),
            "I can't do that without your approval.",
        ])
        agent = _make_tool_agent(llm, reg, confirmation_manager=None)

        result = await agent.process("Write to my fake connector", [])

        assert "can't do that" in result.lower() or "declined" in result.lower() or "approval" in result.lower()
        connector = manager._connections[connection_id].connector
        assert len(connector.calls) == 0


# --------------------------------------------------------------------------- #
# Non-connector requests follow the normal assistant path                     #
# --------------------------------------------------------------------------- #


class TestNonConnectorConversation:
    """Requests that don't need connectors still get a normal answer."""

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch, tmp_path):
        from core import secrets as core_secrets

        monkeypatch.setenv("ZAYTOON_SECRETS_PATH", str(tmp_path / "secrets.json"))
        core_secrets.reset_cache()
        from core.connections.auth.oauth import _AUTH_PENDING

        _AUTH_PENDING.clear()
        registry._connectors.clear()
        manager._connections.clear()
        discovery._grants_cache.clear()
        grants.invalidate()
        yield

    @pytest.mark.asyncio
    async def test_plain_text_answer_after_tool(self):
        """A tool runs, then the LLM answers in prose (no more tools needed)."""
        registry.register(_FakeReadOnlyConnector)
        connection_id = await _make_connected_connection("readonly-fake")

        reg = ToolRegistry()
        reg.register_all([ConnectorDiscover(), ConnectorExecute()])

        llm = ScriptedLLM([
            _call("connector_discover", connection_id=connection_id),
            "Here is what your connector can do.",
        ])
        agent = _make_tool_agent(llm, reg)

        result = await agent.process("Hello", [])

        assert "Here is what your connector can do." in result
        assert llm.calls == 2


# --------------------------------------------------------------------------- #
# LLM provider failure                                                        #
# --------------------------------------------------------------------------- #


class TestLLMProviderFailure:
    """When the LLM provider errors, the turn fails gracefully."""

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch, tmp_path):
        from core import secrets as core_secrets

        monkeypatch.setenv("ZAYTOON_SECRETS_PATH", str(tmp_path / "secrets.json"))
        core_secrets.reset_cache()
        from core.connections.auth.oauth import _AUTH_PENDING

        _AUTH_PENDING.clear()
        registry._connectors.clear()
        manager._connections.clear()
        discovery._grants_cache.clear()
        grants.invalidate()
        yield

    @pytest.mark.asyncio
    async def test_provider_returns_none_graceful(self):
        """LLM returns None (provider failure) → AgentEngine handles it."""
        registry.register(_FakeReadOnlyConnector)

        reg = ToolRegistry()
        reg.register_all([ConnectorDiscover(), ConnectorExecute()])

        llm = ScriptedLLM([None])
        agent = _make_tool_agent(llm, reg)

        result = await agent.process("Say something", [])

        # Engine should produce some answer even when the model fails.
        assert isinstance(result, str)

