"""Basic tests for device discovery and permission flows."""
import pytest

from core.discovery import (
    DeviceDiscoveryCoordinator,
    PermissionScope,
    PermissionManager,
    DeviceInventoryRepository,
    generate_device_identifier,
)
from core.inventory import DeviceInventoryRepository as InventoryRepo, default_inventory


def test_generate_device_identifier():
    """Device IDs should be stable and deterministic."""
    dev_id1 = generate_device_identifier()
    dev_id2 = generate_device_identifier()
    # Same machine, same credentials -> same ID
    assert dev_id1 == dev_id2
    # Should have the zaytoon- prefix
    assert dev_id1.startswith("zaytoon-")


def test_permission_manager():
    """Permission manager should track scope grants."""
    pm = PermissionManager()
    scopes = pm.list_scopes()
    # All scopes should be present
    assert len(scopes) == len(PermissionScope.all_scopes())
    # Initially nothing should be granted
    for scope_name in PermissionScope.all_scopes():
        assert not pm.is_granted(scope_name)


def test_default_inventory():
    """Default inventory should have the right structure."""
    inv = default_inventory()
    assert inv.inventory_version == "1.0"
    assert inv.discovery_scopes == PermissionScope.all_scopes()
    # Should have empty hardware by default
    assert inv.hardware.cpu_model == ""
    assert inv.hardware.os_family == ""


def test_device_inventory_repository_init():
    """Repository should initialize without errors."""
    repo = InventoryRepo()
    # Should be able to create without a db path
    assert repo is not None


def test_permission_scope_constants():
    """Permission scope constants should exist."""
    assert hasattr(PermissionScope, "HARDWARE")
    assert hasattr(PermissionScope, "APPLICATIONS")
    assert hasattr(PermissionScope, "DEVELOPMENT_ENV")
    assert hasattr(PermissionScope, "PROJECTS")
    assert hasattr(PermissionScope, "LOCAL_AI")
    assert hasattr(PermissionScope, "ALL")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])