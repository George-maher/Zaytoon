"""Git tools.

Every invocation is a fixed subcommand with an argument vector and no shell, so
there is no command string for a model to get wrong. Arguments the model
supplies are checked against a conservative character allow-list before being
placed in argv — not because git itself needs it, but because the tool's output
goes back into a model context and a newline in a ref name is a classic
argument-injection vector.

Read-only verbs are ``safe``. Anything that changes repository state is
``confirm``, matching the product's permission model.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from typing import Any, Dict, List, Optional, Tuple

from tools.base import BaseTool, ToolResult, ToolValidationError, _prop
from tools.sandbox import resolve_workspace

logger = logging.getLogger("Zaytoon.Tools.Git")

MAX_OUTPUT_CHARS = 20_000
TIMEOUT_S = 30

#: Conservative allow-list for a value that becomes a single argv entry.
#: Excludes quotes, backticks, semicolons, pipes, newlines and everything else
#: that could be meaningful to a shell or to git's own option parsing.
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_./\\@:+=-]{1,200}$")

#: Ref names get a tighter rule: no leading dash (which git would read as an
#: option), and no dots-only or double-dot sequences (git rejects them anyway,
#: but failing early gives a better message).
_SAFE_REF = re.compile(r"^(?!-)[A-Za-z0-9_./-]{1,200}$")


def _check_value(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolValidationError(f"'{field}' is required")
    text = value.strip()
    if not _SAFE_VALUE.match(text):
        raise ToolValidationError(
            f"'{field}' contains characters that are not allowed: {value!r}"
        )
    return text


def _check_ref(value: str, field: str = "ref") -> str:
    text = _check_value(value, field)
    if not _SAFE_REF.match(text):
        raise ToolValidationError(f"'{field}' is not a valid git reference: {value!r}")
    return text


class _GitTool(BaseTool):
    """Shared plumbing: locate git, resolve the repo, run a fixed subcommand."""

    permission = "safe"

    async def _run(
        self, args: List[str], cwd: str = "."
    ) -> Tuple[bool, str, str]:
        git = shutil.which("git")
        if git is None:
            raise ToolValidationError(
                "git is not installed or not on PATH."
            )
        root = resolve_workspace(cwd or ".")
        if not root:
            raise ToolValidationError(
                f"'{cwd}' is outside the workspace."
            )
        try:
            proc = await asyncio.create_subprocess_exec(
                git, *args,  # noqa: S603 - fixed argv, git resolved via which()
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=root,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_S)
        except asyncio.TimeoutError:
            return False, "", f"git {args[0]} timed out after {TIMEOUT_S}s"
        except OSError as e:
            return False, "", f"Could not run git: {e}"
        return (
            proc.returncode == 0,
            out.decode("utf-8", errors="replace")[:MAX_OUTPUT_CHARS],
            err.decode("utf-8", errors="replace")[:2000],
        )


class GitStatus(_GitTool):
    permission = "safe"

    def name(self) -> str:
        return "git_status"

    def description(self) -> str:
        return "Show the working tree status of a git repository."

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": _prop("string", "Repository path.", default=".")
            },
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        if params.get("path") is not None and not isinstance(params["path"], str):
            raise ToolValidationError("'path' must be a string")

    def risk_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"path": params.get("path", ".")}

    async def execute(self, path: str = ".", **_: Any) -> ToolResult:
        ok, out, err = await self._run(["status", "--short", "--branch"], path)
        if not ok:
            return ToolResult.fail(err.strip() or "git status failed")
        return ToolResult.ok(out.strip() or "(clean working tree)", {"output": out})


class GitLog(_GitTool):
    permission = "safe"

    def name(self) -> str:
        return "git_log"

    def description(self) -> str:
        return "Show recent commits."

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "limit": _prop("integer", "How many commits.", default=10, minimum=1, maximum=100),
                "path": _prop("string", "Repository path.", default="."),
            },
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        limit = params.get("limit", 10)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ToolValidationError("'limit' must be an integer between 1 and 100")

    def risk_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"path": params.get("path", ".")}

    async def execute(self, limit: int = 10, path: str = ".", **_: Any) -> ToolResult:
        ok, out, err = await self._run(
            ["log", f"-{max(1, min(int(limit), 100))}",
             "--pretty=format:%h %ad %an: %s", "--date=short"],
            path,
        )
        if not ok:
            return ToolResult.fail(err.strip() or "git log failed")
        return ToolResult.ok(out.strip() or "(no commits)", {"output": out})


class GitDiff(_GitTool):
    permission = "safe"

    def name(self) -> str:
        return "git_diff"

    def description(self) -> str:
        return "Show uncommitted changes, or the changes introduced by a ref."

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "ref": _prop("string", "Compare against this ref."),
                "path": _prop("string", "Repository path.", default="."),
            },
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        if params.get("ref") is not None:
            _check_ref(params["ref"], "ref")

    def risk_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"path": params.get("path", ".")}

    async def execute(self, ref: str = "", path: str = ".", **_: Any) -> ToolResult:
        args = ["diff"]
        if ref:
            args.append(_check_ref(ref, "ref"))
        ok, out, err = await self._run(args, path)
        if not ok:
            return ToolResult.fail(err.strip() or "git diff failed")
        return ToolResult.ok(out.strip() or "(no changes)", {"output": out})


class GitAdd(_GitTool):
    permission = "confirm"

    def name(self) -> str:
        return "git_add"

    def description(self) -> str:
        return "Stage files for commit. Requires confirmation."

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Paths to stage. Omit for 'all'.",
                },
                "all": _prop("boolean", "Stage everything.", default=False),
                "path": _prop("string", "Repository path.", default="."),
            },
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        files = params.get("files")
        if files is None and not params.get("all"):
            raise ToolValidationError("provide 'files' or set 'all' to true")
        if files is not None:
            if not isinstance(files, list) or not files:
                raise ToolValidationError("'files' must be a non-empty array")
            for item in files:
                _check_value(item, "files[]")

    def risk_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"path": params.get("path", ".")}

    async def execute(
        self, files: Optional[List[str]] = None, all: bool = False, path: str = ".", **_: Any
    ) -> ToolResult:
        args = ["add"] + (["-A"] if all else [_check_value(f, "files[]") for f in files or []])
        ok, out, err = await self._run(args, path)
        if not ok:
            return ToolResult.fail(err.strip() or "git add failed")
        return ToolResult.ok(out.strip() or f"Staged {len(args) - 1} path(s).")


class GitCommit(_GitTool):
    permission = "confirm"

    def name(self) -> str:
        return "git_commit"

    def description(self) -> str:
        return "Commit staged changes with a message. Requires confirmation."

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message": _prop("string", "Commit message."),
                "path": _prop("string", "Repository path.", default="."),
            },
            "required": ["message"],
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        message = params.get("message")
        if not isinstance(message, str) or not message.strip():
            raise ToolValidationError("'message' is required")
        if len(message) > 2000:
            raise ToolValidationError("'message' is too long (max 2000 characters)")

    def risk_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"path": params.get("path", ".")}

    async def execute(self, message: str = "", path: str = ".", **_: Any) -> ToolResult:
        ok, out, err = await self._run(["commit", "-m", message.strip()], path)
        if not ok:
            return ToolResult.fail(err.strip() or "git commit failed")
        return ToolResult.ok(out.strip() or "Committed.")


class GitBranch(_GitTool):
    permission = "safe"

    def name(self) -> str:
        return "git_branch_list"

    def description(self) -> str:
        return "List local and remote branches."

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": _prop("string", "Repository path.", default=".")
            },
            "additionalProperties": False,
        }

    def risk_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"path": params.get("path", ".")}

    async def execute(self, path: str = ".", **_: Any) -> ToolResult:
        ok, out, err = await self._run(["branch", "--all"], path)
        if not ok:
            return ToolResult.fail(err.strip() or "git branch failed")
        return ToolResult.ok(out.strip() or "(no branches)", {"output": out})


class GitCheckout(_GitTool):
    permission = "confirm"

    def name(self) -> str:
        return "git_checkout"

    def description(self) -> str:
        return "Switch to a branch, or restore a file. Requires confirmation."

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "ref": _prop("string", "Branch to switch to."),
                "create": _prop("boolean", "Create the branch.", default=False),
                "path": _prop("string", "Repository path.", default="."),
            },
            "required": ["ref"],
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        _check_ref(params.get("ref"), "ref")

    def risk_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"path": params.get("path", ".")}

    async def execute(
        self, ref: str = "", create: bool = False, path: str = ".", **_: Any
    ) -> ToolResult:
        args = ["checkout"]
        if create:
            args.append("-b")
        args.append(_check_ref(ref, "ref"))
        ok, out, err = await self._run(args, path)
        if not ok:
            return ToolResult.fail(err.strip() or "git checkout failed")
        return ToolResult.ok(out.strip() or f"Switched to {ref}.")


class GitPush(_GitTool):
    permission = "confirm"

    def name(self) -> str:
        return "git_push"

    def description(self) -> str:
        return "Push commits to a remote. Requires confirmation."

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "remote": _prop("string", "Remote name.", default="origin"),
                "branch": _prop("string", "Branch to push."),
                "path": _prop("string", "Repository path.", default="."),
            },
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        if params.get("remote") is not None:
            _check_ref(params["remote"], "remote")
        if params.get("branch") is not None:
            _check_ref(params["branch"], "branch")

    def risk_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"path": params.get("path", ".")}

    async def execute(
        self, remote: str = "origin", branch: str = "", path: str = ".", **_: Any
    ) -> ToolResult:
        args = ["push", _check_ref(remote, "remote") if remote else "origin"]
        if branch:
            args.append(_check_ref(branch, "branch"))
        ok, out, err = await self._run(args, path)
        if not ok:
            return ToolResult.fail(err.strip() or "git push failed")
        return ToolResult.ok(out.strip() or "Pushed.")


def build_tools() -> List[BaseTool]:
    return [
        GitStatus(), GitLog(), GitDiff(), GitBranch(),
        GitAdd(), GitCommit(), GitCheckout(), GitPush(),
    ]
