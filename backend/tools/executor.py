"""Backwards-compatible facade over :class:`tools.registry.ToolRegistry`.

This module used to hold a second, independent implementation of every OS tool,
 alongside the one in ``ToolAgent``. Two copies of ``open_app``, ``type_text``,
 ``key_press``, ``run_code``, ``search_web``, ``file_read``, ``file_write`` and
``system_command`` had already drifted apart: this one used
``powershell -EncodedCommand`` while the other interpolated user text into a
command line, and this one pre-validated ``file_read``/``file_write`` without
having a handler for either, so those two returned ``"Unknown action"``.

There is now exactly one implementation of each, in :mod:`tools.registry` and
the per-domain modules. What remains here is the public surface that
``api/main.py``, ``services/agent_service.py`` and the test suite import:

* ``ToolExecutor(toolkit=..., reminder_service=..., scheduler=...)``
* ``await executor.execute(action, params)``
* ``resolve_windows_app`` (re-exported from :mod:`tools.applications`)

``toolkit`` is still accepted and ignored, because a lot of tests inject a mock
toolkit that no longer has anything to mock.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from tools.applications import (  # noqa: F401 - re-exported for the test suite
    APP_PATHS,
    WINDOWS_APP_LAUNCH,
    resolve_windows_app,
)
from tools.base import OSToolkit  # noqa: F401 - re-exported
from tools.base import ToolResult
from tools.registry import ToolRegistry, build_default_registry
from tools.sandbox import (  # noqa: F401 - re-exported
    DENIED_CODE_LANGUAGES,
    SecurityError,
    is_safe_path,
    resolve_workspace,
    sanitize_app_name,
    split_command,
    validate_app_target,
    workspace_root,
)
from tools.safety import ConfirmationManager, RiskAssessor, RiskLevel

logger = logging.getLogger("Zaytoon.ToolExecutor")


class ToolExecutor:
    """The single chokepoint for OS tool execution.

    Kept as a class with this constructor signature so the existing call sites
    do not change. It owns no tool logic of its own — it delegates to a
    :class:`ToolRegistry`, which is where validation, the permission gate and
    the tool implementations live.
    """

    def __init__(
        self,
        toolkit: Optional[OSToolkit] = None,
        reminder_service=None,
        scheduler=None,
        confirmation_manager: Optional[ConfirmationManager] = None,
        risk_assessor: Optional[iskAssessor] = None,
        registry: Optional[ToolRegistry] = None,
    ) -> None:
        self.toolkit = toolkit  # accepted for compatibility; unused
        self.reminder = reminder_service
        self.scheduler = scheduler
        self.assessor = risk_assessor or RiskAssessor()
        self.confirmation_manager = confirmation_manager
        self.registry = registry or build_default_registry(self.assessor)

    # ------------------------------------------------------------------
    def set_confirmation_manager(self, manager: Optional[ConfirmationManager]) -> None:
        self.confirmation_manager = manager

    async def execute(self, action: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Validate, gate, then run ``action``.

        This is the method every caller goes through. Note that reminders and
        scheduling are *not* tools: they are services with their own
        confirmation semantics, so they are handled here before delegating.
        """
        params = params or {}
        logger.info("ToolExecutor: action=%s params=%s", action, params)

        if action in ("create_reminder", "schedule_task"):
            return await self._handle_scheduling_action(action, params)

        return await self.registry.execute(
            action, params, confirmation_manager=self.confirmation_manager
        )

    async def _handle_scheduling_action(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Reminders/scheduling predate the registry and stay here for now.

        Both mutate persistent state, so both are gated. `scheduler_manager` is
        not a tool and has no schema, which is why it has not moved into the
        registry; promoting it is a natural follow-up.
        """
        text = params.get("text") or params.get("task") or params.get("title")
        if not text:
            return {"success": False, "error": "No reminder text provided"}

        assessment = self.assessor.assess(
            "create_reminder" if action == "create_reminder" else action,
            {"text": text, "time": params.get("time", "")},
        )
        if assessment.level is RiskLevel.HIGH:
            decision = await ToolRegistry._request_approval(
                assessment, self.confirmation_manager, action, params
            )
            if decision is not True:
                return {
                    "success": False,
                    "error": "Cancelled: the action was not approved.",
                    "cancelled": True,
                }

        if action == "schedule_task":
            if not self.scheduler:
                return {"success": False, "error": "Scheduler not available"}
            result = await self.scheduler.create_from_natural(
                text=text, language=params.get("language", "en")
            )
            return {
                "success": result.get("success", False),
                "message": result.get("confirmation", str(result)),
            }

        if not self.reminder:
            return {"success": False, "error": "Reminder service not available"}
        time_str = params.get("time") or params.get("datetime")
        if not time_str:
            return {"success": False, "error": "No time provided"}
        try:
            when = datetime.fromisoformat(time_str)
        except (ValueError, TypeError):
            return {"success": False, "error": f"Unparseable time: {time_str}"}
        result = await self.reminder.create(task=text, scheduled_time=when)
        if result.get("success"):
            return {"success": True, "message": f"Reminder set for {time_str}: {text}"}
        return {"success": False, "error": result.get("error", "Failed to create reminder")}

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    def list_tools(self) -> List[str]:
        return self.registry.list()

    def describe_tools(self) -> List[Dict[str, Any]]:
        return self.registry.describe()

    def tools_by_tier(self) -> Dict[str, List[str]]:
        return self.registry.by_tier()

    def assess(self, action: str, params: Optional[Dict[str, Any]] = None):
        return self.registry.assess(action, params)


__all__ = [
    "APP_PATHS",
    "DENIED_CODE_LANGUAGES",
    "OSToolkit",
    "SecurityError",
    "ToolExecutor",
    "ToolRegistry",
    "ToolResult",
    "WINDOWS_APP_LAUNCH",
    "ConfirmationManager",
    "RiskAssessor",
    "RiskLevel",
    "build_default_registry",
    "is_safe_path",
    "resolve_workspace",
    "resolve_windows_app",
    "sanitize_app_name",
    "split_command",
    "validate_app_target",
    "workspace_root",
]
