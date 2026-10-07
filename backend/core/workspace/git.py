"""Git as a service: structured results for the UI, plain text for the agent.

`tools/git.py` already runs git safely — fixed argv, no shell, containment-checked
cwd, a conservative character allow-list on every model-supplied value. That
plumbing is reused here rather than reimplemented, because a second git runner is
a second set of opinions about what is safe.

What this adds is **structure**. The tools return rendered text because a model
reads text; a source-control panel needs objects — which branch is current, which
files are staged versus modified, what the last five commits were — and
rendering that from `--short` output on the client means parsing porcelain
output in JavaScript, which is the worst place for it.

Read-only by default. Mutating verbs are here too, because the user's own editor
is the caller and the gate exists to stop the *model* acting without a human —
the same asymmetry `core/api/files_api.py` documents. Containment still applies
unconditionally, and `POST /git/commit` refuses to run unattended rather than
implying an approval happened.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Any, Dict, List, Optional

from tools.base import ToolValidationError
from tools.git import _check_ref
from tools.sandbox import resolve_workspace

logger = logging.getLogger("Zaytoon.Core.Workspace.Git")

MAX_OUTPUT_CHARS = 40_000
TIMEOUT_S = 30
#: Network verbs (fetch/pull/push) wait on a remote the user does not control.
NETWORK_TIMEOUT_S = 90


class GitNotAvailable(RuntimeError):
    """git is not installed or not on PATH."""


class GitNotARepository(RuntimeError):
    """The resolved path is not inside a working tree."""


class OutsideWorkspace(PermissionError):
    """The requested path is not inside the workspace root."""


class GitCommandFailed(RuntimeError):
    """git ran and refused. Carries its own stderr, which is the useful part."""

    def __init__(self, message: str, stderr: str = ""):
        super().__init__(message)
        self.stderr = stderr


async def _git(args: List[str], path: str = ".", timeout: float = TIMEOUT_S) -> str:
    """Run one fixed git subcommand and return stdout.

    Raises rather than returning ``(ok, out, err)``: every caller here wants to
    decide what a failure means in its own terms (404 vs 400 vs 500), and a
    boolean tuple makes each of them re-derive the mapping.
    """
    git = shutil.which("git")
    if git is None:
        raise GitNotAvailable("git is not installed or not on PATH.")

    root = resolve_workspace(path or ".")
    if not root:
        raise OutsideWorkspace(f"'{path}' is outside the workspace.")

    try:
        proc = await asyncio.create_subprocess_exec(
            git,
            # Non-ASCII names come back octal-quoted by default
            # (`"b/\331\205\331\204\331\201.md"`), which is unreadable in a
            # diff pane for exactly the files this product treats as
            # first-class. Per-invocation config: it changes what git *prints*,
            # never what git does.
            "-c",
            "core.quotePath=false",
            *args,  # noqa: S603 - fixed argv, git resolved via which()
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=root,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        raise GitCommandFailed(f"git {args[0]} timed out after {timeout:g}s") from None
    except OSError as exc:
        raise GitCommandFailed(f"Could not run git: {exc}") from exc

    if proc.returncode != 0:
        err_text = err.decode("utf-8", errors="replace").strip()
        out_text = out.decode("utf-8", errors="replace").strip()
        # git reports some failures on **stdout**: `merge` prints its conflict
        # list there, and only "fatal:" lines reach stderr. Falling back to
        # stdout keeps "CONFLICT (content): Merge conflict in f.txt" — which
        # names the file — instead of the generic "git merge failed".
        message = err_text or out_text
        # "Not a repository" is not a command failure — it is a different
        # answer, and the panel needs to tell "there is nothing to show here"
        # apart from "git refused". Mapping both to 409 would make an empty
        # folder look like an error.
        if "not a git repository" in message.lower():
            raise GitNotARepository(
                f"'{path}' is not inside a git repository."
            )
        raise GitCommandFailed(message or f"git {args[0]} failed", stderr=message)
    return out.decode("utf-8", errors="replace")[:MAX_OUTPUT_CHARS]


def _check_pathspec(value: str, field: str) -> str:
    """Validate a file path that becomes one argv entry *after* ``--``.

    Deliberately more permissive than :func:`tools.git._check_value`: that
    allow-list also guards values landing in git's option parsing, while a
    pathspec here always sits behind ``--`` and is executed as a single argv
    element that no shell ever re-parses. Spaces, quotes, semicolons and
    non-ASCII characters are **ordinary file names** — refusing them meant the
    panel could list `my notes.md` (the parser is `-z`-precise about exactly
    those names) but could never stage it.

    Still refused: a leading dash (option-shaped; nobody's file is named that
    in practice), control characters (they garble the panel), wrong type,
    empty, and unreasonable length.
    """
    if not isinstance(value, str) or not value:
        raise ToolValidationError(f"'{field}' is required")
    if len(value) > 400:
        raise ToolValidationError(f"'{field}' is too long (max 400 characters)")
    if value.startswith("-"):
        raise ToolValidationError(f"'{field}' must not look like an option: {value!r}")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ToolValidationError(f"'{field}' contains control characters: {value!r}")
    return value


async def _ok(args: List[str], path: str = ".") -> bool:
    """True when the command succeeded. For probes, where a failure is the answer."""
    try:
        await _git(args, path)
        return True
    except (GitCommandFailed, GitNotARepository, GitNotAvailable, OutsideWorkspace):
        # `GitNotARepository` matters: `rev-parse --is-inside-work-tree` is
        # exactly how you *ask* whether something is a repository, so raising it
        # here is the normal answer rather than a failure to swallow.
        return False


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------
async def is_repository(path: str = ".") -> bool:
    """Whether this path is inside a git working tree.

    Not an error case: "this folder is not a repository" is the answer the UI
    needs to decide whether to show the panel at all.
    """
    return await _ok(["rev-parse", "--is-inside-work-tree"], path)


async def repo_root(path: str = ".") -> str:
    """The repository's top level, relative to the workspace root.

    Relative because the API's other file routes are relative to it, and an
    absolute path here would leak the install directory into the UI for no gain.
    """
    from tools.sandbox import workspace_root

    raw = (await _git(["rev-parse", "--show-toplevel"], path)).strip()
    root = str(workspace_root())
    try:
        import os

        return os.path.relpath(raw, root).replace("\\", "/")
    except ValueError:  # pragma: no cover - different drives on Windows
        return raw


#: Porcelain status codes, mapped to what a person would call them. The two-letter
#: codes are ambiguous on their own (``M ` means staged, `` M` means not), which
#: is exactly why the client should not be the one decoding them.
_STATUS_NAMES = {
    "M": "modified", "A": "added", "D": "deleted", "R": "renamed",
    "C": "copied", "U": "unmerged", "T": "typechanged", "X": "unknown",
}


def _entry(code: str, path: str) -> Dict[str, Any]:
    if code[0] == "?" or code[1] == "?":
        return {
            "path": path,
            "status": "untracked",
            "staged": False,
            "unstaged": False,
        }
    index_code, worktree_code = code[0], code[1]
    if index_code != " " and worktree_code == " ":
        status = _STATUS_NAMES.get(index_code, "changed")
    elif index_code == " " and worktree_code != " ":
        status = _STATUS_NAMES.get(worktree_code, "changed")
    else:
        status = "both"
    # Each column is its own porcelain column: `M ` is a staged change whose
    # worktree matches the index, so it has nothing unstaged. Hardcoding
    # `unstaged: True` here made every staged file also appear in the
    # unstaged list — a phantom row the user could not act on.
    return {
        "path": path,
        "status": status,
        "staged": index_code != " ",
        "unstaged": worktree_code != " ",
    }


async def status(path: str = ".") -> Dict[str, Any]:
    """The working tree, as objects rather than porcelain text."""
    raw = await _git(["status", "--porcelain=v1", "--branch", "-z"], path)

    branch = ""
    entries: List[Dict[str, Any]] = []
    # `-z` is NUL-separated, which is the only way this parse is safe: a file
    # name may legitimately contain a space, a quote or a non-ASCII character,
    # and the default line-based output would silently split it.
    for chunk in raw.split("\0"):
        if not chunk:
            continue
        if chunk.startswith("## "):
            # "## main...origin/main [ahead 1, behind 2]"
            head = chunk[3:].strip()
            branch = head.split("...")[0].strip()
            continue
        if len(chunk) < 4:
            continue
        code, name = chunk[:2], chunk[3:]
        if code[0] in ("R", "C") and " -> " in name:
            name = name.split(" -> ", 1)[0]
        entries.append(_entry(code, name))

    return {
        "repository": await repo_root(path),
        "branch": branch,
        "entries": entries,
        "staged": sum(1 for e in entries if e["staged"]),
        "unstaged": sum(1 for e in entries if e["unstaged"]),
        "untracked": sum(1 for e in entries if e["status"] == "untracked"),
        "clean": not entries,
    }


async def log(limit: int = 10, path: str = ".") -> List[Dict[str, Any]]:
    """Recent commits, newest first."""
    limit = max(1, min(int(limit), 100))
    raw = await _git(
        ["log", f"-{limit}", "--pretty=format:%H%x1f%h%x1f%an%x1f%aI%x1f%s%x00"],
        path,
    )
    commits: List[Dict[str, Any]] = []
    # NUL separates records because it is the one byte a git commit message
    # cannot contain — unlike a printable separator, which a subject can carry
    # and which would then split the record in two.
    for record in raw.split("\x00"):
        record = record.strip("\n")
        if not record.strip():
            continue
        # `maxsplit=4`: the subject is the *last* field and is the only one a
        # user controls, so it may legitimately contain the field separator.
        # Splitting without a limit would shift every field after it.
        parts = record.split("\x1f", 4)
        if len(parts) != 5:
            continue
        sha, short, author, date, subject = parts
        commits.append({
            "sha": sha,
            "short": short,
            "author": author,
            "date": date,
            "subject": subject,
        })
    return commits


async def diff(
    path: str = ".", ref: str = "", staged: bool = False, file: str = ""
) -> Dict[str, Any]:
    """A unified diff, plus whether there is anything in it.

    ``file`` narrows the output to one path: a panel that says "show me this
    file's changes" must not hand the user the whole tree. The value passes
    ``_check_pathspec`` and sits after ``--``, so a name with spaces or dots
    works while an option-shaped value is refused before git sees it. An
    untracked file diffs empty — git has nothing to compare it against — and
    the caller decides what to say about that.
    """
    args = ["diff", "--unified=3"]
    if staged:
        args = ["diff", "--cached", "--unified=3"]
    if ref:
        args.append(_check_ref(ref, "ref"))
    if file:
        args += ["--", _check_pathspec(file, "file")]
    text = await _git(args, path)
    return {
        "diff": text,
        "empty": not text.strip(),
        "ref": ref,
        "staged": staged,
        "file": file,
    }


async def branches(path: str = ".") -> Dict[str, Any]:
    """Local and remote branches, with the current one marked.

    `--format` rather than parsing `git branch` output: the human-readable form
    wraps, truncates and decorates, and a branch named for a ticket is long
    enough to hit all three.
    """
    # `%(refname)`, not `%(refname:short)`: the short form of a remote-tracking
    # branch is `origin/master`, which is indistinguishable from a *local*
    # branch that happens to be called `origin/master`. Only the full ref says
    # which it is, and "is this local or remote" is the question this endpoint
    # exists to answer.
    raw = await _git(
        ["branch", "--all", "--format=%(refname)%09%(objectname:short)%09%(HEAD)"],
        path,
    )
    result: List[Dict[str, Any]] = []
    current = ""
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        refname, sha, head = parts
        remote = refname.startswith("refs/remotes/")
        if remote and refname.endswith("/HEAD"):
            # `origin/HEAD` is a symbolic pointer, not a branch worth offering.
            continue
        name = refname[len("refs/heads/"):] if refname.startswith("refs/heads/") else refname
        if remote:
            name = name[len("remotes/"):]
        is_current = head.strip() == "*"
        if is_current:
            current = name
        result.append({
            "name": name,
            "sha": sha,
            "current": is_current,
            "remote": remote,
        })
    return {"branches": result, "current": current}


async def remotes(path: str = ".") -> List[Dict[str, str]]:
    """Configured remotes. The URL is included: it is the user's own config."""
    raw = await _git(["remote", "-v"], path)
    seen: Dict[str, str] = {}
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            seen[parts[0]] = parts[1]
    return [{"name": name, "url": url} for name, url in sorted(seen.items())]


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------
async def add(files: Optional[List[str]] = None, all_files: bool = False, path: str = ".") -> Dict[str, Any]:
    """Stage files, or everything with ``all_files``.

    ``--`` separates options from pathspecs, so a name that happens to start
    with a dash can never be read as a flag (the pathspec validator refuses
    those anyway — two layers, because this is the write path).
    """
    if not all_files and not files:
        raise ValueError("provide 'files' or set 'all' to true")
    checked = [] if all_files else [_check_pathspec(f, "files[]") for f in (files or [])]
    args = ["add", "-A"] if all_files else ["add", "--"] + checked
    output = await _git(args, path)
    # What the index holds *now*, not an argv length: `--` would have been
    # counted by the old `len(args) - 1`, and for `-A` that formula counted
    # the flag itself. `status()` parses porcelain `-z`, so an unborn HEAD
    # (a brand-new repository) answers correctly too.
    staged = (await status(path))["staged"]
    return {"output": output.strip(), "staged": staged}


async def commit(message: str, path: str = ".") -> Dict[str, Any]:
    """Commit what is staged."""
    message = (message or "").strip()
    if not message:
        raise ValueError("'message' is required")
    if len(message) > 2000:
        raise ValueError("'message' is too long (max 2000 characters)")
    output = await _git(["commit", "-m", message], path)
    return {"output": output.strip() or "Committed."}


async def checkout(ref: str, create: bool = False, path: str = ".") -> Dict[str, Any]:
    """Switch branch, or create it."""
    args = ["checkout"] + (["-b"] if create else []) + [_check_ref(ref, "ref")]
    output = await _git(args, path)
    return {"output": output.strip() or f"Switched to {ref}.", "ref": ref}


async def push(remote: str = "origin", branch: str = "", path: str = ".") -> Dict[str, Any]:
    """Push to a remote."""
    args = ["push", _check_ref(remote, "remote") if remote else "origin"]
    if branch:
        args.append(_check_ref(branch, "branch"))
    output = await _git(args, path, timeout=NETWORK_TIMEOUT_S)
    return {"output": output.strip() or "Pushed."}


async def fetch(remote: str = "", path: str = ".") -> Dict[str, Any]:
    """Update remote-tracking refs. Touches neither the worktree nor the index.

    ``--prune`` because a branch deleted on the remote otherwise lingers in the
    panel forever, which reads as "the remote still has it".
    """
    args = ["fetch", "--prune"]
    if remote:
        args.append(_check_ref(remote, "remote"))
    output = await _git(args, path, timeout=NETWORK_TIMEOUT_S)
    return {"output": output.strip() or "Fetched.", "remote": remote or ""}


async def pull(
    remote: str = "",
    branch: str = "",
    ff_only: bool = True,
    path: str = ".",
) -> Dict[str, Any]:
    """Pull, fast-forward by default.

    ``--ff-only`` is the default because an implicit merge commit the user did
    not write is a bad surprise from a button. Diverged history is reported by
    git itself (409) and the panel can then offer an explicit merge. ``ff_only
    = False`` does a real merge with git's own default message (``--no-edit``,
    so no editor can be spawned by a REST call).
    """
    args = ["pull", "--ff-only" if ff_only else "--no-rebase", "--no-edit"]
    if remote:
        args.append(_check_ref(remote, "remote"))
    if branch:
        args.append(_check_ref(branch, "branch"))
    output = await _git(args, path, timeout=NETWORK_TIMEOUT_S)
    return {
        "output": output.strip() or "Pulled.",
        "ff_only": ff_only,
        "remote": remote or "",
        "branch": branch or "",
    }


async def merge(branch: str, message: str = "", path: str = ".") -> Dict[str, Any]:
    """Merge another branch into the current one.

    Git writes its own default merge message when one is not given, so no
    editor is involved. Conflicts come back as ``GitCommandFailed`` (409) with
    git's own words — "CONFLICT (content): Merge conflict in …" names the files
    better than any paraphrase would.
    """
    branch = (branch or "").strip()
    if not branch:
        raise ValueError("'branch' is required")
    message = (message or "").strip()
    if len(message) > 2000:
        raise ValueError("'message' is too long (max 2000 characters)")
    args = ["merge", _check_ref(branch, "branch")]
    if message:
        args += ["-m", message]
    output = await _git(args, path, timeout=NETWORK_TIMEOUT_S)
    return {"output": output.strip() or f"Merged {branch}.", "branch": branch}


async def restore(
    files: List[str], staged: bool = False, path: str = "."
) -> Dict[str, Any]:
    """Unstage (``staged=True``) or discard working-tree changes.

    Discarding is destructive to uncommitted work — the REST layer treats it as
    the user's own editor action (like every route here) and the panel confirms
    client-side before calling.

    Unborn-HEAD fallback: in a repository with no commits yet,
    ``git restore --staged`` fails with "could not resolve HEAD" even though
    un staging is exactly what was asked. ``git rm --cached`` performs the same
    index operation without resolving a commit, so the common first-file case
    works instead of returning git's confusing words.
    """
    if not files:
        raise ValueError("'files' is required")
    checked = [_check_pathspec(f, "files[]") for f in files]
    args = ["restore"] + (["--staged"] if staged else []) + ["--"] + checked
    try:
        output = await _git(args, path)
    except GitCommandFailed as exc:
        # Matched without the quotes: git writes `could not resolve 'HEAD'`,
        # and a check for the bare words silently misses it.
        if not staged or "could not resolve" not in exc.stderr.lower():
            raise
        output = await _git(["rm", "--cached", "--"] + checked, path)
    return {
        "output": output.strip() or ("Unstaged." if staged else "Restored."),
        "files": len(files),
        "staged": staged,
    }