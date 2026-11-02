"""Zaytoon Connections platform.

A general-purpose, extensible integration platform so Zaytoon is not hardcoded
around fixed integrations. It provides:

* :class:`connector_registry.ConnectorRegistry` — catalog of available connectors
* :class:`connection_manager.ConnectionManager` — per-connection lifecycle
* :class:`connector_interface.BaseConnector` — unified connector interface
* :mod:`connector_interface` — capability definitions and structured I/O
* :mod:`capability_discovery` — discover what a connected connector can do
* :mod:`auth.oauth` — OAuth 2.0 (PKCE, state validation, refresh)
* :mod:`auth.api_key` — API key / PAT auth
* :mod:`auth.mcp` — MCP server support

Auth methods: OAuth 2.0, API keys/PATs, MCP servers.
Connector lifecycle states: not_connected, connecting, connected,
needs_reauthorization, error, disabled, disconnecting.

All credentials are stored server-side via :mod:`core.secrets` and never
returned to the frontend. Connector capabilities are gated through the existing
permission system (:mod:`core.permissions`, :mod:`core.grants`) and the
confirmation flow (:mod:`tools.safety`).
"""

from __future__ import annotations

__all__ = []
