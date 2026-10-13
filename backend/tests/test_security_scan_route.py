"""The scan endpoint: the same two-step approval, over HTTP.

The contract mirrors ``/security/probe`` exactly — the same two-step
shape, the same no-socket step 1, the same real gate on step 2 — so the
tests are deliberately parallel. The listener is a real TCP socket.
"""

from __future__ import annotations

import socket
import threading

import pytest

from core.security import scope
from core import db as core_db


@pytest.fixture(autouse=True)
async def clean_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CORE_DB_PATH", str(tmp_path / "core.db"))
    await core_db.reset_for_tests()
    from core.api import registry_api

    registry_api._PROBE_TOKENS.clear()
    yield
    await core_db.reset_for_tests()
    registry_api._PROBE_TOKENS.clear()


@pytest.fixture
def client():
    from api.main import app
    from fastapi.testclient import TestClient

    return TestClient(app)


class _BannerServer:
    def __init__(self, banner: bytes = b"SSH-2.0-TestService_1.2.3\r\n"):
        self.banner = banner
        self.accepted = 0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        self._sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.accepted += 1
            try:
                conn.sendall(self.banner)
            except OSError:
                pass
            finally:
                conn.close()

    def close(self):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)


@pytest.fixture
def banner_server():
    server = _BannerServer()
    yield server
    server.close()


class TestScanRoute:
    def test_step1_no_socket_before_approval(self, banner_server, client):
        """Step 1 returns a token; the listener's counter does not move.

        Scope check runs first, so an authorization is required even for the
        preview — this matches /security/probe's behaviour.
        """
        import asyncio
        from core.security import scope

        asyncio.run(scope.create("127.0.0.1", [scope.OP_PORT_PROBE]))

        resp = client.post("/security/scan", json={"host": "127.0.0.1", "ports": [banner_server.port]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["requires_confirmation"] is True
        assert "confirm" in data
        assert data["host"] == "127.0.0.1"
        assert data["ports"] == [banner_server.port]
        assert banner_server.accepted == 0, "step 1 must not open a socket"

    def test_step2_refused_without_authorization(self, banner_server, client):
        """Step 2 with a fresh token still refuses if not authorised."""
        import asyncio
        from core.security import scope

        # First authorize, then revoke — so step 1 gets a token, but step 2's
        # scope check finds no active authorization.
        async def _setup():
            await scope.create("127.0.0.1", [scope.OP_PORT_PROBE])
        asyncio.run(_setup())
        resp = client.post("/security/scan", json={"host": "127.0.0.1", "ports": [banner_server.port]})
        token = resp.json()["confirm"]

        # Revoke the authorization so step 2 fails
        async def _revoke():
            all_auth = await scope.list_all()
            if all_auth:
                await scope.revoke(all_auth[0].id)
        asyncio.run(_revoke())

        resp = client.post(
            "/security/scan",
            json={"host": "127.0.0.1", "ports": [banner_server.port], "confirm": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["refused"] is True
        assert data["reason"] == "out_of_scope"
        assert banner_server.accepted == 0, "out-of-scope is never contacted"

    def test_step2_approved_and_run(self, banner_server, client):
        """Authorized + token → scan runs and returns findings."""
        import asyncio
        from core.security import scope

        asyncio.run(scope.create("127.0.0.1", [scope.OP_PORT_PROBE]))

        # Step 1
        resp = client.post("/security/scan", json={"host": "127.0.0.1", "ports": [banner_server.port]})
        token = resp.json()["confirm"]

        # Step 2
        resp = client.post(
            "/security/scan",
            json={"host": "127.0.0.1", "ports": [banner_server.port], "confirm": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "data" in data
        assert "results" in data["data"]
        assert data["data"]["open"] == [banner_server.port]
        assert banner_server.accepted == 1, "one real connection accepted"
        # Evidence recorded
        assert data["authorization_id"]
        assert data["data"]["findings"]
        assert any("accepted a TCP connection" in f["title"] for f in data["data"]["findings"])

    def test_a_token_from_the_probe_endpoint_can_be_spent_here(
        self, banner_server, client
    ):
        """Tokens ARE shared: same manager, same prefix. A probe token works for scan."""
        import asyncio
        from core.security import scope

        asyncio.run(scope.create("127.0.0.1", [scope.OP_HTTP_PROBE, scope.OP_PORT_PROBE]))
        resp = client.post("/security/probe", json={"url": f"http://127.0.0.1:{banner_server.port}"})
        probe_token = resp.json()["confirm"]

        resp = client.post(
            "/security/scan",
            json={"host": "127.0.0.1", "ports": [banner_server.port], "confirm": probe_token},
        )
        # Current behaviour: shared manager, so the token is accepted.
        assert resp.status_code == 200
        assert resp.json().get("success") is True


class TestFindingsRoute:
    def test_findings_empty_initially(self, client):
        resp = client.get("/security/findings")
        assert resp.status_code == 200
        assert resp.json() == {"findings": [], "count": 0}

    def test_findings_recorded_after_scan(self, banner_server, client):
        import asyncio
        from core.security import scope

        asyncio.run(scope.create("127.0.0.1", [scope.OP_PORT_PROBE]))
        token = client.post("/security/scan", json={"host": "127.0.0.1", "ports": [banner_server.port]}).json()["confirm"]
        client.post("/security/scan", json={"host": "127.0.0.1", "ports": [banner_server.port], "confirm": token})

        resp = client.get("/security/findings?target=127.0.0.1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] > 0
        assert all(f["tool"] == "port_scan" for f in data["findings"])
        assert all(f["authorization_id"] for f in data["findings"])

    def test_findings_clear_all(self, banner_server, client):
        import asyncio
        from core.security import scope

        asyncio.run(scope.create("127.0.0.1", [scope.OP_PORT_PROBE]))
        token = client.post("/security/scan", json={"host": "127.0.0.1", "ports": [banner_server.port]}).json()["confirm"]
        client.post("/security/scan", json={"host": "127.0.0.1", "ports": [banner_server.port], "confirm": token})

        resp = client.delete("/security/findings")
        assert resp.status_code == 200
        assert resp.json()["removed"] > 0

        resp = client.get("/security/findings")
        assert resp.json()["count"] == 0

    def test_findings_clear_one_target(self, banner_server, client):
        import asyncio
        from core.security import scope

        asyncio.run(scope.create("127.0.0.1", [scope.OP_PORT_PROBE]))
        asyncio.run(scope.create("example.com", [scope.OP_PORT_PROBE]))
        token = client.post("/security/scan", json={"host": "127.0.0.1", "ports": [banner_server.port]}).json()["confirm"]
        client.post("/security/scan", json={"host": "127.0.0.1", "ports": [banner_server.port], "confirm": token})

        resp = client.delete("/security/findings?target=127.0.0.1")
        assert resp.status_code == 200
        assert resp.json()["target"] == "127.0.0.1"


class TestReportRoute:
    def test_report_json(self, banner_server, client):
        import asyncio
        from core.security import scope

        asyncio.run(scope.create("127.0.0.1", [scope.OP_PORT_PROBE]))
        token = client.post("/security/scan", json={"host": "127.0.0.1", "ports": [banner_server.port]}).json()["confirm"]
        client.post("/security/scan", json={"host": "127.0.0.1", "ports": [banner_server.port], "confirm": token})

        resp = client.get("/security/report")
        assert resp.status_code == 200
        data = resp.json()
        assert "generated_at" in data
        assert "scope" in data
        assert "methodology" in data
        assert "findings" in data
        assert "limitations" in data
        assert data["total"] > 0

    def test_report_markdown(self, banner_server, client):
        import asyncio
        from core.security import scope

        asyncio.run(scope.create("127.0.0.1", [scope.OP_PORT_PROBE]))
        token = client.post("/security/scan", json={"host": "127.0.0.1", "ports": [banner_server.port]}).json()["confirm"]
        client.post("/security/scan", json={"host": "127.0.0.1", "ports": [banner_server.port], "confirm": token})

        resp = client.get("/security/report?format=markdown")
        assert resp.status_code == 200
        data = resp.json()
        assert "markdown" in data
        assert "Zaytoon authorised assessment report" in data["markdown"]
        # The port is randomly assigned; check for the finding pattern
        assert "accepted a TCP connection" in data["markdown"]
        assert "discloses a software version" in data["markdown"]


class TestValidation:
    def test_host_is_required(self, client):
        assert client.post("/security/scan", json={"ports": [22]}).status_code == 422

    def test_ports_is_required(self, client):
        assert client.post("/security/scan", json={"host": "127.0.0.1"}).status_code == 422

    def test_ports_must_be_non_empty_list(self, client):
        assert client.post("/security/scan", json={"host": "127.0.0.1", "ports": []}).status_code == 422

    def test_oversized_range_is_refused(self, client):
        resp = client.post("/security/scan", json={"host": "127.0.0.1", "ports": ["1-33"]})
        assert resp.status_code == 422
        assert "at most 32" in resp.json()["error"]

    def test_backwards_range_is_refused(self, client):
        resp = client.post("/security/scan", json={"host": "127.0.0.1", "ports": ["90-80"]})
        assert resp.status_code == 422
        assert "backwards" in resp.json()["error"]

    def test_a_url_is_not_a_host(self, client):
        resp = client.post("/security/scan", json={"host": "http://example.com", "ports": [80]})
        assert resp.status_code == 422
        assert "single hostname" in resp.json()["error"]

    def test_a_cidr_is_not_a_host(self, client):
        resp = client.post("/security/scan", json={"host": "10.0.0.0/8", "ports": [80]})
        assert resp.status_code == 422
        assert "CIDR" in resp.json()["error"]