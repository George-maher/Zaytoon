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
from typing import Any, Dict, List, Optional, Sequence, Tuple

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


def _start_menu_roots() -> List[str]:
    """The Start Menu roots that exist on this machine, in priority order."""
    search_dirs = [
        os.path.join(
            os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"
        ),
        os.path.join(
            os.environ.get("ProgramData", r"C:\ProgramData"),
            "Microsoft", "Windows", "Start Menu", "Programs",
        ),
    ]
    return [d for d in search_dirs if d and os.path.isdir(d)]


def build_shortcut_index() -> List[Tuple[str, str]]:
    """One walk of the Start Menu: ``(shortcut stem, path)``, in walk order.

    The shortcut lookup used to walk both roots on *every call*, so probing
    twenty applications for availability would have cost twenty walks of the
    same tree — enough to block the event loop for the length of a filesystem
    scan. A batch caller builds this once and passes it down.

    The order is exactly the order the per-call walk produced (per root, then
    ``os.walk``'s order within it), so the first match here is the match that
    would have been returned before. Scanning a whole tree to find a match
    that may sit in the first directory costs a little more than an early exit
    on a single lookup; that is a bounded, one-off cost on a path that already
    launches a process.
    """
    index: List[Tuple[str, str]] = []
    for base in _start_menu_roots():
        for root, _, files in os.walk(base):
            for name in files:
                if not name.lower().endswith(".lnk"):
                    continue
                stem = os.path.splitext(name)[0].lower()
                index.append((stem, os.path.join(root, name)))
    return index


def _find_start_menu_shortcut(
    app: str, *, index: Optional[Sequence[Tuple[str, str]]] = None
) -> Optional[str]:
    """First Start Menu shortcut whose stem matches, or ``None``.

    ``index`` lets a caller that resolves many names share one walk; leaving it
    out builds the index here, so a single lookup still works and there is only
    one implementation of what "matches" means.
    """
    app_lower = app.strip().lower()
    if index is None:
        index = build_shortcut_index()
    for stem, path in index:
        if stem == app_lower or app_lower in stem:
            return path
    return None


def _locate(
    name: str, *, shortcut_index: Optional[Sequence[Tuple[str, str]]] = None
) -> Optional[Tuple[str, str]]:
    """Positive evidence that ``name`` is present: ``(target, from_where)``.

    One implementation of "have I actually seen this application?", shared by
    every branch of the resolver so two of them cannot disagree about what
    counts as a hit. It looks for a real artifact — an executable on ``PATH``
    or a Start Menu shortcut — and never treats a *table* of how to launch
    something as proof that it exists.
    """
    alias = APP_PATHS.get(name, [name])[0]
    on_path = shutil.which(alias) or shutil.which(alias + ".exe")
    if on_path:
        return on_path, "path"
    lnk = _find_start_menu_shortcut(name, index=shortcut_index)
    if lnk:
        return lnk, "shortcut"
    return None


def resolve_app_detail(
    app: str, *, shortcut_index: Optional[Sequence[Tuple[str, str]]] = None
) -> Dict[str, Any]:
    """Resolve an application name to a launch target, and say whether the
    application is actually *present*.

    The distinction is the whole point. The old resolver returned a bare alias
    (``"winword"``) when it had looked everywhere and found nothing, and a real
    target when it had found one — both truthy, so a caller could not tell
    "installed" from "will be attempted and may well fail". The Computer view
    needs that answer before it puts an application forward as available, and a
    panel that guesses is worse than one that says nothing.

    Returns a dict with:

    - ``target`` — what a launcher should be handed, or ``None`` if the name
      was rejected outright. It may be a bare alias *without* the application
      being found; that is a deliberate best-effort, because ``OpenApp`` still
      tries it and its failure at exec time is more informative than a silent
      no-op.
    - ``found`` — **positive evidence** that the application is on this
      machine: an executable on ``PATH``, an existing path, or a Start Menu
      shortcut. ``False`` means "I found nothing", never "not installed": an
      install this machine does not advertise (a portable copy, a custom
      location, a portable app with no Start Menu entry) stays invisible, and
      the UI says so rather than calling it absent.
    - ``via`` — where ``target`` came from, so a caller can explain itself.

    ``resolve_windows_app`` is this function with the verdict stripped off;
    there is one decision tree, not two.
    """
    try:
        name = sanitize_app_name(app).lower()
    except SecurityError:
        logger.warning("Blocked app resolution for unsafe name: %r", app)
        return {"target": None, "found": False, "via": "rejected"}

    if name in WINDOWS_APP_LAUNCH:
        # The builtin table says *how* to launch, never *that* anything is
        # installed — `spotify:` is a URI scheme that resolves only when
        # Spotify is present. So the target stays the builtin form (it is what
        # `OpenApp` wants) while `found` is earned from real evidence, checked
        # exactly as it would be for any other name.
        return {
            "target": WINDOWS_APP_LAUNCH[name][0],
            "found": _locate(name, shortcut_index=shortcut_index) is not None,
            "via": "builtin",
        }

    if name in APP_PATHS:
        hit = _locate(name, shortcut_index=shortcut_index)
        if hit:
            return {"target": hit[0], "found": True, "via": hit[1]}
        # Best effort: `OpenApp` attempts the alias and reports honestly.
        return {"target": APP_PATHS[name][0], "found": False, "via": "alias"}

    if _DETECT_EXT.search(name) or os.path.sep in name:
        # A caller-supplied path is honoured only when it exists and is not a
        # script type.
        try:
            validate_app_target(name)
        except SecurityError:
            return {"target": None, "found": False, "via": "rejected"}
        if os.path.exists(name):
            return {"target": name, "found": True, "via": "path"}
        return {"target": None, "found": False, "via": "missing"}

    hit = _locate(name, shortcut_index=shortcut_index)
    if hit:
        return {"target": hit[0], "found": True, "via": hit[1]}
    return {"target": None, "found": False, "via": "none"}


def resolve_windows_app(app: str) -> Optional[str]:
    """Resolve an application name to a launch target, or ``None``.

    A thin view over :func:`resolve_app_detail` — kept at module level (and
    re-exported from ``tools.executor``) because the security-sandbox suite
    tests it directly, and every one of its return values must stay exactly
    what they were before the verdict existed.
    """
    return resolve_app_detail(app)["target"]


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
