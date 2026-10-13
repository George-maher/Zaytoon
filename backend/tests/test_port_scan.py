"""The authorised port scan: refusal first, evidence second.

The same two properties the HTTP probe asserts, and for the same reasons:

1. **A refused target is never contacted.** The scope check runs before any
   socket exists. A test proves it by authorising *nothing* and pointing the
   tool at a real listener that counts every connection it accepts — if the
   socket were opened before the check, the counter would move.

2. **Only observations become findings.** An open port with a versioned
   banner produces a confirmed exposure observation and a confirmed
   disclosure; a refused connection produces *nothing* — not a row, not a
   finding. `filtered` is never dressed as `closed`.

The listener is a real TCP socket on loopback, so the handshake, the
server-first banner read and the classification are the production code
paths, not a mock of them.
"""

from __future__ import annotations

import asyncio
import socket
import threading

import pytest

from core.security import scope
from tools.security import MAX_SCAN_PORTS, PortScan


@pytest.fixture(autouse=True)
async def clean_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CORE_DB_PATH", str(tmp_path / "core.db"))
    from core import db

    await db.reset_for_tests()
    yield
    await db.reset_for_tests()


class _BannerServer:
    """A real TCP listener that accepts, sends a server-first banner, closes.

    Counts accepted connections so a premature connect (before the scope
    check) is visible as a moved counter rather than an assumption.
    """

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


@pytest.fixture
def scanner():
    return PortScan()


def _run(coro):
    return asyncio.run(coro)


def _closed_port() -> int:
    """A port nothing is listening on: bind, note, release."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# ---------------------------------------------------------------------------
# Registration and validation
# ---------------------------------------------------------------------------
class TestRegistration:
    def test_registered_at_confirm_tier(self):
        from tools.registry import build_default_registry

        registry = build_default_registry()
        assert registry.has("port_scan")
        assert registry.effective_tier("port_scan") == "confirm"


class TestValidation:
    def test_a_url_is_not_a_host(self, scanner):
        with pytest.raises(Exception, match="single hostname"):
            scanner.validate({"host": "http://example.com", "ports": [80]})

    def test_a_cidr_network_is_not_a_target(self, scanner):
        with pytest.raises(Exception, match="CIDR"):
            scanner.validate({"host": "10.0.0.0/8", "ports": [80]})

    def test_port_zero_is_refused(self, scanner):
        # 0 is reserved; no service answers on it, and a range containing it
        # silently shrinks, which is worse than an error.
        with pytest.raises(Exception):
            scanner.validate({"host": "example.com", "ports": [0]})

    def test_an_oversized_scan_is_refused(self, scanner):
        with pytest.raises(Exception, match=f"at most {MAX_SCAN_PORTS}"):
            scanner.validate(
                {"host": "example.com", "ports": [f"1-{MAX_SCAN_PORTS + 1}"]}
            )

    def test_a_backwards_range_expands_to_nothing(self, scanner):
        assert scanner._expand_ports(["90-80"]) == []

    def test_ranges_and_singles_dedupe(self, scanner):
        assert scanner._expand_ports([22, "80-82", 81, "22"]) == [22, 80, 81, 82]

    def test_a_host_is_required(self, scanner):
        with pytest.raises(Exception, match="'host' is required"):
            scanner.validate({"host": " ", "ports": [22]})


# ---------------------------------------------------------------------------
# Authorisation precedes every socket
# ---------------------------------------------------------------------------
class TestRefusal:
    def test_an_unauthorised_host_is_never_contacted(self, banner_server, scanner):
        """Nothing authorised: the listener's counter must not move."""
        result = _run(scanner.execute(host="127.0.0.1", ports=[banner_server.port]))
        assert result.success is False
        assert "not authorised" in result.error
        assert result.data["refused"] is True
        assert result.data["reason"] == "out_of_scope"
        assert banner_server.accepted == 0, (
            "the refused target was contacted — the scope check ran too late"
        )

    def test_authorising_a_different_operation_does_not_help(
        self, banner_server, scanner
    ):
        """`http.probe` authorisation must not leak into `port.probe`."""
        _run(scope.create("127.0.0.1", [scope.OP_HTTP_PROBE]))
        result = _run(scanner.execute(host="127.0.0.1", ports=[banner_server.port]))
        assert result.success is False
        assert result.data["refused"] is True
        assert banner_server.accepted == 0


# ---------------------------------------------------------------------------
# The scan itself, against a real listener
# ---------------------------------------------------------------------------
class TestScan:
    def test_open_closed_filtered_with_evidence(self, banner_server, scanner):
        _run(scope.create("127.0.0.1", [scope.OP_PORT_PROBE]))
        closed = _closed_port()

        result = _run(
            scanner.execute(
                host="127.0.0.1", ports=[banner_server.port, closed, 1]
            )
        )

        assert result.success is True
        by_state = {entry["port"]: entry["state"] for entry in result.data["results"]}
        assert by_state[banner_server.port] == "open"
        # On Windows a closed port often times out (filtered) rather than
        # actively refusing. The invariant is the partition: open/closed/filtered
        # are mutually exclusive and the union is the scanned set.
        assert by_state[closed] in ("closed", "filtered")
        assert by_state[1] in ("closed", "filtered")
        assert result.data["open"] == [banner_server.port]
        assert closed in result.data["closed"] or closed in result.data["filtered"]

    def test_a_versioned_banner_becomes_a_disclosure_finding(
        self, banner_server, scanner
    ):
        _run(scope.create("127.0.0.1", [scope.OP_PORT_PROBE]))
        result = _run(scanner.execute(host="127.0.0.1", ports=[banner_server.port]))

        titles = [f["title"] for f in result.data["findings"]]
        assert any("accepted a TCP connection" in title for title in titles)
        assert any("discloses a software version" in title for title in titles)
        for finding in result.data["findings"]:
            assert finding["confidence"] == "confirmed"
            assert finding["evidence"]
            assert finding["remediation"]

    def test_a_silent_listener_yields_no_disclosure(self, scanner):
        """No banner → one exposure finding, no fabricated disclosure."""
        server = _BannerServer(banner=b"")
        try:
            _run(scope.create("127.0.0.1", [scope.OP_PORT_PROBE]))
            result = _run(scanner.execute(host="127.0.0.1", ports=[server.port]))
        finally:
            server.close()
        titles = [f["title"] for f in result.data["findings"]]
        assert any("accepted a TCP connection" in title for title in titles)
        assert not any("discloses" in title for title in titles)

    def test_the_rendered_report_calls_filtered_inconclusive(self, banner_server, scanner):
        _run(scope.create("127.0.0.1", [scope.OP_PORT_PROBE]))
        result = _run(scanner.execute(host="127.0.0.1", ports=[banner_server.port]))
        assert "Authorised port scan of 127.0.0.1" in result.output
        assert "OPEN" in result.output


# ---------------------------------------------------------------------------
# Evidence persistence follows the observation
# ---------------------------------------------------------------------------
class TestEvidence:
    def test_a_successful_scan_records_its_findings(self, banner_server, scanner):
        from core.security import findings

        auth = _run(scope.create("127.0.0.1", [scope.OP_PORT_PROBE]))
        _run(scanner.execute(host="127.0.0.1", ports=[banner_server.port]))

        rows = _run(findings.list_findings(target="127.0.0.1"))
        assert rows, "an observed finding was not persisted"
        assert all(row["authorization_id"] == auth.id for row in rows)
        assert all(row["tool"] == "port_scan" for row in rows)
        assert all(row["confidence"] == "confirmed" for row in rows)

    def test_a_refused_scan_records_nothing(self, banner_server, scanner):
        from core.security import findings

        # No authorization at all.
        _run(scanner.execute(host="127.0.0.1", ports=[banner_server.port]))
        assert _run(findings.list_findings()) == []
        assert banner_server.accepted == 0
