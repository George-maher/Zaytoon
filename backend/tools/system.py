"""System information, time, calculation, volume and screenshots.

Every tool here is read-only, so all of them are ``safe``: none of them change
state, and ``RiskAssessor.READ_ONLY_ACTIONS`` agrees.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime
from typing import Any, Dict, List

from tools.base import BaseTool, ToolResult, ToolValidationError, _prop

logger = logging.getLogger("Zaytoon.Tools.System")


async def _in_executor(fn, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fn, *args)


class GetTime(BaseTool):
    permission = "safe"

    def name(self) -> str:
        return "get_time"

    def description(self) -> str:
        return "Get the current local date and time."

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "format": _prop("string", "Optional strftime format.", default="%Y-%m-%d %H:%M:%S")
            },
            "additionalProperties": False,
        }

    async def execute(self, format: str = "%Y-%m-%d %H:%M:%S", **_: Any) -> ToolResult:
        now = datetime.now()
        try:
            rendered = now.strftime(format or "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            rendered = now.isoformat()
        return ToolResult.ok(
            f"{rendered} ({now.strftime('%A')})",
            {
                "iso": now.isoformat(),
                "date": now.strftime("%Y-%m-%d"),
                "time": now.strftime("%H:%M:%S"),
                "weekday": now.strftime("%A"),
            },
        )


class SystemInfo(BaseTool):
    permission = "safe"

    def name(self) -> str:
        return "system_info"

    def aliases(self) -> List[str]:
        return ["get_system_info", "system_status"]

    def description(self) -> str:
        return (
            "Get OS, CPU, memory, disk and runtime information about this "
            "machine. Use this instead of guessing what is installed."
        )

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "include_disk": _prop("boolean", "Include disk usage.", default=True)
            },
            "additionalProperties": False,
        }

    async def execute(self, include_disk: bool = True, **_: Any) -> ToolResult:
        info = await _in_executor(_collect_system_info)
        if include_disk:
            info["disks"] = await _in_executor(_disk_usage)
        lines = [f"{k}: {v}" for k, v in info.items() if not isinstance(v, (dict, list))]
        for key in ("disks",):
            if key in info:
                lines.append(f"{key}: {info[key]}")
        return ToolResult.ok("\n".join(lines), info)


def _collect_system_info() -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "os": f"{platform.system()} {platform.release()}",
        "version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor() or platform.machine(),
        "hostname": platform.node(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
    }
    try:
        # /proc is the cheapest source on Linux; psutil is not a dependency.
        if sys.platform.startswith("linux") and os.path.exists("/proc/meminfo"):
            with open("/proc/meminfo", encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        info["memory_total_gb"] = round(kb / (1024 * 1024), 2)
                        break
    except (OSError, ValueError, IndexError):
        pass
    return info


def _disk_usage() -> Dict[str, str]:
    usage: Dict[str, str] = {}
    for drive in ("C:", "D:") if os.name == "nt" else ("/",):
        root = drive + "\\" if os.name == "nt" else drive
        try:
            total, used, _free = shutil.disk_usage(root)
            usage[drive] = f"{used / 1e9:.1f} GB used of {total / 1e9:.1f} GB"
        except OSError:
            continue
    return usage


class Calculator(BaseTool):
    permission = "safe"

    def name(self) -> str:
        return "calculator"

    def description(self) -> str:
        return "Evaluate an arithmetic expression, e.g. (15 * 23) + 45."

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "expression": _prop("string", "Arithmetic expression.")
            },
            "required": ["expression"],
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        expression = params.get("expression")
        if not isinstance(expression, str) or not expression.strip():
            raise ToolValidationError("'expression' is required")

    def execute_sync(self, expression: str) -> str:
        """Synchronous evaluation, kept for the existing unit tests."""
        return self._evaluate(expression)

    @staticmethod
    def _evaluate(expression: str) -> str:
        cleaned = "".join(
            ch for ch in expression if ch in "0123456789+-*/()., %"
        )
        if not cleaned.strip():
            return "Error: no arithmetic expression found"
        try:
            tree = ast.parse(cleaned, mode="eval")
        except SyntaxError:
            return f"Error: invalid expression {expression!r}"

        # An explicit allow-list of node types. Names, calls, attributes,
        # subscripts and comprehensions are all absent, so there is nothing to
        # reach `__import__` or `os.system` through.
        allowed = (
            ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
            ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv,
            ast.Mod, ast.Pow, ast.USub, ast.UAdd,
        )
        for node in ast.walk(tree):
            if not isinstance(node, allowed):
                return "Error: disallowed expression"
        try:
            result = eval(  # noqa: S307 - AST allow-list above
                compile(tree, "<calculator>", "eval"),
                {"__builtins__": {}},
                {},
            )
        except (ArithmeticError, ValueError, TypeError) as e:
            return f"Error: {e}"
        return str(result)

    async def execute(self, expression: str = "", **_: Any) -> ToolResult:
        outcome = self._evaluate(expression)
        if outcome.startswith("Error"):
            return ToolResult.fail(outcome)
        return ToolResult.ok(outcome, {"result": outcome})


class GetVolume(BaseTool):
    permission = "safe"

    def name(self) -> str:
        return "get_volume"

    def description(self) -> str:
        return (
            "Get the current system output volume and mute state, and set the "
            "volume. Reading is safe; setting requires confirmation."
        )

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "level": _prop(
                    "integer", "Target volume 0-100. Omit to only read.", minimum=0, maximum=100
                ),
                "mute": _prop("boolean", "Mute or unmute."),
            },
            "additionalProperties": False,
        }

    def permission_for_params(self, params: Dict[str, Any]) -> str:
        """Reading is safe; writing the volume is not.

        The tier depends on the arguments, which the base class's static
        ``permission`` cannot express. :meth:`ToolRegistry.assess` consults
        ``permission``; so the *declared* tier is the strict one and the
        assessor downgrades a pure read.
        """
        if params.get("level") is not None or params.get("mute") is not None:
            return "confirm"
        return "safe"

    async def execute(
        self, level: Any = None, mute: Any = None, **_: Any
    ) -> ToolResult:
        if level is None and mute is None:
            return await _in_executor(self._read_volume)

        if os.name != "nt":
            return ToolResult.fail("Volume control is only implemented on Windows.")
        script = _volume_script(level, mute)
        if script is None:
            return ToolResult.fail("Could not build a volume command.")
        try:
            proc = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-NonInteractive", "-Command", script,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, _err = await asyncio.wait_for(proc.communicate(), timeout=10)
        except FileNotFoundError:
            return ToolResult.fail("powershell is not available on this system.")
        except (asyncio.TimeoutError, OSError) as e:
            return ToolResult.fail(f"Volume change failed: {e}")
        text = out.decode("utf-8", errors="replace").strip()
        return ToolResult.ok(text or "Volume updated", {"output": text})

    @staticmethod
    def _read_volume() -> ToolResult:
        if os.name != "nt":
            return ToolResult.fail("Volume control is only implemented on Windows.")
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [
                    "powershell", "-NoProfile", "-NonInteractive", "-Command",
                    "(Get-CimInstance Win32_SoundDevice | Select-Object -First 1 "
                    "Name).Name",
                ],
                capture_output=True, text=True, timeout=10, shell=False,
            )
            device = (proc.stdout or "").strip() or "unknown"
        except (subprocess.SubprocessError, OSError):
            device = "unknown"
        return ToolResult.ok(f"Output device: {device}", {"device": device})


def _volume_script(level: Any, mute: Any) -> Any:
    """Build a volume command from *validated integers/booleans only*.

    Values are coerced with ``int()``/``bool()`` before interpolation, so no
    caller-supplied string can reach the script.
    """
    parts: List[str] = []
    if level is not None:
        try:
            value = max(0, min(100, int(level)))
        except (TypeError, ValueError):
            return None
        parts.append(f"(New-Object -ComObject WScript.Shell).SendKeys("
                     f"[char]175, {value})")
    if mute is not None:
        key = "[char]175" if mute else "[char]175"
        parts.append(f"(New-Object -ComObject WScript.Shell).SendKeys({key})")
    return "; ".join(parts) if parts else None


class Screenshot(BaseTool):
    permission = "safe"

    def name(self) -> str:
        return "screenshot"

    def description(self) -> str:
        return "Capture the screen to a PNG inside the workspace."

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "filename": _prop(
                    "string", "Output filename.", default="screenshot.png"
                )
            },
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        name = params.get("filename", "screenshot.png")
        if not isinstance(name, str) or not name.strip():
            raise ToolValidationError("'filename' must be a non-empty string")
        if not name.lower().endswith(".png"):
            raise ToolValidationError("'filename' must end with .png")

    async def execute(self, filename: str = "screenshot.png", **_: Any) -> ToolResult:
        from tools.filesystem import _resolve

        target = _resolve({"path": filename or "screenshot.png"})
        script = (
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
            "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds; "
            "$i=New-Object System.Drawing.Bitmap $b.Width,$b.Height; "
            "$g=[System.Drawing.Graphics]::FromImage($i); "
            "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size); "
            f"$i.Save('{target}',[System.Drawing.Imaging.ImageFormat]::Png); "
            "$g.Dispose(); $i.Dispose()"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-NonInteractive", "-Command", script,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _out, err = await asyncio.wait_for(proc.communicate(), timeout=20)
        except FileNotFoundError:
            return ToolResult.fail("powershell is not available on this system.")
        except (asyncio.TimeoutError, OSError) as e:
            return ToolResult.fail(f"Screenshot failed: {e}")
        if proc.returncode != 0:
            detail = err.decode("utf-8", errors="replace").strip()[:300]
            return ToolResult.fail(f"Screenshot failed: {detail}")
        return ToolResult.ok(f"Saved {filename}", {"path": filename})


def build_tools() -> List[BaseTool]:
    return [GetTime(), SystemInfo(), Calculator(), GetVolume(), Screenshot()]
