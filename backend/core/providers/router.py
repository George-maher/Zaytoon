"""Capability routing: which model answers this request.

```text
Simple request   → fast model
Coding           → coding-capable model
Complex planning → reasoning model
Vision           → vision model
Embeddings       → embedding model
```

Three properties that make "why did it pick that one?" answerable, which is the
question the existing OpenRouter chain already answers and the reason that
answer is worth keeping:

* **The plan is reported, not just the choice.** :meth:`RoutingPlan.describe`
  lists the candidates and, for each skipped one, *why* — missing capabilities,
  disabled, refused by policy. A routing decision you cannot interrogate is a
  routing decision you cannot debug.
* **A user override always wins.** ``forced_model`` bypasses routing entirely
  and says so in the plan, rather than pretending the router chose it.
* **Policy is applied here, not in the transport.** A model the provider's policy
  refuses is excluded *before* the candidate list is built, so it never appears
  as "third choice, would have worked".

Ordering is by explicit ``priority``, then by provider id, then by model id —
fully determined by data, so the same request always produces the same plan. A
router whose order depended on dict iteration would make its own bugs
irreproducible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.providers.capabilities import (
    Capability,
    TIER_EMBEDDINGS,
    TIER_FAST,
    TIER_REASONING,
    missing,
    normalise,
)
from core.providers.store import (
    ProviderError,
    assert_model_allowed,
    default_provider,
    get_provider,
    get_tier_assignments,
    list_models,
    list_providers,
)

logger = logging.getLogger("Zaytoon.Core.Providers.Router")


class NoModelAvailable(RuntimeError):
    """Nothing in the registry can serve the request."""

    def __init__(self, message: str, plan: Optional["RoutingPlan"] = None):
        super().__init__(message)
        self.plan = plan


@dataclass
class Candidate:
    """One model considered, with the verdict on it."""

    provider_id: str
    provider_name: str
    model_id: str
    capabilities: List[str] = field(default_factory=list)
    priority: int = 100
    context_length: Optional[int] = None
    usable: bool = False
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "provider": self.provider_name,
            "model": self.model_id,
            "capabilities": list(self.capabilities),
            "priority": self.priority,
            "usable": self.usable,
            "reason": self.reason,
        }


@dataclass
class RoutingPlan:
    """The full routing decision: what was chosen, and what was not, and why."""

    candidates: List[Candidate] = field(default_factory=list)
    required_capabilities: List[str] = field(default_factory=list)
    provider_id: Optional[str] = None
    provider_name: Optional[str] = None
    model: Optional[str] = None
    base_url: Optional[str] = None
    forced: bool = False
    #: How the model was chosen: ``tier`` (a pinned tier assignment),
    #: ``forced`` (per-request override) or ``priority`` (ordinary ordering).
    #: Without this, "the router picked it" and "you pinned it" look alike.
    source: str = ""
    error: str = ""

    @property
    def usable(self) -> List[Candidate]:
        return [c for c in self.candidates if c.usable]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider_name,
            "provider_id": self.provider_id,
            "model": self.model,
            "base_url": self.base_url,
            "forced": self.forced,
            "source": self.source,
            "required_capabilities": list(self.required_capabilities),
            "candidates": [c.to_dict() for c in self.candidates],
            "error": self.error,
        }


#: What each tier implies. A tier is a *shorthand*, and expanding it here keeps
#: the request path from having to know that "fast" means "streaming".
TIER_REQUIREMENTS: Dict[str, List[str]] = {
    TIER_FAST: [Capability.CHAT.value],
    TIER_REASONING: [Capability.CHAT.value, Capability.REASONING.value],
    TIER_EMBEDDINGS: [Capability.EMBEDDINGS.value],
    "coding": [Capability.CHAT.value, Capability.CODING.value],
    "vision": [Capability.CHAT.value, Capability.VISION.value],
    "tools": [Capability.CHAT.value, Capability.TOOLS.value],
    "chat": [Capability.CHAT.value],
}


def requirements_for(tier: Optional[str], extra: Optional[List[str]] = None) -> List[str]:
    """Expand a tier (and any extras) into a capability list."""
    required: List[str] = list(TIER_REQUIREMENTS.get((tier or TIER_FAST).lower(), []))
    for value in extra or []:
        text = str(value).strip()
        if text and text not in required:
            required.append(text)
    return required


async def plan(
    required: Optional[List[str]] = None,
    provider_id: Optional[str] = None,
    forced_model: Optional[str] = None,
    tier: Optional[str] = None,
) -> RoutingPlan:
    """Decide which model should serve a request. Never raises for "none".

    ``tier`` brings the user's pinned assignment into the decision (see
    :func:`core.providers.store.get_tier_assignments`): the pin wins over
    priority order, but only while it is actually eligible — present,
    enabled, policy-clean and capable. An unusable pin falls back to ordinary
    ordering with the reason left on the candidate, because a pin that
    silently stops working is exactly the routing decision nobody can debug.
    """
    wanted = [r for r in (required or []) if str(r).strip()]
    decision = RoutingPlan(required_capabilities=wanted)

    provider = await get_provider(provider_id) if provider_id else await default_provider()
    if provider is None:
        # Two genuinely different situations, and they need different fixes from
        # the user: nothing configured at all, versus everything switched off.
        existing = await list_providers()
        if existing:
            decision.error = (
                f"All {len(existing)} AI provider(s) are disabled. "
                "Enable one under Models."
            )
        else:
            decision.error = (
                "No AI provider is configured. Add one under Models, or start Ollama."
            )
        return decision
    # An explicitly named provider that is switched off is reported as such.
    # A provider found by fallback is simply not a candidate, so "no provider is
    # configured" is the accurate answer there — the user did not pick this one.
    if not provider.get("enabled"):
        if provider_id:
            decision.error = f"Provider '{provider['name']}' is disabled."
        else:
            decision.error = (
                "No enabled AI provider. Enable one under Models, or add a provider."
            )
        return decision

    decision.provider_id = provider["id"]
    decision.provider_name = provider["name"]
    decision.base_url = provider["base_url"]

    # An override is the user's explicit instruction. It is honoured even if the
    # recorded capabilities do not match, and the plan says "forced" so the
    # difference between "the router chose this" and "you chose this" stays
    # visible.
    if forced_model:
        try:
            model_id = assert_model_allowed(provider["kind"], forced_model)
        except ProviderError as exc:
            decision.error = str(exc)
            return decision
        decision.model = model_id
        decision.forced = True
        decision.source = "forced"
        return decision

    models = await list_models(provider_id=provider["id"], enabled_only=True)
    if not models:
        decision.error = (
            f"No models recorded for '{provider['name']}'. "
            "Refresh the model list, or add one manually."
        )
        return decision

    for entry in models:
        candidate = Candidate(
            provider_id=provider["id"],
            provider_name=provider["name"],
            model_id=entry["id"],
            capabilities=list(entry.get("capabilities") or []),
            priority=int(entry.get("priority") or 100),
            context_length=entry.get("context_length"),
        )
        # Policy first: a model this provider may not call is excluded with that
        # as its reason, so it never appears as a viable choice.
        try:
            assert_model_allowed(provider["kind"], candidate.model_id)
        except ProviderError as exc:
            candidate.usable = False
            candidate.reason = str(exc)
            decision.candidates.append(candidate)
            continue

        absent = missing(candidate.capabilities, wanted)
        if absent:
            candidate.usable = False
            candidate.reason = f"lacks required capabilities: {', '.join(absent)}"
        else:
            candidate.usable = True
            candidate.reason = "eligible"
        decision.candidates.append(candidate)

    usable = sorted(decision.usable, key=lambda c: (c.priority, c.provider_id, c.model_id))

    # The user's pin, if this tier has one and it belongs to this provider.
    # Exact tier first, ``default`` as the catch-all (the legacy ``fallback``
    # tier lands here), so "pin the default and forget the rest" works.
    if tier:
        assignments = await get_tier_assignments()
        pinned = assignments.get(tier) or assignments.get("default")
        if pinned and pinned.get("provider_id") == provider["id"]:
            target = pinned["model_id"]
            match = next((c for c in decision.candidates if c.model_id == target), None)
            if match is not None and match.usable:
                decision.model = match.model_id
                decision.source = "tier"
            elif match is not None:
                # Pinned but ineligible (no capability / policy refusal):
                # keep the candidate and say why, then fall through to
                # priority order below.
                match.reason = f"tier assignment: {match.reason}"
            else:
                # Pinned row is disabled or gone. `candidates` only holds
                # enabled rows, so check whether it exists at all to tell
                # "you switched it off" from "it is no longer there".
                known = await list_models(provider_id=provider["id"], enabled_only=False)
                exists = any(row["id"] == target for row in known)
                logger.info(
                    "Tier %s pin %s is %s; routing by priority",
                    tier, target, "disabled" if exists else "no longer recorded",
                )

    if not decision.model:
        if usable:
            decision.model = usable[0].model_id
            decision.source = "priority"
        else:
            decision.error = _explain(decision)

    return decision


def _explain(decision: RoutingPlan) -> str:
    """Why nothing was usable, in terms the user can act on."""
    if not decision.candidates:
        return "No models are registered."
    reasons = {c.reason for c in decision.candidates if c.reason}
    if len(reasons) == 1:
        return f"No model can serve this request: {reasons.pop()}"
    return (
        "No model can serve this request. Needed: "
        f"{', '.join(decision.required_capabilities) or 'nothing in particular'}. "
        f"{len(decision.candidates)} model(s) were considered and all were skipped."
    )


async def resolve(
    tier: Optional[str] = None,
    extra_capabilities: Optional[List[str]] = None,
    provider_id: Optional[str] = None,
    forced_model: Optional[str] = None,
) -> RoutingPlan:
    """Route and fail loudly when nothing can serve the request."""
    decision = await plan(
        required=requirements_for(tier, extra_capabilities),
        provider_id=provider_id,
        forced_model=forced_model,
        tier=tier,
    )
    if not decision.model:
        raise NoModelAvailable(decision.error or "No model available", decision)
    logger.info(
        "Routed to %s/%s (forced=%s, required=%s)",
        decision.provider_name, decision.model, decision.forced,
        ",".join(decision.required_capabilities) or "-",
    )
    return decision


async def refresh_models(provider_id: str) -> Dict[str, Any]:
    """Probe an endpoint and record what it reports.

    Discovery is explicit, on demand, never on a timer: a background probe
    against every provider would be a request the user did not ask for, and a
    local daemon that is asleep would fail it every time.
    """
    from core.providers.client import OpenAICompatibleClient
    from core.providers.store import resolve_api_key

    provider = await get_provider(provider_id)
    if provider is None:
        raise ProviderError(f"No such provider: {provider_id}")

    key = await resolve_api_key(provider_id)
    client = OpenAICompatibleClient(
        provider["base_url"],
        api_key=key,
        kind=provider["kind"],
    )
    try:
        discovered = await client.list_models()
    finally:
        await client.aclose()

    added = 0
    for index, model in enumerate(discovered):
        # Priority preserves the endpoint's own ordering as a tie-break, so a
        # user-added provider behaves the way the endpoint ranks its models.
        saved = await _save_discovered(provider, model, priority=index * 10)
        added += 1 if saved else 0

    logger.info(
        "Refreshed %d model(s) for %s", len(discovered), provider["name"]
    )
    return {
        "provider_id": provider_id,
        "provider": provider["name"],
        "discovered": len(discovered),
        "added": added,
        "skipped": len(discovered) - added,
        "models": [m.to_dict() for m in discovered],
    }


async def _save_discovered(provider: Dict[str, Any], model, priority: int) -> bool:
    from core.providers.store import save_model

    try:
        return await save_model(
            provider["id"],
            model.id,
            display_name=model.display_name,
            capabilities=model.capabilities,
            context_length=model.context_length,
            priority=priority,
            # A discovered model on an OpenRouter provider still has to satisfy
            # the free-only policy; `validate_policy` is left on so a discovered
            # billable id is refused rather than recorded and routed to.
        )
    except ProviderError as exc:
        logger.info(
            "Skipping discovered model %s from %s: %s", model.id, provider["name"], exc
        )
        return False


async def provider_health(provider_id: Optional[str] = None) -> Dict[str, Any]:
    """Test an endpoint's reachability. What the "Test connection" button calls."""
    from core.providers.client import OpenAICompatibleClient
    from core.providers.store import resolve_api_key

    provider = await get_provider(provider_id) if provider_id else await default_provider()
    if provider is None:
        return {"connected": False, "error": "No provider configured", "models": []}

    key = await resolve_api_key(provider["id"]) if provider["requires_key"] else None
    client = OpenAICompatibleClient(provider["base_url"], api_key=key, kind=provider["kind"])
    try:
        result = await client.health()
    finally:
        await client.aclose()
    result.update({"provider": provider["name"], "provider_id": provider["id"],
                   "endpoint": provider["base_url"]})
    if not result.get("connected"):
        logger.info("Provider health check failed for %s: %s",
                    provider["name"], result.get("error"))
    return result


def capabilities_of(plan_result: RoutingPlan) -> List[str]:
    """The chosen model's capabilities, or the defaults."""
    for candidate in plan_result.candidates:
        if candidate.model_id == plan_result.model:
            return sorted(normalise(candidate.capabilities))
    return sorted(normalise(None))


__all__ = [
    "Candidate",
    "NoModelAvailable",
    "RoutingPlan",
    "TIER_REQUIREMENTS",
    "capabilities_of",
    "plan",
    "provider_health",
    "refresh_models",
    "requirements_for",
    "resolve",
]