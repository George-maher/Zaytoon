"""Target-scope authorization, and the bypasses it has to refuse.

The requirement is *"enforce scope restrictions in the execution layer, not only
in model instructions"*. Everything here is about the difference between those
two, so the tests are mostly adversarial: they take the ways a host comparison is
usually made and show that this one does not fall for them.

A model asked nicely to stay in scope will usually comply. ``evil-example.com``
ends with ``example.com``, ``example.com.`` is a different string from
``example.com`` to ``==`` and the same host to DNS, ``10.0.0.1`` is inside
``10.0.0.0/8`` by arithmetic and not by prefix, and ``*.example.com`` must not
cover ``example.com`` itself. Each of those is a test below.
"""

from __future__ import annotations

import pytest

from core.security import scope


@pytest.fixture(autouse=True)
async def clean_db(tmp_path, monkeypatch):
    """A private database per test, migrated through the real path."""
    # `CORE_DB_PATH`, not `ZAYTOON_DB_PATH`: the latter is read by nothing in
    # the product, so setting it isolated nothing and only *looked* like it
    # did. That mattered, because `reset_for_tests()` drops every table in
    # whatever `db_path()` resolves to — the no-op spelling would have left
    # these tests pointing at the user's real `Data/core.db` wherever the
    # conftest fixture is not in scope.
    monkeypatch.setenv("CORE_DB_PATH", str(tmp_path / "core.db"))
    from core import db

    await db.reset_for_tests()
    yield
    await db.reset_for_tests()


# ---------------------------------------------------------------------------
# Pattern parsing
# ---------------------------------------------------------------------------
class TestNormalizePattern:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("example.com", "example.com"),
            ("Example.COM", "example.com"),
            ("example.com.", "example.com"),          # trailing root dot
            ("https://example.com/v1/x", "example.com"),
            ("http://user:pw@example.com:8443/", "example.com"),
            ("example.com:8443", "example.com"),      # NOT parsed as a scheme
            ("10.0.0.5", "10.0.0.5"),
            ("10.0.0.0/8", "10.0.0.0/8"),
            ("*.example.com", "*.example.com"),
            ("[::1]", "::1"),
        ],
    )
    def test_accepted_forms(self, raw, expected):
        assert scope.normalize_pattern(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "*", "*.", "*.*", "*.com", "*.co.uk",       # public suffixes
            "foo.*.com", "**", "api.*.example.com",      # mid-host wildcards
            "localhost",                                  # single label
            "10.0.0.0/999", "not a target", "",
        ],
    )
    def test_refused_forms(self, raw):
        with pytest.raises(scope.ScopeError):
            scope.normalize_pattern(raw)

    def test_a_whole_internet_wildcard_is_refused_with_a_reason(self):
        with pytest.raises(scope.ScopeError) as excinfo:
            scope.normalize_pattern("*")
        assert "not an authorization" in str(excinfo.value)


class TestExtractHost:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("https://api.example.com/v1", "api.example.com"),
            ("API.Example.com", "api.example.com"),
            ("example.com:8443", "example.com"),
            ("https://example.com:8443/a/b", "example.com"),
            ("10.0.0.5", "10.0.0.5"),
            ("http://[::1]:8000/x", "::1"),
        ],
    )
    def test_forms(self, raw, expected):
        assert scope.extract_host(raw) == expected

    def test_a_port_is_not_read_as_a_scheme(self):
        """`urlparse` reads `example.com:8443` as scheme `example.com`.

        Going through it without checking for `://` compares the wrong string,
        which for a scope check means an authorization that never matches and
        an assessment that silently never runs.
        """
        assert scope.extract_host("example.com:8443") == "example.com"


# ---------------------------------------------------------------------------
# Matching — the adversarial half
# ---------------------------------------------------------------------------
class TestPatternMatching:
    def test_exact_host(self):
        assert scope.pattern_matches("example.com", "example.com")
        assert scope.pattern_matches("example.com", "EXAMPLE.COM")
        assert scope.pattern_matches("example.com", "example.com.")

    def test_a_suffix_is_not_a_match(self):
        """The classic bypass: `evil-example.com` ends with `example.com`."""
        assert not scope.pattern_matches("example.com", "evil-example.com")
        assert not scope.pattern_matches("example.com", "notexample.com")
        assert not scope.pattern_matches("example.com", "example.com.evil.test")

    def test_a_subdomain_is_not_a_match_without_the_wildcard(self):
        assert not scope.pattern_matches("example.com", "api.example.com")

    def test_subdomain_wildcard_matches_subdomains_only(self):
        assert scope.pattern_matches("*.example.com", "api.example.com")
        assert scope.pattern_matches("*.example.com", "a.b.example.com")

    def test_subdomain_wildcard_does_not_cover_the_apex(self):
        """`*.example.com` ≠ `example.com`.

        Covering the apex too would make the wildcard a silent superset of the
        exact rule, so "I authorised only the API host" would also authorise the
        production site.
        """
        assert not scope.pattern_matches("*.example.com", "example.com")

    def test_subdomain_wildcard_is_label_bounded(self):
        assert not scope.pattern_matches("*.example.com", "notexample.com")
        assert not scope.pattern_matches("*.example.com", "example.com.evil.test")

    def test_cidr_matches_by_address_arithmetic(self):
        assert scope.pattern_matches("10.0.0.0/8", "10.0.0.1")
        assert not scope.pattern_matches("10.0.0.0/8", "11.0.0.1")

    def test_cidr_does_not_match_a_hostname(self):
        """A name might resolve into the range; matching cannot know without DNS."""
        assert not scope.pattern_matches("10.0.0.0/8", "internal.example.com")

    def test_a_single_address_matches_only_itself(self):
        assert scope.pattern_matches("10.0.0.5", "10.0.0.5")
        assert not scope.pattern_matches("10.0.0.5", "10.0.0.6")

    def test_ipv6(self):
        assert scope.pattern_matches("::1", "::1")
        assert scope.pattern_matches("::1/128", "::1")
        assert not scope.pattern_matches("::1", "::2")

    def test_empty_inputs_never_match(self):
        assert not scope.pattern_matches("", "example.com")
        assert not scope.pattern_matches("example.com", "")
        assert not scope.pattern_matches("example.com", None)

    def test_matching_never_raises(self):
        """It is called on the refusal path; a traceback would replace a refusal."""
        for pattern in ("", "*", "not a target", "10.0.0.0/999", None):
            for host in ("", None, "example.com", "10.0.0.1"):
                assert scope.pattern_matches(pattern, host) in (True, False)


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------
class TestRequireAuthorized:
    async def test_a_target_outside_scope_is_refused(self):
        await scope.create(["example.com"], [scope.OP_HTTP_PROBE])
        with pytest.raises(scope.OutOfScope) as excinfo:
            await scope.require_authorized("https://out-of-scope.test/x", scope.OP_HTTP_PROBE)
        assert "out-of-scope.test" in str(excinfo.value)

    async def test_the_refusal_explains_how_to_authorise(self):
        with pytest.raises(scope.OutOfScope) as excinfo:
            await scope.require_authorized("example.com", scope.OP_HTTP_PROBE)
        message = str(excinfo.value)
        assert "/security/authorizations" in message, (
            "a refusal with no next step is how a security control gets "
            "switched off"
        )

    async def test_an_authorised_target_passes_and_returns_the_record(self):
        created = await scope.create(
            ["example.com"], [scope.OP_HTTP_PROBE], note="engagement 12"
        )
        record = await scope.require_authorized("example.com", scope.OP_HTTP_PROBE)
        assert record.id == created.id

    async def test_an_authorised_target_is_still_refused_for_another_operation(self):
        """Scope is a pair: authorising a read probe is not authorising a scan."""
        await scope.create(["example.com"], [scope.OP_HTTP_PROBE])
        with pytest.raises(scope.OutOfScope):
            await scope.require_authorized("example.com", scope.OP_LOCAL_SCAN)

    async def test_revocation_takes_effect_on_the_next_call(self):
        """No cache: revoking must actually stop the next probe."""
        created = await scope.create(["example.com"], [scope.OP_HTTP_PROBE])
        await scope.require_authorized("example.com", scope.OP_HTTP_PROBE)

        assert await scope.revoke(created.id) is True
        with pytest.raises(scope.OutOfScope):
            await scope.require_authorized("example.com", scope.OP_HTTP_PROBE)

    async def test_revoking_twice_reports_the_second_as_a_no_op(self):
        created = await scope.create(["example.com"], [scope.OP_HTTP_PROBE])
        assert await scope.revoke(created.id) is True
        assert await scope.revoke(created.id) is False

    async def test_an_expired_authorization_is_not_live(self):
        created = await scope.create(
            ["example.com"], [scope.OP_HTTP_PROBE], ttl_days=0.00001
        )
        # Backdate it rather than sleeping: the check is on the stored stamp.
        from core import db

        await db.execute(
            "UPDATE security_authorizations SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", created.id),
        )
        with pytest.raises(scope.OutOfScope):
            await scope.require_authorized("example.com", scope.OP_HTTP_PROBE)

    async def test_an_unparseable_expiry_reads_as_expired(self):
        """`I cannot tell if this is still valid` must mean no."""
        created = await scope.create(["example.com"], [scope.OP_HTTP_PROBE])
        from core import db

        await db.execute(
            "UPDATE security_authorizations SET expires_at = ? WHERE id = ?",
            ("not-a-timestamp", created.id),
        )
        with pytest.raises(scope.OutOfScope):
            await scope.require_authorized("example.com", scope.OP_HTTP_PROBE)

    async def test_authorizations_do_not_leak_between_targets(self):
        await scope.create(["lab-one.test"], [scope.OP_HTTP_PROBE])
        with pytest.raises(scope.OutOfScope):
            await scope.require_authorized("lab-two.test", scope.OP_HTTP_PROBE)

    async def test_a_second_authorization_does_not_widen_the_first(self):
        await scope.create(["lab-one.test"], [scope.OP_HTTP_PROBE])
        await scope.create(["lab-two.test"], [scope.OP_HTTP_PROBE])
        with pytest.raises(scope.OutOfScope):
            await scope.require_authorized("lab-three.test", scope.OP_HTTP_PROBE)

    async def test_an_unknown_operation_is_refused(self):
        await scope.create(["example.com"], [scope.OP_HTTP_PROBE])
        with pytest.raises(scope.ScopeError):
            await scope.require_authorized("example.com", "root.everything")

    async def test_private_and_loopback_targets_are_authorisable(self):
        """The opposite of `netguard`, deliberately.

        Assessing a local lab is the intended use of this feature.
        `netguard` is what stops an *unauthorised* fetch; it is not consulted
        here, and that is the design rather than an oversight.
        """
        await scope.create(["127.0.0.1"], [scope.OP_HTTP_PROBE])
        record = await scope.require_authorized(
            "http://127.0.0.1:8080/admin", scope.OP_HTTP_PROBE
        )
        assert record.target == "127.0.0.1"


class TestCreatingAuthorizations:
    async def test_a_string_target_becomes_one_record(self):
        record = await scope.create("example.com", [scope.OP_HTTP_PROBE])
        assert record.target == "example.com"
        assert record.active

    async def test_several_targets_become_several_records(self):
        await scope.create(
            ["a.example.com", "b.example.com"], [scope.OP_HTTP_PROBE]
        )
        active = await scope.list_all()
        assert {record.target for record in active} == {"a.example.com", "b.example.com"}

    async def test_duplicate_targets_collapse(self):
        await scope.create(["a.example.com", "A.EXAMPLE.COM."], [scope.OP_HTTP_PROBE])
        assert len(await scope.list_all()) == 1

    async def test_an_unknown_operation_is_refused_at_creation(self):
        with pytest.raises(scope.ScopeError):
            await scope.create(["example.com"], ["scan.everything"])

    async def test_no_operations_is_refused(self):
        with pytest.raises(scope.ScopeError):
            await scope.create(["example.com"], [])

    async def test_no_targets_is_refused(self):
        with pytest.raises(scope.ScopeError):
            await scope.create([], [scope.OP_HTTP_PROBE])

    async def test_the_ttl_is_capped(self):
        with pytest.raises(scope.ScopeError):
            await scope.create(["example.com"], [scope.OP_HTTP_PROBE], ttl_days=4000)

    async def test_a_negative_ttl_is_refused(self):
        with pytest.raises(scope.ScopeError):
            await scope.create(["example.com"], [scope.OP_HTTP_PROBE], ttl_days=-1)

    async def test_describe_reports_active_and_full_lists(self):
        created = await scope.create(["a.example.com"], [scope.OP_HTTP_PROBE])
        await scope.revoke(created.id)
        view = await scope.describe()
        assert view["active_count"] == 0
        assert len(view["authorizations"]) == 1
        assert view["authorizations"][0]["active"] is False
        assert set(view["operations"]) == set(scope.ALL_OPERATIONS)


class TestNoToolCanGrantScope:
    """The structural half of the requirement."""

    async def test_no_registered_tool_can_create_an_authorization(self):
        """A model must not be able to widen its own scope.

        The check is on the tool registry rather than on the prompt: if a tool
        existed that called `scope.create`, this would find it.
        """
        import inspect

        from tools.registry import build_default_registry

        registry = build_default_registry()
        offenders = []
        for tool in registry.tools():
            source = inspect.getsource(type(tool))
            if "security.scope" in source or "scope.create" in source:
                offenders.append(tool.name())
        assert offenders == [], (
            f"these tools can grant target authorization: {offenders}. Scope "
            f"must only be grantable by the user through the API."
        )

    async def test_the_backend_is_importable_without_the_api_layer(self):
        """Enforcement must not depend on a route being mounted."""
        from core.security import require_authorized

        # The import *is* the assertion; naming it keeps static analysis from
        # reading the line as dead weight and deleting it in a cleanup pass.
        assert callable(require_authorized)
