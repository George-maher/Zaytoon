import asyncio
import hashlib
import json
import logging
import os
import re
import time
from typing import Dict, Any, List, Optional, Callable

from .base_agent import BaseAgent
from tools.safety import ConfirmationManager, RiskAssessor, RiskLevel
from tools.sandbox import (
    DENIED_CODE_LANGUAGES,
    SecurityError,
    assert_sendkeys_keys,
    assert_sendkeys_text,
    encode_powershell_command,
    powershell_sendkeys_script,
    resolve_workspace,
    sanitize_app_name,
    validate_app_target,
    workspace_root,
)

logger = logging.getLogger("Zaytoon.ToolAgent")

TOOL_DESCRIPTION = (
    '{"action":"tool_name","params":{"key":"value"}} '
    "Tools: open_app, close_app, type_text, key_press, get_time, run_code, search_web, "
    "system_command, calculator, file_read, file_write, system_info"
)

# Matches a tool call object and captures the action name plus the remaining
# key/value pairs. Supports both {"action":..,"p":1} and {"action":..,"params":{..}}.
TOOL_CALL_RE = (
    r'\{\s*"action"\s*:\s*"([^"]+)"'
    r'((?:\s*,\s*"[^"]+"\s*:\s*(?:"[^"]*"|-?\d+(?:\.\d+)?|true|false|null|\{[^}]*\}|\[[^\]]*\]))*)'
    r'\s*\}'
)

# Notices shown when a high-risk action is blocked.
_DECLINED = {
    "en": "Cancelled: user denied a high-risk action.",
    "ar": "اتلغت: المستخدم رفض العملية الخطيرة.",
}
_TIMEOUT = {
    "en": "Cancelled: no confirmation received in time.",
    "ar": "اتلغت: مفيش تأكيد في الوقت المحدد.",
}
_UNAVAILABLE = {
    "en": "Blocked: this action needs confirmation but no approval channel is available.",
    "ar": "متوقف: العملية محتاجة تأكيد ومفيش قناة موافقة متاحة.",
}
_ERROR = {
    "en": "Cancelled: the confirmation request could not be sent.",
    "ar": "اتلغت: مقدرش نبعت طلب التأكيد.",
}


class ToolAgent(BaseAgent):
    def __init__(
        self,
        confirmation_manager: Optional[ConfirmationManager] = None,
        risk_assessor: Optional[RiskAssessor] = None,
    ):
        super().__init__("Tool Agent", max_tokens=256, temperature=0.1)
        self._cache: dict = {}
        self._CACHE_TTL = 300
        self.assessor = risk_assessor or RiskAssessor()
        self.confirmation_manager = confirmation_manager

    def _get_cache_key(self, tool: str, params: dict) -> str:
        raw = f"{tool}:{json.dumps(params, sort_keys=True)}"
        return hashlib.md5(raw.encode()).hexdigest()

    def _get_cached(self, key: str) -> Optional[str]:
        entry = self._cache.get(key)
        if entry and (time.time() - entry["time"]) < self._CACHE_TTL:
            return entry["value"]
        return None

    def _set_cached(self, key: str, value: str):
        self._cache[key] = {"value": value, "time": time.time()}

    async def process(
        self,
        user_message: str,
        context: List[Dict[str, Any]],
        stream_callback: Optional[Callable] = None,
        **kwargs,
    ) -> str:
        lang = kwargs.get("language", "en")

        # A per-request manager (scoped to this websocket session) takes priority
        # over the agent-level default.
        manager = kwargs.get("confirmation_manager", self.confirmation_manager)
        session_id = kwargs.get("session_id", "")

        if lang == "ar":
            sp = (
                f"اختر أداة من: {TOOL_DESCRIPTION}. "
                "أمثلة: 'افتح سبوتيفاي' -> {{\"action\":\"open_app\",\"app\":\"spotify\"}} "
                "اكتب نص -> {{\"action\":\"type_text\",\"text\":\"...\"}} "
                "اضغط زر -> {{\"action\":\"key_press\",\"keys\":\"enter\"}} "
                "الوقت -> {{\"action\":\"get_time\"}} "
                "رد بالعامية المصرية. لخص النتيجة في سطر."
            )
        else:
            sp = (
                f"Choose a tool: {TOOL_DESCRIPTION}. "
                "Examples: 'open spotify' -> {\"action\":\"open_app\",\"app\":\"spotify\"} "
                "type text -> {\"action\":\"type_text\",\"text\":\"...\"} "
                "press key -> {\"action\":\"key_press\",\"keys\":\"enter\"} "
                "what time -> {\"action\":\"get_time\"} "
                "Output JSON only. Summarize result in 1 line."
            )

        messages = [
            {"role": "system", "content": sp},
            {"role": "user", "content": user_message},
        ]

        ai_response = await self.get_ai_response(messages, stream_callback=stream_callback)
        return await self.execute_tools(ai_response, manager, session_id, lang)

    async def execute_tools(
        self,
        ai_response: str,
        confirmation_manager: Optional[ConfirmationManager] = None,
        session_id: str = "",
        language: str = "en",
    ) -> str:
        manager = confirmation_manager or self.confirmation_manager
        matches = re.findall(TOOL_CALL_RE, ai_response)

        if not matches:
            return ai_response

        tasks = []
        for tool_name, params_fragment in matches:
            try:
                params = self._parse_params(params_fragment)
            except json.JSONDecodeError:
                tasks.append(
                    asyncio.sleep(
                        0, result=f"Error: Invalid JSON params for tool '{tool_name}'"
                    )
                )
                continue
            tasks.append(
                self._execute_single_tool(
                    tool_name, params, manager, session_id, language
                )
            )

        results = await asyncio.gather(*tasks, return_exceptions=True)

        tool_results = []
        for result in results:
            if isinstance(result, Exception):
                tool_results.append(f"Tool error: {result}")
            elif result:
                tool_results.append(result)

        return " | ".join(tool_results) if tool_results else ai_response

    @staticmethod
    def _parse_params(fragment: str) -> dict:
        """Rebuild the params object from a captured JSON fragment.

        Handles both the flat form ``{"action": "x", "p": 1}`` and the nested
        form ``{"action": "x", "params": {"p": 1}}``.
        """
        fragment = (fragment or "").strip()
        if not fragment:
            return {}
        raw = fragment.lstrip(",").strip()
        if not raw.startswith("{"):
            raw = "{" + raw + "}"
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and isinstance(parsed.get("params"), dict):
            return parsed["params"]
        return parsed if isinstance(parsed, dict) else {}

    # ------------------------------------------------------------------
    # Safety gate
    # ------------------------------------------------------------------
    async def _request_approval(
        self,
        tool_name: str,
        params: dict,
        manager: Optional[ConfirmationManager],
        session_id: str,
        language: str,
    ) -> Optional[str]:
        """Return ``None`` to proceed, or a refusal string to skip execution."""
        assessment = self.assessor.assess(tool_name, params)
        if assessment.level is not RiskLevel.HIGH:
            return None

        logger.info(
            "High-risk tool '%s' blocked pending approval: %s",
            tool_name, assessment.prompt_text(),
        )

        if manager is None:
            return _UNAVAILABLE.get(language, _UNAVAILABLE["en"])

        try:
            approved = await manager.request(tool_name, params, session_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Confirmation flow error for %s: %s", tool_name, e)
            return _ERROR.get(language, _ERROR["en"])

        if approved:
            return None

        # Tell the user *why* nothing ran: a deliberate "no" reads very
        # differently from a timeout where they may not have been looking.
        outcome = getattr(manager, "last_outcome", None)
        if outcome == ConfirmationManager.OUTCOME_TIMEOUT:
            return _TIMEOUT.get(language, _TIMEOUT["en"])
        if outcome == ConfirmationManager.OUTCOME_UNAVAILABLE:
            return _UNAVAILABLE.get(language, _UNAVAILABLE["en"])
        if outcome == ConfirmationManager.OUTCOME_ERROR:
            return _ERROR.get(language, _ERROR["en"])
        return _DECLINED.get(language, _DECLINED["en"])

    async def _execute_single_tool(
        self,
        tool_name: str,
        params: dict,
        confirmation_manager: Optional[ConfirmationManager] = None,
        session_id: str = "",
        language: str = "en",
    ) -> str:
        params = params or {}
        manager = confirmation_manager or self.confirmation_manager

        # The safety gate runs BEFORE the cache so a cached result can never
        # bypass a required confirmation.
        refusal = await self._request_approval(
            tool_name, params, manager, session_id, language
        )
        if refusal is not None:
            return refusal

        cache_key = self._get_cache_key(tool_name, params)
        cached = self._get_cached(cache_key)
        if cached:
            return f"[Cached] {cached}"

        result = None
        try:
            if tool_name == "open_app" or tool_name == "close_app":
                result = await self._app_control(tool_name, params)
            elif tool_name == "type_text":
                result = await self._type_text_tool(params)
            elif tool_name == "key_press":
                result = await self._key_press_tool(params)
            elif tool_name == "get_time":
                result = self._get_time_tool()
            elif tool_name == "run_code":
                result = await self._run_code_tool(params)
            elif tool_name == "search_web":
                result = await self._search_tool(params.get("query", ""))
            elif tool_name == "system_command":
                result = await self._toolkit_cmd(params.get("command", ""), "cmd")
            elif tool_name == "calculator":
                result = self._calculator_tool(params.get("expression", ""))
            elif tool_name == "file_read":
                result = await self._toolkit_file_op(params.get("path", ""), action="read")
            elif tool_name == "file_write":
                result = await self._toolkit_file_write(
                    params.get("path", ""), params.get("content", "")
                )
            elif tool_name == "system_info":
                result = await self._toolkit_system_info()
            else:
                result = f"Unknown tool: {tool_name}"
        except Exception as e:
            result = f"Tool error: {e}"

        if result and not result.startswith("Error"):
            self._set_cached(cache_key, result)
        return result

    async def _app_control(self, action: str, params: dict) -> str:
        raw_app = params.get("app", "").strip()
        if not raw_app:
            return "Error: No app name provided"
        try:
            # Rejects shell syntax, path traversal, and script targets.
            app = sanitize_app_name(raw_app)
        except SecurityError as e:
            return f"Error: {e}"

        try:
            if action == "open_app":
                if os.name == "nt":
                    from tools.executor import resolve_windows_app
                    target = resolve_windows_app(app)
                    if not target:
                        return f"Error: Could not find application '{app}' on this system."
                    try:
                        validate_app_target(target)
                    except SecurityError as e:
                        return f"Error: {e}"
                    # `cmd /c start` avoids shell-string interpolation; the
                    # target has already been checked and is passed as argv.
                    proc = await asyncio.create_subprocess_exec(
                        "cmd", "/c", "start", "", target,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.sleep(0.2)
                    if proc.returncode not in (0, None):
                        return f"Error: could not launch '{app}'"
                else:
                    await asyncio.create_subprocess_exec(
                        app,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                return f"Opened {app}"
            elif action == "close_app":
                if os.name == "nt":
                    exe = app if app.lower().endswith(".exe") else f"{app}.exe"
                    proc = await asyncio.create_subprocess_exec(
                        "taskkill", "/IM", exe, "/F",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                else:
                    proc = await asyncio.create_subprocess_exec(
                        "pkill", "-f", app,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                stdout, stderr = await proc.communicate()
                if proc.returncode == 0:
                    return f"Closed {app}"
                return f"Could not close {app}: {stderr.decode().strip() or 'no process found'}"
        except SecurityError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error {action} {app}: {e}"

    async def _run_code_tool(self, params: dict) -> str:
        code = params.get("code", "").strip()
        lang = params.get("language", "").strip().lower()
        if not code or not lang:
            return "Error: code and language are required"

        import tempfile

        # Shell interpreters exist to run arbitrary system commands, so they are
        # refused even after the human-in-the-loop gate approves.
        if lang in DENIED_CODE_LANGUAGES:
            return f"Error: language '{lang}' is not permitted for code execution"

        ext_map = {"python": ".py", "javascript": ".js", "js": ".js"}
        cmd_map = {
            "python": ["python", "{file}"],
            "javascript": ["node", "{file}"],
            "js": ["node", "{file}"],
        }

        ext = ext_map.get(lang, ".txt")
        cmd_template = cmd_map.get(lang)
        if not cmd_template:
            return f"Error: Unsupported language '{lang}'"

        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=ext, delete=False, encoding="utf-8")
        try:
            tmp.write(code)
            tmp.close()
            cmd = [p.replace("{file}", tmp.name) for p in cmd_template]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=tempfile.gettempdir(),
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            except asyncio.TimeoutError:
                proc.kill()
                return "Error: Execution timed out (15s)"
            output = stdout.decode("utf-8", errors="replace").strip()
            error = stderr.decode("utf-8", errors="replace").strip()
            if error:
                return f"Output: {output}\nError: {error}" if output else f"Error: {error}"
            return output or "(no output)"
        finally:
            try:
                os.unlink(tmp.name)
            except (OSError, NameError):
                pass

    async def _search_tool(self, query: str) -> str:
        if not query.strip():
            return "Error: No search query provided"
        try:
            from ddgs import DDGS
            ddgs = DDGS()
            loop = asyncio.get_running_loop()
            results = await loop.run_in_executor(
                None, lambda: list(ddgs.text(query, max_results=3))
            )
            if not results:
                return "No results found."
            lines = []
            for i, r in enumerate(results, 1):
                lines.append(f"{i}. {r.get('title', 'No title')}\n{r.get('body', 'No description')}")
            return "\n\n".join(lines)
        except Exception as e:
            return f"Search error: {e}"

    def _calculator_tool(self, expression: str) -> str:
        import ast
        if not expression.strip():
            return "Error: No expression provided"
        cleaned = re.sub(r"[^0-9+\-*/().,%\s]", "", expression)
        try:
            tree = ast.parse(cleaned, mode="eval")
            # `ast.Parens` does not exist (parens are not a node in the AST), so
            # the allow-list deliberately covers operators/nodes only.
            allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
                       ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv,
                       ast.Mod, ast.Pow, ast.USub, ast.UAdd)
            for node in ast.walk(tree):
                if not isinstance(node, allowed):
                    return f"Error: Disallowed expression"
            result = eval(compile(tree, "<string>", "eval"), {"__builtins__": {}}, {})
            return str(result)
        except Exception as e:
            return f"Error: {e}"

    async def _toolkit_file_op(self, path: str, action: str = "read") -> str:
        from tools.base import OSToolkit
        tk = OSToolkit()
        result = await tk.execute("file", path=path, action=action)
        if result.success:
            return result.output
        return f"Error: {result.error}"

    async def _toolkit_file_write(self, path: str, content: str) -> str:
        if not path.strip():
            return "Error: No path provided"
        try:
            # Validate containment and resolve in one step. Checking with
            # `is_safe_path` and then writing to `os.path.abspath(path)` checked
            # a relative path against the workspace but opened it against the
            # process cwd — two different roots.
            safe_path = resolve_workspace(path)
            if not safe_path:
                return (
                    "Error: Access denied - path outside the workspace "
                    f"({workspace_root()}): {path}"
                )
            parent = os.path.dirname(safe_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(safe_path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"Written to {path}"
        except SecurityError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error: {e}"

    async def _toolkit_cmd(self, command: str, shell: str = "cmd") -> str:
        from tools.base import OSToolkit
        tk = OSToolkit()
        result = await tk.execute("cmd", command=command)
        if result.success:
            return result.output
        return f"Error: {result.error}"

    async def _toolkit_system_info(self) -> str:
        from tools.base import OSToolkit
        tk = OSToolkit()
        result = await tk.execute("system_info")
        if result.success:
            return result.output
        return f"Error: {result.error}"

    async def _type_text_tool(self, params: dict) -> str:
        text = (params.get("text") or "").strip()
        if not text:
            return "Error: No text provided"
        try:
            # Literal text: SendKeys metacharacters are data, so escape them.
            payload = assert_sendkeys_text(text)
        except SecurityError as e:
            return f"Error: {e}"
        return await self._send_keys(payload, f"Typed: {text[:80]}")

    async def _key_press_tool(self, params: dict) -> str:
        keys = (params.get("keys") or "").strip()
        if not keys:
            return "Error: No keys provided"
        try:
            # A key combination: `+`/`^`/`%` are the modifier syntax, so they
            # must survive unescaped or `ctrl+c` would stop being ctrl+c.
            payload = assert_sendkeys_keys(keys)
        except SecurityError as e:
            return f"Error: {e}"
        return await self._send_keys(payload, f"Pressed: {keys}")

    async def _send_keys(self, payload: str, ok_message: str) -> str:
        """Deliver keystrokes to the focused window.

        Windows uses ``powershell -EncodedCommand``, so the payload travels as
        base64 UTF-16LE and is never placed on a command line. The previous
        implementation interpolated it into ``-Command "…SendWait('<text>')"``
        with only single-quote doubling, at a permission level that never
        prompted — a crafted ``text`` could close the literal and append
        statements. Encoding removes the escaping problem entirely.
        """
        if os.name == "nt":
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
                    return f"Error: {err}"
                return ok_message
            except FileNotFoundError:
                return "Error: powershell not found on this system"
            except Exception as e:
                return f"Error: {e}"

        try:
            proc = await asyncio.create_subprocess_exec(
                "xdotool", "type", "--clearmodifiers", payload,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            return ok_message
        except FileNotFoundError:
            return "Error: xdotool not found. Install with: sudo apt install xdotool"
        except Exception as e:
            return f"Error: {e}"

    def _get_time_tool(self) -> str:
        from datetime import datetime
        now = datetime.now()
        return f"It's {now.strftime('%I:%M %p')} on {now.strftime('%A, %B %d, %Y')}"
