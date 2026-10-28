"""The device-discovery REST routes.

All three routes were broken in ways no test noticed:

* ``/device/inventory`` called ``loop.run_until_complete`` from inside a running
  event loop, which raises ``RuntimeError`` — the route could not answer at all;
* ``/device/discovery-status`` referenced ``generate_device_identifier`` and the
  discovery ``PermissionScope`` without importing either, *and* ran the full
  discovery pipeline on every poll, so a status read was a Start Menu walk;
* ``/device/permissions`` imported a manager it could not construct.

The tests drive the real FastAPI app and fake only the repository, so the
serialisation and the "status must not scan" rule are exercised as served.
"""

from __future__ import annotations

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api.main as main_module  # noqa: E402
from core import discovery  # noqa: E402
from core.discovery import ApplicationInfo, DeviceInventory, HardwareInfo  # noqa: E402


@pytest.fixture(scope="module")
def client():
    return TestClient(main_module.app)


def _inventory() -> DeviceInventory:
    inventory = DeviceInventory(
        timestamp=discovery.datetime(2026, 1, 2, 3, 4, 5),
        hardware=HardwareInfo(cpu_model="probe-cpu", os_family="windows"),
        applications=[
            ApplicationInfo(
                name="chrome",
                display_name="Google Chrome",
                discovery_source="start_menu",
                target=r"C:\Start\Google Chrome.lnk",
                verified=True,
            )
        ],
        discovery_scopes=["hardware", "applications"],
    )
    return inventory


@pytest.fixture
def fake_repository(monkeypatch):
    """Replace the repository with one that answers from memory."""

    class _Repo:
        inventory = None
        seen: list[str] = []

        def __init__(self, *args, **kwargs):
            pass

        async def load(self, device_id):
            _Repo.seen.append(device_id)
            return _Repo.inventory

        async def latest(self, device_id):
            return await self.load(device_id)

    _Repo.inventory = None
    _Repo.seen = []
    monkeypatch.setattr(discovery, "DeviceInventoryRepository", _Repo)
    return _Repo


class TestDeviceInventory:
    def test_an_empty_registry_is_an_honest_null(self, client, fake_repository):
        response = client.get("/device/inventory")
        assert response.status_code == 200
        body = response.json()
        assert body["inventory"] is None
        assert "No inventory" in body["message"]

    def test_a_stored_inventory_is_returned_as_plain_data(self, client, fake_repository):
        fake_repository.inventory = _inventory()
        response = client.get("/device/inventory")
        assert response.status_code == 200
        body = response.json()["inventory"]
        assert body["hardware"]["cpu_model"] == "probe-cpu"
        assert body["applications"][0]["display_name"] == "Google Chrome"
        assert body["applications"][0]["target"] == r"C:\Start\Google Chrome.lnk"
        assert body["discovery_scopes"] == ["hardware", "applications"]

    def test_the_device_identifier_is_used_for_the_lookup(self, client, fake_repository):
        client.get("/device/inventory")
        assert fake_repository.seen == [discovery.generate_device_identifier()]


class TestDevicePermissions:
    def test_every_scope_is_listed(self, client):
        response = client.get("/device/permissions")
        assert response.status_code == 200
        body = response.json()
        assert set(body["scopes"]) == {
            "hardware",
            "applications",
            "running_processes",
            "development_environment",
            "projects",
            "local_ai",
        }
        assert isinstance(body["permissions"], dict)


class TestDiscoveryStatus:
    def test_a_status_read_never_runs_a_scan(self, client, fake_repository, monkeypatch):
        def _explode(*args, **kwargs):  # pragma: no cover - only runs on a bug
            raise AssertionError("a status read ran the discovery pipeline")

        monkeypatch.setattr(
            discovery.DeviceDiscoveryCoordinator, "discover_all", _explode
        )
        response = client.get("/device/discovery-status")
        assert response.status_code == 200

    def test_an_unscanned_device_says_so(self, client, fake_repository):
        body = client.get("/device/discovery-status").json()
        assert body["device_id"]
        assert body["state"] == "not_scanned"
        assert body["inventory"] is None
        assert body["discovery_scopes"]

    def test_a_scanned_device_reports_ready_with_its_inventory(
        self, client, fake_repository
    ):
        fake_repository.inventory = _inventory()
        body = client.get("/device/discovery-status").json()
        assert body["state"] == "ready"
        assert body["inventory"]["hardware"]["cpu_model"] == "probe-cpu"

    def test_granted_scopes_are_separated_from_every_scope(self, client, fake_repository):
        body = client.get("/device/discovery-status").json()
        assert set(body["granted_scopes"]).issubset(set(body["discovery_scopes"]))


class TestTheToolsSurface:
    def test_the_launcher_tools_are_advertised(self, client):
        names = {row["name"] for row in client.get("/tools").json()["tools"]}
        assert {"open_target", "find_paths", "list_apps", "refresh_inventory"} <= names

    def test_every_advertised_tool_has_a_tier_and_a_schema(self, client):
        for row in client.get("/tools").json()["tools"]:
            assert row["permission"] in ("safe", "confirm", "dangerous"), row
            assert row["schema"]["type"] == "object", row
