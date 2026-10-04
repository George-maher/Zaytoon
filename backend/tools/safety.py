"""Tool execution safety sandbox.

:class:`RiskAssessor` classifies a tool invocation before it runs. High-risk
invocations are not executed directly: a :class:`ConfirmationManager` raises a
pending request, the transport layer relays it to the UI (a ``require_confirmation``
WebSocket event), and the tool only proceeds once the user approves.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional

from config import config
from common.reporting import safe_error

logger = logging.getLogger("Zaytoon.ToolSafety")


class RiskLevel(str, Enum):
    SAFE = "safe"
    ELEVATED = "elevated"
    HIGH = "high"


# --------------------------------------------------------------------------
# High-risk command fragments. Matched case-insensitively against the whole
# command string, so `del`, `rm`, `format` etc. are caught mid-pipeline too.
# --------------------------------------------------------------------------
HIGH_RISK_COMMAND_PATTERNS: List[tuple[str, str]] = [
    # Windows: system lifecycle
    (r"\bshutdown\b", "system shutdown/restart"),
    (r"\brestart-computer\b", "system restart"),
    (r"\bstop-computer\b", "system shutdown"),
    (r"\bformat\s+[a-z]:", "disk format"),
    (r"\bdiskpart\b", "disk partitioning"),
    (r"\bbcdedit\b", "boot configuration change"),
    (r"\bvssadmin\b.*\bdelete\b", "shadow copy deletion"),
    (r"\bcipher\b\s+[/\-:]", "file encryption/wipe"),
    (r"\bfsutil\b", "filesystem utility"),
    (r"\btakeown\b", "ownership takeover"),
    (r"\bicacls\b", "permission modification"),
    (r"\battrib\b", "attribute modification"),
    (r"\breg\s+(delete|add|import)\b", "registry modification"),
    (r"\bregedit\b", "registry editor"),
    (r"\bnet\s+(user|localgroup|stop|share)\b", "network/account administration"),
    (r"\bsc\s+(config|delete|stop|start)\b", "service control"),
    (r"\btaskkill\b", "forced process termination"),
    (r"\bdel\s+/[sqf]", "recursive/forced delete"),
    (r"\b(del|rd|rmdir)\b[^\n|;&]*\s/[sq]\b", "recursive/forced delete"),
    (r"\brmdir\b\s+/s", "recursive directory delete"),
    (r"\berase\b\s+[a-z]:", "drive erase"),
    # POSIX
    (r"\brm\s+(-[a-z]*\s*)*-[a-z]*[rf]", "recursive force delete"),
    (r"\brm\s+-rf\b", "recursive force delete"),
    (r"\bmkfs(\.[a-z0-9]+)?\b", "filesystem creation"),
    (r"\bdd\s+if=", "raw disk write"),
    (r":\(\)\s*\{.*\}\s*;\s*:", "fork bomb"),
    (r"\bchmod\s+(-[a-zA-Z]+\s+)*(777|a\+rwx)\b", "world-writable permissions"),
    (r"\bchown\s+-R\s+root\b", "root ownership change"),
    (r"\bkill(all)?\s+-9\b", "forced kill"),
    (r"\bsudo\b", "privilege escalation"),
    (r"\bmount\b.*\b-o\b", "mount change"),
    (r"\buserdel\b|\bgroupdel\b", "user/group deletion"),
    # Shell / download-and-execute
    (r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba)?sh\b", "remote script execution"),
    (r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(python|perl|ruby)\b", "remote script execution"),
    (r"-enc(odedcommand)?\b", "encoded powershell command"),
    (r"\biex\b|\binvoke-expression\b", "dynamic code execution"),
    (r"\beval\s*\(", "dynamic code execution"),
    # Package / firewall churn
    (r"\bnpm\s+(install|uninstall)\s+-g\b", "global package install"),
    (r"\bpip\s+install\b", "package install"),
    (r"\bnetsh\s+advfirewall\b", "firewall change"),
]

# Destructive tokens inside generated source code (run_code).
HIGH_RISK_CODE_PATTERNS: List[tuple[str, str]] = [
    (r"\bshutil\.rmtree\b", "recursive directory deletion"),
    (r"\bos\.remove\s*\(", "file deletion"),
    (r"\bos\.unlink\s*\(", "file deletion"),
    (r"\bos\.system\s*\(", "shell execution"),
    (r"\bsubprocess\.(Popen|run|call)\b", "subprocess execution"),
    (r"\bfs\.(rm|rmSync|unlink|rmdir)\b", "node filesystem deletion"),
    (r"\bDestructiveDelete\b", "filesystem deletion"),
    (r"\bRemove-Item\b.*-Recurse", "recursive deletion"),
    (r"\bdel\b.*\b/s\b", "recursive deletion"),
    (r"\bexit\s*\(", "process termination"),
    (r"\bos\.kill\b", "process termination"),
]

# Apps whose termination can destabilise the OS.
HIGH_RISK_TARGETS = {
    "explorer", "explorer.exe", "dwm", "dwm.exe", "wininit", "wininit.exe",
    "csrss", "csrss.exe", "lsass", "lsass.exe", "services", "services.exe",
    "system", "system.exe", "taskmgr", "taskmgr.exe", "svchost", "svchost.exe",
    "antivirus", "defender", "msmpeng", "msmpeng.exe", "mcafee", "crowdstrike",
    "audiodg", "audiodg.exe", "registry", "msconfig",
}

# Files that must never be written/deleted via a tool call.
PROTECTED_PATH_PATTERNS = [
    r"[\\/]windows[\\/]system32\b",
    r"[\\/]windows[\\/]syswow64\b",
    r"[\\/]etc[\\/](passwd|shadow|sudoers|hosts)\b",
    r"\bbootmgr\b",
    r"[\\/]boot\b",
    r"\.ssh[\\/](id_rsa|id_ed25519|authorized_keys)\b",
    r"[\\/]system volume information\b",
]

_COMPILED = [(re.compile(p, re.IGNORECASE), label) for p, label in HIGH_RISK_COMMAND_PATTERNS]
_COMPILED_CODE = [(re.compile(p, re.IGNORECASE), label) for p, label in HIGH_RISK_CODE_PATTERNS]
_COMPILED_PROTECTED = [re.compile(p, re.IGNORECASE) for p in PROTECTED_PATH_PATTERNS]

# Actions that carry no side effects at all.
READ_ONLY_ACTIONS = {
    "get_time", "system_info", "search_web", "calculator", "file_read",
    "file_list", "get_volume", "screenshot", "list_reminders",
    # tools/processes.py: inspecting processes Zaytoon started changes nothing.
    "process_list", "process_logs",
}

# --------------------------------------------------------------------------
# Permission tiers
# --------------------------------------------------------------------------
# The product spec defines three levels. They are expressed here as a single
# mapping so the UI, the confirmation dialog and the risk gate all read the same
# classification instead of each re-deriving it from regexes.
#
#   SAFE      - runs without asking
#   CONFIRM   - always asks first, even when the arguments look harmless
#   DANGEROUS - asks, and is additionally subject to the pattern-based
#               HIGH_RISK_* rules and to the unconditional sandbox blocks
PERMISSION_SAFE = "safe"
PERMISSION_CONFIRM = "confirm"
PERMISSION_DANGEROUS = "dangerous"

#: Actions that mutate the workspace or the machine. These require explicit
#: human approval regardless of how innocuous the specific arguments look — the
#: spec puts "write files", "modify code", "install packages", "execute
#: arbitrary terminal commands" and "git operations that modify repository
#: state" in this tier.
#:
#: Before this existed, `file_write` was only escalated when the target path
#: matched a protected-path pattern, so writing an ordinary source file ran
#: with no prompt at all.
CONFIRMATION_REQUIRED_ACTIONS = frozenset({
    # filesystem mutation
    "file_write", "write_file", "file_edit", "file_append", "file_move",
    "file_copy", "create_folder", "make_dir", "mkdir",
    # project scaffolding / dependency installation
    "create_project", "scaffold", "run_npm", "npm_install", "pip_install",
    "install_package",
    # arbitrary terminal execution. The spec puts "execute arbitrary terminal
    # commands" in this tier, so `system_command` now always prompts.
    #
    # This is a deliberate behaviour change. Previously `system_command` was
    # content-inspected only, on the reasoning that `echo hi` is harmless while
    # `rm -rf /` is not. But *any* command reaches an interpreter, so the
    # harmless-looking case is exactly the one an attacker would use to reach a
    # dangerous one. The pattern rules still run and still add their reasons on
    # top; this only guarantees a prompt is always shown.
    "system_command", "cmd", "run_shell", "exec", "run_command",
    # the structured counterpart of `system_command` (tools/local_exec.py):
    # same sandbox, same gate, so it sits in the same tier. Its aliases are
    # listed too because `permission_for()` can be handed an alias directly.
    "execute_local_command", "run_local_command", "exec_local",
    # background processes (tools/processes.py): starting one runs an arbitrary
    # command, stopping one kills something the user can see running. Listing and
    # reading logs stay safe (see READ_ONLY_ACTIONS).
    "process_start", "process_stop",
    # build/test/lint wrappers, which are arbitrary execution by another name
    "run_npm_script", "run_tests", "run_build", "run_linter",
    # input injection can trigger arbitrary application behaviour
    "type_text", "key_press",
})

#: Git sub-commands that change repository state. The spec puts these in the
#: confirmation tier ("git operations that modify repository state"); read-only
#: git (``git_status``, ``git_log``, ``git_diff``) stays SAFE, which is why the
#: check is verb-based rather than a blanket ``git_*`` ban.
GIT_MUTATING_VERBS = frozenset({
    "commit", "add", "checkout", "merge", "rebase", "reset", "push", "pull",
    "stash", "apply", "branch_create", "branch_delete", "clean", "restore",
    "cherry_pick", "revert", "tag_create", "tag_delete", "remote_add",
    "remote_remove", "submodule_update", "am", "format_patch",
})

#: Read-only git verbs, listed for documentation and for the ``git_*`` fallback
#: in :meth:`RiskAssessor.permission_for`.
GIT_READONLY_VERBS = frozenset({
    "status", "log", "diff", "show", "branch_list", "remote_list", "blame",
    "describe", "rev_parse", "ls_files", "config_get", "shortlog", "reflog",
    "stash_list", "tag_list",
})

#: Destructive or privilege-adjacent actions: delete files, delete folders,
#: administrative/system commands, and arbitrary code execution. The spec keeps
#: these separate from CONFIRMATION_REQUIRED so the UI can render them more
#: prominently; the sandbox additionally refuses many of them outright.
DANGEROUS_ACTIONS = frozenset({
    "file_delete", "delete_file", "file_remove", "delete_folder", "rmdir",
    "registry_write", "reg_delete", "run_as_admin", "format_disk",
    "run_code",
    # structured code execution (tools/local_exec.py): identical policy to
    # `run_code`, and its aliases are listed because `permission_for()` can be
    # handed an alias directly.
    "execute_code", "exec_code", "run_snippet",
})

#: Actions whose parameters contain a single command string judged against
#: :data:`HIGH_RISK_COMMAND_PATTERNS`. Named once so the tier tables and the
#: ``assess`` branch cannot drift apart.
COMMAND_ACTIONS = frozenset({
    "system_command", "cmd", "run_shell", "exec",
    "execute_local_command", "run_local_command", "exec_local",
    # a background process is still an arbitrary command, so its command line
    # gets the same pattern inspection
    "process_start",
})

#: The structured-execution members of :data:`COMMAND_ACTIONS`. These escalate
#: to DANGEROUS when a pattern matches; ``system_command`` deliberately does
#: **not**, because its behaviour is pinned by
#: ``test_pattern_rules_still_layer_on_top`` — the tier of an action is a
#: property of what it does, and `rm -rf /` arriving through the ordinary
#: command tool must still be reported as the CONFIRM action it is. The
#: structured tool advertises its exit code and streams as machine-readable
#: output, i.e. it is the one built for unattended, repeated use, so a command
#: that matched a hazard there is labelled for what it is.
STRUCTURED_EXEC_ACTIONS = frozenset({
    "execute_local_command", "run_local_command", "exec_local",
    "process_start",
})

#: Actions whose parameters contain source code judged against both pattern
#: tables and the shell-language rule.
CODE_ACTIONS = frozenset({
    "run_code", "execute_code", "exec_code", "run_snippet",
})


@dataclass
class RiskAssessment:
    level: RiskLevel
    action: str
    reasons: List[str] = field(default_factory=list)
    summary: str = ""
    permission: str = PERMISSION_SAFE

    @property
    def requires_confirmation(self) -> bool:
        return self.level is RiskLevel.HIGH

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level.value,
            "action": self.action,
            "permission": self.permission,
            "reasons": list(self.reasons),
            "summary": self.summary,
        }

    def prompt_text(self) -> str:
        if not self.reasons:
            return self.summary
        return f"{self.summary} — {'; '.join(dict.fromkeys(self.reasons))}"


class RiskAssessor:
    """Stateless classifier for tool invocations."""

    def __init__(self, always_confirm: Optional[Any] = None):
        raw = always_confirm if always_confirm is not None else config.TOOL_ALWAYS_CONFIRM
        if isinstance(raw, str):
            items = raw.split(",")
        else:
            items = list(raw or [])
        self.always_confirm = {
            str(a).strip().lower() for a in items if str(a).strip()
        }

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _as_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return " ".join(str(v) for v in value) if isinstance(value, (list, tuple)) else str(value)

    def _is_protected_path(self, path: str) -> bool:
        if not path:
            return False
        try:
            normalized = os.path.abspath(os.path.expanduser(path))
        except (ValueError, OSError):
            normalized = path
        return any(p.search(normalized) for p in _COMPILED_PROTECTED)

    def _command_reasons(self, command: str) -> List[str]:
        return [label for pattern, label in _COMPILED if pattern.search(command or "")]

    def _code_reasons(self, code: str) -> List[str]:
        return [label for pattern, label in _COMPILED_CODE if pattern.search(code or "")]

    # -- public API ------------------------------------------------------
    @staticmethod
    def permission_for(action: str) -> str:
        """Classify an action into SAFE / CONFIRM / DANGEROUS.

        Deliberately independent of the regex rules: the tier is a property of
        *what the action does*, whereas :meth:`assess` additionally judges the
        *particular arguments*. A write is a write whether the path is
        suspicious or not.
        """
        name = (action or "").strip().lower()
        if name in DANGEROUS_ACTIONS:
            return PERMISSION_DANGEROUS
        if name in CONFIRMATION_REQUIRED_ACTIONS:
            return PERMISSION_CONFIRM
        if name.startswith("git_"):
            # Tier by verb, so a new read-only git tool is SAFE by default and a
            # new mutating one cannot silently become SAFE either.
            verb = name[4:]
            if verb in GIT_READONLY_VERBS:
                return PERMISSION_SAFE
            return PERMISSION_CONFIRM
        return PERMISSION_SAFE

    def _finalise(
        self,
        action: str,
        permission: str,
        reasons: List[str],
        summary: str,
    ) -> RiskAssessment:
        """Single exit point for :meth:`assess`, enforcing one invariant.

        **The gate is never looser than the tier.** ``requires_confirmation`` is
        what ``ToolExecutor``/``ToolAgent`` actually enforce, while
        ``permission`` is what the UI renders in the dialog. If the two can
        disagree, the user is shown a prompt that never appears — or worse, an
        action is labelled DANGEROUS and runs silently. A branch that
        classified no hazard but landed on a non-SAFE tier therefore gets a
        synthesised reason rather than being allowed through.

        The reverse — gate stricter than tier, e.g. an extra pattern match — is
        fine, and is the intended layering.
        """
        reasons = list(dict.fromkeys(reasons))
        if not reasons and permission != PERMISSION_SAFE:
            reasons.append(f"'{action}' is a {permission}-level action")
        level = RiskLevel.HIGH if reasons else RiskLevel.SAFE
        return RiskAssessment(level, action, reasons, summary, permission)

    def assess(self, action: str, params: Optional[Dict[str, Any]] = None) -> RiskAssessment:
        if not config.TOOL_SAFETY_ENABLED:
            return RiskAssessment(
                RiskLevel.SAFE, action, [], f"{action} (safety off)",
                self.permission_for(action),
            )

        params = params or {}
        action_l = (action or "").strip().lower()
        permission = self.permission_for(action_l)
        # Preserve insertion order while removing duplicate reasons (several
        # patterns often describe the same hazard).
        reasons: List[str] = []

        if action_l in self.always_confirm:
            reasons.append(f"'{action_l}' is on the always-confirm list")

        if action_l in READ_ONLY_ACTIONS:
            # Read-only by nature, unless the operator explicitly listed this
            # action in TOOL_ALWAYS_CONFIRM.
            return self._finalise(
                action, permission, reasons, f"Read-only action: {action}"
            )

        if action_l in COMMAND_ACTIONS:
            command = self._as_text(
                params.get("command") or params.get("cmd") or params.get("args")
            )
            if not command.strip():
                return self._finalise(action, permission, [], "Empty command")
            reasons.extend(self._command_reasons(command))
            summary = f"Run command: {command.strip()[:200]}"
            if action_l in STRUCTURED_EXEC_ACTIONS and reasons:
                # Pattern rules escalate *this* action to DANGEROUS. The gate
                # fires either way; the label the dialog shows must not claim
                # "confirm" for a command that matched a hazard.
                permission = PERMISSION_DANGEROUS

        elif action_l in CODE_ACTIONS:
            code = self._as_text(params.get("code"))
            language = self._as_text(params.get("language")).lower()
            if not code.strip():
                return self._finalise(action, permission, [], "Empty code")
            reasons.extend(self._command_reasons(code))
            reasons.extend(self._code_reasons(code))
            if language in ("bash", "sh", "shell", "powershell", "cmd", "batch"):
                reasons.append(f"shell code execution ({language})")
            summary = f"Execute {language or 'code'}: {code.strip()[:200]}"

        elif action_l in ("close_app", "kill_process", "taskkill"):
            app = self._as_text(params.get("app") or params.get("name")).strip().lower()
            if app in HIGH_RISK_TARGETS:
                reasons.append(f"terminating system-critical process '{app}'")
                # Escalate the tier along with the arguments. `close_app` is
                # SAFE in general (closing Notepad needs no prompt), but
                # killing explorer.exe or dwm.exe is a destructive system
                # action, and the spec puts those in DANGEROUS. Without this the
                # dialog would render "SAFE" and *then* ask for approval.
                if permission == PERMISSION_SAFE:
                    permission = PERMISSION_DANGEROUS
            summary = f"Close application: {app or 'unknown'}"

        elif action_l in ("open_app", "start_app"):
            app = self._as_text(params.get("app") or params.get("name")).strip()
            summary = f"Open application: {app or 'unknown'}"

        elif action_l in ("file_delete", "delete_file", "file_remove",
                          "delete_folder", "rmdir"):
            path = self._as_text(params.get("path"))
            reasons.append("destructive: deletes data")
            if self._is_protected_path(path):
                reasons.append(f"target is a protected system path: {path}")
            summary = f"Delete: {path or 'unknown'}"

        elif action_l in ("file_write", "write_file", "file_edit", "file_append",
                          "create_folder", "make_dir", "mkdir", "file_move",
                          "file_copy", "create_project", "scaffold"):
            path = self._as_text(params.get("path") or params.get("target")
                                or params.get("directory"))
            # The tier already requires approval; this states *why* in the
            # dialog so the user is not asked to approve a mystery.
            reasons.append("modifies files in the workspace")
            if self._is_protected_path(path):
                reasons.append(f"target is a protected system path: {path}")
            summary = f"Modify: {path or 'unknown'}"

        elif action_l in ("file_read", "file_list", "search_files"):
            path = self._as_text(params.get("path"))
            if self._is_protected_path(path):
                reasons.append(f"reads a protected system path: {path}")
            summary = f"Read path: {path or 'unknown'}"

        elif action_l in ("type_text", "key_press"):
            # Keystrokes are injected into whatever window has focus and can
            # trigger arbitrary application behaviour (a saved macro, a
            # shortcut, a destructive button), so this is now blocking rather
            # than merely "elevated".
            reasons.append("injects input into the focused application")
            payload = self._as_text(params.get("text") or params.get("keys"))
            summary = f"{action} ({payload[:120]})"

        elif action_l.startswith("git_"):
            verb = action_l[4:]
            if verb not in GIT_READONLY_VERBS:
                # Everything not explicitly read-only blocks, including verbs
                # added after this table was written (`git_reset_hard`).
                reasons.append("modifies git repository state")
            summary = f"git {verb}: {self._as_text(params.get('args'))[:160]}"

        elif action_l in ("run_npm", "npm_install", "pip_install", "install_package"):
            reasons.append("installs packages")
            summary = f"Install: {self._as_text(params.get('package') or params.get('args'))[:160]}"

        else:
            summary = f"{action}"

        return self._finalise(action, permission, reasons, summary)


# Backwards-friendly module-level helper.
_default_assessor = RiskAssessor()


def assess_tool(action: str, params: Optional[Dict[str, Any]] = None) -> RiskAssessment:
    return _default_assessor.assess(action, params)


def is_high_risk(action: str, params: Optional[Dict[str, Any]] = None) -> bool:
    return assess_tool(action, params).requires_confirmation


# --------------------------------------------------------------------------
# Human-in-the-loop confirmation
# --------------------------------------------------------------------------
@dataclass
class PendingConfirmation:
    request_id: str
    action: str
    params: Dict[str, Any]
    assessment: RiskAssessment
    session_id: str = ""
    created_at: float = field(default_factory=time.time)

    def to_event(self) -> Dict[str, Any]:
        return {
            "type": "require_confirmation",
            "request_id": self.request_id,
            "action": self.action,
            "params": self.params,
            "level": self.assessment.level.value,
            "permission": self.assessment.permission,
            "reasons": self.assessment.reasons,
            "summary": self.assessment.summary,
            "text": self.assessment.prompt_text(),
            "session_id": self.session_id,
        }


class ConfirmationManager:
    """Tracks pending approvals and blocks the tool until one arrives."""

    # Denial outcomes, so callers can tell the user *why* nothing ran.
    OUTCOME_APPROVED = "approved"
    OUTCOME_DECLINED = "declined"
    OUTCOME_TIMEOUT = "timeout"
    OUTCOME_UNAVAILABLE = "unavailable"
    OUTCOME_ERROR = "error"

    def __init__(
        self,
        notify: Optional[Callable[[PendingConfirmation], Awaitable[None]]] = None,
        timeout: Optional[float] = None,
        assessor: Optional[RiskAssessor] = None,
    ):
        self._notify = notify
        self.timeout = timeout if timeout is not None else config.TOOL_CONFIRM_TIMEOUT
        self.assessor = assessor or RiskAssessor()
        self._pending: Dict[str, asyncio.Future] = {}
        self._last_outcome: Optional[str] = None

    # -- registration ----------------------------------------------------
    def set_notifier(self, notify: Callable[[PendingConfirmation], Awaitable[None]]):
        self._notify = notify

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def pending_ids(self) -> List[str]:
        return list(self._pending.keys())

    @property
    def last_outcome(self) -> Optional[str]:
        """Why the most recent :meth:`request` returned True/False."""
        return self._last_outcome

    # -- request/resolve -------------------------------------------------
    async def request(
        self,
        action: str,
        params: Optional[Dict[str, Any]] = None,
        session_id: str = "",
    ) -> bool:
        """Ask the user to approve ``action``. Returns True only if approved."""
        assessment = self.assessor.assess(action, params)
        if not assessment.requires_confirmation:
            self._last_outcome = self.OUTCOME_APPROVED
            return True

        if not config.TOOL_SAFETY_ENABLED:
            self._last_outcome = self.OUTCOME_APPROVED
            return True

        if self._notify is None:
            self._last_outcome = self.OUTCOME_UNAVAILABLE
            logger.warning(
                "High-risk %s blocked: no confirmation channel available", action
            )
            return False

        pending = PendingConfirmation(
            request_id=f"cfm_{secrets.token_hex(6)}",
            action=action,
            params=dict(params or {}),
            assessment=assessment,
            session_id=session_id,
        )
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[pending.request_id] = future

        try:
            await self._notify(pending)
        except Exception as e:
            self._pending.pop(pending.request_id, None)
            self._last_outcome = self.OUTCOME_ERROR
            # The failed frame carries the *parameters* being confirmed, so
            # this is reported through the redacting logger, never `str(e)`.
            safe_error(
                e, operation="confirmation.notify", component="ConfirmationManager",
                detail=f"action={action}",
            )
            return False

        try:
            approved = await asyncio.wait_for(future, timeout=self.timeout)
        except asyncio.TimeoutError:
            self._last_outcome = self.OUTCOME_TIMEOUT
            logger.warning("Confirmation for %s timed out; denying", action)
            return False
        except asyncio.CancelledError:
            self._last_outcome = self.OUTCOME_DECLINED
            logger.info("Confirmation for %s cancelled; denying", action)
            return False
        finally:
            self._pending.pop(pending.request_id, None)

        self._last_outcome = (
            self.OUTCOME_APPROVED if approved else self.OUTCOME_DECLINED
        )
        logger.info("Confirmation for %s: %s", action, self._last_outcome)
        return bool(approved)

    def resolve(self, request_id: str, approved: bool) -> bool:
        """Complete a pending request. Returns False if the id is unknown."""
        future = self._pending.get(request_id)
        if future is None or future.done():
            return False
        future.set_result(bool(approved))
        return True

    def cancel_all(self, reason: str = "session closed"):
        count = len(self._pending)
        for request_id in list(self._pending.keys()):
            future = self._pending.pop(request_id)
            if not future.done():
                future.set_result(False)
        logger.info("Cancelled %d pending confirmation(s): %s", count, reason)
