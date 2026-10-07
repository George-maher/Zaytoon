"""``/git`` — source control over REST, for the workspace panel.

The same asymmetry `files_api.py` documents applies here, and for the same
reason: these routes are the **user's** source-control panel, not the model's,
so there is no permission gate between the caller and git. Containment is not
affected by that — `resolve_workspace` runs unconditionally, and "the browser
tab asked nicely" is not authorisation, especially since the browser's origin is
not authenticated at all.

Every function here delegates to `core.workspace.git`, which reuses
`tools.git`'s fixed-argv runner and its value allow-lists. That is the point of
the service: there is one git runner, one containment check and one set of
opinions about which characters are allowed in a ref.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException, Query

from core.workspace import git

logger = logging.getLogger("Zaytoon.Core.Api.Git")

router = APIRouter()


def _fail(exc: Exception) -> HTTPException:
    """Map a git failure to the status the UI can act on.

    403 for a path outside the workspace, 404 for "not a repository here",
    409 for "git refused" (a dirty tree, nothing staged, no such branch) and
    422 for a request the caller got wrong. Collapsing those into one 500 would
    make every one of them look like a bug in Zaytoon.
    """
    if isinstance(exc, git.OutsideWorkspace):
        return HTTPException(403, str(exc))
    if isinstance(exc, git.GitNotARepository):
        return HTTPException(404, str(exc))
    if isinstance(exc, git.GitNotAvailable):
        return HTTPException(503, str(exc))
    if isinstance(exc, git.GitCommandFailed):
        return HTTPException(409, str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(422, str(exc))
    logger.warning("Unhandled git service error: %s", type(exc).__name__)
    return HTTPException(500, "Git error")


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------
@router.get("/git/status")
async def git_status(path: str = Query(".")) -> Dict[str, Any]:
    try:
        return await git.status(path)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@router.get("/git/log")
async def git_log(
    limit: int = Query(20, ge=1, le=100),
    path: str = Query("."),
) -> Dict[str, Any]:
    try:
        commits = await git.log(limit, path)
        return {"commits": commits, "total": len(commits)}
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@router.get("/git/diff")
async def git_diff(
    path: str = Query("."),
    ref: Optional[str] = Query(None),
    staged: bool = Query(False),
    file: Optional[str] = Query(None),
) -> Dict[str, Any]:
    try:
        return await git.diff(path, ref or "", staged, file or "")
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@router.get("/git/branches")
async def git_branches(path: str = Query(".")) -> Dict[str, Any]:
    try:
        return await git.branches(path)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@router.get("/git/remotes")
async def git_remotes(path: str = Query(".")) -> Dict[str, Any]:
    try:
        found = await git.remotes(path)
        return {"remotes": found, "total": len(found)}
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------
@router.post("/git/add")
async def git_add(data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Stage files, or everything.

    Idempotent and reversible, so this is the least consequential write here.
    """
    files = data.get("files")
    if files is not None and not isinstance(files, list):
        raise HTTPException(422, "'files' must be an array")
    try:
        return await git.add(
            files=files, all_files=bool(data.get("all", False)), path=data.get("path", ".")
        )
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@router.post("/git/commit")
async def git_commit(data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    try:
        result = await git.commit(data.get("message") or "", data.get("path", "."))
        # The new SHA comes back from `status`/`log` rather than being parsed out
        # of git's human-readable confirmation, which is not a stable format.
        result["status"] = await git.status(data.get("path", "."))
        return result
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@router.post("/git/checkout")
async def git_checkout(data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    try:
        return await git.checkout(
            data.get("ref") or "", create=bool(data.get("create", False)),
            path=data.get("path", "."),
        )
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@router.post("/git/push")
async def git_push(data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Push to a remote.

    Not reversible from here, and it publishes: the caller is the user's own
    panel, which is the authorisation, but the UI is expected to confirm first.
    """
    try:
        return await git.push(
            remote=data.get("remote") or "origin",
            branch=data.get("branch") or "",
            path=data.get("path", "."),
        )
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@router.post("/git/fetch")
async def git_fetch(data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Update remote-tracking refs. The safest network verb here: it changes
    neither the working tree nor the index."""
    try:
        return await git.fetch(remote=data.get("remote") or "", path=data.get("path", "."))
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@router.post("/git/pull")
async def git_pull(data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Pull, fast-forward by default. A diverged branch is a 409 carrying git's
    own explanation, which the panel shows before offering an explicit merge."""
    try:
        return await git.pull(
            remote=data.get("remote") or "",
            branch=data.get("branch") or "",
            ff_only=bool(data.get("ff_only", True)),
            path=data.get("path", "."),
        )
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@router.post("/git/merge")
async def git_merge(data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Merge a branch into the current one. Conflicts map to 409 with git's own
    conflict list — the file names in it are exactly what the user needs."""
    try:
        return await git.merge(
            branch=data.get("branch") or "",
            message=data.get("message") or "",
            path=data.get("path", "."),
        )
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@router.post("/git/restore")
async def git_restore(data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Unstage, or discard working-tree changes.

    Discarding loses uncommitted work, so the panel must confirm first — there
    is no gate here by design (the human is the caller) and the UI must not
    pretend otherwise.
    """
    files = data.get("files")
    if not isinstance(files, list) or not files:
        raise HTTPException(422, "'files' must be a non-empty array")
    try:
        return await git.restore(
            files=files, staged=bool(data.get("staged", False)), path=data.get("path", ".")
        )
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc