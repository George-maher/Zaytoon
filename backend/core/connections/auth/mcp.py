"""MCP (Model Context Protocol) server support for connectors.

Security properties:

* **Allow-listed command** — an MCP server is launched only if its command is
  on the application allow-list (the same check `open_app` uses), or is an
  explicit path that exists on disk.
* **Sandboxed stdio** — the server runs as a subprocess with bounded concurrency
  and timeouts; its stdout/stderr are never surfaced raw to the assistant.
* **Permission-gated tools** — each discovered MCP tool is exposed as a
  connector capability and gated through :mod:`core.permissions` + the
  confirmation flow before execution.
* **No secret passthrough** — MCP server config (env, args) is stored without
  logging; the capability execution path applies the same redaction as any
  other tool.

This module provides :class:`MCPConnector`, which manages adding, configuring,
validating, connecting to, and discovering tools from an MCP server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.connections.connector_interface import (
    BaseConnector,
    CapabilityResult,
    ConnectorCapability,
    ConnectionState,
)
from tools.sandbox import is_allowed_app

logger = logging.getLogger("Zaytoon.Connections.Auth.MCP")


@dataclass
class MCPServerConfig:
    """Configuration for one MCP server."""

    id: str  # stable server id
    name: str
    command: str  # executable to launch
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    enabled: bool = True

    def to_dict(self, include_env: bool = False) -> Dict[str, Any]:
        data = {
            "id": self.id,
            "name": self.name,
            "command": self.command,
            "args": self.args,
            "enabled": self.enabled,
        }
        if include_env:
            # Env may contain secrets — only include for internal use.
            data["env"] = self.env
        return data


class MCPConnector(BaseConnector):
    """Manages MCP servers as connectors.

    Each MCP server is its own "connection" to this connector. The manager
    creates one Connection per MCP server id, and this connector handles
    launching, tool discovery, and tool execution over stdio.
    """

    OAUTH_AUTHORIZATION_URL: str = ""
    OAUTH_TOKEN_URL: str = ""
    OAUTH_SCOPES: list = []
    OAUTH_CLIENT_ID: str = ""

    def __init__(self, definition):
        super().__init__(definition)
        self._servers: Dict[str, MCPServerConfig] = {}
        self._processes: Dict[str, asyncio.subprocess.Process] = {}
        self._tools_cache: Dict[str, List[ConnectorCapability]] = {}

    # -- server management ----------------------------------------------
    async def validate_config(self, config: Dict[str, Any]) -> None:
        """Validate an MCP server config before creating a connection."""
        command = config.get("command")
        if not command or not isinstance(command, str) or not command.strip():
            raise ValueError("MCP server requires a command")
        command = command.strip()
        # Allow-listed app OR explicit existing path.
        if not is_allowed_app(command) and not self._is_existing_path(command):
            raise ValueError(
                f"MCP command '{command}' is not on the application allow-list "
                "and is not an existing executable path"
            )

    @staticmethod
    def _is_existing_path(command: str) -> bool:
        try:
            return os.path.exists(os.path.expanduser(command))
        except (ValueError, OSError):
            return False

    async def start_auth(
        self, connection_id: str, redirect_uri: str
    ) -> Dict[str, Any]:
        """MCP has no OAuth flow. Returns config fields for server setup."""
        return {
            "auth_method": "mcp",
            "fields": [
                {
                    "name": "name",
                    "label": "Server name",
                    "type": "text",
                    "placeholder": "local-filesystem",
                },
                {
                    "name": "command",
                    "label": "Command",
                    "type": "text",
                    "placeholder": "npx @modelcontextprotocol/server-filesystem",
                },
                {
                    "name": "args",
                    "label": "Arguments",
                    "type": "text",
                    "placeholder": "/path/to/allowed/root",
                },
                {
                    "name": "env",
                    "label": "Environment variables (JSON)",
                    "type": "text",
                    "placeholder": '{"KEY": "value"}',
                },
            ],
            "connection_id": connection_id,
            "connector_id": self.connector_id(),
        }

    async def complete_auth(
        self, connection_id: str, auth_response: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Configure and connect to an MCP server.

        Stores the server config, launches the subprocess, discovers tools,
        and marks the connection connected.
        """
        name = auth_response.get("name") or f"MCP server {connection_id[:8]}"
        command = auth_response.get("command")
        args_raw = auth_response.get("args") or ""
        env_raw = auth_response.get("env") or "{}"

        try:
            args = shlex.split(args_raw)
        except ValueError as exc:
            return {
                "state": ConnectionState.ERROR.value,
                "error": f"Invalid arguments: {exc}",
            }
        try:
            env = json.loads(env_raw) if env_raw.strip() else {}
        except json.JSONDecodeError as exc:
            return {
                "state": ConnectionState.ERROR.value,
                "error": f"Invalid env JSON: {exc}",
            }

        if not isinstance(env, dict):
            return {"state": ConnectionState.ERROR.value, "error": "env must be an object"}

        server = MCPServerConfig(
            id=connection_id,
            name=name,
            command=command,
            args=args,
            env=env,
            enabled=True,
        )
        self._servers[connection_id] = server

        # Launch and discover tools.
        try:
            proc = await self._launch_server(server)
            self._processes[connection_id] = proc
            tools = await self._discover_tools(proc, server)
            self._tools_cache[connection_id] = tools
        except Exception as exc:  # noqa: BLE001
            logger.warning("MCP connect failed for %s: %s", connection_id, exc)
            self._processes.pop(connection_id, None)
            self._servers.pop(connection_id, None)
            return {
                "state": ConnectionState.ERROR.value,
                "error": f"Could not connect to MCP server: {exc}",
            }

        return {
            "state": ConnectionState.CONNECTED.value,
            "scopes": [],
        }

    async def test_connection(self, connection_id: str) -> Dict[str, Any]:
        proc = self._processes.get(connection_id)
        if proc is None:
            return {"ok": False, "error": "MCP server not running"}
        if proc.returncode is not None:
            return {"ok": False, "error": "MCP server process exited"}
        return {"ok": True}

    async def refresh_credentials(self, connection_id: str) -> bool:
        """MCP has no credentials to refresh; reconnect restarts the server."""
        return False

    async def disconnect(self, connection_id: str) -> None:
        proc = self._processes.pop(connection_id, None)
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
            except Exception as exc:  # noqa: BLE001
                logger.warning("MCP disconnect error for %s: %s", connection_id, exc)
        self._servers.pop(connection_id, None)
        self._tools_cache.pop(connection_id, None)

    async def list_capabilities(self, connection_id: str) -> List[ConnectorCapability]:
        cached = self._tools_cache.get(connection_id)
        if cached is not None:
            return cached
        proc = self._processes.get(connection_id)
        server = self._servers.get(connection_id)
        if proc is None or server is None:
            return []
        try:
            tools = await self._discover_tools(proc, server)
            self._tools_cache[connection_id] = tools
            return tools
        except Exception as exc:  # noqa: BLE001
            logger.warning("MCP tool discovery failed for %s: %s", connection_id, exc)
            return []

    async def execute_capability(
        self, connection_id: str, capability_id: str, params: Dict[str, Any]
    ) -> CapabilityResult:
        proc = self._processes.get(connection_id)
        server = self._servers.get(connection_id)
        if proc is None or server is None:
            return CapabilityResult(
                success=False,
                output="MCP server not connected",
                error="MCP server not connected",
            )
        tools = self._tools_cache.get(connection_id) or []
        tool = next((t for t in tools if t.id == capability_id), None)
        if tool is None:
            return CapabilityResult(
                success=False,
                output=f"Unknown capability: {capability_id}",
                error=f"Unknown capability: {capability_id}",
            )

        # Build an MCP tools/call request.
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": capability_id, "arguments": params},
        }
        try:
            response = await self._mcp_request(proc, request, timeout=30.0)
        except asyncio.TimeoutError:
            return CapabilityResult(
                success=False,
                output="MCP call timed out",
                error="MCP call timed out",
            )
        except Exception as exc:  # noqa: BLE001
            return CapabilityResult(
                success=False,
                output="MCP call failed",
                error=str(exc),
            )

        if "error" in response:
            return CapabilityResult(
                success=False,
                output="MCP tool returned an error",
                error=response["error"].get("message", "MCP error"),
            )

        result = response.get("result", {})
        return CapabilityResult(
            success=True,
            output="MCP tool completed",
            data=result,
        )

    # -- internal MCP protocol ------------------------------------------
    async def _launch_server(self, server: MCPServerConfig) -> asyncio.subprocess.Process:
        env = dict(os.environ)
        env.update(server.env)
        # Bounded: stdio only, no inherited file descriptors beyond stdin/out/err.
        proc = await asyncio.create_subprocess_exec(
            server.command,
            *server.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        # MCP initialize handshake.
        init_request = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "zaytoon", "version": "1.0.0"},
            },
        }
        await self._mcp_request(proc, init_request, timeout=15.0)
        return proc

    async def _discover_tools(
        self, proc: asyncio.subprocess.Process, server: MCPServerConfig
    ) -> List[ConnectorCapability]:
        request = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        }
        response = await self._mcp_request(proc, request, timeout=15.0)
        tools = response.get("result", {}).get("tools", [])
        capabilities: List[ConnectorCapability] = []
        for t in tools:
            capabilities.append(
                ConnectorCapability(
                    id=t.get("name", ""),
                    name=t.get("name", ""),
                    type="execute",
                    description=t.get("description", ""),
                    input_schema=t.get("inputSchema", {}) or {},
                    output_schema={},
                    requires_permission=None,  # resolved at execution time
                    risk_level="confirm",  # MCP tools are user code — confirm by default
                    authorized=True,
                )
            )
        return capabilities

    async def _mcp_request(
        self, proc: asyncio.subprocess.Process, request: Dict[str, Any], timeout: float = 30.0
    ) -> Dict[str, Any]:
        if proc.stdin is None or proc.stdout is None:
            raise RuntimeError("MCP process stdio not available")
        message = (json.dumps(request) + "\n").encode("utf-8")
        proc.stdin.write(message)
        await proc.stdin.drain()
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
        if not line:
            raise RuntimeError("MCP process closed stdout")
        return json.loads(line.decode("utf-8"))


__all__ = [
    "MCPServerConfig",
    "MCPConnector",
]
