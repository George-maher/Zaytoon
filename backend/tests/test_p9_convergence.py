"""The four things the audit left open, closed and then re-opened to look.

Each of these was a *known* divergence rather than a mystery, which is why they
survived until now: none of them broke a test, because each was the half of a
contract that no test was standing on.

* `POST /chat` answered with prose only, while the same turn over the WebSocket
  carried a `sources` frame — so a REST client could not tell a read page from a
  hallucination. Fixed by capturing the engine's own event in the turn result.
* `open_url` kept its own copy of the URL policy while `fetch_url` used
  `tools/netguard.py`. Two copies of an address policy drift; this one drifted
  toward "the metadata endpoint is openable".
* `REPEAT_SAFE_TOOLS` was a hand-written list with no tie to the registry, so a
  rename or a re-tier would have left it silently wrong in either direction.
* Session teardown: the WS session builds an `Orchestrator` per connection and
  shares one `MemoryAgent`. A per-session close would have closed the shared
  connection out from under every other session, so what is pinned here is that
  nobody starts doing that by accident.

The full-registry assertions need no network and no model.
"""

import asyncio
import os
import sys

import pytest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.engine import REPEAT_SAFE_TOOLS, repeat_safe_violations  # noqa: E402
from core.orchestrator import TurnResult  # noqa: E402

SOURCE = {
    "n": 1,
    "kind": "page",
    "url": "https://example.com/a",
    "host": "example.com",
    "title": "A page",
    "retrieved_at": "2026-10-11T07:00:00Z",
}


@pytest.fixture(scope="module")
def registry():
    from tools.registry import build_default_registry

    return build_default_registry()


class TestOpenUrlSharesThePolicy:
    """`open_url` now refuses what `fetch_url` refuses, in its own words."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",
            "file:///etc/passwd",
            "javascript:fetch('http://x')",
            "http://admin:pw@internal.test/",
        ],
    )
    def test_the_same_refusals_as_fetch(self, registry, url):
        from tools import fetch as fetch_mod

        open_result = asyncio.run(registry.execute("open_url", {"url": url}))
        assert open_result.get("blocked") is True, open_result
        # `fetch_url` refuses it too — that is the property under test, not the
        # wording: one list of refused addresses, two entry points.
        if url.startswith(("http://", "https://")):
            fetch_result = asyncio.run(registry.execute("fetch_url", {"url": url}))
            assert fetch_result.get("blocked") or fetch_result.get("success") is False

    def test_the_refusal_speaks_of_opening_not_fetching(self, registry):
        result = asyncio.run(registry.execute("open_url", {"url": "http://169.254.169.254/"}))
        assert "openable" in result["error"]
        assert "fetchable" not in result["error"]

    def test_the_authority_the_browser_gets_is_the_one_that_was_judged(self, monkeypatch):
        """Canonicalisation is the point, so it is asserted at the seam.

        `netguard` returns a *rebuilt* URL: the host lower-cased and a default
        port dropped, so the string handed to the browser is byte-for-byte the
        authority that was policy-checked — which is what makes `http://host@evil/`
        read the same to both sides instead of one way to a parser and another way
        to a browser. The **path is left alone on purpose**: `..` resolution
        belongs to the browser, and rewriting a path here would change which
        document gets opened — a security component with an opinion about paths is
        how you end up opening something nobody asked for.

        `webbrowser.open` is stubbed because the assertion is about *which string*
        arrives, and a machine with no browser must not be able to hide a wrong one.
        """
        from tools.web import OpenUrl

        assert OpenUrl._check("HTTPS://EXAMPLE.com:443/x") == "https://example.com/x"
        assert OpenUrl._check("https://Example.COM/a/../b") == "https://example.com/a/../b"

        from tools import web as web_mod

        seen = []
        monkeypatch.setattr(web_mod.webbrowser, "open", lambda url: seen.append(url) or True)
        result = asyncio.run(
            __import__("tools.registry", fromlist=["x"]).build_default_registry().execute(
                "open_url", {"url": "https://Example.COM:443/docs"}
            )
        )
        assert result["success"] is True
        assert seen == ["https://example.com/docs"], seen

    def test_shared_address_space_follows_the_private_flag(self):
        """A gap found while making the two tools share one policy.

        Python's `ipaddress` does **not** count 100.64.0.0/10 (RFC 6598, carrier-
        grade NAT) as `is_private` — and that block is where a home VPN pool
        (Tailscale's default) *and* some clouds' instance metadata both live
        (Aliyun answers at 100.100.100.200). So `is_private and not allow_private`
        left a metadata endpoint open to `fetch_url` on a machine that had no
        business reaching it.

        It is now covered by the same flag rather than refused outright, because
        refusing it outright would break the VPN user to defend a hypothetical.
        This test is the difference between those two choices, written down.
        """
        from tools.netguard import parse_public_url

        assert parse_public_url("http://100.64.0.1/", allow_private=True).url
        for blocked in ("http://100.64.0.1/", "http://100.100.100.200/latest/meta-data/"):
            with pytest.raises(Exception) as excinfo:
                parse_public_url(blocked, allow_private=False)
            assert "FETCH_ALLOW_PRIVATE" in str(excinfo.value), excinfo.value

    def test_the_knob_is_the_only_difference_from_fetch(self, monkeypatch):
        from config import config
        from tools.web import OpenUrl

        assert config.OPEN_URL_ALLOW_PRIVATE is True
        assert config.FETCH_ALLOW_PRIVATE is False
        monkeypatch.setattr(config, "OPEN_URL_ALLOW_PRIVATE", False)
        with pytest.raises(Exception) as excinfo:
            OpenUrl._check("http://localhost:5173")
        assert "this machine" in str(excinfo.value)


class TestRepeatSafeListAgreesWithTheRegistry:
    def test_the_shipped_list_has_no_violations(self, registry):
        assert repeat_safe_violations(registry) == []

    def test_a_stale_name_is_reported(self):
        from tools.registry import ToolRegistry

        assert any("dead config" in p for p in repeat_safe_violations(ToolRegistry()))

    def test_a_tool_that_needs_confirmation_for_a_read_is_reported(self, registry, monkeypatch):
        """The list's premise is "a repeat costs nothing". A tier change breaks it."""
        tool = registry.get("file_read")
        monkeypatch.setattr(tool, "permission", "confirm", raising=False)
        problems = repeat_safe_violations(registry)
        assert any("file_read" in p and "confirm" in p for p in problems), problems

    def test_an_alias_in_the_list_is_reported(self, registry, monkeypatch):
        """Exemptions match the name the model used; an alias-only entry is a hole."""
        tool = registry.get("search_web")
        assert "browse" not in REPEAT_SAFE_TOOLS
        # Pretend `browse` (an alias of search_web) had been listed instead: the
        # canonical call would then not be exempt, and the reviewer would have no
        # way to see that the entry meant nothing.
        monkeypatch.setattr(
            "agents.engine.REPEAT_SAFE_TOOLS",
            frozenset(set(REPEAT_SAFE_TOOLS) - {"search_web"} | {"browse"}),
        )
        problems = repeat_safe_violations(registry)
        assert any("browse" in p and "alias" in p for p in problems), problems

    def test_safe_tier_alone_is_not_enough_to_be_listed(self, registry):
        """`screenshot` is safe-tier and writes a PNG per call. Derivation would re-admit it.

        This is the reason the list is written out and then *checked*, instead of
        computed from tiers: a tier answers "is this dangerous?", not "does
        running it twice change anything?".
        """
        assert registry.assess("screenshot", {}).permission == "safe"
        assert "screenshot" not in REPEAT_SAFE_TOOLS
        assert "open_app" not in REPEAT_SAFE_TOOLS


class TestChatCarriesSources:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        import api.main as main_module

        return TestClient(main_module.app)

    def test_the_payload_has_the_key_even_for_a_sourceless_turn(self, client, monkeypatch):
        import api.main as main_module

        async def fake_turn(*args, **kwargs):
            return TurnResult(reply="It is 3pm.", intent="chat")

        monkeypatch.setattr(main_module.orchestrator, "run_turn", fake_turn)
        monkeypatch.setattr(
            main_module.memory_agent, "store_message", AsyncMock(return_value=None)
        )
        body = client.post("/chat", json={"message": "what time is it"}).json()
        assert body["sources"] == []

    def test_a_sourced_turn_reports_the_same_list_the_frame_carried(self, client, monkeypatch):
        import api.main as main_module

        async def fake_turn(*args, **kwargs):
            return TurnResult(reply="Python 3.13 [1].", intent="tools", sources=[SOURCE])

        monkeypatch.setattr(main_module.orchestrator, "run_turn", fake_turn)
        monkeypatch.setattr(
            main_module.memory_agent, "store_message", AsyncMock(return_value=None)
        )
        body = client.post("/chat", json={"message": "latest python?"}).json()
        assert body["sources"] == [SOURCE]
        assert body["sources"][0]["retrieved_at"] == SOURCE["retrieved_at"]

    def test_an_error_turn_reports_it_too(self, client, monkeypatch):
        """A client must never have to branch on whether the key exists."""
        import api.main as main_module

        async def fake_turn(*args, **kwargs):
            return TurnResult(reply="", error="provider unavailable", sources=[])

        monkeypatch.setattr(main_module.orchestrator, "run_turn", fake_turn)
        monkeypatch.setattr(
            main_module.memory_agent, "store_message", AsyncMock(return_value=None)
        )
        body = client.post("/chat", json={"message": "hi"}).json()
        assert body["sources"] == []
        assert body["error"] == "provider unavailable"


class TestSessionTeardown:
    """A disconnect releases the session's orchestrator; the shared memory agent stays open.

    Verified rather than assumed, because the tempting "cleanup" here is a bug:
    `MemoryAgent` is one process-wide instance injected into every session's
    `Orchestrator`, so closing it when a socket drops would leave every other
    session — and the REST path — holding a closed sqlite connection.
    """

    @pytest.mark.asyncio
    async def test_a_dropped_socket_leaves_no_session_state(self):
        from api import websocket as ws_mod
        from config import config
        from tests.test_websocket_confirmation import FakeWebSocket
        from tests.test_websocket_reminders import run

        memory = MagicMock()
        memory.close = AsyncMock()
        # The handler's collaborators, as the other WS tests assemble them. Only
        # `memory` is real about anything here, and that is the assertion below.
        deps = {
            "tts": MagicMock(),
            "memory": memory,
            "agent_service": MagicMock(),
            "router": MagicMock(),
            "config": config,
        }

        before = set(ws_mod._SESSION_ORCHESTRATORS)
        await run(FakeWebSocket([], idle_grace=0.05), MagicMock(), deps)

        assert set(ws_mod._SESSION_ORCHESTRATORS) == before, "an orchestrator outlived its socket"
        assert not [k for k in ws_mod.session_states if k not in before], "a SessionState did"
        memory.close.assert_not_called()

class TestMigrationsThatFailSaySo:
    """A swallowed schema migration is a bug that shows up somewhere else, later.

    `_migrate` reads `SELECT session_id` inside an `except` on purpose — the
    missing column is what the read is *for*. What used to be silent was the
    `ALTER` failing: the table then has no column that every later query assumes,
    and the failure surfaced as an unrelated error dozens of statements away.
    """

    class StubDB:
        def __init__(self, alter_error=None):
            self.alter_error = alter_error
            self.statements = []

        #: The `SELECT session_id` has to fail first — that failure *is* the
        #: "column missing" test, and the ALTER only runs because of it.
        async def execute(self, sql, params=()):
            self.statements.append(sql)
            if "SELECT session_id FROM context_summary" in sql:
                raise RuntimeError("no such column: session_id")
            if "ALTER TABLE context_summary" in sql and self.alter_error is not None:
                raise self.alter_error
            return None

    @pytest.mark.asyncio
    async def test_a_failed_alter_is_reported_and_does_not_raise(self, caplog):
        import logging

        from agents.memory_agent import MemoryAgent

        db = self.StubDB(alter_error=OSError("disk is full"))
        with caplog.at_level(logging.DEBUG):
            await MemoryAgent()._migrate(db)
        lines = [r.getMessage() for r in caplog.records if r.name == "Zaytoon.Reporting"]
        assert any("migrate.context_summary.session_id" in line for line in lines), lines
        assert "disk is full" in caplog.text

    @pytest.mark.asyncio
    async def test_an_idempotent_rerun_stays_quiet(self, caplog):
        import logging

        from agents.memory_agent import MemoryAgent

        db = self.StubDB(alter_error=RuntimeError('duplicate column name: session_id'))
        with caplog.at_level(logging.DEBUG):
            await MemoryAgent()._migrate(db)
        lines = [r.getMessage() for r in caplog.records if r.name == "Zaytoon.Reporting"]
        assert not any("context_summary" in line for line in lines), lines
        assert any("ALTER TABLE context_summary" in sql for sql in db.statements)
