"""Increment A — one place decides what a failure looks like.

``common/reporting.py`` exists because a raw exception is the most reliable way
to leak a credential: ``str(exc)`` for an HTTP client is exactly where an
``Authorization`` value, an absolute path or a command line shows up, and it
ends up both on a user's screen and in a log file nobody audits.

Three properties are asserted here, each against the real code rather than a
stand-in:

1. **redaction actually removes the secret** — including the two shapes the
   first draft missed (``Bearer <token>`` and ``token: <value>``, where
   masking the keyword alone left the credential beside it);
2. **the internal boundary returns structure, not text** — the registry's
   error path is driven end to end with a tool that explodes mid-call;
3. **the external boundary returns a sentence** — fixed, bilingual, and never
   a traceback, a path or the exception's own words.
"""

import asyncio
import logging

import pytest

from common.concurrency import ReentrantAsyncLock
from common.reporting import client_error, redact, redact_payload, safe_error
from tools.base import BaseTool, ToolResult
from tools.registry import ToolRegistry

SECRET = "sk-or-v1-abcdef1234567890"
TOKEN = "ghp_abcdEFGHijklMNOP123456"


# ---------------------------------------------------------------------------
# redact()
# ---------------------------------------------------------------------------
class TestRedact:
    def test_masks_a_credential_shaped_environment_value(self, monkeypatch):
        monkeypatch.setenv("ZAYTOON_TEST_TOKEN", "supercalifragilistic1")
        out = redact("request failed: supercalifragilistic1 was rejected")
        assert "supercalifragilistic1" not in out
        assert "***" in out

    def test_masks_provider_key_shapes(self):
        out = redact(f"posting with {SECRET} to the router")
        assert SECRET not in out
        assert "***" in out

    def test_masks_a_bearer_header_value(self):
        """The keyword-only pass used to leave the token standing.

        ``Bearer\\S*`` stops at the space, so ``Bearer <jwt>`` came back as
        ``*** <jwt>`` — a redaction that looked right and did nothing.
        """
        out = redact("Authorization: Bearer aaaa.bbbb.cccc-dddd")
        assert "aaaa.bbbb.cccc-dddd" not in out

    def test_masks_a_value_after_a_colon_or_equals(self):
        for text in (
            f"token: {TOKEN}",
            f"token={TOKEN}",
            f"OPENROUTER_API_KEY={SECRET}",
        ):
            out = redact(text)
            assert TOKEN not in out, text
            assert SECRET not in out, text

    def test_masks_a_webhook_secret_buried_in_a_url(self):
        url = "https://hooks.example.com/api/webhooks/1234567890/AbCdEfGhIjKlMnOpQrStUv"
        out = redact(f"webhook failed: {url}")
        assert "AbCdEfGhIjKlMnOpQrStUv" not in out
        # The host survives: an operator needs to know *which* hook failed.
        assert "hooks.example.com" in out

    def test_masks_credentials_embedded_in_a_url_authority(self):
        out = redact("GET https://user:hunter2@example.com/path failed")
        assert "hunter2" not in out
        assert "example.com" in out

    def test_leaves_ordinary_text_untouched(self):
        assert redact("half past three in Cairo") == "half past three in Cairo"

    def test_none_and_empty_come_back_empty(self):
        assert redact(None) == ""
        assert redact("") == ""

    def test_does_not_mangle_an_ordinary_download_url(self):
        out = redact("fetching https://example.com/reports/2026-summary.json")
        assert "2026-summary.json" in out


# ---------------------------------------------------------------------------
# redact_payload() — the structured variant
# ---------------------------------------------------------------------------
class TestRedactPayload:
    def test_masks_values_but_keeps_the_keys(self):
        """A key is a contract the client reads; a value is data."""
        payload = redact_payload({"OPENROUTER_API_KEY": "sk-live-abcdef123456"})
        assert payload == {"OPENROUTER_API_KEY": "***"}

    def test_walks_nesting_without_flattening_it(self):
        payload = redact_payload(
            {
                "task": {"title": "Run with token ghp_abcdefgh1234567890", "id": 7},
                "log": [{"message": "export api_key=sk-test-0123456789"}],
            }
        )
        assert payload["task"]["id"] == 7
        # `_SECRET_ASSIGN` masks keyword + value as one match, so the whole
        # assignment goes — not just the key shape behind it.
        assert payload["log"][0]["message"] == "export ***"

    def test_non_strings_pass_through_untouched(self):
        """Masking a counter would be a bug, not a precaution."""
        assert redact_payload(42) == 42
        assert redact_payload(True) is True
        assert redact_payload(None) is None
        assert redact_payload(["sk-abcdefghij", 1, None]) == ["***", 1, None]

    def test_an_empty_container_stays_empty(self):
        assert redact_payload({}) == {}
        assert redact_payload([]) == []

    def test_sensitive_keys_mask_bare_credential_values(self):
        """A key like ``token`` masks its value even without a ``token:`` prefix.

        ``redact()`` masks text *patterns* (``token: <value>``, ``Bearer …``),
        so a bare credential stored under a sensitive key name passed through
        unmasked — a token in an event payload or audit row would have been
        visible to anyone reading the diagnostics feed.
        """
        payload = redact_payload(
            {
                "token": "ghp_abcdEFGHijklMNOP123456",
                "api_key": "sk-or-v1-abcdef1234567890",
                "nested": {"access_token": "eyJhbGciOiJIUzI1NiJ9"},
                "message": "safe text",
                "count": 42,
            }
        )
        assert payload["token"] == "***"
        assert payload["api_key"] == "***"
        assert payload["nested"]["access_token"] == "***"
        assert payload["message"] == "safe text"
        assert payload["count"] == 42


# ---------------------------------------------------------------------------
# safe_error() — the internal boundary
# ---------------------------------------------------------------------------
class TestSafeError:
    def test_returns_the_operation_and_type_but_not_the_message(self):
        msg = safe_error(
            RuntimeError(f"boom while reading {SECRET}"),
            operation="file_read",
            component="tools.filesystem",
        )
        assert "file_read" in msg
        assert "RuntimeError" in msg
        assert SECRET not in msg
        assert "boom" not in msg

    def test_detail_is_carried_through(self):
        msg = safe_error(
            ValueError("x"), operation="llm.complete", component="engine",
            detail="no free model answered",
        )
        assert "no free model answered" in msg

    def test_logs_exactly_one_structured_line(self, caplog):
        with caplog.at_level(logging.DEBUG):
            safe_error(RuntimeError("kaboom"), operation="op", component="comp")
        records = [r for r in caplog.records if r.name == "Zaytoon.Reporting"]
        assert len(records) == 1
        message = records[0].getMessage()
        assert "op=op" in message
        assert "comp=comp" in message
        assert "type=RuntimeError" in message

    def test_the_logged_detail_is_redacted(self, caplog):
        with caplog.at_level(logging.DEBUG):
            safe_error(
                ValueError(f"failed with token: {TOKEN}"),
                operation="op", component="comp",
            )
        records = [r for r in caplog.records if r.name == "Zaytoon.Reporting"]
        assert TOKEN not in records[0].getMessage()

    def test_a_timeout_is_logged_as_a_warning_not_an_error(self, caplog):
        """Rate limits and timeouts are normal on a free pool, not incidents."""
        with caplog.at_level(logging.DEBUG):
            safe_error(TimeoutError("slow"), operation="llm.complete", component="c")
        records = [r for r in caplog.records if r.name == "Zaytoon.Reporting"]
        assert records
        assert records[0].levelno == logging.WARNING

    def test_an_ordinary_failure_is_logged_at_error(self, caplog):
        with caplog.at_level(logging.DEBUG):
            safe_error(ValueError("bad"), operation="llm.complete", component="c")
        records = [r for r in caplog.records if r.name == "Zaytoon.Reporting"]
        assert records
        assert records[0].levelno == logging.ERROR

    def test_no_traceback_in_the_returned_message(self):
        try:
            raise KeyError("internal_key_name")
        except KeyError as exc:
            msg = safe_error(exc, operation="x", component="y")
        assert "Traceback" not in msg
        assert "internal_key_name" not in msg


# ---------------------------------------------------------------------------
# client_error() — the external boundary
# ---------------------------------------------------------------------------
class TestClientError:
    def test_generic_failure_is_a_fixed_sentence(self):
        msg = client_error(ValueError("kaboom"), operation="turn", component="ws")
        assert msg == "Something went wrong. Please try again."
        assert "kaboom" not in msg
        assert "Traceback" not in msg

    def test_timeout_has_its_own_sentence(self):
        msg = client_error(TimeoutError("slow"), operation="turn", component="ws")
        assert msg == "That timed out. Please try again."

    def test_connection_failure_has_its_own_sentence(self):
        msg = client_error(ConnectionRefusedError("nope"), operation="turn", component="ws")
        assert msg == "The service is unavailable right now. Please try again later."

    def test_arabic_gets_the_arabic_sentence(self):
        msg = client_error(ValueError("x"), operation="turn", component="ws", language="ar")
        assert msg == "حصلت مشكلة في تنفيذ الطلب. جرّب تاني."

    def test_an_unrecognised_language_falls_back_to_english(self):
        msg = client_error(ValueError("x"), operation="t", component="c", language="fr")
        assert msg.endswith("Please try again.")

    def test_none_is_treated_as_generic(self):
        assert client_error(None, operation="t", component="c")

    def test_httpx_style_errors_are_classified_by_name(self):
        """httpx errors are not ``OSError`` subclasses, so the class name counts."""
        class ReadError(Exception):
            pass

        msg = client_error(ReadError("connection reset"), operation="t", component="c")
        assert msg == "The service is unavailable right now. Please try again later."


# ---------------------------------------------------------------------------
# The registry end to end
# ---------------------------------------------------------------------------
class _Exploding(BaseTool):
    """Raises mid-call with a credential in its message — the exact bug."""

    permission = "safe"

    def __init__(self, secret=SECRET, tier="safe"):
        self.secret = secret
        self.permission = tier

    def name(self):
        return "exploding"

    def description(self):
        return "Raises with a secret in the message."

    def schema(self):
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        raise RuntimeError(f"cannot open C:\\secrets\\key with {self.secret}")


class _BadValidator(BaseTool):
    """Raises from ``validate`` instead of from ``execute``."""

    permission = "safe"

    def name(self):
        return "badvalidate"

    def description(self):
        return "Raises during validation."

    def schema(self):
        return {"type": "object", "properties": {}}

    def validate(self, params):
        raise ValueError(f"validator blew up on {SECRET}")

    async def execute(self, **kwargs):
        return ToolResult.ok("never reached")


class TestRegistryDoesNotLeak:
    async def test_a_crashing_tool_reports_structure_not_text(self, caplog):
        registry = ToolRegistry()
        registry.register(_Exploding())

        with caplog.at_level(logging.DEBUG):
            result = await registry.execute("exploding", {})

        assert result["success"] is False
        assert SECRET not in result["error"]
        assert "Traceback" not in result["error"]
        assert "RuntimeError" in result["error"]
        assert result["data"]["exception"] == "RuntimeError"
        # The operator still gets the detail — in the log, redacted.
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "type=RuntimeError" in logged
        assert SECRET not in logged

    async def test_a_crashing_validator_reports_the_same_way(self):
        registry = ToolRegistry()
        registry.register(_BadValidator())

        result = await registry.execute("badvalidate", {})

        assert result["success"] is False
        assert SECRET not in result["error"]
        assert "Traceback" not in result["error"]
        assert result["data"]["exception"] == "ValueError"

    async def test_the_gate_still_runs_before_execution(self):
        """Centralised reporting must not become centralised *bypass*."""
        registry = ToolRegistry()
        registry.register(_Exploding(tier="confirm"))

        denied = await registry.execute("exploding", {}, approval=False)

        # Refused at the gate: never ran, so there is nothing to report.
        assert denied["cancelled"] is True
        assert "data" not in denied


# ---------------------------------------------------------------------------
# The write lock
# ---------------------------------------------------------------------------
class TestReentrantLock:
    async def test_the_same_task_may_enter_twice(self):
        """A plain ``asyncio.Lock`` deadlocks here; this must not."""
        lock = ReentrantAsyncLock()

        async def inner():
            async with lock:
                assert lock.locked

        async def body():
            async with lock:
                await inner()
                # Still held: the inner `__aexit__` released one level only.
                assert lock.locked
            assert not lock.locked

        await asyncio.wait_for(body(), timeout=1.0)

    async def test_a_second_task_blocks_until_the_owner_leaves(self):
        lock = ReentrantAsyncLock()
        order = []

        async def owner():
            async with lock:
                order.append("owner-in")
                await asyncio.sleep(0.05)
                order.append("owner-out")

        async def waiter():
            await asyncio.sleep(0.01)
            async with lock:
                order.append("waiter-in")

        await asyncio.wait_for(
            asyncio.gather(owner(), waiter()), timeout=2.0
        )
        assert order == ["owner-in", "owner-out", "waiter-in"]

    async def test_release_by_a_non_owner_is_refused(self):
        lock = ReentrantAsyncLock()
        await lock.acquire()

        async def intruder():
            lock.release()

        # A separate task, not an inline await — inline would be the *owner*
        # task, and re-entrancy is exactly what we are protecting here.
        task = asyncio.create_task(intruder())
        with pytest.raises(RuntimeError):
            await task

        # The refusal did not corrupt the lock: the owner can still free it.
        lock.release()
        assert not lock.locked
