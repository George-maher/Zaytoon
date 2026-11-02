"""Connection manager — per-connection lifecycle.

Owns the state machine for every live connection:

    not_connected -> connecting -> connected
    connected -> needs_reauthorization (token expired)
    connected -> disconnecting -> not_connected
    any -> error (transient failure)
    any -> disabled (administratively off)

Operations: create, start_auth, complete, test, refresh, reconnect,
disconnect, delete, report_status, handle_errors.

The manager never touches credentials directly — it delegates to the connector,
which stores them via :mod:`core.secrets`. Audit rows are written for every
lifecycle and consequential event (without sensitive payloads).
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from core.connections.connector_interface import (
    BaseConnector,
    CapabilityResult,
    Connection,
    ConnectionState,
    ConnectorCapability,
)
from core.connections.connector_registry import registry

from core import grants
from core.permissions import Permission, required_for
from tools.safety import PERMISSION_CONFIRM, PERMISSION_DANGEROUS, RiskLevel

logger = logging.getLogger("Zaytoon.Connections.Manager")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class ConnectionError(Exception):
    """Raised when a connection operation cannot proceed."""


class ConnectionNotFound(ConnectionError):
    pass


class ConnectorNotFound(ConnectionError):
    pass


class AlreadyConnected(ConnectionError):
    pass


class NotConnected(ConnectionError):
    pass


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------
@dataclass
class ConnectionRecord:
    """Internal bookkeeping: the Connection plus the connector instance."""

    connection: Connection
    connector: BaseConnector


class ConnectionManager:
    """Manages the lifecycle of connections."""

    def __init__(self) -> None:
        self._connections: Dict[str, ConnectionRecord] = {}

    # -- creation --------------------------------------------------------
    async def create_connection(
        self,
        connector_id: str,
        config: Optional[Dict[str, Any]] = None,
    ) -> Connection:
        """Create a new connection record in ``not_connected`` state.

        Validates the connector-specific config through the connector before
        persisting anything.
        """
        connector = registry.instantiate(connector_id)
        if connector is None:
            raise ConnectorNotFound(f"Unknown connector: {connector_id}")

        connection_id = str(uuid.uuid4())
        config = dict(config or {})
        await connector.validate_config(config)

        now = time.time()
        connection = Connection(
            id=connection_id,
            connector_id=connector_id,
            state=ConnectionState.NOT_CONNECTED,
            created_at=now,
            updated_at=now,
            config=config,
        )
        self._connections[connection_id] = ConnectionRecord(
            connection=connection, connector=connector
        )
        logger.info(
            "Created connection %s for connector %s", connection_id, connector_id
        )
        _audit("connection.created", connection_id, connector_id)
        return connection

    # -- auth flow -------------------------------------------------------
    async def start_auth(
        self,
        connection_id: str,
        redirect_uri: str,
    ) -> Dict[str, Any]:
        """Begin authentication for a connection.

        Returns what the API needs to send the user to the provider's
        authorization page (or show an API-key form). Never includes tokens.
        """
        record = self._get(connection_id)
        connection = record.connection

        if connection.state in (
            ConnectionState.CONNECTING,
            ConnectionState.DISCONNECTING,
        ):
            raise ConnectionError(
                f"Connection {connection_id} is already {connection.state.value}"
            )

        connection.state = ConnectionState.CONNECTING
        connection.updated_at = time.time()
        connection.error = None
        _audit("connection.start_auth", connection_id, connection.connector_id)

        try:
            result = await record.connector.start_auth(connection_id, redirect_uri)
        except Exception as exc:  # noqa: BLE001 - auth failures go to error state
            logger.warning("start_auth failed for %s: %s", connection_id, exc)
            connection.state = ConnectionState.ERROR
            connection.error = "Authentication could not be started"
            _audit("connection.auth_failed", connection_id, connection.connector_id, error=str(exc))
            raise ConnectionError("Authentication could not be started") from exc

        return result

    async def complete_auth(
        self,
        connection_id: str,
        auth_response: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Complete authentication (OAuth callback or API-key submission).

        Returns the resulting state. The connector is responsible for storing
        credentials via :mod:`core.secrets`.
        """
        record = self._get(connection_id)
        connection = record.connection

        if connection.state != ConnectionState.CONNECTING:
            raise ConnectionError(
                f"Connection {connection_id} is not connecting (state={connection.state.value})"
            )

        try:
            result = await record.connector.complete_auth(connection_id, auth_response)
        except Exception as exc:  # noqa: BLE001
            logger.warning("complete_auth failed for %s: %s", connection_id, exc)
            connection.state = ConnectionState.ERROR
            connection.error = "Authentication could not be completed"
            connection.updated_at = time.time()
            _audit("connection.auth_completed_failed", connection_id, connection.connector_id, error=str(exc))
            raise ConnectionError("Authentication could not be completed") from exc

        new_state = result.get("state", ConnectionState.CONNECTED.value)
        connection.state = ConnectionState(new_state)
        connection.error = result.get("error")
        if "scopes" in result:
            connection.scopes = list(result["scopes"])
        connection.updated_at = time.time()

        if connection.state == ConnectionState.CONNECTED:
            connection.credentials_key = record.connector.credentials_key_for(connection_id)
            _audit("connection.connected", connection_id, connection.connector_id, scopes=connection.scopes)
        elif connection.state == ConnectionState.NEEDS_REAUTHORIZATION:
            _audit("connection.needs_reauthorization", connection_id, connection.connector_id)
        else:
            _audit("connection.auth_error", connection_id, connection.connector_id, error=connection.error)

        return {
            "state": connection.state.value,
            "error": connection.error,
            "scopes": connection.scopes,
        }

    # -- status / discovery ---------------------------------------------
    def get_connection(self, connection_id: str) -> Optional[Connection]:
        record = self._connections.get(connection_id)
        return record.connection if record else None

    def list_connections(self) -> List[Connection]:
        return [
            record.connection
            for record in self._connections.values()
        ]

    def list_connections_by_connector(self, connector_id: str) -> List[Connection]:
        return [
            record.connection
            for record in self._connections.values()
            if record.connector_id == connector_id
        ]

    def report_status(self, connection_id: str) -> Dict[str, Any]:
        """Structured status for the UI — never includes credentials."""
        record = self._get_raw(connection_id)
        if record is None:
            return {"found": False}
        connection = record.connection
        return {
            "found": True,
            "id": connection.id,
            "connector_id": connection.connector_id,
            "state": connection.state.value,
            "has_credentials": bool(connection.credentials_key),
            "scopes": connection.scopes,
            "error": connection.error,
            "created_at": connection.created_at,
            "updated_at": connection.updated_at,
        }

    async def list_capabilities(self, connection_id: str) -> List[ConnectorCapability]:
        """Capabilities supported and authorized for this connection.

        Only callable when connected (or needs reauthorization — read-only
        discovery may still be useful).
        """
        record = self._get(connection_id)
        connection = record.connection
        if connection.state not in (
            ConnectionState.CONNECTED,
            ConnectionState.NEEDS_REAUTHORIZATION,
        ):
            raise NotConnected(
                f"Connection {connection_id} is not connected (state={connection.state.value})"
            )
        capabilities = await record.connector.list_capabilities(connection_id)
        return capabilities

    async def execute_capability(
        self,
        connection_id: str,
        capability_id: str,
        params: Optional[Dict[str, Any]] = None,
        approval: Optional[bool] = None,
    ) -> CapabilityResult:
        """Execute one capability on a connected connector.

        This is the unified execution entry point. The manager gates on
        permissions and risk level before delegating to the connector, which
        validates input and performs the actual call. Follows the same
        ``validate → gate → execute`` ordering as :class:`ToolRegistry`.

        ``approval`` lets a caller that has already run its own confirmation
        flow (e.g. the WebSocket handler after a user dialog) skip the gate.
        Leave it ``None`` to have the gate run here and return
        ``requires_confirmation`` when the capability is confirm/dangerous.
        """
        record = self._get(connection_id)
        connection = record.connection
        if connection.state != ConnectionState.CONNECTED:
            raise NotConnected(
                f"Connection {connection_id} is not connected (state={connection.state.value})"
            )

        # 1. Find the capability (supports + authorized).
        capabilities = await record.connector.list_capabilities(connection_id)
        capability = next((c for c in capabilities if c.id == capability_id), None)
        if capability is None:
            return CapabilityResult(
                success=False,
                output=f"Unknown capability: {capability_id}",
                error=f"Unknown capability: {capability_id}",
            )
        if not capability.authorized:
            return CapabilityResult(
                success=False,
                output=f"Capability not authorized: {capability_id}",
                error=f"Capability not authorized: {capability_id}",
            )

        # 2. Permission check — fail closed before any confirmation prompt.
        needed = set(required_for(
            f"{connection.connector_id}_{capability_id}",
            extra=[capability.requires_permission] if capability.requires_permission else None,
            tier=capability.risk_level,
        ))
        denied = await grants.denied(needed)
        if denied:
            _audit(
                "capability.denied",
                connection_id,
                connection.connector_id,
                capability=capability_id,
                permissions=denied,
            )
            return CapabilityResult(
                success=False,
                output="Permission denied",
                error=(
                    "This action needs "
                    f"{', '.join(denied)}, which you have turned off for Zaytoon. "
                    "Enable it in Settings > Permissions to allow it."
                ),
                permissions=denied,
            )

        # 3. Risk gate — confirm/dangerous capabilities need explicit approval.
        risk = RiskLevel.HIGH if capability.risk_level in (
            PERMISSION_CONFIRM, PERMISSION_DANGEROUS
        ) else RiskLevel.SAFE
        if risk is RiskLevel.HIGH:
            if approval is False:
                return CapabilityResult(
                    success=False,
                    output="Cancelled: the action was not approved.",
                    error="Cancelled: the action was not approved.",
                    cancelled=True,
                )
            if approval is None:
                return CapabilityResult(
                    success=False,
                    output="Confirmation required",
                    error=f"{capability.risk_level}-level action requires confirmation.",
                    requires_confirmation=True,
                    confirmation_prompt=capability.description,
                    data={
                        "capability_id": capability_id,
                        "risk_level": capability.risk_level,
                        "reasons": [f"'{capability_id}' is a {capability.risk_level}-level action"],
                    },
                )

        # 4. Execute.
        params = dict(params or {})
        result = await record.connector.execute_capability(
            connection_id, capability_id, params
        )
        _audit(
            "capability.executed",
            connection_id,
            connection.connector_id,
            capability=capability_id,
            success=result.success,
        )
        return result

    # -- lifecycle mutations ---------------------------------------------
    async def test_connection(self, connection_id: str) -> Dict[str, Any]:
        record = self._get(connection_id)
        connection = record.connection
        if connection.state != ConnectionState.CONNECTED:
            raise NotConnected(
                f"Connection {connection_id} is not connected (state={connection.state.value})"
            )
        result = await record.connector.test_connection(connection_id)
        connection.updated_at = time.time()
        if not result.get("ok", False):
            connection.error = result.get("error", "Connection test failed")
            connection.state = ConnectionState.ERROR
            _audit("connection.test_failed", connection_id, connection.connector_id, error=connection.error)
        else:
            connection.error = None
            if connection.state == ConnectionState.ERROR:
                connection.state = ConnectionState.CONNECTED
            _audit("connection.test_passed", connection_id, connection.connector_id)
        return result

    async def refresh_credentials(self, connection_id: str) -> Dict[str, Any]:
        record = self._get(connection_id)
        connection = record.connection
        if connection.state not in (
            ConnectionState.CONNECTED,
            ConnectionState.NEEDS_REAUTHORIZATION,
        ):
            raise NotConnected(
                f"Connection {connection_id} is not connected (state={connection.state.value})"
            )
        ok = await record.connector.refresh_credentials(connection_id)
        connection.updated_at = time.time()
        if ok:
            connection.state = ConnectionState.CONNECTED
            connection.error = None
            _audit("connection.refreshed", connection_id, connection.connector_id)
        else:
            connection.state = ConnectionState.NEEDS_REAUTHORIZATION
            connection.error = "Credentials could not be refreshed"
            _audit("connection.refresh_failed", connection_id, connection.connector_id)
        return {"state": connection.state.value, "ok": ok, "error": connection.error}

    async def reconnect(self, connection_id: str, redirect_uri: str) -> Dict[str, Any]:
        """Restart the auth flow for an existing connection (re-authorize)."""
        record = self._get(connection_id)
        connection = record.connection
        if connection.state not in (
            ConnectionState.NEEDS_REAUTHORIZATION,
            ConnectionState.ERROR,
            ConnectionState.NOT_CONNECTED,
        ):
            raise ConnectionError(
                f"Connection {connection_id} cannot reconnect from {connection.state.value}"
            )
        return await self.start_auth(connection_id, redirect_uri)

    async def disconnect(self, connection_id: str) -> Dict[str, Any]:
        """Disconnect: revoke credentials, clear state, keep the record."""
        record = self._get(connection_id)
        connection = record.connection
        if connection.state == ConnectionState.DISCONNECTING:
            raise ConnectionError(f"Connection {connection_id} is already disconnecting")

        connection.state = ConnectionState.DISCONNECTING
        connection.updated_at = time.time()
        _audit("connection.disconnecting", connection_id, connection.connector_id)

        try:
            await record.connector.disconnect(connection_id)
        except Exception as exc:  # noqa: BLE001 - best effort, still mark disconnected
            logger.warning("connector disconnect failed for %s: %s", connection_id, exc)
            connection.error = f"Disconnect completed with warnings: {exc}"

        connection.state = ConnectionState.NOT_CONNECTED
        connection.credentials_key = None
        connection.scopes = []
        connection.error = None
        connection.updated_at = time.time()
        _audit("connection.disconnected", connection_id, connection.connector_id)
        return {"state": connection.state.value}

    async def delete_connection(self, connection_id: str) -> bool:
        """Permanently delete a connection record and its credentials."""
        record = self._get(connection_id)
        connection = record.connection
        if connection.state in (ConnectionState.CONNECTING, ConnectionState.DISCONNECTING):
            raise ConnectionError(
                f"Cannot delete connection {connection_id} while {connection.state.value}"
            )

        # Best-effort credential revocation.
        try:
            if connection.credentials_key:
                await record.connector.disconnect(connection_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("delete revocation failed for %s: %s", connection_id, exc)

        record.connector.delete_credentials(connection_id)
        self._connections.pop(connection_id, None)
        _audit("connection.deleted", connection_id, connection.connector_id)
        return True

    # -- internal --------------------------------------------------------
    def _get(self, connection_id: str) -> ConnectionRecord:
        record = self._connections.get(connection_id)
        if record is None:
            raise ConnectionNotFound(f"Unknown connection: {connection_id}")
        return record

    def _get_raw(self, connection_id: str) -> Optional[ConnectionRecord]:
        return self._connections.get(connection_id)

    @property
    def connector_id(self) -> str:  # pragma: no cover - not used; placeholder
        return ""


#: Global manager. Built at startup.
manager = ConnectionManager()


def _audit(event: str, connection_id: str, connector_id: str, **extra: Any) -> None:
    """Write an audit breadcrumb without sensitive payloads."""
    from core.events import bus, CoreEventType

    try:
        bus.publish(
            CoreEventType.CONNECTION_EVENT,
            event=event,
            connection_id=connection_id,
            connector_id=connector_id,
            **extra,
        )
    except Exception:  # noqa: BLE001 - audit must never break a connection flow
        logger.debug("Audit publish failed for %s on %s", event, connection_id)


__all__ = [
    "ConnectionError",
    "ConnectionNotFound",
    "ConnectorNotFound",
    "AlreadyConnected",
    "NotConnected",
    "ConnectionRecord",
    "ConnectionManager",
    "manager",
]
