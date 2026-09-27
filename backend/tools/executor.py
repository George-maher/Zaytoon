import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import platform
from datetime import datetime, timedelta
from typing import Optional

from tools.base import OSToolkit
from tools.sandbox import (
    DENIED_CODE_LANGUAGES,
    SecurityError,
    assert_sendkeys_keys,
    assert_sendkeys_text,
    encode_powershell_command,
    is_safe_path,
    powershell_sendkeys_script,
    sanitize_app_name,
    split_command,
    validate_app_target,
)
from tools.safety import ConfirmationManager, RiskAssessor, RiskLevel

logger = logging.getLogger("Zaytoon.ToolExecutor")

APP_PATHS: dict[str, list[str]] = {
    "spotify": ["spotify"],
    "chrome": ["chrome"],
    "google chrome": ["chrome"],
    "firefox": ["firefox"],
    "edge": ["msedge"],
    "microsoft edge": ["msedge"],
    "vscode": ["code"],
    "vs code": ["code"],
    "visual studio code": ["code"],
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

WINDOWS_APP_LAUNCH: dict[str, list[str]] = {
    "spotify": ["spotify:", r"shell:AppsFolder\SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"],
    "calculator": ["calc"],
    "settings": ["ms-settings:"],
}

DETECT_EXT = re.compile(r'\.\w+$')

#: Shell builtins that have no standalone executable and therefore need the
#: shell binary to be invoked with a pre-validated argument vector.
SHELL_BUILTIN_NAMES = frozenset(
    {"echo", "dir", "date", "time", "ver", "vol", "cls", "type", "copy",
     "move", "del", "ren", "md", "rd", "set", "path", "title", "start"}
)

POSIX_BUILTIN_NAMES = frozenset(
    {"echo", "pwd", "cd", "export", "set", "exit", "true", "false", ":",
     "read", "test", "eval", "printf", "wait", "umask", "alias"}
)


def _find_start_menu_shortcut(app: str) -> Optional[str]:
    app_lower = app.strip().lower()
    search_dirs = [
        os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
        os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Microsoft", "Windows", "Start Menu", "Programs"),
    ]
    for base in search_dirs:
        if not base or not os.path.isdir(base):
            continue
        for root, _, files in os.walk(base):
            for f in files:
                if not f.lower().endswith(".lnk"):
                    continue
                name = os.path.splitext(f)[0].lower()
                if name == app_lower or app_lower in name:
                    return os.path.join(root, f)
    return None


def resolve_windows_app(app: str) -> Optional[str]:
    """Resolve an application name to a launch target.

    The result is only ever an allow-listed alias, a PATH-resolved executable,
    a Start Menu shortcut, or a caller-supplied path. Names carrying shell
    syntax are rejected instead of being returned verbatim, because callers may
    hand the result to `cmd /c start`.
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

    if DETECT_EXT.search(app) or os.path.sep in app:
        # A caller-supplied path is only honoured when it really exists and is
        # not a script type.
        try:
            validate_app_target(app)
        except SecurityError:
            return None
        return app if os.path.exists(app) else None

    found = shutil.which(app) or shutil.which(app + ".exe")
    if found:
        return found

    lnk = _find_start_menu_shortcut(app)
    if lnk:
        return lnk

    return None


class ToolExecutor:
    """Single chokepoint for OS tool execution.

    Every action passes through :meth:`execute`, which applies the risk gate
    *before* dispatch. Handlers therefore cannot be reached with a high-risk
    request unless a human approved it.
    """

    def __init__(
        self,
        toolkit: Optional[OSToolkit] = None,
        reminder_service=None,
        scheduler=None,
        confirmation_manager: Optional[ConfirmationManager] = None,
        risk_assessor: Optional[RiskAssessor] = None,
    ):
        self.toolkit = toolkit or OSToolkit()
        self.reminder = reminder_service
        self.scheduler = scheduler
        self.assessor = risk_assessor or RiskAssessor()
        self.confirmation_manager = confirmation_manager
        self.is_windows = os.name == "nt"

    async def execute(self, action: str, params: dict) -> dict:
        logger.info("ToolExecutor: action=%s params=%s", action, params)
        try:
            params = params or {}
            handler = getattr(self, f"_action_{action}", None)
            if not handler:
                return {"success": False, "error": f"Unknown action: {action}"}

            # Argument validation runs first: a malformed request should get a
            # precise error rather than an approval prompt for something that
            # could not execute anyway.
            invalid = self._prevalidate(action, params)
            if invalid is not None:
                return invalid

            gate = await self._risk_gate(action, params)
            if gate is not None:
                return gate

            result = await handler(params)
            logger.info("ToolExecutor result: %s", result)
            return result
        except SecurityError as e:
            logger.warning("ToolExecutor security stop on %s: %s", action, e)
            return {"success": False, "error": str(e), "blocked": True}
        except Exception as e:
            logger.error("ToolExecutor error for %s: %s", action, e)
            return {"success": False, "error": str(e)}

    def _prevalidate(self, action: str, params: dict) -> Optional[dict]:
        """Cheap argument checks that must precede the risk gate.

        Two kinds of check live here:

        * **Malformed arguments** — a precise error beats an approval prompt for
          something that could not have run anyway.
        * **Unconditional blocks** — shell metacharacters and command chaining,
          which ``sandbox.split_command`` refuses outright. These are *not*
          approval-overridable: the sandbox exists precisely so that approving
          one command cannot authorise whatever follows a separator. Running
          them here rather than inside the handler means the refusal is reported
          regardless of whether a confirmation channel exists, instead of the
          caller being asked to approve a command that was always going to be
          rejected.
        """
        if action in ("open_app", "close_app"):
            if not (params.get("app") or params.get("name")):
                return {"success": False, "error": "No app name provided"}
        elif action == "run_code":
            if not (params.get("code") or "").strip():
                return {"success": False, "error": "No code provided"}
            if not (params.get("language") or "").strip():
                return {"success": False, "error": "No language specified"}
            if not self._get_runner(params["language"].strip().lower()):
                return {
                    "success": False,
                    "error": f"Unsupported language: {params['language']}",
                }
        elif action in ("system_command", "cmd", "run_shell", "exec"):
            command = (params.get("command") or "").strip()
            if not command:
                return {"success": False, "error": "No command provided"}
            try:
                split_command(command)
            except SecurityError as e:
                return {"success": False, "error": str(e), "blocked": True}
        elif action in ("search_web", "file_read", "file_write"):
            key = {
                "system_command": "command",
                "search_web": "query",
                "file_read": "path",
                "file_write": "path",
            }[action]
            if not (params.get(key) or "").strip():
                return {"success": False, "error": f"No {key} provided"}
        elif action in ("create_reminder",):
            if not (params.get("text") or params.get("task") or params.get("title")):
                return {"success": False, "error": "No reminder text provided"}
        return None

    async def _risk_gate(self, action: str, params: dict) -> Optional[dict]:
        """Return an error dict when the call must not proceed, else None."""
        assessment = self.assessor.assess(action, params)
        if assessment.level is not RiskLevel.HIGH:
            return None

        logger.warning(
            "ToolExecutor: %s-level %s requires approval: %s",
            assessment.permission, action, assessment.prompt_text(),
        )
        manager = self.confirmation_manager
        if manager is None:
            return {
                "success": False,
                "requires_confirmation": True,
                "level": assessment.level.value,
                "permission": assessment.permission,
                "reasons": assessment.reasons,
                "summary": assessment.summary,
                "error": "High-risk action requires confirmation.",
            }
        try:
            approved = await manager.request(action, params)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Confirmation channel failed for %s: %s", action, e)
            approved = False
        if not approved:
            return {
                "success": False,
                "error": "Cancelled: high-risk action was not approved.",
                "cancelled": True,
            }
        return None

    async def _action_open_app(self, params: dict) -> dict:
        # Rejects shell syntax, path traversal, and script targets.
        app = sanitize_app_name(params.get("app") or params.get("name") or "")
        app = app.lower()

        try:
            if self.is_windows:
                return await self._open_app_windows(app)
            cmd = self._resolve_app_command(app)
            if not cmd:
                return {"success": False, "error": f"Unknown application '{app}'."}
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            return {"success": True, "message": f"Opened {app}", "pid": getattr(proc, 'pid', None)}
        except FileNotFoundError:
            return {"success": False, "error": f"Application '{app}' not found on this system."}
        except SecurityError as e:
            return {"success": False, "error": str(e), "blocked": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _open_app_windows(self, app: str) -> dict:
        target = resolve_windows_app(app)
        if not target:
            return {"success": False, "error": f"Could not find application '{app}' on this system."}
        try:
            validate_app_target(target)
        except SecurityError as e:
            return {"success": False, "error": str(e), "blocked": True}

        try:
            # `cmd /c start` keeps the documented behaviour for shell builtins
            # and URI schemes (spotify:, ms-settings:), but the target has been
            # checked for shell metacharacters and must also be quoted.
            proc = await asyncio.create_subprocess_exec(
                "cmd", "/c", "start", "", target,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            # Give the shell a moment to fail on a bad target.
            await asyncio.sleep(0.2)
            if proc.returncode not in (0, None):
                return {
                    "success": False,
                    "error": f"Could not launch '{app}' (exit {proc.returncode})",
                }
            return {"success": True, "message": f"Opened {app}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _action_close_app(self, params: dict) -> dict:
        app = sanitize_app_name(params.get("app") or params.get("name") or "").lower()

        if self.is_windows:
            exe_name = self._app_to_exe(app)
            cmd = ["taskkill", "/IM", exe_name, "/F"]
        else:
            cmd = ["pkill", "-f", app]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                return {"success": True, "message": f"Closed {app}"}
            return {"success": False, "error": stderr.decode().strip() or f"No running process found for '{app}'"}
        except SecurityError as e:
            return {"success": False, "error": str(e), "blocked": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _action_run_code(self, params: dict) -> dict:
        code = params.get("code", "").strip()
        language = (params.get("language") or "").lower().strip()
        if not code:
            return {"success": False, "error": "No code provided"}
        if not language:
            return {"success": False, "error": "No language specified"}

        runner = self._get_runner(language)
        if not runner:
            return {"success": False, "error": f"Unsupported language: {language}"}

        suffix = runner.get("ext", ".txt")
        exec_cmd = runner["cmd"]

        # Defence in depth: even after approval, refuse obviously destructive
        # interpreters rather than trusting the model's output.
        if language in DENIED_CODE_LANGUAGES:
            return {
                "success": False,
                "error": f"Language '{language}' is not permitted for code execution.",
                "blocked": True,
            }

        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=suffix, delete=False, encoding="utf-8"
        )
        try:
            tmp.write(code)
            tmp.close()
            full_cmd = [part.replace("{file}", tmp.name) for part in exec_cmd]

            proc = await asyncio.create_subprocess_exec(
                *full_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=tempfile.gettempdir(),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

            return {
                "success": proc.returncode == 0,
                "output": stdout.decode("utf-8", errors="replace").strip(),
                "error": stderr.decode("utf-8", errors="replace").strip(),
                "exit_code": proc.returncode,
            }
        except asyncio.TimeoutError:
            return {"success": False, "error": "Execution timed out (30s)"}
        except Exception as e:
            return {"success": False, "error": str(e)}
        finally:
            try:
                os.unlink(tmp.name)
            except (OSError, NameError):
                pass

    async def _action_system_command(self, params: dict) -> dict:
        command = params.get("command", "").strip()
        if not command:
            return {"success": False, "error": "No command provided"}

        # Tokenise and reject shell syntax. A high-risk command is only
        # reached here *after* the risk gate approved it, but chaining
        # (`del x & shutdown`) is still refused outright: approval is granted
        # for one command, not for whatever follows a separator.
        parts = split_command(command)

        # Shell builtins (`echo`, `dir`, `date`, ...) have no standalone
        # executable, so they are dispatched through the shell binary with an
        # already-validated argument vector. Because `parts` provably contains
        # no metacharacters, the shell has no syntax left to interpret.
        if shutil.which(parts[0]) is None:
            if self.is_windows:
                if os.path.splitext(parts[0].lower())[0] not in SHELL_BUILTIN_NAMES:
                    return {"success": False, "error": f"Command not found: {parts[0]}"}
                argv = [os.environ.get("COMSPEC", "cmd.exe"), "/c", *parts]
            else:
                if parts[0] not in POSIX_BUILTIN_NAMES:
                    return {"success": False, "error": f"Command not found: {parts[0]}"}
                argv = ["/bin/sh", "-c", " ".join(parts)]
        else:
            argv = parts

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode == 0:
                return {"success": True, "output": stdout.decode("utf-8", errors="replace").strip()[:10000]}
            return {"success": False, "error": stderr.decode("utf-8", errors="replace").strip()[:1000]}
        except FileNotFoundError:
            return {"success": False, "error": f"Command not found: {parts[0]}"}
        except asyncio.TimeoutError:
            return {"success": False, "error": "Command timed out"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _action_search_web(self, params: dict) -> dict:
        query = params.get("query", "").strip()
        if not query:
            return {"success": False, "error": "No search query provided"}
        try:
            from ddgs import DDGS
            ddgs = DDGS()
            loop = asyncio.get_running_loop()
            results = await loop.run_in_executor(
                None, lambda: list(ddgs.text(query, max_results=5))
            )
            if not results:
                return {"success": True, "results": [], "summary": "No results found."}
            lines = []
            for i, r in enumerate(results, 1):
                title = r.get("title", "No title")
                body = r.get("body", "")
                lines.append(f"{i}. {title}\n   {body[:200]}")
            return {
                "success": True,
                "results": results,
                "summary": "\n\n".join(lines),
            }
        except ImportError:
            return {"success": False, "error": "ddgs (duckduckgo-search) not installed."}
        except Exception as e:
            return {"success": False, "error": f"Search error: {e}"}

    async def _action_type_text(self, params: dict) -> dict:
        text = (params.get("text") or "").strip()
        if not text:
            return {"success": False, "error": "No text provided to type"}
        try:
            # Literal text: SendKeys metacharacters are data, so escape them.
            payload = assert_sendkeys_text(text)
        except SecurityError as e:
            return {"success": False, "error": str(e), "blocked": True}
        return await self._send_keys(payload, f"Typed: {text[:80]}{'...' if len(text) > 80 else ''}")

    async def _action_key_press(self, params: dict) -> dict:
        keys = (params.get("keys") or "").strip()
        if not keys:
            return {"success": False, "error": "No keys provided"}
        try:
            # A key combination: `+`/`^`/`%` are the modifier syntax and must
            # survive unescaped, or `ctrl+c` would stop being ctrl+c.
            payload = assert_sendkeys_keys(keys)
        except SecurityError as e:
            return {"success": False, "error": str(e), "blocked": True}
        return await self._send_keys(payload, f"Pressed: {keys}")

    async def _send_keys(self, payload: str, ok_message: str) -> dict:
        """Deliver keystrokes to the focused window.

        Windows uses ``powershell -EncodedCommand``, so the payload travels as
        base64 UTF-16LE and never appears on a command line. Building a
        ``-Command "...SendWait('<text>')"`` string instead means any escaping
        mistake becomes arbitrary PowerShell execution.
        """
        if self.is_windows:
            try:
                script = powershell_sendkeys_script(payload)
                proc = await asyncio.create_subprocess_exec(
                    "powershell", "-NoProfile", "-NonInteractive",
                    "-EncodedCommand", encode_powershell_command(script),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await proc.communicate()
                if proc.returncode != 0:
                    err = stderr.decode("utf-8", errors="replace").strip()
                    return {"success": False, "error": f"SendKeys failed: {err}"}
                return {"success": True, "message": ok_message}
            except FileNotFoundError:
                return {"success": False, "error": "powershell not found on this system"}
            except Exception as e:
                return {"success": False, "error": f"Key delivery failed: {e}"}

        try:
            if ok_message.startswith("Pressed"):
                argv = ["xdotool", "key", "--clearmodifiers", payload]
            else:
                argv = ["xdotool", "type", "--clearmodifiers", payload]
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                err = stderr.decode("utf-8", errors="replace").strip()
                return {"success": False, "error": f"xdotool failed: {err}"}
            return {"success": True, "message": ok_message}
        except FileNotFoundError:
            return {
                "success": False,
                "error": "xdotool not found. Install with: sudo apt install xdotool",
            }
        except Exception as e:
            return {"success": False, "error": f"Key delivery failed: {e}"}

    async def _action_get_time(self, params: dict) -> dict:
        now = datetime.now()
        time_str = now.strftime("%I:%M %p")
        date_str = now.strftime("%A, %B %d, %Y")
        return {
            "success": True,
            "time": time_str,
            "date": date_str,
            "datetime": now.isoformat(),
            "message": f"It's {time_str} on {date_str}",
        }

    async def _action_create_reminder(self, params: dict) -> dict:
        text = params.get("text") or params.get("task") or params.get("title", "")
        time_str = params.get("time") or params.get("datetime", "")
        if not text:
            return {"success": False, "error": "No reminder text provided"}
        if not time_str:
            return {"success": False, "error": "No time provided"}

        if self.reminder:
            scheduled = datetime.fromisoformat(time_str)
            result = await self.reminder.create(
                task=text, scheduled_time=scheduled,
            )
            if result["success"]:
                return {"success": True, "message": f"Reminder set for {time_str}: {text}"}
            return {"success": False, "error": result.get("error", "Failed to create reminder")}

        return {"success": False, "error": "Reminder service not available"}

    async def _action_schedule_task(self, params: dict) -> dict:
        if self.scheduler:
            result = await self.scheduler.create_from_natural(
                text=params.get("task") or params.get("text", ""),
                language=params.get("language", "en"),
            )
            return {
                "success": result.get("success", False),
                "message": result.get("confirmation", str(result)),
            }
        return {"success": False, "error": "Scheduler not available"}

    def _resolve_app_command(self, app: str) -> Optional[list[str]]:
        app = app.strip().lower()
        if app in APP_PATHS:
            return APP_PATHS[app]

        try:
            if self.is_windows:
                return ["cmd", "/c", "start", "", app]
            return [app]
        except Exception:
            return None

    def _app_to_exe(self, app: str) -> str:
        app = app.strip().lower()
        if app in APP_PATHS:
            candidate = APP_PATHS[app][0]
            if not DETECT_EXT.search(candidate):
                return candidate + ".exe"
            return candidate
        return app + ".exe" if not DETECT_EXT.search(app) else app

    def _get_runner(self, language: str) -> Optional[dict]:
        runners = {
            "python": {"cmd": [sys.executable, "{file}"], "ext": ".py"},
            "python3": {"cmd": [sys.executable, "{file}"], "ext": ".py"},
            "javascript": {"cmd": ["node", "{file}"], "ext": ".js"},
            "js": {"cmd": ["node", "{file}"], "ext": ".js"},
            "typescript": {"cmd": ["npx", "ts-node", "{file}"], "ext": ".ts"},
            "ts": {"cmd": ["npx", "ts-node", "{file}"], "ext": ".ts"},
            "bash": {"cmd": ["bash", "{file}"], "ext": ".sh", "shell": True},
            "sh": {"cmd": ["bash", "{file}"], "ext": ".sh", "shell": True},
            "shell": {"cmd": ["bash", "{file}"], "ext": ".sh", "shell": True},
            "powershell": {"cmd": ["powershell", "-File", "{file}"], "ext": ".ps1"},
            "sqlite": {"cmd": ["sqlite3", "{file}"], "ext": ".sql"},
            "ruby": {"cmd": ["ruby", "{file}"], "ext": ".rb"},
            "go": {"cmd": ["go", "run", "{file}"], "ext": ".go"},
            "rust": {"cmd": ["rustc", "{file}", "-o", "{file}.out"], "ext": ".rs"},
        }
        return runners.get(language)
