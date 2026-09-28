"""Request- and response-side validation for the OpenRouter client.

Requirement: a malformed or unexpected upstream payload must degrade to a clean
fallback rather than crashing the agent layer with an ``AttributeError``.
"""

import json

import httpx
import pytest

from config import config
from services.llm_client import (
    LLMClient,
    LLMError,
    LLMRequestError,
    LLMResponseError,
    ProviderEndpoint,
)


GOOD_URL = "https://openrouter.ai/api/v1/chat/completions"


def endpoint(model: str = "nvidia/nemotron-3-super-120b-a12b:free") -> ProviderEndpoint:
    return ProviderEndpoint(
        provider="openrouter",
        model=model,
        url=GOOD_URL,
        headers=dict(config.LLM_HEADERS),
    )


def payload_for(ep: ProviderEndpoint, messages=None) -> dict:
    return ep.build_payload(
        messages or [{"role": "user", "content": "hi"}], 512, 0.5
    )


class Recorder:
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

    async def aclose(self):
        self.is_closed = True


def client_with(handler) -> LLMClient:
    c = LLMClient(max_retries=1)
    c._client = Recorder(handler)
    return c


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------
class TestRequestValidation:
    def test_valid_request_passes(self):
        ep = endpoint()
        msgs = [{"role": "user", "content": "hi"}]
        ep.validate_request(payload_for(ep, msgs), msgs)

    @pytest.mark.parametrize("header", ["Authorization", "HTTP-Referer", "X-Title"])
    def test_missing_required_header_rejected(self, header):
        ep = endpoint()
        headers = dict(ep.headers)
        headers.pop(header)
        bad = ProviderEndpoint(
            provider="openrouter", model="nvidia/nemotron-3-super-120b-a12b:free",
            url=GOOD_URL, headers=headers,
        )
        with pytest.raises(LLMRequestError) as exc:
            bad.validate_request(payload_for(ep), [{"role": "user", "content": "x"}])
        assert header in str(exc.value)

    def test_authorization_must_be_bearer(self):
        headers = dict(config.LLM_HEADERS)
        headers["Authorization"] = "token abc"
        bad = ProviderEndpoint(
            provider="openrouter", model="nvidia/nemotron-3-super-120b-a12b:free",
            url=GOOD_URL, headers=headers,
        )
        with pytest.raises(LLMRequestError):
            bad.validate_request(payload_for(bad), [{"role": "user", "content": "x"}])

    def test_non_openrouter_host_rejected(self):
        ep = ProviderEndpoint(
            provider="openrouter", model="nvidia/nemotron-3-super-120b-a12b:free",
            url="https://evil.example.com/chat", headers=dict(config.LLM_HEADERS),
        )
        with pytest.raises(LLMRequestError):
            ep.validate_request(payload_for(ep), [{"role": "user", "content": "x"}])

    @pytest.mark.parametrize("messages", [[], None, {}, "hi"])
    def test_messages_must_be_a_non_empty_list(self, messages):
        ep = endpoint()
        with pytest.raises(LLMRequestError):
            ep.validate_request(payload_for(ep), messages)

    def test_invalid_role_rejected(self):
        ep = endpoint()
        with pytest.raises(LLMRequestError):
            ep.validate_request(
                payload_for(ep), [{"role": "wizard", "content": "x"}]
            )

    def test_missing_content_rejected(self):
        ep = endpoint()
        with pytest.raises(LLMRequestError):
            ep.validate_request(payload_for(ep), [{"role": "user"}])

    @pytest.mark.parametrize("mt", [0, -1, None, "512", 12.5])
    def test_bad_max_tokens_rejected(self, mt):
        ep = endpoint()
        payload = payload_for(ep)
        payload["max_tokens"] = mt
        with pytest.raises(LLMRequestError):
            ep.validate_request(payload, [{"role": "user", "content": "x"}])

    @pytest.mark.parametrize("temp", [-0.1, 2.5, "hot", None])
    def test_temperature_out_of_range_rejected(self, temp):
        ep = endpoint()
        payload = payload_for(ep)
        payload["temperature"] = temp
        with pytest.raises(LLMRequestError):
            ep.validate_request(payload, [{"role": "user", "content": "x"}])

    async def test_bad_request_never_reaches_the_socket(self, monkeypatch):
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-test", raising=False)

        def explode(*a, **k):
            raise AssertionError("HTTP must not be attempted after a local failure")

        c = client_with(explode)
        with pytest.raises(LLMRequestError):
            await c.complete([{"role": "wizard", "content": "x"}])
        assert c._client.requests == []


# ---------------------------------------------------------------------------
# Response validation
# ---------------------------------------------------------------------------
class TestResponseValidation:
    def test_happy_path(self):
        ep = endpoint()
        r = httpx.Response(
            200, json={"choices": [{"message": {"content": "hello"}}]},
            headers={"content-type": "application/json"},
        )
        assert ep.validate_completion(r) == "hello"

    def test_non_json_content_type_rejected(self):
        ep = endpoint()
        r = httpx.Response(
            200, text="<html>gateway</html>",
            headers={"content-type": "text/html"},
        )
        with pytest.raises(LLMResponseError) as exc:
            ep.validate_completion(r)
        assert "content-type" in str(exc.value)

    def test_invalid_json_rejected(self):
        ep = endpoint()
        r = httpx.Response(
            200, text="{not json", headers={"content-type": "application/json"}
        )
        with pytest.raises(LLMResponseError):
            ep.validate_completion(r)

    def test_json_array_rejected(self):
        ep = endpoint()
        r = httpx.Response(200, json=[1, 2, 3])
        with pytest.raises(LLMResponseError) as exc:
            ep.validate_completion(r)
        assert "JSON object" in str(exc.value)

    def test_error_payload_with_200_surfaces_message(self):
        ep = endpoint()
        r = httpx.Response(200, json={"error": {"message": "model overloaded"}})
        with pytest.raises(LLMResponseError) as exc:
            ep.validate_completion(r)
        assert "model overloaded" in str(exc.value)

    def test_missing_choices_rejected(self):
        ep = endpoint()
        with pytest.raises(LLMResponseError):
            ep.validate_completion(httpx.Response(200, json={"id": "abc"}))

    def test_empty_choices_rejected(self):
        ep = endpoint()
        with pytest.raises(LLMResponseError) as exc:
            ep.validate_completion(httpx.Response(200, json={"choices": []}))
        assert "empty" in str(exc.value)

    def test_choices_not_a_list_rejected(self):
        ep = endpoint()
        with pytest.raises(LLMResponseError):
            ep.validate_completion(httpx.Response(200, json={"choices": "nope"}))

    def test_choice_entry_not_an_object_rejected(self):
        ep = endpoint()
        with pytest.raises(LLMResponseError):
            ep.validate_completion(httpx.Response(200, json={"choices": ["x"]}))

    def test_missing_message_rejected(self):
        ep = endpoint()
        with pytest.raises(LLMResponseError) as exc:
            ep.validate_completion(httpx.Response(200, json={"choices": [{}]}))
        assert "message" in str(exc.value)

    def test_legacy_text_shape_accepted(self):
        ep = endpoint()
        r = httpx.Response(200, json={"choices": [{"text": "legacy"}]})
        assert ep.validate_completion(r) == "legacy"

    def test_null_content_is_empty_string(self):
        """Tool-call responses carry null content; that is not an error."""
        ep = endpoint()
        r = httpx.Response(200, json={"choices": [{"message": {"content": None}}]})
        assert ep.validate_completion(r) == ""

    def test_multimodal_content_blocks_concatenated(self):
        ep = endpoint()
        r = httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "text", "text": "part one "},
                                {"type": "image_url", "image_url": {"url": "x"}},
                                {"type": "text", "text": "part two"},
                            ]
                        }
                    }
                ]
            },
        )
        assert ep.validate_completion(r) == "part one part two"

    def test_unexpected_content_type_rejected(self):
        ep = endpoint()
        r = httpx.Response(200, json={"choices": [{"message": {"content": 42}}]})
        with pytest.raises(LLMResponseError):
            ep.validate_completion(r)


# ---------------------------------------------------------------------------
# Malformed responses must fall back, not crash
# ---------------------------------------------------------------------------
class TestMalformedFallsBack:
    @pytest.mark.parametrize(
        "body",
        [
            {"choices": []},
            {"id": "no-choices-here"},
            {"choices": "garbage"},
            {"choices": [{}]},
            {"error": {"message": "upstream said no"}},
            [1, 2, 3],
        ],
    )
    async def test_malformed_primary_falls_back(self, body, monkeypatch):
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-test", raising=False)
        monkeypatch.setattr(
            config, "OPENROUTER_PRIMARY_MODEL", "qwen/qwen3.8-27b:free", raising=False
        )
        monkeypatch.setattr(config, "OPENROUTER_FALLBACK_MODEL", "nvidia/nemotron-3-super-120b-a12b:free", raising=False)

        def handler(url, payload, headers):
            if payload["model"] == "qwen/qwen3.8-27b:free":
                return httpx.Response(200, json=body)
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "recovered"}}]}
            )

        c = client_with(handler)
        assert await c.complete([{"role": "user", "content": "x"}]) == "recovered"
        assert c._client.requests[-1]["json"]["model"] == "nvidia/nemotron-3-super-120b-a12b:free"
        assert c.metrics.to_dict()["malformed_response"] >= 1

    async def test_all_malformed_raises_clean_llm_error(self, monkeypatch):
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-test", raising=False)
        c = client_with(lambda u, p, h: httpx.Response(200, json={"choices": []}))
        with pytest.raises(LLMError):
            await c.complete([{"role": "user", "content": "x"}])

    async def test_html_error_page_is_not_parsed_as_json(self, monkeypatch):
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-test", raising=False)
        c = client_with(
            lambda u, p, h: httpx.Response(
                200, text="<html>502 Bad Gateway</html>",
                headers={"content-type": "text/html"},
            )
        )
        with pytest.raises(LLMError):
            await c.complete([{"role": "user", "content": "x"}])


# ---------------------------------------------------------------------------
# SSE frame parsing
# ---------------------------------------------------------------------------
class TestSSEParsing:
    def test_extracts_content(self):
        line = "data: " + json.dumps(
            {"choices": [{"delta": {"content": "hi"}}]}
        )
        assert LLMClient._parse_sse_line(line[5:].strip()) == "hi"

    @pytest.mark.parametrize(
        "raw",
        [
            "{not json",
            json.dumps({"choices": []}),
            json.dumps({"choices": [{}]}),
            json.dumps({"choices": [{"delta": {}}]}),
            json.dumps({"choices": [{"delta": {"content": 5}}]}),
            json.dumps({"error": {"message": "boom"}}),
            json.dumps([1, 2, 3]),
            "",
        ],
    )
    def test_ignores_unusable_frames(self, raw):
        assert LLMClient._parse_sse_line(raw) is None
