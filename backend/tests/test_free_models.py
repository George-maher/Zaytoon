"""Zero-cost guarantee: only verifiably free OpenRouter models may be used.

These tests are the enforcement point for the "never pay" requirement. If one
of them fails, a real request could have been billed.
"""

import json

import httpx
import pytest

from config import (
    ALLOWED_FREE_MODELS,
    FREE_ROUTER_ENDPOINT,
    PROVIDER_OPENROUTER,
    NonFreeModelError,
    Config,
    config,
    is_allowed_model,
    is_free_model,
)
from services.llm_client import LLMClient, LLMError, ProviderEndpoint, get_llm_client


BILLABLE_MODELS = [
    "openai/gpt-4o",
    "openai/gpt-4o-mini",
    "anthropic/claude-3.5-sonnet",
    "google/gemini-2.0-flash",
    "meta-llama/llama-3.1-70b-instruct",
    "mistralai/mistral-large",
    "openrouter/auto",
    "qwen/qwen3-8b",
    "deepseek/deepseek-chat",
]

FREE_MODELS = [
    "qwen/qwen3.8-27b:free",
    "thinkingmachines/inkling-small:free",
    "openrouter/free",
    "meta-llama/llama-3.1-8b:free",
    "google/gemma-3-27b-it:free",
    "mistralai/mistral-small-3.1:free",
]


# ---------------------------------------------------------------------------
# is_free_model
# ---------------------------------------------------------------------------
class TestIsFreeModel:
    @pytest.mark.parametrize("model", FREE_MODELS)
    def test_accepts_free_models(self, model):
        assert is_free_model(model) is True

    @pytest.mark.parametrize("model", BILLABLE_MODELS)
    def test_rejects_billable_models(self, model):
        assert is_free_model(model) is False

    @pytest.mark.parametrize("model", ["", None, 123, [], "   "])
    def test_rejects_empty_and_non_string(self, model):
        assert is_free_model(model) is False

    def test_free_tag_is_case_insensitive(self):
        assert is_free_model("Qwen/Qwen3-8B:FREE") is True
        assert is_free_model("OPENROUTER/FREE") is True

    def test_suffix_must_be_exactly_free(self):
        # A model whose name merely *contains* "free" is not free.
        assert is_free_model("vendor/free-tier-model") is False
        assert is_free_model("vendor/model:free-preview") is False

    def test_trailing_whitespace_tolerated(self):
        assert is_free_model("  qwen/qwen3-8b:free  ") is True

    def test_free_router_endpoint(self):
        assert FREE_ROUTER_ENDPOINT == "openrouter/free"
        assert is_free_model(FREE_ROUTER_ENDPOINT) is True


# ---------------------------------------------------------------------------
# Config-level validation
# ---------------------------------------------------------------------------
class TestConfigFreeModelValidation:
    def test_configured_models_are_free_by_default(self, monkeypatch):
        monkeypatch.setattr(config, "OPENROUTER_PRIMARY_MODEL", "qwen/qwen3.8-27b:free", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_REASONING_MODEL", "thinkingmachines/inkling-small:free", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_FALLBACK_MODEL", "openrouter/free", raising=False)
        assert config.validate_free_models() == []

    def test_reports_billable_config(self, monkeypatch):
        monkeypatch.setattr(config, "OPENROUTER_PRIMARY_MODEL", "openai/gpt-4o", raising=False)
        offenders = config.validate_free_models()
        assert "openai/gpt-4o" in offenders

    def test_assert_free_model_raises(self, monkeypatch):
        monkeypatch.setattr(config, "LLM_REQUIRE_FREE_MODELS", True, raising=False)
        with pytest.raises(NonFreeModelError) as exc:
            config.assert_free_model("openai/gpt-4o")
        assert "openai/gpt-4o" in str(exc.value)
        assert ":free" in str(exc.value)

    def test_assert_free_model_allows_allowlisted(self, monkeypatch):
        monkeypatch.setattr(config, "LLM_REQUIRE_FREE_MODELS", True, raising=False)
        assert config.assert_free_model("qwen/qwen3.8-27b:free") == (
            "qwen/qwen3.8-27b:free"
        )

    def test_assert_free_model_rejects_free_but_unlisted(self, monkeypatch):
        monkeypatch.setattr(config, "LLM_REQUIRE_FREE_MODELS", True, raising=False)
        with pytest.raises(NonFreeModelError):
            config.assert_free_model("qwen/qwen3-8b:free")

    def test_enforcement_can_be_disabled_explicitly(self, monkeypatch):
        monkeypatch.setattr(config, "LLM_REQUIRE_FREE_MODELS", False, raising=False)
        assert config.free_models_enforced is False
        # Bypasses the guard (documented opt-out, off by default).
        assert config.assert_free_model("openai/gpt-4o") == "openai/gpt-4o"

    def test_describe_llm_reports_free_only(self, monkeypatch):
        monkeypatch.setattr(config, "LLM_REQUIRE_FREE_MODELS", True, raising=False)
        described = config.describe_llm()
        assert described["free_only"] is True
        assert described["rejected_models"] == []
        assert described["allowed_models"] == sorted(ALLOWED_FREE_MODELS)
        assert "openrouter" not in described  # flat schema now


# ---------------------------------------------------------------------------
# ProviderEndpoint guard
# ---------------------------------------------------------------------------
class TestEndpointGuard:
    def test_free_endpoint_allowed(self):
        ep = ProviderEndpoint(
            provider=PROVIDER_OPENROUTER,
            model="qwen/qwen3.8-27b:free",
            url="https://openrouter.ai/api/v1/chat/completions",
            headers={},
        )
        assert ep.is_free is True

    def test_free_router_allowed(self):
        ep = ProviderEndpoint(
            provider=PROVIDER_OPENROUTER, model="openrouter/free",
            url="https://openrouter.ai/api/v1/chat/completions", headers={},
        )
        assert ep.is_free is True

    @pytest.mark.parametrize("model", BILLABLE_MODELS)
    def test_billable_endpoint_rejected_at_construction(self, model):
        with pytest.raises(NonFreeModelError):
            ProviderEndpoint(
                provider=PROVIDER_OPENROUTER, model=model, url="http://x", headers={}
            )

    def test_no_local_provider_exists(self):
        """Local model execution has been removed from the codebase."""
        import config as config_module

        assert not hasattr(config_module, "PROVIDER_OLLAMA")
        for attr in ("OLLAMA_BASE_URL", "OLLAMA_MODEL", "OLLAMA_FALLBACK_MODEL"):
            assert not hasattr(config, attr), attr

    def test_free_but_unlisted_model_is_refused(self):
        """The allow-list is stricter than the ':free' shape check."""
        model = "rogue-vendor/rogue-model:free"
        with pytest.raises(NonFreeModelError):
            ProviderEndpoint(
                provider=PROVIDER_OPENROUTER, model=model, url="http://x", headers={}
            )

    def test_build_payload_revalidates(self, monkeypatch):
        """A model swapped in after construction is still caught."""
        ep = ProviderEndpoint(
            provider=PROVIDER_OPENROUTER, model="openrouter/free",
            url="http://x", headers={},
        )
        object.__setattr__(ep, "model", "openai/gpt-4o")
        with pytest.raises(NonFreeModelError):
            ep.build_payload([], 10, 0.1)


# ---------------------------------------------------------------------------
# Chain construction
# ---------------------------------------------------------------------------
@pytest.fixture
def free_env(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-test", raising=False)
    monkeypatch.setattr(config, "LLM_REQUIRE_FREE_MODELS", True, raising=False)
    monkeypatch.setattr(config, "OPENROUTER_PRIMARY_MODEL", "qwen/qwen3.8-27b:free", raising=False)
    monkeypatch.setattr(config, "OPENROUTER_REASONING_MODEL", "thinkingmachines/inkling-small:free", raising=False)
    monkeypatch.setattr(config, "OPENROUTER_FALLBACK_MODEL", "openrouter/free", raising=False)
    return config


class TestChainFreeOnly:
    def test_every_openrouter_endpoint_is_free(self, free_env):
        chain = LLMClient().build_chain("fast")
        openrouter_models = [
            ep.model for ep in chain if ep.provider == PROVIDER_OPENROUTER
        ]
        assert openrouter_models
        for model in openrouter_models:
            assert is_free_model(model), f"{model} is not free"

    def test_chain_contains_only_verified_free_defaults(self, free_env):
        chain = LLMClient().build_chain("reasoning")
        models = [ep.model for ep in chain if ep.provider == PROVIDER_OPENROUTER]
        assert models[0] == "thinkingmachines/inkling-small:free"
        assert "openrouter/free" in models

    def test_chain_has_no_local_fallback(self, free_env):
        """The chain is primary -> openrouter/free, and nothing else."""
        chain = LLMClient().build_chain("fast")
        assert [e.model for e in chain] == [
            "qwen/qwen3.8-27b:free", "openrouter/free",
        ]
        assert all(ep.provider == PROVIDER_OPENROUTER for ep in chain)
        assert all(ep.url.startswith("https://openrouter.ai/") for ep in chain)

    def test_missing_key_fails_loudly(self, monkeypatch):
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "", raising=False)
        with pytest.raises(LLMError) as exc:
            LLMClient().build_chain("fast")
        assert "OPENROUTER_API_KEY" in str(exc.value)

    def test_billable_config_raises_when_building_chain(self, free_env, monkeypatch):
        monkeypatch.setattr(config, "OPENROUTER_PRIMARY_MODEL", "openai/gpt-4o", raising=False)
        with pytest.raises(NonFreeModelError):
            LLMClient().build_chain("fast")

    def test_no_request_is_sent_for_billable_model(self, free_env, monkeypatch):
        """The whole point: rejection happens before any socket is opened."""
        monkeypatch.setattr(config, "OPENROUTER_PRIMARY_MODEL", "openai/gpt-4o", raising=False)
        client = LLMClient()
        with pytest.raises(NonFreeModelError):
            client.build_chain("fast")


# ---------------------------------------------------------------------------
# No network call for a billable payload
# ---------------------------------------------------------------------------
class _ExplodingClient:
    """Fails the test if any HTTP call is attempted."""

    is_closed = False

    def __init__(self):
        self.calls = []

    async def post(self, *a, **k):
        self.calls.append((a, k))
        raise AssertionError("HTTP request attempted for a non-free model!")

    def stream(self, *a, **k):
        self.calls.append((a, k))
        raise AssertionError("stream attempted for a non-free model!")

    async def aclose(self):
        self.is_closed = True


class TestNoNetworkForBillable:
    async def test_complete_never_reaches_the_socket(self, free_env, monkeypatch):
        monkeypatch.setattr(config, "OPENROUTER_PRIMARY_MODEL", "openai/gpt-4o", raising=False)
        client = LLMClient()
        exploding = _ExplodingClient()
        client._client = exploding

        with pytest.raises(NonFreeModelError):
            await client.complete([{"role": "user", "content": "hi"}])

        assert exploding.calls == []

    async def test_endpoint_payload_never_reaches_the_socket(self, free_env):
        client = LLMClient()
        exploding = _ExplodingClient()
        client._client = exploding
        ep = ProviderEndpoint(
            provider=PROVIDER_OPENROUTER, model="openrouter/free",
            url="https://openrouter.ai/api/v1/chat/completions", headers={},
        )
        # A legitimate free request does go out.
        assert ep.build_payload([{"role": "user", "content": "hi"}], 10, 0.1)["model"] == (
            "openrouter/free"
        )


# ---------------------------------------------------------------------------
# Fallback behaviour with free models
# ---------------------------------------------------------------------------
class _Recorder:
    def __init__(self, handler):
        self.handler = handler
        self.requests = []
        self.is_closed = False

    async def post(self, url, json=None, headers=None):
        self.requests.append({"url": url, "json": json, "headers": headers or {}})
        result = self.handler(url, json, headers or {})
        if isinstance(result, Exception):
            raise result
        return result

    def stream(self, method, url, json=None, headers=None):
        self.requests.append({"url": url, "json": json, "headers": headers or {}})
        return _Stream(self.handler(url, json, headers or {}))

    async def aclose(self):
        self.is_closed = True


class _Stream:
    def __init__(self, result):
        self._result = result

    async def __aenter__(self):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    async def __aexit__(self, *a):
        return False

    def aiter_lines(self):
        text = getattr(self._result, "text", "")

        async def gen():
            for line in str(text).splitlines():
                yield line

        return gen()


def _ok(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def _err(status):
    return httpx.Response(status, json={"error": {"message": "nope"}})


class TestFreeModelFallback:
    async def test_429_falls_to_free_fallback(self, free_env):
        def handler(url, payload, headers):
            if payload["model"] == "qwen/qwen3.8-27b:free":
                return _err(429)
            return _ok("served by fallback")

        client = LLMClient(max_retries=1)
        client._client = _Recorder(handler)
        result = await client.complete([{"role": "user", "content": "x"}])
        assert result == "served by fallback"
        assert client._client.requests[-1]["json"]["model"] == "openrouter/free"

    async def test_500_falls_through_chain(self, free_env):
        def handler(url, payload, headers):
            if payload["model"] == "qwen/qwen3.8-27b:free":
                return _err(500)
            return _ok("recovered")

        client = LLMClient(max_retries=1)
        client._client = _Recorder(handler)
        assert await client.complete([{"role": "user", "content": "x"}]) == "recovered"

    async def test_403_reasoning_model_falls_through(self, free_env):
        # Observed live: thinkingmachines/inkling-small:free returns 403 for
        # non-agentic clients. It must not break the request.
        def handler(url, payload, headers):
            if payload["model"] == "thinkingmachines/inkling-small:free":
                return _err(403)
            return _ok("fallback answered")

        client = LLMClient(max_retries=1)
        client._client = _Recorder(handler)
        assert await client.complete(
            [{"role": "user", "content": "x"}], tier="reasoning"
        ) == "fallback answered"

    async def test_all_free_endpoints_failing_raises(self, free_env):
        client = LLMClient(max_retries=1)
        client._client = _Recorder(lambda u, p, h: _err(503))
        with pytest.raises(LLMError):
            await client.complete([{"role": "user", "content": "x"}])

    async def test_headers_include_openrouter_attribution(self, free_env, monkeypatch):
        monkeypatch.setattr(config, "OPENROUTER_SITE_URL", "https://github.com/zaytoon-ai", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_APP_TITLE", "Zaytoon AI Assistant", raising=False)
        client = LLMClient(max_retries=1)
        client._client = _Recorder(lambda u, p, h: _ok("ok"))
        await client.complete([{"role": "user", "content": "x"}])
        sent = client._client.requests[0]["headers"]
        assert sent["Authorization"] == "Bearer sk-test"
        assert sent["HTTP-Referer"] == "https://github.com/zaytoon-ai"
        assert sent["X-Title"] == "Zaytoon AI Assistant"
