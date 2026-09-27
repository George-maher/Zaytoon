"""Tests for the OpenRouter LLM client: chain, validation, streaming, metrics.

All HTTP traffic is mocked, so the suite never touches the network.
"""

import asyncio
import json
import time

import httpx
import pytest

from config import (
    PROVIDER_OPENROUTER,
    TIER_FALLBACK,
    TIER_FAST,
    TIER_REASONING,
    config,
)
from services.llm_client import (
    HealthState,
    LLMClient,
    LLMError,
    LLMRequestError,
    LLMResponseError,
    ProviderEndpoint,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def sse_lines(chunks):
    """Build the raw text of an OpenAI-style SSE body."""
    parts = []
    for c in chunks:
        parts.append("data: " + json.dumps({
            "choices": [{"delta": {"content": c}}]
        }))
    parts.append("data: [DONE]")
    return "\n\n".join(parts) + "\n\n"


def completion(content):
    return httpx.Response(
        200, json={"choices": [{"message": {"content": content}}]}
    )


def error_response(status, message="upstream error"):
    return httpx.Response(status, json={"error": {"message": message}})


class MidStreamDrop:
    """Marker: yield these lines, then raise like a dropped SSE connection."""

    status_code = 200

    def __init__(self, text, error=None):
        self.text = text
        self.error = error or httpx.ReadError("peer closed connection")

    def aiter_lines(self):
        drop = self

        async def gen():
            for line in drop.text.splitlines():
                yield line
            raise drop.error

        return gen()


class RecordingClient:
    """Stands in for httpx.AsyncClient and records every request."""

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
        result = self.handler(url, json, headers or {})
        return _FakeStream(result)

    async def aclose(self):
        self.is_closed = True


class _FakeStream:
    def __init__(self, result):
        self._result = result

    async def __aenter__(self):
        if isinstance(self._result, Exception):
            raise self._result
        if isinstance(self._result, httpx.Response):
            if self._result.status_code != 200:
                # Mirror httpx: reading is required before `.text`.
                self._result.request = httpx.Request("POST", "http://x")
            return self._result
        if isinstance(self._result, MidStreamDrop):
            return self._result
        raise AssertionError("non-200 stream must be an httpx.Response")

    async def __aexit__(self, *exc):
        return False

    def aiter_lines(self):
        if isinstance(self._result, MidStreamDrop):
            return self._result.aiter_lines()

        text = self._result

        async def gen():
            for line in text.splitlines():
                yield line

        return gen()

    @property
    def text(self):
        return getattr(self._result, "text", "")


def make_client(handler, **kwargs):
    client = LLMClient(max_retries=1, provider_cooldown=30, **kwargs)
    client._client = RecordingClient(handler)
    return client


# ---------------------------------------------------------------------------
# chain construction
# ---------------------------------------------------------------------------
class TestChainConstruction:
    def test_openrouter_chain_order(self, monkeypatch):
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-test", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_PRIMARY_MODEL", "qwen/qwen3.8-27b:free", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_REASONING_MODEL", "thinkingmachines/inkling-small:free", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_FALLBACK_MODEL", "openrouter/free", raising=False)

        client = LLMClient()
        chain = client.build_chain(TIER_FAST)
        assert [e.model for e in chain] == [
            "qwen/qwen3.8-27b:free",
            "openrouter/free",
        ]
        # Every hop is OpenRouter; local model execution was removed.
        assert all(e.provider == PROVIDER_OPENROUTER for e in chain)
        assert all(e.url == config.LLM_API_URL for e in chain)

    def test_reasoning_tier_uses_reasoning_model(self, monkeypatch):
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-test", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_REASONING_MODEL", "thinkingmachines/inkling-small:free", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_FALLBACK_MODEL", "openrouter/free", raising=False)

        chain = LLMClient().build_chain(TIER_REASONING)
        assert chain[0].model == "thinkingmachines/inkling-small:free"
        assert chain[1].model == "openrouter/free"

    def test_missing_key_raises_a_clear_error(self, monkeypatch):
        """No key and no local fallback means we must fail loudly, not silently."""
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "", raising=False)

        with pytest.raises(LLMError) as exc:
            LLMClient().build_chain(TIER_FAST)
        assert "OPENROUTER_API_KEY" in str(exc.value)

    def test_chain_dedupes_when_fallback_equals_primary(self, monkeypatch):
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-test", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_PRIMARY_MODEL", "openrouter/free", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_FALLBACK_MODEL", "openrouter/free", raising=False)

        chain = LLMClient().build_chain(TIER_FAST)
        assert [e.model for e in chain] == ["openrouter/free"]

    def test_benched_model_is_dropped(self, monkeypatch):
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-test", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_PRIMARY_MODEL", "qwen/qwen3.8-27b:free", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_FALLBACK_MODEL", "openrouter/free", raising=False)

        client = LLMClient(provider_cooldown=999)
        client.health["qwen/qwen3.8-27b:free"] = type(
            "H", (), {"is_open": lambda self: True,
                      "record_success": lambda self: None,
                      "record_failure": lambda self, *a: None}
        )()
        chain = client.build_chain(TIER_FAST)
        assert [e.model for e in chain] == ["openrouter/free"]

    def test_breaker_is_advisory_when_all_models_benched(self, monkeypatch):
        """A full cooldown must not hard-fail; it just gets retried anyway."""
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-test", raising=False)
        client = LLMClient(provider_cooldown=999)
        for model in ("qwen/qwen3.8-27b:free", "openrouter/free"):
            client.health[model] = HealthState(blocked_until=time.monotonic() + 999)
        chain = client.build_chain(TIER_FAST)
        assert chain, "chain must never be empty"


# ---------------------------------------------------------------------------
# headers
# ---------------------------------------------------------------------------
class TestOpenRouterHeaders:
    def test_required_headers_present(self, monkeypatch):
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-or-v1-secret", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_SITE_URL", "https://github.com/zaytoon-ai", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_APP_TITLE", "Zaytoon AI Assistant", raising=False)

        ep = LLMClient()._openrouter_endpoint("qwen/qwen3.8-27b:free")
        assert ep.headers["Authorization"] == "Bearer sk-or-v1-secret"
        assert ep.headers["HTTP-Referer"] == "https://github.com/zaytoon-ai"
        assert ep.headers["X-Title"] == "Zaytoon AI Assistant"
        assert ep.headers["Content-Type"] == "application/json"

    def test_headers_are_sent_on_request(self, monkeypatch):
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-test", raising=False)

        async def run():
            client = make_client(lambda url, payload, headers: completion("hi"))
            await client.complete([{"role": "user", "content": "x"}])
            return client._client.requests[0]["headers"]

        headers = asyncio.run(run())
        assert headers["Authorization"] == "Bearer sk-test"
        assert headers["HTTP-Referer"] == config.OPENROUTER_SITE_URL
        assert headers["X-Title"] == config.OPENROUTER_APP_TITLE

    async def test_headers_present_in_outgoing_call(self, monkeypatch):
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-test", raising=False)

        client = make_client(lambda url, payload, headers: completion("ok"))
        await client.complete([{"role": "user", "content": "hi"}])
        sent = client._client.requests[0]["headers"]
        assert sent["Authorization"] == "Bearer sk-test"
        assert sent["HTTP-Referer"] == config.OPENROUTER_SITE_URL
        assert sent["X-Title"] == config.OPENROUTER_APP_TITLE


# ---------------------------------------------------------------------------
# non-streaming fallback
# ---------------------------------------------------------------------------
class TestCompleteFallback:
    async def test_success_on_first_try(self, monkeypatch):
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-test", raising=False)

        client = make_client(lambda url, payload, headers: completion("first"))
        assert await client.complete([{"role": "user", "content": "x"}]) == "first"
        assert len(client._client.requests) == 1

    async def test_rate_limit_429_falls_through_chain(self, monkeypatch):
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-test", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_PRIMARY_MODEL", "qwen/qwen3.8-27b:free", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_FALLBACK_MODEL", "openrouter/free", raising=False)

        def handler(url, payload, headers):
            if payload["model"] == "qwen/qwen3.8-27b:free":
                return error_response(429, "rate limited")
            return completion("from fallback")

        client = make_client(handler)
        result = await client.complete([{"role": "user", "content": "x"}])
        assert result == "from fallback"
        assert [r["json"]["model"] for r in client._client.requests] == [
            "qwen/qwen3.8-27b:free", "openrouter/free",
        ]

    @pytest.mark.parametrize("status", [500, 502, 503, 504, 529])
    async def test_server_errors_fall_through_chain(self, status, monkeypatch):
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-test", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_PRIMARY_MODEL", "qwen/qwen3.8-27b:free", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_FALLBACK_MODEL", "openrouter/free", raising=False)

        def handler(url, payload, headers):
            if payload["model"] == "qwen/qwen3.8-27b:free":
                return error_response(status)
            return completion(f"recovered-{status}")

        client = make_client(handler)
        assert await client.complete([{"role": "user", "content": "x"}]) == f"recovered-{status}"

    async def test_401_skips_to_next_provider(self, monkeypatch):
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-test", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_PRIMARY_MODEL", "qwen/qwen3.8-27b:free", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_FALLBACK_MODEL", "openrouter/free", raising=False)

        def handler(url, payload, headers):
            if payload["model"] == "qwen/qwen3.8-27b:free":
                return error_response(401, "bad key")
            return completion("fallback answer")

        client = make_client(handler)
        assert await client.complete([{"role": "user", "content": "x"}]) == "fallback answer"
        # 401 is fatal, so the same model is not retried.
        assert [r["json"]["model"] for r in client._client.requests] == [
            "qwen/qwen3.8-27b:free", "openrouter/free",
        ]

    async def test_all_providers_failing_raises(self, monkeypatch):
        client = make_client(lambda url, payload, headers: error_response(503))
        with pytest.raises(LLMError) as exc:
            await client.complete([{"role": "user", "content": "x"}])
        assert exc.value.attempts
        assert all(a.outcome == "server_error" for a in exc.value.attempts)

    async def test_timeout_moves_to_next_provider(self, monkeypatch):
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-test", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_PRIMARY_MODEL", "qwen/qwen3.8-27b:free", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_FALLBACK_MODEL", "openrouter/free", raising=False)

        def handler(url, payload, headers):
            if payload["model"] == "qwen/qwen3.8-27b:free":
                return httpx.ReadTimeout("rate limit timeout")
            return completion("recovered from timeout")

        client = make_client(handler)
        assert await client.complete([{"role": "user", "content": "x"}]) == (
            "recovered from timeout"
        )
        assert client._client.requests[1]["json"]["model"] == "openrouter/free"

    async def test_connect_error_moves_to_next_provider(self, monkeypatch):
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-test", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_PRIMARY_MODEL", "qwen/qwen3.8-27b:free", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_FALLBACK_MODEL", "openrouter/free", raising=False)

        def handler(url, payload, headers):
            if payload["model"] == "qwen/qwen3.8-27b:free":
                return httpx.ConnectError("refused")
            return completion("recovered from connect error")

        client = make_client(handler)
        assert await client.complete([{"role": "user", "content": "x"}]) == (
            "recovered from connect error"
        )


# ---------------------------------------------------------------------------
# circuit breaker
# ---------------------------------------------------------------------------
class TestHealthBreaker:
    async def test_failure_records_health_and_cooldown(self, monkeypatch):
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-test", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_PRIMARY_MODEL", "qwen/qwen3.8-27b:free", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_FALLBACK_MODEL", "openrouter/free", raising=False)

        client = make_client(lambda url, payload, headers: error_response(429))
        with pytest.raises(LLMError):
            await client.complete([{"role": "user", "content": "x"}])
        # Health is keyed per model, not per provider.
        assert client.health["qwen/qwen3.8-27b:free"].failures >= 1
        assert client.health["qwen/qwen3.8-27b:free"].is_open() is True
        assert client.health["openrouter/free"].failures >= 1

    async def test_success_resets_health(self, monkeypatch):
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-test", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_PRIMARY_MODEL", "qwen/qwen3.8-27b:free", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_FALLBACK_MODEL", "openrouter/free", raising=False)

        client = make_client(lambda url, payload, headers: completion("ok"))
        await client.complete([{"role": "user", "content": "x"}])
        assert client.health["qwen/qwen3.8-27b:free"].failures == 0
        assert client.health["qwen/qwen3.8-27b:free"].is_open() is False

    async def test_metrics_track_success_and_latency(self, monkeypatch):
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-test", raising=False)
        client = make_client(lambda url, payload, headers: completion("ok"))
        await client.complete([{"role": "user", "content": "x"}])
        stats = client.metrics.to_dict()
        assert stats["success"] == 1
        assert stats["requests"] == 1
        assert stats["by_model"]["qwen/qwen3.8-27b:free"] == 1

    async def test_metrics_track_rate_limits(self, monkeypatch):
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-test", raising=False)
        client = make_client(lambda url, payload, headers: error_response(429))
        with pytest.raises(LLMError):
            await client.complete([{"role": "user", "content": "x"}])
        assert client.metrics.to_dict()["rate_limited"] >= 2

    def test_health_snapshot_shape(self):
        snapshot = LLMClient().health_snapshot()
        assert isinstance(snapshot, dict)


# ---------------------------------------------------------------------------
# streaming
# ---------------------------------------------------------------------------
class TestStreaming:
    async def test_stream_yields_deltas_in_order(self, monkeypatch):
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-test", raising=False)

        body = sse_lines(["Hel", "lo ", "world"])
        client = make_client(lambda url, payload, headers: httpx.Response(200, text=body))

        deltas = []

        async def on_delta(text):
            deltas.append(text)

        result = await client.stream(
            [{"role": "user", "content": "x"}], on_delta=on_delta
        )
        assert deltas == ["Hel", "lo ", "world"]
        assert result == "Hello world"
        assert client._client.requests[0]["json"]["stream"] is True

    async def test_stream_429_before_first_delta_switches_endpoint(
        self, monkeypatch
    ):
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-test", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_PRIMARY_MODEL", "qwen/qwen3.8-27b:free", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_FALLBACK_MODEL", "openrouter/free", raising=False)

        body = sse_lines(["from ", "fallback"])

        def handler(url, payload, headers):
            if payload["model"] == "qwen/qwen3.8-27b:free":
                return error_response(429, "rate limited")
            return httpx.Response(200, text=body)

        client = make_client(handler)
        deltas = []

        async def on_delta(text):
            deltas.append(text)

        result = await client.stream([{"role": "user", "content": "x"}], on_delta=on_delta)
        # Early-TTS stays functional: the callback fires on the fallback model.
        assert deltas == ["from ", "fallback"]
        assert result == "from fallback"
        assert [r["json"]["model"] for r in client._client.requests] == [
            "qwen/qwen3.8-27b:free", "openrouter/free",
        ]

    async def test_stream_failure_after_first_delta_completes_blocking(
        self, monkeypatch
    ):
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-test", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_PRIMARY_MODEL", "qwen/qwen3.8-27b:free", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_FALLBACK_MODEL", "openrouter/free", raising=False)

        def handler(url, payload, headers):
            if payload.get("stream"):
                if payload["model"] == "qwen/qwen3.8-27b:free":
                    # One delta arrives, then the connection dies before [DONE].
                    first = (
                        "data: " + json.dumps(
                            {"choices": [{"delta": {"content": "partial "}}]}
                        )
                    )
                    return MidStreamDrop(first)
                # The rest of the streaming path is unavailable too.
                return httpx.ConnectError("stream endpoint down")
            return completion("full authoritative answer")

        client = make_client(handler)
        deltas = []

        async def on_delta(text):
            deltas.append(text)

        result = await client.stream([{"role": "user", "content": "x"}], on_delta=on_delta)
        # Partial delta was emitted, then the broken stream is completed by a
        # blocking call so the caller gets the authoritative text exactly once.
        assert deltas == ["partial "]
        assert result == "full authoritative answer"
        # The final request must be non-streaming so tokens aren't duplicated.
        assert client._client.requests[-1]["json"].get("stream") is False

    async def test_stream_degrades_to_partial_when_recovery_also_fails(
        self, monkeypatch
    ):

        def handler(url, payload, headers):
            if payload.get("stream"):
                first = (
                    "data: " + json.dumps(
                        {"choices": [{"delta": {"content": "half an ans"}}]}
                    )
                )
                return MidStreamDrop(first)
            # Every blocking recovery attempt also fails.
            return error_response(503)

        client = make_client(handler)
        deltas = []

        async def on_delta(text):
            deltas.append(text)

        result = await client.stream([{"role": "user", "content": "x"}], on_delta=on_delta)
        # The user already saw this text; returning it beats replacing it with
        # an error string.
        assert deltas == ["half an ans"]
        assert result == "half an ans"

    async def test_stream_all_fail_raises(self, monkeypatch):
        client = make_client(lambda url, payload, headers: error_response(503))
        with pytest.raises(LLMError):
            await client.stream([{"role": "user", "content": "x"}])

    async def test_sse_parser_is_agnostic_to_content(self, monkeypatch):
        client = make_client(
            lambda url, payload, headers: httpx.Response(
                200, text=sse_lines(["from ", "fallback"])
            )
        )
        deltas = []

        async def on_delta(text):
            deltas.append(text)

        result = await client.stream([{"role": "user", "content": "x"}], on_delta=on_delta)
        assert deltas == ["from ", "fallback"]
        assert result == "from fallback"
        assert client._client.requests[0]["url"].startswith("https://openrouter.ai/")

    async def test_malformed_sse_lines_are_skipped(self, monkeypatch):
        raw = (
            ": keep-alive comment\n"
            "data: {not json}\n"
            + sse_lines(["ok"])
        )
        client = make_client(lambda url, payload, headers: httpx.Response(200, text=raw))
        result = await client.stream([{"role": "user", "content": "x"}])
        assert result == "ok"


# ---------------------------------------------------------------------------
# payload shape
# ---------------------------------------------------------------------------
class TestPayload:
    def test_max_tokens_and_temperature_forwarded(self):
        ep = ProviderEndpoint(
            provider=PROVIDER_OPENROUTER,
            model="qwen/qwen3.8-27b:free",
            url="http://x",
            headers={},
        )
        payload = ep.build_payload([{"role": "user", "content": "hi"}], 512, 0.3)
        assert payload["max_tokens"] == 512
        assert payload["temperature"] == 0.3
        assert payload["stream"] is False

    def test_tools_included_when_provided(self):
        ep = ProviderEndpoint(
            provider=PROVIDER_OPENROUTER,
            model="qwen/qwen3.8-27b:free",
            url="http://x",
            headers={},
        )
        tools = [{"type": "function", "function": {"name": "get_time"}}]
        payload = ep.build_payload([], 100, 0.1, tools=tools)
        assert payload["tools"] == tools

    def test_label_defaults_to_provider_and_model(self):
        ep = ProviderEndpoint(
            provider=PROVIDER_OPENROUTER,
            model="qwen/qwen3.8-27b:free",
            url="http://x",
            headers={},
        )
        assert ep.label == "openrouter:qwen/qwen3.8-27b:free"

    async def test_empty_choices_raises_then_falls_back(self, monkeypatch):
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-test", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_PRIMARY_MODEL", "qwen/qwen3.8-27b:free", raising=False)
        monkeypatch.setattr(config, "OPENROUTER_FALLBACK_MODEL", "openrouter/free", raising=False)

        def handler(url, payload, headers):
            if payload["model"] == "qwen/qwen3.8-27b:free":
                return httpx.Response(200, json={"choices": []})
            return completion("recovered")

        client = make_client(handler)
        assert await client.complete([{"role": "user", "content": "x"}]) == "recovered"
