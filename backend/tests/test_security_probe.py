"""The authorised-probe tool: refusal first, findings second.

Two properties matter more than the finding list, and both are asserted here:

1. **A refused target is never contacted.** The scope check runs before the
   client is constructed. A test proves this by authorising *nothing* and
   pointing the tool at a server that records every request it receives — if
   the socket were opened before the check, the recorder would see it.

2. **Nothing speculative is reported as a finding.** Every finding carries
   `confidence: "confirmed"` and an `evidence` string drawn from the response.
   A missing header is reported as missing, never upgraded to a vulnerability,
   and a banner is reported as a banner with its consequence scoped explicitly
   ("whether this build is affected was not tested").
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core.security import scope
from tools.security import SecurityProbe


@pytest.fixture(autouse=True)
async def clean_db(tmp_path, monkeypatch):
    # `CORE_DB_PATH`, not `ZAYTOON_DB_PATH`: the latter is read by nothing in
    # the product, so setting it isolated nothing and only *looked* like it
    # did. That mattered, because `reset_for_tests()` drops every table in
    # whatever `db_path()` resolves to — the no-op spelling would have left
    # these tests pointing at the user's real `Data/core.db` wherever the
    # conftest fixture is not in scope.
    monkeypatch.setenv("CORE_DB_PATH", str(tmp_path / "core.db"))
    from core import db

    await db.reset_for_tests()
    yield
    await db.reset_for_tests()


class _Recorder(BaseHTTPRequestHandler):
    """Records every request it receives, so a premature connection is visible."""

    seen: list[str] = []

    def _respond(self):
        _Recorder.seen.append(self.path)
        self.send_response(200)
        self.send_header("X-Frame-Options", "ALLOWALL")
        self.send_header("Server", "Recorder/9.9.9")
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html>ok</html>")

    do_GET = _respond
    do_HEAD = _respond

    def log_message(self, *args):
        return


@pytest.fixture
def recorder_server():
    _Recorder.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def probe():
    return SecurityProbe()


# ---------------------------------------------------------------------------
# Refusal
# ---------------------------------------------------------------------------
class TestRefusalHappensBeforeTheSocket:
    async def test_an_unauthorised_target_is_refused(self, probe):
        result = await probe.execute("http://127.0.0.1:8099/admin")
        assert result.success is False
        assert result.data["refused"] is True
        assert result.data["reason"] == "out_of_scope"

    async def test_a_refused_target_is_never_contacted(self, probe, recorder_server):
        """The strongest statement available without reading the source.

        Nothing is authorised, the target is a live server, and the tool says no.
        If the connection were opened before the check, `seen` would be non-empty.
        """
        server, base = recorder_server
        result = await probe.execute(f"{base}/admin")

        assert result.data["refused"] is True
        assert _Recorder.seen == [], (
            f"the tool contacted {base} before authorising it: {_Recorder.seen}"
        )

    async def test_the_refusal_names_the_host_and_the_operation(self, probe):
        result = await probe.execute("http://sensitive.internal.test/x")
        assert "sensitive.internal.test" in result.error
        assert "http.probe" in result.error

    async def test_an_authorisation_for_another_operation_does_not_cover_a_probe(
        self, probe, recorder_server
    ):
        server, base = recorder_server
        await scope.create(["127.0.0.1"], [scope.OP_LOCAL_SCAN])
        result = await probe.execute(f"{base}/")
        assert result.data["refused"] is True
        assert _Recorder.seen == []

    async def test_revocation_stops_the_next_probe(self, probe, recorder_server):
        server, base = recorder_server
        created = await scope.create(["127.0.0.1"], [scope.OP_HTTP_PROBE])
        await scope.revoke(created.id)
        result = await probe.execute(f"{base}/")
        assert result.data["refused"] is True
        assert _Recorder.seen == []


# ---------------------------------------------------------------------------
# Authorised probing
# ---------------------------------------------------------------------------
class TestAuthorisedProbe:
    @pytest.fixture(autouse=True)
    async def authorised(self, recorder_server):
        server, base = recorder_server
        await scope.create(["127.0.0.1"], [scope.OP_HTTP_PROBE])
        self.base = base

    async def test_a_probe_is_recorded_against_its_authorization(self, probe):
        result = await probe.execute(f"{self.base}/")
        assert result.success is True
        assert result.data["authorization_id"].startswith("auth_")
        assert result.data["authorized_target"] == "127.0.0.1"
        assert result.data["status"] == 200
        assert _Recorder.seen == ["/"], "exactly one request, as documented"

    async def test_the_body_is_not_returned_unless_asked_for(self, probe):
        result = await probe.execute(f"{self.base}/")
        assert result.data["body_excerpt"] == ""
        with_body = await probe.execute(f"{self.base}/", include_body=True)
        assert "ok" in with_body.data["body_excerpt"]

    async def test_an_ineffective_header_is_reported_as_ineffective(self, probe):
        """`X-Frame-Options: ALLOWALL` is present and does nothing.

        A presence-only check calls this configured, which is the false negative
        that makes a scan report a clean bill of health.
        """
        result = await probe.execute(f"{self.base}/")
        ids = {finding["id"] for finding in result.data["findings"]}
        assert "header.ineffective.x-frame-options" in ids
        assert "header.missing.x-frame-options" not in ids

    async def test_a_version_banner_is_reported_without_overclaiming(self, probe):
        result = await probe.execute(f"{self.base}/")
        banner = next(
            f for f in result.data["findings"] if f["id"] == "disclosure.server"
        )
        assert banner["confidence"] == "confirmed"
        assert "Recorder/9.9.9" in banner["evidence"]
        assert "not tested" in banner["consequence"], (
            "a banner must not be presented as a known vulnerability"
        )

    async def test_hsts_is_not_reported_over_plain_http(self, probe):
        """The header is only honoured over TLS, so its absence says nothing."""
        result = await probe.execute(f"{self.base}/")
        ids = {finding["id"] for finding in result.data["findings"]}
        assert "header.missing.strict-transport-security" not in ids

    async def test_every_finding_is_confirmed_and_actionable(self, probe):
        result = await probe.execute(f"{self.base}/")
        for finding in result.data["findings"]:
            assert finding["confidence"] == "confirmed", finding
            assert finding["evidence"], finding
            assert finding["remediation"], finding
            assert finding["severity"] in {"high", "medium", "low", "info"}
            assert "probab" not in finding["consequence"].lower(), (
                "a speculative consequence would read as a confirmed finding"
            )

    async def test_the_report_states_what_it_did_not_check(self, probe):
        result = await probe.execute(f"{self.base}/")
        assert "non-destructive" in result.output
        assert "not evidence of absence" in result.output


class TestValidation:
    async def test_a_non_http_scheme_is_refused_at_validation(self, probe):
        from tools.base import ToolValidationError

        for url in ("file:///etc/passwd", "javascript:alert(1)", "data:text/html,x"):
            with pytest.raises(ToolValidationError):
                probe.validate({"url": url})

    async def test_a_url_without_a_host_is_refused(self, probe):
        from tools.base import ToolValidationError

        with pytest.raises(ToolValidationError):
            probe.validate({"url": "http:///nohost"})

    async def test_an_empty_url_is_refused(self, probe):
        from tools.base import ToolValidationError

        with pytest.raises(ToolValidationError):
            probe.validate({"url": "  "})

    def test_the_tool_requires_confirmation(self, probe):
        assert probe.permission == "confirm", (
            "touching a target, even an authorised one, must tell the user"
        )

    def test_the_risk_input_is_only_the_url(self, probe):
        """No response content may ever become an input to the gate."""
        assert probe.risk_params({"url": "http://x.test", "include_body": True}) == {
            "url": "http://x.test"
        }


class TestToolIsRegistered:
    def test_it_is_in_the_default_registry(self):
        from tools.registry import build_default_registry

        registry = build_default_registry()
        assert registry.has("security_probe")
        assert registry.effective_tier("security_probe") == "confirm"

    def test_scope_grants_cannot_come_from_a_skill_floor(self):
        """A skill may not lower the tool below `confirm`."""
        from tools.registry import ToolRegistry
        from tools.security import SecurityProbe

        registry = ToolRegistry()
        registry.register(SecurityProbe(), floor="confirm")
        assert registry.effective_tier("security_probe") == "confirm"
