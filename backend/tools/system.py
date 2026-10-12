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
import re
import shutil
import subprocess
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

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
            "volume. Reading is safe; setting requires confirmation. `level` is "
            "absolute (0-100), never a step count. Supported: Windows "
            "(SendKeys), macOS (osascript), PipeWire/PulseAudio (pactl) and ALSA "
            "(amixer) — where a backend cannot report a percentage (Windows) the "
            "answer says so instead of guessing, and mute is refused there because "
            "the key only toggles."
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

    async def execute(self, level: Any = None, mute: Any = None, **_: Any) -> ToolResult:
        backend = _volume_backend()
        if not backend:
            # A precise refusal beats a stack trace or a fake "volume is 50".
            return ToolResult.fail(
                "No supported volume control here: this tool speaks Windows "
                "(SendKeys), macOS (osascript), PipeWire/PulseAudio (pactl) or ALSA "
                "(amixer), and found none of them."
            )

        if level is None and mute is None:
            return await _read_volume(backend)

        argv = volume_argv(backend, level=level, mute=mute)
        if argv is None:
            return ToolResult.fail(volume_refusal(backend, level=level, mute=mute))
        code, out, err = await _run_argv(argv)
        if code != 0:
            return ToolResult.fail(
                f"Volume change failed ({backend}): {(err or out).strip()[:200] or f'exit {code}'}"
            )
        state = {"backend": backend}
        if level is not None:
            state["level"] = max(0, min(100, int(level)))
        if mute is not None:
            state["muted"] = bool(mute)
        return ToolResult.ok(
            f"Volume updated via {backend}"
            + (f" (level {state['level']}%)" if "level" in state else "")
            + (f", muted={state['muted']}" if "muted" in state else ""),
            state,
        )


#: The virtual key codes, named because the numbers are otherwise unreadable — and
#: because one of them used to be wrong. 173 is mute; 174 is volume *down*;
#: 175 is volume *up*. A caller had a `mute ? 175 : 175` in both branches, and the
#: level path pressed "down" N times from wherever the volume happened to be.
_VK_VOLUME_DOWN = 174
_VK_VOLUME_UP = 175


def _volume_backend() -> str:
    """Which dialect of "set the volume" this machine answers to.

    Probed, not assumed: `pactl` exists on PulseAudio and PipeWire, and a box with
    neither but with `amixer` is a plain ALSA system. Empty means "no way to do
    this here", which the caller turns into a refusal the user can act on.
    """
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if shutil.which("pactl"):
        return "pulse"
    if shutil.which("amixer"):
        return "alsa"
    return ""


async def _run_argv(argv: List[str], timeout: float = 8.0) -> "tuple[int, str, str]":
    import asyncio

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except FileNotFoundError:
        return 127, "", f"{argv[0]} is not installed"
    except asyncio.TimeoutError:
        return 124, "", "timed out"
    except OSError as exc:
        return 125, "", str(exc)
    return (
        proc.returncode if proc.returncode is not None else 0,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
    )


async def _read_volume(backend: str) -> ToolResult:
    """The current level and mute state, from whichever tool answers here.

    Windows is the exception: SendKeys can press the keys but nothing in this
    reading path reports the *level*, so it says which device is configured and
    states the rest plainly rather than implying it knows the volume.
    """
    if backend == "macos":
        code, out, _err = await _run_argv(
            ["osascript", "-e",
             "output volume of (get volume settings)\n"
             "output muted of (get volume settings)"]
        )
        lines = [line.strip() for line in (out or "").splitlines()]
        level = parse_volume_number(lines[0]) if lines else None
        if code != 0 or level is None:
            return ToolResult.fail(f"Could not read the macOS volume ({(out or '').strip()[:80]})")
        muted = (lines[1].lower() == "true") if len(lines) > 1 else None
        return ToolResult.ok(
            f"Output volume: {level}%" + ("" if muted is None else f", {'muted' if muted else 'not muted'}"),
            {"backend": backend, "level": level, "muted": muted},
        )
    if backend == "pulse":
        _c1, out1, _e1 = await _run_argv(
            ["pactl", "get-sink-volume", "@DEFAULT_SINK@"]
        )
        _c2, out2, _e2 = await _run_argv(
            ["pactl", "get-sink-mute", "@DEFAULT_SINK@"]
        )
        level = parse_pactl_volume(out1)
        if level is None:
            return ToolResult.fail("Could not read the PulseAudio/PipeWire volume.")
        muted = "yes" in (out2 or "").lower()
        return ToolResult.ok(
            f"Output volume: {level}%, {'muted' if muted else 'not muted'}",
            {"backend": backend, "level": level, "muted": muted},
        )
    if backend == "alsa":
        _c, out, _e = await _run_argv(["amixer", "get", "Master"])
        level = parse_amixer_percent(out)
        if level is None:
            return ToolResult.fail("Could not read the ALSA mixer state.")
        muted = "[off]" in (out or "")
        return ToolResult.ok(
            f"Output volume: {level}%, {'muted' if muted else 'not muted'}",
            {"backend": backend, "level": level, "muted": muted},
        )
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
    return ToolResult.ok(
        f"Output device: {device}. Windows has no level read-back through this "
        "path, so the current percentage is unknown — set one to change it.",
        {"backend": backend, "device": device, "level": None},
    )


def parse_volume_number(text: str) -> Optional[int]:
    try:
        value = int(str(text).strip())
    except (TypeError, ValueError):
        return None
    return value if 0 <= value <= 100 else None


def parse_pactl_volume(text: str) -> Optional[int]:
    """`pactl get-sink-volume` → the first channel's percent.

    The real line is `Volume: front-left: 65536 /  100% / 0x11000` — no closing
    paren, and `pactl` reports over-100% amplification faithfully (base volume ×
    boost), so the value is returned as read rather than clamped: a system at
    153% is *at* 153%, and quietly calling that 100 is how an audit tool starts
    inventing numbers. Setting still clamps, because the schema promises 0-100.
    """
    match = re.search(r"(\d{1,4})%", text or "")
    if not match:
        return None
    return int(match.group(1))


def parse_amixer_percent(text: str) -> Optional[int]:
    match = re.search(r"\[\s*(\d{1,3})%\]", text or "")
    if not match:
        return None
    value = int(match.group(1))
    return value if value <= 100 else None


def volume_argv(backend: str, *, level: Any = None, mute: Any = None) -> Optional[List[str]]:
    """The command that sets the volume, or None when this backend cannot.

    Kept apart from the subprocess call so the argv is testable on any OS: the
    values are coerced to an int and a bool here, so nothing a caller typed can
    reach an argument list.
    """
    if level is not None:
        try:
            value = max(0, min(100, int(level)))
        except (TypeError, ValueError):
            return None
    else:
        value = None

    if backend == "pulse":
        if value is not None:
            return ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{value}%"]
        if mute is not None:
            return ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1" if bool(mute) else "0"]
        return None
    if backend == "alsa":
        if value is not None:
            return ["amixer", "set", "Master", f"{value}%"]
        if mute is not None:
            return ["amixer", "set", "Master", "mute" if bool(mute) else "unmute"]
        return None
    if backend == "macos":
        script = []
        if value is not None:
            script.append(f"set volume output volume {value}")
        if mute is not None:
            script.append(f"set volume output muted {'true' if bool(mute) else 'false'}")
        if not script:
            return None
        return ["osascript", "-e", "\n".join(script)]
    if backend == "windows":
        # Windows has no absolute level here, so the level path climbs to the top
        # (where extra presses are clamped) and steps back down. Mute, though, is
        # a *toggle* on these keys with no read-back — setting it would need the
        # current state, which this path cannot get. Refusing beats guessing.
        if mute is not None or value is None:
            return None
        return [
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            _volume_script(value, None),
        ]
    return None


def volume_refusal(backend: str, *, level: Any = None, mute: Any = None) -> str:
    if backend == "windows" and mute is not None:
        return (
            "Windows volume keys only toggle mute, and this path cannot read the "
            "current state to know which way — so set level to 0 to silence, or "
            "back to a number to un-mute."
        )
    if level is not None:
        return "'level' must be a whole number from 0 to 100."
    return "Could not build a volume command for that request on this system."


def _volume_script(level: Any, mute: Any) -> Any:
    """Build a volume command from *validated integers/booleans only*.

    Values are coerced with ``int()``/``bool()`` before interpolation, so no
    caller-supplied string can reach the script.

    The level is expressed as a climb then a descent: SendKeys has no absolute
    volume, but pressing up 100 times lands on the ceiling — extra presses there
    are clamped by the OS — so pressing down ``100 - level`` times afterwards is
    the same position every time. The first version of this function pressed
    "down" ``level`` times from wherever the volume happened to be, which made
    "set volume to 20" mean "lower by twenty steps", and had ``[char]175`` in both
    arms of the mute ternary (175 is *volume up*; 173 is mute). Mute is refused
    upstream instead, because a toggle with no read-back cannot be set.
    """
    parts: List[str] = []
    if level is not None:
        try:
            value = max(0, min(100, int(level)))
        except (TypeError, ValueError):
            return None
        down = 100 - value
        sends = [f"(New-Object -ComObject WScript.Shell).SendKeys([char]{_VK_VOLUME_UP}, 100)"]
        if down:
            sends.append(
                f"(New-Object -ComObject WScript.Shell).SendKeys([char]{_VK_VOLUME_DOWN}, {down})"
            )
        parts.extend(sends)
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
