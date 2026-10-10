"""The agent loop.

``ToolAgent`` used to be: one LLM call, regex out any JSON, run it, join the
results with ``" | "``, and return that as the answer. No re-planning, no
observation feedback, no error recovery, and — because the model's JSON was
streamed straight at the user — the tool call itself was rendered on screen
before being overwritten.

This is the same shape as a bounded ReAct loop, which fixes all four:

    ┌─► ask the model what to do next (tool catalogue + observations so far)
    │      ├─ no tool call  ─────────────────────────► final answer, streamed
    │      └─ tool call
    │           ├─ validate → gate → execute        (tools/registry.py)
    │           ├─ record the result as an observation
    │           └─ loop
    └─ iteration budget exhausted ──────────────────► answer from what we have

Three properties are load-bearing:

**Bounded.** ``max_iterations`` is a hard ceiling. An agent that can call tools
in a loop can spin forever if the model keeps asking for the same thing; the
budget is what makes it safe to run unattended.

**Observations are fed back.** A tool that fails returns its error as an
observation, so the model can correct itself instead of the turn ending. This
is what turns "create a React project" from one guess into observe-and-retry.

**Tool calls are not prose.** Intermediate model output is parsed and
discarded; only the final no-tool-call answer is emitted as
``RESPONSE_CHUNK``. That is how private reasoning stays off the wire, and it is
structural rather than a matter of prompt wording.

Tool selection is JSON-in-text rather than native function calling. The models
in ``config.ALLOWED_FREE_MODELS`` are a hard-coded allow-list of three, one of
which currently returns HTTP 403; betting the product on a free endpoint
advertising function-calling support is a worse trade than parsing text
reliably. ``ToolRegistry.openai_tools()`` already emits the spec, so switching
is a provider change rather than a rewrite.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional, Tuple

from agents.events import EventBus, NullBus
from common.audit import record as audit_record
from common.reporting import safe_error
from tools.metrics import (
    STATUS_DECLINED,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_TIMED_OUT,
    get_tool_metrics,
    record_tool_call,
)
from services.completion_validator import (
    CompletionContract,
    contract_from_registry,
)
from tools.registry import ToolRegistry
from services.llm_client import user_notice

logger = logging.getLogger("Zaytoon.AgentEngine")

#: Hard ceiling on tool-use rounds. Reaching it is a normal, reported outcome
#: rather than an error: the model gets whatever it had and is asked to wrap up.
DEFAULT_MAX_ITERATIONS = 8

#: Ceiling on how much observation text is carried forward, so a chatty tool
#: cannot blow the context window on the next request.
MAX_OBSERVATION_CHARS = 4_000

#: The longest a single tool may run before the engine stops waiting. Tools
#: have their own timeouts; this is the backstop for one that hangs.
TOOL_TIMEOUT_S = 120

#: How many *safe-tier* calls from one batch may run at once (increment B).
#: Capped rather than unbounded so a model that emits twenty read calls cannot
#: open twenty subprocesses or twenty files at the same time. Confirm and
#: dangerous calls never share this budget — they run one at a time.
PARALLEL_SAFE_TOOLS = 4

#: How often a long-running tool says so (increment C). A module constant
#: rather than a config value because it is a *wire cadence*, not a policy:
#: tests monkeypatch it down to ~0.05s, and nothing outside the engine reads it.
TOOL_HEARTBEAT_S = 2.0

#: Heartbeat text. Fixed and human-written — never the model's own words, for
#: the same reason tool labels are fixed.
_HEARTBEAT = {
    "en": "Still working…",
    "ar": "لسه بشتغل…",
}


async def _heartbeat(name: str, bus: EventBus, language: str) -> None:
    """Emit ``tool_progress`` until cancelled by the call it belongs to.

    Cancellation is the *only* way out, so the frame count for a call is a
    function of its duration — which is what makes it a useful liveness
    signal. The first tick waits a full interval, so a fast tool never
    produces one.
    """
    message = _HEARTBEAT.get(language, _HEARTBEAT["en"])
    while True:
        await asyncio.sleep(TOOL_HEARTBEAT_S)
        await bus.tool_progress(name, message=message)


# ---------------------------------------------------------------------------
# Tool-call extraction
#: Keys a model might use to name the tool. Matched structurally rather than by
#: a fixed key order, because models emit fields in whatever order they like.
_ACTION_KEYS = ("action", "name", "tool", "tool_name")


def _iter_json_objects(text: str):
    """Yield ``(start, end, raw)`` for every balanced ``{...}`` in ``text``.

    A regex such as ``\\{[^{}]*\\}`` cannot do this: the documented call form
    nests its parameters -- ``{"action": "file_write", "params": {...}}`` -- so a
    non-nesting pattern matches nothing at all, and the nested form silently
    became prose. A real scanner also copes with a brace inside a string value,
    which a naive depth counter gets wrong.
    """
    length = len(text)
    index = 0
    while index < length:
        if text[index] != "{":
            index += 1
            continue
        depth = 0
        in_string = False
        escaped = False
        cursor = index
        closed_at = -1
        while cursor < length:
            char = text[cursor]
            if in_string:
                if escaped:
                    escaped = False
                elif char == chr(92):
                    escaped = True
                elif char == chr(34):
                    in_string = False
            elif char == chr(34):
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    closed_at = cursor + 1
                    break
            cursor += 1
        if closed_at < 0:
            # Unbalanced, most likely a truncated object mid-stream. Stop
            # scanning rather than treating the remainder as prose forever.
            return
        yield index, closed_at, text[index:closed_at]
        index = closed_at


#: Some models ignore "reply with a JSON object" and emit a pseudo-XML tool
#: call instead, having absorbed the convention from their own training. The
#: live run showed `qwen/qwen3.8-27b:free` doing exactly this for
#: "what time is it right now":
#:
#:     <tool_call>get_time
#:
#:     </tool_call>
#:
#: The JSON parser finds nothing, the engine concludes "no tool call, so this is
#: the answer", and the user is shown a literal XML tag. That is the worst
#: possible failure: it looks like a bug and it is *the answer*. So the tagged
#: form is parsed too, and anything still tag-shaped afterwards is treated as a
#: parse failure rather than as prose.
_TAG_CALL = re.compile(
    r"<\s*tool_call\s*>(.*?)<\s*/\s*tool_call\s*>",
    re.DOTALL | re.IGNORECASE,
)
#: A tool call the model began but never closed (truncated mid-stream).
_TAG_OPEN = re.compile(r"<\s*tool_call\s*>(.*)\Z", re.DOTALL | re.IGNORECASE)

#: Qwen's native dialect, observed live for the same request:
#:
#:     <|tool_call_start|>[get_time()]<|tool_call_end|>
#:
#: The model reaches for whichever tool-call convention it saw in training, and
#: it reaches for more than one: across four identical live runs of "what time
#: is it right now" this codebase had to cope with the JSON form, the
#: ``<tool_call>`` form and this one. The body is a call expression
#: (``name(arg=value)``), not JSON, so it needs a different parser — but it is
#: a *better* dialect than the XML one, because the arguments survive.
_PIPE_CALL = re.compile(
    r"<\|\s*tool_call_start\s*\|>(.*?)<\|\s*tool_call_end\s*\|>",
    re.DOTALL | re.IGNORECASE,
)
_PIPE_CALL_OPEN = re.compile(
    r"<\|\s*tool_call_start\s*\|>(.*)\Z", re.DOTALL | re.IGNORECASE
)
#: `get_time()` or `file_write(path="a.txt", content="x")`, optionally wrapped in
#: the square brackets Qwen adds.
_CALL_EXPR = re.compile(r"^\[?\s*([A-Za-z_][A-Za-z0-9_]*)\s*\((.*?)\)\s*\]?\s*$", re.DOTALL)


def _iter_tag_calls(text: str):
    """Yield ``(start, end, ToolCall)`` for each tagged call of any dialect.

    Three forms are recognised, tried in order of how much they carry:

    1. ``<|tool_call_start|>[name(args)]<|tool_call_end|>``  (Qwen native)
    2. ``<tool_call>{"action": …}</tool_call>``  (JSON inside a tag)
    3. ``<tool_call>name</tool_call>``  (bare name)

    The unterminated fallback for each only fires when nothing closed cleanly.
    Matching both unconditionally double-counts every well-formed tag.
    """
    for regex, open_regex, parser in (
        (_PIPE_CALL, _PIPE_CALL_OPEN, _parse_call_expr),
        (_TAG_CALL, _TAG_OPEN, _parse_call_body),
    ):
        matched_closed = False
        for match in regex.finditer(text):
            call = parser(match.group(1).strip())
            if call is not None:
                matched_closed = True
                yield match.start(), match.end(), call
        if matched_closed:
            continue

        # An unterminated tag is a truncated call, not prose.
        tail = open_regex.search(text)
        if tail:
            call = parser(tail.group(1).strip())
            if call is not None:
                yield tail.start(), len(text), call


def _read_arg(node: ast.AST) -> Any:
    """Best-effort inert value for an argument that is not a literal.

    A bare name becomes its identifier, which is what a model means by
    ``[search_files(src)]``. Anything else is reduced to its source text, never
    evaluated — this is a parser, not an execution path.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _read_arg(node.value)
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        return ast.dump(node)


def _parse_call_expr(body: str) -> Optional[ToolCall]:
    """Parse ``name(arg=value, …)`` — the Qwen call dialect.

    Uses ``ast.parse`` and reads the tree. The expression is *never* compiled or
    evaluated, so this is a parser and not an execution path; the only reason
    ``ast`` is used rather than a regex is that argument values can be nested
    literals, and splitting on commas inside them is wrong.
    """
    if not body:
        return None
    match = _CALL_EXPR.match(body)
    if not match:
        return None
    name, raw_args = match.group(1), match.group(2)

    params: Dict[str, Any] = {}
    if raw_args.strip():
        try:
            tree = ast.parse(f"f({raw_args})", mode="eval")
        except (SyntaxError, ValueError, MemoryError, RecursionError):
            return None
        node = tree.body
        if not isinstance(node, ast.Call):  # pragma: no cover - regex guarantees
            return None
        for index, arg in enumerate(node.args):
            try:
                params[f"arg{index}"] = ast.literal_eval(arg)
            except (ValueError, SyntaxError):
                # A bare identifier such as `[search_files(src)]` is the common
                # case, and a model means the *name*. `ast.dump` would turn it
                # into "Name(id='src', ctx=Load())", which then travels into the
                # observation text as noise.
                params[f"arg{index}"] = _read_arg(arg)
        for keyword in node.keywords:
            if keyword.arg is None:
                continue  # `**kwargs` splat, not expressible here
            try:
                params[keyword.arg] = ast.literal_eval(keyword.value)
            except (ValueError, SyntaxError):
                params[keyword.arg] = _read_arg(keyword.value)

    return ToolCall(name=name, params=params, raw=body)


def _parse_call_body(body: str) -> Optional[ToolCall]:
    """Parse the inside of a tagged call: JSON object, or a bare name."""
    if not body:
        # An empty tag is not a call. Guarded explicitly because the bare-name
        # branch below indexes `split()[0]`, and an empty body makes that an
        # IndexError -- which would abort the whole turn rather than just this
        # one call.
        return None
    if body.startswith("{"):
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(parsed, dict):
            return None
        name = next(
            (parsed[k] for k in _ACTION_KEYS if isinstance(parsed.get(k), str)), None
        )
        if not name:
            return None
        params = parsed.get("params")
        if not isinstance(params, dict):
            params = {k: v for k, v in parsed.items() if k not in _ACTION_KEYS}
        return ToolCall(name=name.strip(), params=params, raw=body)

    # Bare form. Take the first whitespace-delimited token, so
    # "get_time now please" is still a get_time call rather than a bogus tool.
    name = body.split()[0].strip().strip("\"'`.,;:")
    if not name or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return None
    return ToolCall(name=name, params={}, raw=body)


def looks_like_tool_call(text: str) -> bool:
    """Whether ``text`` is still shaped like an unparsed tool call.

    Used as a guard: text that looks like this is a *parse failure*, and
    returning it to the user as the assistant's answer is worse than admitting
    the model did not comply.

    Deliberately broad. The model has now demonstrated three different
    tool-call dialects in four identical live runs, so this cannot be a list of
    the forms we happen to have implemented — it asks "does this contain
    tool-call machinery at all?", and defaults to yes.
    """
    if not text:
        return False
    stripped = text.strip()
    if not stripped:
        return False

    # Any mention of the convention, in any dialect.
    if "tool_call" in stripped.lower() or "toolcall" in stripped.lower():
        return True
    # Qwen's special tokens.
    if "<|" in stripped and "|>" in stripped:
        return True
    # A lone tool name in angle brackets, e.g. "<get_time>".
    if re.fullmatch(r"</?\s*[A-Za-z_][A-Za-z0-9_]*\s*>", stripped):
        return True
    # A bracketed call expression with no prose around it, e.g. "[get_time()]".
    if _CALL_EXPR.match(stripped) and len(stripped) <= 200:
        return True
    # An entire reply that is one JSON object in the documented dialect and
    # nothing else. It reached here *after* the scanners failed, so showing it
    # verbatim would put raw tool JSON on the user's screen; admitting the
    # model did not comply is strictly better.
    if stripped.startswith("{") and stripped.endswith("}") and len(stripped) <= 2000:
        if any(f'"{key}"' in stripped for key in _ACTION_KEYS):
            return True
    return False


@dataclass
class ToolCall:
    """One parsed request to run a tool."""

    name: str
    params: Dict[str, Any] = field(default_factory=dict)
    #: The raw JSON text, kept for diagnostics and for the model to see again.
    raw: str = ""

    def describe(self) -> str:
        if not self.params:
            return self.name
        rendered = ", ".join(
            f"{k}={_short(v)}" for k, v in list(self.params.items())[:3]
        )
        return f"{self.name}({rendered})"


def _short(value: Any, limit: int = 40) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def extract_tool_calls(text: str) -> Tuple[List[ToolCall], str]:
    """Pull tool calls out of a model response.

    Returns ``(calls, leftover_prose)``. The leftover is any text that was not
    part of a tool call, so a model that narrates *and* calls a tool does not
    lose the narration.

    Deliberately forgiving: a model that wraps its JSON in a ```json fence, or
    prefixes it with "Sure, I'll open that:", still parses. An object that
    fails to parse is left as prose rather than dropped, so nothing is lost.

    Two forms are recognised, and they are **not** independent: the tagged form
    is tried first and wins any overlap, because ``<tool_call>{"action":
    "x"}</tool_call>`` contains a JSON object that the plain scanner would
    otherwise claim. Overlap resolution is why that case yields one call rather
    than two.
    """
    if not text:
        return [], ""

    accepted: List[Tuple[int, int, ToolCall]] = []
    taken: List[Tuple[int, int]] = []

    def overlaps(start: int, end: int) -> bool:
        return any(not (end <= s or start >= e) for s, e in taken)

    for start, end, call in _iter_tag_calls(text):
        if overlaps(start, end):
            continue
        accepted.append((start, end, call))
        taken.append((start, end))

    for start, end, raw in _iter_json_objects(text):
        if overlaps(start, end):
            continue
        call = _parse_json_object(raw)
        if call is None:
            continue
        accepted.append((start, end, call))
        taken.append((start, end))

    if not accepted:
        return [], text

    # Reassemble the prose, dropping every recognised call.
    parts: List[str] = []
    cursor = 0
    for start, end, _ in sorted(accepted):
        parts.append(text[cursor:start])
        cursor = end
    parts.append(text[cursor:])
    leftover = "".join(parts).strip()
    # Strip a code fence left behind by the removed block.
    leftover = leftover.replace("```json", "").replace("```", "").strip()
    return [call for _, _, call in sorted(accepted)], leftover


def _parse_json_object(raw: str) -> Optional[ToolCall]:
    """Turn one balanced ``{...}`` into a :class:`ToolCall`, or ``None``."""
    parsed = None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # A model that was shown ``{{...}}`` copies the doubled braces, and
        # ``{{"action": x}}`` is not valid JSON. Unwrap once before giving up:
        # refusing here is what put raw tool JSON on a user's screen.
        try:
            body = raw.strip()
            if body.startswith("{{") and body.endswith("}}"):
                parsed = json.loads(body[1:-1])
        except (json.JSONDecodeError, ValueError):
            return None
    if parsed is None:
        return None
    if not isinstance(parsed, dict):
        return None

    name = next(
        (parsed[k] for k in _ACTION_KEYS if isinstance(parsed.get(k), str)), None
    )
    if not name:
        return None

    params = parsed.get("params")
    if not isinstance(params, dict):
        # Flat form: {"action": "x", "path": "y"}
        params = {k: v for k, v in parsed.items() if k not in _ACTION_KEYS}
    return ToolCall(name=name.strip(), params=params, raw=raw)


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------
def render_observation(call: ToolCall, result: Dict[str, Any]) -> str:
    """Turn a tool result into the text the model will read next turn.

    Errors are rendered *in full* rather than summarised away: the point of
    feeding observations back is that the model can see what went wrong and
    change course.
    """
    if result.get("success"):
        body = str(result.get("output") or "(no output)")
    elif result.get("blocked"):
        body = f"REFUSED BY THE SECURITY SANDBOX: {result.get('error')}"
    elif result.get("requires_confirmation"):
        body = (
            "NOT RUN - this action needs the user's explicit approval, and it "
            f"was not given. ({result.get('error')})"
        )
    elif result.get("cancelled"):
        body = f"NOT RUN - the user declined. ({result.get('error')})"
    else:
        body = f"FAILED: {result.get('error') or 'unknown error'}"
    if len(body) > MAX_OBSERVATION_CHARS:
        body = body[:MAX_OBSERVATION_CHARS] + " …[truncated]"
    return f"{call.describe()} → {body}"


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
SYSTEM_EN = """\
You are Zaytoon, a desktop assistant that can use tools.
Answer in English only unless the user writes in another language.

How to work:
- If a tool is needed, reply with ONLY a JSON object and nothing else:
  {"action": "tool_name", "params": {...}}
- You may request several tool calls in one reply, one JSON object per line.
- If no tool is needed, reply with plain prose for the user. No JSON.
- After each round you will be shown what the tools actually did. Use those
  results to decide the next step, and to fix anything that failed.
- Do the whole job. If the user asked for several things, keep going until it
  is done rather than stopping after the first step.
- When the work is finished, reply with a short summary in prose. No JSON.

Available tools:
{catalogue}

Rules:
- Never invent a tool name or a parameter. Only use the ones listed above.
- Never claim an action happened unless an observation confirms it.
- Never answer from memory what a tool can tell you: the current time, date,
  or any fact about this machine must come from the tool, not from you.
  If the user asks what time or date it is, call the time tool first.
- If a tool was refused or denied, say so plainly; do not pretend otherwise.
"""

SYSTEM_AR = """\
انت زيتون، مساعد سطح مكتب بيقدر يستخدم أدوات.

طريقة الشغل:
- لو محتاج أداة، رد بـ JSON object بس وخلاص:
  {"action": "اسم_الأداة", "params": {...}}
- ينفع تطلب أكتر من أداة في نفس الرد، كل واحدة JSON object في سطر.
- لو مش محتاج أداة، اكتب كلام عادي للمستخدم. بدون JSON.
- بعد كل جولة هتشوف الأدوات عملت إيه فعلاً. استخدم النتيجة عشان تقرر الخطوة
  اللي بعدها، وتصلّح أي حاجة فشلت.
- كمّل الشغل. لو المستخدم طلب كذا حاجة، كمّل لحد ما تخلص، مش بس أول خطوة.
- لما تخلص، رد بملخص قصير بالكلام. بدون JSON.

الأدوات المتاحة:
{catalogue}

قواعد:
- متخترعش اسم أداة ولا بارامتر. استخدم الموجود في القائمة بس.
- متقولش إنك عملت حاجة إلا لو النتيجة بتأكد.
- متعرّفش من عندك حاجة الأدوات تقدر تقولها: الوقت والتاريخ وأي حقيقة عن
  الجهاز لازم تيجي من الأداة مش منك. لو المستخدم سأل الوقت، نادِ أداة الوقت
  الأول ومتقولش وقتك أنت.
- لو أداة اترفضت أو اتمنعت، قولها بصراحة، متدّعيش إنك عملتها.

مهم جداً (آخر كلام): رد على المستخدم بالعربي المصري فقط. ممنوع الرد
بالإنجليزية مهما كانت لغة نتيجة الأداة — ترجم النتيجة واكتب الرد بالعربي.
"""


def build_system_prompt(catalogue: str, language: str = "en") -> str:
    """Render the tool-loop system prompt.

    The templates contain literal braces — they are *not* ``str.format``
    templates (``{catalogue}`` is substituted by name), so they must show the
    model single braces. They previously showed ``{{...}}``; a model copied the
    doubled braces verbatim, ``json.loads`` rejected them, and raw tool JSON
    reached the user as the assistant's answer.
    """
    template = SYSTEM_AR if language == "ar" else SYSTEM_EN
    return template.replace("{catalogue}", catalogue)


#: Shown when the model twice replies in a format neither scanner understands.
#: Deliberately does not quote the offending text back at the user.
_MALFORMED_REPLY = {
    "en": (
        "I could not get a usable response from the language model that time. "
        "Please try rephrasing your request."
    ),
    "ar": "مقدرتش أجيب رد مفهوم من الموديل المرة دي. جرّب تطلب تاني.",
}

#: Shown when every compatible FREE model was rate-limited or produced a
#: completion that failed validation. The user must never be shown the last
#: invalid completion instead of this.
_ALL_MODELS_FAILED = {
    "en": (
        "All available free AI models are temporarily unavailable or unable to "
        "complete this operation. Please try again in a moment."
    ),
    "ar": "كل الموديلات المجانية دلوقتي مشغولة أو مقدرش تكمّل العملية. جرّب تاني بعد شوية.",
}

#: An *unfilled template* is not an answer. Live run: the Arabic tool turn came
#: back as ``الساعة الآن هي: [أكتب الوقت الحالي]`` — the model wrote a form to
#: fill in instead of calling the tool, and that text was on its way to the
#: user's screen. Narrow on purpose: only bracketed "write/enter/اكتب…"-style
#: slots count, so legitimate prose containing a citation is untouched.
_PLACEHOLDER_RE = re.compile(
    r"\[\s*(?:أكتب|اكتب|أدخل|write|enter|insert|fill|put|your\b|xxx+)",
    re.IGNORECASE,
)


def is_placeholder_reply(text: str) -> bool:
    """Whether ``text`` is an unfilled template rather than an answer."""
    return bool(_PLACEHOLDER_RE.search(text or ""))


#: Model-to-model nudge for a placeholder that came *after* a tool already ran.
#: The generic "_TOOL_EXPECTED_NAG" would be wrong here — a tool did run — so
#: this one points at the observation and asks for prose built from it.
_PLACEHOLDER_NAG = {
    "en": (
        "Your last reply was an unfilled template such as "
        "'[write the time here]', which is not an answer. Use the tool result "
        "shown above and write the actual final answer in prose — no brackets, "
        "no placeholders."
    ),
    "ar": (
        "آخر رد كان قالب فاضي زي [اكتب الوقت هنا]، وده مش رد. استخدم نتيجة "
        "الأداة اللي فوق واكتب الرد النهائي كلام عادي — من غير أقواس ولا قوالب."
    ),
}


#: The second and final corrective round. The first nag explains *what* a tool
#: call looks like; this one insists on it, because a live run against the
#: local 3B model ignored the first nag twice and shipped a fabricated clock
#: reading ("الساعة الآن هي: 12:34:56" while the real time was 12:57).
_TOOL_NAG_FINAL = {
    "en": (
        "Second and final attempt: you still answered in prose and this "
        "request needs a tool. Output ONLY the JSON tool call for the tool "
        "from the list that answers the request — for example "
        '{"action": "get_time"} for time questions. Do not write prose and '
        "do not state any fact from memory."
    ),
    "ar": (
        "المحاولة التانية والأخيرة: لسه رديت كلام والطلب ده محتاج أداة. "
        "اكتب بس استدعاء الأداة بصيغة JSON من القائمة اللي فوق — مثلاً "
        '{"action": "get_time"} لأسئلة الوقت. متكتبش كلام عادي ومتقولش '
        "أي حقيقة من عندك."
    ),
}

#: Shown when the model answered in prose three times in a row on a turn that
#: was routed to the tool agent. Delivering that prose is how a guessed fact
#: reaches the user, so it is refused — honestly, in the user's language.
_NO_TOOL_TAKEN = {
    "en": (
        "I couldn't complete that: the request needs a tool to run, and I "
        "couldn't get one this time. Please try again or rephrase it."
    ),
    "ar": (
        "مقدرتش أكمّل: الطلب محتاج أداة تتنفذ، وملقيتهاش المرة دي. جرّب تاني "
        "أو صيغله بطريقة تانية."
    ),
}

#: Sent back to the model (never to the user) when a request was routed to the
#: tool agent but the model answered in prose on the first attempt. A live run
#: had it reply ``الساعة الآن هي: [أكتب الوقت الحالي]`` — a template placeholder
#: instead of calling ``get_time`` — so the nudge names both failure modes.
_TOOL_EXPECTED_NAG = {
    "en": (
        "That reply used no tool, but this request was routed to the tool "
        "agent because it needs one. Try again: send a tool call as "
        '{"action": "tool_name", "params": {...}} — use get_time for time or '
        "date questions. Never answer with a placeholder such as "
        "'[write the time here]' and never invent a value; the observation "
        "must come from the tool. If you are certain no tool applies, answer "
        "the user in plain prose."
    ),
    "ar": (
        "الرد ده ماستخدمش أداة، بس الطلب ده وصل لوكيل الأدوات لأنه محتاجها. "
        "جرّب تاني: ابعت استدعاء أداة بالمطلوب — لأسئلة الوقت والتاريخ استخدم "
        "get_time. متردش بقالب زي [اكتب الوقت هنا] ومتخترعش قيمة؛ لازم النتيجة "
        "تيجي من الأداة. لو متأكد إن مفيش أداة مناسبة، رد على المستخدم بكلام عادي."
    ),
}


# ---------------------------------------------------------------------------
# User-facing labels
# ---------------------------------------------------------------------------
#: Tool name -> a short, present-tense label for the UI. These are what the
#: user sees instead of the model's reasoning, and they are written here rather
#: than derived from the model precisely so nothing private leaks.
_LABELS_EN: Dict[str, str] = {
    "file_read": "Reading a file",
    "file_write": "Writing a file",
    "create_folder": "Creating a folder",
    "file_delete": "Deleting",
    "file_move": "Moving files",
    "search_files": "Searching the code",
    "system_command": "Running a command",
    "run_code": "Running code",
    "type_text": "Typing",
    "key_press": "Sending a keypress",
    "open_app": "Opening an application",
    "close_app": "Closing an application",
    "get_time": "Checking the time",
    "system_info": "Reading system information",
    "calculator": "Calculating",
    "get_volume": "Checking audio",
    "screenshot": "Taking a screenshot",
    "search_web": "Searching the web",
    "open_url": "Opening a page",
    "git_status": "Checking repository status",
    "git_log": "Reading commit history",
    "git_diff": "Reviewing changes",
    "git_branch_list": "Listing branches",
    "git_add": "Staging changes",
    "git_commit": "Committing",
    "git_checkout": "Switching branch",
    "git_push": "Pushing",
    "create_project": "Creating the project",
    "run_npm": "Running npm",
    "run_tests": "Running tests",
    "play_media": "Controlling media",
}

_LABELS_AR: Dict[str, str] = {
    "file_read": "بقرأ ملف",
    "file_write": "أكتب ملف",
    "create_folder": "أنشئ مجلد",
    "file_delete": "أحذف",
    "file_move": "أنقل ملفات",
    "search_files": "أدور في الكود",
    "system_command": "أنفّذ أمر",
    "run_code": "شغّل كود",
    "type_text": "أكتب نص",
    "key_press": "أبعت زر",
    "open_app": "أفتح برنامج",
    "close_app": "أقفل برنامج",
    "get_time": "أشوف الوقت",
    "system_info": "أقرأ معلومات الجهاز",
    "calculator": "أحسب",
    "get_volume": "أشوف الصوت",
    "screenshot": "أخد صورة للشاشة",
    "search_web": "أبحث في النت",
    "open_url": "أفتح صفحة",
    "git_status": "أشوف حالة المستودع",
    "git_log": "أقرأ سجل الكوميتات",
    "git_diff": "أراجع التغييرات",
    "git_branch_list": "أعرض الفروع",
    "git_add": "أجهّز التغييرات",
    "git_commit": "أعمل كوميت",
    "git_checkout": "أبدّل الفرع",
    "git_push": "أرفع Changes",
    "create_project": "أنشئ المشروع",
    "run_npm": "أنفّذ npm",
    "run_tests": "أشغّل الاختبارات",
    "play_media": "أتحكم في الوسائط",
}

#: A known tool can announce a plan; an unknown one is reported honestly rather
#: than silently no-op'd.
def tool_label(name: str, language: str = "en") -> str:
    table = _LABELS_AR if language == "ar" else _LABELS_EN
    return table.get(name, name.replace("_", " "))


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------
@dataclass
class LoopResult:
    """Outcome of one agent turn."""

    text: str
    iterations: int = 0
    tool_calls: List[str] = field(default_factory=list)
    #: Tools that were dispatched but did not succeed.
    failed_calls: List[str] = field(default_factory=list)
    stopped_reason: str = "answered"
    errors: List[str] = field(default_factory=list)

    @property
    def used_tools(self) -> bool:
        return bool(self.tool_calls)


class AgentEngine:
    """A bounded tool-using loop over a :class:`ToolRegistry`.

    Deliberately not an autonomous agent. It has no persistent memory, no
    self-directed goal, and a hard iteration budget; it runs one request, uses
    tools until the request is satisfied, and returns.
    """

    #: The provider exception behind the most recent failed `_ask`, if any.
    #: A class default so the attribute always exists, even in a subclass that
    #: does not call `__init__`; the instance value is set per turn.
    last_llm_error: Optional[BaseException] = None

    def __init__(
        self,
        llm: Any,
        registry: ToolRegistry,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        metrics: Optional[Any] = None,
    ) -> None:
        #: The exception behind the most recent failed `_ask`, if any. Per-turn
        #: state on a per-turn object; see the comment in `_ask`.
        self.last_llm_error = None
        self.llm = llm
        self.registry = registry
        self.max_iterations = max(1, int(max_iterations))
        #: Execution metrics (increment C). Defaults to the process-wide
        #: instance that ``GET /health`` reads; a test can pass its own to
        #: assert on a clean slate instead of on shared global state.
        self.metrics = metrics if metrics is not None else get_tool_metrics()
        # Built once per turn from the registry's real schemas, so the
        # validator checks tool names and argument types against what actually
        # exists rather than against a hand-written list.
        self._tool_contract = contract_from_registry(
            registry,
            allow_prose=True,
            label="agent_turn",
            # A wrong tool name or a wrong argument is exactly what the
            # observation loop exists to correct: the registry refuses the call
            # by name and the model is shown the refusal. Rejecting the
            # completion here instead would spend the whole attempt budget on a
            # recoverable slip and end the turn with "all models are
            # unavailable" — which is what a live run did, twice, on a local
            # 3B model inventing a plausible tool name.
            allow_unknown_tools=True,
        )

    def _contract(self, require_tool: bool) -> CompletionContract:
        """The contract for one iteration.

        Iteration 1 legitimately may be answered in prose ("you don't need a
        tool for that"), so prose is always allowed. ``require_tool`` is set only
        when the *model's own output showed* that it was going to act — a
        malformed tool call it has to be told about — which is the one case where
        insisting on a real call is unambiguously right.
        """
        return replace(self._tool_contract, require_tool=require_tool)

    # ------------------------------------------------------------------
    async def run(
        self,
        user_message: str,
        context: Optional[List[Dict[str, Any]]] = None,
        language: str = "en",
        bus: Optional[EventBus] = None,
        approval: Optional[Callable[..., Any]] = None,
        response_id: str = "",
        expect_tool: bool = False,
    ) -> LoopResult:
        """Run the loop to completion.

        ``approval`` is the caller's own gate — an async callable taking
        ``(tool_name, params)`` and returning a bool, or a refusal string to
        return to the model. The engine does not own the human-in-the-loop
        policy; it owns the loop.

        ``expect_tool`` says the caller routed this request *because* it needs
        a tool. Prose answers are then corrected twice (call-format nag, then
        a final insistence) before being **refused**: a small local model will
        otherwise hand back a template placeholder
        (``الساعة الآن هي: [أكتب الوقت الحالي]``) or a fabricated fact instead
        of calling the tool. After two ignored nags the reply is replaced with
        an honest bilingual failure, so a misrouted no-tool request terminates
        with an explanation instead of burning the attempt budget — and a
        guessed value is never delivered.
        """
        bus = bus or NullBus()
        catalogue = self._catalogue(language)
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": build_system_prompt(catalogue, language)}
        ]
        for turn in (context or [])[-4:]:
            if turn.get("role") in ("user", "assistant") and turn.get("content"):
                messages.append({"role": turn["role"], "content": turn["content"]})
        messages.append({"role": "user", "content": user_message})

        await bus.thinking()

        tool_names: List[str] = []
        failed: List[str] = []
        errors: List[str] = []
        last_prose = ""
        #: One correction is allowed for a malformed reply. More than one and a
        #: confused model just burns the budget.
        nagged = False
        #: Corrective rounds spent because a tool was expected and prose came
        #: back instead. Two are allowed (``_TOOL_EXPECTED_NAG`` then
        #: ``_TOOL_NAG_FINAL``); the third prose reply is refused rather than
        #: delivered, because that is the path a fabricated fact takes to the
        #: user's screen. Separate from ``nagged`` so a malformed reply and a
        #: skipped tool each get their own allowance.
        tool_nags = 0
        #: One corrective round for a placeholder that came *after* a tool
        #: already ran (the no-tool case is handled by ``tool_nags``).
        placeholder_nagged = False
        #: Set when the user's gate refused a call in any earlier iteration.
        #: After a *deliberate* refusal, prose is the right ending: the tool
        #: must not run, so demanding another call only nags the model into
        #: retrying (re-prompting the user) until the turn dies as
        #: ``no_tool_taken`` — a confusing "I couldn't complete that" for a
        #: turn the user chose to cancel. Only a real refusal sets this; a
        #: model dodging a needed tool with prose cannot fake one, because
        #: the flag is written solely from the gate's own decision.
        gate_refused = False

        for iteration in range(1, self.max_iterations + 1):
            raw = await self._ask(messages)
            if raw is None:
                # No model could answer. Report that, and nothing else -- never
                # the last invalid text.
                #
                # "Free models" is only the truth for the OpenRouter path, and a
                # user whose `ollama serve` is stopped does not need to "try
                # again in a moment": they need the command that fixes it. The
                # client already knows which of those it is, so ask it, and fall
                # back to the generic notice when it has nothing better to say.
                message = user_notice(self.last_llm_error, language) or _ALL_MODELS_FAILED.get(
                    language, _ALL_MODELS_FAILED["en"]
                )
                errors.append(
                    str(getattr(self.last_llm_error, "kind", "pool_exhausted"))
                )
                await bus.error(message)
                await bus.response_chunk(message, response_id=response_id)
                await bus.completed(reason="llm_unavailable", iterations=iteration)
                return LoopResult(
                    text=message, iterations=iteration,
                    stopped_reason="llm_unavailable",
                    errors=errors,
                )

            calls, prose = extract_tool_calls(raw)

            if not calls:
                # A reply that is *still* shaped like a tool call is a parse
                # failure, not an answer. Returning it verbatim means the user
                # sees "<tool_call>get_time</tool_call>" as the assistant's
                # reply, which is what the live run did before the tagged form
                # was taught to the parser. Nudge the model once; if it
                # misbehaves again, say so rather than passing it on.
                if looks_like_tool_call(prose or raw):
                    if not nagged:
                        nagged = True
                        errors.append(
                            "the model replied in an unrecognised tool-call format"
                        )
                        messages.append({"role": "assistant", "content": raw})
                        messages.append({
                            "role": "user",
                            "content": (
                                "That was not a reply I could use. Either send a "
                                'tool call as {"action": "tool_name", "params": '
                                "{...}} or answer the user in plain prose. "
                                "Do not use XML tags."
                            ),
                        })
                        continue

                    # Second offence: stop. Reporting a malformed response is
                    # honest; echoing the raw markup is not. Falling through to
                    # the "no tool call, so this is the answer" branch here is
                    # exactly the bug this guard exists to prevent.
                    errors.append(
                        "the model repeated an unrecognised tool-call format"
                    )
                    answer = _MALFORMED_REPLY.get(language, _MALFORMED_REPLY["en"])
                    await bus.error(answer)
                    await bus.response_chunk(answer, response_id=response_id)
                    await bus.completed(reason="malformed_response", iterations=iteration)
                    return LoopResult(
                        text=answer,
                        iterations=iteration,
                        tool_calls=tool_names,
                        failed_calls=failed,
                        stopped_reason="malformed_response",
                        errors=errors,
                    )

                # A tool was expected and prose came back. Correct the model:
                # the first nag explains the call format, the second insists
                # on it. Two ignored nags and the prose is refused — see the
                # refusal below — so a guessed fact is never delivered.
                # A gate refusal exempts this: the user already declined, so
                # prose declining in turn *is* the answer.
                if expect_tool and not tool_names and not gate_refused:
                    if tool_nags >= 2:
                        # Third prose answer with no tool call. Delivering it
                        # is exactly how the fabricated clock reading above
                        # reached the user; report the failure instead.
                        answer = _NO_TOOL_TAKEN.get(
                            language, _NO_TOOL_TAKEN["en"]
                        )
                        errors.append(
                            "no tool call after two corrective rounds"
                        )
                        await bus.error(answer)
                        await bus.response_chunk(
                            answer, response_id=response_id
                        )
                        await bus.completed(
                            reason="no_tool_taken", iterations=iteration
                        )
                        return LoopResult(
                            text=answer,
                            iterations=iteration,
                            tool_calls=tool_names,
                            failed_calls=failed,
                            stopped_reason="no_tool_taken",
                            errors=errors,
                        )
                    nag = _TOOL_EXPECTED_NAG if tool_nags == 0 else _TOOL_NAG_FINAL
                    tool_nags += 1
                    messages.append({"role": "assistant", "content": raw})
                    messages.append({
                        "role": "user",
                        "content": nag.get(language, nag["en"]),
                    })
                    continue

                # No tool call: this is the answer. Stream it as prose.
                answer = prose or raw
                if expect_tool and is_placeholder_reply(answer):
                    if not placeholder_nagged:
                        # The tool ran; only the summary is a template. One
                        # corrective round pointing at the observation.
                        placeholder_nagged = True
                        messages.append({"role": "assistant", "content": raw})
                        messages.append({
                            "role": "user",
                            "content": _PLACEHOLDER_NAG.get(
                                language, _PLACEHOLDER_NAG["en"]
                            ),
                        })
                        continue
                    # An unfilled template is not an answer, and this is the
                    # second one. Report the failure honestly instead of
                    # shipping a form to fill in.
                    answer = _MALFORMED_REPLY.get(
                        language, _MALFORMED_REPLY["en"]
                    )
                    errors.append(
                        "the model returned a placeholder instead of acting"
                    )
                    await bus.error(answer)
                await bus.response_chunk(answer, response_id=response_id)
                await bus.completed(reason="answered", iterations=iteration)
                return LoopResult(
                    text=answer,
                    iterations=iteration,
                    tool_calls=tool_names,
                    failed_calls=failed,
                    stopped_reason="answered",
                    errors=errors,
                )

            if iteration == 1 and len(calls) > 1:
                # A multi-tool opening request is worth surfacing as a plan.
                await bus.planning([tool_label(c.name, language) for c in calls])

            messages.append({"role": "assistant", "content": raw})

            observations: List[str] = []
            # Increment B: safe-tier calls in a batch run concurrently; every
            # confirm/dangerous call is a barrier that runs on its own, in the
            # order the model wrote it. Results are keyed by index and folded
            # back in that original order, so `observations`, `tool_calls` and
            # `failed_calls` describe what happened in the sequence the model
            # asked for — never in completion order. The start/finish frames
            # are emitted inside each task, so the feed still shows progress
            # live instead of every tool finishing at once.
            outcomes = await self._run_batch(
                calls, approval, bus, language,
                # Increment F: what the user actually asked for, so the audit
                # reads as a history of requests rather than of anonymous
                # commands. Passed down instead of stored on `self` because one
                # engine can serve overlapping turns.
                audit={"trace_id": response_id, "user_request": user_message},
            )
            for call, outcome in zip(calls, outcomes):
                if isinstance(outcome, str):
                    # The gate declined; the model is told, not the user.
                    gate_refused = True
                    observations.append(render_observation(call, {"error": outcome, "cancelled": True}))
                    continue

                if not outcome.get("success"):
                    errors.append(f"{call.name}: {outcome.get('error') or 'failed'}")
                observations.append(render_observation(call, outcome))
                # `tool_calls` is every tool dispatched, so the UI can show the
                # full activity trail; `failed_calls` is what did not work, so a
                # caller can tell "nothing happened" from "it tried and failed".
                if self.registry.has(call.name):
                    tool_names.append(call.name)
                    if not outcome.get("success"):
                        failed.append(call.name)

            last_prose = prose
            messages.append({
                "role": "user",
                "content": (
                    "Tool results:\n"
                    + "\n".join(observations)
                    + "\n\nContinue with the next step, or reply with a final "
                    "summary in prose if the work is done."
                ),
            })

        # Budget exhausted. Ask once more for a summary, without tools, so the
        # user gets an answer rather than a truncated transcript.
        await bus.planning([], reason="budget")
        final = await self._ask(
            messages
            + [{"role": "user", "content": "Summarise what was accomplished so far, in prose."}],
            force_prose=True,
        )
        answer = final or last_prose or "I ran out of steps before finishing that."
        await bus.response_chunk(answer, response_id=response_id)
        await bus.completed(reason="max_iterations", iterations=self.max_iterations)
        return LoopResult(
            text=answer,
            iterations=self.max_iterations,
            tool_calls=tool_names,
            failed_calls=failed,
            stopped_reason="max_iterations",
            errors=errors,
        )

    # ------------------------------------------------------------------
    def _catalogue(self, language: str = "en") -> str:
        """The tool list the model sees: name, tier, and required parameters."""
        lines = []
        for tool in self.registry.tools():
            required = tool.schema().get("required", [])
            signature = ", ".join(required) if required else "(no parameters)"
            # `name` and `description` are abstract *methods* on BaseTool, not
            # properties. Without the parens Python formats the bound method
            # itself, so the model was being handed
            #   - <bound method Calculator.name of <Calculator ...>>([safe]) <bound ...>
            # and could not tell a single tool apart. Caught by driving the real
            # registry; the unit test passed only because the test double's
            # __repr__ happened to contain its name. There is now a regression
            # test asserting no line contains "bound method".
            lines.append(
                f"- {tool.name()}({signature}) [{tool.permission}] {tool.description()}"
            )
        return "\n".join(lines)

    async def _ask(
        self,
        messages: List[Dict[str, str]],
        force_prose: bool = False,
        require_tool: bool = False,
    ) -> Optional[str]:
        """One LLM call, validated. Returns ``None`` if no free model could answer.

        Never streams: the intermediate output is a tool call, and streaming it
        would put raw JSON on the user's screen. The final prose is streamed by
        the caller, from the parsed result.

        The contract is what makes this safe. The client will walk the
        capability-filtered free pool and only return a completion that passes
        it; a 200 that fails is recorded, benned and skipped. If every model in
        the budget is exhausted we get ``None`` and the loop reports that
        honestly rather than passing along the last invalid text.
        """
        self.last_llm_error = None
        try:
            return await self.llm.complete(
                messages,
                tier=self.llm.tier if hasattr(self.llm, "tier") else None,
                max_tokens=2048,
                temperature=0.1,
                contract=self._contract(require_tool),
                extract=extract_tool_calls,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # One structured line, no traceback in the return value: `None`
            # already means "no model could answer", and the loop reports that
            # honestly rather than echoing an exception at the user.
            #
            # The exception itself is kept on the instance (never returned) so
            # the caller can name the *reason*. `AgentEngine` is built per turn
            # (see `agents/tool_agent.py`), so this cannot leak between users.
            safe_error(e, operation="llm.complete", component="agents.engine")
            self.last_llm_error = e
            return None

    async def _run_batch(
        self,
        calls: List["ToolCall"],
        approval: Optional[Callable[..., Any]],
        bus: EventBus,
        language: str,
        audit: Optional[Dict[str, Any]] = None,
    ) -> List[Any]:
        """Run one round's tool calls, returning one outcome per call.

        Two groups, one fold:

        * a **run** of consecutive safe-tier calls executes concurrently under
          :data:`PARALLEL_SAFE_TOOLS`, because they have no ordering
          relationship with each other and reading three files should take one
          file's worth of time;
        * every confirm/dangerous call is a **barrier**: it runs alone, and
          nothing written after it starts until it finishes.

        The barrier rule is what keeps the fold honest. Results are returned
        positionally, so `observations`, `tool_calls` and `failed_calls` are in
        the order the model asked for. If a read were allowed to overtake the
        `create_folder` that precedes it, the observation list would claim the
        read happened after the folder existed while its own text said
        "not found" — the model would then reason about an impossible sequence.

        Outcomes are the registry's result dict, or a refusal **string** when
        the caller's gate declined (kept distinct so the fold can tell
        "declined" apart from "failed").
        """
        outcomes: List[Any] = [None] * len(calls)
        budget = asyncio.Semaphore(PARALLEL_SAFE_TOOLS)
        index = 0
        while index < len(calls):
            if self._needs_approval(calls[index]):
                outcomes[index] = await self._run_one(
                    calls[index], approval, bus, language, budget=None, audit=audit
                )
                index += 1
                continue
            end = index
            while end < len(calls) and not self._needs_approval(calls[end]):
                end += 1
            group = calls[index:end]
            if len(group) == 1:
                outcomes[index] = await self._run_one(
                    group[0], approval, bus, language, budget=budget, audit=audit
                )
            else:
                # `gather` without `return_exceptions`: a child that raises
                # must cancel its siblings rather than leave them running, and
                # CancelledError (an interrupt) has to reach the caller. The
                # failure path that matters here is handled inside `_run_one`.
                outcomes[index:end] = list(
                    await asyncio.gather(
                        *[
                            self._run_one(c, approval, bus, language, budget=budget, audit=audit)
                            for c in group
                        ]
                    )
                )
            index = end
        return outcomes

    async def _run_one(
        self,
        call: "ToolCall",
        approval: Optional[Callable[..., Any]],
        bus: EventBus,
        language: str,
        budget: Optional[asyncio.Semaphore] = None,
        audit: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Invoke one call: progress frames in, an outcome and metrics out.

        Three things happen around the actual call, and none of them may
        interfere with it:

        * a **heartbeat** task emits ``tool_progress`` every
          :data:`TOOL_HEARTBEAT_S` while the call is in flight, so a slow tool
          reads as "still working" rather than as a frozen UI. It is cancelled
          in the ``finally``, because a heartbeat that outlives its tool is a
          permanent lie on the wire.
        * the **start/finish frames** are emitted here rather than in the fold,
          so concurrent siblings interleave honestly.
        * **metrics** are recorded through :func:`tools.metrics.record_tool_call`,
          which swallows its own failures: instrumentation must never be able
          to fail the call it is measuring.
        """

        async def _body() -> Any:
            started_at = time.perf_counter()
            beat: Optional[asyncio.Task] = None
            status = STATUS_FAILED
            emitted = False
            #: Bound before the try so the audit hook in `finally` can always
            #: read it, including on the cancellation path.
            outcome: Any = None
            try:
                await bus.tool_started(
                    call.name, label=tool_label(call.name, language)
                )
                if self.metrics is not None:
                    # Instrumentation must never fail the call it measures.
                    try:
                        self.metrics.begin(call.name)
                    except Exception:  # pragma: no cover - defensive
                        pass
                beat = asyncio.create_task(
                    _heartbeat(call.name, bus, language), name=f"beat:{call.name}"
                )
                outcome: Any = await self._invoke(call, approval, bus, language)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never escapes the batch
                # `_invoke` should not raise (the registry turns tool failures
                # into results), but if it ever does the user gets a structured
                # failure instead of a dead turn. This branch also guarantees
                # that a concurrent sibling can never take the batch down with
                # it: `gather` is used without `return_exceptions`, so a child
                # that raises would cancel the others.
                outcome = {
                    "success": False,
                    "error": safe_error(
                        exc, operation=call.name, component="agents.engine"
                    ),
                }

            try:
                if isinstance(outcome, str):
                    status = STATUS_DECLINED
                    await bus.tool_finished(call.name, False, summary=outcome)
                elif outcome.get("success"):
                    status = STATUS_OK
                    # `mutates`/`params` give the turn's recorder what it
                    # needs to record a real file change — the tool's own
                    # declaration plus the call's arguments — without the
                    # recorder needing the registry (or re-deriving
                    # "which tools mutate" from names, which never agrees
                    # with the aliases). Attached on success only: a tool
                    # that failed or was declined changed nothing. Both are
                    # internal event fields — the transport builds its frames
                    # from the fields it already knows, so neither reaches
                    # the wire.
                    resolved = self.registry.get(call.name)
                    await bus.tool_finished(
                        call.name, True,
                        summary=str(outcome.get("output") or "")[:200],
                        params=dict(call.params or {}),
                        mutates=bool(getattr(resolved, "mutates_files", False)),
                    )
                elif outcome.get("timeout"):
                    status = STATUS_TIMED_OUT
                    await bus.tool_finished(
                        call.name, False,
                        summary=str(outcome.get("error") or "timed out")[:200],
                    )
                else:
                    status = STATUS_FAILED
                    await bus.tool_finished(
                        call.name, False,
                        summary=str(outcome.get("error") or "failed")[:200],
                    )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a display must not break a turn
                pass
            finally:
                if beat is not None:
                    beat.cancel()
                    try:
                        await beat
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
                elapsed_ms = (time.perf_counter() - started_at) * 1000.0
                record_tool_call(
                    self.metrics, call.name, status, elapsed_ms,
                )
                await self._audit_call(call, outcome, status, elapsed_ms, audit)
            return outcome

        if budget is None:
            return await _body()
        async with budget:
            return await _body()

    async def _audit_call(
        self,
        call: ToolCall,
        outcome: Any,
        status: str,
        elapsed_ms: float,
        context: Optional[Dict[str, Any]],
    ) -> None:
        """Write one execution record. Never raises, never blocks the turn.

        The approval decision is *inferred* from the outcome rather than asked
        of the gate: a refusal string means a human said no, and a result that
        reports it reached execution means the prompt was answered. Guessing
        beats threading the gate's private decision through the loop, and the
        distinction that matters — asked-and-approved vs. declined — survives.
        """
        params = call.params or {}
        data = outcome.get("data") if isinstance(outcome, dict) else None
        approved: Optional[bool]
        if isinstance(outcome, str):
            approved = False                      # the gate declined
        elif isinstance(data, dict) and data.get("requires_confirmation"):
            approved = True                       # asked, and approved
        else:
            approved = None                       # ran without asking

        try:
            tier = self.registry.assessor.permission_for(call.name)
        except Exception:  # noqa: BLE001 - auditing must not depend on this
            tier = "unknown"

        exit_code = data.get("exit_code") if isinstance(data, dict) else None
        await audit_record(
            tool=call.name,
            trace_id=(context or {}).get("trace_id"),
            user_request=(context or {}).get("user_request"),
            command=params.get("command") or params.get("code"),
            cwd=params.get("cwd") or params.get("working_directory"),
            risk=tier,
            approved=approved,
            success=status == STATUS_OK,
            exit_code=exit_code if isinstance(exit_code, int) else None,
            duration_ms=elapsed_ms,
            error=outcome.get("error") if isinstance(outcome, dict) else outcome,
        )

    async def _invoke(
        self,
        call: ToolCall,
        approval: Optional[Callable[..., Any]],
        bus: EventBus,
        language: str,
    ) -> Any:
        """Run one tool call through the caller's gate and the registry.

        The approval callback is consulted **only** when the registry says this
        particular call reaches the gate. Calling it for every tool would mean
        plumbing a confirmation channel through for `get_time`, and the refusal
        branch would have to tell "declined" apart from "never asked".

        Returns the registry's result dict, or a string when the caller's gate
        declined (in which case the string is the model's explanation).
        """
        if not self.registry.has(call.name):
            known = ", ".join(self.registry.list()[:10])
            return {
                "success": False,
                "error": (
                    f"Unknown tool '{call.name}'. Available tools include: {known}."
                ),
            }

        if approval is not None and self._needs_approval(call):
            try:
                decision = approval(call.name, call.params)
                if hasattr(decision, "__await__"):
                    decision = await decision
            except asyncio.CancelledError:
                raise
            except Exception as e:
                safe_error(
                    e, operation="approval", component="agents.engine",
                    detail=f"for {call.name}",
                )
                decision = False
            if decision is not True:
                reason = (
                    decision if isinstance(decision, str)
                    else "The user did not approve this action."
                )
                return reason

        try:
            return await asyncio.wait_for(
                self.registry.execute(call.name, call.params, approval=True),
                timeout=TOOL_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            return {
                "success": False,
                "timeout": True,
                "error": f"'{call.name}' did not finish within {TOOL_TIMEOUT_S}s.",
            }

    def _needs_approval(self, call: "ToolCall") -> bool:
        """Whether this specific call would reach the permission gate.

        Asked of the registry rather than re-derived here, so the engine and
        the gate can never disagree about what needs a prompt.
        """
        try:
            from tools.safety import RiskLevel

            return self.registry.assess(call.name, call.params).level is RiskLevel.HIGH
        except Exception:  # pragma: no cover - defensive
            # If we cannot tell, ask. Failing toward a prompt is the safe side.
            return True
