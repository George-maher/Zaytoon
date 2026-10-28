"""``/git`` — source control over REST, for the workspace panel.

The same asymmetry `files_api.py` documents applies here, and for the same
reason: these routes are the **user's** source-control panel, not the model's,
so there is no permission gate between the caller and git. Containment is not
affected by that — `resolve_workspace` runs unconditionally, and "the browser
tab asked nicely" is not authorisation, especially since the browser's origin is
not authenticated at all.

**The consequential verbs are two-step.** A push publishes, a merge rewrites a
branch, a discard deletes uncommitted work, a commit hangs the user's name on
tree state, and a checkout moves their head — none of which should be carried
out by the first button that mentioned them. The other surfaces that need a
genuine confirmation over REST (the security probe, the VM actions) already do
this, and `core.api.approvals` documents the token contract: single use,
short-lived, and checked for age at use. Two properties are kept
locally and are the reason this surface bothers:

* **Step 1 has no side effect.** Arguments are validated, containment is
  checked with the very same `resolve_workspace` the service will run, and the
  preview is built from the arguments — no git command executes. A preview that
  can fail halfway through doing the work is not a preview.
* **The token is bound to the operation it authorises.** Like the VM guest-exec
  route, the exact parameters are recorded at preview time and replayed on
  confirm, so a token minted for one branch and one remote cannot push another.
  A caller without a "confirm" field gets a preview only, never an effect.

Every function here delegates to `core.workspace.git`, which reuses
`tools.git`'s fixed-argv runner and its value allow-lists. That is the point of
the service: there is one git runner, one containment check and one set of
opinions about which characters are allowed in a ref.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException, Query

from core.api.approvals import ApprovalTokens, tier_of
from core.workspace import git

logger = logging.getLogger("Zaytoon.Core.Api.Git")

router = APIRouter()

#: Approval tokens for this surface. Deliberately its own store: a token minted
#: for `/git` cannot be spent on the terminal or the VM actions, and vice versa.
_GIT_APPROVALS = ApprovalTokens(ttl=120.0, cap=256, prefix="git")

#: token -> {"action", "summary", "params"}. The whole operation a token
#: authorises, fixed at preview time — the confirm step replays *this* record,
#: not the confirm request's body, so an approved summary cannot describe one
#: commit and carry out another. Swept against the token store so it cannot
#: outlive it.
_PENDING: Dict[str, Dict[str, Any]] = {}


def _sweep_pending() -> None:
    """Drop pending operations whose token is gone (expired or evicted)."""
    live = _GIT_APPROVALS.tokens
    for token in list(_PENDING):
        if token not in live:
            _PENDING.pop(token, None)


async def _commit(params: Dict[str, Any]) -> Dict[str, Any]:
    result = await git.commit(params["message"], params["path"])
    # The new SHA comes back from `status`/`log` rather than being parsed out
    # of git's human-readable confirmation, which is not a stable format.
    result["status"] = await git.status(params["path"])
    return result


async def _checkout(params: Dict[str, Any]) -> Dict[str, Any]:
    return await git.checkout(
        params["ref"], create=params["create"], path=params["path"]
    )


async def _push(params: Dict[str, Any]) -> Dict[str, Any]:
    return await git.push(
        remote=params["remote"], branch=params["branch"], path=params["path"]
    )


async def _merge(params: Dict[str, Any]) -> Dict[str, Any]:
    return await git.merge(
        branch=params["branch"], message=params["message"], path=params["path"]
    )


async def _restore(params: Dict[str, Any]) -> Dict[str, Any]:
    return await git.restore(
        files=params["files"], staged=params["staged"], path=params["path"]
    )


_HANDLERS = {
    "commit": _commit,
    "checkout": _checkout,
    "push": _push,
    "merge": _merge,
    "restore": _restore,
}

#: Where a preview reads its tier, for exactly the verbs the tool registry also
#: classifies. `merge` and `restore` are panel-only verbs with no registered
#: counterpart — the router classifies them itself, and "confirm" is the
#: classification: the tier this router enforces for them by asking.
_PREVIEW_TOOLS = {
    "commit": "git_commit",
    "checkout": "git_checkout",
    "push": "git_push",
    "merge": None,
    "restore": None,
}


def _contained(path: str) -> None:
    """Refuse an outside path at preview time.

    The same `resolve_workspace` the service runs, called before a token is
    minted: a preview must not offer to confirm an action that can never run,
    and a refusal is the honest 403 whether or not a caller asked for one.
    """
    from tools.sandbox import resolve_workspace

    if not resolve_workspace(path or "."):
        raise HTTPException(403, f"'{path}' is outside the workspace.")


async def _two_step(
    data: Dict[str, Any], action: str, summary: str, params: Dict[str, Any]
) -> Dict[str, Any]:
    """Run the two-step flow and return the outcome the route should send.

    Without ``confirm``: mint a token, record the whole operation against it,
    and return the preview frame — nothing has run. With a valid token: replay
    the recorded operation and return its result. Anything else is a 409, the
    same refusal the probe and VM routes give.

    ``params`` keys must not be user-controlled *names*: the handlers unpack
    them positionally by name, so a stray key is dead weight rather than a
    path in.
    """
    contained = params.get("path", ".")
    _contained(contained)

    confirm = str(data.get("confirm") or "").strip()
    if not confirm:
        token = _GIT_APPROVALS.issue()
        _sweep_pending()
        _PENDING[token] = {"action": action, "summary": summary, "params": params}
        tool = _PREVIEW_TOOLS[action]
        return {
            "requires_confirmation": True,
            "confirm": token,
            "action": action,
            # Read from the live registry where a registered counterpart
            # exists (`_PREVIEW_TOOLS`), so a preview cannot disagree with
            # what the same action would cost the model's path.
            "tier": tier_of(tool, params) if tool else "confirm",
            "summary": summary,
            "note": "Nothing has run yet. Confirming carries out exactly this.",
        }

    if not _GIT_APPROVALS.consume(confirm):
        raise HTTPException(
            409, "Unknown or expired confirmation token. Preview the change again."
        )
    pending = _PENDING.pop(confirm, None)
    if pending is None or pending["action"] != action:
        # A token minted by one verb must not be spent through another route's
        # door — even though the recorded params would be replayed, being
        # silently told "commit" while confirming a checkout is a contract
        # breach rather than a mismatch.
        raise HTTPException(
            409, "This approval is no longer attached to an action. Preview it again."
        )
    try:
        return await _HANDLERS[pending["action"]](pending["params"])
    except Exception as exc:  # noqa: BLE001
        # Step 2 keeps the same failure vocabulary (`_fail`) the routes have
        # always reported: containment 403, "not a repository" 404, git's own
        # refusal 409. Containment was already checked at preview, but git can
        # still refuse (dirty tree, nothing staged, unknown branch).
        raise _fail(exc) from exc


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
    path = data.get("path", ".")
    message = data.get("message") or ""
    if not str(message).strip():
        # The service would raise the same error; checking here means a
        # malformed request never mints a token.
        raise HTTPException(422, "'message' is required")
    shown = " ".join(str(message).split())[:120]
    return await _two_step(
        data, "commit", f'Commit staged changes as "{shown}"',
        {"message": str(message), "path": path},
    )


@router.post("/git/checkout")
async def git_checkout(data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    ref = data.get("ref") or ""
    create = bool(data.get("create", False))
    if not str(ref).strip():
        raise HTTPException(422, "'ref' is required")
    return await _two_step(
        data, "checkout",
        f"Switch to {ref}" + (" (new branch)" if create else ""),
        {"ref": str(ref), "create": create, "path": data.get("path", ".")},
    )


@router.post("/git/push")
async def git_push(data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Push to a remote.

    Publishing is not reversible from here, so this is a confirmation verb.
    """
    remote = data.get("remote") or "origin"
    branch = data.get("branch") or ""
    return await _two_step(
        data, "push",
        f"Push {branch or 'the current branch'} to {remote}"
        + ("" if branch else " (no branch named — pushes upstream's default)"),
        {"remote": str(remote), "branch": str(branch), "path": data.get("path", ".")},
    )


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
    branch = data.get("branch") or ""
    if not str(branch).strip():
        raise HTTPException(422, "'branch' is required")
    return await _two_step(
        data, "merge",
        f"Merge {branch} into the current branch",
        {
            "branch": str(branch),
            "message": data.get("message") or "",
            "path": data.get("path", "."),
        },
    )


@router.post("/git/restore")
async def git_restore(data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Unstage, or discard working-tree changes.

    Discarding loses uncommitted work — the irreversible half — so it is a
    confirmation verb. Unstaging (`staged: true`) only moves entries between
    the index and the tree, which is reversible, so it stays single-step.

    This is the one verb gated by *what it does* rather than by route: the same
    endpoint carries both, and the panel's Unstage button must not grow the
    ceremony of the panel's Discard button. The severance is on the server, so
    restoring the ceremony later needs no UI change.
    """
    files = data.get("files")
    if not isinstance(files, list) or not files:
        raise HTTPException(422, "'files' must be a non-empty array")
    staged = bool(data.get("staged", False))
    params = {
        "files": [str(f) for f in files],
        "staged": staged,
        "path": data.get("path", "."),
    }
    if staged:
        try:
            return await git.restore(**params)
        except Exception as exc:  # noqa: BLE001
            raise _fail(exc) from exc
    shown = ", ".join(params["files"][:5])
    if len(params["files"]) > 5:
        shown += f" … (+{len(params['files']) - 5} more)"
    return await _two_step(
        data, "restore",
        f"Discard uncommitted changes to: {shown}",
        params,
    )