"""Ollama (local) provider tests.

The OpenRouter suites pin ``config.PROVIDER`` to ``openrouter`` (see
``conftest.py``); these tests opt into the local path explicitly and assert
the properties the migration must guarantee:

* Ollama is the default provider and needs no API key.
* Tier routing is configuration, not hard-coding.
* A missing model reports ``MODEL NOT INSTALLED`` with an ``ollama pull``
  hint and never downloads anything.
* An unreachable endpoint reports ``ollama serve`` — it never silently
  falls back to OpenRouter.
* Contract validation and the attempt budget still apply on the local path.
"""

import asyncio

import httpx
import pytest

from config import TIER_FAST, TIER_REASONING, PROVIDER_OLLAMA, config
from services.llm_client import AllModelsUnavailableError, LLMClient


class FakeOllama:
    """Stands in for ``services.ollama_client.OllamaClient``."""

    def __init__(self, content="hello", exc=None, chunks=None):
        self.content = content
        self.exc = exc
        self.chunks = chunks
        self.calls = []

    async def chat(self, messages, model, stream=False, tools=None,
                   options=None, keep_alive="5m"):
        self.calls.append({"model": model, "stream": stream, "tools": tools})
        if self.exc is not None:
            raise self.exc
        if stream:
            async def gen():
                for c in (self.chunks or [self.content]):
                    yield c
            return gen()
        return self.content

    async def aclose(self):
        return None


def _use(fake, monkeypatch):
    monkeypatch.setattr(config, "PROVIDER", PROVIDER_OLLAMA)
    import services.ollama_client as oc
    monkeypatch.setattr(oc, "get_ollama_client", lambda *a, **k: fake)


class TestProviderDefault:
    def test_ollama_is_the_default_provider(self):
        import os

        from config import Config

        assert PROVIDER_OLLAMA == "ollama"
        # Class-level default (the autouse fixture pins the *instance* to
        # openrouter for the OpenRouter suites).
        assert Config.PROVIDER == os.getenv("AI_PROVIDER", "ollama")

    def test_ollama_needs_no_api_key(self, monkeypatch):
        monkeypatch.setattr(config, "PROVIDER", PROVIDER_OLLAMA)
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "", raising=False)
        assert config.provider_ready is True

    def test_tier_routing_is_configuration(self, monkeypatch):
        monkeypatch.setattr(config, "OLLAMA_GENERAL_MODEL", "general-model")
        monkeypatch.setattr(config, "OLLAMA_CODING_MODEL", "code-model")
        monkeypatch.setattr(config, "OLLAMA_FAST_MODEL", "fast-model")
        assert config.ollama_model_for_tier("general") == "general-model"
        assert config.ollama_model_for_tier(TIER_FAST) == "fast-model"
        assert config.ollama_model_for_tier(TIER_REASONING) == "code-model"

    def test_openrouter_pool_is_not_polluted_by_local_models(self):
        """model_pool() must keep returning only OpenRouter ids."""
        pool = config.model_pool(TIER_FAST)
        assert pool
        assert all(":free" in m for m in pool), pool


class TestLocalCompletion:
    @pytest.mark.asyncio
    async def test_routes_to_the_configured_local_model(self, monkeypatch):
        fake = FakeOllama(content="مرحبا")
        _use(fake, monkeypatch)
        out = await LLMClient().complete(
            [{"role": "user", "content": "hi"}], tier=TIER_REASONING
        )
        assert out == "مرحبا"
        assert fake.calls[0]["model"] == config.OLLAMA_CODING_MODEL

    @pytest.mark.asyncio
    async def test_missing_model_says_pull_and_never_downloads(
        self, monkeypatch
    ):
        err = httpx.HTTPStatusError(
            "404", request=httpx.Request("POST", "http://localhost:11434/api/chat"),
            response=httpx.Response(404, text='{"error": "model not found"}'),
        )
        fake = FakeOllama(exc=err)
        _use(fake, monkeypatch)
        with pytest.raises(AllModelsUnavailableError) as exc:
            await LLMClient().complete([{"role": "user", "content": "hi"}])
        msg = str(exc.value)
        assert "MODEL NOT INSTALLED" in msg
        assert "ollama pull" in msg
        assert fake.calls, "the chat endpoint was tried, nothing was pulled"

    @pytest.mark.asyncio
    async def test_unreachable_endpoint_never_falls_back_to_openrouter(
        self, monkeypatch
    ):
        fake = FakeOllama(
            exc=httpx.ConnectError("refused"),
        )
        _use(fake, monkeypatch)
        with pytest.raises(AllModelsUnavailableError) as exc:
            await LLMClient().complete([{"role": "user", "content": "hi"}])
        assert "ollama serve" in str(exc.value)

    @pytest.mark.asyncio
    async def test_stream_delivers_deltas_and_returns_the_full_text(
        self, monkeypatch
    ):
        fake = FakeOllama(chunks=["he", "llo"])
        _use(fake, monkeypatch)
        seen = []

        async def on_delta(d):
            seen.append(d)

        out = await LLMClient().stream(
            [{"role": "user", "content": "hi"}], on_delta=on_delta
        )
        assert out == "hello"
        assert seen == ["he", "llo"]
        assert fake.calls[0]["stream"] is True

    @pytest.mark.asyncio
    async def test_contract_violation_is_not_returned_as_an_answer(
        self, monkeypatch
    ):
        from services.completion_validator import CompletionContract

        fake = FakeOllama(content="")
        _use(fake, monkeypatch)
        strict = CompletionContract(
            allow_prose=False, require_tool=True, label="tool-only"
        )
        client = LLMClient()
        with pytest.raises(AllModelsUnavailableError):
            await client.complete(
                [{"role": "user", "content": "hi"}], contract=strict
            )
        # It was attempted, rejected by the contract, and never handed up.
        assert client.metrics.invalid
