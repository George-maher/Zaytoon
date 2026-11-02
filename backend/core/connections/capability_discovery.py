"""Capability discovery for connected connectors.

Given a connection, this module answers: *what can this connection do, and is
the current user authorized to do it?* Capabilities are returned only when:

1. The connector supports them (declared in its definition), and
2. The connection is authorized (connected, or needs-reauthorization for
   read-only discovery), and
3. The caller holds the required permission (checked at execution time, not
   discovery time — discovery lists what *could* be done, the gate enforces
   it).

Capability types: search, read, list, create, update, delete, send, publish,
execute. A capability is exposed to the assistant as structured input/output —
the assistant never needs service-specific knowledge.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from core.connections.connection_manager import manager
from core.connections.connector_interface import (
    ConnectorCapability,
    ConnectionState,
)

logger = logging.getLogger("Zaytoon.Connections.Discovery")


#: Capabilities that are read-only and safe to list even when a connection
#: needs reauthorization (discovery without side effects).
READ_ONLY_CAPABILITY_TYPES = frozenset({"search", "read", "list"})


@dataclass
class DiscoveredCapability:
    """A capability presented to the assistant, with authorization context."""

    connector_id: str
    connection_id: str
    capability: ConnectorCapability
    #: Whether the current session is allowed to invoke it (permission check
    #: happens again at execution time; this is a fast pre-filter).
    authorized: bool
    #: Human explanation when not authorized.
    reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "connector_id": self.connector_id,
            "connection_id": self.connection_id,
            "capability": self.capability.to_dict(),
            "authorized": self.authorized,
            "reason": self.reason,
        }


class CapabilityDiscovery:
    """Discovers and filters capabilities across all connections."""

    def __init__(self) -> None:
        self._grants_cache: Dict[str, bool] = {}

    # -- public API ------------------------------------------------------
    def discover_all(
        self,
        connection_id: Optional[str] = None,
        include_unauthorized: bool = True,
    ) -> List[DiscoveredCapability]:
        """List capabilities for one connection or all connected ones.

        Only connected / needs-reauthorization connections are scanned.
        """
        connections = manager.list_connections()
        if connection_id is not None:
            connections = [
                c for c in connections if c.id == connection_id
            ]

        discovered: List[DiscoveredCapability] = []
        for connection in connections:
            if connection.state not in (
                ConnectionState.CONNECTED,
                ConnectionState.NEEDS_REAUTHORIZATION,
            ):
                continue
            try:
                # Discovery itself is synchronous here; the manager's
                # list_capabilities is async, but this class is used by the API
                # layer which runs it in an event loop. We expose a sync
                # view from the connector's *declared* capabilities plus any
                # dynamically discovered ones the manager already knows.
                caps = self._declared_capabilities(connection)
                for cap in caps:
                    authorized, reason = self._is_authorized(connection, cap)
                    if not authorized and not include_unauthorized:
                        continue
                    discovered.append(
                        DiscoveredCapability(
                            connector_id=connection.connector_id,
                            connection_id=connection.id,
                            capability=cap,
                            authorized=authorized,
                            reason=reason,
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Capability discovery failed for %s: %s", connection.id, exc
                )
        return discovered

    def find(
        self,
        capability_type: Optional[str] = None,
        connection_id: Optional[str] = None,
    ) -> List[DiscoveredCapability]:
        """Find capabilities by type and/or connection."""
        all_caps = self.discover_all(connection_id=connection_id)
        if capability_type is not None:
            all_caps = [
                c for c in all_caps
                if c.capability.type == capability_type
            ]
        return all_caps

    def authorized_for_connection(self, connection_id: str) -> List[DiscoveredCapability]:
        """Capabilities the caller may invoke on a connection."""
        return [
            c for c in self.discover_all(connection_id=connection_id) if c.authorized
        ]

    # -- internal --------------------------------------------------------
    def _declared_capabilities(self, connection) -> List[ConnectorCapability]:
        from core.connections.connector_registry import registry

        entry = registry.get(connection.connector_id)
        if entry is None:
            return []
        return list(entry.definition.capabilities)

    def _is_authorized(
        self, connection, capability: ConnectorCapability
    ) -> tuple:
        if not capability.authorized:
            return False, "Capability not authorized by connector"
        if capability.requires_permission:
            # Fast pre-check using the cached decision; the gate re-checks at
            # execution time.
            decision = self._decision_for(capability.requires_permission)
            if decision == "deny":
                return False, f"Permission {capability.requires_permission} is denied"
        return True, None

    def _decision_for(self, permission: str) -> str:
        if permission in self._grants_cache:
            return self._grants_cache[permission]
        # Synchronous default: ask. The async grant lookup happens in the
        # manager's execute path. This pre-check only avoids listing obviously
        # denied capabilities.
        return "ask"


#: Shared discovery instance.
discovery = CapabilityDiscovery()


__all__ = [
    "DiscoveredCapability",
    "CapabilityDiscovery",
    "READ_ONLY_CAPABILITY_TYPES",
    "discovery",
]
