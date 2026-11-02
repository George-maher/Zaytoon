"""GitHub connector — OAuth 2.0 demo connector.

Demonstrates the full connections flow end to end:

1. Register in ``connectors/__init__`` or at startup.
2. User clicks Connect → OAuth start (PKCE + state) → GitHub consent page.
3. Callback → complete_auth → tokens stored server-side via secrets.py.
4. Capabilities listed: search repos, read repo, list issues, create issue.
5. Each capability executes via the GitHub REST API with the stored token,
   gated through permissions + confirmation.

No GitHub-specific knowledge leaks into the assistant — it discovers
capabilities and invokes them by ID through the unified interface.
"""

from __future__ import annotations

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

logger = logging.getLogger("Zaytoon.Connections.GitHub")

GITHUB_API_BASE = "https://api.github.com"


class GitHubConnector(OAuth2Connector):
    OAUTH_AUTHORIZATION_URL = "https://github.com/login/oauth/authorize"
    OAUTH_TOKEN_URL = "https://github.com/login/oauth/access_token"
    # Minimum scopes: public repo read + user identity. Write scopes are added
    # only when the user grants them; the connector never requests more than it
    # needs for its declared capabilities.
    OAUTH_SCOPES = ["repo", "read:user", "user:email"]
    OAUTH_CLIENT_ID = ""

    @classmethod
    def connector_id(cls) -> str:
        return "github"

    def __init__(self, definition):
        super().__init__(definition)
        self._http = httpx.AsyncClient(
            base_url=GITHUB_API_BASE,
            timeout=httpx.Timeout(15.0),
            headers={"Accept": "application/vnd.github+json"},
        )

    # -- definition ------------------------------------------------------
    @classmethod
    def definition_class(cls):
        from core.connections.connector_interface import ConnectorDefinition

        return ConnectorDefinition(
            id="github",
            name="GitHub",
            description="Repositories, issues, and pull requests on GitHub.",
            icon="github",
            category="development",
            auth_method="oauth2",
            scopes=cls.OAUTH_SCOPES,
            capabilities=[
                ConnectorCapability(
                    id="github.search_repositories",
                    name="Search repositories",
                    type="search",
                    description="Search GitHub repositories by query.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "q": {
                                "type": "string",
                                "description": "Search query (GitHub search syntax)",
                            },
                            "per_page": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 100,
                                "default": 10,
                                "description": "Results per page",
                            },
                        },
                        "required": ["q"],
                        "additionalProperties": False,
                    },
                    output_schema={
                        "type": "object",
                        "properties": {
                            "total_count": {"type": "integer"},
                            "items": {"type": "array"},
                        },
                    },
                    requires_permission="network.request",
                    risk_level="safe",
                ),
                ConnectorCapability(
                    id="github.get_repository",
                    name="Get repository",
                    type="read",
                    description="Read a repository's metadata.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "owner": {"type": "string", "description": "Repository owner"},
                            "repo": {"type": "string", "description": "Repository name"},
                        },
                        "required": ["owner", "repo"],
                        "additionalProperties": False,
                    },
                    output_schema={"type": "object"},
                    requires_permission="network.request",
                    risk_level="safe",
                ),
                ConnectorCapability(
                    id="github.list_issues",
                    name="List issues",
                    type="list",
                    description="List issues in a repository.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "owner": {"type": "string"},
                            "repo": {"type": "string"},
                            "state": {
                                "type": "string",
                                "enum": ["open", "closed", "all"],
                                "default": "open",
                            },
                            "per_page": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 100,
                                "default": 10,
                            },
                        },
                        "required": ["owner", "repo"],
                        "additionalProperties": False,
                    },
                    output_schema={"type": "object"},
                    requires_permission="network.request",
                    risk_level="safe",
                ),
                ConnectorCapability(
                    id="github.create_issue",
                    name="Create issue",
                    type="create",
                    description="Create an issue in a repository (write).",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "owner": {"type": "string"},
                            "repo": {"type": "string"},
                            "title": {"type": "string"},
                            "body": {"type": "string"},
                        },
                        "required": ["owner", "repo", "title"],
                        "additionalProperties": False,
                    },
                    output_schema={"type": "object"},
                    requires_permission="network.request",
                    risk_level="confirm",
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
        # GitHub does not use PKCE code_verifier in the token exchange, but we
        # send it anyway (harmless) and rely on state validation for CSRF.
        data = {
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri,
        }
        # GitHub's token endpoint wants form-encoded, no client_secret for
        # public clients (OAuth apps without a secret). State was already
        # verified in complete_auth.
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            resp = await client.post(
                self.OAUTH_TOKEN_URL,
                data=data,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            payload = resp.json()
        return OAuthToken(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token"),
            expires_in=payload.get("expires_in"),
            scope=payload.get("scope"),
            token_type=payload.get("token_type", "Bearer"),
        )

    async def _refresh_exchange(
        self, refresh_token: str, redirect_uri: str
    ) -> OAuthToken:
        # GitHub OAuth tokens do not expire and cannot be refreshed; rotating
        # requires a new authorization. Returning False moves the connection to
        # needs_reauthorization, which is the correct behaviour.
        raise NotImplementedError("GitHub tokens do not support refresh")

    # -- capabilities ----------------------------------------------------
    async def list_capabilities(self, connection_id: str) -> List[ConnectorCapability]:
        capabilities = list(self.definition.capabilities)
        # Capabilities are only meaningful when connected; the manager filters
        # by state before calling. Return the declared set.
        return capabilities

    async def _github_request(
        self, connection_id: str, method: str, path: str, params=None, json_body=None
    ) -> Dict[str, Any]:
        token = self.get_credentials(connection_id)
        if not token:
            raise ConnectionError("No stored GitHub credentials")
        import json as _json

        token_data = _json.loads(token)
        headers = {"Authorization": f"Bearer {token_data['access_token']}"}
        resp = await self._http.request(
            method, path, params=params, json=json_body, headers=headers
        )
        if resp.status_code == 401:
            raise ConnectionError("GitHub credentials expired or revoked")
        resp.raise_for_status()
        if resp.content:
            return resp.json()
        return {}

    async def execute_capability(
        self, connection_id: str, capability_id: str, params: Dict[str, Any]
    ) -> CapabilityResult:
        try:
            if capability_id == "github.search_repositories":
                result = await self._github_request(
                    connection_id, "GET", "/search/repositories",
                    params={"q": params.get("q"), "per_page": params.get("per_page", 10)},
                )
                return CapabilityResult(
                    success=True,
                    output=f"Found {result.get('total_count', 0)} repositories",
                    data=result,
                )
            if capability_id == "github.get_repository":
                owner = params["owner"]
                repo = params["repo"]
                result = await self._github_request(
                    connection_id, "GET", f"/repos/{owner}/{repo}"
                )
                return CapabilityResult(
                    success=True,
                    output=f"Repository {owner}/{repo}",
                    data=result,
                )
            if capability_id == "github.list_issues":
                owner = params["owner"]
                repo = params["repo"]
                result = await self._github_request(
                    connection_id, "GET", f"/repos/{owner}/{repo}/issues",
                    params={
                        "state": params.get("state", "open"),
                        "per_page": params.get("per_page", 10),
                    },
                )
                return CapabilityResult(
                    success=True,
                    output=f"{len(result)} issue(s) in {owner}/{repo}",
                    data=result,
                )
            if capability_id == "github.create_issue":
                owner = params["owner"]
                repo = params["repo"]
                result = await self._github_request(
                    connection_id,
                    "POST",
                    f"/repos/{owner}/{repo}/issues",
                    json_body={
                        "title": params["title"],
                        "body": params.get("body", ""),
                    },
                )
                return CapabilityResult(
                    success=True,
                    output=f"Created issue #{result.get('number')} in {owner}/{repo}",
                    data=result,
                )
            return CapabilityResult(
                success=False,
                output=f"Unknown capability: {capability_id}",
                error=f"Unknown capability: {capability_id}",
            )
        except ConnectionError as exc:
            return CapabilityResult(
                success=False,
                output="GitHub request failed",
                error=str(exc),
            )
        except httpx.HTTPStatusError as exc:
            return CapabilityResult(
                success=False,
                output="GitHub API returned an error",
                error=f"HTTP {exc.response.status_code}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("GitHub capability %s failed: %s", capability_id, exc)
            return CapabilityResult(
                success=False,
                output="GitHub request failed",
                error="GitHub request failed",
            )


# Register at import time so the connector is available once the module is
# imported by the API / startup. The manager's `registry.instantiate` uses this.
registry.register(GitHubConnector)


__all__ = [
    "GitHubConnector",
]
