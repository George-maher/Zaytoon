"""Device authorization and pairing for remote access.

The same backend serves Desktop, Web, and Mobile clients. Remote clients must
not automatically gain unrestricted desktop control. This module implements:

1. Device pairing with explicit user approval
2. Permission scopes (READ_ONLY, VOICE, CHAT, SAFE_TOOLS, CONFIRM_TOOLS, FULL_CONTROL)
3. Token-based authentication for WebSocket connections
4. Session management and revocation
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set

from config import config

logger = logging.getLogger("Zaytoon.DeviceAuth")


class PermissionScope(str, Enum):
    """Permission scopes for paired devices, from least to most privileged."""

    READ_ONLY = "read_only"           # Can only read state, no actions
    VOICE = "voice"                   # Can use voice input/output
    CHAT = "chat"                     # Can send/receive chat messages
    SAFE_TOOLS = "safe_tools"         # Can use safe-tier tools (get_time, search_web, etc.)
    CONFIRM_TOOLS = "confirm_tools"   # Can request confirm-tier tools (needs user approval)
    FULL_CONTROL = "full_control"     # Can use all tools including dangerous (with approval)


# Scope hierarchy: each scope implies all scopes before it
SCOPE_HIERARCHY = [
    PermissionScope.READ_ONLY,
    PermissionScope.VOICE,
    PermissionScope.CHAT,
    PermissionScope.SAFE_TOOLS,
    PermissionScope.CONFIRM_TOOLS,
    PermissionScope.FULL_CONTROL,
]

SCOPE_RANK = {scope: i for i, scope in enumerate(SCOPE_HIERARCHY)}


@dataclass
class DevicePermission:
    """Permission grant for a paired device."""

    device_id: str
    device_name: str
    scope: PermissionScope
    token_hash: str  # bcrypt-style hash of the auth token
    paired_at: float = field(default_factory=time.time)
    last_used: float = 0
    revoked: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def check_token(self, token: str) -> bool:
        """Verify a plaintext token against the stored hash."""
        # Simple hash comparison (in production, use bcrypt/scrypt)
        test_hash = hashlib.sha256(token.encode()).hexdigest()
        return secrets.compare_digest(test_hash, self.token_hash)

    def has_scope(self, required: PermissionScope) -> bool:
        """Check if this permission includes the required scope."""
        if self.revoked:
            return False
        return SCOPE_RANK.get(self.scope, -1) >= SCOPE_RANK.get(required, -1)

    def to_dict(self, include_token: bool = False) -> Dict[str, Any]:
        """Serialize for API responses."""
        d = {
            "device_id": self.device_id,
            "device_name": self.device_name,
            "scope": self.scope.value,
            "paired_at": self.paired_at,
            "last_used": self.last_used,
            "revoked": self.revoked,
            "metadata": self.metadata,
        }
        if include_token:
            d["token_hash"] = self.token_hash
        return d


class DeviceAuthService:
    """Manages device pairing, authentication, and authorization."""

    def __init__(self):
        self._devices: Dict[str, DevicePermission] = {}  # device_id -> permission
        self._token_index: Dict[str, str] = {}  # token_hash -> device_id

    def pair_device(
        self,
        device_id: str,
        device_name: str,
        scope: PermissionScope = PermissionScope.CHAT,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> tuple[str, DevicePermission]:
        """Pair a new device, returning the plaintext token (shown once)."""
        # Generate secure token
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()

        permission = DevicePermission(
            device_id=device_id,
            device_name=device_name,
            scope=scope,
            token_hash=token_hash,
            metadata=metadata or {},
        )

        self._devices[device_id] = permission
        self._token_index[token_hash] = device_id

        logger.info("Paired device %s (%s) with scope %s", device_id, device_name, scope.value)
        return token, permission

    def authenticate(self, device_id: str, token: str) -> Optional[DevicePermission]:
        """Authenticate a device with its token."""
        permission = self._devices.get(device_id)
        if not permission or permission.revoked:
            return None

        if not permission.check_token(token):
            logger.warning("Failed auth attempt for device %s", device_id)
            return None

        permission.last_used = time.time()
        return permission

    def authenticate_ws(self, device_id: str, token: str) -> Optional[DevicePermission]:
        """Authenticate a WebSocket connection."""
        return self.authenticate(device_id, token)

    def get_device(self, device_id: str) -> Optional[DevicePermission]:
        """Get device permission by ID."""
        return self._devices.get(device_id)

    def list_devices(self) -> List[Dict[str, Any]]:
        """List all paired devices (without tokens)."""
        return [p.to_dict() for p in self._devices.values()]

    def revoke_device(self, device_id: str) -> bool:
        """Revoke a device's access."""
        permission = self._devices.get(device_id)
        if not permission:
            return False
        permission.revoked = True
        self._token_index.pop(permission.token_hash, None)
        logger.info("Revoked device %s", device_id)
        return True

    def update_scope(self, device_id: str, scope: PermissionScope) -> Optional[DevicePermission]:
        """Update a device's permission scope."""
        permission = self._devices.get(device_id)
        if not permission or permission.revoked:
            return None
        old_scope = permission.scope
        permission.scope = scope
        logger.info("Updated device %s scope: %s -> %s", device_id, old_scope.value, scope.value)
        return permission

    def check_permission(self, device_id: str, required: PermissionScope) -> bool:
        """Check if a device has the required permission scope."""
        permission = self._devices.get(device_id)
        if not permission:
            return False
        return permission.has_scope(required)

    def cleanup_stale(self, max_age_seconds: float = 86400 * 30) -> int:
        """Remove devices not used for max_age_seconds."""
        now = time.time()
        stale = [
            did for did, p in self._devices.items()
            if p.revoked or (now - p.last_used > max_age_seconds and p.last_used > 0)
        ]
        for did in stale:
            p = self._devices.pop(did, None)
            if p:
                self._token_index.pop(p.token_hash, None)
        return len(stale)


# Global singleton
_device_auth: Optional[DeviceAuthService] = None


def get_device_auth() -> DeviceAuthService:
    global _device_auth
    if _device_auth is None:
        _device_auth = DeviceAuthService()
    return _device_auth


def require_device_auth(device_id: str, token: str, required_scope: PermissionScope = PermissionScope.CHAT) -> DevicePermission:
    """Dependency for FastAPI routes: authenticate and authorize a device."""
    auth = get_device_auth()
    permission = auth.authenticate(device_id, token)
    if not permission:
        from fastapi import HTTPException
        raise HTTPException(401, "Invalid device credentials")
    if not permission.has_scope(required_scope):
        from fastapi import HTTPException
        raise HTTPException(403, f"Insufficient permissions: requires {required_scope.value}")
    return permission