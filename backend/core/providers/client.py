"""Any OpenAI-compatible endpoint, as a provider.

One client covers ``/v1/chat/completions`` and ``/v1/models``, which is what
Ollama, llama.cpp's server, LM Studio, vLLM, text-generation-webui and every
cloud gateway speak. That is the whole point of the abstraction: a user adds a
provider by pasting a URL, and if the endpoint speaks the format, it works.

Two things it is deliberately not:

* **Not a fallback chain.** :mod:`core.providers.router` decides *which* model;
  this decides *how* to talk to it. A transport that also picked models would
  make "why did it use that one?" unanswerable.
* **Not an unrestricted client.** The free-only policy is applied by the router
  before a request is built, and this module refuses a provider whose policy
  would be violated rather than sending it anyway.

Errors are classified, not string-matched at the call site, because the
distinction the product cares about is *retry this model elsewhere* versus *give
up*: a 429 means move on, a 401 means the key is wrong and no other model will
help.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from common.reporting import safe_error
from core.providers.capabilities import Capability, normalise

logger = logging.getLogger("Zaytoon.Core.Providers.Client")

DEFAULT_TIMEOUT = 120.0
DISCOVERY_TIMEOUT = 10.0

#: How long a streamed completion may produce nothing before it is considered
#: stalled. A model that opens the stream and then thinks for three minutes is
#: still working; one that never writes a byte is not.
STREAM_IDLE_TIMEOUT = 90.0


def canonical_base_url(url: str) -> str:
    """The endpoint shaped the way this client will join it.

    The client always appends ``/v1/...`` — the OpenAI convention lives in
    the *path*, so ``base_url`` is an origin plus any real prefix, never its
    own ``/v1``. Users paste what their provider documents
    (``https://openrouter.ai/api/v1``), and un-canonicalised that requests
    ``.../api/v1/v1/models`` and returns a 404 that reads like a broken
    endpoint rather than a pasted URL.

    Parsed, not string-sliced: ``http://v1`` is a *host* called ``v1``, and
    stripping a trailing ``/v1`` off it would corrupt the authority.
    """
    clean = (url or "").strip().rstrip("/")
    if not clean:
        return clean
    parts = urlsplit(clean)
    path = parts.path.rstrip("/")
    if path.lower().endswith("/v1"):
        path = path[: -len("/v1")]
    return urlunsplit(
        (parts.scheme, parts.netloc, path, parts.query, parts.fragment)
    )


class ProviderTransportError(RuntimeError):
    """A provider request failed.

    ``retryable`` says whether to try another *model* (the budget decision);
    ``kind`` says what class of failure this is, in the same vocabulary the
    attempt records and cooldowns use: ``rate_limited``, ``server_error``,
    ``fatal``, ``timeout``, ``malformed``, ``connect_error``. It is set here,
    at the failure site where the cause is known — the alternative was
    string-matching the message downstream, so rewording an error silently
    re-labelled every future failure.

    ``kind=""`` means one class could not be determined: the discovery
    aggregate joins failures of several kinds, and an exception constructed
    by an older caller carries none.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        retryable: bool = False,
        kind: str = "",
    ):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.kind = kind


#: Status codes where trying a *different* model is the right response. A 401 is
#: deliberately absent: a bad key fails identically for every model on that
#: provider, so switching models just burns the budget.
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})

FATAL_STATUS = frozenset({400, 401, 402, 403, 404, 405, 413, 415, 422})


@dataclass
class DiscoveredModel:
    """One model an endpoint reports."""

    id: str
    capabilities: List[str] = field(default_factory=list)
    context_length: Optional[int] = None
    display_name: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.display_name or self.id,
            "capabilities": list(self.capabilities),
            "context_length": self.context_length,
        }


class OpenAICompatibleClient:
    """Chat completions and model discovery against one endpoint."""

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        kind: str = "openai_compatible",
        transport: Optional[httpx.BaseTransport] = None,
        org_id: Optional[str] = None,
    ) -> None:
        # ``transport`` is a test seam (httpx.MockTransport), the same shape
        # ``tools/fetch.py`` uses: adapter request/response behaviour must be
        # provable without a live endpoint or a network.
        self.base_url = canonical_base_url(base_url)
        self.kind = kind
        self._timeout = timeout
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers=self._headers(api_key, org_id),
            transport=transport,
            # Explicit, not httpx's default-by-luck: a registered endpoint
            # must answer for the authority that was judged at save time.
            # Following a redirect would hand the request — including the
            # Authorization header — to an address nobody approved.
            follow_redirects=False,
        )

    @staticmethod
    def _headers(api_key: Optional[str], org_id: Optional[str] = None) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if org_id:
            # OpenAI's organisation header, for endpoints that bill or scope
            # by org. An identifier, not a credential — but still supplied by
            # the backend, never the frontend, so it travels only server-side.
            headers["OpenAI-Organization"] = str(org_id)
        return headers

    # -- discovery --------------------------------------------------------
    async def list_models(self) -> List[DiscoveredModel]:
        """Ask the endpoint what it has.

        Tries ``/v1/models`` then ``/api/tags`` (Ollama's native shape), because
        "OpenAI-compatible" and "Ollama" are different answers for the same
        question and a provider row can be either.
        """
        errors: List[str] = []
        for path, parser in (
            ("/v1/models", _parse_openai_models),
            ("/api/tags", _parse_ollama_tags),
        ):
            try:
                response = await self._client.get(path, timeout=DISCOVERY_TIMEOUT)
            except httpx.HTTPError as exc:
                errors.append(f"{path}: {type(exc).__name__}")
                continue
            if response.status_code >= 400:
                errors.append(f"{path}: HTTP {response.status_code}")
                continue
            try:
                payload = response.json()
            except ValueError:
                errors.append(f"{path}: response was not JSON")
                continue
            models = parser(payload)
            if models:
                return models
            errors.append(f"{path}: no models in the response")

        raise ProviderTransportError(
            "Could not list models. Tried " + "; ".join(errors), retryable=False
        )

    # -- chat -------------------------------------------------------------
    async def complete(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        max_tokens: int = 2048,
        temperature: float = 0.7,
        tools: Optional[List[Dict[str, Any]]] = None,
        stop: Optional[List[str]] = None,
    ) -> str:
        """One non-streaming completion. Returns the assistant's text."""
        payload = self._payload(messages, model, max_tokens, temperature, tools, stop)
        response = await self._post("/v1/chat/completions", payload, stream=False)
        return _extract_text(_json(response))

    async def stream(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        max_tokens: int = 2048,
        temperature: float = 0.7,
        tools: Optional[List[Dict[str, Any]]] = None,
        stop: Optional[List[str]] = None,
        tool_calls_out: Optional[List[str]] = None,
    ) -> AsyncGenerator[str, None]:
        """Stream a completion as text deltas.

        Yields only text. Tool calls arrive as structured deltas on some
        endpoints; those are accumulated and rendered into ``tool_calls_out``
        as JSON-in-text dialect lines rather than streamed as text, because a
        JSON blob arriving character by character is exactly the bug the agent
        engine's "tool calls are not prose" rule exists to prevent — and the
        rendered call must still reach the validator, so it joins the caller's
        accumulated text *after* the stream, off the wire.

        OpenAI-shaped servers fragment a native call across chunks (name in
        one, ``arguments`` pieces in the rest), so entries are merged by
        ``index`` before rendering. With no ``tool_calls_out`` the rendered
        call is yielded as a final delta instead — losing it would turn a
        model's tool call into an empty completion.
        """
        payload = self._payload(messages, model, max_tokens, temperature, tools, stop)
        payload["stream"] = True
        pending: Dict[int, Dict[str, str]] = {}
        # The stall guard. A whole stream is unbounded by design — a long
        # generation is normal — but *silence* is not: a model that opens the
        # stream and then says nothing must not hold the turn open forever.
        # The bound is the provider's own request timeout when it is tighter,
        # STREAM_IDLE_TIMEOUT as the cap. It applies twice over, deliberately:
        # as the httpx read timeout (covers connect and the header wait on a
        # real socket) and around each line read below (covers any transport,
        # including test doubles that never touch a socket).
        read_bound = min(self._timeout, STREAM_IDLE_TIMEOUT)
        stream_timeout = httpx.Timeout(self._timeout, read=read_bound)
        try:
            async with self._client.stream(
                "POST", "/v1/chat/completions", json=payload, timeout=stream_timeout
            ) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    raise _classify(response.status_code, body)

                lines = response.aiter_lines()
                while True:
                    try:
                        line = await asyncio.wait_for(lines.__anext__(), read_bound)
                    except StopAsyncIteration:
                        break
                    except (asyncio.TimeoutError, TimeoutError):
                        raise ProviderTransportError(
                            f"Stream stalled: no data for {read_bound:g}s",
                            retryable=True, kind="timeout",
                        ) from None
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        if data == "[DONE]":
                            break
                        continue
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        # A malformed SSE frame is skipped, not fatal: the stream
                        # may still deliver a complete answer, and aborting a
                        # half-received response loses real content.
                        logger.debug("Skipping unparseable SSE frame")
                        continue
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        text = delta.get("content")
                        if text:
                            yield text
                        for call in delta.get("tool_calls") or []:
                            if not isinstance(call, dict):
                                continue
                            index = call.get("index")
                            slot = pending.setdefault(
                                index if isinstance(index, int) else len(pending),
                                {"name": "", "arguments": ""},
                            )
                            function = call.get("function") or {}
                            name = function.get("name")
                            if name and not slot["name"]:
                                slot["name"] = str(name)
                            arguments = function.get("arguments")
                            if isinstance(arguments, str):
                                slot["arguments"] += arguments
                            elif arguments is not None:
                                slot["arguments"] = json.dumps(
                                    arguments, ensure_ascii=False
                                )

                # The stream closed normally — render what was accumulated.
                rendered = [
                    _render_call(slot["name"].strip(), slot["arguments"])
                    for index in sorted(pending)
                    if (slot := pending[index])["name"].strip()
                ]
                if rendered:
                    if tool_calls_out is not None:
                        tool_calls_out.extend(rendered)
                    else:
                        for line in rendered:
                            yield "\n" + line
        except httpx.HTTPError as exc:
            if isinstance(exc, httpx.TimeoutException):
                raise ProviderTransportError(
                    f"Stream stalled: no data for {read_bound:g}s",
                    retryable=True, kind="timeout",
                ) from exc
            raise ProviderTransportError(
                f"Transport error while streaming: {type(exc).__name__}",
                retryable=True, kind="connect_error",
            ) from exc

    async def health(self) -> Dict[str, Any]:
        """Whether the endpoint answers, and how fast."""
        import time

        started = time.monotonic()
        try:
            response = await self._client.get("/v1/models", timeout=DISCOVERY_TIMEOUT)
        except httpx.HTTPError as exc:
            return {
                "connected": False,
                "error": f"{type(exc).__name__}: {exc}",
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        latency = int((time.monotonic() - started) * 1000)
        if response.status_code in (401, 403):
            return {
                "connected": False,
                "error": f"HTTP {response.status_code}: the endpoint rejected the credentials",
                "latency_ms": latency,
            }
        if response.status_code >= 400:
            return {
                "connected": False,
                "error": f"HTTP {response.status_code} from /v1/models",
                "latency_ms": latency,
            }
        try:
            payload = response.json()
        except ValueError:
            return {"connected": False, "error": "/v1/models did not return JSON", "latency_ms": latency}
        models = _parse_openai_models(payload)
        return {
            "connected": True,
            "error": "",
            "latency_ms": latency,
            "models": [m.id for m in models],
        }

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- internals --------------------------------------------------------
    @staticmethod
    def _payload(
        messages, model, max_tokens, temperature, tools, stop
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if stop:
            payload["stop"] = stop
        return payload

    async def _post(self, path: str, payload: Dict[str, Any], stream: bool):
        try:
            response = await self._client.post(path, json=payload)
        except httpx.HTTPError as exc:
            if isinstance(exc, httpx.TimeoutException):
                raise ProviderTransportError(
                    f"Request timed out ({type(exc).__name__})",
                    retryable=True, kind="timeout",
                ) from exc
            raise ProviderTransportError(
                f"Transport error: {type(exc).__name__}",
                retryable=True, kind="connect_error",
            ) from exc
        if response.status_code >= 400:
            raise _classify(response.status_code, response.content)
        return response


def _classify(status: int, body: bytes) -> ProviderTransportError:
    """Turn an HTTP error into a typed failure.

    The body is decoded and truncated for the message, but only after
    redaction — a provider's error body can echo the request, and the request
    carries the prompt.

    ``kind`` mirrors ``LLMClient._classify_status`` exactly: the outcome
    vocabulary (``rate_limited`` / ``server_error`` / ``fatal``) is one set,
    produced where the status is read. Cooldowns and metrics key off it.
    """
    from common.reporting import redact

    text = redact(body.decode("utf-8", errors="replace"))[:300]
    retryable = status in RETRYABLE_STATUS and status not in FATAL_STATUS
    if status == 429:
        kind = "rate_limited"
    elif status in FATAL_STATUS:
        kind = "fatal"
    elif status in RETRYABLE_STATUS:
        kind = "server_error"
    else:
        kind = "fatal"
    hint = ""
    if status == 429:
        hint = " (rate limited — try a different model)"
    elif status == 401:
        hint = " (the endpoint rejected the API key)"
    elif status == 404:
        hint = (
            " (no such endpoint or model — check the model id; Zaytoon adds "
            "/v1 to the base URL itself)"
        )
    return ProviderTransportError(
        f"HTTP {status}{hint}: {text or '(empty body)'}",
        status=status, retryable=retryable, kind=kind,
    )


def _json(response) -> Dict[str, Any]:
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderTransportError(
            "Provider returned a non-JSON response",
            retryable=False, kind="malformed",
        ) from exc


def _render_call(name: str, arguments: Any) -> str:
    """One native tool call -> one line of the engine's JSON-in-text dialect.

    ``{"action": name, "params": args}`` is the documented dialect
    ``agents.engine.extract_tool_calls`` parses, so a native OpenAI
    ``tool_calls`` entry and a model that types the JSON itself become the
    *same* text for the validator, the contract check and the tool gate.
    ``function.arguments`` is a stringified JSON object per the OpenAI spec
    (Ollama's native shape gives a dict); anything unparseable becomes
    ``params._raw`` rather than being dropped, so the contract reports bad
    arguments instead of silently losing the call.
    """
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments) if arguments.strip() else {}
        except ValueError:
            parsed = {"_raw": arguments}
    elif isinstance(arguments, dict):
        parsed = arguments
    else:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {"_raw": json.dumps(parsed, ensure_ascii=False)}
    return json.dumps({"action": name, "params": parsed}, ensure_ascii=False)


def _render_tool_calls(message: Dict[str, Any]) -> str:
    """Every native ``tool_calls`` entry on a message, rendered as dialect text."""
    lines: List[str] = []
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        lines.append(_render_call(name, function.get("arguments")))
    return "\n".join(lines)


def _extract_text(payload: Dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices:
        raise ProviderTransportError(
            "Provider returned no choices — the completion is empty",
            retryable=True, kind="malformed",
        )
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        # Some endpoints return content as parts rather than a string.
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    text = str(content or "")
    # A model that answers with a *native* tool call and no prose used to come
    # back as an empty string and burn an attempt; render the call so the
    # engine sees it exactly as it would see the JSON dialect.
    native = _render_tool_calls(message)
    if native:
        return f"{text}\n{native}" if text else native
    return text


def _parse_openai_models(payload: Dict[str, Any]) -> List[DiscoveredModel]:
    entries = payload.get("data")
    if not isinstance(entries, list):
        return []
    models: List[DiscoveredModel] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        if not model_id:
            continue
        models.append(DiscoveredModel(
            id=str(model_id),
            display_name=str(entry.get("name") or model_id),
            # An endpoint that reports no capabilities is assumed to do the two
            # the chat-completions contract guarantees. Assuming *less* would
            # make every model unroutable on a freshly added provider.
            capabilities=sorted(normalise(entry.get("capabilities"))),
            context_length=entry.get("context_length") or entry.get("max_context_length"),
            raw=entry,
        ))
    return models


def _parse_ollama_tags(payload: Dict[str, Any]) -> List[DiscoveredModel]:
    entries = payload.get("models")
    if not isinstance(entries, list):
        return []
    models: List[DiscoveredModel] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("model")
        if not name:
            continue
        details = entry.get("details") or {}
        capabilities = {Capability.CHAT.value, Capability.STREAMING.value}
        family = str(details.get("family") or "").lower()
        parameter_size = str(details.get("parameter_size") or "")
        if "embed" in family:
            capabilities = {Capability.EMBEDDINGS.value}
        elif family in ("llama", "gemma", "qwen", "mistral", "phi", "deepseek"):
            capabilities.add(Capability.CODING.value)
        elif family:
            # An unrecognised family still does chat and streaming. The coding
            # capability is *inferred* from a known family and never granted by
            # default, so a model is not advertised as good at code on the
            # strength of an unrecognised name.
            logger.debug("Unknown Ollama model family %r; chat/streaming assumed", family)
        models.append(DiscoveredModel(
            id=str(name),
            display_name=str(entry.get("name") or name),
            capabilities=sorted(capabilities),
            context_length=None,
            raw={"parameter_size": parameter_size} if parameter_size else {},
        ))
    return models


__all__ = [
    "DEFAULT_TIMEOUT",
    "DiscoveredModel",
    "FATAL_STATUS",
    "OpenAICompatibleClient",
    "ProviderTransportError",
    "RETRYABLE_STATUS",
    "STREAM_IDLE_TIMEOUT",
]