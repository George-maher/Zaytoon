"""Tests for the Connections platform.

Nothing here touches a real Zaytoon install — secrets go to an isolated temp
file, OAuth flows are exercised against fakes, and no network calls are made.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from core import secrets as core_secrets
from core.connections.auth.api_key import APIKeyConnector
from core.connections.auth.mcp import MCPConnector, MCPServerConfig
from core.connections.auth.oauth import OAuth2Connector, OAuthToken, _AUTH_PENDING
from core.connections.capability_discovery import discovery
from core.connections.connection_manager import (
    ConnectionManager,
    ConnectionNotFound,
    ConnectorNotFound,
    manager,
)
from core.connections.connector_interface import (
    CapabilityResult,
    ConnectionState,
    ConnectorCapability,
    ConnectorDefinition,
)
from core.connections.connector_registry import RegisteredConnector, registry
from core import grants
from core import grants
from core.permissions import Permission, required_for


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class _FakeConnector(OAuth2Connector):
    """OAuth connector that records calls instead of hitting the network."""

    OAUTH_AUTHORIZATION_URL = "https://example.com/oauth/authorize"
    OAUTH_TOKEN_URL = "https://example.com/oauth/token"
    OAUTH_SCOPES = ["read"]
    OAUTH_CLIENT_ID = "fake-client"

    def __init__(self, definition):
        super().__init__(definition)
        self.calls: list = []

    @classmethod
    def connector_id(cls) -> str:
        return "fake"

    @classmethod
    def definition_class(cls):
        return ConnectorDefinition(
            id="fake",
            name="Fake",
            description="A test connector.",
            icon="fake",
            category="test",
            auth_method="oauth2",
            scopes=cls.OAUTH_SCOPES,
            capabilities=[
                ConnectorCapability(
                    id="fake.read",
                    name="Read",
                    type="read",
                    description="Read something.",
                    input_schema={"type": "object", "properties": {}},
                    requires_permission="network.request",
                    risk_level="safe",
                ),
                ConnectorCapability(
                    id="fake.write",
                    name="Write",
                    type="create",
                    description="Write something.",
                    input_schema={"type": "object", "properties": {}},
                    requires_permission="filesystem.write",
                    risk_level="confirm",
                ),
            ],
            version="1.0.0",
        )

    async def _token_exchange(self, client_id, code, code_verifier, redirect_uri):
        self.calls.append(("token_exchange", code, code_verifier, redirect_uri))
        return OAuthToken(
            access_token="at_" + code,
            refresh_token="rt_" + code,
            expires_in=3600,
            scope="read",
        )

    async def _refresh_exchange(self, refresh_token, redirect_uri):
        self.calls.append(("refresh", refresh_token, redirect_uri))
        return OAuthToken(
            access_token="at_refreshed",
            refresh_token=refresh_token,
            expires_in=3600,
            scope="read",
        )

    async def list_capabilities(self, connection_id: str):
        return list(self.definition.capabilities)

    async def execute_capability(self, connection_id, capability_id, params):
        return CapabilityResult(
            success=True,
            output=f"Executed {capability_id}",
            data={"capability": capability_id, "params": params},
        )


# Reset singletons between tests so they do not leak state.
@pytest.fixture(autouse=True)
def _reset_connections(monkeypatch, tmp_path):
    # Isolate secrets.
    monkeypatch.setenv("ZAYTOON_SECRETS_PATH", str(tmp_path / "secrets.json"))
    core_secrets.reset_cache()
    # Clear in-flight OAuth state.
    _AUTH_PENDING.clear()
    # Reset the global registry and manager.
    registry._connectors.clear()
    manager._connections.clear()
    discovery._grants_cache.clear()
    yield


# ---------------------------------------------------------------------------
# Connector registry
# ---------------------------------------------------------------------------
def test_registry_register_and_list():
    registry.register(_FakeConnector)
    entries = registry.list_available()
    assert len(entries) == 1
    assert entries[0].definition.id == "fake"
    assert entries[0].definition.name == "Fake"
    assert entries[0].definition.auth_method == "oauth2"


def test_registry_get_and_has():
    assert not registry.has("fake")
    registry.register(_FakeConnector)
    assert registry.has("fake")
    entry = registry.get("fake")
    assert entry is not None
    assert entry.connector_class is _FakeConnector


def test_registry_instantiate():
    registry.register(_FakeConnector)
    connector = registry.instantiate("fake")
    assert isinstance(connector, _FakeConnector)
    assert connector.connector_id() == "fake"
    # Unknown connector returns None.
    assert registry.instantiate("nope") is None


def test_registry_describe_no_secrets():
    registry.register(_FakeConnector)
    payload = registry.describe()
    assert len(payload) == 1
    assert "scopes" in payload[0]
    assert "capabilities" in payload[0]
    assert "credentials" not in str(payload).lower()


# ---------------------------------------------------------------------------
# OAuth PKCE + state
# ---------------------------------------------------------------------------
def test_pkce_code_verifier_and_challenge():
    from core.connections.auth.oauth import _new_code_verifier, _code_challenge

    v1 = _new_code_verifier()
    v2 = _new_code_verifier()
    assert v1 != v2
    assert 43 <= len(v1) <= 128
    # Challenge is deterministic from the verifier.
    assert _code_challenge(v1) == _code_challenge(v1)
    # Different verifier → different challenge.
    assert _code_challenge(v1) != _code_challenge(v2)


@pytest.mark.asyncio
async def test_oauth_start_auth_returns_url_and_state():
    registry.register(_FakeConnector)
    conn = await manager.create_connection("fake")
    result = await manager.start_auth(conn.id, "http://localhost/callback")
    assert "auth_url" in result
    assert "state" in result
    assert result["connection_id"] == conn.id
    # State was bound server-side.
    assert conn.id in _AUTH_PENDING
    assert _AUTH_PENDING[conn.id]["state"] == result["state"]
    assert "code_challenge" in result["auth_url"]
    assert "code_challenge_method=S256" in result["auth_url"]
    assert "prompt=consent" in result["auth_url"]
    assert "access_type=offline" in result["auth_url"]
    # Connection moved to connecting.
    assert manager.get_connection(conn.id).state == ConnectionState.CONNECTING


@pytest.mark.asyncio
async def test_oauth_complete_auth_stores_token():
    registry.register(_FakeConnector)
    conn = await manager.create_connection("fake")

    # Start auth to bind state.
    result = await manager.start_auth(conn.id, "http://localhost/callback")
    state = result["state"]

    # Complete with matching state + code.
    outcome = await manager.complete_auth(
        conn.id, {"code": "auth_code_123", "state": state}
    )
    assert outcome["state"] == ConnectionState.CONNECTED.value
    connector = registry.instantiate("fake")
    assert connector.has_credentials(conn.id)
    # Pending state cleared.
    assert conn.id not in _AUTH_PENDING
    # Connection is connected.
    assert manager.get_connection(conn.id).state == ConnectionState.CONNECTED


@pytest.mark.asyncio
async def test_oauth_state_mismatch_rejected():
    registry.register(_FakeConnector)
    conn = await manager.create_connection("fake")

    await manager.start_auth(conn.id, "http://localhost/callback")

    outcome = await manager.complete_auth(
        conn.id, {"code": "auth_code_123", "state": "wrong-state"}
    )
    assert outcome["state"] == ConnectionState.ERROR.value
    assert "Invalid authorization state" in outcome["error"]
    connector = registry.instantiate("fake")
    assert not connector.has_credentials(conn.id)


@pytest.mark.asyncio
async def test_oauth_provider_error():
    registry.register(_FakeConnector)
    conn = await manager.create_connection("fake")

    await manager.start_auth(conn.id, "http://localhost/callback")

    outcome = await manager.complete_auth(
        conn.id,
        {
            "error": "access_denied",
            "error_description": "The user denied the request",
            "state": _AUTH_PENDING[conn.id]["state"],
        },
    )
    assert outcome["state"] == ConnectionState.NEEDS_REAUTHORIZATION.value
    assert "user denied" in outcome["error"]


@pytest.mark.asyncio
async def test_oauth_refresh_then_refresh_fails():
    registry.register(_FakeConnector)
    conn = await manager.create_connection("fake")

    await manager.start_auth(conn.id, "http://localhost/callback")
    await manager.complete_auth(
        conn.id, {"code": "auth_code_123", "state": _AUTH_PENDING[conn.id]["state"]}
    )
    connector = registry.instantiate("fake")
    assert await connector.refresh_credentials(conn.id) is True
    assert connector.has_credentials(conn.id)


# ---------------------------------------------------------------------------
# API key connector
# ---------------------------------------------------------------------------
class _FakeAPIKeyConnector(APIKeyConnector):
    AUTH_HEADER = "Authorization"
    AUTH_HEADER_PREFIX = "Bearer"

    @classmethod
    def connector_id(cls) -> str:
        return "apikey-fake"

    @classmethod
    def definition_class(cls):
        return ConnectorDefinition(
            id="apikey-fake",
            name="API Key Fake",
            description="Test.",
            icon="key",
            category="test",
            auth_method="api_key",
            capabilities=[
                ConnectorCapability(
                    id="apikey-fake.read",
                    name="Read",
                    type="read",
                    description="Read.",
                    input_schema={"type": "object", "properties": {}},
                    risk_level="safe",
                ),
            ],
            version="1.0.0",
        )

    async def _test_credential(self, api_key):
        return api_key == "good-key"

    async def list_capabilities(self, connection_id: str):
        return list(self.definition.capabilities)

    async def execute_capability(self, connection_id, capability_id, params):
        return CapabilityResult(
            success=True,
            output=f"Executed {capability_id}",
            data={"capability": capability_id, "params": params},
        )


@pytest.mark.asyncio
async def test_api_key_stored_and_masked():
    registry.register(_FakeAPIKeyConnector)
    conn = await manager.create_connection("apikey-fake")
    await manager.start_auth(conn.id, "http://localhost/callback")

    result = await manager.complete_auth(
        conn.id, {"api_key": "good-key"}
    )
    assert result["state"] == ConnectionState.CONNECTED.value
    connector = registry.instantiate("apikey-fake")
    assert connector.has_credentials(conn.id)
    # Masked rendering shows only last 4.
    assert connector.credential_masked(conn.id) == "…-key"
    # Full value never returned by to_dict.
    d = conn.to_dict()
    assert "good-key" not in str(d)
    assert d["has_credentials"] is True

    # Bad key rejected.
    conn2 = await manager.create_connection("apikey-fake")
    await manager.start_auth(conn2.id, "http://localhost/callback")
    outcome = await manager.complete_auth(conn2.id, {"api_key": "bad-key"})
    assert outcome["state"] == ConnectionState.ERROR.value
    assert not connector.has_credentials(conn2.id)

    # Empty key rejected.
    await manager.start_auth(conn2.id, "http://localhost/callback")
    outcome = await manager.complete_auth(conn2.id, {"api_key": ""})
    assert outcome["state"] == ConnectionState.ERROR.value


# ---------------------------------------------------------------------------
# Connection manager lifecycle
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_create_connection_unknown_connector():
    with pytest.raises(ConnectorNotFound):
        await manager.create_connection("does-not-exist")


@pytest.mark.asyncio
async def test_connection_state_machine():
    registry.register(_FakeConnector)
    conn = await manager.create_connection("fake")
    assert conn.state == ConnectionState.NOT_CONNECTED

    # Start auth → connecting.
    await manager.start_auth(conn.id, "http://localhost/callback")
    stored = manager.get_connection(conn.id)
    assert stored.state == ConnectionState.CONNECTING

    # Complete → connected.
    await manager.complete_auth(
        conn.id,
        {"code": "code", "state": _AUTH_PENDING[conn.id]["state"]},
    )
    stored = manager.get_connection(conn.id)
    assert stored.state == ConnectionState.CONNECTED
    connector = registry.instantiate("fake")
    assert connector.has_credentials(conn.id)

    # Disconnect → not_connected.
    await manager.disconnect(conn.id)
    stored = manager.get_connection(conn.id)
    assert stored.state == ConnectionState.NOT_CONNECTED
    assert stored.credentials_key is None
    assert not connector.has_credentials(conn.id)


@pytest.mark.asyncio
async def test_delete_connection_removes_credentials():
    registry.register(_FakeConnector)
    conn = await manager.create_connection("fake")
    await manager.start_auth(conn.id, "http://localhost/callback")
    await manager.complete_auth(
        conn.id, {"code": "code", "state": _AUTH_PENDING[conn.id]["state"]}
    )
    connector = registry.instantiate("fake")
    assert connector.has_credentials(conn.id)

    await manager.delete_connection(conn.id)
    assert manager.get_connection(conn.id) is None
    assert not connector.has_credentials(conn.id)


@pytest.mark.asyncio
async def test_connect_while_connecting_raises():
    registry.register(_FakeConnector)
    conn = await manager.create_connection("fake")
    await manager.start_auth(conn.id, "http://localhost/callback")
    with pytest.raises(Exception):
        await manager.start_auth(conn.id, "http://localhost/callback")


@pytest.mark.asyncio
async def test_report_status_no_secrets():
    registry.register(_FakeConnector)
    conn = await manager.create_connection("fake")
    status = manager.report_status(conn.id)
    assert status["found"] is True
    assert status["state"] == ConnectionState.NOT_CONNECTED.value
    assert status["has_credentials"] is False
    assert "credentials" not in status


# ---------------------------------------------------------------------------
# Capability execution + permission gate
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_execute_capability_requires_connection():
    from core.connections.connection_manager import NotConnected

    registry.register(_FakeConnector)
    conn = await manager.create_connection("fake")
    with pytest.raises(NotConnected):
        await manager.execute_capability(conn.id, "fake.read", {})


@pytest.mark.asyncio
async def test_execute_capability_unknown_capability():
    registry.register(_FakeConnector)
    conn = await manager.create_connection("fake")
    await manager.start_auth(conn.id, "http://localhost/callback")
    await manager.complete_auth(
        conn.id, {"code": "code", "state": _AUTH_PENDING[conn.id]["state"]}
    )
    result = await manager.execute_capability(conn.id, "fake.nope", {})
    assert result.success is False
    assert "Unknown capability" in result.error


@pytest.mark.asyncio
async def test_execute_capability_confirm_requires_approval():
    registry.register(_FakeConnector)
    conn = await manager.create_connection("fake")
    await manager.start_auth(conn.id, "http://localhost/callback")
    await manager.complete_auth(
        conn.id, {"code": "code", "state": _AUTH_PENDING[conn.id]["state"]}
    )
    # Confirm-tier without approval → requires_confirmation.
    result = await manager.execute_capability(conn.id, "fake.write", {})
    assert result.requires_confirmation is True
    assert result.confirmation_prompt is not None

    # With approval → executes.
    result = await manager.execute_capability(
        conn.id, "fake.write", {}, approval=True
    )
    assert result.success is True

    # With approval=False → cancelled.
    result = await manager.execute_capability(conn.id, "fake.write", {}, approval=False)
    assert result.success is False
    assert result.cancelled is True


# ---------------------------------------------------------------------------
# Missing permissions — grants.denied blocks execution before approval gate
# ---------------------------------------------------------------------------
class _FakePermConnector(OAuth2Connector):
    """Connector with a confirm capability requiring a permission."""

    OAUTH_AUTHORIZATION_URL = "https://example.com/oauth/authorize"
    OAUTH_TOKEN_URL = "https://example.com/oauth/token"
    OAUTH_SCOPES = ["read"]
    OAUTH_CLIENT_ID = "fake-client"

    def __init__(self, definition):
        super().__init__(definition)

    @classmethod
    def connector_id(cls) -> str:
        return "perm-fake"

    @classmethod
    def definition_class(cls):
        return ConnectorDefinition(
            id="perm-fake",
            name="Perm Fake",
            description="Connector with permission-gated capability.",
            icon="perm",
            category="test",
            auth_method="oauth2",
            scopes=cls.OAUTH_SCOPES,
            capabilities=[
                ConnectorCapability(
                    id="perm-fake.write",
                    name="Write",
                    type="create",
                    description="Needs filesystem.write permission.",
                    input_schema={"type": "object", "properties": {}},
                    requires_permission="filesystem.write",
                    risk_level="confirm",
                ),
            ],
            version="1.0.0",
        )

    async def _token_exchange(self, client_id, code, code_verifier, redirect_uri):
        return OAuthToken(
            access_token="at_" + code,
            refresh_token="rt_" + code,
            expires_in=3600,
            scope="read",
        )

    async def _refresh_exchange(self, refresh_token, redirect_uri):
        return OAuthToken(
            access_token="at_refreshed", refresh_token=refresh_token,
            expires_in=3600, scope="read",
        )

    async def list_capabilities(self, connection_id: str):
        return list(self.definition.capabilities)

    async def execute_capability(self, connection_id, capability_id, params):
        return CapabilityResult(
            success=True, output="Executed", data={"capability": capability_id},
        )


@pytest.mark.asyncio
async def test_execute_capability_denied_permission_blocks_before_approval():
    """A denied permission blocks execution without ever asking for approval."""
    registry.register(_FakePermConnector)
    conn = await manager.create_connection("perm-fake")
    await manager.start_auth(conn.id, "http://localhost/callback")
    await manager.complete_auth(
        conn.id, {"code": "code", "state": _AUTH_PENDING[conn.id]["state"]}
    )

    # Deny the permission required by the capability.
    from core import db

    # Insert a deny decision directly into the grants table.
    await db.execute(
        "INSERT OR REPLACE INTO permission_grants (permission, decision) VALUES (?, ?)",
        ("filesystem.write", "deny"),
    )
    grants.invalidate()

    result = await manager.execute_capability(
        conn.id, "perm-fake.write", {}
    )
    assert result.success is False
    assert result.permissions == ["filesystem.write"]
    assert "filesystem.write" in (result.error or "")

    # With approval=True it is still blocked: deny takes precedence over the gate.
    result = await manager.execute_capability(
        conn.id, "perm-fake.write", {}, approval=True
    )
    assert result.success is False
    assert result.permissions == ["filesystem.write"]


# ---------------------------------------------------------------------------
# Expired / revoked connection — NEEDS_REAUTHORIZATION blocks execution
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_execute_capability_on_needs_reauthorization_is_refused():
    """Executing on a connection that needs reauthorization raises NotConnected."""
    registry.register(_FakeConnector)
    conn = await manager.create_connection("fake")
    await manager.start_auth(conn.id, "http://localhost/callback")
    await manager.complete_auth(
        conn.id, {"code": "code", "state": _AUTH_PENDING[conn.id]["state"]}
    )

    # Sanity: connected and executable.
    result = await manager.execute_capability(conn.id, "fake.read", {})
    assert result.success is True

    # Force the connection into needs_reauthorization (simulating expired token).
    stored = manager.get_connection(conn.id)
    stored.state = ConnectionState.NEEDS_REAUTHORIZATION
    stored.error = "Token expired"

    from core.connections.connection_manager import NotConnected
    with pytest.raises(NotConnected):
        await manager.execute_capability(conn.id, "fake.read", {})

    # Refresh restores connected state and re-enables execution.
    await manager.refresh_credentials(conn.id)
    stored = manager.get_connection(conn.id)
    assert stored.state == ConnectionState.CONNECTED

    result = await manager.execute_capability(conn.id, "fake.read", {})
    assert result.success is True


# ---------------------------------------------------------------------------
# Multiple connectors — discover across all connections
# ---------------------------------------------------------------------------
class _FakeSecondConnector(OAuth2Connector):
    """A second, distinct connector for multi-connector discovery tests."""

    OAUTH_AUTHORIZATION_URL = "https://example2.com/oauth/authorize"
    OAUTH_TOKEN_URL = "https://example2.com/oauth/token"
    OAUTH_SCOPES = ["read"]
    OAUTH_CLIENT_ID = "fake-client-2"

    def __init__(self, definition):
        super().__init__(definition)

    @classmethod
    def connector_id(cls) -> str:
        return "second-fake"

    @classmethod
    def definition_class(cls):
        return ConnectorDefinition(
            id="second-fake",
            name="Second Fake",
            description="A second test connector.",
            icon="second",
            category="test",
            auth_method="oauth2",
            scopes=cls.OAUTH_SCOPES,
            capabilities=[
                ConnectorCapability(
                    id="second-fake.read",
                    name="Read",
                    type="read",
                    description="Read from the second connector.",
                    input_schema={"type": "object", "properties": {}},
                    risk_level="safe",
                ),
            ],
            version="1.0.0",
        )

    async def _token_exchange(self, client_id, code, code_verifier, redirect_uri):
        return OAuthToken(
            access_token="at_" + code, refresh_token="rt_" + code,
            expires_in=3600, scope="read",
        )

    async def _refresh_exchange(self, refresh_token, redirect_uri):
        return OAuthToken(
            access_token="at_refreshed", refresh_token=refresh_token,
            expires_in=3600, scope="read",
        )

    async def list_capabilities(self, connection_id: str):
        return list(self.definition.capabilities)

    async def execute_capability(self, connection_id, capability_id, params):
        return CapabilityResult(
            success=True, output="Executed", data={"capability": capability_id},
        )


@pytest.mark.asyncio
async def test_discover_all_scans_multiple_connections():
    """connector_discover without connection_id lists capabilities across all connections."""
    registry.register(_FakeConnector)
    registry.register(_FakeSecondConnector)

    # Connect both connectors.
    conn1 = await manager.create_connection("fake")
    await manager.start_auth(conn1.id, "http://localhost/callback")
    await manager.complete_auth(
        conn1.id, {"code": "code1", "state": _AUTH_PENDING[conn1.id]["state"]}
    )

    conn2 = await manager.create_connection("second-fake")
    await manager.start_auth(conn2.id, "http://localhost/callback")
    await manager.complete_auth(
        conn2.id, {"code": "code2", "state": _AUTH_PENDING[conn2.id]["state"]}
    )

    found = discovery.discover_all()
    ids = {c.capability.id for c in found}
    assert "fake.read" in ids
    assert "fake.write" in ids
    assert "second-fake.read" in ids
    assert len(found) == 3

    # Per-connection discovery still works.
    found_one = discovery.discover_all(connection_id=conn1.id)
    ids_one = {c.capability.id for c in found_one}
    assert ids_one == {"fake.read", "fake.write"}

    found_two = discovery.discover_all(connection_id=conn2.id)
    ids_two = {c.capability.id for c in found_two}
    assert ids_two == {"second-fake.read"}


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------
def test_google_drive_permissions_exist():
    assert Permission.GOOGLE_DRIVE_ACCESS.value == "google_drive.access"
    assert Permission.GMAIL_ACCESS.value == "gmail.access"
    assert Permission.GMAIL_SEND.value == "gmail.send"


def test_required_for_google_drive():
    perms = required_for("google_drive_list_files")
    assert Permission.GOOGLE_DRIVE_ACCESS in perms


def test_required_for_gmail_send():
    perms = required_for("gmail_send")
    assert Permission.GMAIL_ACCESS in perms
    assert Permission.GMAIL_SEND in perms


def test_describe_includes_connections_perms():
    from core.permissions import describe as describe_permissions

    described = {p["permission"] for p in describe_permissions()}
    assert "google_drive.access" in described
    assert "gmail.access" in described
    assert "gmail.send" in described


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def test_discovery_returns_declared_capabilities():
    registry.register(_FakeConnector)
    found = discovery.discover_all()
    # No connections yet → nothing discovered.
    assert found == []


@pytest.mark.asyncio
async def test_discovery_after_connect():
    registry.register(_FakeConnector)
    conn = await manager.create_connection("fake")
    await manager.start_auth(conn.id, "http://localhost/callback")
    await manager.complete_auth(
        conn.id, {"code": "code", "state": _AUTH_PENDING[conn.id]["state"]}
    )
    found = discovery.discover_all(connection_id=conn.id)
    assert len(found) == 2
    ids = {c.capability.id for c in found}
    assert ids == {"fake.read", "fake.write"}


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------
def test_api_list_connectors():
    from core.api.connections_api import router as conn_router
    from api.main import app

    registry.register(_FakeConnector)
    client = TestClient(app)
    resp = client.get("/connectors")
    assert resp.status_code == 200
    data = resp.json()
    ids = {c["id"] for c in data["connectors"]}
    assert "fake" in ids


def test_api_create_and_list_connections():
    from api.main import app

    registry.register(_FakeConnector)
    client = TestClient(app)
    resp = client.post("/connections", json={"connector_id": "fake"})
    assert resp.status_code == 200
    conn = resp.json()
    assert conn["connector_id"] == "fake"
    assert conn["state"] == "not_connected"
    assert conn["has_credentials"] is False

    resp = client.get("/connections")
    assert resp.status_code == 200
    assert len(resp.json()["connections"]) == 1


def test_api_connector_not_found():
    from api.main import app
    client = TestClient(app)
    resp = client.post("/connections", json={"connector_id": "nope"})
    assert resp.status_code == 404


def test_api_get_connection_404():
    from api.main import app
    client = TestClient(app)
    resp = client.get("/connections/nope-id")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_api_auth_start_returns_url():
    from api.main import app

    registry.register(_FakeConnector)
    client = TestClient(app)
    resp = client.post("/connections", json={"connector_id": "fake"})
    conn_id = resp.json()["id"]

    resp = client.post(
        f"/connections/{conn_id}/auth/start",
        json={"redirect_uri": "http://localhost/callback"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "auth_url" in data
    assert "state" in data


def test_api_secrets_never_returned():
    from api.main import app

    registry.register(_FakeConnector)
    client = TestClient(app)
    resp = client.post("/connections", json={"connector_id": "fake"})
    conn = resp.json()
    assert "credentials_key" not in conn
    assert "access_token" not in str(conn)
    assert conn["has_credentials"] is False


def test_api_discover_endpoint():
    from api.main import app
    client = TestClient(app)
    resp = client.get("/connections/discover")
    assert resp.status_code == 200
    data = resp.json()
    assert "capabilities" in data
    assert "total" in data


# ---------------------------------------------------------------------------
# MCP connector (structural only — no real subprocess)
# ---------------------------------------------------------------------------
def test_mcp_connector_structure():
    definition = MCPServerConfig(
        id="mcp-1", name="test", command="echo"
    ).to_dict()
    assert definition["id"] == "mcp-1"
    assert definition["command"] == "echo"
    assert "env" not in definition  # env excluded from public rendering


__all__ = []