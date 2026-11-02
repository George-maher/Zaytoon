"""OAuth 2.0 authorization-code flow with PKCE for connectors.

Security properties:

* **PKCE** — a code verifier/challenge pair is generated per authorization
  request, so an intercepted authorization code cannot be exchanged without it.
* **State validation** — a cryptographically random state nonce is bound to the
  connection and verified on callback, preventing CSRF.
* **Minimum scopes** — only the scopes the connector declares are requested;
  the user sees exactly what is being approved on the provider's consent page.
* **No tokens to frontend** — access/refresh tokens are stored server-side in
  :mod:`core.secrets` under a per-connection key. The API reports only
  ``has_credentials``.
* **Secure redirect** — the callback is handled entirely server-side; the
  frontend receives a redirect result, never the code or token.

This module provides :class:`OAuth2Connector`, a base that connectors
(Google Drive, GitHub) subclass. Subclasses supply:

* ``OAUTH_AUTHORIZATION_URL`` — provider authorize endpoint
* ``OAUTH_TOKEN_URL`` — provider token endpoint
* ``OAUTH_SCOPES`` — the OAuth scopes to request
* ``OAUTH_CLIENT_ID`` — OAuth client id (configured per deployment)
* ``_token_exchange(...)`` — exchange code for tokens (httpx call to provider)
* ``_refresh_exchange(...)`` — refresh an access token
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from abc import abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Optional

from core.connections.connector_interface import BaseConnector, CapabilityResult, ConnectionState

logger = logging.getLogger("Zaytoon.Connections.Auth.OAuth")


@dataclass
class OAuthToken:
    """An OAuth token pair, held in memory only long enough to store."""

    access_token: str
    refresh_token: Optional[str]
    expires_in: Optional[int]  # seconds
    scope: Optional[str]
    token_type: str = "Bearer"

    @property
    def expired(self) -> bool:
        if self.expires_in is None:
            return False
        return time.time() >= getattr(self, "_expires_at", float("inf"))

    def store(self, connection_id: str, connector_id: str) -> None:
        from core.secrets import set_secret

        key = f"connections.{connector_id}.{connection_id}"
        payload = {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_in": self.expires_in,
            "scope": self.scope,
            "token_type": self.token_type,
            "obtained_at": time.time(),
        }
        set_secret(key, json.dumps(payload))


class OAuth2Connector(BaseConnector):
    """Base for OAuth 2.0 (authorization code + PKCE) connectors."""

    #: Subclasses must set these class attributes.
    OAUTH_AUTHORIZATION_URL: str = ""
    OAUTH_TOKEN_URL: str = ""
    OAUTH_SCOPES: list = []
    OAUTH_CLIENT_ID: str = ""

    # -- connector interface ---------------------------------------------
    async def validate_config(self, config: Dict[str, Any]) -> None:
        """OAuth connectors need no connector-specific config beyond scopes."""
        if not self.OAUTH_AUTHORIZATION_URL or not self.OAUTH_TOKEN_URL:
            raise ValueError(
                f"{self.connector_id()} is not configured with OAuth endpoints"
            )
        if not self.OAUTH_CLIENT_ID:
            raise ValueError(f"{self.connector_id()} has no OAuth client id")

    async def start_auth(
        self, connection_id: str, redirect_uri: str
    ) -> Dict[str, Any]:
        """Build the authorization URL with PKCE + state.

        The state nonce is stored server-side (tied to the connection) and
        verified on callback. Never returns a token.
        """
        if not self.OAUTH_AUTHORIZATION_URL:
            raise ValueError(
                f"{self.connector_id()} has no OAuth authorization URL"
            )

        code_verifier = _new_code_verifier()
        code_challenge = _code_challenge(code_verifier)
        state = secrets.token_urlsafe(32)

        # Bind state + verifier to this connection for callback verification.
        _AUTH_PENDING[connection_id] = {
            "code_verifier": code_verifier,
            "state": state,
            "connector_id": self.connector_id(),
            "redirect_uri": redirect_uri,
            "created_at": time.time(),
        }

        params = {
            "response_type": "code",
            "client_id": self.OAUTH_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": " ".join(self.OAUTH_SCOPES),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",  # request a refresh token where supported
            "prompt": "consent",  # always show the consent screen
        }
        url = self.OAUTH_AUTHORIZATION_URL + "?" + _encode_params(params)
        logger.info("OAuth start for %s (connection %s)", self.connector_id(), connection_id)
        return {
            "auth_url": url,
            "state": state,
            "connection_id": connection_id,
            "connector_id": self.connector_id(),
        }

    async def complete_auth(
        self, connection_id: str, auth_response: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Exchange the authorization code for tokens.

        ``auth_response`` carries ``code`` and ``state`` from the provider
        callback (delivered server-side). Verifies state, exchanges the code,
        stores tokens via secrets.py, returns the connection state.
        """
        pending = _AUTH_PENDING.pop(connection_id, None)
        if pending is None:
            logger.warning("OAuth complete with no pending auth for %s", connection_id)
            return {"state": ConnectionState.ERROR.value, "error": "No pending authorization"}

        # 1. State validation — CSRF protection.
        if auth_response.get("state") != pending["state"]:
            logger.warning("OAuth state mismatch for %s", connection_id)
            return {"state": ConnectionState.ERROR.value, "error": "Invalid authorization state"}

        # 2. Provider error (user declined, etc.).
        if auth_response.get("error"):
            return {
                "state": ConnectionState.NEEDS_REAUTHORIZATION.value,
                "error": auth_response.get("error_description")
                or auth_response.get("error"),
            }

        code = auth_response.get("code")
        if not code:
            return {"state": ConnectionState.ERROR.value, "error": "No authorization code"}

        # 3. Exchange code for tokens (server-to-server, never via frontend).
        try:
            token = await self._token_exchange(
                client_id=self.OAUTH_CLIENT_ID,
                code=code,
                code_verifier=pending["code_verifier"],
                redirect_uri=pending["redirect_uri"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("OAuth token exchange failed for %s: %s", connection_id, exc)
            return {"state": ConnectionState.ERROR.value, "error": "Token exchange failed"}

        # 4. Store tokens server-side only.
        token.store(connection_id, self.connector_id())

        scopes = (token.scope or " ").split() if token.scope else list(self.OAUTH_SCOPES)
        return {
            "state": ConnectionState.CONNECTED.value,
            "scopes": scopes,
        }

    async def test_connection(self, connection_id: str) -> Dict[str, Any]:
        """Lightweight check: do we have stored credentials?"""
        if not self.has_credentials(connection_id):
            return {"ok": False, "error": "No stored credentials"}
        return {"ok": True}

    async def refresh_credentials(self, connection_id: str) -> bool:
        """Refresh an expired access token using the stored refresh token.

        Connectors that do not support refresh return False, which moves the
        connection to ``needs_reauthorization``.
        """
        stored = self.get_credentials(connection_id)
        if not stored:
            return False
        try:
            data = json.loads(stored)
        except (ValueError, TypeError):
            return False
        refresh_token = data.get("refresh_token")
        if not refresh_token:
            return False
        try:
            token = await self._refresh_exchange(
                refresh_token=refresh_token,
                redirect_uri=_AUTH_PENDING.get(connection_id, {}).get("redirect_uri", ""),
            )
            token.store(connection_id, self.connector_id())
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("OAuth refresh failed for %s: %s", connection_id, exc)
            return False

    async def disconnect(self, connection_id: str) -> None:
        """Remove stored credentials (best-effort revocation at provider optional)."""
        self.delete_credentials(connection_id)

    # -- subclass hooks --------------------------------------------------
    @abstractmethod
    async def _token_exchange(
        self,
        client_id: str,
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> OAuthToken:
        """Exchange an authorization code for tokens. Subclasses implement."""

    @abstractmethod
    async def _refresh_exchange(
        self, refresh_token: str, redirect_uri: str
    ) -> OAuthToken:
        """Refresh an access token. Subclasses implement."""


# ---------------------------------------------------------------------------
# In-flight authorization state (server-side only)
# ---------------------------------------------------------------------------
# Keyed by connection_id. Lives only until complete_auth is called, then
# removed. Never serialized, never sent to the frontend.
_AUTH_PENDING: Dict[str, Dict[str, Any]] = {}


def _new_code_verifier() -> str:
    """PKCE code verifier: 43–128 chars, unreserved."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


def _code_challenge(code_verifier: str) -> str:
    """PKCE code challenge: SHA-256(verifier), base64url no padding."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _encode_params(params: Dict[str, Any]) -> str:
    from urllib.parse import urlencode

    return urlencode({k: v for k, v in params.items() if v is not None and v != ""})


__all__ = [
    "OAuthToken",
    "OAuth2Connector",
]
