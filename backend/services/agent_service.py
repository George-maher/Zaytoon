import json
import logging
import re
from datetime import datetime
from typing import Callable, Optional

from domain.models import Intent
from scheduler.parser import TimeParser
from reminder.service import ReminderService
from reminder.notification import NotificationManager
from tools.executor import ToolExecutor
from tools.safety import RiskAssessor, RiskLevel
from common.reporting import safe_error

logger = logging.getLogger("Zaytoon.AgentService")

ACTION_JSON_RE = re.compile(
    r'\{\s*"action"\s*:\s*"([^"]+)"\s*,\s*"[^"]+"\s*:\s*"[^"]*"(?:\s*,\s*"[^"]+"\s*:\s*"[^"]*")*\s*\}'
)


class AgentService:
    def __init__(
        self,
        router_agent,
        fast_agent,
        planner_agent,
        code_agent,
        tool_agent,
        memory_agent,
        scheduler_llm_agent,
        scheduler_manager,
        reminder_service: ReminderService,
        notification_mgr: NotificationManager,
        tool_executor: ToolExecutor,
        os_toolkit,
    ):
        self.router = router_agent
        self.fast = fast_agent
        self.planner = planner_agent
        self.code = code_agent
        self.tool = tool_agent
        self.memory = memory_agent
        self.scheduler_llm = scheduler_llm_agent
        self.scheduler = scheduler_manager
        self.reminder_service = reminder_service
        self.notification_mgr = notification_mgr
        self.tool_executor = tool_executor
        self.toolkit = os_toolkit

    async def route_intent(self, message: str, context: list) -> dict:
        return await self.router.route(message, context)

    async def dispatch(
        self,
        intent: str,
        message: str,
        context: list,
        language: str,
        session_id: str,
        stream_callback: Optional[Callable] = None,
        confirmation_manager=None,
        event_bus=None,
        response_id: str = "",
    ) -> str:
        """Route to an agent and return its reply.

        ``event_bus`` is the agent's progress channel. Passing it through is
        what lets the UI show "Creating the project" / "Running tests" while a
        multi-step tool turn is in flight, instead of a single spinner.
        """
        if intent == Intent.ACTION.value or intent == Intent.TOOLS.value:
            return await self.tool.process(
                message, context[-4:],
                stream_callback=stream_callback,
                language=language,
                confirmation_manager=confirmation_manager,
                session_id=session_id,
                event_bus=event_bus,
                response_id=response_id,
            ) or ""

        if intent == Intent.SCHEDULING.value:
            return await self._handle_scheduling(message, language, session_id)

        if intent == Intent.MEMORY.value:
            results = await self.memory.search_memory(message)
            if results:
                return "Found:\n" + "\n".join(
                    f"{r['role']}: {r['content'][:200]}" for r in results[:3]
                )
            return "Nothing found."

        if intent == Intent.CODE.value:
            return await self.code.process(
                message, context[-2:], stream_callback=stream_callback, language=language
            ) or ""

        if intent == Intent.COMPLEX.value:
            return await self.planner.process(
                message, context[-2:], stream_callback=stream_callback, language=language
            ) or ""

        return await self.fast.process(
            message, context[-3:], stream_callback=stream_callback, language=language
        ) or ""

    async def _handle_scheduling(self, message: str, language: str, session_id: str) -> str:
        parsed_time = TimeParser.parse_time(message)
        if parsed_time is not None:
            result = await self.scheduler.create_from_natural(message, language, session_id)
            return result.get("confirmation", str(result))

        try:
            schedule_data = await self.scheduler_llm.parse_schedule(message, [], language=language)
        except Exception as e:
            safe_error(
                e, operation="schedule.parse", component="AgentService",
                detail="falling back to the raw message",
            )
            schedule_data = {"action": "create", "title": message}

        if parsed_time is None and schedule_data.get("datetime"):
            try:
                parsed_time = datetime.fromisoformat(schedule_data["datetime"])
            except (ValueError, TypeError):
                pass

        action = schedule_data.get("action", "create")

        if action == "create":
            result = await self.reminder_service.create_from_natural(
                message, language, session_id, parsed_time=parsed_time,
            )
            return result.get("confirmation", str(result))

        elif action == "list":
            # Workspace-wide, deliberately: "what are my reminders?" is not
            # "what did *this socket* ask for" — the session id changes on
            # every reconnect (see `ReminderService.list`).
            reminders = await self.reminder_service.list()
            return await self.reminder_service.format_list_for_display(reminders, language)

        elif action == "delete":
            rid = self._extract_id(schedule_data, message)
            if rid is not None:
                ok = await self.reminder_service.delete(rid)
                if ok:
                    return "Deleted." if language == "en" else "تم الحذف."
                return "Reminder not found." if language == "en" else "التذكير مش موجود."
            return "Could not identify which reminder to delete." if language == "en" else "مش عارف أذاكر تذكير إيه."

        elif action == "done" or action == "complete":
            rid = self._extract_id(schedule_data, message)
            if rid is not None:
                ok = await self.reminder_service.mark_completed(rid)
                if ok:
                    return "Marked as done." if language == "en" else "تم."
                return "Reminder not found." if language == "en" else "التذكير مش موجود."
            return "Could not identify which reminder." if language == "en" else "مش عارف أذاكر تذكير إيه."

        return schedule_data.get("confirmation", str(schedule_data))

    async def _execute_tool_commands(self, text: str, confirmation_manager=None) -> str:
        matches = ACTION_JSON_RE.findall(text)
        if not matches:
            return text
        result_text = text
        for full_match in ACTION_JSON_RE.finditer(text):
            raw = full_match.group(0)
            try:
                cmd = json.loads(raw)
                action = cmd.get("action", "")
                params = {k: v for k, v in cmd.items() if k != "action"}

                # Same human-in-the-loop gate as ToolAgent: the raw-JSON path must
                # not be a way around the sandbox.
                if not await self._is_approved(action, params, confirmation_manager):
                    result_text = result_text.replace(
                        raw, f"[{action.upper()}: cancelled — confirmation required]", 1
                    )
                    continue

                execution_result = await self.tool_executor.execute(action, params)
                summary = execution_result.get("message") or execution_result.get("output") or execution_result.get("summary", "")
                if not summary and execution_result.get("success"):
                    summary = f"{action} completed"
                elif not summary:
                    summary = execution_result.get("error", f"{action} failed")
                replacement = f"[{action.upper()}: {summary}]"
                result_text = result_text.replace(raw, replacement, 1)
            except (json.JSONDecodeError, Exception) as e:
                # Test-only path, but the placeholder is shaped like user-facing
                # text: report the type, not `str(e)`.
                kind = type(e).__name__
                result_text = result_text.replace(raw, f"[ERROR: {kind}]", 1)
        return result_text

    async def _is_approved(
        self, action: str, params: dict, confirmation_manager=None
    ) -> bool:
        """True when the action is safe or the user approved it.

        Fails closed: if the approval channel errors, the action is denied.
        """
        assessment = RiskAssessor().assess(action, params)
        if assessment.level is not RiskLevel.HIGH:
            return True

        manager = confirmation_manager or getattr(self.tool, "confirmation_manager", None)
        if manager is None or not hasattr(manager, "request"):
            logger.warning("High-risk %s blocked: no confirmation channel", action)
            return False
        try:
            return bool(await manager.request(action, params))
        except Exception as e:
            # Fail closed; the detail goes to the log, redacted.
            safe_error(
                e, operation="confirmation", component="AgentService",
                detail=f"for {action}",
            )
            return False

    def _extract_id(self, schedule_data: dict, message: str) -> Optional[int]:
        rid = schedule_data.get("id")
        if rid:
            try:
                return int(rid)
            except (ValueError, TypeError):
                pass
        m = re.search(r"[#]?(\d+)", message)
        if m:
            return int(m.group(1))
        return None

    # NOTE: `execute_command` used to expose OSToolkit directly:
    #
    #     async def execute_command(self, command, params):
    #         result = await self.toolkit.execute(command, **params)
    #
    # It was reachable from the WebSocket `command` message, which applied
    # neither RiskAssessor nor ConfirmationManager — an unauthenticated,
    # un-gated tool path. It is removed rather than gated: the WebSocket has no
    # way to present an approval prompt for a caller that is not the assistant,
    # and the gated `ToolExecutor.execute` already covers every real use.
