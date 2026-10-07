"""Filesystem tools.

All paths are resolved through :func:`tools.sandbox.resolve_workspace`, which
both validates containment *and* returns the concrete path to operate on. There
is deliberately no second way to turn user input into a path: an earlier
version checked containment with ``is_safe_path`` and then opened
``os.path.abspath(path)``, which resolved relative paths against the process
cwd rather than the workspace — so a path could be validated against one root
and opened against another.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from typing import Any, Dict, Iterable, List, Optional

from tools.base import BaseTool, ToolResult, ToolValidationError, _prop
from tools.sandbox import SecurityError, resolve_workspace, workspace_root

logger = logging.getLogger("Zaytoon.Tools.Filesystem")

#: Hard cap on a single read, so a multi-gigabyte log cannot exhaust memory or
#: blow up the LLM context window.
MAX_READ_BYTES = 200_000

#: Cap on lines returned by a search, for the same reason.
MAX_SEARCH_RESULTS = 200


def _require_path(params: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ToolValidationError(
        f"one of {', '.join(keys)} is required"
    )


def _resolve(params: Dict[str, Any], *keys: str) -> str:
    """Resolve a caller-supplied path inside the workspace, or raise.

    ``keys`` names the accepted parameter spellings; the first non-empty one
    wins. Pass at least one.

    Raises :class:`SecurityError` (not ``ToolValidationError``) for an escape
    attempt so the registry reports it as ``blocked`` — the request is refused
    by policy, and no approval would change that.
    """
    if not keys:
        raise AssertionError("_resolve() requires at least one parameter key")
    raw = _require_path(params, *keys)
    resolved = resolve_workspace(raw)
    if not resolved:
        raise SecurityError(
            f"Access denied: '{raw}' is outside the workspace ({workspace_root()})"
        )
    return resolved


async def _in_executor(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    if kwargs:
        from functools import partial

        return await loop.run_in_executor(None, partial(fn, *args, **kwargs))
    return await loop.run_in_executor(None, fn, *args)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------
class FileRead(BaseTool):
    permission = "safe"

    def name(self) -> str:
        return "file_read"

    def aliases(self) -> List[str]:
        return ["read_file", "file_list", "list_files", "list_directory"]

    def description(self) -> str:
        return (
            "Read a text file, or list a directory, inside the workspace. "
            "Relative paths are resolved against the workspace root."
        )

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": _prop(
                    "string",
                    "File or directory to read, relative to the workspace root.",
                ),
            },
            "required": ["path"],
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        _require_path(params, "path")

    async def execute(self, path: str = "", **_: Any) -> ToolResult:
        target = _resolve({"path": path}, "path")
        if not await _in_executor(os.path.exists, target):
            return ToolResult.fail(f"Not found: {path}")

        if await _in_executor(os.path.isdir, target):
            return await self._list_dir(target, path)

        try:
            raw = await _in_executor(_read_capped, target)
        except OSError as e:
            return ToolResult.fail(f"Could not read {path}: {e}")
        return ToolResult.ok(raw, {"path": path, "bytes": len(raw)})

    @staticmethod
    async def _list_dir(target: str, display: str) -> ToolResult:
        entries = await _in_executor(os.listdir, target)
        lines = []
        for name in sorted(entries, key=str.lower):
            full = os.path.join(target, name)
            is_dir = await _in_executor(os.path.isdir, full)
            lines.append(f"{'[DIR] ' if is_dir else '      '}{name}")
        listing = "\n".join(lines) if lines else "(empty directory)"
        return ToolResult.ok(
            f"{display}/ ({len(entries)} entries)\n{listing}",
            {"path": display, "entries": entries, "count": len(entries)},
        )


def _read_capped(path: str) -> str:
    """Read at most :data:`MAX_READ_BYTES`, reporting when truncated."""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        data = handle.read(MAX_READ_BYTES + 1)
    if len(data) > MAX_READ_BYTES:
        return data[:MAX_READ_BYTES] + f"\n... [truncated at {MAX_READ_BYTES} bytes]"
    return data


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------
class FileWrite(BaseTool):
    permission = "confirm"
    mutates_files = True

    def name(self) -> str:
        return "file_write"

    def aliases(self) -> List[str]:
        return ["write_file", "file_edit", "create_file"]

    def description(self) -> str:
        return (
            "Create or overwrite a text file inside the workspace, creating "
            "parent directories as needed. Requires confirmation."
        )

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": _prop("string", "File to write, relative to the workspace root."),
                "content": _prop("string", "Full contents to write."),
                "append": _prop(
                    "boolean",
                    "Append instead of overwriting.",
                    default=False,
                ),
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        _require_path(params, "path")
        content = params.get("content")
        if content is None:
            raise ToolValidationError("'content' is required")
        if not isinstance(content, str):
            raise ToolValidationError(
                f"'content' must be a string, got {type(content).__name__}"
            )

    async def execute(
        self, path: str = "", content: str = "", append: bool = False, **_: Any
    ) -> ToolResult:
        target = _resolve({"path": path}, "path")
        try:
            await _in_executor(_write_file, target, content, bool(append))
        except OSError as e:
            return ToolResult.fail(f"Could not write {path}: {e}")
        verb = "Appended to" if append else "Wrote"
        size = len(content)
        return ToolResult.ok(f"{verb} {path} ({size} chars)", {"path": path, "chars": size})


def _write_file(target: str, content: str, append: bool) -> None:
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(target, "a" if append else "w", encoding="utf-8") as handle:
        handle.write(content)


class CreateFolder(BaseTool):
    permission = "confirm"
    mutates_files = True

    def name(self) -> str:
        return "create_folder"

    def aliases(self) -> List[str]:
        return ["make_dir", "mkdir", "create_directory"]

    def description(self) -> str:
        return "Create a directory inside the workspace, including parents."

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": _prop("string", "Directory to create."),
                "parents": _prop(
                    "boolean", "Create missing parents.", default=True
                ),
            },
            "required": ["path"],
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        _require_path(params, "path", "directory", "name")

    async def execute(
        self, path: str = "", parents: bool = True, **_: Any
    ) -> ToolResult:
        target = _resolve({"path": path}, "path")
        if await _in_executor(os.path.isdir, target):
            return ToolResult.ok(f"{path} already exists", {"path": path, "created": False})
        try:
            if parents:
                # `os.makedirs` has no `parents` argument — it always creates
                # intermediates. The flag is honoured by creating each level
                # explicitly instead.
                await _in_executor(os.makedirs, target, exist_ok=False)
            else:
                await _in_executor(_mkdir_one_level, target)
        except FileExistsError:
            return ToolResult.ok(f"{path} already exists", {"path": path, "created": False})
        except OSError as e:
            return ToolResult.fail(f"Could not create {path}: {e}")
        return ToolResult.ok(f"Created {path}", {"path": path, "created": True})


def _mkdir_one_level(target: str) -> None:
    """Create exactly one directory, refusing to create missing parents."""
    os.mkdir(target)


class FileDelete(BaseTool):
    permission = "dangerous"
    mutates_files = True

    def name(self) -> str:
        return "file_delete"

    def aliases(self) -> List[str]:
        return ["delete_file", "file_remove", "delete_folder", "rmdir"]

    def description(self) -> str:
        return (
            "Delete a file or directory inside the workspace. Destructive and "
            "irreversible; requires confirmation."
        )

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": _prop("string", "File or directory to delete."),
                "recursive": _prop(
                    "boolean",
                    "Required to delete a non-empty directory.",
                    default=False,
                ),
            },
            "required": ["path"],
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        _require_path(params, "path")

    def risk_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        # The command-pattern scanner would read the path as a command; keep the
        # assessment focused on the fact that this deletes.
        return {"path": params.get("path", "")}

    async def execute(self, path: str = "", recursive: bool = False, **_: Any) -> ToolResult:
        target = _resolve({"path": path}, "path")
        if not await _in_executor(os.path.exists, target):
            return ToolResult.fail(f"Not found: {path}")

        is_dir = await _in_executor(os.path.isdir, target)
        if is_dir:
            entries = await _in_executor(os.listdir, target)
            if entries and not recursive:
                return ToolResult.fail(
                    f"{path} is not empty ({len(entries)} entries). "
                    "Pass recursive=true to delete it and its contents."
                )
            try:
                await _in_executor(
                    shutil.rmtree if recursive else os.rmdir, target
                )
            except OSError as e:
                return ToolResult.fail(f"Could not delete {path}: {e}")
            return ToolResult.ok(
                f"Deleted directory {path}", {"path": path, "entries": len(entries)}
            )

        try:
            await _in_executor(os.unlink, target)
        except OSError as e:
            return ToolResult.fail(f"Could not delete {path}: {e}")
        return ToolResult.ok(f"Deleted {path}", {"path": path})


class FileMove(BaseTool):
    permission = "confirm"
    mutates_files = True

    def name(self) -> str:
        return "file_move"

    def aliases(self) -> List[str]:
        return ["move_file", "file_rename", "file_copy"]

    def description(self) -> str:
        return "Move or copy a file or directory within the workspace."

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "source": _prop("string", "Existing path."),
                "destination": _prop("string", "New path."),
                "copy": _prop("boolean", "Copy instead of move.", default=False),
            },
            "required": ["source", "destination"],
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        _require_path(params, "source", "from", "path")
        _require_path(params, "destination", "to", "target")

    async def execute(
        self, source: str = "", destination: str = "", copy: bool = False, **_: Any
    ) -> ToolResult:
        src = _resolve({"source": source}, "source")
        dst = _resolve({"destination": destination}, "destination")
        if not await _in_executor(os.path.exists, src):
            return ToolResult.fail(f"Not found: {source}")
        if await _in_executor(os.path.isdir, dst):
            return ToolResult.fail(f"{destination} is a directory")
        try:
            parent = os.path.dirname(dst)
            if parent:
                await _in_executor(os.makedirs, parent, exist_ok=True)
            if copy:
                await _in_executor(shutil.copy2, src, dst)
            else:
                await _in_executor(shutil.move, src, dst)
        except (OSError, shutil.Error) as e:
            return ToolResult.fail(f"Could not transfer: {e}")
        verb = "Copied" if copy else "Moved"
        return ToolResult.ok(f"{verb} {source} -> {destination}")


class SearchFiles(BaseTool):
    permission = "safe"

    def name(self) -> str:
        return "search_files"

    def aliases(self) -> List[str]:
        return ["find_files", "grep", "search_code"]

    def description(self) -> str:
        return (
            "Search file contents inside the workspace for a text or regular "
            "expression, or find files by name pattern."
        )

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": _prop("string", "Text or regex to look for."),
                "path": _prop("string", "Directory to search.", default="."),
                "glob": _prop("string", "Only search files matching this glob."),
                "regex": _prop("boolean", "Treat query as a regex.", default=False),
                "max_results": _prop("integer", "Cap on matches.", default=50),
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        _require_path(params, "query", "pattern")
        raw = params.get("regex")
        if raw is not None and not isinstance(raw, bool):
            raise ToolValidationError("'regex' must be a boolean")

    def risk_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"path": params.get("path", ".")}

    async def execute(
        self,
        query: str = "",
        path: str = ".",
        glob: str = "**/*",
        regex: bool = False,
        max_results: int = 50,
        **_: Any,
    ) -> ToolResult:
        root = _resolve({"path": path or "."}, "path")
        if not await _in_executor(os.path.isdir, root):
            return ToolResult.fail(f"Not a directory: {path}")

        limit = min(int(max_results or 50), MAX_SEARCH_RESULTS)
        pattern = None
        if regex:
            try:
                pattern = re.compile(query, re.IGNORECASE)
            except re.error as e:
                return ToolResult.fail(f"Invalid regular expression: {e}")

        matches: List[Dict[str, Any]] = []
        truncated = False
        for filepath, line_no, line in _walk_and_match(root, query, glob, pattern):
            if len(matches) >= limit:
                truncated = True
                break
            matches.append({"file": filepath, "line": line_no, "text": line.strip()[:200]})

        if not matches:
            return ToolResult.ok(
                f"No matches for {query!r} in {path}",
                {"matches": [], "count": 0},
            )

        lines = [
            f"{m['file']}:{m['line']}: {m['text']}" for m in matches
        ]
        suffix = f"\n... [capped at {limit} matches]" if truncated else ""
        return ToolResult.ok(
            f"{len(matches)} match(es) for {query!r}:\n" + "\n".join(lines) + suffix,
            {"matches": matches, "count": len(matches), "truncated": truncated},
        )


_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".mypy_cache", ".pytest_cache", ".next", "target", ".idea", ".vscode",
}


def _walk_and_match(
    root: str, query: str, glob: str, pattern: Optional[re.Pattern]
) -> Iterable[tuple]:
    """Yield ``(relative_path, line_number, line)`` for each match."""
    needle = None if pattern else query.lower()
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune in place so os.walk does not descend into them.
        dirnames[:] = sorted(
            d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")
        )
        for filename in sorted(filenames):
            if glob and glob not in ("**/*", "*") and not _glob_match(filename, glob):
                continue
            full = os.path.join(dirpath, filename)
            try:
                if os.path.getsize(full) > MAX_READ_BYTES:
                    continue
                with open(full, "r", encoding="utf-8", errors="replace") as handle:
                    for line_no, line in enumerate(handle, 1):
                        haystack = line if pattern else line.lower()
                        if (pattern.search(line) if pattern else needle in haystack):
                            yield os.path.relpath(full, root), line_no, line
            except OSError:
                continue


def _glob_match(filename: str, glob: str) -> bool:
    import fnmatch

    return fnmatch.fnmatch(filename, glob) or fnmatch.fnmatch(filename, f"*{glob}*")


def build_tools() -> List[BaseTool]:
    return [FileRead(), FileWrite(), CreateFolder(), FileDelete(), FileMove(), SearchFiles()]
