"""``/files`` — the file service over HTTP, for the editor and the explorer.

Every mutating route here is reachable **without** the tool registry's
permission gate, because it is the *user's* editor, not the model's. That is a
deliberate and important asymmetry, and it is safe for one reason: the
containment check still applies, unconditionally. The gate exists to stop the
*model* acting without a human; here the human is the caller.

The one thing that does **not** differ is the sandbox. A user-initiated request
cannot write outside the workspace either, because "the browser tab said so" is
not authorisation — and the browser's origin is not authenticated at all. There
is a test asserting that a path outside the workspace is refused here too.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query

from core.workspace import files as fs


async def _off_loop(fn, *args):
    """Run a blocking service function off the event loop."""
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

logger = logging.getLogger("Zaytoon.Core.Api.Files")

router = APIRouter()


def _fail(exc: Exception) -> HTTPException:
    """Map a service exception to the right status.

    The distinction matters to the UI: 404 is "check the path", 403 is "that is
    not yours to touch", 422 is "the request was malformed". All three are far
    more useful than one generic message.
    """
    if isinstance(exc, fs.FileAccessDenied):
        return HTTPException(403, str(exc))
    if isinstance(exc, FileNotFoundError):
        return HTTPException(404, str(exc))
    if isinstance(exc, FileExistsError):
        return HTTPException(409, str(exc))
    if isinstance(exc, IsADirectoryError):
        return HTTPException(400, str(exc))
    if isinstance(exc, (ValueError, NotADirectoryError)):
        return HTTPException(422, str(exc))
    if isinstance(exc, PermissionError):
        return HTTPException(403, str(exc))
    if isinstance(exc, OSError):
        # An OSError's message can be a path, so only its errno name is used.
        return HTTPException(500, f"Filesystem error: {type(exc).__name__}")
    logger.warning("Unhandled file service error: %s", type(exc).__name__)
    return HTTPException(500, "Filesystem error")


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------
@router.get("/files/root")
async def file_root() -> Dict[str, Any]:
    """What the file API is rooted at."""
    return fs.workspace_root_info()


@router.get("/files/tree")
async def file_tree(
    path: str = Query("."),
    depth: int = Query(2, ge=1, le=6),
    show_hidden: bool = Query(False),
) -> Dict[str, Any]:
    """A bounded recursive listing."""
    try:
        return await fs.atree(path, depth=depth, show_hidden=show_hidden)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@router.get("/files/list")
async def file_list(
    path: str = Query("."),
    show_hidden: bool = Query(False),
) -> Dict[str, Any]:
    """One directory level."""
    try:
        return await fs.alist(path, show_hidden=show_hidden)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@router.get("/files/read")
async def file_read(path: str = Query(...)) -> Dict[str, Any]:
    """File contents with metadata and a language hint."""
    try:
        return await fs.aread(path)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@router.get("/files/stat")
async def file_stat(path: str = Query(...)) -> Dict[str, Any]:
    try:
        return await fs.astat(path)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@router.get("/files/search")
async def file_search(
    q: str = Query(..., description="Text or pattern to find"),
    path: str = Query("."),
    glob: Optional[str] = Query(None),
    regex: bool = Query(False),
    names_only: bool = Query(False, description="Match file names, not contents"),
    limit: int = Query(100, ge=1, le=300),
) -> Dict[str, Any]:
    try:
        return await fs.asearch(
            query=q, path=path, glob=glob, regex=regex, content=not names_only, limit=limit
        )
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@router.get("/files/diff")
async def file_diff(
    path: str = Query(...),
    content: str = Query(..., description="Proposed content"),
) -> Dict[str, Any]:
    """Diff proposed content against the file, without writing it."""
    try:
        return await _off_loop(fs.diff_against_disk, path, content, None)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------
@router.post("/files/write")
async def file_write(data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Write a file. The previous version is kept as ``.bak`` by default."""
    path = (data.get("path") or "").strip()
    if not path:
        raise HTTPException(422, "'path' is required")
    if "content" not in data:
        raise HTTPException(422, "'content' is required")
    content = data.get("content")
    if not isinstance(content, str):
        raise HTTPException(422, "'content' must be a string")
    try:
        return await fs.awrite(path, content, create_backup=bool(data.get("backup", True)))
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@router.post("/files/mkdir")
async def file_mkdir(data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    path = (data.get("path") or "").strip()
    if not path:
        raise HTTPException(422, "'path' is required")
    try:
        return await _off_loop(fs.make_dir, path, None, bool(data.get("parents", True)))
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@router.post("/files/rename")
async def file_rename(data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    path = (data.get("path") or "").strip()
    new_name = (data.get("name") or data.get("new_name") or "").strip()
    if not path or not new_name:
        raise HTTPException(422, "'path' and 'name' are required")
    try:
        return await _off_loop(fs.rename, path, new_name, None)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@router.post("/files/move")
async def file_move(data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    source = (data.get("source") or "").strip()
    destination = (data.get("destination") or "").strip()
    if not source or not destination:
        raise HTTPException(422, "'source' and 'destination' are required")
    try:
        return await _off_loop(fs.move, source, destination, None)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@router.post("/files/copy")
async def file_copy(data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    source = (data.get("source") or "").strip()
    destination = (data.get("destination") or "").strip()
    if not source or not destination:
        raise HTTPException(422, "'source' and 'destination' are required")
    try:
        return await _off_loop(fs.copy, source, destination, None)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@router.post("/files/delete")
async def file_delete(data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Delete a file, or a directory when ``recursive`` is set.

    Destructive, so the *UI* is expected to confirm first — but see the module
    docstring: this route is the user's own editor, not the model's, and the
    containment check still applies unconditionally.
    """
    path = (data.get("path") or "").strip()
    if not path:
        raise HTTPException(422, "'path' is required")
    try:
        return await _off_loop(fs.delete, path, None, bool(data.get("recursive", False)))
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc