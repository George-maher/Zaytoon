"""The probe endpoint: the two-step approval, over HTTP.

`POST /security/probe` is the only path by which an active assessment is
permitted from the UI, so what it does *between* its two steps is the
security-relevant part:

* **Step 1 opens no socket.** It asks the scope layer the same question step 2
  will ask, and returns a token instead of a probe. A test proves the
  no-socket claim by pointing it at a server that records every request — if
  the check ran after the client was built, the recorder would see one.

* **Step 2 is still gated.** The endpoint does not "remember" that the user
  clicked. It runs the tool through the process's real `ToolRegistry`, which
  applies validation, the reconciled permission tier and the audit record, and
  it answers *that* manager's prompt — the same `resolve()` call the WebSocket
  handler makes when a user approves. `approved_request_id` in the response is
  the receipt: it is only present because a real prompt was issued and
  answered. This assertion is the route-level guard for a defect that shipped
  once — `ConfirmationManager.request()` re-assessed the tool with a bare
  `RiskAssessor`, saw `safe`, and returned approval without ever prompting.

* **A token is a gesture, not a permission.** Single use, short lived, and it
  cannot widen scope: the target still has to be authorised, and revoking the
  authorisation stops the next probe even with a fresh token.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api.main as main_module  # noqa: E402
from core.api import registry_api  # noqa: E402
from core.security import scope  # noqa: E402


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
    registry_api._PROBE_TOKENS.clear()
    yield
    await db.reset_for_tests()
    registry_api._PROBE_TOKENS.clear()


@pytest.fixture
def client():
    return TestClient(main_module.app)


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
def recorder():
    _Recorder.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


async def _authorise(target: str, operation: str = "http.probe") -> str:
    record = await scope.create([target], [operation], note="route test")
    return record.id


def _preview(client, url: str, **extra):
    return client.post("/security/probe", json={"url": url, **extra})


def _confirm(client, url: str, token: str, **extra):
    return client.post("/security/probe", json={"url": url, "confirm": token, **extra})


# ---------------------------------------------------------------------------
# Step 1: the ask
# ---------------------------------------------------------------------------
class TestTheAsk:
    async def test_step_one_opens_no_socket(self, client, recorder):
        await _authorise("127.0.0.1")
        response = _preview(client, f"{recorder}/admin")
        assert response.status_code == 200
        assert response.json()["requires_confirmation"] is True
        # The recorder is the assertion that matters.
        assert _Recorder.seen == []

    async def test_step_one_says_what_it_would_do(self, client, recorder):
        authorization_id = await _authorise("127.0.0.1")
        body = _preview(client, f"{recorder}/admin").json()
        assert body["host"] == "127.0.0.1"
        assert body["authorization_id"] == authorization_id
        assert body["authorized_target"] == "127.0.0.1"
        # The message names the host and the authorisation it acts under, so
        # the user is answering a question about *this* target, not one in
        # general.
        assert "127.0.0.1" in body["message"]
        assert authorization_id in body["message"]
        assert body["confirm"].startswith("probe_")

    async def test_step_one_gives_a_fresh_token_each_time(self, client, recorder):
        await _authorise("127.0.0.1")
        first = _preview(client, f"{recorder}/a").json()["confirm"]
        second = _preview(client, f"{recorder}/b").json()["confirm"]
        assert first != second

    async def test_no_socket_is_opened_for_an_unauthorised_target(self, client, recorder):
        response = _preview(client, f"{recorder}/admin")
        assert response.status_code == 200
        body = response.json()
        assert body["refused"] is True
        assert body["reason"] == "out_of_scope"
        # Scope refusal is a 200 with an answer, not a 4xx: it is the control
        # working, and the UI renders the reason rather than an error toast.
        assert _Recorder.seen == []
        assert "not authorised" in body["message"]

    async def test_refused_is_not_answered_with_a_token(self, client, recorder):
        body = _preview(client, f"{recorder}/admin").json()
        assert "confirm" not in body


# ---------------------------------------------------------------------------
# Step 2: the answer
# ---------------------------------------------------------------------------
class TestTheAnswer:
    async def test_it_actually_probes(self, client, recorder):
        await _authorise("127.0.0.1")
        token = _preview(client, f"{recorder}/admin").json()["confirm"]
        response = _confirm(client, f"{recorder}/admin", token)
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"]["status"] == 200
        assert _Recorder.seen == ["/admin"]

    async def test_the_approval_is_real_and_recorded(self, client, recorder):
        """The route-level guard for the tier gate.

        `security_probe` is declared `confirm`. If the endpoint ran it without
        a genuine prompt — which is exactly what the manager's old re-assessment
        did — `approved_request_id` would be absent and the tool would have run
        anyway. Both halves are asserted, so neither can pass alone.
        """
        await _authorise("127.0.0.1")
        token = _preview(client, f"{recorder}/admin").json()["confirm"]
        body = _confirm(client, f"{recorder}/admin", token).json()
        assert body.get("approved_request_id"), (
            "the probe ran without a request id, which means no prompt was issued"
        )
        assert body["approved_request_id"].startswith("cfm_")

    async def test_the_authorisation_is_echoed_into_the_result(self, client, recorder):
        authorization_id = await _authorise("127.0.0.1")
        token = _preview(client, f"{recorder}/admin").json()["confirm"]
        body = _confirm(client, f"{recorder}/admin", token).json()
        assert body["authorization_id"] == authorization_id
        assert body["data"]["authorization_id"] == authorization_id

    async def test_a_token_is_single_use(self, client, recorder):
        await _authorise("127.0.0.1")
        token = _preview(client, f"{recorder}/admin").json()["confirm"]
        assert _confirm(client, f"{recorder}/admin", token).status_code == 200
        replay = _confirm(client, f"{recorder}/admin", token)
        assert replay.status_code == 409
        # And the replay must not have reached the target a second time.
        assert _Recorder.seen == ["/admin"]

    async def test_an_unknown_token_is_refused(self, client, recorder):
        await _authorise("127.0.0.1")
        response = _confirm(client, f"{recorder}/admin", "probe_deadbeefdead")
        assert response.status_code == 409
        assert _Recorder.seen == []

    async def test_an_expired_token_is_refused(self, client, recorder):
        await _authorise("127.0.0.1")
        token = _preview(client, f"{recorder}/admin").json()["confirm"]
        # Age the token past its TTL rather than sleeping for two minutes.
        registry_api._PROBE_TOKENS[token] = time.monotonic() - 10_000
        response = _confirm(client, f"{recorder}/admin", token)
        assert response.status_code == 409
        assert _Recorder.seen == []

    async def test_the_token_cannot_be_replayed_against_another_host(self, client, recorder):
        await _authorise("127.0.0.1")
        token = _preview(client, f"{recorder}/admin").json()["confirm"]
        # A token is bound to nothing on purpose: scope is re-checked on every
        # call, so using it on a second, unauthorised host is refused by the
        # scope layer rather than by the token.
        response = _confirm(client, "http://lab.internal.test/admin", token)
        assert response.status_code == 200
        assert response.json()["refused"] is True
        assert _Recorder.seen == []

    async def test_revoking_the_authorisation_stops_the_next_probe(self, client, recorder):
        authorization_id = await _authorise("127.0.0.1")
        await scope.revoke(authorization_id)
        body = _preview(client, f"{recorder}/admin").json()
        assert body["refused"] is True
        assert _Recorder.seen == []


# ---------------------------------------------------------------------------
# Input validation and the token table
# ---------------------------------------------------------------------------
class TestValidation:
    @pytest.mark.parametrize(
        "url",
        ["", "   ", "file:///etc/passwd", "ftp://example.com/x", "not-a-url", "http://"],
    )
    def test_a_url_that_is_not_an_absolute_http_url_is_rejected(self, client, url):
        assert _preview(client, url).status_code == 422

    def test_a_missing_url_is_rejected(self, client):
        assert client.post("/security/probe", json={}).status_code == 422

    async def test_a_non_http_scheme_is_rejected_even_when_authorised(self, client):
        await _authorise("127.0.0.1")
        assert _preview(client, "file:///etc/passwd").status_code == 422


class TestTheTokenTable:
    def test_expired_tokens_are_pruned(self):
        now = time.monotonic()
        registry_api._PROBE_TOKENS.clear()
        registry_api._PROBE_TOKENS["probe_old"] = now - 10_000
        registry_api._PROBE_TOKENS["probe_fresh"] = now
        registry_api._prune_probe_tokens(now)
        assert "probe_old" not in registry_api._PROBE_TOKENS
        assert "probe_fresh" in registry_api._PROBE_TOKENS

    def test_the_table_is_capped_and_evicts_the_oldest(self):
        now = time.monotonic()
        registry_api._PROBE_TOKENS.clear()
        total = registry_api._PROBE_TOKEN_MAX + 40
        for i in range(total):
            registry_api._PROBE_TOKENS[f"probe_{i:04d}"] = now + i / 1000.0
        newest = f"probe_{total - 1:04d}"
        registry_api._prune_probe_tokens(now)
        assert len(registry_api._PROBE_TOKENS) == registry_api._PROBE_TOKEN_MAX
        # The newest survive, so the cap cannot evict the token in flight.
        assert newest in registry_api._PROBE_TOKENS
        assert "probe_0000" not in registry_api._PROBE_TOKENS


# ---------------------------------------------------------------------------
# The authorisation endpoints the view drives
# ---------------------------------------------------------------------------
class TestAuthorisationRoutes:
    def test_listing_reports_the_vocabulary_and_the_count(self, client):
        body = client.get("/security/authorizations").json()
        assert body["active_count"] == 0
        assert "http.probe" in body["operations"]

    async def test_creating_returns_the_record(self, client):
        response = client.post(
            "/security/authorizations",
            json={"targets": ["127.0.0.1"], "operations": ["http.probe"], "note": "n"},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["id"].startswith("auth_")
        assert body["target"] == "127.0.0.1"
        assert body["active"] is True

    def test_a_wildcard_for_every_host_is_refused(self, client):
        response = client.post(
            "/security/authorizations", json={"targets": ["*"], "operations": ["http.probe"]}
        )
        assert response.status_code == 422
        assert "not an authorization" in response.json()["error"]

    def test_a_public_suffix_wildcard_is_refused(self, client):
        response = client.post(
            "/security/authorizations",
            json={"targets": ["*.co.uk"], "operations": ["http.probe"]},
        )
        assert response.status_code == 422
        assert "public suffix" in response.json()["error"]

    def test_revoking_an_unknown_id_is_a_404(self, client):
        assert client.delete("/security/authorizations/auth_nope").status_code == 404
