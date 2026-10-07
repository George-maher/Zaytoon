"""Secrets: stored encrypted, never returned, never in the file in the clear."""

import json
import os

import pytest

from core import secrets


class TestRoundTrip:
    def test_set_then_get(self, temp_secrets):
        assert secrets.set_secret("prov-1", "sk-abcdef123456") is True
        assert secrets.get_secret("prov-1") == "sk-abcdef123456"

    def test_missing_secret_is_none(self, temp_secrets):
        assert secrets.get_secret("never-set") is None

    def test_has_secret_reports_presence_only(self, temp_secrets):
        assert secrets.has_secret("prov-1") is False
        secrets.set_secret("prov-1", "sk-abcdef123456")
        assert secrets.has_secret("prov-1") is True

    def test_empty_provider_id_is_refused(self, temp_secrets):
        assert secrets.set_secret("", "value") is False

    def test_delete_removes_it(self, temp_secrets):
        secrets.set_secret("prov-1", "sk-abcdef123456")
        assert secrets.delete_secret("prov-1") is True
        assert secrets.get_secret("prov-1") is None

    def test_delete_reports_a_miss(self, temp_secrets):
        assert secrets.delete_secret("absent") is False

    def test_list_returns_ids_only(self, temp_secrets):
        secrets.set_secret("a", "one")
        secrets.set_secret("b", "two")
        assert secrets.list_secret_ids() == ["a", "b"]


class TestAtRest:
    def test_the_value_is_not_in_the_file(self, temp_secrets):
        """The whole point: a stray backup or a committed file must not leak it."""
        secrets.set_secret("openrouter", "sk-supersecretvalue1234567890")

        with open(temp_secrets, "r", encoding="utf-8") as handle:
            raw = handle.read()

        assert "sk-supersecretvalue1234567890" not in raw
        assert "supersecretvalue" not in raw

    def test_the_file_carries_the_format_marker(self, temp_secrets):
        secrets.set_secret("prov-1", "sk-abcdef123456")
        with open(temp_secrets, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        assert data["prov-1"].startswith("zsec1:")

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only")
    def test_the_file_is_owner_only(self, temp_secrets):
        secrets.set_secret("prov-1", "sk-abcdef123456")
        mode = os.stat(temp_secrets).st_mode
        assert mode & 0o077 == 0, "the secrets file is readable by others"


class TestFailureModes:
    def test_a_corrupt_file_reads_as_no_secrets(self, temp_secrets):
        with open(temp_secrets, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        secrets.reset_cache()
        assert secrets.list_secret_ids() == []
        # And it is still writable, rather than permanently poisoned.
        assert secrets.set_secret("prov-1", "sk-abcdef123456") is True
        assert secrets.get_secret("prov-1") == "sk-abcdef123456"

    def test_a_foreign_format_is_refused_not_misread(self, temp_secrets):
        secrets.set_secret("prov-1", "sk-abcdef123456")
        with open(temp_secrets, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        data["prov-1"] = "plaintext-leaked"
        with open(temp_secrets, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        secrets.reset_cache()

        # No marker -> refused, not decoded as if it were ours.
        assert secrets.get_secret("prov-1") is None

    def test_an_unreadable_ciphertext_is_none(self, temp_secrets):
        secrets.set_secret("prov-1", "sk-abcdef123456")
        with open(temp_secrets, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        data["prov-1"] = "zsec1:dpapi:not-valid-base64!!!"
        with open(temp_secrets, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        secrets.reset_cache()
        assert secrets.get_secret("prov-1") is None


class TestRedaction:
    def test_a_stored_secret_is_masked_by_redact(self, temp_secrets):
        """Defence in depth: even if the value leaks into a log, redact catches it."""
        from common.reporting import redact

        key = "sk-abcdefghijklmnop1234"
        secrets.set_secret("prov-1", key)
        fetched = secrets.get_secret("prov-1")
        assert fetched == key
        assert key not in redact(f"using {fetched} for auth")

    def test_the_public_api_has_no_bulk_reader(self, temp_secrets):
        """There is deliberately no `get_all_secrets()`.

        A bulk accessor is an invitation: one caller wants it and then it is in
        a response body. The only reader is `get_secret(provider_id)`.
        """
        assert not hasattr(secrets, "get_all_secrets")
        assert not hasattr(secrets, "get_all")
        assert not hasattr(secrets, "dump")