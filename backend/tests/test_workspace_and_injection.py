"""Security fixes introduced in the workspace/powershell hardening pass.

These are regression guards for specific defects found in the audit. Each test
names the defect it prevents from coming back.
"""

import base64
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import sandbox  # noqa: E402
from tools.safety import (  # noqa: E402
    PERMISSION_CONFIRM,
    RiskAssessor,
    RiskLevel,
)


# ---------------------------------------------------------------------------
# 1. PowerShell injection through keystroke payloads
# ---------------------------------------------------------------------------
class TestPowerShellInjection:
    """The payload must never be placed on a command line.

    The defect: ``_type_text_tool``/``_key_press_tool`` built
    ``powershell -Command "...SendWait('<text>')"`` with only ``'`` -> ``''``
    escaping, and ran at ``RiskLevel.ELEVATED`` which never prompts. A crafted
    ``text`` could close the PowerShell string literal and append statements.
    """

    def test_encoding_keeps_the_payload_off_the_command_line(self):
        script = sandbox.powershell_sendkeys_script("'; Remove-Item C:\\x; #")
        encoded = sandbox.encode_powershell_command(script)
        # The dangerous text is not literally present in the encoded form.
        assert "Remove-Item" not in encoded
        assert "'; " not in encoded
        # And it decodes back to exactly the script.
        assert base64.b64decode(encoded).decode("utf-16-le") == script

    def test_encoded_command_is_utf16le_base64(self):
        encoded = sandbox.encode_powershell_command("Write-Host 'x'")
        raw = base64.b64decode(encoded)
        assert raw.decode("utf-16-le") == "Write-Host 'x'"

    def test_single_quotes_are_doubled_in_the_script(self):
        script = sandbox.powershell_sendkeys_script("it's here")
        # PowerShell escapes an embedded quote by doubling it.
        assert "it''s here" in script
        assert script.count("'") % 2 == 0, "quotes must balance"

    def test_script_shape_is_a_single_sendwait_call(self):
        script = sandbox.powershell_sendkeys_script("hello")
        assert script.startswith("Add-Type -AssemblyName System.Windows.Forms;")
        assert script.count("SendWait") == 1

    @pytest.mark.parametrize(
        "payload",
        [
            "'; Remove-Item -Recurse C:\\ #",
            "'); Start-Process calc; #",
            "'; [System.Reflection.Assembly]::LoadWithPartialName('x'); #",
            "\" ; whoami ; \"",
        ],
    )
    def test_injection_payloads_survive_only_as_escaped_data(self, payload):
        """A breakout attempt must remain inside the string literal."""
        script = sandbox.powershell_sendkeys_script(payload)
        literal = script[script.index("SendWait('") + len("SendWait('"):]
        assert literal.endswith("')")
        # Nothing may terminate the literal early: every quote is doubled, so
        # the number of quotes in the whole script is even.
        assert script.count("'") % 2 == 0, script

    def test_type_text_escapes_sendkeys_metacharacters(self):
        # Literal text: `+` is data, so it must become `{+}`.
        assert sandbox.assert_sendkeys_text("1+1") == "1{+}1"
        assert sandbox.assert_sendkeys_text("a^b") == "a{^}b"
        assert sandbox.assert_sendkeys_text("50%") == "50{%}"
        assert sandbox.assert_sendkeys_text("f(x)") == "f{(}x{)}"
        assert sandbox.assert_sendkeys_text("{a}") == "{{}a{}}"

    def test_key_press_keeps_modifier_syntax_intact(self):
        # Key combinations: `+`/`^`/`%` ARE the modifier syntax. Escaping them
        # would break the very shortcut the caller asked for.
        assert sandbox.assert_sendkeys_keys("^c") == "^c"
        assert sandbox.assert_sendkeys_keys("+a") == "+a"
        assert sandbox.assert_sendkeys_keys("%{F4}") == "%{F4}"
        assert sandbox.assert_sendkeys_keys("enter") == "enter"

    def test_plain_text_is_untouched(self):
        assert sandbox.assert_sendkeys_text("hello world") == "hello world"
        assert sandbox.assert_sendkeys_text("café مرحبا") == "café مرحبا"

    def test_empty_and_none_payloads_are_refused(self):
        for bad in ("", "   ", None):
            with pytest.raises(sandbox.SecurityError):
                sandbox.assert_sendkeys_text(bad)
            with pytest.raises(sandbox.SecurityError):
                sandbox.assert_sendkeys_keys(bad)

    def test_oversized_payload_is_refused(self):
        with pytest.raises(sandbox.SecurityError) as exc:
            sandbox.assert_sendkeys_text("a" * (sandbox.MAX_SENDKEYS_CHARS + 1))
        assert "too long" in str(exc.value)

    def test_null_byte_is_refused(self):
        with pytest.raises(sandbox.SecurityError) as exc:
            sandbox.assert_sendkeys_text("ok\x00evil")
        assert "null byte" in str(exc.value)

    def test_keystroke_actions_are_confirmation_required(self):
        """They used to be ELEVATED, which never prompts."""
        assessor = RiskAssessor()
        for action in ("type_text", "key_press"):
            result = assessor.assess(action, {"text": "hi", "keys": "^c"})
            assert result.level is RiskLevel.HIGH, action
            assert result.permission == PERMISSION_CONFIRM, action


# ---------------------------------------------------------------------------
# 2. The removed ungated WebSocket command channel
# ---------------------------------------------------------------------------
class TestNoUngatedCommandChannel:
    """`{"type":"command"}` reached OSToolkit with no RiskAssessor at all.

    A WebSocket handshake is not subject to CORS, so any web page the user
    visited could open ws://127.0.0.1:8000/ws and drive it.
    """

    def test_websocket_handler_has_no_command_branch(self):
        source = Path(sandbox.__file__).resolve().parent.parent / "api" / "websocket.py"
        text = source.read_text(encoding="utf-8")
        assert 'msg_type == "command"' not in text, (
            "the ungated `command` WebSocket branch has been reintroduced"
        )
        assert "execute_command" not in text

    def test_agent_service_no_longer_exposes_execute_command(self):
        import ast

        source = (
            Path(sandbox.__file__).resolve().parent.parent
            / "services" / "agent_service.py"
        )
        tree = ast.parse(source.read_text(encoding="utf-8"))
        # Parsed rather than grepped: the file carries a comment quoting the
        # removed signature, and that note should stay.
        defined = {
            node.name
            for cls in ast.walk(tree)
            if isinstance(cls, ast.ClassDef)
            for node in cls.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert "execute_command" not in defined, (
            "AgentService.execute_command bypasses the risk gate; do not restore it"
        )

    def test_no_client_sends_a_command_message(self):
        """The browser hook is the only shipped client; it has no such sender."""
        hook = (
            Path(sandbox.__file__).resolve().parents[2]
            / "frontend" / "src" / "hooks" / "useWebSocket.js"
        )
        if not hook.is_file():
            pytest.skip("frontend not present")
        text = hook.read_text(encoding="utf-8")
        assert 'type: "command"' not in text

    def test_every_remaining_tool_path_goes_through_the_executor(self):
        """Both REST tool routes must be gated."""
        source = Path(sandbox.__file__).resolve().parent.parent / "api" / "main.py"
        text = source.read_text(encoding="utf-8")
        assert "risk_assessor.assess" in text, (
            "POST /command/run must keep its risk assessment"
        )


# ---------------------------------------------------------------------------
# 3. File tools resolve relative paths against the workspace, not the cwd
# ---------------------------------------------------------------------------
class TestRelativePathResolution:
    """`is_safe_path` checked one root while `os.path.abspath` opened another.

    With the root moved off the cwd, a relative path was validated against the
    workspace and then opened relative to the process directory.
    """

    @pytest.fixture
    def workspace(self, monkeypatch, tmp_path):
        root = tmp_path / "workspace"
        (root / "sub").mkdir(parents=True)
        monkeypatch.setattr(sandbox, "workspace_root", lambda: str(root))
        return root

    def test_relative_path_resolves_against_the_workspace(self, workspace):
        resolved = sandbox.resolve_workspace("sub/file.txt")
        assert Path(resolved) == (workspace / "sub" / "file.txt").resolve()

    def test_relative_path_is_inside_even_when_cwd_elsewhere(
        self, workspace, monkeypatch, tmp_path
    ):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        assert sandbox.is_safe_path("sub/file.txt") is True

    def test_a_relative_path_anchors_to_the_workspace_not_the_cwd(
        self, workspace, monkeypatch, tmp_path
    ):
        """A relative path resolves inside the workspace, period.

        The point is not that it is refused — it is that it can only ever point
        at the workspace. A file sitting in the process cwd is simply not
        reachable by a relative path, which is the property that matters now
        that the root and the cwd are different directories.
        """
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "secret.txt").write_text("stolen", encoding="utf-8")
        monkeypatch.chdir(elsewhere)

        resolved = sandbox.resolve_workspace("secret.txt")
        assert Path(resolved) == (workspace / "secret.txt").resolve()
        assert str(elsewhere) not in resolved
        assert sandbox.is_safe_path("secret.txt") is True

    def test_parent_traversal_is_refused(self, workspace):
        assert sandbox.resolve_workspace("../escape.txt") == ""
        assert sandbox.resolve_workspace("sub/../../escape.txt") == ""

    def test_workspace_root_itself_is_allowed(self, workspace):
        assert sandbox.is_safe_path(str(workspace)) is True

    def test_sibling_prefix_is_refused(self, workspace, tmp_path):
        sibling = tmp_path / "workspace_evil"
        sibling.mkdir()
        (sibling / "secret.txt").write_text("stolen", encoding="utf-8")
        assert sandbox.is_safe_path(str(sibling / "secret.txt")) is False


# ---------------------------------------------------------------------------
# 4. Unconditional shell-syntax blocks precede the approval gate
# ---------------------------------------------------------------------------
class TestUnconditionalBlocksPrecedeTheGate:
    """Approving one command must never authorise what follows a separator."""

    def _executor(self):
        from tools.executor import ToolExecutor

        return ToolExecutor()

    @pytest.mark.parametrize(
        "command",
        [
            "echo hi & hostname",
            "echo hi && shutdown /s",
            "echo hi | net user",
            "echo hi; del C:\\x.txt",
            "echo $(whoami)",
            "echo `whoami`",
            "echo %PATH%",
            "echo hi > C:\\x.txt",
        ],
    )
    async def test_chaining_is_blocked_without_asking(self, command):
        result = await self._executor().execute("system_command", {"command": command})
        assert result["success"] is False
        assert result.get("blocked") is True, command
        # Crucially it is NOT reported as "needs confirmation": confirming it
        # would not help.
        assert not result.get("requires_confirmation"), command

    async def test_a_harmless_command_does_ask(self):
        result = await self._executor().execute("system_command", {"command": "echo hi"})
        assert result.get("requires_confirmation") is True

    async def test_empty_command_is_a_prevalidation_error(self):
        result = await self._executor().execute("system_command", {"command": "  "})
        assert result["success"] is False
        assert "No command" in result["error"]
        assert not result.get("requires_confirmation")
