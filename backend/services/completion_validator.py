"""Deterministic validation of a completion against the request's contract.

The bug this exists to prevent
-------------------------------

``LLMClient.complete()`` returned whatever text came back in ``content``. The
WebSocket layer checked the HTTP status and the JSON shape, and then handed the
string to the agent. So when the primary model was rate-limited and the chain
fell through to the uncontrolled ``openrouter/free`` router, an arbitrary model
could answer a ``get_time`` request with::

    User Safety: safe

and the system would present that to the user as the assistant's answer. It was
a structurally valid 200 with a non-empty ``content`` string. **The transport
succeeded and the answer did not exist.**

The fix is not a bigger model and not a judge model. It is a contract.

What this validates, and what it deliberately does not
-----------------------------------------------------

Validation is **structural and local**: shape, emptiness, tool-name membership,
argument types, and a narrow deny-list of *provider metadata leaked as content*.
There is no LLM in this path (Step 16 of the brief) — a judge would be another
unreliable free model, would double latency, and would be judging a completion
with exactly the judgement we are trying to avoid outsourcing.

The honest limitation, stated up front: **a fluent, confident, wrong paragraph
of prose cannot be detected here.** "It's half past three in Cairo" is
structurally indistinguishable from "it's half past three in London". This layer
catches protocol violations and the specific metadata-leak class of failure that
was actually observed. It is a real improvement, not a proof of correctness, and
pretending otherwise would be the more dangerous claim.

Two things make it stronger than it sounds:

* The engine knows when it *asked for a tool call* and did not get one, so
  ``require_tool`` catches the whole class of "answered instead of acting".
* Metadata leaks are not fuzzy. ``User Safety:`` is a provider field name, and
  a reply that is nothing but a ``Label: value`` pair using such a field name is
  a leak by any reasonable reading.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Mapping, Optional

from services.model_registry import CAP_TOOLS
from common.reporting import safe_error


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------
class Verdict:
    OK = "OK"
    EMPTY = "EMPTY_COMPLETION"
    MALFORMED = "MALFORMED_COMPLETION"
    UNKNOWN_TOOL = "INVALID_TOOL_UNKNOWN"
    BAD_ARGS = "INVALID_TOOL_ARGS"
    MISSING_TOOL = "MISSING_TOOL_CALL"
    METADATA_LEAK = "PROVIDER_METADATA_LEAK"
    PROTOCOL = "AGENT_PROTOCOL_VIOLATION"


@dataclass
class ValidationResult:
    """Whether a completion satisfies the contract, and why not if it doesn't."""

    ok: bool
    verdict: str = Verdict.OK
    detail: str = ""
    #: Tool calls recovered from the completion. Populated even when ``ok`` is
    #: False, so a caller can log what the model actually tried.
    tool_calls: List[Any] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "verdict": self.verdict,
            "detail": self.detail[:300],
            "tool_calls": [
                {"name": c.name, "params": c.params} for c in self.tool_calls
            ],
        }


@dataclass(frozen=True)
class ToolShape:
    """Just enough of a tool to check a call against it."""

    name: str
    #: JSON-schema ``properties``; value types are compared, not the whole schema.
    properties: Mapping[str, str] = field(default_factory=dict)
    required: FrozenSet[str] = frozenset()

    @classmethod
    def from_tool(cls, tool: Any) -> "ToolShape":
        schema = {}
        try:
            schema = tool.schema() or {}
        except Exception:
            schema = {}
        props = {}
        for key, spec in (schema.get("properties") or {}).items():
            if isinstance(spec, dict):
                props[key] = spec.get("type", "any")
        return cls(
            name=tool.name(),
            properties=props,
            required=frozenset(schema.get("required") or ()),
        )


@dataclass(frozen=True)
class CompletionContract:
    """What a valid completion of *this* request must look like.

    Built per-request, not global: iteration 1 of the agent loop genuinely
    requires a tool call, while the final iteration requires prose. A single
    global rule would either reject every answer or accept every non-answer.
    """

    #: The model must produce at least one valid tool call.
    require_tool: bool = False
    #: Prose with no tool call is an acceptable terminal answer.
    allow_prose: bool = True
    #: Tools that may be called, and their argument shapes.
    tools: Mapping[str, ToolShape] = field(default_factory=dict)
    #: Human-readable note, used in logs. Never includes user content.
    label: str = ""
    #: Let a call naming a tool that does not exist pass validation, so the
    #: *registry* refuses it with a message the model can correct itself from.
    #:
    #: Off by default. The reasoning behind the flag: a wrong tool name is a
    #: recoverable mistake, and the agent loop is built to feed failures back to
    #: the model — but a fatal validator verdict happens one layer too early. It
    #: spends the attempt budget on a slip the loop would have fixed on its next
    #: iteration, and ends the turn with "all models are unavailable", which
    #: describes the provider and not what happened. The engine still cannot run
    #: the call: `ToolRegistry.execute` refuses an unknown tool by name, so this
    #: trades a dead turn for a corrected one without loosening what can execute.
    allow_unknown_tools: bool = False

    @property
    def required_capabilities(self) -> FrozenSet[str]:
        return frozenset({CAP_TOOLS}) if self.require_tool else frozenset()

    def to_dict(self) -> dict:
        return {
            "require_tool": self.require_tool,
            "allow_prose": self.allow_prose,
            "known_tools": sorted(self.tools),
            "allow_unknown_tools": self.allow_unknown_tools,
            "label": self.label,
        }


#: A plain chat turn. Any non-empty prose is fine.
CHAT_CONTRACT = CompletionContract(allow_prose=True, label="chat")

#: No contract at all — only emptiness is checked. Used by the streaming path,
#: where the text has already been shown to the user and cannot be retracted.
STREAM_CONTRACT = CompletionContract(allow_prose=True, label="stream")


def contract_from_registry(registry: Any, **kwargs) -> CompletionContract:
    """Build a contract that knows the registry's real tools and schemas.

    **Aliases are registered too, not just primary names.** The registry already
    resolves them — ``file_list`` reaches ``file_read`` through
    ``BaseTool.aliases`` — so a contract built from primary names alone would
    reject a call the executor would happily run. That is not a theoretical
    gap: the aliases include names a model reaches for constantly
    (``list_directory``, ``read_file``), and one live turn was rejected as
    ``INVALID_TOOL_UNKNOWN`` for calling ``file_list`` while the registry sat
    there able to serve it.

    The alias maps to the same shape as its primary, so the schema check the
    model has to satisfy is the one the tool really enforces.
    """
    tools: Dict[str, ToolShape] = {}
    try:
        for tool in registry.tools():
            shape = ToolShape.from_tool(tool)
            tools[shape.name] = shape
            for alias in _aliases_of(tool):
                # A primary name always wins over another tool's alias.
                tools.setdefault(alias, shape)
    except Exception as e:  # noqa: BLE001 - a broken tool must not block boot
            # This one is load-bearing to get wrong: an empty `tools` map means
            # the validator quietly stops validating anything, while still
            # reporting "no violations" — the most convincing kind of useless.
            safe_error(e, operation="completion_validator.load_shapes",
                       component="CompletionValidator")
    return CompletionContract(tools=tools, **kwargs)


def _aliases_of(tool: Any) -> List[str]:
    """The aliases a tool declares, as a plain list of non-empty strings."""
    getter = getattr(tool, "aliases", None)
    if getter is None:
        return []
    try:
        aliases = getter() if callable(getter) else getter
    except Exception:
        return []
    if not isinstance(aliases, (list, tuple, set)):
        return []
    return [a for a in aliases if isinstance(a, str) and a.strip()]


# ---------------------------------------------------------------------------
# Metadata-leak detection
# ---------------------------------------------------------------------------
#: Field names OpenRouter and upstream providers put in structured output. A
#: completion that is *nothing but* one of these labels is the provider's own
#: metadata, not an answer — which is exactly what `openrouter/free` produced.
_METADATA_LABELS = (
    "user safety",
    "safety",
    "moderation",
    "finish reason",
    "finish_reason",
    "native_finish_reason",
    "category",
    "severity",
    "blocked",
    "refusal",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "system_fingerprint",
    "logprobs",
)

#: ``Label: value`` with nothing else around it.
_LABEL_ONLY = re.compile(
    r"^\s*(?:" + "|".join(re.escape(x) for x in _METADATA_LABELS) + r")\s*:\s*.*$",
    re.IGNORECASE | re.DOTALL,
)

#: A few labels joined on one line, e.g. "Safety: safe Category: x".
_LABEL_RUN = re.compile(
    r"(?:" + "|".join(re.escape(x) for x in _METADATA_LABELS) + r")\s*:",
    re.IGNORECASE,
)


def looks_like_metadata_leak(text: str) -> Optional[str]:
    """Return a reason when ``text`` is provider metadata, else ``None``.

    Narrow on purpose. It fires when the *whole* completion is a metadata label
    (the observed failure), or when a run of metadata labels accounts for the
    entire reply. A normal answer that happens to contain the word "safety" is
    not flagged.
    """
    stripped = (text or "").strip()
    if not stripped:
        return None

    if _LABEL_ONLY.match(stripped):
        return f"completion is a provider metadata label: {stripped[:80]!r}"

    # Everything in the reply is metadata labels and separators?
    if _LABEL_RUN.search(stripped):
        residue = _LABEL_RUN.sub(" ", stripped)
        residue = re.sub(r"[\s,;|]+", " ", residue).strip()
        if not residue:
            return f"completion is only provider metadata labels: {stripped[:80]!r}"

    return None


# ---------------------------------------------------------------------------
# Argument checking
# ---------------------------------------------------------------------------
_JSON_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": (list, tuple),
    "object": dict,
}


def _check_args(call: Any, shape: Optional[ToolShape]) -> Optional[str]:
    """Return a reason the arguments are unacceptable, or ``None``."""
    params = getattr(call, "params", None)
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return f"arguments are {type(params).__name__}, expected an object"

    if shape is None:
        return None

    missing = sorted(shape.required - set(params))
    if missing:
        return f"missing required argument(s): {', '.join(missing)}"

    for key, declared in shape.properties.items():
        if key not in params:
            continue
        value = params[key]
        expected = _JSON_TYPES.get(str(declared))
        if expected is None:
            continue
        # bool is a subclass of int; an integer field must not accept True.
        if declared in ("integer", "number") and isinstance(value, bool):
            return f"argument {key!r} is a bool, expected {declared}"
        if not isinstance(value, expected):
            return (
                f"argument {key!r} is {type(value).__name__}, expected {declared}"
            )

    unknown = sorted(set(params) - set(shape.properties)) if shape.properties else []
    if unknown and shape.properties:
        return f"unknown argument(s) for {shape.name}: {', '.join(unknown)}"

    return None


# ---------------------------------------------------------------------------
# The validator
# ---------------------------------------------------------------------------
class CompletionValidator:
    """Deterministic contract check. No network, no model, no I/O."""

    def validate(
        self,
        content: Optional[str],
        contract: CompletionContract = CHAT_CONTRACT,
        *,
        extract=None,
    ) -> ValidationResult:
        """Check ``content`` against ``contract``.

        ``extract`` is the caller-supplied tool-call parser
        (``agents.engine.extract_tool_calls``). It is injected rather than
        imported so this module stays free of an agents dependency and can be
        tested in isolation.
        """
        if content is None or not str(content).strip():
            return ValidationResult(
                False, Verdict.EMPTY, "completion was empty"
            )

        text = str(content)

        calls: List[Any] = []
        if extract is not None and contract.tools:
            try:
                parsed, _prose = extract(text)
                calls = list(parsed or [])
            except Exception as e:  # a parser bug must not mask the verdict
                return ValidationResult(
                    False, Verdict.MALFORMED, f"tool-call parser raised: {e}"
                )

        # A tool call was attempted. It must be a real one — unless the caller
        # opted in to a correction round, in which case the name is passed
        # through for the registry to refuse with a message the model learns
        # from. See `CompletionContract.allow_unknown_tools`.
        if calls:
            for call in calls:
                name = getattr(call, "name", "")
                if name not in contract.tools:
                    if contract.allow_unknown_tools:
                        continue
                    return ValidationResult(
                        False,
                        Verdict.UNKNOWN_TOOL,
                        f"model called unknown tool {name!r}",
                        tool_calls=calls,
                    )
                problem = _check_args(call, contract.tools.get(name))
                if problem:
                    if contract.allow_unknown_tools:
                        continue
                    return ValidationResult(
                        False,
                        Verdict.BAD_ARGS,
                        f"{name}: {problem}",
                        tool_calls=calls,
                    )
            return ValidationResult(True, tool_calls=calls)

        leak = looks_like_metadata_leak(text)
        if leak:
            return ValidationResult(False, Verdict.METADATA_LEAK, leak, tool_calls=calls)

        if contract.require_tool:
            # The model was asked to act and produced prose instead. That is a
            # contract violation even when the prose is fluent and plausible.
            return ValidationResult(
                False,
                Verdict.MISSING_TOOL,
                "a tool call was required but the model replied in prose",
                tool_calls=calls,
            )

        if not contract.allow_prose:
            return ValidationResult(
                False,
                Verdict.PROTOCOL,
                "prose was not an acceptable response for this contract",
                tool_calls=calls,
            )

        return ValidationResult(True, tool_calls=calls)


#: Shared instance. The validator holds no state.
validator = CompletionValidator()


def validate_completion(
    content: Optional[str],
    contract: CompletionContract = CHAT_CONTRACT,
    *,
    extract=None,
) -> ValidationResult:
    """Module-level convenience wrapper around :meth:`CompletionValidator.validate`."""
    return validator.validate(content, contract, extract=extract)
