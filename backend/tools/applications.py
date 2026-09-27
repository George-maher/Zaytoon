"""Application launching, closing and window control.

The Windows resolver moved here from ``tools/executor.py`` unchanged in
behaviour: it only ever returns an allow-listed alias, a PATH-resolved
executable, a Start Menu shortcut, or a caller-supplied path that exists and is
not a script type. Names carrying shell syntax are rejected rather than
returned verbatim, because the result may be handed to ``cmd /c start``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from typing import Any, Dict, List, Optional

from tools.base import BaseTool, ToolResult, ToolValidationError, _prop
from tools.sandbox import (
    SecurityError,
    sanitize_app_name,
    validate_app_target,
)

logger = logging.getLogger("Zaytoon.Tools.Applications")

#: Friendly name → executable/alias.
APP_PATHS: Dict[str, List[str]] = {
    "spotify": ["spotify"],
    "chrome": ["chrome"],
    "google chrome": ["chrome"],
    "firefox": ["firefox"],
    "edge": ["msedge"],
    "microsoft edge": ["msedge"],
    "vscode": ["code"],
    "vs code": ["code"],
    "visual studio code": ["code"],
    "cursor": ["cursor"],
    "terminal": ["cmd"],
    "command prompt": ["cmd"],
    "cmd": ["cmd"],
    "powershell": ["powershell"],
    "notepad": ["notepad"],
    "calculator": ["calc"],
    "discord": ["discord"],
    "slack": ["slack"],
    "zoom": ["zoom"],
    "teams": ["teams"],
    "microsoft teams": ["teams"],
    "outlook": ["outlook"],
    "file explorer": ["explorer"],
    "explorer": ["explorer"],
    "control panel": ["control"],
    "settings": ["ms-settings:"],
    "task manager": ["taskmgr"],
    "regedit": ["regedit"],
    "notepad++": ["notepad++"],
    "paint": ["mspaint"],
    "word": ["winword"],
    "excel": ["excel"],
    "powerpoint": ["powerpnt"],
    "fxsound": ["fxsound"],
    "fx audio": ["fxsound"],
    "fx sound": ["fxsound"],
}

#: Targets that need the shell because they are URI schemes or builtins.
WINDOWS_APP_LAUNCH: Dict[str, List[str]] = {
    "spotify": ["spotify:", r"shell:AppsFolder\SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"],
    "calculator": ["calc"],
    "settings": ["ms-settings:"],
}

_DETECT_EXT = re.compile(r"\.\w+$")

_IS_WINDOWS = os.name == "nt"


def _find_start_menu_shortcut(app: str) -> Optional[str]:
    app_lower = app.strip().lower()
    search_dirs = [
        os.path.join(
            os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"
        ),
        os.path.join(
            os.environ.get("ProgramData", r"C:\ProgramData"),
            "Microsoft", "Windows", "Start Menu", "Programs",
        ),
    ]
    for base in search_dirs:
        if not base or not os.path.isdir(base):
            continue
        for root, _, files in os.walk(base):
            for name in files:
                if not name.lower().endswith(".lnk"):
                    continue
                stem = os.path.splitext(name)[0].lower()
                if stem == app_lower or app_lower in stem:
                    return os.path.join(root, name)
    return None


def resolve_windows_app(app: str) -> Optional[str]:
    """Resolve an application name to a launch target, or ``None``.

    Kept at module level (and re-exported from ``tools.executor``) because the
    security-sandbox suite tests it directly.
    """
    try:
        app = sanitize_app_name(app).lower()
    except SecurityError:
        logger.warning("Blocked app resolution for unsafe name: %r", app)
        return None

    if app in WINDOWS_APP_LAUNCH:
        return WINDOWS_APP_LAUNCH[app][0]

    if app in APP_PATHS:
        alias = APP_PATHS[app][0]
        found = shutil.which(alias) or shutil.which(alias + ".exe")
        if found:
            return found
        lnk = _find_start_menu_shortcut(app)
        if lnk:
            return lnk
        return alias

    if _DETECT_EXT.search(app) or os.path.sep in app:
        # A caller-supplied path is honoured only when it exists and is not a
        # script type.
        try:
            validate_app_target(app)
        except SecurityError:
            return None
        return app if os.path.exists(app) else None

    found = shutil.which(app) or shutil.which(app + ".exe")
    if found:
        return found
    return _find_start_menu_shortcut(app)


def _normalise(params: Dict[str, Any]) -> str:
    raw = params.get("app") or params.get("name") or params.get("application")
    if not isinstance(raw, str) or not raw.strip():
        raise ToolValidationError("'app' is required")
    try:
        return sanitize_app_name(raw).lower()
    except SecurityError as e:
        raise ToolValidationError(str(e)) from None


class OpenApp(BaseTool):
    permission = "safe"

    def name(self) -> str:
        return "open_app"

    def aliases(self) -> List[str]:
        return ["start_app", "launch_app"]

    def description(self) -> str:
        return (
            "Open a desktop application by name, e.g. spotify, vscode, chrome, "
            "notepad, calculator."
        )

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "app": _prop("string", "Application name, or a path to an executable."),
            },
            "required": ["app"],
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        _normalise(params)

    async def execute(self, app: str = "", **_: Any) -> ToolResult:
        target = _normalise({"app": app})
        try:
            if _IS_WINDOWS:
                return await self._open_windows(target)
            argv = _resolve_posix(target)
            if argv is None:
                return ToolResult.fail(f"Could not find application '{target}'.")
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            return ToolResult.ok(
                f"Opened {target}", {"app": target, "pid": getattr(proc, "pid", None)}
            )
        except SecurityError as e:
            return ToolResult.denied(str(e))
        except FileNotFoundError:
            return ToolResult.fail(f"Application '{target}' is not installed.")
        except OSError as e:
            return ToolResult.fail(f"Could not open {target}: {e}")

    @staticmethod
    async def _open_windows(target: str) -> ToolResult:
        resolved = resolve_windows_app(target)
        if not resolved:
            return ToolResult.fail(
                f"Could not find application '{target}' on this system."
            )
        try:
            validate_app_target(resolved)
        except SecurityError as e:
            return ToolResult.denied(str(e))

        # `cmd /c start` is unavoidable for URI schemes and shell builtins, but
        # the target has been checked for shell metacharacters and is passed as
        # argv rather than interpolated into a command string.
        proc = await asyncio.create_subprocess_exec(
            "cmd", "/c", "start", "", resolved,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        # Give the shell a moment to fail on a bad target.
        await asyncio.sleep(0.2)
        if proc.returncode not in (0, None):
            return ToolResult.fail(
                f"Could not launch '{target}' (exit {proc.returncode})"
            )
        return ToolResult.ok(f"Opened {target}", {"app": target, "target": resolved})


def _resolve_posix(app: str) -> Optional[List[str]]:
    if app in APP_PATHS:
        alias = APP_PATHS[app][0]
        found = shutil.which(alias)
        return [found] if found else None
    found = shutil.which(app)
    return [found] if found else None


class CloseApp(BaseTool):
    permission = "confirm"

    def name(self) -> str:
        return "close_app"

    def aliases(self) -> List[str]:
        return ["kill_process", "quit_app"]

    def description(self) -> str:
        return "Close a running application. Requires confirmation."

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {"app": _prop("string", "Application name.")},
            "required": ["app"],
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        _normalise(params)

    def risk_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"app": (params.get("app") or params.get("name") or "")}

    async def execute(self, app: str = "", **_: Any) -> ToolResult:
        target = _normalise({"app": app})
        if _IS_WINDOWS:
            exe = target if target.endswith(".exe") else f"{target}.exe"
            if target in APP_PATHS:
                alias = APP_PATHS[target][0]
                exe = alias if _DETECT_EXT.search(alias) else f"{alias}.exe"
            argv = ["taskkill", "/IM", exe, "/F"]
        else:
            argv = ["pkill", "-f", target]

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
        except FileNotFoundError:
            return ToolResult.fail(f"Could not close {target}: helper not available.")
        except OSError as e:
            return ToolResult.fail(f"Could not close {target}: {e}")

        if proc.returncode == 0:
            return ToolResult.ok(f"Closed {target}")
        detail = _decode(stderr)
        return ToolResult.fail(
            detail or f"No running process found for '{target}'"
        )


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace").strip()


def build_tools() -> List[BaseTool]:
    return [OpenApp(), CloseApp()]
