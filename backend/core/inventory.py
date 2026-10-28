"""Device inventory model, persistence and refresh scheduling.

This module used to be a **second copy** of :mod:`core.discovery`: its own
``HardwareInfo``/``ApplicationInfo``/``DeviceInventory`` dataclasses and its own
``DeviceInventoryRepository``. The two disagreed the moment discovery grew a
field — discovery's ``ApplicationInfo`` learns the launch ``target`` and the
copy here does not, so a caller that imported the inventory version silently
lost the one field a launcher needs. Worse, the copy's ``load()`` referenced
``json`` while importing it only inside the sibling function, so every read
raised ``NameError`` and the round-trip was untested.

The types therefore live in one place. This module keeps the inventory-facing
names (``DiscoveryScope``, ``InventoryState``, ``default_inventory``,
``InventoryRefreshScheduler``) and re-exports the shared model, so existing
imports keep working without a second definition existing.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, List, Optional

from core.discovery import (
    ApplicationInfo,
    DeviceInventory,
    DeviceInventoryRepository,
    DevelopmentTool,
    HardwareInfo,
    LocalAIModel,
    PermissionScope,
    ProcessInfo,
    ProjectInfo,
    generate_device_identifier,
)

logger = logging.getLogger("Zaytoon.Inventory")

__all__ = [
    "ApplicationInfo",
    "DeviceInventory",
    "DeviceInventoryRepository",
    "DevelopmentTool",
    "DiscoveryScope",
    "HardwareInfo",
    "InventoryRefreshScheduler",
    "InventoryState",
    "LocalAIModel",
    "ProcessInfo",
    "ProjectInfo",
    "default_inventory",
    "generate_device_identifier",
    "inventory_state",
]


# ---------------------------------------------------------------------------
# Permission scopes
# ---------------------------------------------------------------------------

class DiscoveryScope:
    """Discovery permission scopes.

    ``PermissionScope`` in :mod:`core.discovery` is the authority; this is the
    same vocabulary under the name the inventory API has always used.
    """

    HARDWARE = PermissionScope.HARDWARE
    APPLICATIONS = PermissionScope.APPLICATIONS
    RUNNING_PROCESSES = PermissionScope.RUNNING_PROCESSES
    DEVELOPMENT_ENV = PermissionScope.DEVELOPMENT_ENV
    PROJECTS = PermissionScope.PROJECTS
    LOCAL_AI = PermissionScope.LOCAL_AI
    USER_CONSENT = "user_consent"

    ALL = PermissionScope.all_scopes()


# ---------------------------------------------------------------------------
# Inventory refresh scheduler
# ---------------------------------------------------------------------------

class InventoryRefreshScheduler:
    """Scheduled refresh of device inventory.

    The loop is a *staleness marker*: it does not scan in the background (a
    scan is expensive and belongs to a request that asked for one), it simply
    wakes periodically so a caller can decide the cached record is old. That is
    also why its imports are declared — ``asyncio`` and ``logger`` were both
    used and neither imported, so constructing the scheduler and starting it
    raised ``NameError`` before any refresh could happen.
    """

    def __init__(self, repository: DeviceInventoryRepository, ttl_hours: int = 24):
        self._repository = repository
        self._ttl_hours = ttl_hours
        self._task: Optional[asyncio.Task] = None
        self._running = False

    @property
    def ttl_seconds(self) -> int:
        return max(1, int(self._ttl_hours)) * 3600

    async def start(self) -> None:
        """Start the refresh scheduler."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._refresh_loop())
        logger.info(
            "Inventory refresh scheduler started (every %d hours)", self._ttl_hours
        )

    async def stop(self) -> None:
        """Stop the refresh scheduler."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Inventory refresh scheduler stopped")

    async def _refresh_loop(self) -> None:
        """Background loop that periodically marks the inventory stale."""
        while self._running:
            try:
                await asyncio.sleep(self.ttl_seconds)
                # Refresh is triggered by a request when the inventory is stale.
            except asyncio.CancelledError:
                break
            except Exception as e:  # never let a marker loop die loudly
                logger.warning("Inventory refresh loop error: %s", e)


# ---------------------------------------------------------------------------
# Inventory state enum
# ---------------------------------------------------------------------------

class InventoryState:
    """States of a device inventory."""

    NOT_AUTHORIZED = "not_authorized"
    NOT_SCANNED = "not_scanned"
    SCANNING = "scanning"
    PARTIALLY_AVAILABLE = "partially_available"
    READY = "ready"
    NEEDS_REFRESH = "needs_refresh"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

def default_inventory() -> DeviceInventory:
    """Create a default (empty) inventory."""
    return DeviceInventory(
        timestamp=datetime.now(),
        hardware=HardwareInfo(),
        discovery_scopes=DiscoveryScope.ALL,
    )


def inventory_state(inventory: DeviceInventory, pm: Any) -> str:
    """Determine the current state of an inventory."""
    if not pm.is_granted(DiscoveryScope.USER_CONSENT):
        return InventoryState.NOT_AUTHORIZED
    if not inventory.hardware.os_family:
        return InventoryState.NOT_SCANNED
    if not inventory.applications and not inventory.development_tools:
        return InventoryState.FAILED
    if not inventory.errors:
        return InventoryState.READY
    return InventoryState.NEEDS_REFRESH
