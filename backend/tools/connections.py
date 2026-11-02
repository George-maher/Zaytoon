"""Connector tools — bridge the assistant to the Connections platform.

Two tools, both service-agnostic:

* ``connector_discover`` — list capabilities for a connection (or all
  connections), read-only, ``safe`` tier. The assistant uses this to learn
  what a connected service can do before deciding whether to act.
* ``connector_execute`` — run one capability on a connection, ``confirm``
  tier. The assistant passes a connection id, capability id and parameters;
  the tool validates, checks permissions via the ConnectionManager's gate,
  handles the approval flow, and returns a structured :class:`CapabilityResult`.

Neither tool knows anything about GitHub, Google Drive or any specific
service. They delegate entirely to :class:`core.connections.connection_manager.ConnectionManager`,
which owns the registry, the state machine and the credential store.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from tools.base import BaseTool, ToolResult, ToolValidationError, _prop

logger = logging.getLogger("Zaytoon.Tools.Connections")


# ---------------------------------------------------------------------------
# Connector discovery tool
# ---------------------------------------------------------------------------
class ConnectorDiscover(BaseTool):
    """List connector capabilities, read-only. Safe tier."""

    permission = "safe"

    def name(self) -> str:
        return "connector_discover"

    def aliases(self) -> List[str]:
        return ["connections_discover", "list_connector_capabilities"]

    def description(self) -> str:
        return (
            "List the capabilities a connection can offer. Without a "
            "connection_id, lists capabilities across every connected "
            "service. Read-only: no action is taken, no credentials are "
            "touched. Use this before connector_execute to learn what a "
            "connected service can do."
        )

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "connection_id": _prop(
                    "string",
                    "Optional. If given, only this connection's capabilities "
                    "are listed. Otherwise all connected services are scanned.",
                ),
            },
            "additionalProperties": False,
        }

    async def execute(
        self, connection_id: Optional[str] = None, **_: Any
    ) -> ToolResult:
        from core.connections.connection_manager import manager
        from core.connections.capability_discovery import discovery

        try:
            found = discovery.discover_all(connection_id=connection_id)
        except Exception as exc:  # noqa: BLE001 - never leak internals
            logger.warning("connector_discover failed: %s", exc)
            return ToolResult.fail(
                f"Could not discover capabilities: {type(exc).__name__}"
            )

        if not found:
            hint = (
                f"connection {connection_id}"
                if connection_id
                else "no connected services"
            )
            return ToolResult.ok(
                f"No capabilities available for {hint}.",
                {"capabilities": [], "total": 0},
            )

        entries: List[Dict[str, Any]] = []
        for item in found:
            cap = item.capability
            entries.append(
                {
                    "connection_id": item.connection_id,
                    "connector_id": item.connector_id,
                    "capability_id": cap.id,
                    "name": cap.name,
                    "type": cap.type,
                    "description": cap.description,
                    "input_schema": cap.input_schema,
                    "requires_permission": cap.requires_permission,
                    "risk_level": cap.risk_level,
                    "authorized": item.authorized,
                    "reason": item.reason,
                }
            )

        by_conn: Dict[str, List[Dict[str, Any]]] = {}
        for entry in entries:
            by_conn.setdefault(entry["connection_id"], []).append(entry)

        summary_parts = []
        for cid, caps in by_conn.items():
            summary_parts.append(
                f"{cid}: {len(caps)} capability"
                + ("s" if len(caps) != 1 else "")
            )
        summary = "; ".join(summary_parts)

        return ToolResult.ok(
            f"Discovered {len(entries)} capability"
            + ("s" if len(entries) != 1 else "")
            + f" across {len(by_conn)} connection(s): {summary}.",
            {"capabilities": entries, "total": len(entries)},
        )


# ---------------------------------------------------------------------------
# Connector execution tool
# ---------------------------------------------------------------------------
class ConnectorExecute(BaseTool):
    """Execute one capability on a connection. Confirm tier — always asks."""

    permission = "confirm"

    def name(self) -> str:
        return "connector_execute"

    def aliases(self) -> List[str]:
        return ["connections_execute", "run_connector_capability"]

    def description(self) -> str:
        return (
            "Run one capability on a connected service. Provide the "
            "connection id, the capability id (from connector_discover), and "
            "the capability's parameters. The call is gated by permissions "
            "and may require your explicit approval before it runs. No "
            "credential values are ever returned — only the structured "
            "result of the action."
        )

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "connection_id": _prop(
                    "string",
                    "The connection id (from the Connections page or "
                    "connector_discover).",
                ),
                "capability_id": _prop(
                    "string",
                    "The capability id to execute, e.g. "
                    "\"github.search_repositories\".",
                ),
                "params": _prop(
                    "object",
                    "Parameters for the capability, matching its input_schema. "
                    "Defaults to {}.",
                    default={},
                ),
            },
            "required": ["connection_id", "capability_id"],
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        if not params.get("connection_id") or not str(params["connection_id"]).strip():
            raise ToolValidationError("'connection_id' is required")
        if not params.get("capability_id") or not str(params["capability_id"]).strip():
            raise ToolValidationError("'capability_id' is required")

    def risk_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "connection_id": params.get("connection_id", ""),
            "capability_id": params.get("capability_id", ""),
        }

    async def execute(
        self,
        connection_id: str,
        capability_id: str,
        params: Optional[Dict[str, Any]] = None,
        **_: Any,
    ) -> ToolResult:
        from core.connections.connection_manager import (
            manager,
            NotConnected,
        )

        try:
            result = await manager.execute_capability(
                connection_id, capability_id, dict(params or {}), approval=True
            )
        except NotConnected as exc:
            return ToolResult.fail(str(exc))
        except Exception as exc:  # noqa: BLE001 - never leak internals
            logger.warning(
                "connector_execute failed for %s/%s: %s",
                connection_id, capability_id, exc,
            )
            return ToolResult.fail(
                f"Capability execution failed: {type(exc).__name__}"
            )

        # Normalise the CapabilityResult into a ToolResult. The model reads
        # output/data; requires_confirmation is surfaced so it can ask the user.
        if result.requires_confirmation:
            return ToolResult(
                success=False,
                output=result.output or "Confirmation required",
                error=result.error,
                data={
                    "requires_confirmation": True,
                    "confirmation_prompt": result.confirmation_prompt,
                    "capability_id": capability_id,
                    "connection_id": connection_id,
                },
            )
        if result.cancelled:
            return ToolResult.fail(
                result.error or "Cancelled: the action was not approved.",
            )
        if not result.success:
            return ToolResult.fail(
                result.error or "Capability execution failed.",
                output=result.output,
            )
        return ToolResult.ok(
            result.output or f"Capability {capability_id} completed.",
            data=result.data or {},
        )


def build_tools() -> List[BaseTool]:
    return [ConnectorDiscover(), ConnectorExecute()]


__all__ = ["ConnectorDiscover", "ConnectorExecute", "build_tools"]