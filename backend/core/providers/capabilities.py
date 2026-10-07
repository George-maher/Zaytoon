"""Model capabilities, as a vocabulary the router can filter on.

Capability is a set of strings, not a boolean per model, because the useful
question is a *subset* question: "which models can both stream and emit a tool
call?" A table of booleans answers "can it do X" but makes the conjunction
awkward, and the conjunction is what routing actually does.

An **unknown** capability is not the same as a false one. A model nobody has
probed is not known to be incapable of tool calls — it is just unprobed, and
refusing to route to it would make a freshly installed model unusable. So the
distinction is preserved: capability values are declared, and a model with no
record carries the :data:`DEFAULT_CAPABILITIES` set, which is what a plain
chat-completion endpoint can be relied on to do.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set


class Capability(str, Enum):
    """What a model can do."""

    CHAT = "chat"
    STREAMING = "streaming"
    TOOLS = "tools"
    STRUCTURED_OUTPUT = "structured_output"
    REASONING = "reasoning"
    CODING = "coding"
    VISION = "vision"
    EMBEDDINGS = "embeddings"

    @property
    def label(self) -> str:
        return _LABELS.get(self, self.value)


_LABELS: Dict[Capability, str] = {
    Capability.CHAT: "Chat completion",
    Capability.STREAMING: "Streaming responses",
    Capability.TOOLS: "Tool / function calling",
    Capability.STRUCTURED_OUTPUT: "Structured JSON output",
    Capability.REASONING: "Multi-step reasoning",
    Capability.CODING: "Code generation",
    Capability.VISION: "Image input",
    Capability.EMBEDDINGS: "Embeddings",
}

ALL_CAPABILITIES: FrozenSet[Capability] = frozenset(Capability)

#: What any OpenAI-compatible ``/chat/completions`` endpoint can be assumed to
#: do. Deliberately minimal: streaming and tools are the two the application
#: actually depends on, and both are part of the chat-completions contract.
DEFAULT_CAPABILITIES: FrozenSet[Capability] = frozenset({
    Capability.CHAT,
    Capability.STREAMING,
})

#: Tiers, kept because the existing routing is tier-based. A new provider does
#: not have to adopt them, but the built-in ones do.
TIER_FAST = "fast"
TIER_REASONING = "reasoning"
TIER_EMBEDDINGS = "embeddings"


def normalise(values: Optional[Iterable[Any]]) -> Set[str]:
    """Coerce anything into a set of capability strings.

    Unknown strings are **kept**, not dropped: a newer client may send a
    capability this build has not heard of, and silently discarding it would
    make the model unroutable for no reason.
    """
    if values is None:
        return set(DEFAULT_CAPABILITIES)
    if isinstance(values, str):
        values = [values]
    result: Set[str] = set()
    for value in values:
        text = str(value).strip().lower()
        if not text:
            continue
        try:
            result.add(Capability(text).value)
        except ValueError:
            result.add(text)
    return result or set(DEFAULT_CAPABILITIES)


def has_all(capabilities: Iterable[Any], required: Iterable[Any]) -> bool:
    """True when every required capability is present."""
    have = normalise(capabilities)
    return all(str(r).strip().lower() in have for r in required if str(r).strip())


def missing(capabilities: Iterable[Any], required: Iterable[Any]) -> List[str]:
    """Which required capabilities are absent. Empty when nothing is."""
    have = normalise(capabilities)
    return [str(r) for r in required if str(r).strip() and str(r).strip().lower() not in have]


def describe() -> List[Dict[str, str]]:
    """Every capability, for the models screen."""
    return [{"capability": c.value, "label": c.label} for c in sorted(Capability, key=lambda x: x.value)]


__all__ = [
    "ALL_CAPABILITIES",
    "Capability",
    "DEFAULT_CAPABILITIES",
    "TIER_EMBEDDINGS",
    "TIER_FAST",
    "TIER_REASONING",
    "describe",
    "has_all",
    "missing",
    "normalise",
]