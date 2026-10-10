"""`/health` must not call a dead provider healthy, and must not hang doing it.

The endpoint used to derive its status from `config.provider_ready`, which for
Ollama was `return True` — a statement about the *config file*. Measured live:
the same process logged `AI provider not connected: Connection failed` and then
answered `{"status": "ok"}` for the desktop supervisor to poll.

These tests pin the replacement: a real, bounded, cached verdict; `unverified`
rather than `ok` when there is no evidence; and an `uptime` that is a duration.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import PROVIDER_OLLAMA, PROVIDER_OPENROUTER, config  # noqa: E402
from services.provider_health import (  # noqa: E402
    REACHABLE,
    UNREACHABLE,
    UNCONFIGURED,
    UNKNOWN,
    ProviderHealth,
)


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("boom", request=None, response=None)

    def json(self):
        return self._payload


class _FakeClient:
    """Answers the two probe endpoints and counts every request made."""

    def __init__(self, *, versions=None, tags=None, refuse=False, hang=False, log=None):
        self.versions = versions if versions is not None else {"version": "0.5.7"}
        self.tags = tags or {"models": []}
        self.refuse = refuse
        self.hang = hang
        self.log = log if log is not None else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, url, **kwargs):
        self.log.append(url)
        if self.refuse:
            import httpx

            raise httpx.ConnectError("All connection attempts failed")
        if self.hang:
            await asyncio.sleep(30)
        if url.endswith("/api/version"):
            return _FakeResponse(self.versions)
        return _FakeResponse(self.tags)


@pytest.fixture
def ollama(monkeypatch):
    """Configured as a local provider on a port nothing listens on."""
    monkeypatch.setattr(config, "PROVIDER", PROVIDER_OLLAMA)
    monkeypatch.setattr(config, "OLLAMA_BASE_URL", "http://127.0.0.1:1", raising=False)
    monkeypatch.setattr(config, "HEALTH_PROBE_MODELS", True)
    monkeypatch.setattr(config, "HEALTH_PROBE_TTL_S", 5.0)
    monkeypatch.setattr(config, "HEALTH_PROBE_TIMEOUT_S", 2.0)
    return config


def _patch_client(monkeypatch, client):
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)


class TestVerdicts:
    async def test_a_refused_daemon_is_unreachable(self, ollama, monkeypatch):
        client = _FakeClient(refuse=True)
        _patch_client(monkeypatch, client)
        status = await ProviderHealth().status()
        assert status["state"] == UNREACHABLE
        assert "ConnectError" in status["error"]

    async def test_a_hung_daemon_cannot_hang_the_endpoint(self, ollama, monkeypatch):
        """The probe's timeout is the reason `/health` is safe to poll.

        Without it, `OllamaClient`'s 120 s request timeout meant one wedged
        daemon could hold up every reader of the endpoint that tells you
        whether the backend is alive.
        """
        import httpx

        monkeypatch.setattr(config, "HEALTH_PROBE_TIMEOUT_S", 0.4)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _HangingClient())
        started = time.monotonic()
        status = await ProviderHealth().status()
        elapsed = time.monotonic() - started
        assert status["state"] == UNREACHABLE
        # A generous ceiling: the claim is "bounded by the probe's timeout",
        # not "exactly 400 ms on a loaded CI box".
        assert elapsed < config.HEALTH_PROBE_TIMEOUT_S + 1.5, f"{elapsed:.2f}s"

    async def test_a_configured_daemon_reports_which_models_are_missing(
        self, ollama, monkeypatch
    ):
        _patch_client(monkeypatch, _FakeClient(tags={"models": [
            {"name": f"{config.OLLAMA_FAST_MODEL}"},
        ]}))
        status = await ProviderHealth().status()
        assert status["state"] == REACHABLE
        # general + coding were never installed; fast was.
        assert set(status["missing_models"]) == {
            config.OLLAMA_GENERAL_MODEL,
            config.OLLAMA_CODING_MODEL,
        }

    async def test_latest_satisfies_a_bare_configured_name(self, ollama, monkeypatch):
        """`qwen3.5` in the config and `qwen3.5:latest` from ollama are the same model.

        A strict string compare would report every tag-less configuration as a
        missing model and mark a healthy backend degraded.
        """
        model = "qwen3.5"
        for attr in ("OLLAMA_GENERAL_MODEL", "OLLAMA_CODING_MODEL", "OLLAMA_FAST_MODEL"):
            monkeypatch.setattr(config, attr, model, raising=False)
        _patch_client(
            monkeypatch, _FakeClient(tags={"models": [{"name": f"{model}:latest"}]})
        )
        status = await ProviderHealth().status()
        assert status["state"] == REACHABLE
        assert status["missing_models"] == []

    async def test_all_three_present_is_a_clean_bill(self, ollama, monkeypatch):
        _patch_client(monkeypatch, _FakeClient(tags={"models": [
            {"name": config.OLLAMA_GENERAL_MODEL},
            {"name": config.OLLAMA_CODING_MODEL},
            {"name": config.OLLAMA_FAST_MODEL},
        ]}))
        status = await ProviderHealth().status()
        assert status["state"] == REACHABLE
        assert status["missing_models"] == []

    async def test_the_cloud_path_never_sends_a_synthetic_request(
        self, monkeypatch
    ):
        """A metered API is not something to ping from a health check."""
        from services import provider_health as mod

        monkeypatch.setattr(config, "PROVIDER", PROVIDER_OPENROUTER)
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "", raising=False)

        def explode(**kwargs):  # pragma: no cover - must not be reached
            raise AssertionError("the cloud path must not probe the network")

        monkeypatch.setattr(mod.httpx, "AsyncClient", explode)
        status = await ProviderHealth().status()
        assert status["state"] == UNCONFIGURED
        # Not "observations" and certainly not "probe": the answer came from the
        # environment, and pretending otherwise is how a health check starts
        # asserting things it never measured.
        assert status["source"] == "config"


class _HangingClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, url, **kwargs):
        await asyncio.sleep(30)


class TestCachingAndStampede:
    async def test_the_cache_answers_the_second_call(self, ollama, monkeypatch):
        log = []
        _patch_client(monkeypatch, _FakeClient(log=log))
        prober = ProviderHealth()
        first = await prober.status()
        second = await prober.status()
        assert first["cached"] is False
        assert second["cached"] is True
        assert len(log) == 2, "one probe = version + tags, not two probes"

    async def test_force_ignores_the_cache(self, ollama, monkeypatch):
        log = []
        _patch_client(monkeypatch, _FakeClient(log=log))
        prober = ProviderHealth()
        await prober.status()
        forced = await prober.status(force=True)
        assert forced["cached"] is False
        assert len(log) == 4

    async def test_ten_pollers_produce_one_probe(self, ollama, monkeypatch):
        log = []
        _patch_client(monkeypatch, _FakeClient(log=log))
        prober = ProviderHealth()
        results = await asyncio.gather(*(prober.status() for _ in range(10)))
        assert len(log) == 2, f"stampede: {len(log)} requests for 10 pollers"
        assert sum(1 for r in results if r["cached"]) == 9

    async def test_repointing_the_endpoint_invalidates_the_cache(
        self, ollama, monkeypatch
    ):
        log = []
        _patch_client(monkeypatch, _FakeClient(refuse=False, log=log))
        prober = ProviderHealth()
        await prober.status()
        monkeypatch.setattr(config, "OLLAMA_BASE_URL", "http://127.0.0.1:2", raising=False)
        await prober.status()
        assert len(log) == 4, "a cached verdict for another endpoint is not an answer"

    def test_cached_never_probes_and_says_when_it_last_asked(self, ollama):
        prober = ProviderHealth()
        assert prober.cached()["state"] == UNKNOWN
        assert prober.cached()["error"] == "no probe has run"


class TestHealthEndpoint:
    @pytest.fixture
    def client(self, monkeypatch):
        from fastapi.testclient import TestClient

        import api.main as main_module

        monkeypatch.setattr(config, "PROVIDER", PROVIDER_OLLAMA, raising=False)
        return TestClient(main_module.app)

    async def test_status_is_degraded_when_the_provider_cannot_be_reached(
        self, client, monkeypatch
    ):
        import api.main as main_module

        monkeypatch.setattr(
            main_module.provider_health,
            "status",
            lambda force=False: _unreachable(),
        )
        body = client.get("/health").json()
        assert body["status"] == "degraded"
        assert "Connection refused" in body["status_reason"]

    async def test_status_is_ok_only_with_a_reachable_daemon_and_no_missing_model(
        self, client, monkeypatch
    ):
        import api.main as main_module

        monkeypatch.setattr(
            main_module.provider_health, "status", lambda force=False: _reachable([])
        )
        assert client.get("/health").json()["status"] == "ok"

        monkeypatch.setattr(
            main_module.provider_health,
            "status",
            lambda force=False: _reachable(["qwen3.5:9b"]),
        )
        body = client.get("/health").json()
        assert body["status"] == "degraded"
        assert body["llm"]["probe"]["missing_models"] == ["qwen3.5:9b"]

    def test_uptime_is_a_duration_not_an_epoch(self, client):
        body = client.get("/health").json()
        # The old value was `time.time()`: 1.79e9, read as ~56 years of uptime.
        assert 0 <= body["uptime"] < 3600, body["uptime"]
        assert body["uptime_seconds"] == body["uptime"]
        datetime.fromisoformat(body["started_at"])

    def test_the_reported_version_is_the_app_version(self, client):
        """Two literals used to say "3.0.0" independently; a bump could drift one.

        There is now one constant, so assert against the app object rather than
        against a third copy of the string.
        """
        import api.main as main_module

        body = client.get("/health").json()
        assert body["version"] == main_module.app.version

    async def test_the_probe_reaches_the_response(self, client, monkeypatch):
        """`configured` and `reachable` are different facts; both must be visible."""
        import api.main as main_module

        monkeypatch.setattr(
            main_module.provider_health, "status", lambda force=False: _unreachable()
        )
        body = client.get("/health").json()
        assert body["llm"]["configured"] is True, "the config *is* valid..."
        assert body["llm"]["probe"]["state"] == UNREACHABLE, "...and that is not health"
        assert json.dumps(body), "the payload must stay serialisable"


async def _unreachable():
    return {
        "provider": PROVIDER_OLLAMA,
        "endpoint": "http://127.0.0.1:1",
        "state": UNREACHABLE,
        "error": "ConnectError: Connection refused",
        "missing_models": [],
        "latency_ms": 1,
        "age_s": 0.0,
        "cached": False,
        "source": "probe",
        "checked_at": datetime.now().isoformat(),
    }


async def _reachable(missing):
    return {
        "provider": PROVIDER_OLLAMA,
        "endpoint": "http://127.0.0.1:1",
        "state": REACHABLE,
        "error": "",
        "missing_models": missing,
        "version": "0.5.7",
        "latency_ms": 4,
        "age_s": 0.0,
        "cached": False,
        "source": "probe",
        "checked_at": datetime.now().isoformat(),
    }
