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

logger = logging.getLogger("Zaytoon.Tools.Windows")

ALLOWED_COMMANDS = {
    "dir", "echo", "whoami", "date", "time", "ver",
    "systeminfo", "ipconfig", "tasklist", "hostname",
}

#: Allow-listed commands that only exist as cmd.exe builtins, with no
#: standalone executable. They are dispatched via `cmd /c` with an
#: already-validated, metacharacter-free argument list, so no user-controlled
#: shell syntax can reach the interpreter.
CMD_BUILTINS = {"dir", "echo", "date", "time", "ver", "vol", "cls"}


class WindowsCmdTool(BaseTool):
    def name(self) -> str: return "cmd"

    def description(self) -> str: return "Execute a Windows shell command"

    async def execute(self, command: str = "", **kwargs) -> ToolResult:
        if not command.strip():
            return ToolResult(False, "", error="No command provided")

        # SECURITY: the allow-list is enforced on a tokenised, shell-free
        # invocation. Previously `echo`/`dir`/`date`/`time` were passed to
        # `subprocess.run(command, shell=True)`, so a payload such as
        #   "echo hi & del C:\important.txt"
        # passed the `cmd_base in ALLOWED_COMMANDS` check and then executed the
        # chained `del`. Rejecting shell metacharacters here closes that.
        try:
            parts = split_command(command)
        except SecurityError as e:
            logger.warning("Blocked shell injection attempt: %s", e)
            return ToolResult(False, "", error=str(e))

        # Normalise a leading path and extension:
        #   C:\Windows\System32\whoami.exe -> whoami
        cmd_base = os.path.basename(parts[0]).lower()
        if cmd_base.endswith(".exe"):
            cmd_base = cmd_base[:-4]

        if cmd_base not in ALLOWED_COMMANDS:
            return ToolResult(
                False,
                "",
                error=f"Command '{cmd_base}' not allowed. Allowed: {', '.join(sorted(ALLOWED_COMMANDS))}",
            )

        try:
            loop = asyncio.get_running_loop()
            if cmd_base in CMD_BUILTINS:
                # `cmd /c` is unavoidable for builtins, but `parts` has already
                # been rejected for every shell metacharacter, so the only
                # syntax cmd sees is the command name and literal arguments.
                argv = [os.environ.get("COMSPEC", "cmd.exe"), "/c", *parts]
            else:
                argv = parts
            proc = await loop.run_in_executor(
                None, lambda: subprocess.run(
                    argv, capture_output=True, text=True, timeout=15, shell=False,
                )
            )
            if proc.returncode == 0:
                return ToolResult(True, proc.stdout.strip()[:10000] or "(no output)")
            return ToolResult(False, "", error=proc.stderr.strip()[:1000])
        except FileNotFoundError:
            return ToolResult(
                False, "", error=f"Command not found: {cmd_base}"
            )
        except subprocess.TimeoutExpired:
            return ToolResult(False, "", error="Command timed out")
        except Exception as e:
            return ToolResult(False, "", error=str(e))


class WindowsFileTool(BaseTool):
    def name(self) -> str: return "file"

    def description(self) -> str: return "Read or list files on Windows"

    async def execute(self, path: str = "", action: str = "read", **kwargs) -> ToolResult:
        if not path.strip():
            return ToolResult(False, "", error="No path provided")

        # `resolve_workspace` both validates containment *and* returns the
        # concrete absolute path. Doing the check with `is_safe_path` and then
        # opening `os.path.abspath(path)` separately was a real mismatch: a
        # relative path is resolved against the process cwd, so it was checked
        # against one root and opened against another. One call, one root.
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


class WindowsSystemTool(BaseTool):
    def name(self) -> str: return "system_info"

    def description(self) -> str: return "Get Windows system information"

    async def execute(self, **kwargs) -> ToolResult:
        try:
            loop = asyncio.get_running_loop()
            info = await loop.run_in_executor(None, lambda: {
                "os": "Windows",
                "version": platform.version(),
                "release": platform.release(),
                "machine": platform.machine(),
                "processor": platform.processor(),
                "hostname": platform.node(),
                "python": platform.python_version(),
            })
            return ToolResult(True, str(info), data=info)
        except Exception as e:
            return ToolResult(False, "", error=str(e))


class WindowsToolSet:
    def get_tools(self) -> List[BaseTool]:
        return [
            WindowsCmdTool(),
            WindowsFileTool(),
            WindowsSystemTool(),
        ]
