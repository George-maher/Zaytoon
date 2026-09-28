"""OpenRouter client: FREE-only, capability-routed, contract-validated.

Every request goes to OpenRouter's OpenAI-compatible endpoint
``https://openrouter.ai/api/v1/chat/completions``. There is no local fallback
and there never was one — the free tier is the whole cost model.

Four guarantees are enforced here, in this order:

1. **Allow-list + free form** — checked in :meth:`ProviderEndpoint.__post_init__`
   and again in :meth:`ProviderEndpoint.build_payload`, i.e. *before a socket is
   opened*. A non-allow-listed or non-free model id cannot be called.
2. **Capability routing** — :meth:`LLMClient.build_chain` selects only pool
   members that satisfy the request's requirements, and refuses the
   uncontrolled ``openrouter/free`` auto-router outright.
3. **Request validation** — headers and payload shape are verified in
   :meth:`ProviderEndpoint.validate_request` before dispatch.
4. **Contract validation** — an HTTP 200 is *not* success. The completion is
   checked against the request's :class:`CompletionContract` before it is
   allowed to become the answer.

Point 4 is the one that was missing. Previously a 200 with a non-empty string
in ``content`` was accepted, so when the primary was rate-limited and the chain
fell through to ``openrouter/free``, an arbitrary free model could answer a
``get_time`` request with the literal string ``User Safety: safe`` and the user
would be shown it as the assistant's reply.

Fallback now happens on **both** transport failure (429/5xx/timeout) *and*
contract failure (unknown tool, bad arguments, missing tool call, empty
completion, provider metadata leaked as content), under a single global attempt
budget so the loop is provably finite.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import re
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

import httpx

from config import (
    PROVIDER_OPENROUTER,
    TIER_FALLBACK,
    TIER_FAST,
    TIER_REASONING,
    NoCapableModelError,
    NonFreeModelError,
    config,
    is_allowed_model,
)
from services.completion_validator import (
    CHAT_CONTRACT,
    STREAM_CONTRACT,
    CompletionContract,
    validate_completion,
)
from services.model_registry import Selection, select_models

logger = logging.getLogger("Zaytoon.LLMClient")

# HTTP statuses that should move us on to the next endpoint in the chain.
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})

# Statuses that will never succeed on retry, so we skip ahead immediately.
FATAL_STATUS = frozenset({400, 401, 402, 403, 404, 405, 413, 422})

#: Headers OpenRouter requires/rewards.
REQUIRED_HEADERS = ("Authorization", "HTTP-Referer", "X-Title")

#: Outcome names for a completion that was structurally fine but failed its
#: contract. Distinct from ``malformed`` (a broken body) because the remedy is
#: different: a different *model*, not a retry of the same one.
OUTCOME_INVALID = "invalid"


class LLMError(Exception):
    """Raised when every endpoint in the chain has been exhausted."""

    def __init__(self, message: str, attempts: Optional[List["AttemptRecord"]] = None):
        super().__init__(message)
        self.attempts = attempts or []


class LLMRequestError(LLMError):
    """The request we built failed local validation; nothing was sent."""


class LLMResponseError(LLMError):
    """The upstream response was not a well-formed completion."""


class LLMContractError(LLMResponseError):
    """A well-formed completion that does not satisfy the request.

    The distinction matters: a malformed body suggests the provider is broken,
    whereas a valid body that answers the wrong question suggests the *model* is
    unfit, and the only remedy is a different free model.
    """

    def __init__(self, message: str, verdict: str = ""):
        super().__init__(message)
        self.verdict = verdict


class AllModelsUnavailableError(LLMError):
    """Every compatible free model failed. The user gets a controlled message.

    Never a fabricated answer and never the last invalid completion.
    """


@dataclass
class AttemptRecord:
    label: str
    provider: str
    model: str
    outcome: str
    status: Optional[int] = None
    detail: str = ""
    latency_ms: int = 0
    #: 1-based position in the attempt budget, across all models.
    attempt: int = 0
    #: Correlates every line of one logical request. Generated here so the agent
    #: loop and the client agree without threading a parameter everywhere.
    request_id: str = ""
    #: Verdict from the contract validator (``OK`` when it passed).
    validation: str = ""
    #: Capabilities the request required. Empty for a plain chat turn.
    required_capabilities: List[str] = field(default_factory=list)
    #: Tool the model called, when it called one.
    tool_called: str = ""
    #: Why we moved off this model, in the operator's vocabulary.
    fallback_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "outcome": self.outcome,
            "status": self.status,
            "detail": self.detail[:200],
            "latency_ms": self.latency_ms,
            "attempt": self.attempt,
            "request_id": self.request_id,
            "validation": self.validation,
            "required_capabilities": self.required_capabilities,
            "tool_called": self.tool_called,
            "fallback_reason": self.fallback_reason,
        }


@dataclass
class _AttemptBudget:
    """A provably finite number of model calls for one logical request.

    This is the guard against an infinite fallback loop. It counts *model calls*,
    not HTTP retries within a model, so ``LLM_MAX_RETRIES`` cannot multiply it
    back out.
    """

    limit: int
    _counter: "itertools.count" = field(default_factory=lambda: itertools.count(1))
    used: int = 0

    def take(self) -> Optional[int]:
        """Consume one unit, returning its 1-based number, or ``None`` if spent."""
        if self.used >= max(1, self.limit):
            return None
        self.used += 1
        return next(self._counter)

    @property
    def exhausted(self) -> bool:
        return self.used >= max(1, self.limit)


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
    #: Contract-failure counts, keyed by verdict. This is the metric that would
    #: have caught the ``openrouter/free`` problem: a model that answers every
    #: request with valid-looking garbage shows up here, not in ``success``.
    invalid: Dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def record_success(self, model: str, latency_ms: int) -> None:
        self.success += 1
        self.total_latency_ms += latency_ms
        self.by_model[model] += 1

    def record_invalid(self, verdict: str) -> None:
        self.invalid[verdict or "UNKNOWN"] += 1

    def record(self, outcome: str, latency_ms: int = 0) -> None:
        if outcome == "rate_limited":
            self.rate_limited += 1
        elif outcome == "server_error":
            self.server_error += 1
        elif outcome == "malformed":
            self.malformed += 1
        elif outcome in ("timeout", "stream_error", "connect_error"):
            self.timeouts += 1
        else:
            self.fatal += 1

    def to_dict(self) -> dict:
        total = self.success + self.rate_limited + self.server_error + (
            self.malformed + self.timeouts + self.fatal
        ) + sum(self.invalid.values())
        return {
            "requests": total,
            "success": self.success,
            "rate_limited": self.rate_limited,
            "server_error": self.server_error,
            "malformed_response": self.malformed,
            "invalid_completion": dict(self.invalid),
            "invalid_completion_total": sum(self.invalid.values()),
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

    # `default_factory` rather than a bare default, so these read `config` at
    # *instance* construction. A bare default is evaluated once when the class
    # body executes, which meant an `OPENROUTER_MAX_ATTEMPTS` set after import
    # — by a test, or by a runtime settings change — was silently ignored.
    max_retries: int = field(default_factory=lambda: config.LLM_MAX_RETRIES)
    #: Global ceiling on model calls per logical request, across every model in
    #: the chain. This is what makes an infinite fallback loop impossible.
    max_attempts: int = field(
        default_factory=lambda: config.OPENROUTER_MAX_ATTEMPTS
    )
    timeout: float = field(default_factory=lambda: config.LLM_TIMEOUT)
    provider_cooldown: float = field(
        default_factory=lambda: config.LLM_PROVIDER_COOLDOWN
    )
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

    def build_chain(
        self,
        tier: str = TIER_FAST,
        requires: Sequence[str] = (),
    ) -> List[ProviderEndpoint]:
        """Ordered, capability-satisfying endpoints for ``tier``.

        Deterministic by construction: the same configuration and the same
        requirement always produce the same chain, so a routing bug can be
        reproduced and a fix can be verified. The previous chain ended in
        ``openrouter/free``, which made that impossible.

        Raises :class:`NoCapableModelError` when nothing in the pool can do the
        job. That is the honest outcome — better than silently degrading to a
        model that cannot comply, which is how "User Safety: safe" reached a
        user.
        """
        if not config.llm_configured:
            raise LLMError(
                "OPENROUTER_API_KEY is not set. Zaytoon calls OpenRouter only "
                "(local model execution has been removed); set the key in "
                "backend/.env."
            )

        tier = tier if tier in (TIER_FAST, TIER_REASONING, TIER_FALLBACK) else TIER_FAST
        pool = config.model_pool(tier)
        selection = select_models(pool, requires)

        for model, reason in selection.skipped:
            logger.warning("LLM POOL  excluded %s: %s", model, reason)

        if not selection.models:
            # Distinguish "you configured something billable" from "nothing in
            # the pool can do this job". The first is a mistake to fix in .env
            # and deserves to say so; conflating the two would leave an operator
            # staring at a capability error caused by a typo in a model id.
            billable = [
                model for model, reason in selection.skipped
                if "not a free model" in reason or "auto-router" in reason
            ]
            if billable:
                raise NonFreeModelError(", ".join(billable))

            raise NoCapableModelError(
                f"No allow-listed free model satisfies the required capabilities "
                f"{sorted(set(requires)) or '[]'} for tier {tier!r}. "
                f"Configured pool: {pool}"
            )

        chain = [self._openrouter_endpoint(model) for model in selection.models]

        billable = [ep.label for ep in chain if not ep.is_free]
        if billable:  # defence in depth: the constructor already refused these
            raise NonFreeModelError(", ".join(billable))

        healthy = [ep for ep in chain if not self._is_blocked(ep)]
        # If every model is benched, ignore the breaker rather than failing
        # outright — the cooldown is advisory, not a hard gate. Ordering is
        # preserved either way, so this cannot reintroduce a random model.
        return healthy or chain

    def plan(self, tier: str = TIER_FAST, requires: Sequence[str] = ()) -> Selection:
        """The routing decision without building endpoints. For /health and logs."""
        tier = tier if tier in (TIER_FAST, TIER_REASONING, TIER_FALLBACK) else TIER_FAST
        return select_models(config.model_pool(tier), requires)

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

    @staticmethod
    def _retry_after_seconds(response: Optional[httpx.Response], default: float) -> float:
        """Honour a ``Retry-After`` header when the provider sends one.

        Capped, because an upstream that says "retry in an hour" must not be able
        to hang a local desktop assistant for an hour.
        """
        if response is None:
            return default
        raw = response.headers.get("retry-after") if response.headers else None
        if not raw:
            return default
        try:
            seconds = float(str(raw).strip())
        except (TypeError, ValueError):
            return default
        if seconds < 0:
            return default
        return min(seconds, 5.0)

    def _log_attempt(self, record: AttemptRecord) -> None:
        """One structured line per attempt.

        Deliberately never includes the API key, the Authorization header, or
        user message content. The model id, status, verdict and reason are the
        whole diagnostic surface, and none of it is sensitive.
        """
        level = logging.INFO
        if record.outcome in ("rate_limited", "invalid", OUTCOME_INVALID):
            level = logging.WARNING
        elif record.outcome in ("error", "connect_error", "timeout", "malformed",
                                "stream_error", "fatal"):
            level = logging.ERROR
        logger.log(
            level,
            "LLM_ATTEMPT request=%s attempt=%d/%s model=%s status=%s "
            "validation=%s tool=%s caps=%s reason=%s latency=%dms",
            record.request_id or "-",
            record.attempt,
            self.max_attempts,
            record.model,
            record.status if record.status is not None else "-",
            record.validation or "-",
            record.tool_called or "-",
            ",".join(record.required_capabilities) or "-",
            record.fallback_reason or record.detail[:120] or "-",
            record.latency_ms,
        )

    def _cooldown_for(self, outcome: str) -> float:
        """429 benches a model for longer than a transient 5xx."""
        if outcome == "rate_limited":
            return max(self.provider_cooldown, config.LLM_RATE_LIMIT_COOLDOWN)
        return self.provider_cooldown

    async def complete(
        self,
        messages: List[Dict[str, Any]],
        tier: str = TIER_FAST,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        tools: Optional[List[Dict[str, Any]]] = None,
        retries: Optional[int] = None,
        contract: CompletionContract = CHAT_CONTRACT,
        extract: Optional[Callable[[str], Any]] = None,
        request_id: str = "",
    ) -> str:
        """Non-streaming completion, validated against ``contract``.

        Walks the capability-filtered chain under a single global attempt
        budget, and moves on for **contract** failures as well as transport
        failures. A completion is returned only if it satisfies the contract; a
        200 that does not is recorded and discarded, never handed up as the
        answer.

        Raises :class:`AllModelsUnavailableError` when the budget is spent. The
        caller must treat that as "no answer", not as an error to show verbatim.
        """
        attempts: List[AttemptRecord] = []
        rid = request_id or uuid.uuid4().hex[:12]
        required = list(contract.required_capabilities)
        chain = self.build_chain(tier, required)
        budget = _AttemptBudget(self.max_attempts)
        retries_allowed = max(1, self.max_retries if retries is None else retries)

        for endpoint in chain:
            if budget.exhausted:
                break

            payload = endpoint.build_payload(
                messages, max_tokens, temperature, stream=False, tools=tools
            )
            endpoint.validate_request(payload, messages)

            for _ in range(retries_allowed):
                number = budget.take()
                if number is None:
                    break
                started = time.monotonic()

                try:
                    client = await self._get_client()
                    response = await client.post(
                        endpoint.url, json=payload, headers=endpoint.headers
                    )
                except httpx.TimeoutException:
                    record = AttemptRecord(
                        endpoint.label, endpoint.provider, endpoint.model,
                        "timeout", detail="timeout",
                        latency_ms=int((time.monotonic() - started) * 1000),
                        attempt=number, request_id=rid,
                        required_capabilities=required,
                        fallback_reason="TIMEOUT",
                    )
                    self._log_attempt(record)
                    attempts.append(record)
                    self.metrics.record("timeout", record.latency_ms)
                    self._health(endpoint.model).record_failure(
                        self.provider_cooldown, "timeout"
                    )
                    break  # next model in the chain
                except httpx.ConnectError as e:
                    record = AttemptRecord(
                        endpoint.label, endpoint.provider, endpoint.model,
                        "connect_error", detail=str(e),
                        latency_ms=int((time.monotonic() - started) * 1000),
                        attempt=number, request_id=rid,
                        required_capabilities=required,
                        fallback_reason="CONNECT_ERROR",
                    )
                    self._log_attempt(record)
                    attempts.append(record)
                    self.metrics.record("connect_error", record.latency_ms)
                    self._health(endpoint.model).record_failure(
                        self.provider_cooldown, "connect_error"
                    )
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    record = AttemptRecord(
                        endpoint.label, endpoint.provider, endpoint.model,
                        "error", detail=str(e),
                        latency_ms=int((time.monotonic() - started) * 1000),
                        attempt=number, request_id=rid,
                        required_capabilities=required,
                        fallback_reason="ERROR",
                    )
                    self._log_attempt(record)
                    attempts.append(record)
                    self.metrics.record("error", record.latency_ms)
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
                        record = AttemptRecord(
                            endpoint.label, endpoint.provider, endpoint.model,
                            "malformed", 200, str(e), elapsed,
                            attempt=number, request_id=rid,
                            required_capabilities=required,
                            fallback_reason="MALFORMED_BODY",
                        )
                        self._log_attempt(record)
                        attempts.append(record)
                        self.metrics.record("malformed", elapsed)
                        self._health(endpoint.model).record_failure(
                            self.provider_cooldown, str(e)
                        )
                        continue  # retry same model, then move on

                    # --- The fix. A 200 is not success. ------------------
                    verdict = validate_completion(content, contract, extract=extract)
                    if not verdict.ok:
                        tool_name = ""
                        if verdict.tool_calls:
                            tool_name = getattr(verdict.tool_calls[0], "name", "")
                        record = AttemptRecord(
                            endpoint.label, endpoint.provider, endpoint.model,
                            OUTCOME_INVALID, 200, verdict.detail, elapsed,
                            attempt=number, request_id=rid,
                            validation=verdict.verdict,
                            required_capabilities=required,
                            tool_called=tool_name,
                            fallback_reason=verdict.verdict,
                        )
                        self._log_attempt(record)
                        attempts.append(record)
                        self.metrics.record_invalid(verdict.verdict)
                        # The model answered, wrongly. Benning it is right: a
                        # second identical request usually gets a second
                        # identical non-answer.
                        self._health(endpoint.model).record_failure(
                            self.provider_cooldown, verdict.verdict
                        )
                        break  # a different model, not a retry of this one

                    self._health(endpoint.model).record_success()
                    self.metrics.record_success(endpoint.model, elapsed)
                    record = AttemptRecord(
                        endpoint.label, endpoint.provider, endpoint.model,
                        "ok", 200, latency_ms=elapsed,
                        attempt=number, request_id=rid,
                        validation=verdict.verdict,
                        required_capabilities=required,
                        tool_called=(
                            getattr(verdict.tool_calls[0], "name", "")
                            if verdict.tool_calls else ""
                        ),
                    )
                    self._log_attempt(record)
                    attempts.append(record)
                    return content

                body = (response.text or "")[:500]
                kind = self._classify_status(status)
                detail = self._human_error(status, body)
                record = AttemptRecord(
                    endpoint.label, endpoint.provider, endpoint.model,
                    kind, status, detail, elapsed,
                    attempt=number, request_id=rid,
                    required_capabilities=required,
                    fallback_reason=kind.upper(),
                )
                self._log_attempt(record)
                attempts.append(record)
                self.metrics.record(kind, elapsed)
                self._health(endpoint.model).record_failure(
                    self._cooldown_for(kind), detail
                )
                if kind == "fatal":
                    break
                if kind == "rate_limited":
                    # Do not sit on a rate-limited model. Move to another free
                    # model now; that is the whole point of having a pool.
                    break
                await asyncio.sleep(0.5 * (2 ** _))

        verdict = self._summarise(attempts)
        logger.error(
            "LLM EXHAUSTED  request=%s attempts=%d models=%s reason=%s",
            rid, len(attempts), [a.model for a in attempts] or "(none)", verdict,
        )
        raise AllModelsUnavailableError(
            f"All compatible free models failed ({len(attempts)} attempts): {verdict}",
            attempts,
        )

    @staticmethod
    def _summarise(attempts: List[AttemptRecord]) -> str:
        """A short, human-usable explanation of why the budget was spent."""
        if not attempts:
            return "no attempt was made"
        reasons: List[str] = []
        for record in attempts:
            reason = record.fallback_reason or record.outcome
            if reason and reason not in reasons:
                reasons.append(reason)
        return ", ".join(reasons[:4]) or "unknown"

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
        contract: CompletionContract = STREAM_CONTRACT,
        extract: Optional[Callable[[str], Any]] = None,
        request_id: str = "",
    ) -> str:
        """Stream SSE deltas through ``on_delta``, returning the full text.

        Fallback semantics:

        * Failure *before* the first delta retries the next model, still
          streaming, so early-TTS triggers keep working.
        * Failure *after* deltas were emitted switches to a non-streaming call
          on the next model. The caller still receives the authoritative full
          text, and the WebSocket layer overwrites the streamed bubble with the
          final ``message`` event, so no duplication reaches the user.
        * A 429 moves straight to the next free model rather than retrying the
          contended one.

        The assembled text is validated too, with one honest caveat: **deltas
        have already been shown to the user by the time the whole reply can be
        judged.** That is why a violation here falls back to a blocking
        ``complete()`` whose result replaces the bubble, rather than being
        accepted silently. Perfect recall is not achievable once text is on
        screen; the WebSocket layer's final ``message`` overwrite is the
        mitigation, and it is the reason the blocking recovery exists.
        """
        attempts: List[AttemptRecord] = []
        rid = request_id or uuid.uuid4().hex[:12]
        required = list(contract.required_capabilities)
        chain = self.build_chain(tier, required)
        budget = _AttemptBudget(self.max_attempts)
        attempts_allowed = max(1, self.max_retries if retries is None else retries)
        client = await self._get_client()
        emitted = False
        last_partial = ""

        for endpoint in chain:
            if budget.exhausted:
                break

            payload = endpoint.build_payload(
                messages, max_tokens, temperature, stream=True, tools=tools
            )
            endpoint.validate_request(payload, messages)

            for attempt in range(attempts_allowed):
                number = budget.take()
                if number is None:
                    break
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
                                self._cooldown_for(kind), detail
                            )
                            break  # next model (429 included)

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

                    if not full:
                        raise LLMResponseError("stream completed with no content")

                    verdict = validate_completion(full, contract, extract=extract)
                    if not verdict.ok:
                        attempts.append(
                            AttemptRecord(
                                endpoint.label, endpoint.provider, endpoint.model,
                                OUTCOME_INVALID, 200, verdict.detail,
                                int((time.monotonic() - started) * 1000),
                                attempt=number, request_id=rid,
                                validation=verdict.verdict,
                                required_capabilities=required,
                                fallback_reason=verdict.verdict,
                            )
                        )
                        self.metrics.record_invalid(verdict.verdict)
                        self._health(endpoint.model).record_failure(
                            self.provider_cooldown, verdict.verdict
                        )
                        break  # a different model, not a retry of this one

                    elapsed = int((time.monotonic() - started) * 1000)
                    self._health(endpoint.model).record_success()
                    self.metrics.record_success(endpoint.model, elapsed)
                    attempts.append(
                        AttemptRecord(endpoint.label, endpoint.provider,
                                      endpoint.model, "ok", 200,
                                      latency_ms=elapsed,
                                      attempt=number, request_id=rid,
                                      validation=verdict.verdict,
                                      required_capabilities=required)
                    )
                    logger.info(
                        "LLM STREAM OK  model=%s chars=%d latency=%dms",
                        endpoint.model, len(full), elapsed,
                    )
                    return full

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
                        contract=contract, extract=extract, request_id=rid,
                    )
                except LLMError:
                    # Returning the partial stream here is the one place text
                    # that has not been validated can reach the user, and it is
                    # unavoidable: those tokens are already on their screen. It
                    # is the streamed output of a model that produced
                    # *something*, and the WebSocket layer overwrites the bubble
                    # with the final message. The alternative — raising and
                    # leaving the bubble frozen mid-sentence — is worse.
                    logger.warning(
                        "recovery completion failed; returning %d chars of "
                        "partial stream from %s", len(last_partial), endpoint.model,
                    )
                    return last_partial
            emitted = False

        raise AllModelsUnavailableError(
            f"All compatible free models failed while streaming "
            f"({len(attempts)} attempts): {self._summarise(attempts)}",
            attempts,
        )


# Module-level singleton used by the agents.
_default_client = LLMClient()


def get_llm_client() -> LLMClient:
    return _default_client


async def close_llm_client():
    await _default_client.aclose()
