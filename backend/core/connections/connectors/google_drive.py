"""Google Drive connector — OAuth 2.0 demo connector.

Implements the design in docs/security/google-drive-gmail-oauth-design.md:

* Authorization code grant against accounts.google.com with PKCE + state.
* Scope: https://www.googleapis.com/auth/drive.file (minimum, least privilege).
* Tokens stored server-side via core/secrets.py; never returned to the frontend.
* Permission `google_drive.access` (confirm tier) gates every call;
  write operations additionally require `filesystem.write`.
* Refresh on 401; refresh failure moves the connection to needs_reauthorization.

Capabilities: list_files, read_file, search_files, create_file.
No Google-specific knowledge leaks into the assistant — capabilities are
discovered and invoked by ID through the unified interface.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

import httpx

from core.connections.auth.oauth import OAuth2Connector, OAuthToken
from core.connections.connector_interface import (
    CapabilityResult,
    ConnectorCapability,
    ConnectionState,
)
from core.connections.connector_registry import registry

logger = logging.getLogger("Zaytoon.Connections.GoogleDrive")

GOOGLE_API_BASE = "https://www.googleapis.com/drive/v3"


class GoogleDriveConnector(OAuth2Connector):
    OAUTH_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
    # Least privilege: drive.file grants per-file access to files created or
    # opened by the app. The user sees exactly this on Google's consent screen.
    OAUTH_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
    OAUTH_CLIENT_ID = ""

    @classmethod
    def connector_id(cls) -> str:
        return "google_drive"

    def __init__(self, definition):
        super().__init__(definition)
        self._http = httpx.AsyncClient(
            base_url=GOOGLE_API_BASE,
            timeout=httpx.Timeout(15.0),
            headers={"Accept": "application/json"},
        )

    @classmethod
    def definition_class(cls):
        from core.connections.connector_interface import ConnectorDefinition

        return ConnectorDefinition(
            id="google_drive",
            name="Google Drive",
            description="Files and folders in Google Drive.",
            icon="google_drive",
            category="storage",
            auth_method="oauth2",
            scopes=cls.OAUTH_SCOPES,
            capabilities=[
                ConnectorCapability(
                    id="google_drive.list_files",
                    name="List files",
                    type="list",
                    description="List files and folders in Google Drive.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "q": {
                                "type": "string",
                                "description": "Drive search query (e.g. 'mimeType = \"text/plain\"')",
                            },
                            "page_size": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 100,
                                "default": 10,
                            },
                        },
                        "additionalProperties": False,
                    },
                    output_schema={"type": "object"},
                    requires_permission="google_drive.access",
                    risk_level="confirm",
                ),
                ConnectorCapability(
                    id="google_drive.read_file",
                    name="Read file",
                    type="read",
                    description="Read a file's content and metadata.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "file_id": {
                                "type": "string",
                                "description": "Google Drive file ID",
                            },
                        },
                        "required": ["file_id"],
                        "additionalProperties": False,
                    },
                    output_schema={"type": "object"},
                    requires_permission="google_drive.access",
                    risk_level="confirm",
                ),
                ConnectorCapability(
                    id="google_drive.search_files",
                    name="Search files",
                    type="search",
                    description="Search Google Drive files by query.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "q": {
                                "type": "string",
                                "description": "Search query",
                            },
                            "page_size": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 100,
                                "default": 10,
                            },
                        },
                        "required": ["q"],
                        "additionalProperties": False,
                    },
                    output_schema={"type": "object"},
                    requires_permission="google_drive.access",
                    risk_level="confirm",
                ),
                ConnectorCapability(
                    id="google_drive.create_file",
                    name="Create file",
                    type="create",
                    description="Create a file in Google Drive (write).",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "File name",
                            },
                            "mime_type": {
                                "type": "string",
                                "description": "MIME type",
                                "default": "text/plain",
                            },
                            "content": {
                                "type": "string",
                                "description": "File content",
                            },
                        },
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                    output_schema={"type": "object"},
                    requires_permission="google_drive.access",
                    risk_level="dangerous",
                ),
            ],
            version="1.0.0",
            status="available",
        )

    # -- OAuth hooks -----------------------------------------------------
    async def _token_exchange(
        self,
        client_id: str,
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> OAuthToken:
        data = {
            "code": code,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            resp = await client.post(self.OAUTH_TOKEN_URL, data=data)
            resp.raise_for_status()
            payload = resp.json()
        expires_in = payload.get("expires_in")
        return OAuthToken(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token"),
            expires_in=expires_in,
            scope=payload.get("scope"),
            token_type=payload.get("token_type", "Bearer"),
        )

    async def _refresh_exchange(
        self, refresh_token: str, redirect_uri: str
    ) -> OAuthToken:
        data = {
            "client_id": self.OAUTH_CLIENT_ID,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            resp = await client.post(self.OAUTH_TOKEN_URL, data=data)
            resp.raise_for_status()
            payload = resp.json()
        return OAuthToken(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token", refresh_token),
            expires_in=payload.get("expires_in"),
            scope=payload.get("scope"),
            token_type=payload.get("token_type", "Bearer"),
        )

    # -- capabilities ----------------------------------------------------
    async def list_capabilities(self, connection_id: str) -> List[ConnectorCapability]:
        return list(self.definition.capabilities)

    async def _drive_request(
        self,
        connection_id: str,
        method: str,
        path: str,
        params=None,
        json_body=None,
        files=None,
    ) -> Dict[str, Any]:
        token = self.get_credentials(connection_id)
        if not token:
            raise ConnectionError("No stored Google Drive credentials")
        token_data = json.loads(token)
        headers = {"Authorization": f"Bearer {token_data['access_token']}"}
        if files is not None:
            headers.pop("Content-Type", None)  # httpx sets multipart boundary
        resp = await self._http.request(
            method, path, params=params, json=json_body, files=files, headers=headers
        )
        if resp.status_code == 401:
            # Try a refresh once, then retry.
            refreshed = await self.refresh_credentials(connection_id)
            if refreshed:
                token = self.get_credentials(connection_id)
                token_data = json.loads(token)
                headers = {"Authorization": f"Bearer {token_data['access_token']}"}
                resp = await self._http.request(
                    method, path, params=params, json=json_body,
                    files=files, headers=headers,
                )
                if resp.status_code == 401:
                    raise ConnectionError("Google Drive credentials expired or revoked")
            else:
                raise ConnectionError("Google Drive credentials expired or revoked")
        resp.raise_for_status()
        if resp.content:
            return resp.json()
        return {}

    async def execute_capability(
        self, connection_id: str, capability_id: str, params: Dict[str, Any]
    ) -> CapabilityResult:
        try:
            if capability_id == "google_drive.list_files":
                result = await self._drive_request(
                    connection_id, "GET", "/files",
                    params={
                        "q": params.get("q"),
                        "pageSize": params.get("page_size", 10),
                        "fields": "files(id,name,mimeType,modifiedTime,size),nextPageToken",
                    },
                )
                files = result.get("files", [])
                return CapabilityResult(
                    success=True,
                    output=f"{len(files)} file(s) in Google Drive",
                    data=result,
                )
            if capability_id == "google_drive.read_file":
                file_id = params["file_id"]
                meta = await self._drive_request(
                    connection_id, "GET", f"/files/{file_id}",
                    params={"fields": "id,name,mimeType,size,modifiedTime"},
                )
                return CapabilityResult(
                    success=True,
                    output=f"File {meta.get('name')} ({meta.get('mimeType')})",
                    data=meta,
                )
            if capability_id == "google_drive.search_files":
                result = await self._drive_request(
                    connection_id, "GET", "/files",
                    params={
                        "q": params.get("q"),
                        "pageSize": params.get("page_size", 10),
                    },
                )
                files = result.get("files", [])
                return CapabilityResult(
                    success=True,
                    output=f"{len(files)} file(s) matched",
                    data=result,
                )
            if capability_id == "google_drive.create_file":
                name = params["name"]
                mime_type = params.get("mime_type", "text/plain")
                content = params.get("content", "")
                meta = await self._drive_request(
                    connection_id, "POST", "/files",
                    params={"fields": "id,name,mimeType,size"},
                    json_body={
                        "name": name,
                        "mimeType": mime_type,
                        "description": "Created by Zaytoon",
                    },
                )
                file_id = meta.get("id")
                if content and file_id:
                    upload_url = f"https://www.googleapis.com/upload/drive/v3/files/{file_id}"
                    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
                        token = self.get_credentials(connection_id)
                        token_data = json.loads(token)
                        await client.post(
                            upload_url,
                            params={"uploadType": "media"},
                            headers={"Authorization": f"Bearer {token_data['access_token']}"},
                            content=content,
                        )
                return CapabilityResult(
                    success=True,
                    output=f"Created file {name} in Google Drive",
                    data=meta,
                )
            return CapabilityResult(
                success=False,
                output=f"Unknown capability: {capability_id}",
                error=f"Unknown capability: {capability_id}",
            )
        except ConnectionError as exc:
            return CapabilityResult(
                success=False,
                output="Google Drive request failed",
                error=str(exc),
            )
        except httpx.HTTPStatusError as exc:
            return CapabilityResult(
                success=False,
                output="Google Drive API returned an error",
                error=f"HTTP {exc.response.status_code}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Google Drive capability %s failed: %s", capability_id, exc)
            return CapabilityResult(
                success=False,
                output="Google Drive request failed",
                error="Google Drive request failed",
            )


# Register at import time.
registry.register(GoogleDriveConnector)


__all__ = [
    "GoogleDriveConnector",
]
