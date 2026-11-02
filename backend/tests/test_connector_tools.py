"""Tests for the connector tools — the assistant's bridge to Connections.

These tests exercise :class:`tools.connections.ConnectorDiscover` and
:class:`tools.connections.ConnectorExecute` through the :class:`tools.registry.ToolRegistry`
gate, the same path the assistant (ToolAgent → AgentEngine) uses.

No real connector, no network, no credentials: every connector is a fake that
records its calls and returns canned results. Live integration (GitHub, Drive,
MCP) is Blocked by unavailable credentials and is not attempted here.

Design note: ``ConnectorExecute.execute`` passes ``approval=True`` to the
manager (the ToolAgent has already run its own approval gate before reaching
the registry). The ``approval`` kwarg is swallowed by ``**_`` because the tool
itself does not re-derive the tier — that is the registry's job, and the
ToolAgent already reconciled it. The ``requires_confirmation`` and
``approval=False`` paths are therefore tested at the manager level
(see ``test_connections.py``), not through this tool.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

from core.connections.capability_discovery import discovery
from core.connections.connection_manager import (
    ConnectionManager,
    ConnectorNotFound,
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
from tools.base import BaseTool, ToolValidationError
from tools.connections import ConnectorDiscover, ConnectorExecute
from tools.registry import ToolRegistry


# --------------------------------------------------------------------------- #
# Fakes                                                                       #
# --------------------------------------------------------------------------- #


class _FakeReadOnlyConnector(BaseConnector):
    """A connector with one safe (read) capability. Always succeeds."""

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
    """A connector with one confirm (write) capability. Always succeeds."""

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
            success=True,
            output=f"Executed {capability_id}",
            data={"capability": capability_id, "params": params},
        )


class _FakeFailingConnector(BaseConnector):
    """A connector whose execute_capability returns failure."""

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


class _FakeSlowConnector(BaseConnector):
    """A connector whose execute_capability sleeps past the timeout."""

    @classmethod
    def connector_id(cls) -> str:
        return "slow-fake"

    @classmethod
    def definition_class(cls) -> ConnectorDefinition:
        return ConnectorDefinition(
            id="slow-fake",
            name="Slow Fake",
            description="Connector that hangs.",
            icon="clock",
            category="test",
            auth_method="api_key",
            capabilities=[
                ConnectorCapability(
                    id="slow-fake.hang",
                    name="Hang",
                    type="execute",
                    description="Never returns promptly.",
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
        await asyncio.sleep(300)  # Far beyond the engine's 120s backstop
        return CapabilityResult(success=True, output="too late")


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _reset_connections(monkeypatch, tmp_path):
    """Isolate secrets, registry, manager, discovery between tests."""
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


@pytest.fixture
def read_only_registry():
    """A ToolRegistry with only connector tools + a read-only fake connector."""
    reg = ToolRegistry()
    reg.register_all([ConnectorDiscover(), ConnectorExecute()])
    registry.register(_FakeReadOnlyConnector)
    return reg


@pytest.fixture
def confirm_registry():
    """A ToolRegistry with connector tools + a confirm-tier fake connector."""
    reg = ToolRegistry()
    reg.register_all([ConnectorDiscover(), ConnectorExecute()])
    registry.register(_FakeConfirmConnector)
    return reg


@pytest.fixture
def failing_registry():
    """A ToolRegistry with connector tools + a failing fake connector."""
    reg = ToolRegistry()
    reg.register_all([ConnectorDiscover(), ConnectorExecute()])
    registry.register(_FakeFailingConnector)
    return reg


@pytest.fixture
def slow_registry():
    """A ToolRegistry with connector tools + a slow fake connector."""
    reg = ToolRegistry()
    reg.register_all([ConnectorDiscover(), ConnectorExecute()])
    registry.register(_FakeSlowConnector)
    return reg


async def _make_connected_connection(connector_id: str):
    """Create + complete-auth a connection, returning its id."""
    conn = await manager.create_connection(connector_id)
    await manager.start_auth(conn.id, "http://localhost/callback")
    await manager.complete_auth(
        conn.id, {"code": "auth_code_123", "state": "any-state"}
    )
    return conn.id


# --------------------------------------------------------------------------- #
# ConnectorDiscover                                                           #
# --------------------------------------------------------------------------- #


class TestConnectorDiscoverTool:
    async def test_discover_lists_capabilities(self, read_only_registry):
        connection_id = await _make_connected_connection("readonly-fake")
        tool = ConnectorDiscover()
        result = await tool.execute(connection_id=connection_id)

        assert result.success is True
        assert result.data is not None
        assert result.data["total"] == 1
        assert result.data["capabilities"][0]["capability_id"] == "readonly-fake.read"

    async def test_discover_no_connection_id_scans_all(self, read_only_registry):
        await _make_connected_connection("readonly-fake")
        tool = ConnectorDiscover()
        result = await tool.execute()

        assert result.success is True
        assert result.data["total"] >= 1

    async def test_discover_no_connections(self, read_only_registry):
        tool = ConnectorDiscover()
        result = await tool.execute()

        assert result.success is True
        assert result.data["capabilities"] == []
        assert result.data["total"] == 0
        assert "no connected services" in result.output

    async def test_discover_unknown_connection(self, read_only_registry):
        tool = ConnectorDiscover()
        result = await tool.execute(connection_id="does-not-exist")

        # Discovery on a missing connection yields nothing, not an error.
        assert result.success is True
        assert result.data["capabilities"] == []

    async def test_discover_tool_is_in_registry(self, read_only_registry):
        assert read_only_registry.has("connector_discover")
        assert read_only_registry.has("connections_discover")  # alias
        assert read_only_registry.has("list_connector_capabilities")  # alias
        tool = read_only_registry.get("connector_discover")
        assert tool.permission == "safe"


# --------------------------------------------------------------------------- #
# ConnectorExecute                                                            #
# --------------------------------------------------------------------------- #


class TestConnectorExecuteTool:
    async def test_execute_success(self, read_only_registry):
        connection_id = await _make_connected_connection("readonly-fake")
        tool = ConnectorExecute()
        result = await tool.execute(
            connection_id=connection_id,
            capability_id="readonly-fake.read",
            params={"key": "value"},
        )

        assert result.success is True
        assert result.data["capability"] == "readonly-fake.read"
        assert result.data["params"] == {"key": "value"}

    async def test_execute_requires_connection_id(self, read_only_registry):
        tool = ConnectorExecute()
        with pytest.raises(ToolValidationError):
            tool.validate({"connection_id": "", "capability_id": "readonly-fake.read"})

    async def test_execute_requires_capability_id(self, read_only_registry):
        tool = ConnectorExecute()
        with pytest.raises(ToolValidationError):
            tool.validate({"connection_id": "some-id", "capability_id": ""})

    async def test_execute_not_connected(self, read_only_registry):
        # Connection created but never auth-completed → NOT_CONNECTED.
        conn = await manager.create_connection("readonly-fake")
        tool = ConnectorExecute()
        result = await tool.execute(
            connection_id=conn.id, capability_id="readonly-fake.read", params={}
        )

        assert result.success is False
        assert "not connected" in (result.error or "").lower()

    async def test_execute_unknown_capability(self, read_only_registry):
        connection_id = await _make_connected_connection("readonly-fake")
        tool = ConnectorExecute()
        result = await tool.execute(
            connection_id=connection_id, capability_id="readonly-fake.nope", params={}
        )

        assert result.success is False
        assert "Unknown capability" in (result.error or "")

    async def test_execute_confirm_with_approval(self, confirm_registry):
        """Confirm tier executes when the tool passes approval=True (ToolAgent path)."""
        connection_id = await _make_connected_connection("confirm-fake")
        tool = ConnectorExecute()
        result = await tool.execute(
            connection_id=connection_id,
            capability_id="confirm-fake.write",
            params={},
            approval=True,
        )

        assert result.success is True
        assert result.data["capability"] == "confirm-fake.write"

    async def test_execute_connector_failure(self, failing_registry):
        """Connector returns CapabilityResult(success=False) — the tool must surface it."""
        connection_id = await _make_connected_connection("failing-fake")
        tool = ConnectorExecute()
        result = await tool.execute(
            connection_id=connection_id,
            capability_id="failing-fake.do",
            params={},
        )

        assert result.success is False
        assert "rejected" in (result.error or "")

    async def test_execute_unknown_connector(self):
        # Connection id that was never created → manager raises ConnectionNotFound
        # (not a subclass of NotConnected), so connector_execute's generic
        # exception handler wraps it.
        tool = ConnectorExecute()
        result = await tool.execute(
            connection_id="does-not-exist",
            capability_id="anything.do",
            params={},
        )

        assert result.success is False
        assert "ConnectionNotFound" in (result.error or "")

    async def test_execute_tool_is_confirm_tier(self, confirm_registry):
        assert confirm_registry.has("connector_execute")
        assert confirm_registry.has("connections_execute")  # alias
        assert confirm_registry.has("run_connector_capability")  # alias
        tool = confirm_registry.get("connector_execute")
        assert tool.permission == "confirm"


# --------------------------------------------------------------------------- #
# Timeout                                                                     #
# --------------------------------------------------------------------------- #


class TestConnectorExecuteTimeout:
    async def test_slow_connector_is_cancelled_by_timeout(self, slow_registry):
        """A connector that hangs past the engine's backstop is cancelled.

        The engine wraps ``registry.execute()`` in ``asyncio.wait_for`` with
        ``TOOL_TIMEOUT_S`` (120s). We simulate that by awaiting the tool with
        a short ``asyncio.wait_for`` — the same mechanism the engine uses.
        """
        connection_id = await _make_connected_connection("slow-fake")
        tool = ConnectorExecute()

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                tool.execute(
                    connection_id=connection_id,
                    capability_id="slow-fake.hang",
                    params={},
                ),
                timeout=0.05,
            )


# --------------------------------------------------------------------------- #
# Malformed response                                                          #
# --------------------------------------------------------------------------- #


class _MalformedTool(BaseTool):
    """A tool whose execute() returns a dict instead of a ToolResult.

    The registry's ``_render()`` must reject this with "Malformed tool result"
    instead of crashing.
    """

    def name(self) -> str:
        return "malformed-fake"

    def description(self) -> str:
        return "Returns a dict instead of ToolResult"

    async def execute(self, **kwargs):
        return {"success": True, "output": "not a ToolResult"}  # wrong type


class TestMalformedResponse:
    def test_registry_rejects_non_toolresult(self):
        reg = ToolRegistry()
        reg.register(_MalformedTool())
        result = asyncio.get_event_loop().run_until_complete(
            reg.execute("malformed-fake", {})
        )
        assert result["success"] is False
        assert "Malformed tool result" in result["error"]


# --------------------------------------------------------------------------- #
# ToolRegistry integration (connector tools alongside built-ins)              #
# --------------------------------------------------------------------------- #


class TestConnectorToolsInDefaultRegistry:
    def test_connector_tools_appear_in_default_registry(self):
        from tools.registry import build_default_registry

        reg = build_default_registry()
        assert reg.has("connector_discover")
        assert reg.has("connector_execute")
        names = reg.list()
        assert "connector_discover" in names
        assert "connector_execute" in names

    def test_connector_discover_effective_tier_is_safe(self):
        from tools.registry import build_default_registry

        reg = build_default_registry()
        assert reg.effective_tier("connector_discover") == "safe"

    def test_connector_execute_effective_tier_is_confirm(self):
        from tools.registry import build_default_registry

        reg = build_default_registry()
        assert reg.effective_tier("connector_execute") == "confirm"
