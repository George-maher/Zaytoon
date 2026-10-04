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
from services.llm_client import LLMClient, LLMError, ProviderEndpoint
from services.model_registry import (
    MODEL_REGISTRY,
    is_free_router,
    select_models,
)


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
    "nvidia/nemotron-3-super-120b-a12b:free",
    "thinkingmachines/inkling-small:free",
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

    def test_suffix_must_be_exactly_free(self):
        # A model whose name merely *contains* "free" is not free.
        assert is_free_model("vendor/free-tier-model") is False
        assert is_free_model("vendor/model:free-preview") is False

    def test_trailing_whitespace_tolerated(self):
        assert is_free_model("  qwen/qwen3-8b:free  ") is True

    def test_the_free_router_is_not_a_free_form_we_call(self):
        """`openrouter/free` is free, but it is not *controlled*.

        It resolves to a different arbitrary free model on each call depending on
        upstream load, which makes a routing bug unreproducible and a fix
        unverifiable -- and it answered a `get_time` request with the literal
        string "User Safety: safe". So it is recognised in order to be refused.
        """
        assert FREE_ROUTER_ENDPOINT == "openrouter/free"
        assert is_free_model(FREE_ROUTER_ENDPOINT) is False
        assert is_free_router(FREE_ROUTER_ENDPOINT) is True
        assert is_allowed_model(FREE_ROUTER_ENDPOINT) is False

    @pytest.mark.parametrize("model", BILLABLE_MODELS)
    def test_no_billable_model_is_allow_listed(self, model):
        assert is_allowed_model(model) is False


# ---------------------------------------------------------------------------
# Config-level validation
# ---------------------------------------------------------------------------
class TestConfigFreeModelValidation:
    def test_configured_models_are_free_by_default(self, monkeypatch):
        monkeypatch.setattr(config, "OPENROUTER_PRIMARY_MODEL", "qwen/qwen3.8-27b:free", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_REASONING_MODEL", "thinkingmachines/inkling-small:free", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_FALLBACK_MODEL", "nvidia/nemotron-3-super-120b-a12b:free", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_FREE_FALLBACK_MODELS", "nvidia/nemotron-3-super-120b-a12b:free", raising=False)
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

    def test_the_free_router_is_refused_at_construction(self):
        """The auto-router cannot be constructed, let alone called."""
        with pytest.raises(NonFreeModelError) as exc:
            ProviderEndpoint(
                provider=PROVIDER_OPENROUTER, model="openrouter/free",
                url="https://openrouter.ai/api/v1/chat/completions", headers={},
            )
        assert "openrouter/free" in str(exc.value)

    @pytest.mark.parametrize("model", BILLABLE_MODELS)
    def test_billable_endpoint_rejected_at_construction(self, model):
        with pytest.raises(NonFreeModelError):
            ProviderEndpoint(
                provider=PROVIDER_OPENROUTER, model=model, url="http://x", headers={}
            )

    def test_local_provider_exists_but_legacy_endpoints_do_not(self):
        """Local execution is back, but only behind the provider abstraction.

        The free-model gates below still apply to every OpenRouter request:
        adding Ollama must not have loosened them, so the old un-abstracted
        Ollama endpoint attributes must stay gone.
        """
        import config as config_module

        assert config_module.PROVIDER_OLLAMA in config_module.VALID_PROVIDERS
        for attr in ("OLLAMA_MODEL", "OLLAMA_FALLBACK_MODEL"):
            assert not hasattr(config_module, attr), attr
        from services.ai_provider import AIProvider  # noqa: F401

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
            provider=PROVIDER_OPENROUTER, model="qwen/qwen3.8-27b:free",
            url="http://x", headers={},
        )
        object.__setattr__(ep, "model", "openai/gpt-4o")
        with pytest.raises(NonFreeModelError):
            ep.build_payload([], 10, 0.1)


# ---------------------------------------------------------------------------
# Chain construction
# ---------------------------------------------------------------------------
#: Two explicitly configured free models, so chain-order assertions are about
#: order and not about how many models happen to be allow-listed today.
POOL_HEAD = "qwen/qwen3.8-27b:free"
POOL_TAIL = "nvidia/nemotron-3-super-120b-a12b:free"


@pytest.fixture
def free_env(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-test", raising=False)
    monkeypatch.setattr(config, "LLM_REQUIRE_FREE_MODELS", True, raising=False)
    monkeypatch.setattr(config, "OPENROUTER_PRIMARY_MODEL", POOL_HEAD, raising=False)
    monkeypatch.setattr(
        config, "OPENROUTER_REASONING_MODEL", "thinkingmachines/inkling-small:free",
        raising=False,
    )
    monkeypatch.setattr(config, "OPENROUTER_FALLBACK_MODEL", POOL_TAIL, raising=False)
    monkeypatch.setattr(
        config, "OPENROUTER_FREE_FALLBACK_MODELS", POOL_TAIL, raising=False
    )
    monkeypatch.setattr(config, "OPENROUTER_MAX_ATTEMPTS", 4, raising=False)
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
            assert is_allowed_model(model), f"{model} is not allow-listed"

    def test_chain_follows_the_configured_order(self, free_env):
        chain = LLMClient().build_chain("reasoning")
        models = [ep.model for ep in chain if ep.provider == PROVIDER_OPENROUTER]
        # The tier head is the deliberate choice and stays first, whatever the
        # registry's `rank` says about it.
        assert models[0] == "thinkingmachines/inkling-small:free"
        assert models[1] == POOL_TAIL

    def test_chain_never_contains_the_auto_router(self, free_env):
        for tier in ("fast", "reasoning", "fallback"):
            models = [e.model for e in LLMClient().build_chain(tier)]
            assert "openrouter/free" not in models, tier

    def test_chain_is_explicitly_configured_only(self, free_env):
        """Nothing appears in the chain unless an operator configured it."""
        chain = LLMClient().build_chain("fast")
        assert [e.model for e in chain] == [POOL_HEAD, POOL_TAIL]
        assert all(ep.provider == PROVIDER_OPENROUTER for ep in chain)
        assert all(ep.url.startswith("https://openrouter.ai/") for ep in chain)

    def test_missing_key_fails_loudly(self, monkeypatch):
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "", raising=False)
        with pytest.raises(LLMError) as exc:
            LLMClient().build_chain("fast")
        assert "OPENROUTER_API_KEY" in str(exc.value)

    def test_a_billable_primary_is_dropped_not_called(self, free_env, monkeypatch):
        """A paid id in .env must never be called.

        It used to raise, bricking the request. Now the bad model is *excluded*
        from the pool and the remaining free models serve the turn, which is
        strictly better: a typo in a model id degrades instead of breaking the
        assistant. The invariant that matters is unchanged -- it is never called.
        """
        monkeypatch.setattr(config, "OPENROUTER_PRIMARY_MODEL", "openai/gpt-4o", raising=False)
        client = LLMClient()
        chain = client.build_chain("fast")

        assert "openai/gpt-4o" not in [e.model for e in chain]
        assert chain, "the pool must still serve the request"

    def test_a_fully_billable_pool_raises_rather_than_calling(self, free_env, monkeypatch):
        """With nothing free left, the answer is an error, not a paid call."""
        for attr in (
            "OPENROUTER_PRIMARY_MODEL",
            "OPENROUTER_REASONING_MODEL",
            "OPENROUTER_FALLBACK_MODEL",
        ):
            monkeypatch.setattr(config, attr, "openai/gpt-4o", raising=False)
        monkeypatch.setattr(
            config, "OPENROUTER_FREE_FALLBACK_MODELS", "openai/gpt-4o", raising=False
        )
        with pytest.raises(NonFreeModelError):
            LLMClient().build_chain("fast")

    async def test_no_request_is_sent_for_a_billable_model(self, free_env, monkeypatch):
        """The whole point: rejection happens before any socket is opened."""
        monkeypatch.setattr(config, "OPENROUTER_PRIMARY_MODEL", "openai/gpt-4o", raising=False)
        client = LLMClient()
        exploding = _ExplodingClient()
        client._client = exploding

        def handler(url, payload, headers):
            # Only a *free* model may ever reach this.
            assert is_free_model(payload["model"]), payload["model"]
            return _ok("served by a free model")

        client._client = _Recorder(handler)
        assert await client.complete(
            [{"role": "user", "content": "x"}]
        ) == "served by a free model"
        assert all(
            is_free_model(r["json"]["model"]) for r in client._client.requests
        )


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
    async def test_a_billable_only_pool_never_reaches_the_socket(self, free_env, monkeypatch):
        for attr in (
            "OPENROUTER_PRIMARY_MODEL",
            "OPENROUTER_REASONING_MODEL",
            "OPENROUTER_FALLBACK_MODEL",
        ):
            monkeypatch.setattr(config, attr, "openai/gpt-4o", raising=False)
        monkeypatch.setattr(
            config, "OPENROUTER_FREE_FALLBACK_MODELS", "openai/gpt-4o", raising=False
        )
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
            provider=PROVIDER_OPENROUTER, model=POOL_HEAD,
            url="https://openrouter.ai/api/v1/chat/completions", headers={},
        )
        # A legitimate free request does build a payload.
        assert ep.build_payload([{"role": "user", "content": "hi"}], 10, 0.1)["model"] == (
            POOL_HEAD
        )
        assert exploding.calls == []


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
    async def test_429_falls_to_the_next_configured_free_model(self, free_env):
        def handler(url, payload, headers):
            if payload["model"] == POOL_HEAD:
                return _err(429)
            return _ok("served by fallback")

        client = LLMClient(max_retries=1)
        client._client = _Recorder(handler)
        result = await client.complete([{"role": "user", "content": "x"}])
        assert result == "served by fallback"
        assert client._client.requests[-1]["json"]["model"] == POOL_TAIL
        # A 429 must not be retried on the same model: the point of the pool is
        # to move to a different free model, not to hammer a contended one.
        assert [r["json"]["model"] for r in client._client.requests] == [
            POOL_HEAD, POOL_TAIL,
        ]

    async def test_500_falls_through_chain(self, free_env):
        def handler(url, payload, headers):
            if payload["model"] == POOL_HEAD:
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
