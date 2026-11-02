"""Unified connector interface.

Every connector implements :class:`BaseConnector`. It exposes a structured,
service-agnostic surface: declared capabilities with JSON-Schema input/output,
and a single :meth:`execute_capability` entry point. The assistant (or any
consumer) never needs service-specific knowledge — it browses capabilities and
invokes them by ID.

Connector lifecycle states live here as :class:`ConnectionState`.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Zaytoon.Connections.Interface")


# ---------------------------------------------------------------------------
# Lifecycle states
# ---------------------------------------------------------------------------
class ConnectionState(str, Enum):
    """States a connection can be in.

    Transitions (not all enforced here — the manager owns the machine):

        not_connected  --connect-->  connecting
        connecting     --success-->  connected
        connecting     --failure-->  error / needs_reauthorization
        connected      --disconnect-->  disconnecting -> not_connected
        connected      --token expired-->  needs_reauthorization
        any            --disable-->    disabled
        disabled       --enable-->     not_connected
        error          --reconnect-->  connecting
    """

    NOT_CONNECTED = "not_connected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    NEEDS_REAUTHORIZATION = "needs_reauthorization"
    ERROR = "error"
    DISABLED = "disabled"
    DISCONNECTING = "disconnecting"


# All states a connection can be returned in (used by the API schema).
ALL_STATES = [s.value for s in ConnectionState]


# ---------------------------------------------------------------------------
# Capability types — the verbs a connector can expose.
# ---------------------------------------------------------------------------
CAPABILITY_SEARCH = "search"
CAPABILITY_READ = "read"
CAPABILITY_LIST = "list"
CAPABILITY_CREATE = "create"
CAPABILITY_UPDATE = "update"
CAPABILITY_DELETE = "delete"
CAPABILITY_SEND = "send"
CAPABILITY_PUBLISH = "publish"
CAPABILITY_EXECUTE = "execute"

ALL_CAPABILITY_TYPES = frozenset({
    CAPABILITY_SEARCH,
    CAPABILITY_READ,
    CAPABILITY_LIST,
    CAPABILITY_CREATE,
    CAPABILITY_UPDATE,
    CAPABILITY_DELETE,
    CAPABILITY_SEND,
    CAPABILITY_PUBLISH,
    CAPABILITY_EXECUTE,
})


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class ConnectorCapability:
    """One thing a connector can do, with structured input/output."""

    id: str  # stable within a connector, e.g. "github.search_repositories"
    name: str  # human name for the UI
    type: str  # one of ALL_CAPABILITY_TYPES
    description: str  # what it does, for the UI and the assistant
    input_schema: Dict[str, Any] = field(default_factory=dict)  # JSON Schema
    output_schema: Dict[str, Any] = field(default_factory=dict)  # JSON Schema
    requires_permission: Optional[str] = None  # permission needed to invoke
    risk_level: str = "safe"  # "safe" | "confirm" | "dangerous"
    #: Whether this capability is currently authorized for the active connection.
    authorized: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "requires_permission": self.requires_permission,
            "risk_level": self.risk_level,
            "authorized": self.authorized,
        }


@dataclass
class ConnectorDefinition:
    """Static descriptor of a connector kind (registered once at startup)."""

    id: str  # stable ID, e.g. "github", "google_drive", "mcp:filesystem"
    name: str  # human name
    description: str
    icon: str  # icon identifier for the UI
    category: str  # "development", "storage", "communication", "mcp", ...
    auth_method: str  # "oauth2", "api_key", "mcp", "none"
    config: Dict[str, Any] = field(default_factory=dict)  # connector-specific config
    scopes: List[str] = field(default_factory=list)  # OAuth scopes requested
    capabilities: List[ConnectorCapability] = field(default_factory=list)
    version: str = "1.0.0"
    status: str = "available"  # "available" | "deprecated" | "disabled"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "icon": self.icon,
            "category": self.category,
            "auth_method": self.auth_method,
            "config": self.config,
            "scopes": self.scopes,
            "capabilities": [c.to_dict() for c in self.capabilities],
            "version": self.version,
            "status": self.status,
        }


@dataclass
class Connection:
    """One live (or attempted) connection of a user to a connector."""

    id: str  # UUID — the wire identifier
    connector_id: str  # references ConnectorDefinition.id
    state: ConnectionState = ConnectionState.NOT_CONNECTED
    created_at: float = 0.0
    updated_at: float = 0.0
    credentials_key: Optional[str] = None  # key into secrets store
    scopes: List[str] = field(default_factory=list)  # scopes actually granted
    error: Optional[str] = None  # last error, never contains secrets
    config: Dict[str, Any] = field(default_factory=dict)  # connection-specific config

    def to_dict(self, include_secret: bool = False) -> Dict[str, Any]:
        # Secrets are never serialised — `include_secret` exists only for
        # internal callers that legitimately need to fetch one (and they read
        # it from secrets.py directly, never from this dict).
        return {
            "id": self.id,
            "connector_id": self.connector_id,
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "has_credentials": bool(self.credentials_key),
            "scopes": self.scopes,
            "error": self.error,
            "config": self.config,
        }


@dataclass
class CapabilityResult:
    """Structured result of executing a capability."""

    success: bool
    output: str  # human-readable summary
    data: Optional[Dict[str, Any]] = None  # structured payload
    error: Optional[str] = None
    requires_confirmation: bool = False
    confirmation_prompt: Optional[str] = None
    cancelled: bool = False
    #: Permissions that were denied and blocked this call (populated only when
    #: a grant decision forbids the capability before the approval gate).
    permissions: Optional[List[str]] = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"success": self.success, "output": self.output}
        if self.data is not None:
            payload["data"] = self.data
        if self.error:
            payload["error"] = self.error
        if self.requires_confirmation:
            payload["requires_confirmation"] = True
            payload["confirmation_prompt"] = self.confirmation_prompt
        if self.cancelled:
            payload["cancelled"] = True
        if self.permissions:
            payload["permissions"] = self.permissions
        return payload


# ---------------------------------------------------------------------------
# Base connector
# ---------------------------------------------------------------------------
class BaseConnector(ABC):
    """Abstract base for every connector.

    Subclasses declare a :class:`ConnectorDefinition` and implement the auth
    and capability methods. No subclass may return credential values to the
    caller — :meth:`get_credentials` reads them from :mod:`core.secrets` and
    is the only place a secret leaves storage.
    """

    definition: ConnectorDefinition

    def __init__(self, definition: ConnectorDefinition):
        self.definition = definition
        if definition.id and definition.id != self.connector_id():
            logger.warning(
                "Connector %s definition id %s does not match connector_id() %s",
                type(self).__name__, definition.id, self.connector_id(),
            )

    @classmethod
    @abstractmethod
    def connector_id(cls) -> str:
        """Stable identifier, must match ``definition.id``."""

    @classmethod
    @abstractmethod
    def definition_class(cls) -> type:
        """Return the :class:`ConnectorDefinition` subclass (or dataclass) to register."""

    # -- lifecycle -------------------------------------------------------
    @abstractmethod
    async def validate_config(self, config: Dict[str, Any]) -> None:
        """Validate connector-specific config before a connection is created.

        Raises :class:`ValueError` on invalid config. Never touches secrets.
        """

    @abstractmethod
    async def start_auth(
        self, connection_id: str, redirect_uri: str
    ) -> Dict[str, Any]:
        """Begin authentication.

        Returns a dict the API turns into a redirect for the user. Must never
        include tokens or secrets. For OAuth this is the authorization URL plus
        a state nonce. For API keys it returns instructions/form fields.
        """

    @abstractmethod
    async def complete_auth(
        self, connection_id: str, auth_response: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Finish authentication (e.g. OAuth callback).

        Stores credentials via :mod:`core.secrets` under a per-connection key
        and returns ``{"state": "connected" | "needs_reauthorization" | "error",
        "error": ...}``.
        """

    @abstractmethod
    async def test_connection(self, connection_id: str) -> Dict[str, Any]:
        """Prove the connection works (lightweight call). Returns ``{"ok": bool, "error": ...}``."""

    @abstractmethod
    async def refresh_credentials(self, connection_id: str) -> bool:
        """Attempt to refresh expired credentials. Returns True on success."""

    @abstractmethod
    async def disconnect(self, connection_id: str) -> None:
        """Revoke credentials and mark the connection disconnected."""

    # -- capabilities ----------------------------------------------------
    @abstractmethod
    async def list_capabilities(self, connection_id: str) -> List[ConnectorCapability]:
        """Capabilities supported *and* authorized for this connection."""

    @abstractmethod
    async def execute_capability(
        self, connection_id: str, capability_id: str, params: Dict[str, Any]
    ) -> CapabilityResult:
        """Run one capability. Validates input, enforces permissions, returns structured output."""

    # -- credentials (internal only, never returns values to callers) ----
    def credentials_key_for(self, connection_id: str) -> str:
        return f"connections.{self.connector_id()}.{connection_id}"

    def get_credentials(self, connection_id: str) -> Optional[str]:
        """Read the stored credential for this connection.

        Only the auth layer and capability execution call this. Nothing that
        renders to a client may call it.
        """
        from core.secrets import get_secret

        return get_secret(self.credentials_key_for(connection_id))

    def set_credentials(self, connection_id: str, value: str) -> bool:
        from core.secrets import set_secret

        return set_secret(self.credentials_key_for(connection_id), value or "")

    def delete_credentials(self, connection_id: str) -> bool:
        from core.secrets import delete_secret

        return delete_secret(self.credentials_key_for(connection_id))

    def has_credentials(self, connection_id: str) -> bool:
        from core.secrets import has_secret

        return has_secret(self.credentials_key_for(connection_id))


__all__ = [
    "ConnectionState",
    "ALL_STATES",
    "ALL_CAPABILITY_TYPES",
    "ConnectorCapability",
    "ConnectorDefinition",
    "Connection",
    "CapabilityResult",
    "BaseConnector",
    "CAPABILITY_SEARCH",
    "CAPABILITY_READ",
    "CAPABILITY_LIST",
    "CAPABILITY_CREATE",
    "CAPABILITY_UPDATE",
    "CAPABILITY_DELETE",
    "CAPABILITY_SEND",
    "CAPABILITY_PUBLISH",
    "CAPABILITY_EXECUTE",
]
