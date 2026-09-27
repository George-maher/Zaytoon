"""OpenRouter client with a strict free-model policy and a model fallback chain.

Local model execution (Ollama/Llama) has been removed: every request goes to
OpenRouter's OpenAI-compatible endpoint
``https://openrouter.ai/api/v1/chat/completions``.

Three guarantees are enforced here, in this order:

1. **Allow-list + free form** — checked in :meth:`ProviderEndpoint.__post_init__`
   and re-checked in :meth:`ProviderEndpoint.build_payload`, i.e. *before a
   socket is opened*. A non-allow-listed or non-free model id cannot be called.
2. **Request validation** — headers and payload shape are verified in
   :meth:`ProviderEndpoint.validate_request` before dispatch.
3. **Response validation** — the JSON body is structurally verified in
   :meth:`LLMClient._validate_completion` so a malformed or truncated upstream
   response degrades to a clean fallback instead of an ``AttributeError``.

Fallback order per capability tier: primary model -> ``openrouter/free``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx

from config import (
    PROVIDER_OPENROUTER,
    TIER_FALLBACK,
    TIER_FAST,
    TIER_REASONING,
    NonFreeModelError,
    config,
    is_allowed_model,
)

logger = logging.getLogger("Zaytoon.LLMClient")

# HTTP statuses that should move us on to the next endpoint in the chain.
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})

# Statuses that will never succeed on retry, so we skip ahead immediately.
FATAL_STATUS = frozenset({400, 401, 402, 403, 404, 405, 413, 422})

#: Headers OpenRouter requires/rewards.
REQUIRED_HEADERS = ("Authorization", "HTTP-Referer", "X-Title")


class LLMError(Exception):
    """Raised when every endpoint in the chain has been exhausted."""

    def __init__(self, message: str, attempts: Optional[List["AttemptRecord"]] = None):
        super().__init__(message)
        self.attempts = attempts or []


class LLMRequestError(LLMError):
    """The request we built failed local validation; nothing was sent."""


class LLMResponseError(LLMError):
    """The upstream response was not a well-formed completion."""


@dataclass
class AttemptRecord:
    label: str
    provider: str
    model: str
    outcome: str
    status: Optional[int] = None
    detail: str = ""
    latency_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "outcome": self.outcome,
            "status": self.status,
            "detail": self.detail[:200],
            "latency_ms": self.latency_ms,
        }


@dataclass
class LLMMetrics:
    """Lightweight counters so request/response health is observable."""

    success: int = 0
    rate_limited: int = 0
    server_error: int = 0
    fatal: int = 0
    malformed: int = 0
    timeouts: int = 0
    blocked: int = 0
    total_latency_ms: int = 0
    by_model: Dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def record_success(self, model: str, latency_ms: int) -> None:
        self.success += 1
        self.total_latency_ms += latency_ms
        self.by_model[model] += 1

    def record(self, outcome: str, latency_ms: int = 0) -> None:
        if outcome == "rate_limited":
            self.rate_limited += 1
        elif outcome == "server_error":
            self.server_error += 1
        elif outcome == "malformed":
            self.malformed += 1
        elif outcome in ("timeout", "stream_error"):
            self.timeouts += 1
        else:
            self.fatal += 1

    def to_dict(self) -> dict:
        total = self.success + self.rate_limited + self.server_error + (
            self.malformed + self.timeouts + self.fatal
        )
        return {
            "requests": total,
            "success": self.success,
            "rate_limited": self.rate_limited,
            "server_error": self.server_error,
            "malformed_response": self.malformed,
            "timeouts": self.timeouts,
            "fatal": self.fatal,
            "blocked_by_policy": self.blocked,
            "avg_latency_ms": (
                self.total_latency_ms // self.success if self.success else 0
            ),
            "by_model": dict(self.by_model),
        }


@dataclass(frozen=True)
class ProviderEndpoint:
    provider: str
    model: str
    url: str
    headers: Dict[str, str]
    label: str = ""

    def __post_init__(self):
        if not self.label:
            object.__setattr__(self, "label", f"{self.provider}:{self.model}")
        # Zero-cost guard: a model that is not allow-listed / not free is
        # rejected here, before any network I/O.
        if self.provider == PROVIDER_OPENROUTER and config.free_models_enforced:
            if not is_allowed_model(self.model):
                raise NonFreeModelError(self.model)

    @property
    def is_free(self) -> bool:
        return self.provider != PROVIDER_OPENROUTER or is_allowed_model(self.model)

    # -- request side ----------------------------------------------------
    def build_payload(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: int,
        temperature: float,
        stream: bool = False,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        # Re-checked at call time so a hand-built endpoint, or a config mutated
        # after import, can never slip a billable model past the constructor.
        if self.provider == PROVIDER_OPENROUTER:
            config.assert_free_model(self.model)

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        if stream:
            payload["stream_options"] = {"include_usage": False}
        if tools:
            payload["tools"] = tools
        return payload

    def validate_request(
        self, payload: Dict[str, Any], messages: List[Dict[str, Any]]
    ) -> None:
        """Local pre-flight check. Raises :class:`LLMRequestError` on any fault.

        Catching a malformed payload here keeps the failure deterministic and
        attributable instead of surfacing as an opaque upstream 400.
        """
        if self.provider == PROVIDER_OPENROUTER:
            config.assert_free_model(payload.get("model", ""))
            if not self.url.startswith("https://openrouter.ai/"):
                raise LLMRequestError(f"unexpected endpoint host: {self.url}")

        missing = [h for h in REQUIRED_HEADERS if not self.headers.get(h)]
        if missing:
            raise LLMRequestError(f"missing required headers: {missing}")
        if not str(self.headers.get("Authorization", "")).startswith("Bearer "):
            raise LLMRequestError("Authorization header must be a Bearer token")

        if not isinstance(messages, list) or not messages:
            raise LLMRequestError("messages must be a non-empty list")
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                raise LLMRequestError(f"messages[{i}] is not an object")
            if msg.get("role") not in ("system", "user", "assistant", "tool"):
                raise LLMRequestError(f"messages[{i}] has invalid role {msg.get('role')!r}")
            if "content" not in msg:
                raise LLMRequestError(f"messages[{i}] missing 'content'")

        mt = payload.get("max_tokens")
        if not isinstance(mt, int) or mt <= 0:
            raise LLMRequestError(f"max_tokens must be a positive int, got {mt!r}")
        temp = payload.get("temperature")
        if not isinstance(temp, (int, float)) or not 0.0 <= float(temp) <= 2.0:
            raise LLMRequestError(f"temperature out of range: {temp!r}")
        if not isinstance(payload.get("stream"), bool):
            raise LLMRequestError("stream must be a bool")

    # -- response side ---------------------------------------------------
    def validate_headers(self, response: httpx.Response) -> None:
        """Surface a 200 with a non-JSON content type as a malformed body."""
        ctype = (response.headers.get("content-type") or "").lower()
        if ctype and "json" not in ctype:
            raise LLMResponseError(
                f"expected JSON content-type, got {ctype!r}"
            )

    def validate_completion(self, response: httpx.Response) -> str:
        """Structurally verify a non-streaming completion and return its text.

        Tolerates a provider that answers 200 with an ``error`` object, and
        normalises the multimodal ``content`` list form, but refuses anything
        that would otherwise raise deep inside a caller's formatting code.
        """
        self.validate_headers(response)
        try:
            data = response.json()
        except (json.JSONDecodeError, ValueError) as e:
            raise LLMResponseError(f"response body is not valid JSON: {e}")

        if not isinstance(data, dict):
            raise LLMResponseError(
                f"expected a JSON object, got {type(data).__name__}"
            )

        # Some upstreams return 200 with an error payload.
        error = data.get("error")
        if error:
            message = (
                error.get("message") if isinstance(error, dict) else str(error)
            )
            raise LLMResponseError(f"upstream error payload: {message}")

        choices = data.get("choices")
        if choices is None:
            raise LLMResponseError("response has no 'choices' field")
        if not isinstance(choices, list):
            raise LLMResponseError(
                f"'choices' must be a list, got {type(choices).__name__}"
            )
        if not choices:
            raise LLMResponseError("'choices' is empty")

        first = choices[0]
        if not isinstance(first, dict):
            raise LLMResponseError("choices[0] is not an object")

        message = first.get("message")
        if message is None:
            # Legacy/completions-style shape.
            if "text" in first and isinstance(first["text"], str):
                return first["text"]
            raise LLMResponseError("choices[0] has no 'message'")
        if not isinstance(message, dict):
            raise LLMResponseError(
                f"choices[0].message must be an object, got {type(message).__name__}"
            )

        content = message.get("content")
        if content is None:
            # Tool-call responses legitimately carry null content.
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            # Multimodal: concatenate the text blocks, ignore images.
            parts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            return "".join(parts)
        raise LLMResponseError(
            f"unexpected content type {type(content).__name__}"
        )


@dataclass
class HealthState:
    """Per-model circuit breaker so we stop retrying a rate-limited model."""

    failures: int = 0
    blocked_until: float = 0.0
    last_error: str = ""

    def is_open(self, now: Optional[float] = None) -> bool:
        return (now or time.monotonic()) < self.blocked_until

    def record_success(self):
        self.failures = 0
        self.blocked_until = 0.0
        self.last_error = ""

    def record_failure(self, cooldown: float, detail: str = ""):
        self.failures += 1
        self.last_error = detail
        self.blocked_until = time.monotonic() + cooldown


@dataclass
class LLMClient:
    """Owns model selection, request/response validation, and failover."""

    max_retries: int = config.LLM_MAX_RETRIES
    timeout: float = config.LLM_TIMEOUT
    provider_cooldown: float = config.LLM_PROVIDER_COOLDOWN
    health: Dict[str, HealthState] = field(default_factory=dict)
    metrics: LLMMetrics = field(default_factory=LLMMetrics)

    _client: Optional[httpx.AsyncClient] = None
    _owns_client: bool = False

    # ------------------------------------------------------------------
    # HTTP client lifecycle
    # ------------------------------------------------------------------
    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
            self._owns_client = True
        return self._client

    async def aclose(self):
        if self._client and not self._client.is_closed and self._owns_client:
            await self._client.aclose()
        self._client = None
        self._owns_client = False

    # ------------------------------------------------------------------
    # Chain construction
    # ------------------------------------------------------------------
    def _openrouter_endpoint(self, model: str) -> ProviderEndpoint:
        return ProviderEndpoint(
            provider=PROVIDER_OPENROUTER,
            model=model,
            url=config.LLM_API_URL,
            headers=dict(config.LLM_HEADERS),
        )

    def build_chain(self, tier: str = TIER_FAST) -> List[ProviderEndpoint]:
        """Ordered endpoints for ``tier``.

        Primary model, then ``openrouter/free``. Local model execution is not
        part of the chain.
        """
        if not config.llm_configured:
            raise LLMError(
                "OPENROUTER_API_KEY is not set. Zaytoon calls OpenRouter only "
                "(local model execution has been removed); set the key in "
                "backend/.env."
            )

        tier = tier if tier in (TIER_FAST, TIER_REASONING, TIER_FALLBACK) else TIER_FAST
        head = config.model_for_tier(tier)
        fallback = config.OPENROUTER_FALLBACK_MODEL

        chain: List[ProviderEndpoint] = [self._openrouter_endpoint(head)]
        if fallback and fallback != head:
            chain.append(self._openrouter_endpoint(fallback))

        billable = [ep.label for ep in chain if not ep.is_free]
        if billable:
            raise NonFreeModelError(", ".join(billable))

        healthy = [ep for ep in chain if not self._is_blocked(ep)]
        # If every model is benched, ignore the breaker rather than failing
        # outright — the cooldown is advisory, not a hard gate.
        return healthy or chain

    def _health(self, model: str) -> HealthState:
        if model not in self.health:
            self.health[model] = HealthState()
        return self.health[model]

    def _is_blocked(self, endpoint: ProviderEndpoint) -> bool:
        return self._health(endpoint.model).is_open()

    def health_snapshot(self) -> Dict[str, dict]:
        return {
            model: {
                "failures": state.failures,
                "blocked": state.is_open(),
                "last_error": state.last_error,
            }
            for model, state in self.health.items()
        }

    # ------------------------------------------------------------------
    # Request execution
    # ------------------------------------------------------------------
    def _classify_status(self, status: int) -> str:
        if status == 429:
            return "rate_limited"
        if status in FATAL_STATUS:
            return "fatal"
        if status in RETRYABLE_STATUS:
            return "server_error"
        return "fatal"

    def _human_error(self, status: int, body: str) -> str:
        if status == 401:
            return "Unauthorized — check your OPENROUTER_API_KEY."
        if status == 402:
            return "OpenRouter requires payment. Add credits at https://openrouter.ai."
        if status == 403:
            return (
                "Forbidden — this model may be restricted to specific client "
                "types. The chain will fall back."
            )
        if status == 404:
            return "Model not available on OpenRouter."
        if status == 429:
            return "Rate limited by OpenRouter (free tier is heavily contended)."
        return f"HTTP {status}: {(body or '')[:200]}"

    async def complete(
        self,
        messages: List[Dict[str, Any]],
        tier: str = TIER_FAST,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        tools: Optional[List[Dict[str, Any]]] = None,
        retries: Optional[int] = None,
    ) -> str:
        """Non-streaming completion. Walks the chain until one model answers."""
        attempts: List[AttemptRecord] = []
        chain = self.build_chain(tier)
        attempts_allowed = max(1, self.max_retries if retries is None else retries)

        for endpoint in chain:
            payload = endpoint.build_payload(
                messages, max_tokens, temperature, stream=False, tools=tools
            )
            endpoint.validate_request(payload, messages)

            for attempt in range(attempts_allowed):
                started = time.monotonic()
                try:
                    client = await self._get_client()
                    response = await client.post(
                        endpoint.url, json=payload, headers=endpoint.headers
                    )
                except httpx.TimeoutException:
                    elapsed = int((time.monotonic() - started) * 1000)
                    attempts.append(
                        AttemptRecord(endpoint.label, endpoint.provider,
                                      endpoint.model, "timeout",
                                      detail="timeout", latency_ms=elapsed)
                    )
                    self.metrics.record("timeout", elapsed)
                    logger.warning(
                        "LLM TIMEOUT  model=%s attempt=%d after %dms",
                        endpoint.model, attempt + 1, elapsed,
                    )
                    self._health(endpoint.model).record_failure(
                        self.provider_cooldown, "timeout"
                    )
                    break  # next model in the chain
                except httpx.ConnectError as e:
                    elapsed = int((time.monotonic() - started) * 1000)
                    attempts.append(
                        AttemptRecord(endpoint.label, endpoint.provider,
                                      endpoint.model, "connect_error",
                                      detail=str(e), latency_ms=elapsed)
                    )
                    self.metrics.record("connect_error", elapsed)
                    logger.warning(
                        "LLM CONNECT_ERROR  model=%s: %s", endpoint.model, e
                    )
                    self._health(endpoint.model).record_failure(
                        self.provider_cooldown, "connect_error"
                    )
                    break
                except Exception as e:
                    elapsed = int((time.monotonic() - started) * 1000)
                    attempts.append(
                        AttemptRecord(endpoint.label, endpoint.provider,
                                      endpoint.model, "error",
                                      detail=str(e), latency_ms=elapsed)
                    )
                    self.metrics.record("error", elapsed)
                    logger.error("LLM ERROR  model=%s: %s", endpoint.model, e)
                    self._health(endpoint.model).record_failure(
                        self.provider_cooldown, str(e)
                    )
                    break

                elapsed = int((time.monotonic() - started) * 1000)
                status = response.status_code

                if status == 200:
                    try:
                        content = endpoint.validate_completion(response)
                    except LLMResponseError as e:
                        attempts.append(
                            AttemptRecord(endpoint.label, endpoint.provider,
                                          endpoint.model, "malformed", 200,
                                          str(e), elapsed)
                        )
                        self.metrics.record("malformed", elapsed)
                        logger.error(
                            "LLM MALFORMED  model=%s: %s", endpoint.model, e
                        )
                        self._health(endpoint.model).record_failure(
                            self.provider_cooldown, str(e)
                        )
                        continue  # retry same model, then move on

                    self._health(endpoint.model).record_success()
                    self.metrics.record_success(endpoint.model, elapsed)
                    attempts.append(
                        AttemptRecord(endpoint.label, endpoint.provider,
                                      endpoint.model, "ok", 200, latency_ms=elapsed)
                    )
                    logger.info(
                        "LLM OK  model=%s tokens_hint=%d latency=%dms",
                        endpoint.model, len(content.split()), elapsed,
                    )
                    return content

                body = (response.text or "")[:500]
                kind = self._classify_status(status)
                detail = self._human_error(status, body)
                attempts.append(
                    AttemptRecord(endpoint.label, endpoint.provider,
                                  endpoint.model, kind, status, detail, elapsed)
                )
                self.metrics.record(kind, elapsed)
                logger.warning(
                    "LLM %s  model=%s status=%s: %s",
                    kind.upper(), endpoint.model, status, detail,
                )
                self._health(endpoint.model).record_failure(
                    self.provider_cooldown, detail
                )
                if kind == "fatal":
                    break
                if attempt < attempts_allowed - 1:
                    await asyncio.sleep(0.5 * (2 ** attempt))

        logger.error(
            "LLM EXHAUSTED  %d attempts across %s",
            len(attempts), [a.model for a in attempts] or "(none)",
        )
        raise LLMError(
            f"All OpenRouter models failed ({len(attempts)} attempts)", attempts
        )

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_sse_line(data_str: str) -> Optional[str]:
        """Return the content delta from one SSE ``data:`` payload, or None."""
        try:
            chunk = json.loads(data_str)
        except json.JSONDecodeError:
            return None  # keep-alive / comment / truncated frame
        if not isinstance(chunk, dict):
            return None
        if chunk.get("error"):
            return None
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        delta = (choices[0] or {}).get("delta") or {}
        content = delta.get("content")
        return content if isinstance(content, str) else None

    async def stream(
        self,
        messages: List[Dict[str, Any]],
        on_delta: Optional[Callable[[str], Awaitable[None]]] = None,
        tier: str = TIER_FAST,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        tools: Optional[List[Dict[str, Any]]] = None,
        retries: Optional[int] = None,
    ) -> str:
        """Stream SSE deltas through ``on_delta``, returning the full text.

        Fallback semantics:

        * Failure *before* the first delta retries the next model, still
          streaming, so early-TTS triggers keep working.
        * Failure *after* deltas were emitted switches to a non-streaming call
          on the next model. The caller still receives the authoritative full
          text, and the WebSocket layer overwrites the streamed bubble with the
          final ``message`` event, so no duplication reaches the user.
        """
        attempts: List[AttemptRecord] = []
        chain = self.build_chain(tier)
        attempts_allowed = max(1, self.max_retries if retries is None else retries)
        client = await self._get_client()
        emitted = False
        last_partial = ""

        for endpoint in chain:
            payload = endpoint.build_payload(
                messages, max_tokens, temperature, stream=True, tools=tools
            )
            endpoint.validate_request(payload, messages)

            for attempt in range(attempts_allowed):
                full = ""
                started = time.monotonic()
                try:
                    async with client.stream(
                        "POST", endpoint.url, json=payload, headers=endpoint.headers
                    ) as response:
                        if response.status_code != 200:
                            await response.aread()
                            body = (response.text or "")[:500]
                            kind = self._classify_status(response.status_code)
                            detail = self._human_error(response.status_code, body)
                            attempts.append(
                                AttemptRecord(
                                    endpoint.label, endpoint.provider,
                                    endpoint.model, kind, response.status_code,
                                    detail, int((time.monotonic() - started) * 1000),
                                )
                            )
                            self.metrics.record(
                                kind, int((time.monotonic() - started) * 1000)
                            )
                            logger.warning(
                                "LLM STREAM %s  model=%s status=%s: %s",
                                kind.upper(), endpoint.model,
                                response.status_code, detail,
                            )
                            self._health(endpoint.model).record_failure(
                                self.provider_cooldown, detail
                            )
                            break  # next model

                        async for line in response.aiter_lines():
                            if not line or not line.startswith("data:"):
                                continue
                            data_str = line[5:].strip()
                            if data_str == "[DONE]":
                                break
                            delta = self._parse_sse_line(data_str)
                            if not delta:
                                continue
                            full += delta
                            emitted = True
                            last_partial = full
                            if on_delta:
                                await on_delta(delta)

                    elapsed = int((time.monotonic() - started) * 1000)
                    self._health(endpoint.model).record_success()
                    self.metrics.record_success(endpoint.model, elapsed)
                    attempts.append(
                        AttemptRecord(endpoint.label, endpoint.provider,
                                      endpoint.model, "ok", 200,
                                      latency_ms=elapsed)
                    )
                    logger.info(
                        "LLM STREAM OK  model=%s chars=%d latency=%dms",
                        endpoint.model, len(full), elapsed,
                    )
                    if full:
                        return full
                    raise LLMResponseError("stream completed with no content")

                except LLMResponseError as e:
                    attempts.append(
                        AttemptRecord(endpoint.label, endpoint.provider,
                                      endpoint.model, "malformed", 200, str(e),
                                      int((time.monotonic() - started) * 1000))
                    )
                    self.metrics.record("malformed")
                    logger.error("LLM STREAM MALFORMED  model=%s: %s", endpoint.model, e)
                    self._health(endpoint.model).record_failure(
                        self.provider_cooldown, str(e)
                    )
                    break
                except (httpx.TimeoutException, httpx.ConnectError) as e:
                    elapsed = int((time.monotonic() - started) * 1000)
                    attempts.append(
                        AttemptRecord(endpoint.label, endpoint.provider,
                                      endpoint.model, "stream_error", None,
                                      str(e), elapsed)
                    )
                    self.metrics.record("stream_error", elapsed)
                    logger.warning(
                        "LLM STREAM ERROR  model=%s after %dms: %s",
                        endpoint.model, elapsed, e,
                    )
                    self._health(endpoint.model).record_failure(
                        self.provider_cooldown, str(e)
                    )
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    attempts.append(
                        AttemptRecord(endpoint.label, endpoint.provider,
                                      endpoint.model, "stream_error", None,
                                      str(e), int((time.monotonic() - started) * 1000))
                    )
                    self.metrics.record("stream_error")
                    logger.warning(
                        "LLM STREAM FAILURE  model=%s: %s", endpoint.model, e
                    )
                    self._health(endpoint.model).record_failure(
                        self.provider_cooldown, str(e)
                    )
                    break

            # This model is done. If we already pushed tokens we must not
            # stream a second copy — complete it with a blocking call instead.
            if emitted:
                logger.info(
                    "LLM STREAM BROKE after first token on %s; "
                    "completing via remaining models", endpoint.model,
                )
                try:
                    return await self.complete(
                        messages, tier=tier, max_tokens=max_tokens,
                        temperature=temperature, tools=tools,
                    )
                except LLMError:
                    logger.warning(
                        "recovery completion failed; returning %d chars of "
                        "partial stream from %s", len(last_partial), endpoint.model,
                    )
                    return last_partial
            emitted = False

        raise LLMError(
            f"All OpenRouter models failed while streaming ({len(attempts)} attempts)",
            attempts,
        )


# Module-level singleton used by the agents.
_default_client = LLMClient()


def get_llm_client() -> LLMClient:
    return _default_client


async def close_llm_client():
    await _default_client.aclose()
