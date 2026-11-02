"""REST routes for the Connections platform.

Endpoints:

    GET    /connectors                          list available connectors
    GET    /connectors/{id}                     connector definition
    GET    /connections                         list connections
    GET    /connections/{id}                    connection status
    POST   /connections                         create a connection
    POST   /connections/{id}/auth/start         start OAuth / API-key flow
    POST   /connections/{id}/auth/complete      complete auth (callback)
    POST   /connections/{id}/test               test connection
    POST   /connections/{id}/refresh            refresh credentials
    POST   /connections/{id}/disconnect         disconnect
    DELETE /connections/{id}                    delete connection
    GET    /connections/{id}/capabilities       list capabilities
    POST   /connections/{id}/capabilities/{cap}/execute   execute a capability

Credentials are never returned by any route — only ``has_credentials``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException, Path, Query

from core.connections.capability_discovery import discovery
from core.connections.connection_manager import (
    AlreadyConnected,
    ConnectionManager,
    ConnectionNotFound,
    ConnectorNotFound,
    manager,
)
from core.connections.connector_interface import ConnectionState
from core.connections.connector_registry import registry

logger = logging.getLogger("Zaytoon.Connections.API")

router = APIRouter()


# ---------------------------------------------------------------------------
# Connectors
# ---------------------------------------------------------------------------
@router.get("/connectors")
async def list_connectors() -> Dict[str, Any]:
    """All available connectors (never secrets)."""
    return {"connectors": registry.describe()}


@router.get("/connectors/{connector_id}")
async def get_connector(
    connector_id: str = Path(..., description="Connector stable id, e.g. github"),
) -> Dict[str, Any]:
    entry = registry.get(connector_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Unknown connector: {connector_id}")
    return entry.to_dict()


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------
@router.get("/connections")
async def list_connections() -> Dict[str, Any]:
    """All connections with status — no credentials."""
    return {
        "connections": [
            c.to_dict() for c in manager.list_connections()
        ],
    }


# -- Static-path routes first (before /connections/{connection_id}) --
@router.get("/connections/discover")
async def discover_capabilities(
    connection_id: Optional[str] = Query(None, description="Filter to one connection"),
) -> Dict[str, Any]:
    """Discover capabilities across all (or one) connected connector."""
    found = discovery.discover_all(connection_id=connection_id)
    return {
        "capabilities": [c.to_dict() for c in found],
        "total": len(found),
    }


@router.get("/connections/{connection_id}")
async def get_connection(
    connection_id: str = Path(..., description="Connection id"),
) -> Dict[str, Any]:
    connection = manager.get_connection(connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail=f"Unknown connection: {connection_id}")
    return connection.to_dict()


@router.post("/connections")
async def create_connection(
    connector_id: str = Body(..., embed=True),
    config: Optional[Dict[str, Any]] = Body(default=None, embed=True),
) -> Dict[str, Any]:
    try:
        connection = await manager.create_connection(connector_id, config)
    except ConnectorNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Create connection failed for %s: %s", connector_id, exc)
        raise HTTPException(
            status_code=400,
            detail=f"Could not create connection: {exc}",
        )
    return connection.to_dict()


@router.post("/connections/{connection_id}/auth/start")
async def auth_start(
    connection_id: str = Path(...),
    redirect_uri: str = Body(..., embed=True),
) -> Dict[str, Any]:
    """Begin the auth flow. Returns an auth_url (OAuth) or form fields (API key)."""
    connection = manager.get_connection(connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail=f"Unknown connection: {connection_id}")
    try:
        return await manager.start_auth(connection_id, redirect_uri)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Auth start failed for %s: %s", connection_id, exc)
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/connections/{connection_id}/auth/complete")
async def auth_complete(
    connection_id: str = Path(...),
    auth_response: Dict[str, Any] = Body(..., embed=True),
) -> Dict[str, Any]:
    """Complete the auth flow (OAuth callback or API-key submission)."""
    connection = manager.get_connection(connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail=f"Unknown connection: {connection_id}")
    try:
        return await manager.complete_auth(connection_id, auth_response)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Auth complete failed for %s: %s", connection_id, exc)
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/connections/{connection_id}/test")
async def test_connection(
    connection_id: str = Path(...),
) -> Dict[str, Any]:
    connection = manager.get_connection(connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail=f"Unknown connection: {connection_id}")
    try:
        return await manager.test_connection(connection_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Connection test failed for %s: %s", connection_id, exc)
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/connections/{connection_id}/refresh")
async def refresh_connection(
    connection_id: str = Path(...),
) -> Dict[str, Any]:
    connection = manager.get_connection(connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail=f"Unknown connection: {connection_id}")
    try:
        return await manager.refresh_credentials(connection_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Refresh failed for %s: %s", connection_id, exc)
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/connections/{connection_id}/disconnect")
async def disconnect_connection(
    connection_id: str = Path(...),
) -> Dict[str, Any]:
    connection = manager.get_connection(connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail=f"Unknown connection: {connection_id}")
    try:
        return await manager.disconnect(connection_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Disconnect failed for %s: %s", connection_id, exc)
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/connections/{connection_id}")
async def delete_connection(
    connection_id: str = Path(...),
) -> Dict[str, Any]:
    connection = manager.get_connection(connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail=f"Unknown connection: {connection_id}")
    try:
        await manager.delete_connection(connection_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Delete connection failed for %s: %s", connection_id, exc)
        raise HTTPException(status_code=400, detail=str(exc))
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------
@router.get("/connections/{connection_id}/capabilities")
async def list_capabilities(
    connection_id: str = Path(...),
) -> Dict[str, Any]:
    connection = manager.get_connection(connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail=f"Unknown connection: {connection_id}")
    try:
        caps = await manager.list_capabilities(connection_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("List capabilities failed for %s: %s", connection_id, exc)
        raise HTTPException(status_code=400, detail=str(exc))
    return {"capabilities": [c.to_dict() for c in caps]}


@router.post(
    "/connections/{connection_id}/capabilities/{capability_id}/execute"
)
async def execute_capability(
    connection_id: str = Path(...),
    capability_id: str = Path(...),
    params: Optional[Dict[str, Any]] = Body(default=None, embed=True),
    approval: Optional[bool] = Body(default=None, embed=True),
) -> Dict[str, Any]:
    """Execute one capability through the unified gate.

    Returns the CapabilityResult dict. When the capability is confirm- or
    dangerous-tier and no approval was given, the response carries
    ``requires_confirmation: true`` plus a confirmation prompt — the caller
    (WebSocket handler / assistant) shows the dialog and retries with
    ``approval: true``.
    """
    connection = manager.get_connection(connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail=f"Unknown connection: {connection_id}")
    try:
        result = await manager.execute_capability(
            connection_id, capability_id, params, approval=approval
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Capability execution failed: %s/%s", connection_id, capability_id, exc_info=True)
        raise HTTPException(status_code=400, detail=str(exc))
    return result.to_dict()


__all__ = ["router"]
