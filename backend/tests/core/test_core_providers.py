"""The provider registry: CRUD, the free-only policy, discovery and routing."""

import pytest

from core.providers import (
    Capability,
    NoModelAvailable,
    ProviderError,
    assert_model_allowed,
    capabilities_of,
    default_provider,
    delete_model,
    delete_provider,
    ensure_builtins,
    get_provider,
    has_all,
    list_models,
    list_providers,
    mark_verified,
    missing,
    normalise,
    plan,
    provider_health,
    refresh_models,
    requirements_for,
    resolve,
    save_model,
    save_provider,
    validate_provider,
)
from core.providers.capabilities import DEFAULT_CAPABILITIES, describe
from core.providers.client import DiscoveredModel, ProviderTransportError
from core.providers.router import Candidate, NoModelAvailable as RouterNoModel
from core.providers.store import FREE_MODEL_RE


async def make_provider(name="Local", kind="ollama", url="http://localhost:11434", **kwargs):
    return await save_provider(
        provider_id=kwargs.pop("provider_id", None),
        name=name, kind=kind, base_url=url, **kwargs,
    )


class TestCapabilities:
    def test_normalise_defaults_when_nothing_is_declared(self):
        """A model nobody has probed is unprobed, not incapable — assuming less
        would make a freshly installed model unroutable."""
        assert normalise(None) == set(DEFAULT_CAPABILITIES)
        assert normalise([]) == set(DEFAULT_CAPABILITIES)

    def test_normalise_lowercases_and_strips(self):
        assert normalise(["Chat", " TOOLS "]) == {"chat", "tools"}

    def test_normalise_accepts_a_bare_string(self):
        assert normalise("tools") == {"tools"}

    def test_an_unknown_capability_is_kept(self):
        """A newer client may send a capability this build has not heard of."""
        assert "quantum" in normalise(["quantum"])

    def test_has_all_is_a_subset_test(self):
        assert has_all(["chat", "tools"], ["chat"]) is True
        assert has_all(["chat"], ["chat", "tools"]) is False

    def test_missing_names_what_is_absent(self):
        assert missing(["chat"], ["chat", "tools", "vision"]) == ["tools", "vision"]

    def test_every_capability_is_described(self):
        labels = describe()
        assert len(labels) == len(list(Capability))
        assert all(row["label"] for row in labels)


class TestProviderValidation:
    def test_a_valid_provider(self):
        result = validate_provider("Local Ollama", "ollama", "http://localhost:11434/", False)
        assert result["base_url"] == "http://localhost:11434", "the trailing slash is normalised"

    def test_a_name_is_required(self):
        with pytest.raises(ProviderError, match="'name' is required"):
            validate_provider("", "ollama", "http://x", False)

    def test_an_unknown_kind_names_the_valid_ones(self):
        with pytest.raises(ProviderError) as caught:
            validate_provider("X", "magic", "http://x", False)
        assert "ollama" in str(caught.value)

    def test_a_url_is_required(self):
        with pytest.raises(ProviderError, match="'base_url' is required"):
            validate_provider("X", "ollama", "", False)

    def test_the_url_scheme_is_checked(self):
        with pytest.raises(ProviderError, match="http"):
            validate_provider("X", "ollama", "localhost:11434", False)

    def test_credentials_in_the_url_are_refused(self):
        """The URL is stored, logged and echoed back to a client."""
        with pytest.raises(ProviderError, match="must not embed credentials"):
            validate_provider("X", "ollama", "http://user:pass@host", False)

    def test_an_over_long_name_is_refused(self):
        with pytest.raises(ProviderError, match="80 characters"):
            validate_provider("x" * 100, "ollama", "http://x", False)

    def test_a_pasted_trailing_v1_is_canonicalised(self):
        """The client appends /v1 itself; storing a pasted /v1 doubles it.

        Measured: base ``…/api/v1`` + client path ``/v1/models`` joined to
        ``…/api/v1/v1/models`` — a 404 that reads like a broken endpoint
        rather than a pasted URL.
        """
        result = validate_provider(
            "OpenRouter", "openrouter", "https://openrouter.ai/api/v1/", False
        )
        assert result["base_url"] == "https://openrouter.ai/api"

        result = validate_provider(
            "X", "openai_compatible", "https://api.example.com/v1", False
        )
        assert result["base_url"] == "https://api.example.com"

    def test_a_host_named_v1_is_not_stripped(self):
        """``http://v1`` is a host called v1, not a path."""
        result = validate_provider("X", "ollama", "http://v1", False)
        assert result["base_url"] == "http://v1"

    def test_a_real_prefix_is_kept(self):
        result = validate_provider(
            "X", "openai_compatible", "http://gateway.local/ai", False
        )
        assert result["base_url"] == "http://gateway.local/ai"

    def test_a_query_or_fragment_is_refused(self):
        """httpx joins base paths by raw concatenation, so a query on the
        base would be swallowed into the first appended segment."""
        with pytest.raises(ProviderError, match="query string or fragment"):
            validate_provider("X", "ollama", "http://x/v1?token=1", False)
        with pytest.raises(ProviderError, match="query string or fragment"):
            validate_provider("X", "ollama", "http://x/#frag", False)

    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254",              # AWS/OpenStack metadata
            "http://169.254.169.254/latest/meta-data/",
            "http://169.254.171.1",                # Azure's IMDS address
            "https://[fe80::1]",                   # IPv6 link-local
            "https://[::ffff:169.254.169.254]",    # IPv4-mapped link-local
            "http://metadata.google.internal",     # GCP's metadata hostname
        ],
    )
    def test_the_cloud_metadata_plane_is_refused(self, url):
        """A request pointed there returns host credentials in the error body.

        The address class is what is refused, not a spelling — the IPv6 and
        IPv4-mapped forms reach the same service.
        """
        with pytest.raises(ProviderError, match="metadata|link-local"):
            validate_provider("X", "openai_compatible", url, False)

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:11434",   # the primary use case: a local daemon
            "http://127.0.0.1:1234",
            "http://192.168.1.10:8080",  # a NAS running a local model
            "http://10.0.0.5",
            "https://openrouter.ai/api/v1",
        ],
    )
    def test_private_and_public_endpoints_are_allowed(self, url):
        """Local endpoints are the point of this product; only the metadata
        plane is refused."""
        assert validate_provider("X", "openai_compatible", url, False)["base_url"]


class TestFreeOnlyPolicy:
    """The rule is a property of *Zaytoon's own* OpenRouter account, so it
    applies to that provider and to nothing else."""

    @pytest.mark.parametrize("model", [
        "qwen/qwen3.8-27b:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "cohere/north-mini-code:free",
    ])
    def test_allow_listed_free_models_pass(self, model):
        assert assert_model_allowed("openrouter", model) == model

    @pytest.mark.parametrize("model", [
        "openai/gpt-4o",
        "meta-llama/llama-3.1-70b",
        "qwen/qwen3.8-27b",
    ])
    def test_billable_and_untagged_models_are_refused(self, model):
        with pytest.raises(ProviderError, match="free"):
            assert_model_allowed("openrouter", model)

    def test_the_uncontrolled_auto_router_is_refused_by_name(self):
        with pytest.raises(ProviderError, match="auto-router"):
            assert_model_allowed("openrouter", "openrouter/free")

    def test_a_free_but_un_allow_listed_model_is_refused(self):
        with pytest.raises(ProviderError, match="allow-listed"):
            assert_model_allowed("openrouter", "attacker/evil:free")

    def test_a_local_model_is_not_gated(self):
        """The user is pointing at infrastructure they already run."""
        assert assert_model_allowed("ollama", "qwen3.5:9b") == "qwen3.5:9b"

    def test_a_custom_endpoint_model_is_not_gated(self):
        assert assert_model_allowed("openai_compatible", "my-model") == "my-model"

    def test_an_empty_model_is_refused_everywhere(self):
        for kind in ("openrouter", "ollama", "openai_compatible"):
            with pytest.raises(ProviderError):
                assert_model_allowed(kind, "  ")

    def test_the_free_form_pattern_is_narrow(self):
        assert FREE_MODEL_RE.match("vendor/model:free")
        assert not FREE_MODEL_RE.match("model:free")
        assert not FREE_MODEL_RE.match("vendor/model")
        assert not FREE_MODEL_RE.match("vendor/model:free:extra")


class TestProviderCrud:
    async def test_builtins_are_created_once(self, temp_core_db, temp_secrets):
        assert await ensure_builtins() == 1
        assert await ensure_builtins() == 0, "the second call must be a no-op"

    async def test_the_builtin_needs_no_key(self, temp_core_db, temp_secrets):
        await ensure_builtins()
        providers = await list_providers()
        assert providers[0]["kind"] == "ollama"
        assert providers[0]["requires_key"] is False
        assert providers[0]["is_default"] is True

    async def test_create_and_read_back(self, temp_core_db, temp_secrets):
        created = await make_provider()
        assert created["id"]
        found = await get_provider(created["id"])
        assert found["name"] == "Local"
        assert found["base_url"] == "http://localhost:11434"

    async def test_the_id_is_derived_from_the_name(self, temp_core_db, temp_secrets):
        created = await make_provider("My Local LLM")
        assert created["id"] == "my-local-llm"

    async def test_an_explicit_id_is_kept(self, temp_core_db, temp_secrets):
        created = await make_provider(provider_id="custom-id")
        assert created["id"] == "custom-id"

    async def test_saving_the_same_id_updates_in_place(self, temp_core_db, temp_secrets):
        await make_provider(provider_id="p1", name="First")
        await make_provider(provider_id="p1", name="Second")
        assert len(await list_providers()) == 1
        assert (await get_provider("p1"))["name"] == "Second"

    async def test_a_key_bearing_provider_needs_a_key(self, temp_core_db, temp_secrets):
        with pytest.raises(ProviderError, match="needs an API key"):
            await make_provider("Cloud", kind="openrouter",
                                url="https://openrouter.ai/api/v1", requires_key=True)

    async def test_a_key_is_stored_and_never_returned(self, temp_core_db, temp_secrets):
        created = await make_provider(
            "Cloud", kind="openrouter", url="https://openrouter.ai/api/v1",
            requires_key=True, api_key="sk-secret-value-123456",
        )
        fetched = await get_provider(created["id"])
        assert fetched["has_api_key"] is True
        assert "api_key" not in fetched
        assert "sk-secret-value-123456" not in str(fetched)

    async def test_an_omitted_key_does_not_wipe_the_stored_one(self, temp_core_db, temp_secrets):
        created = await make_provider(
            "Cloud", kind="openrouter", url="https://openrouter.ai/api/v1",
            requires_key=True, api_key="sk-secret-value-123456",
        )
        await make_provider(provider_id=created["id"], name="Cloud renamed",
                            kind="openrouter", url="https://openrouter.ai/api/v1",
                            requires_key=True)
        assert (await get_provider(created["id"]))["has_api_key"] is True

    async def test_a_default_flag_is_exclusive(self, temp_core_db, temp_secrets):
        first = await make_provider("First", is_default=True)
        second = await make_provider("Second", is_default=True)

        assert (await get_provider(first["id"]))["is_default"] is False
        assert (await get_provider(second["id"]))["is_default"] is True

    async def test_delete_removes_the_provider_and_its_key(self, temp_core_db, temp_secrets):
        created = await make_provider(
            "Cloud", kind="openrouter", url="https://openrouter.ai/api/v1",
            requires_key=True, api_key="sk-secret-value-123456",
        )
        assert await delete_provider(created["id"]) is True
        assert await get_provider(created["id"]) is None
        assert (await list_providers()) == []

    async def test_deleting_an_unknown_provider_is_false(self, temp_core_db, temp_secrets):
        assert await delete_provider("nope") is False

    async def test_the_default_falls_back_to_any_enabled(self, temp_core_db, temp_secrets):
        await make_provider("Only")
        found = await default_provider()
        assert found is not None and found["name"] == "Only"

    async def test_no_provider_is_none(self, temp_core_db, temp_secrets):
        assert await default_provider() is None

    async def test_timeout_and_org_round_trip(self, temp_core_db, temp_secrets):
        provider = await make_provider(
            "Hosted", kind="openai_compatible", url="https://api.example.com",
            timeout_s=30.5, org_id="org-abc",
        )
        assert provider["timeout_s"] == 30.5
        assert provider["org_id"] == "org-abc"
        # Unverified until a real test passes — never a default "connected".
        assert provider["verified_at"] is None

        reread = await get_provider(provider["id"])
        assert reread["timeout_s"] == 30.5
        assert reread["org_id"] == "org-abc"

    async def test_a_bad_timeout_is_refused(self, temp_core_db, temp_secrets):
        for bad in (0, -5, 700, "abc"):
            with pytest.raises(ProviderError, match="timeout_s"):
                await make_provider("T", url="http://x", timeout_s=bad)

    async def test_an_empty_org_clears_the_value(self, temp_core_db, temp_secrets):
        provider = await make_provider("O", url="http://x", org_id="org-keep")
        cleared = await save_provider(
            provider_id=provider["id"], name="O", kind="ollama",
            base_url="http://x", org_id="  ",
        )
        assert cleared["org_id"] is None

    async def test_mark_verified_records_a_pass_only(self, temp_core_db, temp_secrets):
        provider = await make_provider("V", url="http://x")
        assert provider["verified_at"] is None

        stamp = await mark_verified(provider["id"])
        reread = await get_provider(provider["id"])
        assert reread["verified_at"] == stamp


class TestModels:
    async def test_record_and_list(self, temp_core_db, temp_secrets):
        provider = await make_provider()
        await save_model(provider["id"], "qwen3.5:9b", capabilities=["chat", "coding"])

        models = await list_models(provider_id=provider["id"])
        assert [m["id"] for m in models] == ["qwen3.5:9b"]
        assert models[0]["capabilities"] == ["chat", "coding"]

    async def test_capabilities_default_when_not_declared(self, temp_core_db, temp_secrets):
        provider = await make_provider()
        await save_model(provider["id"], "mystery:7b")
        assert set((await list_models())[0]["capabilities"]) == set(DEFAULT_CAPABILITIES)

    async def test_saving_the_same_model_twice_updates_it(self, temp_core_db, temp_secrets):
        provider = await make_provider()
        await save_model(provider["id"], "qwen3.5:9b", capabilities=["chat"])
        await save_model(provider["id"], "qwen3.5:9b", capabilities=["chat", "tools"])
        assert len(await list_models()) == 1

    async def test_a_model_on_an_unknown_provider_is_refused(self, temp_core_db, temp_secrets):
        with pytest.raises(ProviderError, match="No such provider"):
            await save_model("ghost", "model")

    async def test_an_openrouter_billable_model_cannot_be_recorded(self, temp_core_db, temp_secrets):
        """A billable id must not enter the table even by hand and later be
        picked up by the router."""
        provider = await make_provider(
            "Cloud", kind="openrouter", url="https://openrouter.ai/api/v1",
            requires_key=True, api_key="sk-secret-value-123456",
        )
        with pytest.raises(ProviderError):
            await save_model(provider["id"], "openai/gpt-4o")

    async def test_filtering_by_required_capability(self, temp_core_db, temp_secrets):
        provider = await make_provider()
        await save_model(provider["id"], "coder:7b", capabilities=["chat", "coding"])
        await save_model(provider["id"], "chatty:3b", capabilities=["chat"])

        found = await list_models(provider_id=provider["id"], required=["coding"])
        assert [m["id"] for m in found] == ["coder:7b"]

    async def test_disabled_models_are_hidden_by_default(self, temp_core_db, temp_secrets):
        provider = await make_provider()
        await save_model(provider["id"], "off:1b", enabled=False)
        assert await list_models() == []
        assert len(await list_models(enabled_only=False)) == 1

    async def test_delete(self, temp_core_db, temp_secrets):
        provider = await make_provider()
        await save_model(provider["id"], "gone:1b")
        assert await delete_model(provider["id"], "gone:1b") is True
        assert await delete_model(provider["id"], "gone:1b") is False

    async def test_priority_orders_the_listing(self, temp_core_db, temp_secrets):
        provider = await make_provider()
        await save_model(provider["id"], "second", priority=200)
        await save_model(provider["id"], "first", priority=10)
        assert [m["id"] for m in await list_models()] == ["first", "second"]

    async def test_a_bad_priority_falls_back(self, temp_core_db, temp_secrets):
        provider = await make_provider()
        await save_model(provider["id"], "m", priority="urgent")
        assert (await list_models())[0]["priority"] == 100


class TestRouting:
    async def test_requirements_expand_a_tier(self):
        assert requirements_for("fast") == ["chat"]
        assert "tools" in requirements_for("tools")
        assert "coding" in requirements_for("coding")
        assert "embeddings" in requirements_for("embeddings")

    async def test_extra_capabilities_are_appended(self):
        assert requirements_for("fast", ["vision"])[-1] == "vision"

    async def test_no_provider_reports_clearly(self, temp_core_db, temp_secrets):
        decision = await plan()
        assert decision.model is None
        assert "No AI provider is configured" in decision.error

    async def test_no_models_reports_clearly(self, temp_core_db, temp_secrets):
        await make_provider()
        decision = await plan()
        assert "No models recorded" in decision.error

    async def test_the_capable_model_is_chosen(self, temp_core_db, temp_secrets):
        provider = await make_provider()
        await save_model(provider["id"], "chatty:3b", capabilities=["chat"])
        await save_model(provider["id"], "coder:7b", capabilities=["chat", "coding"], priority=10)

        decision = await plan(required=["coding"])
        assert decision.model == "coder:7b"

    async def test_priority_beats_alphabetical_order(self, temp_core_db, temp_secrets):
        provider = await make_provider()
        await save_model(provider["id"], "zzz", capabilities=["chat"], priority=1)
        await save_model(provider["id"], "aaa", capabilities=["chat"], priority=99)
        assert (await plan(required=["chat"])).model == "zzz"

    async def test_the_plan_explains_each_rejection(self, temp_core_db, temp_secrets):
        """"Why did it pick that one?" has to be answerable."""
        provider = await make_provider()
        await save_model(provider["id"], "chatty:3b", capabilities=["chat"])
        await save_model(provider["id"], "blind:1b", capabilities=["chat", "streaming"])

        decision = await plan(required=["tools"])
        reasons = {c.model_id: c.reason for c in decision.candidates}
        assert "lacks required capabilities: tools" in reasons["chatty:3b"]
        assert all(not c.usable for c in decision.candidates)

    async def test_a_forced_model_bypasses_capability_routing(self, temp_core_db, temp_secrets):
        provider = await make_provider()
        await save_model(provider["id"], "chatty:3b", capabilities=["chat"])

        decision = await plan(required=["tools"], forced_model="chatty:3b")
        assert decision.model == "chatty:3b"
        assert decision.forced is True

    async def test_a_forced_model_still_obeys_policy(self, temp_core_db, temp_secrets):
        """A user override is not a way past the free-only gate."""
        provider = await make_provider(
            "Cloud", kind="openrouter", url="https://openrouter.ai/api/v1",
            requires_key=True, api_key="sk-secret-value-123456",
        )
        decision = await plan(forced_model="openai/gpt-4o")
        assert decision.model is None
        assert "free" in decision.error

    async def test_a_disabled_provider_is_refused(self, temp_core_db, temp_secrets):
        provider = await make_provider()
        await save_model(provider["id"], "m", capabilities=["chat"])
        from core import db
        await db.execute("UPDATE providers SET enabled = 0 WHERE id = ?", (provider["id"],))

        decision = await plan(required=["chat"])
        assert "disabled" in decision.error

    async def test_naming_a_disabled_provider_says_which(self, temp_core_db, temp_secrets):
        provider = await make_provider()
        await save_model(provider["id"], "m", capabilities=["chat"])
        from core import db
        await db.execute("UPDATE providers SET enabled = 0 WHERE id = ?", (provider["id"],))

        decision = await plan(required=["chat"], provider_id=provider["id"])
        assert "Local" in decision.error
        assert "disabled" in decision.error

    async def test_resolve_raises_when_nothing_can_serve(self, temp_core_db, temp_secrets):
        provider = await make_provider()
        await save_model(provider["id"], "chatty:3b", capabilities=["chat"])
        with pytest.raises(NoModelAvailable):
            await resolve(extra_capabilities=["tools"])

    async def test_resolve_returns_a_plan_when_it_can(self, temp_core_db, temp_secrets):
        provider = await make_provider()
        await save_model(provider["id"], "m", capabilities=["chat", "tools"])
        decision = await resolve(extra_capabilities=["tools"])
        assert decision.model == "m"

    async def test_capabilities_of_the_chosen_model(self, temp_core_db, temp_secrets):
        provider = await make_provider()
        await save_model(provider["id"], "m", capabilities=["chat", "coding"])
        decision = await plan(required=["coding"])
        assert "coding" in capabilities_of(decision)

    async def test_capabilities_of_a_forced_model_falls_back(self, temp_core_db, temp_secrets):
        provider = await make_provider()
        await save_model(provider["id"], "m", capabilities=["chat"])
        decision = await plan(forced_model="m")
        assert capabilities_of(decision) == sorted(DEFAULT_CAPABILITIES)

    async def test_a_policy_excluded_model_never_appears_as_a_candidate(
        self, temp_core_db, temp_secrets
    ):
        provider = await make_provider(
            "Cloud", kind="openrouter", url="https://openrouter.ai/api/v1",
            requires_key=True, api_key="sk-secret-value-123456",
        )
        # Recorded with the policy check off, the way a migration might.
        await save_model(provider["id"], "openai/gpt-4o", validate_policy=False)
        await save_model(provider["id"], "qwen/qwen3.8-27b:free", capabilities=["chat"])

        decision = await plan(required=["chat"])
        reasons = {c.model_id: c.reason for c in decision.candidates}
        assert decision.model == "qwen/qwen3.8-27b:free"
        assert "free" in reasons["openai/gpt-4o"]

    async def test_the_plan_serialises_to_plain_data(self, temp_core_db, temp_secrets):
        import json

        provider = await make_provider()
        await save_model(provider["id"], "m", capabilities=["chat"])
        json.dumps((await plan(required=["chat"])).to_dict())

    async def test_the_plan_carries_the_provider_settings(self, temp_core_db, temp_secrets):
        """timeout_s/org_id travel with the decision the router made.

        The transport that answers the request must be configured from the
        *same* row the router chose — a default the two disagree about is how
        one provider gets contacted with another's settings.
        """
        provider = await make_provider(
            "Hosted", kind="openai_compatible", url="https://api.example.com",
            timeout_s=42.0, org_id="org-x",
        )
        await save_model(provider["id"], "m1", capabilities=["chat"])

        decision = await plan(required=["chat"])

        assert decision.provider_id == provider["id"]
        assert decision.timeout_s == 42.0
        assert decision.org_id == "org-x"
        assert decision.to_dict()["timeout_s"] == 42.0


class TestTierAssignments:
    """A pinned tier: persisted, validated at write time, honoured at plan time."""

    async def test_set_get_round_trip(self, temp_core_db, temp_secrets):
        from core.providers import get_tier_assignments, set_tier_assignment

        provider = await make_provider()
        await save_model(provider["id"], "coder:7b", capabilities=["chat", "coding"])

        await set_tier_assignment("coding", provider["id"], "coder:7b")
        assignments = await get_tier_assignments()
        assert assignments["coding"] == {
            "provider_id": provider["id"], "model_id": "coder:7b",
        }

    async def test_clearing_a_pin(self, temp_core_db, temp_secrets):
        from core.providers import get_tier_assignments, set_tier_assignment

        provider = await make_provider()
        await save_model(provider["id"], "m", capabilities=["chat"])
        await set_tier_assignment("fast", provider["id"], "m")

        await set_tier_assignment("fast", None, None)
        assert "fast" not in await get_tier_assignments()

    async def test_an_unknown_tier_is_refused(self, temp_core_db, temp_secrets):
        from core.providers import ProviderError, set_tier_assignment

        with pytest.raises(ProviderError, match="tier"):
            await set_tier_assignment("turbo", None, "m")

    async def test_an_unrecorded_model_is_refused(self, temp_core_db, temp_secrets):
        from core.providers import ProviderError, set_tier_assignment

        provider = await make_provider()
        with pytest.raises(ProviderError, match="not recorded"):
            await set_tier_assignment("fast", provider["id"], "ghost:1b")

    async def test_an_unknown_provider_is_refused(self, temp_core_db, temp_secrets):
        from core.providers import ProviderError, set_tier_assignment

        with pytest.raises(ProviderError, match="No such provider"):
            await set_tier_assignment("fast", "nowhere", "m")

    async def test_a_billable_model_cannot_be_pinned(self, temp_core_db, temp_secrets):
        """The zero-cost policy must hold through the pin as well."""
        from core.providers import ProviderError, set_tier_assignment

        provider = await make_provider(
            "Cloud", kind="openrouter", url="https://openrouter.ai/api/v1",
            requires_key=True, api_key="sk-secret-value-123456",
        )
        with pytest.raises(ProviderError, match="free"):
            await set_tier_assignment("fast", provider["id"], "openai/gpt-4o")

    async def test_unreadable_assignments_read_as_empty(self, temp_core_db, temp_secrets):
        """A corrupt preference steers nowhere; it does not abort the read."""
        from core import db
        from core.providers import get_tier_assignments

        await db.kv_set("models.tiers", "{not json")
        assert await get_tier_assignments() == {}

    async def test_a_pin_beats_priority_order(self, temp_core_db, temp_secrets):
        from core.providers import plan, set_tier_assignment

        provider = await make_provider()
        await save_model(provider["id"], "aaa:7b", capabilities=["chat"], priority=1)
        await save_model(provider["id"], "zzz:3b", capabilities=["chat"], priority=99)
        await set_tier_assignment("fast", provider["id"], "zzz:3b")

        decision = await plan(required=["chat"], tier="fast")
        assert decision.model == "zzz:3b"
        assert decision.source == "tier"

    async def test_an_ineligible_pin_falls_back_with_its_reason_kept(
        self, temp_core_db, temp_secrets
    ):
        from core.providers import plan, set_tier_assignment

        provider = await make_provider()
        await save_model(provider["id"], "coder:7b", capabilities=["chat", "coding"])
        await save_model(provider["id"], "chatty:3b", capabilities=["chat"])
        # Pin cannot serve tools-capable requests.
        await set_tier_assignment("fast", provider["id"], "chatty:3b")

        decision = await plan(required=["coding"], tier="fast")
        assert decision.model == "coder:7b"
        assert decision.source == "priority"
        reasons = {c.model_id: c.reason for c in decision.candidates}
        assert reasons["chatty:3b"].startswith("tier assignment:")

    async def test_a_pin_on_another_provider_is_ignored(
        self, temp_core_db, temp_secrets
    ):
        """Plans are per-provider; a pin from elsewhere must not leak in."""
        from core.providers import plan, set_tier_assignment

        provider_a = await make_provider("Local")
        await save_model(provider_a["id"], "m", capabilities=["chat"])
        provider_b = await make_provider(
            "Other", kind="openai_compatible", url="http://localhost:9999",
        )
        await save_model(provider_b["id"], "other:1b", capabilities=["chat"])
        await set_tier_assignment("fast", provider_b["id"], "other:1b")

        decision = await plan(
            required=["chat"], provider_id=provider_a["id"], tier="fast",
        )
        assert decision.model == "m"
        assert decision.source == "priority"

    async def test_a_forced_model_still_beats_a_pin(self, temp_core_db, temp_secrets):
        from core.providers import plan, set_tier_assignment

        provider = await make_provider()
        await save_model(provider["id"], "pinned:3b", capabilities=["chat"])
        await save_model(provider["id"], "forced:3b", capabilities=["chat"])
        await set_tier_assignment("fast", provider["id"], "pinned:3b")

        decision = await plan(required=["chat"], tier="fast", forced_model="forced:3b")
        assert decision.model == "forced:3b"
        assert decision.source == "forced"

    async def test_default_is_the_catch_all_for_unpinned_tiers(
        self, temp_core_db, temp_secrets
    ):
        from core.providers import plan, set_tier_assignment

        provider = await make_provider()
        await save_model(provider["id"], "default:7b", capabilities=["chat"])
        await set_tier_assignment("default", provider["id"], "default:7b")

        decision = await plan(required=["chat"], tier="fast")
        assert decision.model == "default:7b"
        assert decision.source == "tier"

    async def test_a_disabled_pin_falls_back_to_priority(self, temp_core_db, temp_secrets):
        from core import db
        from core.providers import plan, set_tier_assignment

        provider = await make_provider()
        await save_model(provider["id"], "pinned:3b", capabilities=["chat"])
        await save_model(provider["id"], "backup:3b", capabilities=["chat"])
        await set_tier_assignment("fast", provider["id"], "pinned:3b")
        await db.execute(
            "UPDATE models SET enabled = 0 WHERE model_id = ?", ("pinned:3b",)
        )

        decision = await plan(required=["chat"], tier="fast")
        assert decision.model == "backup:3b"
        assert decision.source == "priority"

    async def test_the_plan_reports_its_source(self, temp_core_db, temp_secrets):
        from core.providers import plan, set_tier_assignment

        provider = await make_provider()
        await save_model(provider["id"], "m", capabilities=["chat"])
        await set_tier_assignment("fast", provider["id"], "m")

        decision = await plan(required=["chat"], tier="fast")
        assert decision.to_dict()["source"] == "tier"


class TestDiscovery:
    async def test_a_refresh_records_what_the_endpoint_reports(
        self, temp_core_db, temp_secrets, monkeypatch
    ):
        provider = await make_provider()

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def list_models(self):
                return [
                    DiscoveredModel(id="qwen3.5:9b", capabilities=["chat", "coding"]),
                    DiscoveredModel(id="nomic-embed", capabilities=["embeddings"]),
                ]

            async def aclose(self):
                return None

        monkeypatch.setattr("core.providers.client.OpenAICompatibleClient", FakeClient)
        result = await refresh_models(provider["id"])

        assert result["discovered"] == 2
        assert {m["id"] for m in await list_models()} == {"qwen3.5:9b", "nomic-embed"}

    async def test_a_discovered_pillaged_model_is_skipped_not_recorded(
        self, temp_core_db, temp_secrets, monkeypatch
    ):
        provider = await make_provider(
            "Cloud", kind="openrouter", url="https://openrouter.ai/api/v1",
            requires_key=True, api_key="sk-secret-value-123456",
        )

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def list_models(self):
                return [
                    DiscoveredModel(id="openai/gpt-4o"),
                    DiscoveredModel(id="qwen/qwen3.8-27b:free"),
                ]

            async def aclose(self):
                return None

        monkeypatch.setattr("core.providers.client.OpenAICompatibleClient", FakeClient)
        await refresh_models(provider["id"])

        assert [m["id"] for m in await list_models()] == ["qwen/qwen3.8-27b:free"]

    async def test_refresh_preserves_the_endpoint_order_as_a_tiebreak(
        self, temp_core_db, temp_secrets, monkeypatch
    ):
        provider = await make_provider()

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def list_models(self):
                return [DiscoveredModel(id="second"), DiscoveredModel(id="first")]

            async def aclose(self):
                return None

        monkeypatch.setattr("core.providers.client.OpenAICompatibleClient", FakeClient)
        await refresh_models(provider["id"])

        assert [m["id"] for m in await list_models()] == ["second", "first"]

    async def test_health_reports_a_failure_without_raising(
        self, temp_core_db, temp_secrets, monkeypatch
    ):
        provider = await make_provider()

        class DeadClient:
            def __init__(self, *args, **kwargs):
                pass

            async def health(self):
                return {"connected": False, "error": "ConnectError", "latency_ms": 3,
                        "models": []}

            async def aclose(self):
                return None

        monkeypatch.setattr("core.providers.client.OpenAICompatibleClient", DeadClient)
        result = await provider_health(provider["id"])

        # A 200 with connected:false, not a 500: the endpoint being down is the
        # answer, and the UI needs the reason text.
        assert result["connected"] is False
        assert result["error"] == "ConnectError"
        assert result["provider"] == "Local"

    async def test_health_of_no_provider(self, temp_core_db, temp_secrets):
        result = await provider_health()
        assert result["connected"] is False
        assert "No provider configured" in result["error"]


class TestTransportClassification:
    def test_a_429_is_retryable(self):
        from core.providers.client import _classify

        error = _classify(429, b"slow down")
        assert error.retryable is True
        assert "rate limited" in str(error)

    def test_a_401_is_fatal(self):
        """A bad key fails identically for every model, so switching models
        only burns the attempt budget."""
        from core.providers.client import _classify

        error = _classify(401, b"unauthorized")
        assert error.retryable is False
        assert "rejected the API key" in str(error)

    def test_a_404_suggests_checking_the_url(self):
        from core.providers.client import _classify

        assert "base URL" in str(_classify(404, b""))

    def test_the_error_body_is_redacted(self):
        """A provider's error body can echo the request, which carries the prompt."""
        from common.reporting import redact  # noqa: F401 - imported for the contract
        from core.providers.client import _classify

        error = _classify(500, b'{"error": "api_key=sk-live-abcdefghijklmnop1234 rejected"}')
        assert "sk-live-abcdefghijklmnop1234" not in str(error)


class TestNoSecretLeakage:
    async def test_nothing_in_the_provider_shape_carries_a_key(self, temp_core_db, temp_secrets):
        provider = await make_provider(
            "Cloud", kind="openrouter", url="https://openrouter.ai/api/v1",
            requires_key=True, api_key="sk-secret-value-123456",
        )
        rendered = str(await get_provider(provider["id"])) + str(await list_providers())
        assert "sk-secret-value-123456" not in rendered

    async def test_the_store_module_exposes_no_bulk_secret_reader(self):
        """`resolve_api_key` takes a provider id; there is no "give me all of them"."""
        from core.providers import store

        assert not hasattr(store, "get_all_secrets")
        assert not hasattr(store, "all_secrets")
        assert not hasattr(store, "dump_secrets")