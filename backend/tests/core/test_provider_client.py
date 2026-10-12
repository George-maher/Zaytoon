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

import json

import httpx
import pytest

from agents.engine import extract_tool_calls
from core.providers.client import OpenAICompatibleClient

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
