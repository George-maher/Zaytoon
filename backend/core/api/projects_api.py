"""``/projects`` — open, list, pin, and correct a detected project profile."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query

from core.projects import (
    ProjectRejected,
    build_context,
    detect,
    get,
    list_projects,
    recent,
    register,
    remove,
    set_pinned,
    update_commands,
    validate_path,
)

logger = logging.getLogger("Zaytoon.Core.Api.Projects")

router = APIRouter()


@router.get("/projects")
async def list_all(pinned_only: bool = Query(False)) -> Dict[str, Any]:
    """Registered projects, pinned first."""
    projects = await list_projects(pinned_only=pinned_only)
    return {"projects": projects, "count": len(projects)}


@router.get("/projects/recent")
async def list_recent(limit: int = Query(10, ge=1, le=50)) -> Dict[str, Any]:
    """Most recently opened."""
    entries = await recent(limit)
    return {"projects": entries, "count": len(entries)}


@router.post("/projects", status_code=201)
async def open_project(data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Open a folder as a project.

    Detection runs here and the result is stored, so the next turn does not
    repeat it. A path outside the workspace is refused with a message naming the
    workspace — the containment boundary is the one thing the user cannot work
    around by asking.
    """
    path = (data.get("path") or "").strip()
    if not path:
        raise HTTPException(400, "'path' is required")
    try:
        return await register(path, pinned=bool(data.get("pinned")))
    except ProjectRejected as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/projects/{project_id}")
async def get_project(project_id: str) -> Dict[str, Any]:
    project = await get(project_id)
    if project is None:
        raise HTTPException(404, f"No such project: {project_id}")
    return project


@router.post("/projects/{project_id}/pin")
async def pin_project(project_id: str, data: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    pinned = bool(data.get("pinned", True))
    if not await set_pinned(project_id, pinned):
        raise HTTPException(404, f"No such project: {project_id}")
    return await get_project(project_id)


@router.patch("/projects/{project_id}/commands")
async def set_commands(
    project_id: str, data: Dict[str, Any] = Body(...)
) -> Dict[str, Any]:
    """Correct the detected run/test/build commands.

    Only the keys present in the body are written, so fixing the test command
    does not clear the run command the caller did not mention.
    """
    ok = await update_commands(
        project_id,
        run_command=data.get("run_command"),
        test_command=data.get("test_command"),
        build_command=data.get("build_command"),
    )
    if not ok:
        raise HTTPException(404, f"No such project: {project_id}, or nothing to update")
    return await get_project(project_id)


@router.get("/projects/{project_id}/context")
async def project_context(project_id: str) -> Dict[str, Any]:
    """The exact block that would be given to the model.

    Exposed so the settings panel can show the user what the assistant knows
    about their project. A context block the user cannot inspect is a source of
    confident mistakes they cannot correct.
    """
    project = await get(project_id)
    if project is None:
        raise HTTPException(404, f"No such project: {project_id}")
    return {"project_id": project_id, "context": build_context(project)}


@router.post("/projects/detect")
async def detect_only(data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Detect without registering. For the "what is this folder?" button."""
    path = (data.get("path") or "").strip()
    if not path:
        raise HTTPException(400, "'path' is required")
    try:
        resolved = validate_path(path)
    except ProjectRejected as exc:
        raise HTTPException(400, str(exc)) from exc
    return detect(resolved).to_dict()


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str) -> Dict[str, Any]:
    """Forget a project. The directory is never touched."""
    if not await remove(project_id):
        raise HTTPException(404, f"No such project: {project_id}")
    return {"status": "deleted", "id": project_id}