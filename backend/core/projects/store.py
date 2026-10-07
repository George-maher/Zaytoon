"""The project registry: which directories the user works in.

The design constraint is that a project is **not** the workspace. The workspace
is the containment root every file tool is confined to; a project is a directory
*inside* it that the user has opened. Several projects can exist at once, one
can be pinned, and switching between them must not silently widen what the
assistant can reach — which is why :func:`register` refuses a path outside the
workspace instead of quietly extending the root.

That refusal is the whole point. A registry that would accept any absolute path
would become a containment bypass: "open D:\\ as a project" and every file tool
follows.
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from common.reporting import safe_error
from core import db
from core.events import CoreEventType, bus
from core.projects.detect import ProjectProfile, detect

logger = logging.getLogger("Zaytoon.Core.Projects.Store")


def _now() -> str:
    """Microsecond precision, deliberately.

    "Most recently opened" is a recency ordering, and second resolution makes
    two opens in the same second indistinguishable — so the recent list would
    order them by row id instead, which is neither correct nor stable. Every
    other table here uses second resolution because nothing orders by it.
    """
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def project_id_for(path: str) -> str:
    """A stable id for a path.

    Derived from the resolved absolute path, so reopening the same project
    updates its row rather than creating a second one — which is what a random
    uuid would do, and the duplicate rows are the reason ``git status`` in two
    places showing different branches happens.
    """
    resolved = os.path.realpath(os.path.abspath(path)).lower()
    return hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:16]


class ProjectRejected(ValueError):
    """The path cannot be registered, with a reason the user can act on."""


def _workspace_root() -> str:
    try:
        from config import config

        return str(config.workspace_path)
    except Exception:  # pragma: no cover - defensive
        return os.getcwd()


def validate_path(path: str) -> str:
    """Resolve ``path`` and require it to sit inside the workspace.

    Returns the resolved absolute path. Raises :class:`ProjectRejected` with a
    message naming the workspace, because "access denied" without saying where
    the boundary is makes the user guess.
    """
    raw = (path or "").strip()
    if not raw:
        raise ProjectRejected("No path provided")

    root = _workspace_root()
    expanded = os.path.expandvars(os.path.expanduser(raw))
    if not os.path.isabs(expanded):
        expanded = os.path.join(root, expanded)
    try:
        resolved = os.path.realpath(os.path.abspath(expanded))
        root_real = os.path.realpath(os.path.abspath(root))
    except (OSError, ValueError) as exc:
        raise ProjectRejected(f"Cannot use that path: {type(exc).__name__}") from exc

    if resolved != root_real:
        try:
            contained = os.path.commonpath([resolved, root_real]) == root_real
        except ValueError:
            contained = False
        if not contained:
            raise ProjectRejected(
                f"'{path}' is outside the workspace ({root}). "
                "Open the workspace there first, or choose a directory inside it."
            )

    if not os.path.isdir(resolved):
        raise ProjectRejected(f"Not a directory: {path}")
    return resolved


async def register(
    path: str,
    profile: Optional[ProjectProfile] = None,
    pinned: bool = False,
) -> Dict[str, Any]:
    """Open a project: validate, detect and upsert.

    Validation is the interesting part — see :func:`validate_path`. Detection is
    one shallow pass; see :mod:`core.projects.detect`.
    """
    resolved = validate_path(path)
    found = profile or detect(resolved)
    project_id = project_id_for(resolved)
    stamp = _now()
    await db.execute(
        """
        INSERT INTO projects
            (id, name, path, kind, language, package_manager, framework,
             run_command, test_command, build_command, entry_points,
             is_git, branch, pinned, last_opened_at, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name, kind = excluded.kind,
            language = excluded.language, package_manager = excluded.package_manager,
            framework = excluded.framework, run_command = excluded.run_command,
            test_command = excluded.test_command, build_command = excluded.build_command,
            entry_points = excluded.entry_points, is_git = excluded.is_git,
            branch = excluded.branch, pinned = excluded.pinned,
            last_opened_at = excluded.last_opened_at
        """,
        (
            project_id,
            os.path.basename(resolved) or resolved,
            resolved,
            found.kind, found.language, found.package_manager, found.framework,
            found.run_command, found.test_command, found.build_command,
            ",".join(found.entry_points), int(found.is_git), found.branch,
            int(bool(pinned)), stamp, stamp,
        ),
    )
    payload = found.to_dict()
    payload.update({
        "id": project_id,
        "name": os.path.basename(resolved) or resolved,
        "pinned": bool(pinned),
        "last_opened_at": stamp,
    })
    bus.publish(CoreEventType.PROJECT_CHANGED, project=payload, action="opened")
    logger.info("Project opened: %s (%s)", payload["name"], found.kind)
    return payload


async def get(project_id: str) -> Optional[Dict[str, Any]]:
    row = await db.fetch_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    return _row_to_dict(row) if row else None


async def get_by_path(path: str) -> Optional[Dict[str, Any]]:
    try:
        resolved = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    except (OSError, ValueError):
        return None
    row = await db.fetch_one("SELECT * FROM projects WHERE path = ?", (resolved,))
    return _row_to_dict(row) if row else None


async def list_projects(pinned_only: bool = False, limit: int = 100) -> List[Dict[str, Any]]:
    """Pinned first, then most recently opened."""
    where = "WHERE pinned = 1" if pinned_only else ""
    try:
        bound = max(1, min(int(limit or 100), 500))
    except (TypeError, ValueError):
        bound = 100
    rows = await db.fetch_all(
        f"SELECT * FROM projects {where} ORDER BY pinned DESC, "
        "COALESCE(last_opened_at, '') DESC, rowid DESC, name ASC LIMIT ?",
        (bound,),
    )
    return [_row_to_dict(row) for row in rows]


async def recent(limit: int = 10) -> List[Dict[str, Any]]:
    try:
        bound = max(1, min(int(limit or 10), 50))
    except (TypeError, ValueError):
        bound = 10
    rows = await db.fetch_all(
        # `rowid DESC` is the tie-break: two opens in the same microsecond are
        # still ordered by insertion, deterministically, rather than left to
        # SQLite's discretion.
        "SELECT * FROM projects WHERE last_opened_at IS NOT NULL "
        "ORDER BY last_opened_at DESC, rowid DESC LIMIT ?",
        (bound,),
    )
    return [_row_to_dict(row) for row in rows]


async def set_pinned(project_id: str, pinned: bool) -> bool:
    # `execute_affected`, not `execute`: an UPDATE matching no rows succeeds as a
    # statement, and reporting "pinned" for a project that does not exist is
    # reporting something that did not happen.
    changed = await db.execute_affected(
        "UPDATE projects SET pinned = ? WHERE id = ?", (int(bool(pinned)), project_id)
    )
    if changed > 0:
        bus.publish(
            CoreEventType.PROJECT_CHANGED,
            project={"id": project_id},
            action="pinned" if pinned else "unpinned",
        )
    return changed > 0


async def update_commands(
    project_id: str,
    run_command: Optional[str] = None,
    test_command: Optional[str] = None,
    build_command: Optional[str] = None,
) -> bool:
    """Correct the detected defaults.

    Only non-``None`` values are written, so a caller can fix the test command
    without clearing the run command it did not mean to touch.
    """
    sets: List[str] = []
    params: List[Any] = []
    for column, value in (
        ("run_command", run_command),
        ("test_command", test_command),
        ("build_command", build_command),
    ):
        if value is not None:
            sets.append(f"{column} = ?")
            params.append(str(value).strip() or None)
    if not sets:
        return False
    params.append(project_id)
    return await db.execute(
        f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", params
    )


async def remove(project_id: str) -> bool:
    """Forget a project. The directory itself is never touched.

    Reports whether a row was actually removed, so "deleted" is never claimed
    for a project that was not there.
    """
    return await db.execute_affected(
        "DELETE FROM projects WHERE id = ?", (project_id,)
    ) > 0


def _row_to_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    entry_points = row.get("entry_points") or ""
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "path": row.get("path"),
        "kind": row.get("kind"),
        "language": row.get("language"),
        "package_manager": row.get("package_manager"),
        "framework": row.get("framework"),
        "run_command": row.get("run_command"),
        "test_command": row.get("test_command"),
        "build_command": row.get("build_command"),
        "entry_points": [e for e in entry_points.split(",") if e],
        "is_git": bool(row.get("is_git")),
        "branch": row.get("branch"),
        "pinned": bool(row.get("pinned")),
        "last_opened_at": row.get("last_opened_at"),
        "created_at": row.get("created_at"),
    }


__all__ = [
    "ProjectRejected",
    "get",
    "get_by_path",
    "list_projects",
    "project_id_for",
    "recent",
    "register",
    "remove",
    "set_pinned",
    "update_commands",
    "validate_path",
]