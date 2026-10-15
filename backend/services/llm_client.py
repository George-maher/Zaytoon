"""The one LLM entry point: FREE-only for the cloud path, contract-validated.

Every agent goes through :class:`LLMClient`. Which transport answers is decided
once by ``AI_PROVIDER``: **Ollama by default** (local, no key), OpenRouter as
the optional cloud path. Both share the same attempt budget, the same
``AttemptRecord`` log line and the same completion contract, so a model that
behaves differently per transport is visible rather than structural.

The OpenRouter path is what the free-only policy governs: its requests go to
``https://openrouter.ai/api/v1/chat/completions`` and the free tier is the whole
cost model there. **There is no fallback between providers** — a local install
with a stopped daemon must never start making cloud calls, and a rate-limited
cloud call must never start running weights on the user's machine. Degradation is
a reported failure, not a silent switch.

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
import ipaddress
import itertools
import json
import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Sequence
from urllib.parse import urlsplit

import httpx

from common.tracing import get_trace_id
from config import (
    PROVIDER_OLLAMA,
    PROVIDER_OPENROUTER,
    TIER_FALLBACK,
    TIER_FAST,
    TIER_REASONING,
    NoCapableModelError,
    NonFreeModelError,
    config,
    is_allowed_model,
)
from core.events import CoreEventType, bus
from services.completion_validator import (
    CHAT_CONTRACT,
    STREAM_CONTRACT,
    CompletionContract,
    validate_completion,
)
from services.model_registry import Selection, select_models
from contextlib import asynccontextmanager

from common.concurrency import BoundedGate, QueueTimeout

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


class AllModelsUnavailableError(LLMError):
    """Every compatible model failed. The user gets a controlled message.

    Never a fabricated answer and never the last invalid completion.

    `kind` exists so a caller can *react* to the reason instead of pattern-
    matching the sentence. The engine used to replace whatever this exception
    said with one generic "all available free AI models are temporarily
    unavailable" notice — which told a user with a stopped `ollama serve` to
    wait a moment for a cloud rate limit that had not happened. The actionable
    text was already here; it was being thrown away one layer up.
    """

    def __init__(
        self, message: str, attempts: Optional[List["AttemptRecord"]] = None,
        kind: str = "pool_exhausted",
    ) -> None:
        super().__init__(message, attempts)
        #: provider_unreachable | model_missing | provider_unconfigured |
        #: local_model_failed | pool_exhausted | local_default_blocked
        self.kind = kind


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
    #: Cumulative latency per model. `by_model` counts calls — the pair gives
    #: "how long does *this* model take?", which is the question behind a
    #: fallback decision, and no counter here had it.
    model_latency_ms: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    #: Contract-failure counts, keyed by verdict. This is the metric that would
    #: have caught the ``openrouter/free`` problem: a model that answers every
    #: request with valid-looking garbage shows up here, not in ``success``.
    invalid: Dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def record_success(self, model: str, latency_ms: int) -> None:
        self.success += 1
        self.total_latency_ms += latency_ms
        self.by_model[model] += 1
        self.model_latency_ms[model] += latency_ms

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
            # Mean per model: the cumulative ms over the count `record_success`
            # already incremented — one pair of counters, no drift.
            "by_model_latency_ms": {
                model: (self.model_latency_ms[model] // count) if count else 0
                for model, count in self.by_model.items()
            },
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
                "AI_PROVIDER=openrouter requires OPENROUTER_API_KEY; set it in "
                "backend/.env, or switch back to the local runtime with "
                "AI_PROVIDER=ollama. Zaytoon will not fall between providers on "
                "its own."
            ) from None

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
        self._emit_model_event(record)

    def _emit_model_event(self, record: AttemptRecord) -> None:
        """Publish this attempt's outcome on the core bus as ``model.*``.

        The same diagnostic surface as the log line, and the same rule: model
        id, provider, outcome, verdict, reason and latency — never
        ``record.detail`` (it can hold a response body), never the key, never
        message content. Fire-and-forget, so a slow or absent subscriber
        cannot reach into the request path.

        Granularity, stated plainly because it is not uniform:
        ``model.started`` is emitted **per call** (one per ``complete()`` /
        ``stream()`` entry) while the outcome events are **per attempt** — a
        call that fell back produced several. ``request_id`` is the join key
        for anyone counting: all events of one call share it.
        """
        common = {
            "model": record.model,
            "provider": record.provider,
            "request_id": record.request_id,
            "attempt": record.attempt,
            "latency_ms": record.latency_ms,
            # The turn this attempt belongs to, when one is bound (the transport
            # sets it in `_run_turn`, so every model call made inside a turn sees
            # it). Without it a client can only guess which of two overlapping
            # turns an attempt served, and a badge on the wrong answer is worse
            # than no badge: it is a claim about routing that happens to be
            # false. `request_id` alone cannot do this — a request may contain
            # several attempts, and nothing maps a request id back to a turn.
            "trace_id": get_trace_id(),
        }
        if record.outcome == "ok":
            bus.publish(
                CoreEventType.MODEL_COMPLETED,
                validation=record.validation or "",
                tool_called=record.tool_called or "",
                **common,
            )
        else:
            bus.publish(
                CoreEventType.MODEL_FAILED,
                outcome=record.outcome,
                reason=record.fallback_reason or "",
                **common,
            )

    def _cooldown_for(self, outcome: str) -> float:
        """429 benches a model for longer than a transient 5xx."""
        if outcome == "rate_limited":
            return max(self.provider_cooldown, config.LLM_RATE_LIMIT_COOLDOWN)
        return self.provider_cooldown

    # ------------------------------------------------------------------
    # Registry (router-first) path — same budget, same contract, no policy
    # ------------------------------------------------------------------
    async def _route_plan(self, tier: str, required: List[str]) -> Optional[Any]:
        """Ask the registry which model it would use for this request.

        ``None`` means "not answered here" — no provider, no models, an
        unreadable kv row, a broken import, any of it. Routing must never be
        the *reason* a request fails: the configured chain below is the
        fallback by design, so every failure mode of this method collapses
        into "use the chain".
        """
        try:
            from core.providers import plan as registry_plan

            decision = await registry_plan(required=required, tier=tier)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - see the docstring
            logger.debug(
                "Registry routing unavailable (%s); using the configured chain",
                type(exc).__name__,
            )
            return None
        if not decision.model or not decision.base_url:
            logger.debug(
                "Registry routing: %s", decision.error or "no usable model"
            )
            return None
        return decision

    async def _local_default_refuses_hosted(self) -> Optional[str]:
        """Why a hosted chain must not run when the default provider is local.

        The registry default is the user's statement about where this
        install's traffic belongs. When it is *local* and could not answer,
        handing the turn to a cloud model would send data out of the machine
        the user never chose to send — a fallback that is silent by
        definition. Hosted→local and within-provider fallbacks are unchanged:
        those are visible in Settings and in the attempt log.

        Returns the reason to refuse, or ``None`` when the hosted chain may
        proceed: no registry, no enabled default, a hosted default, or an
        unreadable registry (the guard must never be the *reason* a request
        fails).
        """
        try:
            from core.providers import default_provider

            provider = await default_provider()
        except Exception:  # noqa: BLE001 - see the docstring
            return None
        if not provider or not provider.get("enabled"):
            return None
        local = provider.get("kind") == "ollama" or _looks_local(
            provider.get("base_url")
        )
        if not local:
            return None
        return (
            f"The default provider '{provider.get('name')}' is local and could "
            "not answer; not crossing to a hosted model without being asked."
        )

    def _registry_outcome(self, exc: Any) -> str:
        """Map a transport failure onto the chain's attempt vocabulary.

        The client classifies each failure once, at the failure site, and the
        ``kind`` travels on the exception. The old version guessed from
        ``str(exc)`` — matching the literal substring ``"Timeout"`` — so
        rewording a message silently re-labelled every future failure
        (``connect_error`` vs ``timeout`` decided cooldowns and metrics).
        The status/message fallbacks remain for exceptions constructed
        without a kind.
        """
        kind = getattr(exc, "kind", "") or ""
        if kind:
            return kind
        status = getattr(exc, "status", 0) or 0
        if status:
            return self._classify_status(status)
        detail = str(exc)
        if "Timeout" in detail:
            return "timeout"
        if "non-JSON" in detail or "no choices" in detail:
            return "malformed"
        return "connect_error"

    async def _complete_registry(
        self,
        decision: Any,
        messages: List[Dict[str, Any]],
        max_tokens: int,
        temperature: float,
        tools: Optional[List[Dict[str, Any]]],
        contract: CompletionContract,
        extract: Optional[Callable[[str], Any]],
        rid: str,
        required: List[str],
        budget: _AttemptBudget,
        attempts: List[AttemptRecord],
    ) -> Optional[str]:
        """One pass over the registry's usable candidates.

        Returns the validated text, or ``None`` — "not answered here" — which
        hands the request to the configured chain with whatever budget is
        left. One attempt per candidate: a transient failure's retry *is* the
        fall-through, because the chain below already does its own backoff.

        None of the three invariants are reimplemented here: the plan has
        already applied free-only policy per candidate, ``budget`` and
        ``attempts`` are the same objects the chain uses, and
        ``validate_completion`` is the same contract check the chain makes.
        """
        from core.providers import (
            OpenAICompatibleClient,
            ProviderTransportError,
            resolve_api_key,
        )

        api_key = await resolve_api_key(decision.provider_id)
        transport = OpenAICompatibleClient(
            decision.base_url, api_key=api_key,
            timeout=decision.timeout_s or self.timeout,
            org_id=decision.org_id,
        )
        try:
            for candidate in decision.usable:
                number = budget.take()
                if number is None:
                    break
                label = f"{candidate.provider_id}:{candidate.model_id}"
                started = time.monotonic()
                try:
                    content = await transport.complete(
                        messages, model=candidate.model_id,
                        max_tokens=max_tokens, temperature=temperature,
                        tools=tools,
                    )
                except asyncio.CancelledError:
                    raise
                except ProviderTransportError as exc:
                    elapsed = int((time.monotonic() - started) * 1000)
                    kind = self._registry_outcome(exc)
                    detail = str(exc)
                    record = AttemptRecord(
                        label, candidate.provider_id, candidate.model_id,
                        kind, getattr(exc, "status", 0) or 0, detail, elapsed,
                        attempt=number, request_id=rid,
                        required_capabilities=required,
                        fallback_reason=kind.upper(),
                    )
                    self._log_attempt(record)
                    attempts.append(record)
                    self.metrics.record(kind, elapsed)
                    self._health(candidate.model_id).record_failure(
                        self._cooldown_for(kind), detail
                    )
                    # Candidates are different models behind one endpoint: a
                    # 401 applies to all of them, a 404 does not — and the
                    # budget decides how many are worth discovering. Next.
                    continue

                elapsed = int((time.monotonic() - started) * 1000)
                verdict = validate_completion(content, contract, extract=extract)
                if not verdict.ok:
                    record = AttemptRecord(
                        label, candidate.provider_id, candidate.model_id,
                        OUTCOME_INVALID, 200, verdict.detail, elapsed,
                        attempt=number, request_id=rid,
                        validation=verdict.verdict,
                        required_capabilities=required,
                        tool_called=(
                            getattr(verdict.tool_calls[0], "name", "")
                            if verdict.tool_calls else ""
                        ),
                        fallback_reason=verdict.verdict,
                    )
                    self._log_attempt(record)
                    attempts.append(record)
                    self.metrics.record_invalid(verdict.verdict)
                    self._health(candidate.model_id).record_failure(
                        self.provider_cooldown, verdict.verdict
                    )
                    continue

                self._health(candidate.model_id).record_success()
                self.metrics.record_success(candidate.model_id, elapsed)
                record = AttemptRecord(
                    label, candidate.provider_id, candidate.model_id,
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
                logger.info(
                    "LLM REGISTRY OK  model=%s latency=%dms",
                    candidate.model_id, elapsed,
                )
                return content
            return None
        finally:
            await transport.aclose()

    async def _stream_registry(
        self,
        decision: Any,
        messages: List[Dict[str, Any]],
        on_delta: Optional[Callable[[str], Awaitable[None]]],
        max_tokens: int,
        temperature: float,
        tools: Optional[List[Dict[str, Any]]],
        contract: CompletionContract,
        extract: Optional[Callable[[str], Any]],
        rid: str,
        required: List[str],
        budget: _AttemptBudget,
        attempts: List[AttemptRecord],
        tier: str,
    ) -> Optional[str]:
        """Stream from the registry's candidates. ``None`` = nothing was
        emitted, so the chain may stream from scratch.

        The chain's fallback semantics are mirrored, because the reason they
        exist does not change with the transport: a failure *after* the first
        delta must not re-emit, so it recovers through a blocking call whose
        result replaces the bubble (or, if that fails too, the partial — those
        tokens are already on screen); a failure *before* any delta falls
        through and the chain streams freely. Either way at most one stream
        reaches the user.
        """
        from core.providers import (
            OpenAICompatibleClient,
            ProviderTransportError,
            resolve_api_key,
        )

        api_key = await resolve_api_key(decision.provider_id)
        transport = OpenAICompatibleClient(
            decision.base_url, api_key=api_key,
            timeout=decision.timeout_s or self.timeout,
            org_id=decision.org_id,
        )
        last_partial = ""
        try:
            for candidate in decision.usable:
                number = budget.take()
                if number is None:
                    break
                label = f"{candidate.provider_id}:{candidate.model_id}"
                started = time.monotonic()
                full = ""
                got_delta = False
                try:
                    sink: List[str] = []
                    async for delta in transport.stream(
                        messages, model=candidate.model_id,
                        max_tokens=max_tokens, temperature=temperature,
                        tools=tools, tool_calls_out=sink,
                    ):
                        full += delta
                        got_delta = True
                        last_partial = full
                        if on_delta:
                            await on_delta(delta)

                    # Native tool calls travelled off-wire (see stream()'s
                    # tool_calls_out): they join `full` for validation as the
                    # JSON-in-text dialect, but never reached `on_delta` —
                    # tool calls are not prose.
                    if sink:
                        full = full + ("\n" if full else "") + "\n".join(sink)

                    if not full:
                        raise LLMResponseError("stream completed with no content")

                    verdict = validate_completion(full, contract, extract=extract)
                    if verdict.ok:
                        elapsed = int((time.monotonic() - started) * 1000)
                        self._health(candidate.model_id).record_success()
                        self.metrics.record_success(candidate.model_id, elapsed)
                        record = AttemptRecord(
                            label, candidate.provider_id, candidate.model_id,
                            "ok", 200, latency_ms=elapsed,
                            attempt=number, request_id=rid,
                            validation=verdict.verdict,
                            required_capabilities=required,
                        )
                        attempts.append(record)
                        self._emit_model_event(record)
                        logger.info(
                            "LLM REGISTRY STREAM OK  model=%s chars=%d latency=%dms",
                            candidate.model_id, len(full), elapsed,
                        )
                        return full
                    record = AttemptRecord(
                        label, candidate.provider_id, candidate.model_id,
                        OUTCOME_INVALID, 200, verdict.detail,
                        int((time.monotonic() - started) * 1000),
                        attempt=number, request_id=rid,
                        validation=verdict.verdict,
                        required_capabilities=required,
                        fallback_reason=verdict.verdict,
                    )
                    attempts.append(record)
                    self._emit_model_event(record)
                    self.metrics.record_invalid(verdict.verdict)
                    self._health(candidate.model_id).record_failure(
                        self.provider_cooldown, verdict.verdict
                    )
                except asyncio.CancelledError:
                    raise
                except ProviderTransportError as exc:
                    elapsed = int((time.monotonic() - started) * 1000)
                    kind = self._registry_outcome(exc)
                    record = AttemptRecord(
                        label, candidate.provider_id, candidate.model_id,
                        kind, getattr(exc, "status", 0) or 0, str(exc), elapsed,
                        attempt=number, request_id=rid,
                        required_capabilities=required,
                        fallback_reason=kind.upper(),
                    )
                    attempts.append(record)
                    self._emit_model_event(record)
                    self.metrics.record(kind, elapsed)
                    self._health(candidate.model_id).record_failure(
                        self._cooldown_for(kind), str(exc)
                    )
                except LLMResponseError as exc:
                    record = AttemptRecord(
                        label, candidate.provider_id, candidate.model_id,
                        "malformed", 200, str(exc),
                        int((time.monotonic() - started) * 1000),
                        attempt=number, request_id=rid,
                    )
                    attempts.append(record)
                    self._emit_model_event(record)
                    self.metrics.record("malformed")
                    self._health(candidate.model_id).record_failure(
                        self.provider_cooldown, str(exc)
                    )

                # This candidate is done. Tokens on screen mean a second
                # stream would duplicate what the user already sees — the
                # blocking recovery replaces the bubble instead.
                if got_delta:
                    logger.info(
                        "LLM REGISTRY STREAM broke after first token on %s; "
                        "completing via blocking call", candidate.model_id,
                    )
                    try:
                        return await self.complete(
                            messages, tier=tier, max_tokens=max_tokens,
                            temperature=temperature, tools=tools,
                            contract=contract, extract=extract,
                            request_id=rid,
                        )
                    except LLMError:
                        logger.warning(
                            "recovery completion failed; returning %d chars of "
                            "partial stream from %s", len(last_partial),
                            candidate.model_id,
                        )
                        return last_partial
                # Nothing emitted: the next candidate may still stream.
            return None
        finally:
            await transport.aclose()

    # ------------------------------------------------------------------
    # Ollama (local) path — same contract, same budget, no cloud
    # ------------------------------------------------------------------
    @staticmethod
    def _using_ollama() -> bool:
        return config.PROVIDER == PROVIDER_OLLAMA

    def _ollama(self):
        """The shared Ollama HTTP client for the configured endpoint."""
        from services.ollama_client import get_ollama_client

        return get_ollama_client(config.OLLAMA_BASE_URL, config.OLLAMA_TIMEOUT)

    @staticmethod
    def _ollama_options(max_tokens: int, temperature: float) -> Dict[str, Any]:
        return {"num_predict": max_tokens, "temperature": temperature}

    async def _complete_ollama(
        self,
        messages: List[Dict[str, Any]],
        tier: str,
        max_tokens: int,
        temperature: float,
        tools: Optional[List[Dict[str, Any]]],
        retries: Optional[int],
        contract: CompletionContract,
        extract: Optional[Callable[[str], Any]],
        rid: str,
        budget: Optional[_AttemptBudget] = None,
        attempts: Optional[List[AttemptRecord]] = None,
    ) -> str:
        """Non-streaming local completion under the same contract + budget.

        ``budget`` and ``attempts`` arrive shared when a registry pass ran
        first, so an exhausted ceiling is reported as one exhausted request
        rather than two permissive ones.
        """
        # ``tier`` may arrive as ``None`` (the engine has no tier to offer when
        # its LLM client carries none), and the OpenRouter ``build_chain``
        # normalises it the same way — defaulting to FAST rather than quietly
        # escalating a tool turn to the big general model on CPU.
        tier = tier if tier in (TIER_FAST, TIER_REASONING, TIER_FALLBACK) else TIER_FAST
        if attempts is None:
            attempts = []
        required = list(contract.required_capabilities)
        model = config.ollama_model_for_tier(tier)
        label = f"ollama:{model}"
        client = self._ollama()
        if budget is None:
            budget = _AttemptBudget(self.max_attempts)
        retries_allowed = max(1, self.max_retries if retries is None else retries)
        reachable = False

        for _ in range(retries_allowed):
            number = budget.take()
            if number is None:
                break
            started = time.monotonic()
            try:
                content = await client.chat(
                    messages, model=model, stream=False, tools=tools,
                    options=self._ollama_options(max_tokens, temperature),
                )
                reachable = True
            except asyncio.CancelledError:
                raise
            except httpx.HTTPStatusError as e:
                elapsed = int((time.monotonic() - started) * 1000)
                status = e.response.status_code
                try:
                    body = (e.response.text or "")[:400]
                except Exception:  # pragma: no cover - defensive
                    body = ""
                if status == 404 or "not found" in body.lower():
                    # Never auto-download: tell the operator the exact command.
                    raise AllModelsUnavailableError(
                        f"MODEL NOT INSTALLED: {model!r} is not available at "
                        f"{config.OLLAMA_BASE_URL}. Install it with "
                        f"`ollama pull {model}` and try again "
                        "(Zaytoon never downloads models on its own).",
                        attempts,
                        kind="model_missing",
                    ) from e
                record = AttemptRecord(
                    label, "ollama", model, self._classify_status(status),
                    status, body, elapsed, attempt=number, request_id=rid,
                    required_capabilities=required,
                    fallback_reason=f"HTTP_{status}",
                )
                self._log_attempt(record)
                attempts.append(record)
                self.metrics.record(record.outcome, elapsed)
                self._health(model).record_failure(
                    self._cooldown_for(record.outcome), body
                )
                break
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                elapsed = int((time.monotonic() - started) * 1000)
                detail = (
                    f"Ollama is not reachable at {config.OLLAMA_BASE_URL}: {e}. "
                    "Start it with `ollama serve`."
                )
                record = AttemptRecord(
                    label, "ollama", model, "connect_error", None, detail,
                    elapsed, attempt=number, request_id=rid,
                    required_capabilities=required,
                    fallback_reason="CONNECT_ERROR",
                )
                self._log_attempt(record)
                attempts.append(record)
                self.metrics.record("connect_error", elapsed)
                self._health(model).record_failure(self.provider_cooldown, detail)
                break
            except httpx.TimeoutException:
                elapsed = int((time.monotonic() - started) * 1000)
                record = AttemptRecord(
                    label, "ollama", model, "timeout", None, "timeout", elapsed,
                    attempt=number, request_id=rid,
                    required_capabilities=required, fallback_reason="TIMEOUT",
                )
                self._log_attempt(record)
                attempts.append(record)
                self.metrics.record("timeout", elapsed)
                self._health(model).record_failure(self.provider_cooldown, "timeout")
                break
            except Exception as e:
                elapsed = int((time.monotonic() - started) * 1000)
                record = AttemptRecord(
                    label, "ollama", model, "error", None, str(e), elapsed,
                    attempt=number, request_id=rid,
                    required_capabilities=required, fallback_reason="ERROR",
                )
                self._log_attempt(record)
                attempts.append(record)
                self.metrics.record("error", elapsed)
                self._health(model).record_failure(self.provider_cooldown, str(e))
                break

            elapsed = int((time.monotonic() - started) * 1000)
            verdict = validate_completion(content, contract, extract=extract)
            if not verdict.ok:
                record = AttemptRecord(
                    label, "ollama", model, OUTCOME_INVALID, 200, verdict.detail,
                    elapsed, attempt=number, request_id=rid,
                    validation=verdict.verdict,
                    required_capabilities=required,
                    tool_called=(
                        getattr(verdict.tool_calls[0], "name", "")
                        if verdict.tool_calls else ""
                    ),
                    fallback_reason=verdict.verdict,
                )
                self._log_attempt(record)
                attempts.append(record)
                self.metrics.record_invalid(verdict.verdict)
                self._health(model).record_failure(
                    self.provider_cooldown, verdict.verdict
                )
                break

            self._health(model).record_success()
            self.metrics.record_success(model, elapsed)
            record = AttemptRecord(
                label, "ollama", model, "ok", 200, latency_ms=elapsed,
                attempt=number, request_id=rid, validation=verdict.verdict,
                required_capabilities=required,
                tool_called=(
                    getattr(verdict.tool_calls[0], "name", "")
                    if verdict.tool_calls else ""
                ),
            )
            self._log_attempt(record)
            attempts.append(record)
            return content

        if not reachable and attempts and attempts[0].outcome == "connect_error":
            raise AllModelsUnavailableError(
                attempts[0].detail, attempts, kind="provider_unreachable"
            )
        raise AllModelsUnavailableError(
            f"Local model {model!r} failed "
            f"({len(attempts)} attempts): {self._summarise(attempts)}",
            attempts,
            kind="local_model_failed",
        )

    async def _stream_ollama(
        self,
        messages: List[Dict[str, Any]],
        on_delta: Optional[Callable[[str], Awaitable[None]]],
        tier: str,
        max_tokens: int,
        temperature: float,
        tools: Optional[List[Dict[str, Any]]],
        retries: Optional[int],
        contract: CompletionContract,
        extract: Optional[Callable[[str], Any]],
        rid: str,
        budget: Optional[_AttemptBudget] = None,
        attempts: Optional[List[AttemptRecord]] = None,
    ) -> str:
        """Streaming local completion; identical semantics to the SSE path.

        ``budget`` and ``attempts`` arrive shared when a registry pass ran
        first — see :meth:`_complete_ollama`."""
        tier = tier if tier in (TIER_FAST, TIER_REASONING, TIER_FALLBACK) else TIER_FAST
        if attempts is None:
            attempts = []
        required = list(contract.required_capabilities)
        model = config.ollama_model_for_tier(tier)
        label = f"ollama:{model}"
        client = self._ollama()
        if budget is None:
            budget = _AttemptBudget(self.max_attempts)
        attempts_allowed = max(1, self.max_retries if retries is None else retries)
        emitted = False
        last_partial = ""
        reachable = False

        for _ in range(attempts_allowed):
            number = budget.take()
            if number is None:
                break
            full = ""
            started = time.monotonic()
            try:
                stream = await client.chat(
                    messages, model=model, stream=True, tools=tools,
                    options=self._ollama_options(max_tokens, temperature),
                )
                async for delta in stream:
                    full += delta
                    emitted = True
                    last_partial = full
                    if on_delta:
                        await on_delta(delta)
                reachable = True
            except asyncio.CancelledError:
                raise
            except Exception as e:
                elapsed = int((time.monotonic() - started) * 1000)
                kind = "error"
                detail = str(e)
                if isinstance(e, httpx.HTTPStatusError):
                    status = e.response.status_code
                    if status == 404:
                        raise AllModelsUnavailableError(
                            f"MODEL NOT INSTALLED: {model!r} is not available at "
                            f"{config.OLLAMA_BASE_URL}. Install it with "
                            f"`ollama pull {model}` and try again "
                            "(Zaytoon never downloads models on its own).",
                            attempts,
                        ) from e
                    kind = self._classify_status(status)
                    detail = self._human_error(status, detail)
                elif isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)):
                    kind = "connect_error"
                    detail = (
                        f"Ollama is not reachable at {config.OLLAMA_BASE_URL}: {e}. "
                        "Start it with `ollama serve`."
                    )
                elif isinstance(e, httpx.TimeoutException):
                    kind = "timeout"
                    detail = "timeout"
                record = AttemptRecord(
                    label, "ollama", model, kind, None, detail, elapsed,
                    attempt=number, request_id=rid,
                    required_capabilities=required, fallback_reason=kind.upper(),
                )
                self._log_attempt(record)
                attempts.append(record)
                self.metrics.record(kind, elapsed)
                self._health(model).record_failure(
                    self._cooldown_for(kind), detail
                )
                if emitted:
                    # Text is already on screen: never stream a second copy.
                    logger.warning(
                        "LLM STREAM BROKE after first token on %s; "
                        "recovering with a blocking call", model,
                    )
                    try:
                        return await self.complete(
                            messages, tier=tier, max_tokens=max_tokens,
                            temperature=temperature, tools=tools,
                            contract=contract, extract=extract, request_id=rid,
                        )
                    except LLMError:
                        return last_partial
                break

            if not full:
                elapsed = int((time.monotonic() - started) * 1000)
                attempts.append(
                    AttemptRecord(
                        label, "ollama", model, "malformed", 200,
                        "stream completed with no content", elapsed,
                        attempt=number, request_id=rid,
                        required_capabilities=required,
                    )
                )
                self.metrics.record("malformed", elapsed)
                self._health(model).record_failure(
                    self.provider_cooldown, "empty_stream"
                )
                continue

            elapsed = int((time.monotonic() - started) * 1000)
            verdict = validate_completion(full, contract, extract=extract)
            if not verdict.ok:
                record = AttemptRecord(
                    label, "ollama", model, OUTCOME_INVALID, 200, verdict.detail,
                    elapsed, attempt=number, request_id=rid,
                    validation=verdict.verdict, required_capabilities=required,
                    fallback_reason=verdict.verdict,
                )
                self._log_attempt(record)
                attempts.append(record)
                self.metrics.record_invalid(verdict.verdict)
                self._health(model).record_failure(
                    self.provider_cooldown, verdict.verdict
                )
                if emitted:
                    # The streamed bubble is already on screen; a blocking
                    # recovery replaces it via the WebSocket `message` frame.
                    try:
                        return await self.complete(
                            messages, tier=tier, max_tokens=max_tokens,
                            temperature=temperature, tools=tools,
                            contract=contract, extract=extract, request_id=rid,
                        )
                    except LLMError:
                        return last_partial
                break

            self._health(model).record_success()
            self.metrics.record_success(model, elapsed)
            attempts.append(
                AttemptRecord(
                    label, "ollama", model, "ok", 200, latency_ms=elapsed,
                    attempt=number, request_id=rid, validation=verdict.verdict,
                    required_capabilities=required,
                )
            )
            return full

        if not reachable and attempts and attempts[0].outcome == "connect_error":
            raise AllModelsUnavailableError(
                attempts[0].detail, attempts, kind="provider_unreachable"
            )
        raise AllModelsUnavailableError(
            f"Local model {model!r} failed while streaming "
            f"({len(attempts)} attempts): {self._summarise(attempts)}",
            attempts,
            kind="local_model_failed",
        )

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

        Holds one slot of the process-wide gate while it runs — see
        :func:`llm_gate` for why the bound lives here rather than in the
        transport, and why a full gate *queues* before it ever refuses.
        """
        async with _gated():
            return await self._complete(
                messages, tier=tier, max_tokens=max_tokens,
                temperature=temperature, tools=tools, retries=retries,
                contract=contract, extract=extract, request_id=request_id,
            )

    async def _complete(
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
        # The trace id, when one is bound to this request, correlates every
        # attempt of this turn in the `LLM_ATTEMPT` log line. An explicit
        # `request_id` still wins; a random id is the last resort, so the
        # field is never empty.
        rid = request_id or get_trace_id() or uuid.uuid4().hex[:12]
        # `started` marks the *call*; the outcome events it produces are per
        # attempt — join them on request_id (see `_emit_model_event`).
        bus.publish(
            CoreEventType.MODEL_STARTED,
            request_id=rid, tier=tier, provider=config.PROVIDER,
            # Same join key as the outcome events below, so a client can tell
            # "an attempt is starting for the turn I am showing" from "for the
            # turn I just replaced".
            trace_id=get_trace_id(),
        )
        required = list(contract.required_capabilities)
        # One budget for the whole request. The registry path and the
        # configured chain draw from the same counter, so router-first cannot
        # multiply the attempt ceiling — the invariant `_AttemptBudget`
        # exists to protect.
        budget = _AttemptBudget(self.max_attempts)

        # Router-first: the registry decides when it can answer. Every
        # registry failure falls through to the configured chain below, which
        # keeps its free-only policy, retries and contract validation exactly
        # as they were — this path only ever adds a candidate set in front.
        routed = await self._route_plan(tier, required)
        if routed is not None:
            content = await self._complete_registry(
                routed, messages, max_tokens, temperature, tools,
                contract, extract, rid, required, budget, attempts,
            )
            if content is not None:
                return content

        if self._using_ollama():
            return await self._complete_ollama(
                messages, tier, max_tokens, temperature, tools,
                retries, contract, extract, rid,
                budget=budget, attempts=attempts,
            )
        # The local→hosted guard, before the chain is even built.
        blocked = await self._local_default_refuses_hosted()
        if blocked:
            raise AllModelsUnavailableError(
                blocked, attempts, kind="local_default_blocked"
            )
        chain = self.build_chain(tier, required)
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
            kind="pool_exhausted",
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

        The gate is held for the whole stream, which is the point: a generation
        is the expensive resource, not the first byte. The blocking recovery
        below re-enters the same slot rather than asking for a second one.
        """
        async with _gated():
            return await self._stream(
                messages, on_delta=on_delta, tier=tier, max_tokens=max_tokens,
                temperature=temperature, tools=tools, retries=retries,
                contract=contract, extract=extract, request_id=request_id,
            )

    async def _stream(
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
        # The trace id, when one is bound to this request, correlates every
        # attempt of this turn in the `LLM_ATTEMPT` log line. An explicit
        # `request_id` still wins; a random id is the last resort, so the
        # field is never empty.
        rid = request_id or get_trace_id() or uuid.uuid4().hex[:12]
        # `started` marks the *call*; the outcome events it produces are per
        # attempt — join them on request_id (see `_emit_model_event`).
        bus.publish(
            CoreEventType.MODEL_STARTED,
            request_id=rid, tier=tier, provider=config.PROVIDER,
            trace_id=get_trace_id(),
        )
        required = list(contract.required_capabilities)
        # Shared across the registry pass and the chain, as in `complete()`:
        # router-first must not multiply the attempt ceiling.
        budget = _AttemptBudget(self.max_attempts)

        # Router-first, mirroring `complete()`. A `None` return guarantees
        # nothing was emitted, so the stream below cannot duplicate what the
        # user has already seen; a `str` is the authoritative full text.
        routed = await self._route_plan(tier, required)
        if routed is not None:
            streamed = await self._stream_registry(
                routed, messages, on_delta, max_tokens, temperature, tools,
                contract, extract, rid, required, budget, attempts, tier,
            )
            if streamed is not None:
                return streamed

        if self._using_ollama():
            return await self._stream_ollama(
                messages, on_delta, tier, max_tokens, temperature, tools,
                retries, contract, extract, rid,
                budget=budget, attempts=attempts,
            )
        # The local→hosted guard, before the chain is even built.
        blocked = await self._local_default_refuses_hosted()
        if blocked:
            raise AllModelsUnavailableError(
                blocked, attempts, kind="local_default_blocked"
            )
        chain = self.build_chain(tier, required)
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
                    record = AttemptRecord(
                        endpoint.label, endpoint.provider,
                        endpoint.model, "ok", 200,
                        latency_ms=elapsed,
                        attempt=number, request_id=rid,
                        validation=verdict.verdict,
                        required_capabilities=required,
                    )
                    attempts.append(record)
                    self._emit_model_event(record)
                    logger.info(
                        "LLM STREAM OK  model=%s chars=%d latency=%dms",
                        endpoint.model, len(full), elapsed,
                    )
                    return full

                except LLMResponseError as e:
                    record = AttemptRecord(
                        endpoint.label, endpoint.provider,
                        endpoint.model, "malformed", 200, str(e),
                        int((time.monotonic() - started) * 1000),
                        attempt=number, request_id=rid,
                    )
                    attempts.append(record)
                    self._emit_model_event(record)
                    self.metrics.record("malformed")
                    logger.error("LLM STREAM MALFORMED  model=%s: %s", endpoint.model, e)
                    self._health(endpoint.model).record_failure(
                        self.provider_cooldown, str(e)
                    )
                    break
                except (httpx.TimeoutException, httpx.ConnectError) as e:
                    elapsed = int((time.monotonic() - started) * 1000)
                    record = AttemptRecord(
                        endpoint.label, endpoint.provider,
                        endpoint.model, "stream_error", None,
                        str(e), elapsed,
                        attempt=number, request_id=rid,
                    )
                    attempts.append(record)
                    self._emit_model_event(record)
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
                    record = AttemptRecord(
                        endpoint.label, endpoint.provider,
                        endpoint.model, "stream_error", None,
                        str(e), int((time.monotonic() - started) * 1000),
                        attempt=number, request_id=rid,
                    )
                    attempts.append(record)
                    self._emit_model_event(record)
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
            kind="pool_exhausted",
        )


#: What to tell the user, per failure kind. The provider's own detail is
#: appended when it exists — it already names the command to run, and a
#: paraphrase would be worse than the quotation.
_USER_NOTICES: Dict[str, Dict[str, str]] = {
    "provider_unreachable": {
        "en": "The local model provider is not running.",
        "ar": "الموديل المحلي مش شغال.",
    },
    "model_missing": {
        "en": "The model this request needs is not installed locally.",
        "ar": "الموديل المطلوب مش متسطب على الجهاز.",
    },
    "local_model_failed": {
        "en": "The local model could not complete this request.",
        "ar": "الموديل المحلي مقدرش يكمّل الطلب ده.",
    },
    "provider_unconfigured": {
        "en": "No AI provider is configured.",
        "ar": "مفيش مزوّد ذكاء اصطناعي متفعّل.",
    },
    "llm_queue_timeout": {
        "en": "The model is still working on other requests and freed up too late.",
        "ar": "الموديل لسه بيشتغل على طلبات تانية ومفيش غير متأخر.",
    },
    "local_default_blocked": {
        "en": (
            "Your default AI provider is local and it cannot answer right now. "
            "Zaytoon did not switch to a hosted model on its own — start the "
            "local provider, or choose a hosted one in Settings."
        ),
        "ar": (
            "مزوّد الذكاء الاصطناعي الافتراضي محلي ومش قادير يرد دلوقتي. "
            "زيتون مبدّلش لموديل أونلاين لوحده — شغّل المزوّد المحلي، "
            "أو اختار مزوّد أونلاين من الإعدادات."
        ),
    },
}


def _looks_local(base_url: Optional[str]) -> bool:
    """Does this endpoint address name *this machine* (or the LAN)?

    An ``ollama`` kind is local by construction; a custom OpenAI-compatible
    provider is local when its host is an address class that cannot leave the
    machine (loopback or private). A hostname that would need DNS
    (``my-nas.local``) is treated as hosted — save-time resolution would be
    slow and re-checkable after the fact, so the honest answer is the address
    class we can actually see.
    """
    host = (urlsplit(base_url or "").hostname or "").lower().rstrip(".")
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(address.is_loopback or address.is_private)


def user_notice(exc: Optional[BaseException], language: str = "en") -> Optional[str]:
    """A bilingual, actionable sentence for a provider failure — or ``None``.

    ``None`` means "nothing better to say than the caller's own generic notice",
    which keeps the pool-exhausted path (every free model rate-limited or
    answering invalid) exactly as it was worded. The point of this function is
    narrower: a stopped daemon, a missing model and a missing key are each
    fixable by the user in one command, and telling them to "try again in a
    moment" for those is not a diagnosis, it is a deferral.
    """
    kind = getattr(exc, "kind", None)
    if kind not in _USER_NOTICES or kind == "pool_exhausted":
        return None
    text = _USER_NOTICES[kind].get(language) or _USER_NOTICES[kind]["en"]
    detail = str(exc or "").strip()
    if detail:
        # The provider detail is ours, not upstream's body: every raise site
        # composes it from config plus the exception type. No credential can be
        # in it, and `redact` has already run on the way to the log.
        text = f"{text} {detail}"
    return text


#: How many model calls may run at once, process-wide.
#:
#: A local runtime is the reason this exists. Ollama on one GPU serves one or two
#: generations well and six badly: the requests do not queue politely, they
#: contend for the same memory, and the usual outcome is that *every* session
#: gets a slow or failed answer — or the daemon is OOM-killed and the assistant
#: goes dark entirely. Nothing else in the stack bounded this: the only other
#: semaphore is the per-turn tool parallelism cap, which is scoped to one turn
#: and says nothing about how many turns the process is running at once.
#:
#: Queued, not failed: a second question in the same second is normal use, and
#: answering it with "the model is busy" would be a self-inflicted outage. Only a
#: request that waits longer than ``LLM_QUEUE_TIMEOUT_S`` gives up, and it gives up
#: with a sentence the user can act on.
_llm_gate = BoundedGate(
    limit=lambda: config.LLM_MAX_CONCURRENCY,
    queue_timeout=lambda: config.LLM_QUEUE_TIMEOUT_S,
)


def llm_gate() -> BoundedGate:
    """The process-wide gate. Exposed for ``/health`` and for tests."""
    return _llm_gate


@asynccontextmanager
async def _gated() -> "AsyncIterator[None]":
    """Hold one gate slot, translating a full queue into an honest failure."""
    try:
        async with _llm_gate:
            yield
    except QueueTimeout as exc:
        raise AllModelsUnavailableError(
            f"every model slot was busy: {exc}", kind="llm_queue_timeout"
        ) from exc


# Module-level singleton used by the agents.
_default_client = LLMClient()


def get_llm_client() -> LLMClient:
    return _default_client


async def close_llm_client():
    await _default_client.aclose()
