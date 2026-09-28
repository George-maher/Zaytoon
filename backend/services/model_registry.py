"""Capability registry for the free-model pool.

This module is deliberately **pure data plus pure selection**. It imports
nothing from the rest of the project, so both ``config`` and ``llm_client`` can
depend on it without a cycle, and so a test can reason about model selection
without touching the network or the environment.

The problem it solves
---------------------

The application used to build a two-link chain: the configured primary, then
``openrouter/free``. That second link is an *uncontrolled* auto-router. When the
primary was rate-limited — which, on the free tier, is most of the time —
OpenRouter would hand back whatever free model happened to be available. For a
`get_time` request that model returned the literal string::

    User Safety: safe

which the agent loop had no reason to reject: it was a structurally valid HTTP
200 with a non-empty string in `content`. So a successful *transport* was being
treated as a successful *answer*.

Two separate policies fix that, and they are deliberately not merged:

**Cost.** A model must be in a guaranteed zero-cost form (``<vendor>/<model>:free``).
This is non-negotiable and is enforced again on the request path.

**Fitness.** A model must be *known* and must declare the capabilities the
request needs. This is a curated list, because "free" is not the same as "can
be trusted to emit a valid tool call". A small model that reliably follows the
tool protocol is worth more here than a large one that is rate-limited 90% of
the time or answers a tool request with a refusal.

``supports_tools`` is therefore an explicit, per-model claim — not an inference
from the id — and it is only ever set to ``True`` for a model observed to
produce a well-formed tool call in this application.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple

#: OpenRouter's free-only auto-router. It is recognised so it can be *refused*
#: with a clear message, never so it can be called.
FREE_ROUTER_ENDPOINT = "openrouter/free"

#: Suffix OpenRouter appends to zero-cost variants.
FREE_MODEL_SUFFIX = ":free"


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------
#: The capabilities a request can demand of a model. Kept as a small enum-like
#: frozenset of strings so it survives logging and JSON without ceremony.
CAP_TOOLS = "tools"
CAP_STRUCTURED_OUTPUT = "structured_output"
CAP_STREAMING = "streaming"

ALL_CAPABILITIES = (CAP_TOOLS, CAP_STRUCTURED_OUTPUT, CAP_STREAMING)


@dataclass(frozen=True)
class ModelSpec:
    """What we know about one model. Claims are evidence, not guesses."""

    id: str
    #: Always true. Present so the field can be asserted on and logged, which
    #: makes the zero-cost policy visible in /health rather than implied.
    free: bool = True
    #: Emits a well-formed tool call when the prompt asks for one. Verified by
    #: driving this application's real agent loop, not by reading a model card.
    supports_tools: bool = False
    #: Reliably emits parseable JSON when asked. Related to, but weaker than,
    #: ``supports_tools``.
    supports_structured_output: bool = False
    #: Verified to stream usable SSE deltas.
    supports_streaming: bool = True
    #: How this model earned its place in the pool.
    notes: str = ""
    #: Where it sits in the preference order. Lower is tried first, and this is
    #: *evidence-based only*: observed tool-call validity, then observed
    #: availability. It is never a proxy for parameter count.
    rank: int = 50
    #: When observed to be persistently rate-limited, it is benched behind the
    #: others rather than removed. A model that is merely busy is still better
    #: than no model.
    deprioritise: bool = False

    @property
    def capabilities(self) -> FrozenSet[str]:
        caps = {CAP_STREAMING} if self.supports_streaming else set()
        if self.supports_tools:
            caps.add(CAP_TOOLS)
        if self.supports_structured_output:
            caps.add(CAP_STRUCTURED_OUTPUT)
        return frozenset(caps)

    def supports(self, required: Iterable[str]) -> bool:
        """Whether this model satisfies every requested capability."""
        return set(required).issubset(self.capabilities)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "free": self.free,
            "capabilities": sorted(self.capabilities),
            "rank": self.rank,
            "deprioritise": self.deprioritise,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# The pool
# ---------------------------------------------------------------------------
#: Every model the application is allowed to call.
#:
#: **These ids were verified against OpenRouter's live catalogue, not
#: remembered.** An earlier draft of this file was written from memory and four
#: of its six ids returned HTTP 404 at request time, which is exactly how a
#: fallback chain becomes a liability: it fails in a way that looks like an
#: outage. `scripts/check_free_models.py` re-checks availability, and
#: `scripts/verify_tool_capability.py` re-checks the tool claim. Run both after
#: editing this table, because OpenRouter's free roster rotates.
#:
#: `supports_tools` means *observed to emit a well-formed tool call in this
#: application*, not "the endpoint accepts a `tools` parameter". Those are
#: different claims: OpenRouter lists `tools` under `supported_parameters` for
#: models that will happily answer a tool request with prose instead.
#:
#: `rank` records observed behaviour, not parameter count.
MODEL_REGISTRY: Dict[str, ModelSpec] = {
    spec.id: spec
    for spec in (
        ModelSpec(
            id="qwen/qwen3.8-27b:free",
            supports_tools=True,
            supports_structured_output=True,
            notes=(
                "Head. Verified end to end: observed emitting all three "
                "tool-call dialects (JSON, <tool_call>, "
                "<|tool_call_start|>) in one session. Heavily contended — "
                "usually 429, which is why the pool below exists."
            ),
            rank=10,
        ),
        ModelSpec(
            id="nvidia/nemotron-3-super-120b-a12b:free",
            supports_tools=True,
            supports_structured_output=True,
            notes=(
                "Verified 2026-09-28: clean JSON tool call. Different vendor "
                "family from the head, so a Qwen-side rate limit does not take "
                "it down with it. 262k context."
            ),
            rank=20,
        ),
        ModelSpec(
            id="nvidia/nemotron-3.5-lightning:free",
            supports_tools=True,
            supports_structured_output=True,
            notes="Verified 2026-09-28: clean JSON tool call. 1M context.",
            rank=30,
        ),
        ModelSpec(
            id="cohere/north-mini-code:free",
            supports_tools=True,
            supports_structured_output=True,
            notes=(
                "Verified 2026-09-28: clean JSON tool call. Code-tuned, so "
                "preferred for the reasoning/code tier."
            ),
            rank=40,
        ),
        ModelSpec(
            id="poolside/laguna-s-2.1:free",
            supports_tools=True,
            supports_structured_output=False,
            notes=(
                "Verified 2026-09-28, but via the pseudo-XML dialect "
                "(<tool_call>get_time</tool_call>) rather than JSON. "
                "Parsed correctly -- a live confirmation that the "
                "multi-dialect parser in agents/engine.py earns its keep."
            ),
            rank=50,
        ),
        ModelSpec(
            id="google/gemma-4-31b-it:free",
            supports_tools=False,
            supports_structured_output=False,
            notes=(
                "Available and free, but NOT claimed tool-capable: it was 429 "
                "on every probe, so the claim is unverified. Admitting an "
                "unverified model to a tool chain would manufacture exactly "
                "the failure this system exists to prevent. Re-probe before "
                "promoting it."
            ),
            rank=80,
        ),
        ModelSpec(
            id="thinkingmachines/inkling-small:free",
            supports_tools=False,
            supports_structured_output=False,
            notes=(
                "Returns HTTP 403 for non-agentic clients, re-confirmed "
                "2026-09-28. Still allow-listed because that may change, but "
                "benched last: a 403 costs a wasted round-trip."
            ),
            rank=99,
            deprioritise=True,
        ),
    )
}


#: The curated allow-list, derived from the registry so the two cannot drift.
ALLOWED_FREE_MODELS: FrozenSet[str] = frozenset(
    spec.id for spec in MODEL_REGISTRY.values() if spec.free
)


def is_free_form(model: str) -> bool:
    """True when the id is in a guaranteed zero-cost shape.

    The cost guard, deliberately independent of the allow-list: even an
    un-allow-listed model must be refused unless it is provably free, because
    being un-allow-listed is a quality problem, not a billing one.
    """
    if not model or not isinstance(model, str):
        return False
    candidate = model.strip()
    if not candidate:
        return False
    return candidate.lower().endswith(FREE_MODEL_SUFFIX)


def is_free_router(model: str) -> bool:
    """True for the uncontrolled auto-router, which is never callable."""
    return bool(model) and model.strip().lower() == FREE_ROUTER_ENDPOINT


def spec_for(model: str) -> Optional[ModelSpec]:
    """The registered spec for ``model``, or ``None`` if unknown."""
    if not model:
        return None
    return MODEL_REGISTRY.get(model.strip())


def is_allowlisted(model: str) -> bool:
    """True only for a model that is both allow-listed and in a free form."""
    if not is_free_form(model):
        return False
    return model.strip() in ALLOWED_FREE_MODELS


def missing_capabilities(model: str, required: Iterable[str]) -> List[str]:
    """Which of ``required`` the model cannot satisfy. Empty means it can."""
    spec = spec_for(model)
    if spec is None:
        return sorted(set(required))
    return sorted(set(required) - spec.capabilities)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Selection:
    """The outcome of routing, with enough context to explain it."""

    models: Tuple[str, ...] = ()
    #: Models that were dropped, and why. Surfaced in /health and logs so a
    #: "why did it pick that?" question has an answer.
    skipped: Tuple[Tuple[str, str], ...] = ()

    def __bool__(self) -> bool:
        return bool(self.models)

    def to_dict(self) -> dict:
        return {
            "models": list(self.models),
            "skipped": [{"model": m, "reason": r} for m, r in self.skipped],
        }


def select_models(
    candidates: Iterable[str],
    required: Iterable[str] = (),
    *,
    order_by_rank: bool = False,
) -> Selection:
    """Filter ``candidates`` to a capability-satisfying, **declaration-ordered** list.

    Order is the caller's, and that is the point. The brief's requirement is
    determinism and debuggability: "Qwen failed -> free model #1 -> #2", with the
    sequence written down. An earlier version of this function re-sorted by the
    registry's ``rank``, which quietly overrode an operator's configured order —
    a configured reasoning head could be pushed behind a chat model because its
    rank was higher. Configuration is intent; rank is a default, not an override.

    Set ``order_by_rank`` only when ``candidates`` came from the registry
    wholesale and there is no configured preference to preserve.

    The old chain ended in ``openrouter/free``, which made the *same request*
    resolve to a *different model* per call depending on upstream load — so a bug
    could never be reproduced and a fix could never be verified. Here the pool
    and its order are a pure function of the configuration.
    """
    required_set = set(required)
    kept: List[str] = []
    skipped: List[Tuple[str, str]] = []

    for model in candidates:
        name = (model or "").strip()
        if not name:
            continue

        if is_free_router(name):
            skipped.append((name, "uncontrolled auto-router; excluded by policy"))
            continue
        if not is_free_form(name):
            skipped.append((name, "not a free model (missing ':free')"))
            continue
        if not is_allowlisted(name):
            skipped.append((name, "not in the allow-list; add it to MODEL_REGISTRY"))
            continue

        spec = spec_for(name)
        if spec is not None and not spec.free:
            skipped.append((name, "registry marks it as not free"))
            continue

        unmet = missing_capabilities(name, required_set)
        if unmet:
            skipped.append((name, f"lacks required capabilities: {', '.join(unmet)}"))
            continue

        # Deduplicate, preserving first occurrence.
        if name not in kept:
            kept.append(name)

    if order_by_rank:
        def sort_key(name: str) -> tuple:
            spec = spec_for(name)
            if spec is None:
                return (1, 999, name)
            return (1 if spec.deprioritise else 0, spec.rank, name)

        kept = sorted(kept, key=sort_key)

    return Selection(models=tuple(kept), skipped=tuple(skipped))


def ranked_pool() -> Tuple[str, ...]:
    """Every allow-listed model, in the registry's own preference order.

    Only for callers that want the *default* ordering — for example building a
    suggested pool. Never used to override a configured sequence.
    """
    selection = select_models(
        (spec.id for spec in MODEL_REGISTRY.values()), order_by_rank=True
    )
    return selection.models
