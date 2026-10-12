"""Router-first: the registry answers when it can, the chain when it cannot.

Three properties the design rests on, each proved against a real temp
registry with the transport mocked:

* the registry's answer reaches the caller **before** the configured chain runs;
* any registry failure falls through to the chain (or the local default),
  which is unchanged — **unless the registry default is local**, which refuses
  the crossing to a hosted model instead (TestTheLocalToHostedGuard);
* **one** attempt budget spans both paths — router-first cannot double the
  ceiling `_AttemptBudget` exists to protect.

The chain is left unkeyed by default (``_unconfigured_chain``), so "the chain
was reached" is observable as a deterministic configuration error rather than
a request to a real endpoint.
"""

import pytest

from config import config
from core.providers import OpenAICompatibleClient, save_model, save_provider
from core.providers.client import ProviderTransportError
from services.llm_client import LLMClient, LLMError, AllModelsUnavailableError

from tests.test_llm_client import RecordingClient, completion


@pytest.fixture(autouse=True)
def _unconfigured_chain(monkeypatch):
    """No key by default: an unconfigured chain fails fast, never online."""
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "", raising=False)


@pytest.fixture
def chain(monkeypatch):
    """A pinned two-model free pool plus a recording transport."""
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-test", raising=False)
    monkeypatch.setattr(
        config, "OPENROUTER_PRIMARY_MODEL", "qwen/qwen3.8-27b:free", raising=False
    )
    monkeypatch.setattr(
        config, "OPENROUTER_REASONING_MODEL", "qwen/qwen3.8-27b:free", raising=False
    )
    monkeypatch.setattr(
        config, "OPENROUTER_FALLBACK_MODEL",
        "nvidia/nemotron-3-super-120b-a12b:free", raising=False,
    )
    monkeypatch.setattr(config, "OPENROUTER_MAX_ATTEMPTS", 4, raising=False)
    return RecordingClient(lambda url, payload, headers: completion("chain answer"))


async def _registry(*pins):
    """A **local** provider with models `(model_id, priority)` — lower first."""
    provider = await save_provider(
        None, name="Local", kind="ollama", base_url="http://localhost:11434"
    )
    for model_id, priority in pins:
        await save_model(
            provider["id"], model_id,
            capabilities=["chat", "tools", "coding", "streaming"],
            priority=priority,
        )
    return provider


async def _hosted_registry(*pins):
    """A **hosted** default provider with models.

    The fall-through tests use this rather than `_registry`: falling from a
    hosted registry default to the chain is visible and preserved, while
    falling from a *local* default is the silent crossing the guard refuses.
    """
    provider = await save_provider(
        None, name="Cloud", kind="openai_compatible",
        base_url="https://api.example.com",
    )
    for model_id, priority in pins:
        await save_model(
            provider["id"], model_id,
            capabilities=["chat", "tools", "coding", "streaming"],
            priority=priority,
        )
    return provider


def _patch_complete(monkeypatch, responder):
    """Replace the registry transport's non-streaming call.

    ``responder(model)`` returns the text (or an Exception to raise).
    Returns the ordered list of models actually asked.
    """
    calls: list = []

    async def fake(self, messages, model, max_tokens=2048, temperature=0.7,
                   tools=None, stop=None):
        calls.append(model)
        result = responder(model)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(OpenAICompatibleClient, "complete", fake)
    return calls


def _patch_stream(monkeypatch, script):
    """Replace the registry transport's streaming call.

    ``script(model)`` returns either an Exception (raised before any delta),
    or ``(deltas, error_or_None)`` — deltas yielded first, then optionally a
    mid-stream failure. Returns the ordered list of models actually asked.
    """
    calls: list = []

    async def fake(self, messages, model, max_tokens=2048, temperature=0.7,
                   tools=None, stop=None, tool_calls_out=None):
        calls.append(model)
        spec = script(model)
        if isinstance(spec, Exception):
            raise spec
            yield  # unreachable; a function with `yield` is an async generator
        deltas, error = spec
        for piece in deltas:
            yield piece
        if error is not None:
            raise error

    monkeypatch.setattr(OpenAICompatibleClient, "stream", fake)
    return calls


class TestRegistryFirst:
    async def test_the_registry_answers_before_the_chain_runs(
        self, temp_core_db, temp_secrets, monkeypatch
    ):
        """Router-first, proved by the chain being unable to answer at all."""
        await _registry(("local:7b", 1))
        calls = _patch_complete(monkeypatch, lambda model: "registry answer")
        llm = LLMClient()

        answer = await llm.complete([{"role": "user", "content": "hi"}])

        # The chain is unkeyed in this fixture set: reaching it would raise
        # LLMError instead of returning. Getting text back at all proves the
        # registry answered first.
        assert answer == "registry answer"
        assert calls == ["local:7b"]

    async def test_chain_reachable_after_registry_failure(
        self, temp_core_db, temp_secrets, chain, monkeypatch
    ):
        """A transport failure falls through; the chain then answers.

        The registry default is *hosted* here: a local default refuses the
        crossing instead (TestTheLocalToHostedGuard).
        """
        await _hosted_registry(("cloud:7b", 1))
        calls = _patch_complete(
            monkeypatch,
            lambda model: ProviderTransportError("HTTP 500: upstream", status=500),
        )
        llm = LLMClient()
        llm._client = chain  # noqa: SLF001 - the same seam the chain tests use

        answer = await llm.complete([{"role": "user", "content": "hi"}])

        assert answer == "chain answer"
        assert calls == ["cloud:7b"]
        assert chain.requests, "the chain should have served the request"

    async def test_one_budget_spans_registry_and_chain(
        self, temp_core_db, temp_secrets, chain, monkeypatch
    ):
        """Registry attempts must consume the same ceiling the chain draws.

        Hosted default: the point is budget sharing, and a local default
        would refuse the chain fall-through for an unrelated reason.
        """
        await _hosted_registry(("aaa:7b", 1), ("bbb:7b", 2))
        calls = _patch_complete(
            monkeypatch,
            lambda model: ProviderTransportError("HTTP 500: upstream", status=500),
        )
        llm = LLMClient(max_attempts=2)
        llm._client = chain  # noqa: SLF001

        with pytest.raises(AllModelsUnavailableError) as excinfo:
            await llm.complete([{"role": "user", "content": "hi"}])

        # Both registry candidates spent the two attempts, so the chain —
        # which would have answered — never got one. A fresh per-path budget
        # would have let it answer, which is exactly the multiplication the
        # shared budget exists to prevent.
        assert calls == ["aaa:7b", "bbb:7b"]
        assert chain.requests == []
        assert "2 attempts" in str(excinfo.value)

    async def test_invalid_registry_output_moves_to_the_next_candidate(
        self, temp_core_db, temp_secrets, monkeypatch
    ):
        """The contract check applies to registry answers too."""
        await _registry(("aaa:7b", 1), ("bbb:7b", 2))
        calls = _patch_complete(
            monkeypatch,
            lambda model: "" if model == "aaa:7b" else "the real answer",
        )
        llm = LLMClient()

        answer = await llm.complete([{"role": "user", "content": "hi"}])

        assert answer == "the real answer"
        assert calls == ["aaa:7b", "bbb:7b"]

    async def test_an_unconfigured_chain_is_reached_not_skipped(
        self, temp_core_db, temp_secrets, monkeypatch
    ):
        """Without registry models the configured chain runs, as before."""
        calls = _patch_complete(monkeypatch, lambda model: "should not happen")
        llm = LLMClient()

        with pytest.raises(LLMError, match="OPENROUTER_API_KEY"):
            await llm.complete([{"role": "user", "content": "hi"}])

        assert calls == []  # the registry never had a model to offer

    async def test_the_ollama_default_path_still_answers_when_the_registry_fails(
        self, temp_core_db, temp_secrets, monkeypatch
    ):
        """The production default (local Ollama) is one of the fall-throughs."""
        await _registry(("local:7b", 1))
        _patch_complete(
            monkeypatch,
            lambda model: ProviderTransportError("HTTP 500: upstream", status=500),
        )
        monkeypatch.setattr(config, "PROVIDER", "ollama")

        class FakeOllama:
            async def chat(self, messages, model=None, stream=False,
                           tools=None, options=None):
                return "local answer"

        monkeypatch.setattr(LLMClient, "_ollama", lambda self: FakeOllama())
        llm = LLMClient()

        answer = await llm.complete([{"role": "user", "content": "hi"}])

        assert answer == "local answer"


class TestTheLocalToHostedGuard:
    """A local default that cannot answer never crosses to a hosted model.

    The registry default is the user's statement about where this install's
    traffic belongs. Handing the turn to a cloud model would send data out of
    the machine the user never chose to send — a fallback that is silent by
    definition. The fall-through tests above keep their meaning by running
    with a *hosted* default instead.
    """

    @pytest.fixture
    def chain_spy(self, monkeypatch):
        """Record chain construction and pin the chain to hosted."""
        calls: list = []
        original = LLMClient.build_chain

        def spy(self, tier, required=None):
            calls.append(tier)
            return original(self, tier, required)

        monkeypatch.setattr(LLMClient, "build_chain", spy)
        # The ambient env default here is openrouter; pin it so the guard,
        # not the environment, is what decides.
        monkeypatch.setattr(config, "PROVIDER", "openrouter")
        return calls

    async def test_a_local_default_refuses_the_hosted_chain(
        self, temp_core_db, temp_secrets, chain_spy, monkeypatch
    ):
        await _registry(("local:7b", 1))
        _patch_complete(
            monkeypatch,
            lambda model: ProviderTransportError("HTTP 500: upstream", status=500),
        )
        llm = LLMClient()

        with pytest.raises(AllModelsUnavailableError) as caught:
            await llm.complete([{"role": "user", "content": "hi"}])

        assert caught.value.kind == "local_default_blocked"
        assert chain_spy == []  # the hosted chain was never even built

    async def test_the_streaming_path_refuses_too(
        self, temp_core_db, temp_secrets, chain_spy, monkeypatch
    ):
        await _registry(("local:7b", 1))
        _patch_stream(
            monkeypatch,
            lambda model: ProviderTransportError("Transport error: ConnectError"),
        )
        llm = LLMClient()

        with pytest.raises(AllModelsUnavailableError) as caught:
            await llm.stream([{"role": "user", "content": "hi"}])

        assert caught.value.kind == "local_default_blocked"
        assert chain_spy == []

    async def test_a_custom_local_endpoint_is_local_too(
        self, temp_core_db, temp_secrets, chain_spy
    ):
        """A NAS-backed OpenAI-compatible provider is local by address class."""
        await save_provider(
            None, name="NAS", kind="openai_compatible",
            base_url="http://192.168.1.10:8080",
        )
        llm = LLMClient()

        with pytest.raises(AllModelsUnavailableError) as caught:
            await llm.complete([{"role": "user", "content": "hi"}])

        assert caught.value.kind == "local_default_blocked"
        assert chain_spy == []

    async def test_a_hosted_default_still_runs_the_hosted_chain(
        self, temp_core_db, temp_secrets, chain, chain_spy, monkeypatch
    ):
        await _hosted_registry(("cloud:7b", 1))
        _patch_complete(
            monkeypatch,
            lambda model: ProviderTransportError("HTTP 500: upstream", status=500),
        )
        llm = LLMClient()
        llm._client = chain  # noqa: SLF001

        answer = await llm.complete([{"role": "user", "content": "hi"}])

        assert answer == "chain answer"
        assert chain_spy, "the chain was consulted, not blocked"

    async def test_an_unreadable_registry_never_blocks(
        self, temp_core_db, temp_secrets, chain, chain_spy, monkeypatch
    ):
        """The guard must never be the *reason* a request fails."""

        async def boom():
            raise RuntimeError("db gone")

        monkeypatch.setattr("core.providers.default_provider", boom)
        llm = LLMClient()
        llm._client = chain  # noqa: SLF001

        answer = await llm.complete([{"role": "user", "content": "hi"}])

        assert answer == "chain answer"
        assert chain_spy

    async def test_the_refusal_is_a_real_bilingual_notice(
        self, temp_core_db, temp_secrets, chain_spy, monkeypatch
    ):
        """The user is told in their language, not handed a generic notice."""
        from services.llm_client import user_notice

        await _registry(("local:7b", 1))
        _patch_complete(
            monkeypatch,
            lambda model: ProviderTransportError("HTTP 500: upstream", status=500),
        )
        llm = LLMClient()

        with pytest.raises(AllModelsUnavailableError) as caught:
            await llm.complete([{"role": "user", "content": "hi"}])

        english = user_notice(caught.value, "en")
        arabic = user_notice(caught.value, "ar")
        assert english and arabic and english != arabic  # genuinely translated
        assert "hosted" in english
        assert "أونلاين" in arabic
        assert chain_spy == []


class TestRegistryStreaming:
    async def test_deltas_reach_the_caller(
        self, temp_core_db, temp_secrets, monkeypatch
    ):
        await _registry(("local:7b", 1))
        calls = _patch_stream(monkeypatch, lambda model: (["Hel", "lo"], None))
        seen: list = []

        async def collect(delta):
            seen.append(delta)

        llm = LLMClient()
        full = await llm.stream(
            [{"role": "user", "content": "hi"}], on_delta=collect
        )

        assert full == "Hello"
        assert seen == ["Hel", "lo"]
        assert calls == ["local:7b"]

    async def test_a_native_tool_call_reaches_validation_not_the_screen(
        self, temp_core_db, temp_secrets, monkeypatch
    ):
        """tool_calls accumulated off-wire join `full` but never `on_delta`.

        The client's stream() keeps native tool calls out of the yielded text;
        this pins the other half: `_stream_registry` folds them into the text
        that gets validated (so the call is *seen*) without pushing one byte
        to the screen (so "tool calls are not prose" stays structural).
        """
        await _registry(("local:7b", 1))
        rendered = '{"action": "get_time", "params": {}}'
        calls: list = []

        async def fake(self, messages, model, max_tokens=2048, temperature=0.7,
                       tools=None, stop=None, tool_calls_out=None):
            calls.append(model)
            if tool_calls_out is not None:
                tool_calls_out.append(rendered)
            # No text deltas at all: a pure native-tool response.
            if False:
                yield  # makes this an async generator that yields nothing

        monkeypatch.setattr(OpenAICompatibleClient, "stream", fake)
        seen: list = []

        async def collect(delta):
            seen.append(delta)

        llm = LLMClient()
        full = await llm.stream(
            [{"role": "user", "content": "hi"}], on_delta=collect
        )

        assert full == rendered
        assert seen == []          # nothing rendered-as-prose reached the UI
        assert calls == ["local:7b"]

    async def test_failure_before_a_delta_falls_through_to_the_chain(
        self, temp_core_db, temp_secrets, monkeypatch
    ):
        """Nothing was emitted, so the chain may still stream from scratch."""
        await _hosted_registry(("cloud:7b", 1))
        calls = _patch_stream(
            monkeypatch,
            lambda model: ProviderTransportError("Transport error: ConnectError"),
        )
        llm = LLMClient()

        # The chain is unkeyed, so reaching it is observable as its
        # deterministic configuration error — not a network call.
        with pytest.raises(LLMError, match="OPENROUTER_API_KEY"):
            await llm.stream([{"role": "user", "content": "hi"}])

        assert calls == ["cloud:7b"]

    async def test_failure_after_a_delta_recovers_with_a_blocking_call(
        self, temp_core_db, temp_secrets, monkeypatch
    ):
        """Tokens are on screen: never re-emit — replace with a full answer."""
        await _registry(("local:7b", 1))
        _patch_stream(monkeypatch, lambda model: (["partial "], ProviderTransportError("boom")))
        _patch_complete(monkeypatch, lambda model: "recovered answer")
        seen: list = []

        async def collect(delta):
            seen.append(delta)

        llm = LLMClient()
        full = await llm.stream(
            [{"role": "user", "content": "hi"}], on_delta=collect
        )

        assert full == "recovered answer"
        assert seen == ["partial "]  # no second stream of anything


def test_the_outcome_is_read_from_the_exception_not_the_message():
    """`kind` wins; the string-matching fallbacks remain only for old callers.

    The old version matched the literal substring "Timeout" in str(exc), so a
    reworded message would silently re-label every future failure.
    """
    llm = LLMClient()

    assert llm._registry_outcome(
        ProviderTransportError("worded differently now", kind="rate_limited")
    ) == "rate_limited"
    assert llm._registry_outcome(
        ProviderTransportError("anything", status=500)
    ) == "server_error"                      # legacy: status, no kind
    assert llm._registry_outcome(
        ProviderTransportError("Timeout waiting")
    ) == "timeout"                           # legacy: message, no kind/status
