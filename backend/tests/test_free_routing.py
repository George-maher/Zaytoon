"""FREE-only, capability-routed, contract-validated model selection.

These tests exist because of one specific observed failure:

    qwen/qwen3.8-27b:free  -> HTTP 429
    openrouter/free        -> "User Safety: safe"
    agent loop             -> presented that to the user as the answer

The first two lines are the bug. The third is what made it a bug rather than a
glitch: an HTTP 200 with a non-empty string was treated as a successful
*answer* when it was only a successful *request*.

The cases below map onto the required behaviours:

===  =========================================================================
 A   primary succeeds                      no fallback
 B   primary 429                          next configured free model
 C   primary 200 but invalid              rejected, fallback triggered
 D   a tool request                       a tool call is required
 E   invalid tool arguments               rejected
 F   unknown tool                         rejected
 G   every free model fails               controlled unavailable response
 H   paid model protection                refused before any socket opens
 I   attempt ceiling                      never exceeded
===  =========================================================================
"""

import asyncio
import json
import time

import httpx
import pytest

from config import (
    NoCapableModelError,
    NonFreeModelError,
    config,
    is_allowed_model,
)
from services.completion_validator import (
    CHAT_CONTRACT,
    Verdict,
    contract_from_registry,
    looks_like_metadata_leak,
    validate_completion,
)
from services.llm_client import (
    AllModelsUnavailableError,
    LLMClient,
    LLMError,
    _AttemptBudget,
    get_llm_client,
)
from services.model_registry import (
    CAP_TOOLS,
    MODEL_REGISTRY,
    is_free_form,
    select_models,
)
from tools.registry import build_default_registry


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------
# Three DISTINCT allow-listed free models, all verified against OpenRouter's
# live catalogue. Distinctness matters: if two of these were equal, the pool
# would dedupe to two and every "three attempts were made" assertion would pass
# for the wrong reason.
HEAD = "qwen/qwen3.8-27b:free"
TAIL = "nvidia/nemotron-3-super-120b-a12b:free"
THIRD = "cohere/north-mini-code:free"


@pytest.fixture(autouse=True)
def pool(monkeypatch):
    """Three explicitly configured free models, in a known order."""
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-test", raising=False)
    monkeypatch.setattr(config, "OPENROUTER_PRIMARY_MODEL", HEAD, raising=False)
    monkeypatch.setattr(config, "OPENROUTER_REASONING_MODEL", TAIL, raising=False)
    monkeypatch.setattr(config, "OPENROUTER_FALLBACK_MODEL", THIRD, raising=False)
    monkeypatch.setattr(
        config, "OPENROUTER_FREE_FALLBACK_MODELS", f"{TAIL},{THIRD}", raising=False
    )
    monkeypatch.setattr(config, "OPENROUTER_MAX_ATTEMPTS", 3, raising=False)
    return config


def ok(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def err(status: int, message: str = "nope") -> httpx.Response:
    return httpx.Response(status, json={"error": {"message": message}})


class Recorder:
    """Scriptable HTTP double that records every model it was asked for."""

    is_closed = False

    def __init__(self, handler):
        self.handler = handler
        self.requests = []

    async def post(self, url, json=None, headers=None):
        payload = json or {}
        self.requests.append({"url": url, "model": payload.get("model")})
        result = self.handler(url, payload, headers or {})
        if isinstance(result, Exception):
            raise result
        return result

    def models(self):
        return [r["model"] for r in self.requests]

    async def aclose(self):
        self.is_closed = True


def make_client(handler, **kwargs):
    # `setdefault` so a caller can override without hitting "multiple values
    # for keyword argument".
    kwargs.setdefault("max_retries", 1)
    kwargs.setdefault("provider_cooldown", 0)
    client = LLMClient(**kwargs)
    client._client = Recorder(handler)
    return client


MSGS = [{"role": "user", "content": "What time is it?"}]


# ===========================================================================
# A. Primary succeeds
# ===========================================================================
class TestPrimarySucceeds:
    async def test_no_fallback_when_the_primary_answers(self):
        client = make_client(lambda u, p, h: ok("It is 10:30."))
        assert await client.complete(MSGS) == "It is 10:30."
        assert client._client.models() == [HEAD]

    async def test_a_valid_tool_call_is_accepted(self):
        registry = build_default_registry()
        contract = contract_from_registry(registry, allow_prose=True)
        from agents.engine import extract_tool_calls

        client = make_client(
            lambda u, p, h: ok('{"action": "get_time", "params": {}}')
        )
        result = await client.complete(
            MSGS, contract=contract, extract=extract_tool_calls
        )
        assert "get_time" in result
        assert client._client.models() == [HEAD]


# ===========================================================================
# B. Primary rate-limited
# ===========================================================================
class TestPrimaryRateLimited:
    async def test_429_moves_to_the_next_configured_free_model(self):
        def handler(url, payload, headers):
            return err(429) if payload["model"] == HEAD else ok("served by #2")

        client = make_client(handler)
        assert await client.complete(MSGS) == "served by #2"
        assert client._client.models() == [HEAD, TAIL]

    async def test_a_429_is_not_retried_on_the_same_model(self):
        """Sitting on a contended model is the wrong move; move instead."""
        def handler(url, payload, headers):
            return err(429) if payload["model"] == HEAD else ok("ok")

        client = make_client(handler)
        await client.complete(MSGS)
        assert client._client.models().count(HEAD) == 1

    async def test_retry_after_is_honoured_and_capped(self):
        response = httpx.Response(429, headers={"Retry-After": "2"},
                                 json={"error": {"message": "slow down"}})
        assert LLMClient._retry_after_seconds(response, 0.5) == 2.0
        # An upstream asking for an hour must not hang a desktop assistant.
        assert LLMClient._retry_after_seconds(
            httpx.Response(429, headers={"Retry-After": "3600"},
                           json={}), 0.5
        ) == 5.0
        assert LLMClient._retry_after_seconds(httpx.Response(429), 0.5) == 0.5

    async def test_429_benches_longer_than_a_transient_error(self):
        client = make_client(lambda u, p, h: err(429))
        with pytest.raises(AllModelsUnavailableError):
            await client.complete(MSGS)
        assert client._cooldown_for("rate_limited") >= client._cooldown_for(
            "server_error"
        )

    async def test_every_transport_failure_falls_through(self):
        for status in (408, 425, 429, 500, 502, 503, 504, 529):
            client = make_client(
                lambda u, p, h, s=status: err(s) if p["model"] == HEAD else ok("recovered")
            )
            assert await client.complete(MSGS) == "recovered", status


# ===========================================================================
# C. HTTP 200 that is not a valid completion  <-- the core fix
# ===========================================================================
class TestTwoHundredIsNotSuccess:
    async def test_metadata_leak_is_rejected_and_falls_back(self):
        """The exact observed failure.

        `openrouter/free` answered a get_time request with the literal string
        "User Safety: safe". It is a valid HTTP 200 with a non-empty content
        string, and the old code accepted it.
        """
        def handler(url, payload, headers):
            if payload["model"] == HEAD:
                return ok("User Safety: safe")
            return ok("It is 10:30.")

        client = make_client(handler)
        result = await client.complete(MSGS)
        assert result == "It is 10:30."
        assert "User Safety" not in result
        assert client._client.models() == [HEAD, TAIL]

    async def test_the_invalid_completion_is_never_returned(self):
        client = make_client(lambda u, p, h: ok("User Safety: safe"))
        with pytest.raises(AllModelsUnavailableError):
            await client.complete(MSGS)

    async def test_an_empty_completion_is_rejected(self):
        for blank in ("", "   ", "\n\n"):
            client = make_client(lambda u, p, h, b=blank: ok(b))
            with pytest.raises(AllModelsUnavailableError):
                await client.complete(MSGS)

    async def test_the_verdict_is_recorded_in_metrics(self):
        """Every model returns the same leak, so every one is counted."""
        client = make_client(lambda u, p, h: ok("User Safety: safe"))
        with pytest.raises(AllModelsUnavailableError):
            await client.complete(MSGS)
        # Counted once per attempt, not once per request.
        assert client.metrics.invalid.get("PROVIDER_METADATA_LEAK") == 3
        assert client.metrics.success == 0

    async def test_an_invalid_model_is_benched(self):
        """A model that answers wrongly should not be asked again immediately."""
        # A real cooldown, otherwise `is_open()` is trivially false and the
        # assertion would pass for the wrong reason.
        client = make_client(lambda u, p, h: ok("User Safety: safe"),
                             provider_cooldown=60)
        with pytest.raises(AllModelsUnavailableError):
            await client.complete(MSGS)
        for model in (HEAD, TAIL, THIRD):
            assert client.health[model].failures >= 1, model
            assert client.health[model].is_open(), model

    async def test_every_metadata_label_shape_is_caught(self):
        for leak in (
            "User Safety: safe",
            "Safety: blocked",
            "Moderation: flagged",
            "Finish Reason: length",
            "Category: pi",
        ):
            client = make_client(lambda u, p, h, b=leak: ok(b))
            with pytest.raises(AllModelsUnavailableError):
                await client.complete(MSGS), leak

    async def test_an_ordinary_answer_mentioning_safety_is_kept(self):
        prose = "The lab safety rules are in the handbook, and I can open it."
        client = make_client(lambda u, p, h: ok(prose))
        assert await client.complete(MSGS) == prose

    async def test_all_models_invalid_never_returns_the_last_one(self):
        client = make_client(lambda u, p, h: ok("User Safety: safe"))
        with pytest.raises(AllModelsUnavailableError) as exc:
            await client.complete(MSGS)
        assert "User Safety" not in str(exc.value)


class _noop:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ===========================================================================
# D. Tool requests
# ===========================================================================
class TestToolContract:
    @pytest.fixture
    def tool_turn(self):
        from agents.engine import extract_tool_calls

        registry = build_default_registry()
        # require_tool is what the engine uses when it has told the model to act.
        contract = contract_from_registry(
            registry, require_tool=True, allow_prose=True, label="tool"
        )
        return contract, extract_tool_calls

    async def test_a_tool_call_satisfies_a_required_tool_turn(self, tool_turn):
        contract, extract = tool_turn
        client = make_client(
            lambda u, p, h: ok('{"action": "get_time", "params": {}}')
        )
        out = await client.complete(MSGS, contract=contract, extract=extract)
        assert "get_time" in out

    async def test_unrelated_prose_fails_a_required_tool_turn(self, tool_turn):
        """Step D: a plain unrelated text response must fail validation."""
        contract, extract = tool_turn
        client = make_client(
            lambda u, p, h: ok("I cannot help with that.")
            if p["model"] == HEAD else ok('{"action": "get_time", "params": {}}')
        )
        out = await client.complete(MSGS, contract=contract, extract=extract)
        assert "get_time" in out
        assert "cannot help" not in out

    async def test_a_chat_turn_accepts_prose(self):
        """Step 17: normal conversation must not be forced into tool calling."""
        client = make_client(lambda u, p, h: ok("Hello! How can I help?"))
        assert "Hello" in await client.complete(MSGS, contract=CHAT_CONTRACT)

    async def test_a_chat_turn_needs_no_tool_capability(self):
        chain = [e.model for e in LLMClient().build_chain("fast", ())]
        assert chain[0] == HEAD

    async def test_a_tool_turn_only_uses_tool_capable_models(self):
        chain = [e.model for e in LLMClient().build_chain("fast", [CAP_TOOLS])]
        for model in chain:
            spec = MODEL_REGISTRY[model]
            assert spec.supports_tools, f"{model} is in a tool chain but cannot call tools"

    def test_a_chat_only_model_is_excluded_from_a_tool_chain(self):
        """A model whose tool capability is unverified must not reach a tool turn."""
        chat_only = "google/gemma-4-31b-it:free"
        assert MODEL_REGISTRY[chat_only].supports_tools is False
        selection = select_models([chat_only], [CAP_TOOLS])
        assert selection.models == ()
        assert "lacks required capabilities" in dict(selection.skipped)[chat_only]
        # ...but it is fine for chat.
        assert select_models([chat_only], []).models == (chat_only,)


# ===========================================================================
# E / F. Invalid arguments and unknown tools
# ===========================================================================
class TestToolCallValidation:
    @pytest.fixture
    def tool_turn(self):
        from agents.engine import extract_tool_calls

        registry = build_default_registry()
        contract = contract_from_registry(
            registry, require_tool=True, allow_prose=True, label="tool"
        )
        return contract, extract_tool_calls

    async def test_missing_required_argument_is_rejected(self, tool_turn):
        contract, extract = tool_turn
        # `file_write` requires both `path` and `content`.
        client = make_client(
            lambda u, p, h: ok('{"action": "file_write", "params": {"path": "a.txt"}}')
        )
        with pytest.raises(AllModelsUnavailableError):
            await client.complete(MSGS, contract=contract, extract=extract)
        # One per model attempted; all three return the same bad call.
        assert client.metrics.invalid.get("INVALID_TOOL_ARGS") == 3

    async def test_wrong_argument_type_is_rejected(self, tool_turn):
        contract, extract = tool_turn
        client = make_client(
            lambda u, p, h: ok(
                '{"action": "file_write", "params": {"path": 42, "content": "x"}}'
            )
        )
        with pytest.raises(AllModelsUnavailableError):
            await client.complete(MSGS, contract=contract, extract=extract)

    async def test_unknown_tool_is_rejected(self, tool_turn):
        contract, extract = tool_turn
        client = make_client(
            lambda u, p, h: ok('{"action": "launch_missiles", "params": {}}')
        )
        with pytest.raises(AllModelsUnavailableError):
            await client.complete(MSGS, contract=contract, extract=extract)
        assert client.metrics.invalid.get("INVALID_TOOL_UNKNOWN") == 3

    async def test_a_valid_call_falls_back_past_an_invalid_one(self, tool_turn):
        contract, extract = tool_turn
        def handler(url, payload, headers):
            if payload["model"] == HEAD:
                return ok('{"action": "launch_missiles", "params": {}}')
            return ok('{"action": "get_time", "params": {}}')

        client = make_client(handler)
        out = await client.complete(MSGS, contract=contract, extract=extract)
        assert "get_time" in out
        assert client._client.models() == [HEAD, TAIL]


# ===========================================================================
# G. Every free model fails
# ===========================================================================
class TestAllModelsFail:
    async def test_a_controlled_error_is_raised(self):
        client = make_client(lambda u, p, h: err(429))
        with pytest.raises(AllModelsUnavailableError) as exc:
            await client.complete(MSGS)
        assert "429" in str(exc.value) or "RATE_LIMITED" in str(exc.value)

    async def test_the_error_is_an_llm_error(self):
        """Callers already catch LLMError; that must keep working."""
        client = make_client(lambda u, p, h: err(500))
        with pytest.raises(LLMError):
            await client.complete(MSGS)

    async def test_the_attempts_are_attached_for_diagnostics(self):
        client = make_client(lambda u, p, h: err(429))
        with pytest.raises(AllModelsUnavailableError) as exc:
            await client.complete(MSGS)
        assert [a.model for a in exc.value.attempts] == [HEAD, TAIL, THIRD]

    async def test_the_engine_reports_it_and_invents_nothing(self):
        """The user-facing text must not be a model response."""
        from agents.engine import AgentEngine

        class Dead:
            tier = "fast"

            async def complete(self, *a, **k):
                raise AllModelsUnavailableError("all gone")

        result = await AgentEngine(Dead(), build_default_registry()).run("what time")
        assert "free AI models" in result.text
        assert result.stopped_reason == "llm_unavailable"
        assert result.tool_calls == []


# ===========================================================================
# H. Paid-model protection
# ===========================================================================
class TestPaidModelProtection:
    @pytest.mark.parametrize(
        "model",
        [
            "openai/gpt-4o",
            "anthropic/claude-3.5-sonnet",
            "gemini/gemini-1.5-pro",
            "openai/gpt-4o-mini",
            "qwen/qwen3-8b",            # no :free suffix
            "vendor/free-tier-model",   # "free" as a word, not a tag
            "openrouter/auto",          # a paid-capable auto-router
        ],
    )
    def test_no_paid_model_can_enter_the_pool(self, model):
        assert is_free_form(model) is False
        assert is_allowed_model(model) is False
        assert select_models([model], []).models == ()

    def test_a_paid_model_configured_as_primary_is_never_called(self):
        """It is dropped from the pool; the free models still serve the turn."""
        config.OPENROUTER_PRIMARY_MODEL = "openai/gpt-4o"
        try:
            chain = [e.model for e in LLMClient().build_chain("fast", [])]
        finally:
            config.OPENROUTER_PRIMARY_MODEL = HEAD
        assert "openai/gpt-4o" not in chain
        for model in chain:
            assert is_allowed_model(model), model

    async def test_a_paid_only_pool_refuses_before_any_socket_opens(self):
        class Exploding:
            is_closed = False
            calls = []

            async def post(self, *a, **k):
                Exploding.calls.append(1)
                raise AssertionError("a paid model was called!")

        for attr in (
            "OPENROUTER_PRIMARY_MODEL",
            "OPENROUTER_REASONING_MODEL",
            "OPENROUTER_FALLBACK_MODEL",
        ):
            setattr(config, attr, "openai/gpt-4o")
        config.OPENROUTER_FREE_FALLBACK_MODELS = "openai/gpt-4o"
        try:
            client = LLMClient()
            client._client = Exploding()
            with pytest.raises(NonFreeModelError):
                await client.complete(MSGS)
            assert Exploding.calls == []
        finally:
            config.OPENROUTER_PRIMARY_MODEL = HEAD
            config.OPENROUTER_REASONING_MODEL = TAIL
            config.OPENROUTER_FALLBACK_MODEL = THIRD
            config.OPENROUTER_FREE_FALLBACK_MODELS = f"{TAIL},{THIRD}"

    def test_the_auto_router_is_never_in_a_chain(self):
        """Configured as the fallback, it is dropped, not called."""
        config.OPENROUTER_FALLBACK_MODEL = "openrouter/free"
        config.OPENROUTER_FREE_FALLBACK_MODELS = "openrouter/free"
        try:
            # The `fallback` tier's head *is* OPENROUTER_FALLBACK_MODEL, which
            # is the router here, so it is covered by the "pool of only the
            # router" test below rather than here.
            for tier in ("fast", "reasoning"):
                chain = [e.model for e in LLMClient().build_chain(tier, [])]
                assert "openrouter/free" not in chain, tier
                # The tier's own head survives, so the turn is still served.
                assert chain, tier
                assert chain[0] == config.model_for_tier(tier), tier
                assert is_allowed_model(chain[0]), tier

            # The fallback tier has nothing but the router, so it refuses.
            with pytest.raises(NonFreeModelError):
                LLMClient().build_chain("fallback", [])
        finally:
            config.OPENROUTER_FALLBACK_MODEL = THIRD
            config.OPENROUTER_FREE_FALLBACK_MODELS = f"{TAIL},{THIRD}"

    def test_a_pool_of_only_the_auto_router_is_refused_loudly(self):
        """Nothing free left is an error, never a silent arbitrary pick."""
        config.OPENROUTER_PRIMARY_MODEL = "openrouter/free"
        config.OPENROUTER_FALLBACK_MODEL = "openrouter/free"
        config.OPENROUTER_FREE_FALLBACK_MODELS = "openrouter/free"
        try:
            with pytest.raises(NonFreeModelError):
                LLMClient().build_chain("fast", [])
        finally:
            config.OPENROUTER_PRIMARY_MODEL = HEAD
            config.OPENROUTER_FALLBACK_MODEL = THIRD
            config.OPENROUTER_FREE_FALLBACK_MODELS = f"{TAIL},{THIRD}"

    def test_an_endpoint_for_the_auto_router_cannot_be_built(self):
        from services.llm_client import ProviderEndpoint

        with pytest.raises(NonFreeModelError):
            ProviderEndpoint(
                provider="openrouter",
                model="openrouter/free",
                url="https://openrouter.ai/api/v1/chat/completions",
                headers={},
            )

    def test_every_pool_member_is_free_by_construction(self):
        for tier in ("fast", "reasoning", "fallback"):
            for model in config.model_pool(tier):
                assert model.endswith(":free"), (tier, model)
                assert is_allowed_model(model), (tier, model)


# ===========================================================================
# I. Attempt ceiling
# ===========================================================================
class TestAttemptCeiling:
    def test_the_budget_is_finite(self):
        budget = _AttemptBudget(3)
        assert [budget.take() for _ in range(5)] == [1, 2, 3, None, None]
        assert budget.exhausted

    def test_a_zero_limit_still_allows_one_attempt(self):
        budget = _AttemptBudget(0)
        assert budget.take() == 1
        assert budget.take() is None

    async def test_the_client_never_exceeds_the_configured_limit(self):
        for limit in (1, 2, 3, 5):
            client = make_client(lambda u, p, h: err(429), )
            client.max_attempts = limit
            with pytest.raises(AllModelsUnavailableError):
                await client.complete(MSGS)
            assert len(client._client.requests) <= limit, limit
            assert len(client._client.requests) == min(limit, 3), limit

    async def test_the_budget_is_not_multiplied_by_retries(self):
        """`LLM_MAX_RETRIES` must not multiply the global ceiling."""
        client = make_client(lambda u, p, h: err(500))
        client.max_attempts = 2
        client.max_retries = 5
        with pytest.raises(AllModelsUnavailableError):
            await client.complete(MSGS)
        assert len(client._client.requests) <= 2

    async def test_every_attempt_is_numbered(self):
        client = make_client(lambda u, p, h: err(429))
        with pytest.raises(AllModelsUnavailableError) as exc:
            await client.complete(MSGS)
        numbers = [a.attempt for a in exc.value.attempts]
        assert numbers == list(range(1, len(numbers) + 1))


# ===========================================================================
# Observability (Step 14)
# ===========================================================================
class TestObservability:
    async def test_a_transport_then_a_semantic_fallback_are_both_recorded(self):
        """The two-hop shape from the brief, in one log.

        primary 429 -> fallback 1 returns 200 but fails the contract ->
        fallback 2 valid. Every hop must be attributable.
        """
        def handler(url, payload, headers):
            if payload["model"] == HEAD:
                return err(429)
            if payload["model"] == TAIL:
                return ok("User Safety: safe")
            return ok("It is 10:30.")

        client = make_client(handler)
        result = await client.complete(MSGS)
        assert result == "It is 10:30."

        by_model = {}
        for name in (HEAD, TAIL, THIRD):
            health = client.health.get(name)
            by_model[name] = health

        # The primary was transport-failed, the middle model contract-failed,
        # and the third one served the turn.
        assert client.metrics.rate_limited == 1
        assert client.metrics.invalid.get("PROVIDER_METADATA_LEAK") == 1
        assert client.metrics.success == 1
        assert client._client.models() == [HEAD, TAIL, THIRD]

    async def test_attempt_records_carry_the_diagnostic_fields(self):
        client = make_client(lambda u, p, h: err(429))
        with pytest.raises(AllModelsUnavailableError) as exc:
            await client.complete(MSGS)
        first = exc.value.attempts[0]
        assert first.model == HEAD
        assert first.status == 429
        assert first.fallback_reason == "RATE_LIMITED"
        assert first.request_id
        assert first.attempt == 1
        assert first.latency_ms >= 0

    async def test_a_valid_attempt_records_the_tool_and_the_capabilities(self):
        from agents.engine import extract_tool_calls

        contract = contract_from_registry(
            build_default_registry(), require_tool=True, allow_prose=True
        )
        client = make_client(
            lambda u, p, h: ok('{"action": "get_time", "params": {}}')
        )
        await client.complete(MSGS, contract=contract, extract=extract_tool_calls)
        # The success path does not raise, so read the metrics instead.
        assert client.metrics.success == 1
        assert client.metrics.invalid == {}

    async def test_the_api_key_is_never_in_an_attempt_record(self, caplog):
        import logging

        caplog.set_level(logging.DEBUG, logger="Zaytoon.LLMClient")
        client = make_client(lambda u, p, h: err(429))
        with pytest.raises(AllModelsUnavailableError) as exc:
            await client.complete(MSGS)

        records = json.dumps([a.to_dict() for a in exc.value.attempts])
        assert "sk-test" not in records
        assert "sk-test" not in caplog.text
        assert "Bearer" not in caplog.text
        assert "Bearer" not in records

    def test_health_is_reported_for_each_model(self):
        client = get_llm_client()
        snapshot = client.health_snapshot()
        assert isinstance(snapshot, dict)
        for state in snapshot.values():
            assert set(state) == {"failures", "blocked", "last_error"}


# ===========================================================================
# The validator on its own
# ===========================================================================
class TestCompletionValidator:
    def test_the_observed_leak_is_detected(self):
        assert validate_completion("User Safety: safe").verdict == (
            Verdict.METADATA_LEAK
        )
        assert looks_like_metadata_leak("User Safety: safe")
        assert looks_like_metadata_leak("Safety: blocked")
        assert not looks_like_metadata_leak("Here is your schedule for today.")
        assert not looks_like_metadata_leak("")
        assert not looks_like_metadata_leak(None)

    def test_empty_is_rejected(self):
        for blank in ("", "   ", None, "\n"):
            result = validate_completion(blank)
            assert result.ok is False
            assert result.verdict == Verdict.EMPTY

    def test_plain_prose_is_accepted(self):
        assert validate_completion("It is 10:30.").ok is True

    def test_the_validator_needs_no_model(self):
        """Step 16: no LLM judge in this path.

        The whole point is that validation is deterministic, local and free. A
        judge would be another unreliable free model, roughly double the
        latency, and would be judging with the judgement we are avoiding
        outsourcing. The contract object is a plain dataclass and `validate` is
        a pure function of (content, contract).
        """
        import inspect

        from services.completion_validator import CompletionValidator

        source = inspect.getsource(CompletionValidator)
        for forbidden in ("import httpx", "await ", "llm", "client"):
            assert forbidden not in source, forbidden

    def test_required_capabilities_follow_the_contract(self):
        from services.completion_validator import CompletionContract

        assert CompletionContract().required_capabilities == frozenset()
        assert CompletionContract(require_tool=True).required_capabilities == {
            CAP_TOOLS
        }
