import os
import platform
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from dataclasses import dataclass

logger = logging.getLogger("Zaytoon.Tools")


@dataclass
class ToolResult:
    success: bool
    output: str
    error: Optional[str] = None
    data: Optional[Dict[str, Any]] = None


class BaseTool(ABC):
    @abstractmethod
    def name(self) -> str:
        pass

    @abstractmethod
    def description(self) -> str:
        pass

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult:
        pass


class OSToolkit:
    def __init__(self):
        self.is_windows = os.name == "nt"
        self.system = platform.system().lower()
        self._tools: Dict[str, BaseTool] = {}
        self._initialize()

    def _initialize(self):
        if self.is_windows:
            from .windows import WindowsToolSet
            toolkit = WindowsToolSet()
        else:
            from .linux import LinuxToolSet
            toolkit = LinuxToolSet()

        for tool in toolkit.get_tools():
            self._tools[tool.name()] = tool

        logger.info(
            f"OS Toolkit initialized for {self.system} with {len(self._tools)} tools"
        )

    def get_tool(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)

    def list_tools(self) -> List[str]:
        return list(self._tools.keys())

    async def execute(self, tool_name: str, **params) -> ToolResult:
        tool = self.get_tool(tool_name)
        if not tool:
            return ToolResult(
                success=False,
                output="",
                error=f"Tool '{tool_name}' not found. Available: {', '.join(self.list_tools())}",
            )

        try:
            logger.info(f"Executing tool: {tool_name} with params: {params}")
            result = await tool.execute(**params)
            logger.info(f"Tool {tool_name} completed: success={result.success}")
            return result
        except Exception as e:
            logger.error(f"Tool {tool_name} error: {e}")
            return ToolResult(success=False, output="", error=str(e))
