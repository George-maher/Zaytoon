"""Connector registry — the catalog of available connectors.

A registry holds :class:`ConnectorDefinition` objects, one per connector kind
(GitHub, Google Drive, an MCP server, …). It is the single place new
connectors are added, and the single place the UI lists what can be connected.

The registry is intentionally passive: it knows *what* connectors exist, not
which ones are connected. Connection state belongs to
:class:`connection_manager.ConnectionManager`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.connections.connector_interface import (
    ConnectorCapability,
    ConnectorDefinition,
)

logger = logging.getLogger("Zaytoon.Connections.Registry")


@dataclass
class RegisteredConnector:
    """A connector kind plus its implementation class."""

    definition: ConnectorDefinition
    connector_class: type  # subclass of BaseConnector

    def to_dict(self) -> Dict[str, Any]:
        return self.definition.to_dict()


class ConnectorRegistry:
    """Catalog of available connectors.

    Thread-safe-ish (single asyncio event loop in this app; locks are not
    needed because registration happens at startup before any requests).
    """

    def __init__(self) -> None:
        self._connectors: Dict[str, RegisteredConnector] = {}

    # -- registration ----------------------------------------------------
    def register(self, connector_class: type, definition: Optional[ConnectorDefinition] = None) -> None:
        """Register a connector kind.

        ``definition`` defaults to ``connector_class.definition_class()``.
        Re-registering the same id replaces the previous entry.
        """
        connector_id = connector_class.connector_id()
        if definition is None:
            definition = connector_class.definition_class()
        if definition.id != connector_id:
            raise ValueError(
                f"definition.id {definition.id!r} != connector_id() {connector_id!r}"
            )
        self._connectors[connector_id] = RegisteredConnector(
            definition=definition, connector_class=connector_class
        )
        logger.info("Registered connector %s (%s)", connector_id, connector_class.__name__)

    def unregister(self, connector_id: str) -> None:
        self._connectors.pop(connector_id, None)

    # -- lookup ----------------------------------------------------------
    def get(self, connector_id: str) -> Optional[RegisteredConnector]:
        return self._connectors.get(connector_id)

    def has(self, connector_id: str) -> bool:
        return connector_id in self._connectors

    def list(self) -> List[RegisteredConnector]:
        return sorted(self._connectors.values(), key=lambda r: r.definition.name)

    def list_available(self) -> List[RegisteredConnector]:
        """Connectors whose status is ``available`` (not deprecated/disabled)."""
        return [
            c for c in self.list() if c.definition.status == "available"
        ]

    # -- instantiation ---------------------------------------------------
    def instantiate(self, connector_id: str) -> Optional[BaseConnector]:
        """Create a fresh connector instance for a connection to use."""
        entry = self.get(connector_id)
        if entry is None:
            return None
        return entry.connector_class(entry.definition)

    # -- introspection ---------------------------------------------------
    def describe(self) -> List[Dict[str, Any]]:
        """Plain-data listing for the API / UI. Never includes secrets."""
        return [c.to_dict() for c in self.list_available()]


#: Global registry. Built at startup and populated by the connectors module.
registry = ConnectorRegistry()


__all__ = [
    "RegisteredConnector",
    "ConnectorRegistry",
    "registry",
]
