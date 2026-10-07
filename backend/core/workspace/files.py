"""The file service: one definition of what a path means, for every caller.

The REST routes and the agent tools both come through here, and that is the
entire reason this is a *service* rather than a set of tool classes. An editor
and the agent must not be able to disagree about whether ``src/app.py`` exists,
what it contains, or whether ``../etc`` is inside the workspace — because the
moment they can, one of them is a containment hole.

Containment is delegated to :func:`tools.sandbox.resolve_workspace`, which is
already the single definition used by every file tool. There is deliberately no
second resolution path here; ``AGENTS.md`` records that two of them already
existed once and disagreed about the process cwd.

What this adds over the tools:

* **A tree**, for the explorer. Shallow per call and lazy, because walking a
  monorepo on every keystroke is how a file explorer becomes the slowest thing
  in the app.
* **Metadata** (size, modified, language hint), because a UI needs it and the
  agent does not.
* **Structured search** returning objects rather than rendered text, so the UI
  can render results and an agent can read them.
* **An edit operation** with a diff summary, so "change review" (§30) has
  something real to show.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from tools.sandbox import SecurityError, resolve_workspace, workspace_root

logger = logging.getLogger("Zaytoon.Core.Workspace.Files")

#: Directories never listed. Same list the search walker uses, kept in one place
#: so a tree and a search cannot disagree about what exists.
SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    "target", ".next", ".mypy_cache", ".pytest_cache", ".idea", ".vscode",
    ".tox", "vendor", "coverage", ".turbo",
})

#: Largest file the editor will open. A 400 MB log is not a file to edit, and
#: reading it would exhaust the request's memory.
MAX_EDIT_BYTES = 2 * 1024 * 1024

#: Largest file a preview will render.
MAX_PREVIEW_BYTES = 512 * 1024

#: Cap on tree entries per call.
MAX_TREE_ENTRIES = 2000

#: Cap on search results.
MAX_SEARCH_RESULTS = 300

#: Extensions treated as text by the preview. Anything else is reported as
#: binary rather than being decoded into mojibake.
TEXT_SUFFIXES = frozenset({
    ".txt", ".md", ".markdown", ".rst", ".json", ".jsonc", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".env", ".gitignore", ".editorconfig",
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue",
    ".svelte", ".css", ".scss", ".sass", ".less", ".html", ".htm", ".xml",
    ".svg", ".sql", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd", ".lua",
    ".rb", ".go", ".rs", ".java", ".kt", ".kts", ".c", ".h", ".cpp", ".hpp",
    ".cs", ".php", ".swift", ".dart", ".r", ".jl", ".ex", ".exs", ".clj",
    ".gradle", ".dockerfile", ".makefile", ".log", ".csv", ".tsv",
})

#: Extension → language id for the editor's syntax highlighting.
LANGUAGE_BY_SUFFIX: Dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".vue": "html", ".svelte": "html",
    ".json": "json", ".jsonc": "json",
    ".yaml": "yaml", ".yml": "yaml", ".toml": "ini", ".ini": "ini",
    ".md": "markdown", ".markdown": "markdown",
    ".css": "css", ".scss": "scss", ".less": "css",
    ".html": "html", ".htm": "html", ".xml": "xml", ".svg": "xml",
    ".sql": "sql", ".sh": "shell", ".bash": "shell", ".ps1": "powershell",
    ".rb": "ruby", ".go": "go", ".rs": "rust", ".java": "java",
    ".kt": "kotlin", ".cs": "csharp", ".php": "php", ".swift": "swift",
    ".dart": "dart", ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp",
}


class FileAccessDenied(PermissionError):
    """A path is outside the workspace. No approval would change that."""


class FileNotFound(FileNotFoundError):
    """The path does not exist."""


def _resolve(path: str, root: Optional[str] = None) -> str:
    """Resolve inside the workspace or raise. The one gate."""
    resolved = resolve_workspace(path, root)
    if not resolved:
        raise FileAccessDenied(
            f"'{path}' is outside the workspace ({root or workspace_root()})"
        )
    return resolved


def language_for(path: str) -> str:
    """Language id for the editor. ``"plaintext"`` when unknown."""
    name = os.path.basename(path).lower()
    if name in ("dockerfile", "makefile"):
        return "dockerfile" if name == "dockerfile" else "shell"
    suffix = os.path.splitext(name)[1]
    return LANGUAGE_BY_SUFFIX.get(suffix, "plaintext")


def is_text_file(path: str) -> bool:
    name = os.path.basename(path).lower()
    if name in ("dockerfile", "makefile", ".gitignore", ".env"):
        return True
    return os.path.splitext(name)[1] in TEXT_SUFFIXES


async def _run(fn, *args, **kwargs):
    """Off-loop blocking work. Every filesystem call goes through here."""
    loop = asyncio.get_running_loop()
    if kwargs:
        from functools import partial

        return await loop.run_in_executor(None, partial(fn, *args, **kwargs))
    return await loop.run_in_executor(None, fn, *args)


# ---------------------------------------------------------------------------
# Tree / listing
# ---------------------------------------------------------------------------
def list_dir(path: str = ".", root: Optional[str] = None, show_hidden: bool = False) -> Dict[str, Any]:
    """One directory level. Synchronous body; awaited through :func:`tree`."""
    base = _resolve(path, root)
    if not os.path.exists(base):
        raise FileNotFound(f"Not found: {path}")
    if not os.path.isdir(base):
        # A distinct exception from "not found": the caller passed a path that
        # exists and is the wrong shape, and the two want different corrections.
        raise NotADirectoryError(f"{path} is a file, not a directory")

    entries: List[Dict[str, Any]] = []
    truncated = False
    try:
        names = sorted(os.listdir(base))
    except OSError as exc:
        raise OSError(f"Cannot list {path}: {exc.strerror}") from exc

    for name in names:
        if not show_hidden and name.startswith(".") and name not in (".gitignore", ".env"):
            continue
        full = os.path.join(base, name)
        try:
            stat = os.stat(full)
        except OSError:
            continue
        is_dir = os.path.isdir(full)
        entries.append({
            "name": name,
            "path": _relative(full, root),
            "is_dir": is_dir,
            "size": 0 if is_dir else stat.st_size,
            "modified": _iso(stat.st_mtime),
            "language": "" if is_dir else language_for(name),
            "readable": os.access(full, os.R_OK),
            "skipped": is_dir and name in SKIP_DIRS,
        })

    if len(entries) > MAX_TREE_ENTRIES:
        entries = entries[:MAX_TREE_ENTRIES]
        truncated = True

    # Directories first, then alphabetical. A flat list of files with no
    # grouping forces the UI to sort, and every UI sorts differently.
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return {
        "path": _relative(base, root),
        "entries": entries,
        "count": len(entries),
        "truncated": truncated,
    }


def tree(
    path: str = ".",
    root: Optional[str] = None,
    depth: int = 2,
    show_hidden: bool = False,
) -> Dict[str, Any]:
    """A bounded recursive listing for the explorer.

    Bounded in two directions — depth and total entries — because an
    unbounded walk of a monorepo is a request that never returns.
    """
    base = _resolve(path, root)
    counter = {"n": 0}
    truncated = {"value": False}

    def _walk(current: str, level: int) -> List[Dict[str, Any]]:
        if level > depth or counter["n"] >= MAX_TREE_ENTRIES:
            return []
        try:
            names = sorted(os.listdir(current))
        except OSError:
            return []
        out: List[Dict[str, Any]] = []
        for name in names:
            if not show_hidden and name.startswith(".") and name not in (".gitignore", ".env"):
                continue
            if counter["n"] >= MAX_TREE_ENTRIES:
                truncated["value"] = True
                return out
            full = os.path.join(current, name)
            if os.path.isdir(full):
                if name in SKIP_DIRS:
                    continue
                children = _walk(full, level + 1)
                counter["n"] += 1 + len(children)
                out.append({
                    "name": name,
                    "path": _relative(full, root),
                    "is_dir": True,
                    "children": children,
                    "skipped": name in SKIP_DIRS,
                })
            else:
                counter["n"] += 1
                out.append({
                    "name": name,
                    "path": _relative(full, root),
                    "is_dir": False,
                    "language": language_for(name),
                })
        return out

    return {
        "path": _relative(base, root),
        "entries": _walk(base, 1),
        "count": counter["n"],
        "truncated": truncated["value"],
    }


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------
def read_file(path: str, root: Optional[str] = None, max_bytes: int = MAX_EDIT_BYTES) -> Dict[str, Any]:
    """Read a text file with metadata. Binary is reported, not decoded."""
    target = _resolve(path, root)
    if not os.path.exists(target):
        raise FileNotFound(f"Not found: {path}")
    if os.path.isdir(target):
        raise IsADirectoryError(f"{path} is a directory")

    size = os.path.getsize(target)
    if size > max_bytes:
        raise ValueError(
            f"{path} is {size} bytes; the limit for opening it is {max_bytes}"
        )

    if not is_text_file(target):
        with open(target, "rb") as handle:
            probe = handle.read(4096)
        if b"\x00" in probe:
            return {
                "path": _relative(target, root),
                "binary": True,
                "content": "",
                "size": size,
                "modified": _iso(os.path.getmtime(target)),
                "language": "plaintext",
                "note": "Binary file. Not shown as text.",
            }

    with open(target, "r", encoding="utf-8", errors="replace") as handle:
        content = handle.read()
    return {
        "path": _relative(target, root),
        "binary": False,
        "content": content,
        "size": size,
        "lines": content.count("\n") + (0 if content.endswith("\n") or not content else 1),
        "modified": _iso(os.path.getmtime(target)),
        "language": language_for(target),
    }


def write_file(
    path: str,
    content: str,
    root: Optional[str] = None,
    create_backup: bool = True,
) -> Dict[str, Any]:
    """Write a file, optionally keeping the previous version alongside it.

    The backup is on by default. Overwriting a source file with no way back is
    the kind of irreversible action the whole permission system exists to
    prevent, and a ``.bak`` file costs nothing.
    """
    target = _resolve(path, root)
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)

    backup_path: Optional[str] = None
    if create_backup and os.path.isfile(target):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        backup_path = f"{target}.{stamp}.bak"
        try:
            shutil.copy2(target, backup_path)
        except OSError as exc:
            # A failed backup is reported, not silently ignored: writing anyway
            # is the user's call, but they should know the backup is missing.
            logger.warning("Could not back up %s: %s", path, exc)
            backup_path = None

    with open(target, "w", encoding="utf-8", newline="") as handle:
        handle.write(content or "")

    return {
        "path": _relative(target, root),
        "bytes": len(content or ""),
        "backup": _relative(backup_path, root) if backup_path else None,
        "modified": _iso(os.path.getmtime(target)),
        "language": language_for(target),
    }


def make_dir(path: str, root: Optional[str] = None, parents: bool = True) -> Dict[str, Any]:
    target = _resolve(path, root)
    existed = os.path.isdir(target)
    os.makedirs(target, exist_ok=parents)
    return {"path": _relative(target, root), "created": not existed}


def rename(path: str, new_name: str, root: Optional[str] = None) -> Dict[str, Any]:
    """Rename within the same parent directory."""
    source = _resolve(path, root)
    clean = (new_name or "").strip()
    if not clean:
        raise ValueError("A new name is required")
    if os.path.sep in clean or (os.path.altsep and os.path.altsep in clean) or clean in (".", ".."):
        # Refused rather than sanitised: "rename" that silently moves a file to a
        # different directory is a different action with a different name.
        raise ValueError(
            "The new name must be a bare filename. Use move to change directories."
        )
    destination = _resolve(os.path.join(os.path.dirname(source), clean), root)
    if os.path.exists(destination):
        raise FileExistsError(f"{clean} already exists")
    os.rename(source, destination)
    return {"from": _relative(source, root), "to": _relative(destination, root)}


def move(source: str, destination: str, root: Optional[str] = None) -> Dict[str, Any]:
    src = _resolve(source, root)
    dst = _resolve(destination, root)
    if not os.path.exists(src):
        raise FileNotFound(f"Not found: {source}")
    parent = os.path.dirname(dst)
    if parent:
        os.makedirs(parent, exist_ok=True)
    shutil.move(src, dst)
    return {"from": _relative(src, root), "to": _relative(dst, root)}


def copy(source: str, destination: str, root: Optional[str] = None) -> Dict[str, Any]:
    src = _resolve(source, root)
    dst = _resolve(destination, root)
    if not os.path.exists(src):
        raise FileNotFound(f"Not found: {source}")
    if os.path.isdir(src):
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
    else:
        parent = os.path.dirname(dst)
        if parent:
            os.makedirs(parent, exist_ok=True)
        shutil.copy2(src, dst)
    return {"from": _relative(src, root), "to": _relative(dst, root)}


def delete(path: str, root: Optional[str] = None, recursive: bool = False) -> Dict[str, Any]:
    """Delete a file, or a directory when ``recursive`` is set."""
    target = _resolve(path, root)
    if not os.path.exists(target):
        raise FileNotFound(f"Not found: {path}")
    if os.path.isdir(target):
        entries = os.listdir(target)
        if entries and not recursive:
            raise ValueError(
                f"{path} is not empty ({len(entries)} entries). "
                "Set recursive=true to delete it and its contents."
            )
        # `entries` is counted *before* the removal, because after it the
        # directory no longer exists and the count would always read zero.
        if recursive:
            shutil.rmtree(target)
        else:
            os.rmdir(target)
        return {"path": _relative(target, root), "kind": "directory", "entries": len(entries)}
    os.unlink(target)
    return {"path": _relative(target, root), "kind": "file"}


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
def search(
    query: str,
    path: str = ".",
    root: Optional[str] = None,
    glob: Optional[str] = None,
    regex: bool = False,
    content: bool = True,
    limit: int = MAX_SEARCH_RESULTS,
) -> Dict[str, Any]:
    """Find files by name, or lines by content.

    Returns objects, not a rendered string: the UI needs file/line/column and a
    model needs the same facts in a form it can reason about. The agent-facing
    tool renders them separately.
    """
    import re

    base = _resolve(path, root)
    needle = str(query or "")
    if not needle.strip():
        raise ValueError("A search query is required")

    try:
        cap = max(1, min(int(limit or MAX_SEARCH_RESULTS), MAX_SEARCH_RESULTS))
    except (TypeError, ValueError):
        cap = MAX_SEARCH_RESULTS

    # The pattern is always compiled, even for a name-only search: a filename
    # match is still a pattern match, and leaving it unset here made
    # `names_only=True` crash on the first candidate rather than search.
    if regex:
        try:
            pattern = re.compile(needle, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"Invalid regular expression: {exc}") from exc
    else:
        pattern = re.compile(re.escape(needle), re.IGNORECASE)

    matches: List[Dict[str, Any]] = []
    truncated = False

    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
        )
        for filename in sorted(filenames):
            if len(matches) >= cap:
                truncated = True
                break
            if glob and not fnmatch.fnmatch(filename, glob):
                continue
            full = os.path.join(dirpath, filename)
            rel = _relative(full, root)

            if not content:
                if pattern.search(filename):
                    matches.append({"path": rel, "name": filename, "line": 0})
                continue

            if not content:
                continue
            if not is_text_file(full):
                continue
            try:
                if os.path.getsize(full) > MAX_PREVIEW_BYTES:
                    continue
                with open(full, "r", encoding="utf-8", errors="replace") as handle:
                    for number, line_text in enumerate(handle, 1):
                        found = pattern.search(line_text)
                        if found:
                            matches.append({
                                "path": rel,
                                "name": filename,
                                "line": number,
                                "column": found.start() + 1,
                                "text": line_text.strip()[:300],
                            })
                            if len(matches) >= cap:
                                truncated = True
                                break
            except OSError:
                continue
        if truncated:
            break

    return {"query": needle, "path": _relative(base, root), "matches": matches,
            "count": len(matches), "truncated": truncated}


# ---------------------------------------------------------------------------
# Metadata and diffs
# ---------------------------------------------------------------------------
def stat(path: str, root: Optional[str] = None) -> Dict[str, Any]:
    target = _resolve(path, root)
    if not os.path.exists(target):
        raise FileNotFound(f"Not found: {path}")
    info = os.stat(target)
    return {
        "path": _relative(target, root),
        "name": os.path.basename(target),
        "is_dir": os.path.isdir(target),
        "size": 0 if os.path.isdir(target) else info.st_size,
        "modified": _iso(info.st_mtime),
        "created": _iso(info.st_ctime),
        "readable": os.access(target, os.R_OK),
        "writable": os.access(target, os.W_OK),
        "language": "" if os.path.isdir(target) else language_for(target),
        "binary": (not os.path.isdir(target)) and not is_text_file(target),
    }


def unified_diff(old: str, new: str, path: str) -> str:
    """A standard unified diff between two texts.

    Used by change review so the user sees exactly what an edit did, in the
    format they already know from git.
    """
    import difflib

    return "".join(difflib.unified_diff(
        (old or "").splitlines(keepends=True),
        (new or "").splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=3,
    ))


def diff_against_disk(path: str, proposed: str, root: Optional[str] = None) -> Dict[str, Any]:
    """Diff proposed content against what is on disk, without writing."""
    target = _resolve(path, root)
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as handle:
            current = handle.read()
    except FileNotFoundError:
        current = ""
    except OSError as exc:
        raise OSError(f"Cannot read {path}: {exc.strerror}") from exc
    text = unified_diff(current, proposed, path)
    return {
        "path": _relative(target, root),
        "diff": text,
        "changed": bool(text.strip()),
        "added_lines": sum(1 for l in text.splitlines() if l.startswith("+") and not l.startswith("+++")),
        "removed_lines": sum(1 for l in text.splitlines() if l.startswith("-") and not l.startswith("---")),
    }


# ---------------------------------------------------------------------------
# Async wrappers
# ---------------------------------------------------------------------------
# The service is written synchronously — filesystem calls are blocking, and a
# function that is sometimes async and sometimes not is a trap for callers. The
# async API is what everything above the service actually uses.
async def atree(path: str = ".", root: Optional[str] = None, **kwargs) -> Dict[str, Any]:
    return await _run(tree, path, root, kwargs.get("depth", 2), kwargs.get("show_hidden", False))


async def alist(path: str = ".", root: Optional[str] = None, show_hidden: bool = False) -> Dict[str, Any]:
    return await _run(list_dir, path, root, show_hidden)


async def aread(path: str, root: Optional[str] = None) -> Dict[str, Any]:
    return await _run(read_file, path, root)


async def awrite(
    path: str,
    content: str,
    root: Optional[str] = None,
    create_backup: bool = True,
) -> Dict[str, Any]:
    """Async form of :func:`write_file`.

    ``create_backup`` is exposed rather than hard-coded, because the REST layer
    lets the caller choose — and a wrapper that silently dropped the flag would
    make "no backup please" impossible to honour.
    """
    return await _run(write_file, path, content, root, create_backup)


async def asearch(**kwargs) -> Dict[str, Any]:
    return await _run(search, **kwargs)


async def astat(path: str, root: Optional[str] = None) -> Dict[str, Any]:
    return await _run(stat, path, root)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _relative(path: str, root: Optional[str]) -> str:
    """Workspace-relative, forward-slashed, for the UI and for the model."""
    base = root or workspace_root()
    try:
        rel = os.path.relpath(path, base)
    except ValueError:
        return path.replace("\\", "/")
    return rel.replace("\\", "/")


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")


def workspace_root_info() -> Dict[str, Any]:
    """What the file API is rooted at. For the explorer's header."""
    root = workspace_root()
    return {
        "root": root,
        "exists": os.path.isdir(root),
        "skipped_dirs": sorted(SKIP_DIRS),
        "max_edit_bytes": MAX_EDIT_BYTES,
    }


__all__ = [
    "FileAccessDenied",
    "FileNotFound",
    "LANGUAGE_BY_SUFFIX",
    "MAX_EDIT_BYTES",
    "MAX_PREVIEW_BYTES",
    "MAX_SEARCH_RESULTS",
    "MAX_TREE_ENTRIES",
    "SKIP_DIRS",
    "TEXT_SUFFIXES",
    "aread",
    "alist",
    "asearch",
    "astat",
    "atree",
    "awrite",
    "copy",
    "delete",
    "diff_against_disk",
    "is_text_file",
    "language_for",
    "list_dir",
    "make_dir",
    "move",
    "read_file",
    "rename",
    "search",
    "stat",
    "tree",
    "unified_diff",
    "workspace_root_info",
    "write_file",
]