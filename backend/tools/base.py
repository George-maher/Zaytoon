"""The tool contract.

A tool is a named, self-describing, permission-classified unit of work. Every
tool declares:

* ``name``        — the stable identifier used in the LLM protocol and the API
* ``description`` — what it does, for the model *and* for humans
* ``schema``      — a JSON Schema for its parameters, so the registry can
                    validate arguments and advertise the tool without a
                    hand-maintained second list
* ``permission``  — its tier (see :mod:`tools.safety`)
* ``validate``    — cheap argument checks that run *before* the approval gate
* ``execute``     — the work itself

The ordering matters and is the point of the design: **validation runs before
the permission gate**, so a malformed request gets a precise error instead of an
approval prompt for something that could not have run anyway. And a tool's
``execute`` is only ever reached *after* the gate has approved it, so no tool
needs to re-implement confirmation.
"""

from __future__ import annotations

import logging
import os
import platform
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Zaytoon.Tools")


class ToolValidationError(ValueError):
    """Raised by :meth:`BaseTool.validate` for a malformed request.

    Distinguished from an execution failure so the caller can report
    "you asked for the wrong thing" rather than "it went wrong".
    """


@dataclass
class ToolResult:
    """Uniform result shape.

    ``data`` carries structured output for programmatic consumers; ``output`` is
    the human/model-readable rendering of the same thing.
    """

    success: bool
    output: str
    error: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    #: Set when the sandbox refused the call outright (no approval could help).
    blocked: bool = False

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"success": self.success, "output": self.output}
        if self.error:
            payload["error"] = self.error
        if self.data:
            payload["data"] = self.data
        if self.blocked:
            payload["blocked"] = True
        return payload

    @classmethod
    def ok(cls, output: str, data: Optional[Dict[str, Any]] = None) -> "ToolResult":
        return cls(success=True, output=output, data=data)

    @classmethod
    def fail(
        cls, error: str, output: str = "", blocked: bool = False
    ) -> "ToolResult":
        return cls(success=False, output=output, error=error, blocked=blocked)

    @classmethod
    def denied(cls, error: str = "Blocked: path outside the workspace") -> "ToolResult":
        return cls(success=False, output="", error=error, blocked=True)


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------
def _prop(kind: str, description: str, **extra: Any) -> Dict[str, Any]:
    return {"type": kind, "description": description, **extra}


class BaseTool(ABC):
    """Base class for every tool.

    Subclasses must implement :meth:`name`, :meth:`description` and
    :meth:`execute`. :meth:`schema` defaults to "no parameters", which keeps
    trivial tools (get_time) from having to declare an empty object.
    """

    #: Default tier. Overridden per subclass; see :mod:`tools.safety`.
    permission: str = "safe"

    @abstractmethod
    def name(self) -> str:
        """Stable identifier, e.g. ``file_read``."""

    @abstractmethod
    def description(self) -> str:
        """One line, addressed to the model choosing between tools."""

    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult:
        """Do the work. Only called after the permission gate has approved."""

    def schema(self) -> Dict[str, Any]:
        """JSON Schema for this tool's parameters.

        The default advertises an empty object, which is correct for tools that
        take no arguments.
        """
        return {"type": "object", "properties": {}, "required": [], "additionalProperties": False}

    def aliases(self) -> List[str]:
        """Alternative names accepted for this tool.

        The model is inconsistent about which name it reaches for
        (``file_read`` vs ``read_file``), and the permission tiers already
        recognise both spellings. Registering the aliases keeps the tier
        classification and the dispatch table in agreement.
        """
        return []

    def validate(self, params: Dict[str, Any]) -> None:
        """Reject malformed arguments. Raise :class:`ToolValidationError`.

        Runs *before* the permission gate. The base implementation checks only
        that ``params`` is a mapping and that every declared ``required``
        property is present and non-empty — enough to catch the common
        "the model forgot the path" case without each tool repeating it.
        """
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ToolValidationError(
                f"{self.name()}: parameters must be an object, got {type(params).__name__}"
            )
        for field_name in self.schema().get("required", []):
            value = params.get(field_name)
            if value is None or (isinstance(value, str) and not value.strip()):
                raise ToolValidationError(f"{self.name()}: '{field_name}' is required")

    def risk_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """The parameters the risk assessor should judge.

        Defaults to the whole parameter dict. Override when a tool carries an
        irrelevant or oversized field — the assessor regex-scans these, so
        passing a 100 KB code blob to a command classifier wastes work and can
        produce a misleading match.
        """
        return params or {}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} name={self.name()!r} tier={self.permission}>"


# ---------------------------------------------------------------------------
# Backwards-compatible toolkit
# ---------------------------------------------------------------------------
class OSToolkit:
    """The original platform-dispatch registry.

    Retained because ``tools/windows.py`` and ``tools/linux.py`` are covered by
    the security-sandbox suite, and because :class:`ToolRegistry` builds on
    them for the OS-specific primitives. New code should use
    :class:`tools.registry.ToolRegistry` — this class only knows about the three
    original tools (``cmd``, ``file``, ``system_info``) and knows nothing about
    permissions.
    """

    def __init__(self) -> None:
        self.is_windows = os.name == "nt"
        self.system = platform.system().lower()
        self._tools: Dict[str, BaseTool] = {}
        self._initialize()

    def _initialize(self) -> None:
        if self.is_windows:
            from .windows import WindowsToolSet

            toolkit = WindowsToolSet()
        else:
            from .linux import LinuxToolSet

            toolkit = LinuxToolSet()

        for tool in toolkit.get_tools():
            self._tools[tool.name()] = tool

        logger.info(
            "OS Toolkit initialized for %s with %d tools", self.system, len(self._tools)
        )

    def get_tool(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)

    def list_tools(self) -> List[str]:
        return list(self._tools.keys())

    async def execute(self, tool_name: str, **params: Any) -> ToolResult:
        tool = self.get_tool(tool_name)
        if not tool:
            return ToolResult.fail(
                f"Tool '{tool_name}' not found. Available: {', '.join(self.list_tools())}"
            )
        try:
            logger.info("Executing tool: %s with params: %s", tool_name, params)
            result = await tool.execute(**params)
            logger.info("Tool %s completed: success=%s", tool_name, result.success)
            return result
        except Exception as e:
            logger.error("Tool %s error: %s", tool_name, e)
            return ToolResult.fail(str(e))


__all__ = [
    "BaseTool",
    "OSToolkit",
    "ToolResult",
    "ToolValidationError",
    "_prop",
]
