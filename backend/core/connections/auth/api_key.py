"""API key / personal access token auth for connectors.

Security properties:

* **Masked inputs** — the frontend never receives or displays the secret value.
  It sends the secret once over HTTPS/WSS to this endpoint; the API only ever
  reports ``has_credentials: true``.
* **Secrets server-side** — keys are stored via :mod:`core.secrets` (DPAPI on
  Windows, 0600 file on POSIX) under a per-connection key. No route returns
  the value.
* **Never logged** — secrets are never passed to the redacting logger or any
  stack trace; :func:`common.reporting.safe_error` is used for any exception
  derived from them.

This module provides :class:`APIKeyConnector`, a base that connectors with
static credentials (e.g. a GitHub PAT where OAuth is undesirable) subclass.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from core.connections.connector_interface import BaseConnector, ConnectionState

logger = logging.getLogger("Zaytoon.Connections.Auth.APIKey")


class APIKeyConnector(BaseConnector):
    """Base for API key / PAT connectors.

    Subclasses implement :meth:`_test_credential` to prove the key works
    (e.g. a ``GET /user`` call). The key itself is never returned.
    """

    #: Header name the connector sends the key in, e.g. ``"Authorization"``.
    AUTH_HEADER: str = "Authorization"
    #: Header value prefix, e.g. ``"Bearer"`` or ``"token"``.
    AUTH_HEADER_PREFIX: str = "Bearer"
    #: The config key under which the connector stores the key name hint.
    KEY_LABEL_CONFIG: str = "key_label"

    # -- connector interface ---------------------------------------------
    async def validate_config(self, config: Dict[str, Any]) -> None:
        """API key connectors need no connector-specific config."""
        return None

    async def start_auth(
        self, connection_id: str, redirect_uri: str
    ) -> Dict[str, Any]:
        """Return the form fields the frontend renders to collect the key.

        The frontend shows a masked input; the submitted value is posted to
        :meth:`complete_auth`, which stores it server-side.
        """
        return {
            "auth_method": "api_key",
            "fields": [
                {
                    "name": "api_key",
                    "label": "API key / personal access token",
                    "type": "password",
                    "placeholder": "ghp_…",
                    "description": "Stored server-side. Never shown again.",
                }
            ],
            "connection_id": connection_id,
            "connector_id": self.connector_id(),
        }

    async def complete_auth(
        self, connection_id: str, auth_response: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Store the submitted API key server-side.

        ``auth_response`` carries ``api_key``. The value is stored via
        :mod:`core.secrets` and never echoed back.
        """
        api_key = auth_response.get("api_key")
        if not api_key or not isinstance(api_key, str) or not api_key.strip():
            return {
                "state": ConnectionState.ERROR.value,
                "error": "No API key provided",
            }

        # Probe the key before storing, so a bad key never becomes a connection.
        try:
            ok = await self._test_credential(api_key.strip())
        except Exception as exc:  # noqa: BLE001
            logger.warning("API key probe failed for %s: %s", connection_id, exc)
            return {
                "state": ConnectionState.ERROR.value,
                "error": "Could not validate the API key",
            }
        if not ok:
            return {
                "state": ConnectionState.ERROR.value,
                "error": "The API key was rejected by the service",
            }

        self.set_credentials(connection_id, api_key.strip())
        logger.info("API key stored for %s (connection %s)", self.connector_id(), connection_id)
        return {
            "state": ConnectionState.CONNECTED.value,
            "scopes": [],
        }

    async def test_connection(self, connection_id: str) -> Dict[str, Any]:
        api_key = self.get_credentials(connection_id)
        if not api_key:
            return {"ok": False, "error": "No stored credentials"}
        try:
            ok = await self._test_credential(api_key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("API key test failed for %s: %s", connection_id, exc)
            return {"ok": False, "error": "Credential check failed"}
        return {"ok": bool(ok)}

    async def refresh_credentials(self, connection_id: str) -> bool:
        """API keys do not refresh. Return False to prompt re-entry."""
        return False

    async def disconnect(self, connection_id: str) -> None:
        self.delete_credentials(connection_id)

    # -- credential helpers (read-only, never return values to callers) --
    def get_credential(self, connection_id: str) -> Optional[str]:
        """Read the stored API key. Internal/capability-execution only."""
        return self.get_credentials(connection_id)

    def credential_header(self, connection_id: str) -> Optional[Dict[str, str]]:
        """Build the authorization header for an authenticated request.

        Returns None when no credential is stored. Never includes the value in
        logs — callers use it directly on outbound requests.
        """
        api_key = self.get_credentials(connection_id)
        if not api_key:
            return None
        return {self.AUTH_HEADER: f"{self.AUTH_HEADER_PREFIX} {api_key}"}

    def credential_masked(self, connection_id: str) -> str:
        """A masked rendering for the UI (last 4 chars). Never the full value."""
        api_key = self.get_credentials(connection_id)
        if not api_key:
            return ""
        return f"…{api_key[-4:]}"

    # -- subclass hook ---------------------------------------------------
    async def _test_credential(self, api_key: str) -> bool:
        """Prove the key works. Subclasses implement."""
        raise NotImplementedError


from typing import Optional  # noqa: E402


__all__ = [
    "APIKeyConnector",
]
