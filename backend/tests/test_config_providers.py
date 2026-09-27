"""Configuration tests for the OpenRouter-only, free-model-only setup."""

import pytest

from config import (
    ALLOWED_FREE_MODELS,
    FREE_ROUTER_ENDPOINT,
    PROVIDER_OPENROUTER,
    TIER_FALLBACK,
    TIER_FAST,
    TIER_REASONING,
    Config,
    NonFreeModelError,
    config,
    is_allowed_model,
    is_free_model,
)


class TestProviderIsOpenRouterOnly:
    def test_provider_constant(self):
        assert config.PROVIDER == PROVIDER_OPENROUTER

    def test_local_model_config_is_gone(self):
        """Ollama configuration must not exist any more."""
        for attr in (
            "OLLAMA_BASE_URL", "OLLAMA_MODEL", "OLLAMA_FALLBACK_MODEL",
            "PRIMARY_PROVIDER",
        ):
            assert not hasattr(config, attr), f"{attr} still present"
        for prop in (
            "ollama_api_url", "ollama_generate_url", "ollama_headers",
            "ollama_openai_api_url", "ollama_model_for_tier",
        ):
            assert not hasattr(config, prop), f"{prop} still present"

    def test_base_url_is_openrouter_chat_completions(self):
        assert config.LLM_API_URL == (
            "https://openrouter.ai/api/v1/chat/completions"
        )

    def test_headers_carry_openrouter_attribution(self):
        headers = config.LLM_HEADERS
        assert headers["Authorization"].startswith("Bearer ")
        assert headers["HTTP-Referer"]
        assert headers["X-Title"]
        assert headers["Content-Type"] == "application/json"


class TestHardcodedModelSet:
    def test_exactly_three_allowed_models(self):
        assert len(ALLOWED_FREE_MODELS) == 3

    def test_the_expected_models_are_allowed(self):
        assert ALLOWED_FREE_MODELS == frozenset({
            "qwen/qwen3.8-27b:free",
            "thinkingmachines/inkling-small:free",
            "openrouter/free",
        })

    def test_defaults_use_allowed_models(self):
        assert config.OPENROUTER_PRIMARY_MODEL in ALLOWED_FREE_MODELS
        assert config.OPENROUTER_REASONING_MODEL in ALLOWED_FREE_MODELS
        assert config.OPENROUTER_FALLBACK_MODEL in ALLOWED_FREE_MODELS
        assert config.validate_free_models() == []

    @pytest.mark.parametrize("model", sorted(ALLOWED_FREE_MODELS))
    def test_allowed_models_pass_both_checks(self, model):
        assert is_free_model(model) is True
        assert is_allowed_model(model) is True

    def test_fallback_is_the_free_router(self):
        assert config.OPENROUTER_FALLBACK_MODEL == FREE_ROUTER_ENDPOINT


class TestAllowListIsStricterThanFreeSuffix:
    def test_free_but_not_allowlisted_is_refused(self):
        """The allow-list is the primary guard; ':free' alone is not enough."""
        model = "some-vendor/some-model:free"
        assert is_free_model(model) is True
        assert is_allowed_model(model) is False

    @pytest.mark.parametrize(
        "model", ["openai/gpt-4o", "anthropic/claude-3.5-sonnet", "qwen/qwen3-8b"]
    )
    def test_billable_is_refused_by_both(self, model):
        assert is_free_model(model) is False
        assert is_allowed_model(model) is False

    def test_empty_and_non_string_refused(self):
        for value in ("", None, 123, [], "   "):
            assert is_allowed_model(value) is False

    def test_suffix_must_be_exact(self):
        assert is_free_model("vendor/free-tier") is False
        assert is_free_model("vendor/model:free-preview") is False

    def test_validate_free_models_flags_unlisted(self, monkeypatch):
        monkeypatch.setattr(
            config, "OPENROUTER_PRIMARY_MODEL", "rogue/model:free", raising=False
        )
        assert "rogue/model:free" in config.validate_free_models()

    def test_assert_free_model_raises_for_unlisted(self, monkeypatch):
        monkeypatch.setattr(config, "LLM_REQUIRE_FREE_MODELS", True, raising=False)
        with pytest.raises(NonFreeModelError) as exc:
            config.assert_free_model("rogue/model:free")
        assert "allow-listed" in str(exc.value)

    def test_enforcement_opt_out_still_exists_but_is_off_by_default(self, monkeypatch):
        assert config.LLM_REQUIRE_FREE_MODELS is True
        monkeypatch.setattr(config, "LLM_REQUIRE_FREE_MODELS", False, raising=False)
        assert config.assert_free_model("anything/at-all") == "anything/at-all"


class TestTierResolution:
    def test_tiers_map_to_distinct_models(self):
        fast = config.model_for_tier(TIER_FAST)
        reasoning = config.model_for_tier(TIER_REASONING)
        fallback = config.model_for_tier(TIER_FALLBACK)
        assert fast == config.OPENROUTER_PRIMARY_MODEL
        assert reasoning == config.OPENROUTER_REASONING_MODEL
        assert fallback == config.OPENROUTER_FALLBACK_MODEL
        assert reasoning == "thinkingmachines/inkling-small:free"

    def test_unknown_tier_falls_back_to_primary(self):
        assert config.model_for_tier("nonsense") == config.OPENROUTER_PRIMARY_MODEL


class TestDescribeLlm:
    def test_shape(self):
        described = config.describe_llm()
        assert described["provider"] == "openrouter"
        assert described["base_url"] == config.LLM_API_URL
        assert described["allowed_models"] == sorted(ALLOWED_FREE_MODELS)
        assert described["rejected_models"] == []
        assert "ollama" not in described
        assert set(described) >= {"provider", "free_only", "configured", "chain_error"} - {"chain_error"}

    def test_reports_configured_state(self, monkeypatch):
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "", raising=False)
        assert config.llm_configured is False
        assert Config().llm_configured is not None  # type: ignore[misc]
