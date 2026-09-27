import asyncio
import os
import subprocess
import platform
import logging
from typing import List

from .base import BaseTool, ToolResult
from .sandbox import (
    SecurityError,
    resolve_workspace,
    split_command,
    workspace_root,
)

logger = logging.getLogger("Zaytoon.Tools.Linux")

ALLOWED_COMMANDS = {
    "ls", "echo", "whoami", "date", "time", "uname",
    "pwd", "cat", "head", "tail", "hostname", "df",
    "free", "ps", "uptime",
}


class LinuxCmdTool(BaseTool):
    def name(self) -> str: return "cmd"

    def description(self) -> str: return "Execute a Linux shell command"

    async def execute(self, command: str = "", **kwargs) -> ToolResult:
        if not command.strip():
            return ToolResult(False, "", error="No command provided")

        # Reject shell syntax *before* the allow-list check. Tokenising and
        # exec'ing without a shell means `ls; rm -rf /` can only ever be
        # attempted as a literal filename, never as a second command.
        try:
            parts = split_command(command)
        except SecurityError as e:
            return ToolResult(False, "", error=str(e))

        cmd_base = os.path.basename(parts[0]).lower()

        if cmd_base not in ALLOWED_COMMANDS:
            return ToolResult(
                False,
                "",
                error=f"Command '{cmd_base}' not allowed. Allowed: {', '.join(sorted(ALLOWED_COMMANDS))}",
            )

        try:
            loop = asyncio.get_running_loop()
            proc = await loop.run_in_executor(
                None, lambda: subprocess.run(parts, capture_output=True, text=True, timeout=15)
            )
            if proc.returncode == 0:
                return ToolResult(True, proc.stdout.strip()[:10000] or "(no output)")
            return ToolResult(False, "", error=proc.stderr.strip()[:1000])
        except subprocess.TimeoutExpired:
            return ToolResult(False, "", error="Command timed out")
        except Exception as e:
            return ToolResult(False, "", error=str(e))


class LinuxFileTool(BaseTool):
    def name(self) -> str: return "file"

    def description(self) -> str: return "Read or list files on Linux"

    async def execute(self, path: str = "", action: str = "read", **kwargs) -> ToolResult:
        if not path.strip():
            return ToolResult(False, "", error="No path provided")

        # Validate containment and obtain the concrete path in one step; see the
        # matching note in tools/windows.py.
        safe_path = resolve_workspace(path)
        if not safe_path:
            return ToolResult(
                False, "",
                error=f"Access denied: path outside the workspace ({workspace_root()}): {path}",
            )

        try:
            loop = asyncio.get_running_loop()
            if action == "read":
                exists = await loop.run_in_executor(None, lambda: os.path.exists(safe_path))
                if not exists:
                    return ToolResult(False, "", error=f"File not found: {path}")
                content = await loop.run_in_executor(
                    None, lambda: open(safe_path, "r", encoding="utf-8").read()
                )
                return ToolResult(True, content[:100000])
            elif action == "list":
                is_dir = await loop.run_in_executor(None, lambda: os.path.isdir(safe_path))
                if not is_dir:
                    return ToolResult(False, "", error=f"Not a directory: {path}")
                entries = await loop.run_in_executor(None, lambda: os.listdir(safe_path))
                result = "\n".join(
                    f"{'[DIR]' if os.path.isdir(os.path.join(safe_path, e)) else '[FILE]'} {e}"
                    for e in entries
                )
                return ToolResult(True, result)
            else:
                return ToolResult(False, "", error=f"Unknown action: {action}")
        except Exception as e:
            return ToolResult(False, "", error=str(e))


class LinuxSystemTool(BaseTool):
    def name(self) -> str: return "system_info"

    def description(self) -> str: return "Get Linux system information"

    async def execute(self, **kwargs) -> ToolResult:
        try:
            loop = asyncio.get_running_loop()
            info = await loop.run_in_executor(None, lambda: {
                "os": "Linux",
                "distro": platform.freedesktop_os_release().get("PRETTY_NAME", "unknown")
                if hasattr(platform, "freedesktop_os_release")
                else platform.version(),
                "machine": platform.machine(),
                "processor": platform.processor(),
                "hostname": platform.node(),
                "python": platform.python_version(),
            })
            return ToolResult(True, str(info), data=info)
        except Exception as e:
            return ToolResult(False, "", error=str(e))


class LinuxToolSet:
    def get_tools(self) -> List[BaseTool]:
        return [
            LinuxCmdTool(),
            LinuxFileTool(),
            LinuxSystemTool(),
        ]
