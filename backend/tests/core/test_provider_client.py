"""OpenAICompatibleClient: native tool-call normalisation and stream semantics.

The defect these tests pin: a hosted model that answers a tool request with a
**native** OpenAI ``tool_calls`` entry (and no prose) used to come back as an
empty string — the transport read only ``message.content`` — which burned an
attempt and eventually failed the turn. The fix renders native calls into the
engine's documented JSON-in-text dialect, so ``extract_tool_calls`` treats a
native call and a model that types the JSON itself as the same thing.

HTTP is mocked through the ``transport`` seam (``httpx.MockTransport``), so
adapter behaviour is proven deterministically with no network.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from agents.engine import extract_tool_calls
from core.providers.client import (
    STREAM_IDLE_TIMEOUT,
    OpenAICompatibleClient,
    ProviderTransportError,
)

pytestmark = pytest.mark.asyncio

MESSAGES = [{"role": "user", "content": "hi"}]


def make_client(handler) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        "http://provider.test", api_key="test-key",
        transport=httpx.MockTransport(handler),
    )


def chat_response(message: dict) -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": message}]}
    )


def sse(*frames: dict) -> httpx.Response:
    body = "".join(f"data: {json.dumps(f)}\n\n" for f in frames)
    body += "data: [DONE]\n\n"
    return httpx.Response(
        200,
        content=body.encode("utf-8"),
        headers={"content-type": "text/event-stream"},
    )


# ---------------------------------------------------------------------------
# Non-streaming: native tool_calls -> JSON-in-text dialect
# ---------------------------------------------------------------------------
async def test_native_tool_call_with_object_arguments_is_rendered():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        return chat_response({
            "content": "",
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "create_folder", "arguments": {"path": "demo"}},
            }],
        })

    client = make_client(handler)
    text = await client.complete(MESSAGES, model="m", tools=[{"type": "function"}])

    # The request carried the tools spec — the endpoint was *asked* to call.
    assert seen["payload"]["tools"]
    # What came back parses as the dialect the engine accepts, with real params.
    calls, prose = extract_tool_calls(text)
    assert [c.name for c in calls] == ["create_folder"]
    assert calls[0].params == {"path": "demo"}
    assert prose.strip() == ""


async def test_native_tool_call_with_stringified_arguments_is_parsed():
    # Per the OpenAI spec, function.arguments is a *stringified* JSON object.
    def handler(request: httpx.Request) -> httpx.Response:
        return chat_response({
            "content": "",
            "tool_calls": [{
                "function": {"name": "get_time", "arguments": '{"tz": "UTC"}'},
            }],
        })

    client = make_client(handler)
    text = await client.complete(MESSAGES, model="m", tools=[{"type": "function"}])

    calls, _ = extract_tool_calls(text)
    assert calls[0].name == "get_time"
    assert calls[0].params == {"tz": "UTC"}


async def test_prose_and_native_call_both_survive():
    def handler(request: httpx.Request) -> httpx.Response:
        return chat_response({
            "content": "Creating that now.",
            "tool_calls": [{
                "function": {"name": "create_folder", "arguments": "{}"},
            }],
        })

    client = make_client(handler)
    text = await client.complete(MESSAGES, model="m", tools=[{"type": "function"}])

    calls, prose = extract_tool_calls(text)
    assert prose == "Creating that now."
    assert [c.name for c in calls] == ["create_folder"]
    assert calls[0].params == {}


async def test_unparseable_arguments_are_kept_as_raw_not_dropped():
    def handler(request: httpx.Request) -> httpx.Response:
        return chat_response({
            "content": "",
            "tool_calls": [{
                "function": {"name": "file_write", "arguments": "{oops"},
            }],
        })

    client = make_client(handler)
    text = await client.complete(MESSAGES, model="m", tools=[{"type": "function"}])

    calls, _ = extract_tool_calls(text)
    # The call is not lost: it surfaces with the raw body so the contract can
    # report bad arguments instead of the turn silently seeing no call at all.
    assert calls[0].name == "file_write"
    assert calls[0].params == {"_raw": "{oops"}


async def test_plain_prose_is_untouched():
    def handler(request: httpx.Request) -> httpx.Response:
        return chat_response({"content": "Just an answer."})

    client = make_client(handler)
    text = await client.complete(MESSAGES, model="m")
    assert text == "Just an answer."
    assert extract_tool_calls(text) == ([], "Just an answer.")


# ---------------------------------------------------------------------------
# Streaming: text on the wire, tool calls off the wire
# ---------------------------------------------------------------------------
async def test_stream_yields_text_deltas_only():
    def handler(request: httpx.Request) -> httpx.Response:
        return sse(
            {"choices": [{"delta": {"content": "Hel"}}]},
            {"choices": [{"delta": {"content": "lo"}}]},
        )

    client = make_client(handler)
    deltas = []
    sink = []
    async for delta in client.stream(MESSAGES, model="m", tool_calls_out=sink):
        deltas.append(delta)

    assert deltas == ["Hel", "lo"]
    assert sink == []


async def test_stream_tool_call_accumulates_off_wire():
    """A streamed native call never reaches the deltas — only the sink."""
    def handler(request: httpx.Request) -> httpx.Response:
        # Name and arguments fragmented across frames, OpenAI-style.
        return sse(
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "c1",
                 "function": {"name": "get_time", "arguments": ""}},
            ]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": '{"tz":'}},
            ]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": ' "UTC"}'}},
            ]}}]},
        )

    client = make_client(handler)
    deltas = []
    sink = []
    async for delta in client.stream(
        MESSAGES, model="m", tools=[{"type": "function"}], tool_calls_out=sink,
    ):
        deltas.append(delta)

    # Nothing rendered-as-prose went out...
    assert deltas == []
    # ...but the call arrived complete and parses as the dialect.
    assert len(sink) == 1
    calls, prose = extract_tool_calls(sink[0])
    assert calls[0].name == "get_time"
    assert calls[0].params == {"tz": "UTC"}
    assert prose == ""


async def test_stream_with_prose_and_call_splits_correctly():
    def handler(request: httpx.Request) -> httpx.Response:
        return sse(
            {"choices": [{"delta": {"content": "On it."}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"name": "get_time", "arguments": "{}"}},
            ]}}]},
        )

    client = make_client(handler)
    deltas = []
    sink = []
    async for delta in client.stream(MESSAGES, model="m", tool_calls_out=sink):
        deltas.append(delta)

    assert deltas == ["On it."]
    assert len(sink) == 1
    calls, _ = extract_tool_calls(sink[0])
    assert calls[0].name == "get_time"


async def test_stream_without_sink_yields_the_rendered_call():
    """Losing the call would turn a tool response into an empty completion."""
    def handler(request: httpx.Request) -> httpx.Response:
        return sse(
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"name": "get_time", "arguments": "{}"}},
            ]}}]},
        )

    client = make_client(handler)
    deltas = []
    async for delta in client.stream(MESSAGES, model="m", tools=[{"type": "function"}]):
        deltas.append(delta)

    assert deltas and all("\n" in d or d.startswith('{"') for d in deltas)
    calls, _ = extract_tool_calls("".join(deltas))
    assert calls[0].name == "get_time"


# ---------------------------------------------------------------------------
# URL joining: what actually goes on the wire
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "base,expected",
    [
        # Origin forms — the client appends /v1 itself.
        ("http://localhost:11434", "http://localhost:11434/v1/chat/completions"),
        ("http://localhost:11434/", "http://localhost:11434/v1/chat/completions"),
        # Pasted exactly as a provider documents it: /v1 is stripped once,
        # not doubled. This is the measured defect (…/api/v1/v1/models → 404).
        ("https://openrouter.ai/api/v1",
         "https://openrouter.ai/api/v1/chat/completions"),
        ("https://api.example.com/v1",
         "https://api.example.com/v1/chat/completions"),
        ("https://api.example.com/v1/",
         "https://api.example.com/v1/chat/completions"),
        # A real prefix that is not a version suffix stays intact.
        ("https://host.example/prefix",
         "https://host.example/prefix/v1/chat/completions"),
        # A host *called* v1 is not a path.
        ("http://v1", "http://v1/v1/chat/completions"),
    ],
)
async def test_url_join_matrix(base, expected):
    """The joined request URL is decided by canonicalisation, not by luck."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return chat_response({"content": "ok"})

    client = OpenAICompatibleClient(base, transport=httpx.MockTransport(handler))
    text = await client.complete(MESSAGES, model="m")

    assert text == "ok"
    assert seen["url"] == expected


async def test_classify_404_explains_the_v1_convention():
    """A 404 must say how the base URL is joined, not just that it failed."""
    from core.providers.client import _classify

    error = _classify(404, b"{}")
    assert "/v1" in str(error)
    assert "base URL" in str(error)


# ---------------------------------------------------------------------------
# Organization header (§2A: identifier "when applicable")
# ---------------------------------------------------------------------------
async def test_the_org_header_is_sent_when_configured():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["org"] = request.headers.get("openai-organization")
        return chat_response({"content": "ok"})

    client = OpenAICompatibleClient(
        "https://api.example.com", org_id="org-42",
        transport=httpx.MockTransport(handler),
    )
    await client.complete(MESSAGES, model="m")

    assert seen["org"] == "org-42"


async def test_no_org_header_by_default():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["org"] = request.headers.get("openai-organization")
        return chat_response({"content": "ok"})

    client = OpenAICompatibleClient(
        "https://api.example.com", transport=httpx.MockTransport(handler),
    )
    await client.complete(MESSAGES, model="m")

    assert seen["org"] is None


# ---------------------------------------------------------------------------
# Failure taxonomy (W3): one vocabulary, produced at the failure site
# ---------------------------------------------------------------------------
class TestFailureKinds:
    @pytest.mark.parametrize(
        "status,kind,retryable",
        [
            (429, "rate_limited", True),
            (500, "server_error", True),
            (503, "server_error", True),
            (529, "server_error", True),
            (401, "fatal", False),
            (404, "fatal", False),
            (400, "fatal", False),
            (422, "fatal", False),
        ],
    )
    async def test_http_status_is_classified_into_the_attempt_vocabulary(
        self, status, kind, retryable
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json={"error": "boom"})

        client = make_client(handler)
        with pytest.raises(ProviderTransportError) as caught:
            await client.complete(MESSAGES, model="m")

        assert caught.value.kind == kind
        assert caught.value.retryable is retryable
        assert caught.value.status == status

    async def test_a_connect_error_is_not_a_timeout(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client = make_client(handler)
        with pytest.raises(ProviderTransportError) as caught:
            await client.complete(MESSAGES, model="m")

        assert caught.value.kind == "connect_error"
        assert caught.value.retryable is True

    async def test_a_read_timeout_is_a_timeout(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow")

        client = make_client(handler)
        with pytest.raises(ProviderTransportError) as caught:
            await client.complete(MESSAGES, model="m")

        assert caught.value.kind == "timeout"

    async def test_a_non_json_body_is_malformed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, text="<html>login page</html>",
                headers={"content-type": "text/html"},
            )

        client = make_client(handler)
        with pytest.raises(ProviderTransportError) as caught:
            await client.complete(MESSAGES, model="m")

        assert caught.value.kind == "malformed"
        assert caught.value.retryable is False

    async def test_an_empty_choices_list_is_malformed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": []})

        client = make_client(handler)
        with pytest.raises(ProviderTransportError) as caught:
            await client.complete(MESSAGES, model="m")

        assert caught.value.kind == "malformed"


# ---------------------------------------------------------------------------
# The stall guard: silence has a deadline, duration does not
# ---------------------------------------------------------------------------
class _StallingStream(httpx.AsyncByteStream):
    """A stream that opens and then says nothing — the failure mode that hung."""

    def __init__(self, delay: float) -> None:
        self._delay = delay

    async def __aiter__(self):
        await asyncio.sleep(self._delay)
        yield b'data: {"choices": [{"delta": {"content": "late"}}]}\n\n'

    async def aclose(self) -> None:
        return None


async def test_a_silent_stream_is_reported_as_stalled_not_hung():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, stream=_StallingStream(30.0),
            headers={"content-type": "text/event-stream"},
        )

    client = OpenAICompatibleClient(
        "http://provider.test", timeout=0.3,
        transport=httpx.MockTransport(handler),
    )

    started = time.monotonic()
    with pytest.raises(ProviderTransportError) as caught:
        async for _ in client.stream(MESSAGES, model="m"):
            pass
    elapsed = time.monotonic() - started

    assert caught.value.kind == "timeout"
    assert caught.value.retryable is True
    assert "stalled" in str(caught.value)
    # Fails in a fraction of a second rather than holding the turn for the
    # 30 s the stream would have taken to say anything.
    assert elapsed < 5.0


async def test_the_read_bound_is_the_tighter_of_provider_timeout_and_the_cap():
    """STREAM_IDLE_TIMEOUT was exported and never enforced; now it bounds reads."""
    captured: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.extensions.get("timeout", {}))
        return sse({"choices": [{"delta": {"content": "x"}}]})

    # Default provider timeout (120 s) -> the 90 s stall cap applies.
    client = OpenAICompatibleClient(
        "http://provider.test", transport=httpx.MockTransport(handler),
    )
    async for _ in client.stream(MESSAGES, model="m"):
        pass
    assert captured[-1]["read"] == STREAM_IDLE_TIMEOUT

    # A tighter per-provider timeout wins over the cap.
    client2 = OpenAICompatibleClient(
        "http://provider.test", timeout=10,
        transport=httpx.MockTransport(handler),
    )
    async for _ in client2.stream(MESSAGES, model="m"):
        pass
    assert captured[-1]["read"] == 10


# ---------------------------------------------------------------------------
# The redirect pin: the authority judged at save time is the authority reached
# ---------------------------------------------------------------------------
async def test_a_redirect_is_not_followed():
    """A 302 must not hand the request (and its key header) elsewhere.

    ``follow_redirects`` is set False explicitly in the client so a future
    httpx default change cannot silently start following them; this test is
    the tripwire.
    """
    hits: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        if len(hits) == 1:
            return httpx.Response(
                302, headers={
                    "location": "http://169.254.169.254/latest/meta-data/",
                },
            )
        return chat_response({"content": "should never be reached"})

    client = make_client(handler)
    with pytest.raises(ProviderTransportError) as caught:
        await client.complete(MESSAGES, model="m")

    # The redirect target was never contacted: one request, one answer, and
    # the answer (no JSON body) is reported rather than chased.
    assert len(hits) == 1
    assert "169.254" not in hits[0]
    assert caught.value.kind == "malformed"
