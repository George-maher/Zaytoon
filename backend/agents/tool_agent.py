import asyncio
import hashlib
import json
import logging
import re
import time
from typing import Dict, Any, List, Optional, Callable

from .base_agent import BaseAgent
from .engine import AgentEngine
from .events import AgentEventType, NullBus
from common.reporting import safe_error
from tools.registry import ToolRegistry, build_default_registry
from tools.safety import ConfirmationManager, RiskAssessor, RiskLevel

logger = logging.getLogger("Zaytoon.ToolAgent")

#: The tool catalogue is no longer hand-written here. It comes from the
#: registry, so the prompt, the permission tier and the implementation can
#: never disagree about what exists — the previous hard-coded list omitted
#: every tool added since, which is how `file_read`/`file_write` kept
#: pre-validating with no handler behind them.
def _tool_catalogue() -> str:
    names = [tool.name() for tool in build_default_registry().tools()]
    return (
        '{"action":"tool_name","params":{"key":"value"}}\n'
        f"Available tools: {', '.join(names)}"
    )


TOOL_DESCRIPTION = _tool_catalogue()

# Matches a tool call object and captures the action name plus the remaining
# key/value pairs. Supports both {"action":..,"p":1} and {"action":..,"params":{..}}.
TOOL_CALL_RE = (
    r'\{\s*"action"\s*:\s*"([^"]+)"'
    r'((?:\s*,\s*"[^"]+"\s*:\s*(?:"[^"]*"|-?\d+(?:\.\d+)?|true|false|null|\{[^}]*\}|\[[^\]]*\]))*)'
    r'\s*\}'
)

# Notices shown when a high-risk action is blocked.
_DECLINED = {
    "en": "Cancelled: user denied a high-risk action.",
    "ar": "اتلغت: المستخدم رفض العملية الخطيرة.",
}
_TIMEOUT = {
    "en": "Cancelled: no confirmation received in time.",
    "ar": "اتلغت: مفيش تأكيد في الوقت المحدد.",
}
_UNAVAILABLE = {
    "en": "Blocked: this action needs confirmation but no approval channel is available.",
    "ar": "متوقف: العملية محتاجة تأكيد ومفيش قناة موافقة متاحة.",
}
_ERROR = {
    "en": "Cancelled: the confirmation request could not be sent.",
    "ar": "اتلغت: مقدرش نبعت طلب التأكيد.",
}


class ToolAgent(BaseAgent):
    """Turns a model's JSON tool call into a gated, executed tool result.

    This agent owns *no* tool implementations. It asks the model what to do,
    parses the JSON, and hands the call to :class:`ToolRegistry`, which owns
    validation, the permission gate and every tool. It previously carried its
    own weaker copy of ten of them.
    """

    def __init__(
        self,
        confirmation_manager: Optional[ConfirmationManager] = None,
        risk_assessor: Optional[RiskAssessor] = None,
        registry: Optional[ToolRegistry] = None,
    ):
        super().__init__("Tool Agent", max_tokens=256, temperature=0.1)
        self._cache: dict = {}
        self._CACHE_TTL = 300
        self.assessor = risk_assessor or RiskAssessor()
        self.confirmation_manager = confirmation_manager
        self.registry = registry or build_default_registry(self.assessor)

    def _get_cache_key(self, tool: str, params: dict) -> str:
        raw = f"{tool}:{json.dumps(params, sort_keys=True)}"
        return hashlib.md5(raw.encode()).hexdigest()

    def _get_cached(self, key: str) -> Optional[str]:
        entry = self._cache.get(key)
        if entry and (time.time() - entry["time"]) < self._CACHE_TTL:
            return entry["value"]
        return None

    def _set_cached(self, key: str, value: str):
        self._cache[key] = {"value": value, "time": time.time()}

    async def process(
        self,
        user_message: str,
        context: List[Dict[str, Any]],
        stream_callback: Optional[Callable] = None,
        **kwargs,
    ) -> str:
        """Run the agent loop for one turn.

        Used to be a single LLM call whose JSON was regex-scanned, executed,
        and joined with " | ". It now drives :class:`AgentEngine`, which loops:
        ask, act, observe, repeat, until the model answers in prose or the
        iteration budget runs out.

        The signature is unchanged, so :class:`AgentService` needs no edit.
        """
        lang = kwargs.get("language", "en")
        bus = kwargs.get("event_bus") or NullBus()
        manager = kwargs.get("confirmation_manager", self.confirmation_manager)
        session_id = kwargs.get("session_id", "")

        # A caller that only wants text still gets it, streamed. Subscribing the
        # legacy callback to RESPONSE_CHUNK is what stops the tool-call JSON
        # from reaching the user: only final prose is a response chunk. The
        # previous code passed `stream_callback` straight into the LLM call, so
        # {"action":"open_app","app":"spotify"} was typed out on screen before
        # being overwritten by the real reply.
        if stream_callback is not None and bus.subscriber_count == 0:

            async def _forward(event):
                if event.type is AgentEventType.RESPONSE_CHUNK:
                    result = stream_callback(event.data.get("content", ""))
                    if hasattr(result, "__await__"):
                        await result

            bus.subscribe(_forward)

        engine = AgentEngine(self.llm, self.registry)
        result = await engine.run(
            user_message=user_message,
            context=context,
            language=lang,
            bus=bus,
            approval=self._make_approval(manager, session_id, lang),
            response_id=kwargs.get("response_id", ""),
            # The router only sends intent=tools here, so a first-attempt prose
            # reply is worth one corrective round rather than being accepted as
            # the answer (a placeholder or a guessed fact otherwise reaches the
            # user verbatim).
            expect_tool=True,
        )
        return result.text

    def _make_approval(self, manager, session_id: str, language: str):
        """Adapt the agent's bilingual gate to the engine's callback shape.

        Returning the refusal *string* rather than a bool is deliberate: the
        engine feeds it back to the model as an observation, so the model learns
        the user declined instead of the tool failing for no stated reason.
        """

        # NOTE: the notices are *module-level* dicts (`_DECLINED`, ...), not
        # instance attributes — `self._DECLINED` raised AttributeError on
        # every non-approval branch, so the engine's approval handler logged
        # `op=approval type=AttributeError` and fed an error string back to
        # the model instead of the bilingual refusal. Found by the live E2E
        # decline scenario, which no unit test covered.
        async def approve(tool_name: str, params: dict):
            if manager is None:
                return _DECLINED.get(language, _DECLINED["en"])
            try:
                if await manager.request(tool_name, params, session_id):
                    return True
            except asyncio.CancelledError:
                raise
            except Exception as e:
                safe_error(
                    e, operation="confirmation", component="ToolAgent",
                    detail=f"for {tool_name}",
                )
                return _ERROR.get(language, _ERROR["en"])

            outcome = getattr(manager, "last_outcome", None)
            if outcome == ConfirmationManager.OUTCOME_TIMEOUT:
                return _TIMEOUT.get(language, _TIMEOUT["en"])
            if outcome == ConfirmationManager.OUTCOME_UNAVAILABLE:
                return _UNAVAILABLE.get(language, _UNAVAILABLE["en"])
            if outcome == ConfirmationManager.OUTCOME_ERROR:
                return _ERROR.get(language, _ERROR["en"])
            return _DECLINED.get(language, _DECLINED["en"])

        return approve

    async def execute_tools(
        self,
        ai_response: str,
        confirmation_manager: Optional[ConfirmationManager] = None,
        session_id: str = "",
        language: str = "en",
    ) -> str:
        """Parse and run every tool call in a raw model response.

        **Not on any request path** — `ToolAgent` routes through
        `AgentEngine`, whose `_run_batch` runs gated calls as barriers under a
        semaphore. This legacy fold gathers *all* matches concurrently with no
        cap, which is fine for the test fixtures that call it and would be the
        wrong shape for live traffic; it is retained for the safety suite
        (`tests/test_tool_agent_safety.py`), not as an execution path.
        """
        manager = confirmation_manager or self.confirmation_manager
        matches = re.findall(TOOL_CALL_RE, ai_response)

        if not matches:
            return ai_response

        tasks = []
        for tool_name, params_fragment in matches:
            try:
                params = self._parse_params(params_fragment)
            except json.JSONDecodeError:
                tasks.append(
                    asyncio.sleep(
                        0, result=f"Error: Invalid JSON params for tool '{tool_name}'"
                    )
                )
                continue
            tasks.append(
                self._execute_single_tool(
                    tool_name, params, manager, session_id, language
                )
            )

        results = await asyncio.gather(*tasks, return_exceptions=True)

        tool_results = []
        for result in results:
            if isinstance(result, Exception):
                tool_results.append(f"Tool error: {result}")
            elif result:
                tool_results.append(result)

        return " | ".join(tool_results) if tool_results else ai_response

    @staticmethod
    def _parse_params(fragment: str) -> dict:
        """Rebuild the params object from a captured JSON fragment.

        Handles both the flat form ``{"action": "x", "p": 1}`` and the nested
        form ``{"action": "x", "params": {"p": 1}}``.
        """
        fragment = (fragment or "").strip()
        if not fragment:
            return {}
        raw = fragment.lstrip(",").strip()
        if not raw.startswith("{"):
            raw = "{" + raw + "}"
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and isinstance(parsed.get("params"), dict):
            return parsed["params"]
        return parsed if isinstance(parsed, dict) else {}

    # ------------------------------------------------------------------
    # Safety gate
    # ------------------------------------------------------------------
    async def _request_approval(
        self,
        tool_name: str,
        params: dict,
        manager: Optional[ConfirmationManager],
        session_id: str,
        language: str,
    ) -> Optional[str]:
        """Return ``None`` to proceed, or a refusal string to skip execution."""
        assessment = self.assessor.assess(tool_name, params)
        if assessment.level is not RiskLevel.HIGH:
            return None

        logger.info(
            "High-risk tool '%s' blocked pending approval: %s",
            tool_name, assessment.prompt_text(),
        )

        if manager is None:
            return _UNAVAILABLE.get(language, _UNAVAILABLE["en"])

        try:
            approved = await manager.request(tool_name, params, session_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            safe_error(
                e, operation="confirmation", component="ToolAgent",
                detail=f"for {tool_name}",
            )
            return _ERROR.get(language, _ERROR["en"])

        if approved:
            return None

        # Tell the user *why* nothing ran: a deliberate "no" reads very
        # differently from a timeout where they may not have been looking.
        outcome = getattr(manager, "last_outcome", None)
        if outcome == ConfirmationManager.OUTCOME_TIMEOUT:
            return _TIMEOUT.get(language, _TIMEOUT["en"])
        if outcome == ConfirmationManager.OUTCOME_UNAVAILABLE:
            return _UNAVAILABLE.get(language, _UNAVAILABLE["en"])
        if outcome == ConfirmationManager.OUTCOME_ERROR:
            return _ERROR.get(language, _ERROR["en"])
        return _DECLINED.get(language, _DECLINED["en"])

    async def _execute_single_tool(
        self,
        tool_name: str,
        params: dict,
        confirmation_manager: Optional[ConfirmationManager] = None,
        session_id: str = "",
        language: str = "en",
    ) -> str:
        """Gate and run one tool, returning a model-readable string.

        The whole implementation used to be an ``if/elif`` chain over
        ``open_app``, ``type_text``, ``key_press``, ``get_time``, ``run_code``,
        ``search_web``, ``system_command``, ``calculator``, ``file_read``,
        ``file_write`` and ``system_info`` — each with its own private method
        duplicating what ``ToolExecutor`` already did, less safely. Those copies
        had already drifted: this one built a ``powershell -Command`` string
        from user text (injectable) while the other used ``-EncodedCommand``,
        and this one supported three ``run_code`` languages against the other's
        five.

        Now it delegates to the one registry, which owns validation, the
        permission gate and every implementation. The cache is kept, but it is
        consulted *after* the gate so a cached result can never bypass a
        required confirmation.
        """
        params = params or {}
        manager = confirmation_manager or self.confirmation_manager

        # The gate runs first, and unconditionally: a cache hit must never be a
        # way around asking the user. It happens *here* rather than in the
        # registry so the decline / timeout / no-channel notices can be
        # bilingual, which the registry's generic message is not.
        refusal = await self._request_approval(
            tool_name, params, manager, session_id, language
        )
        if refusal is not None:
            return refusal

        cache_key = self._get_cache_key(tool_name, params)
        cached = self._get_cached(cache_key)
        if cached:
            return f"[Cached] {cached}"

        # Hand the decision to the registry. Passing `approval=True` is what
        # stops the registry running a *second* gate and refusing the call we
        # just had approved; passing `confirmation_manager` instead would make
        # it ask the user twice.
        result = await self.registry.execute(
            tool_name, params, confirmation_manager=None, approval=True
        )
        rendered = _render_for_model(result)

        # Only successful, non-blocked results are cached. A denial or a
        # sandbox refusal must be re-evaluated next time, and an "Error: ..."
        # string is not a result.
        if result.get("success") and not result.get("blocked"):
            self._set_cached(cache_key, rendered)
        return rendered


def _render_for_model(result: Dict[str, Any]) -> str:
    """Flatten a registry result into the single string the model sees.

    The agent contract is one string in, one string out, because the tool call
    is spliced into the assistant's reply.
    """
    if result.get("success"):
        return str(result.get("output") or "(no output)")

    error = result.get("error") or "Tool reported failure without a reason"
    if result.get("blocked"):
        return f"Blocked by the security sandbox: {error}"
    if result.get("cancelled"):
        return f"Cancelled: {error}"
    if result.get("requires_confirmation"):
        return f"Needs confirmation: {error}"
    return f"Error: {error}"
