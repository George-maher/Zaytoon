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

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from agents.events import AgentEventType, EventBus, NullBus
from tools.registry import ToolRegistry

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
    """
    if not text:
        return [], ""

    calls: List[ToolCall] = []
    consumed: List[Tuple[int, int]] = []

    for start, end, raw in _iter_json_objects(text):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(parsed, dict):
            continue

        name = next(
            (parsed[k] for k in _ACTION_KEYS if isinstance(parsed.get(k), str)),
            None,
        )
        if not name:
            continue

        params = parsed.get("params")
        if not isinstance(params, dict):
            # Flat form: {"action": "x", "path": "y"}
            params = {k: v for k, v in parsed.items() if k not in _ACTION_KEYS}
        calls.append(ToolCall(name=name.strip(), params=params, raw=raw))
        consumed.append((start, end))

    if not consumed:
        return [], text

    # Reassemble the prose, dropping the JSON blocks.
    parts: List[str] = []
    cursor = 0
    for start, end in consumed:
        parts.append(text[cursor:start])
        cursor = end
    parts.append(text[cursor:])
    leftover = "".join(parts).strip()
    # Strip a code fence left behind by the removed block.
    leftover = leftover.replace("```json", "").replace("```", "").strip()
    return calls, leftover


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

How to work:
- If a tool is needed, reply with ONLY a JSON object and nothing else:
  {{"action": "tool_name", "params": {{...}}}}
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
- If a tool was refused or denied, say so plainly; do not pretend otherwise.
"""

SYSTEM_AR = """\
انت زيتون، مساعد سطح مكتب بيقدر يستخدم أدوات.

طريقة الشغل:
- لو محتاج أداة، رد بـ JSON对象 بس وخلاص:
  {{"action": "اسم_الأداة", "params": {{...}}}}
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
- لو أداة اترفضت أو اتمنعت، قولها بصراحة، متدّعيش إنك عملتها.
"""


def build_system_prompt(catalogue: str, language: str = "en") -> str:
    template = SYSTEM_AR if language == "ar" else SYSTEM_EN
    return template.replace("{catalogue}", catalogue)


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

    def __init__(
        self,
        llm: Any,
        registry: ToolRegistry,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.max_iterations = max(1, int(max_iterations))

    # ------------------------------------------------------------------
    async def run(
        self,
        user_message: str,
        context: Optional[List[Dict[str, Any]]] = None,
        language: str = "en",
        bus: Optional[EventBus] = None,
        approval: Optional[Callable[..., Any]] = None,
        response_id: str = "",
    ) -> LoopResult:
        """Run the loop to completion.

        ``approval`` is the caller's own gate — an async callable taking
        ``(tool_name, params)`` and returning a bool, or a refusal string to
        return to the model. The engine does not own the human-in-the-loop
        policy; it owns the loop.
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

        for iteration in range(1, self.max_iterations + 1):
            raw = await self._ask(messages)
            if raw is None:
                # The provider failed. Nothing can be recovered; say so.
                message = errors[-1] if errors else "The language model is unavailable."
                await bus.error(message)
                await bus.completed(reason="llm_unavailable")
                return LoopResult(
                    text=message, iterations=iteration, stopped_reason="llm_unavailable",
                    errors=errors or [message],
                )

            calls, prose = extract_tool_calls(raw)

            if not calls:
                # No tool call: this is the answer. Stream it as prose.
                answer = prose or raw
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
            for call in calls:
                label = tool_label(call.name, language)
                await bus.tool_started(call.name, label=label)

                outcome = await self._invoke(call, approval, bus, language)
                if isinstance(outcome, str):
                    # The gate declined; the model is told, not the user.
                    await bus.tool_finished(call.name, False, summary=outcome)
                    observations.append(render_observation(call, {"error": outcome, "cancelled": True}))
                    continue

                if outcome.get("success"):
                    summary = str(outcome.get("output") or "")[:200]
                    await bus.tool_finished(call.name, True, summary=summary)
                else:
                    detail = outcome.get("error") or "failed"
                    errors.append(f"{call.name}: {detail}")
                    await bus.tool_finished(call.name, False, summary=str(detail)[:200])
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
            lines.append(f"- {tool.name}({signature}) [{tool.permission}] {tool.description}")
        return "\n".join(lines)

    async def _ask(
        self, messages: List[Dict[str, str]], force_prose: bool = False
    ) -> Optional[str]:
        """One LLM call. Returns ``None`` if the provider failed.

        Never streams: the intermediate output is a tool call, and streaming it
        would put raw JSON on the user's screen. The final prose is streamed by
        the caller, from the parsed result.
        """
        try:
            return await self.llm.complete(
                messages,
                tier=self.llm.tier if hasattr(self.llm, "tier") else None,
                max_tokens=2048,
                temperature=0.1,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("AgentEngine LLM call failed: %s", e)
            return None

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
                logger.warning("Approval callback failed for %s: %s", call.name, e)
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
