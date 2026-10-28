"""Launching and path-finding tools.

Two deliberately separate jobs:

* :class:`FindPaths`, :class:`ListApps` and :class:`RefreshInventory` are
  **read-only**. They stat, list, read the registry and the Start Menu, and
  return what they saw. Nothing they do launches a process or opens a file.
* :class:`OpenTarget` is the single launch entry point for the natural-language
  request. It resolves through :mod:`core.targets` and then hands the verified
  artifact to :func:`tools.applications.launch_target`.

The split is the point. A read-only discovery tool that could also launch would
make "list my installed apps" and "run this program" the same permission, and
the inventory would have to be trusted with an execution primitive to answer a
question about a filename.

Every refusal that matters lives below the tool, not in it:
``core.targets.is_refused_open_target`` refuses code-executing suffixes,
``tools.sandbox`` refuses shell syntax, and ``launch_target`` re-verifies that
the target still exists **at the moment of launch** rather than trusting the
scan that produced it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional

from core.targets import (
    is_authorized_path,
    is_refused_open_target,
    resolve_open_request,
    search_paths,
)
from tools.applications import launch_target
from tools.base import BaseTool, ToolResult, ToolValidationError, _prop

logger = logging.getLogger("Zaytoon.Tools.Launcher")

#: How long an application scan is reused before a listing refreshes it. The
#: cost of a stale entry is one honest "no longer installed" from the launch
#: path; the cost of scanning on every message is a Start Menu walk per turn.
_APP_CACHE_TTL_S = 300.0

_APP_CACHE: List[Any] = []
_APP_CACHE_AT = 0.0


def _cache_is_fresh() -> bool:
    return bool(_APP_CACHE) and (time.monotonic() - _APP_CACHE_AT) < _APP_CACHE_TTL_S


async def _installed_apps(force: bool = False) -> List[Any]:
    """The discovered application list, scanning in a thread when stale.

    A scan is a Start Menu walk plus PATH entries and two registry hives, so it
    is filesystem work and belongs off the event loop. ``force`` is what
    :class:`RefreshInventory` exposes; the read-only listing reuses a recent
    scan.
    """
    global _APP_CACHE, _APP_CACHE_AT
    if not force and _cache_is_fresh():
        return list(_APP_CACHE)

    from core.discovery import DeviceDiscoveryCoordinator

    coordinator = DeviceDiscoveryCoordinator()
    loop = asyncio.get_running_loop()
    apps = await loop.run_in_executor(
        None, coordinator.provider.discover_installed_applications
    )
    _APP_CACHE = list(apps)
    _APP_CACHE_AT = time.monotonic()
    logger.info("Application inventory refreshed: %d record(s)", len(_APP_CACHE))
    return list(_APP_CACHE)


def _app_dict(app: Any) -> Dict[str, Any]:
    return {
        "name": getattr(app, "display_name", "") or getattr(app, "name", ""),
        "target": getattr(app, "target", ""),
        "verified": bool(getattr(app, "verified", False)),
        "source": getattr(app, "discovery_source", ""),
        "install_location": getattr(app, "install_location", ""),
    }


class OpenTarget(BaseTool):
    """Open what the user named — site, application, file or folder.

    ``safe`` is the same tier ``open_app`` and ``open_url`` already carry, and
    for the same reason: the tool launches a target the user named, verifies it
    exists, and refuses every suffix that would turn "open my notes" into "run
    my code". It is not a shell: no text from the request is concatenated into
    a command, and the launcher receives one resolved path or one hard-coded
    URL.
    """

    permission = "safe"

    def name(self) -> str:
        return "open_target"

    def aliases(self) -> List[str]:
        return ["open", "launch", "open_thing"]

    def description(self) -> str:
        return (
            "Open whatever the user named: a website ('YouTube', 'يوتيوب'), an "
            "installed application ('chrome', 'vscode'), or a file/folder in "
            "the user's known folders ('report.pdf'). Uses only verified "
            "targets; when several items match it lists them and asks instead "
            "of guessing, and it never reports success for something it did "
            "not open."
        )

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "request": _prop(
                    "string",
                    "The thing to open, as named by the user (no verbs needed).",
                )
            },
            "required": ["request"],
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        request = params.get("request")
        if not isinstance(request, str) or not request.strip():
            raise ToolValidationError("'request' is required")

    async def execute(self, request: str = "", **_: Any) -> ToolResult:
        loop = asyncio.get_running_loop()
        resolved = await loop.run_in_executor(
            None, lambda: resolve_open_request(request, limit=8)
        )
        kind = resolved.get("kind")

        if kind == "website":
            from tools.web import OpenUrl

            site = resolved.get("site") or request
            url = resolved.get("url") or ""
            if not url:
                return ToolResult.fail(f"No website address for '{request}'.")
            opened = await OpenUrl().execute(url=url)
            if opened.success:
                return ToolResult.ok(
                    f"Opened {site} at {url}",
                    {"kind": "website", "site": site, "url": url},
                )
            return opened

        if kind == "app":
            target = resolved.get("target") or ""
            if not target:
                return ToolResult.fail(f"No verified target for '{request}'.")
            result = await launch_target(target)
            if result.data is None:
                result.data = {}
            result.data.update(
                {"kind": "app", "app": resolved.get("app") or request, "via": resolved.get("via", "")}
            )
            return result

        if kind in ("file", "folder"):
            return await self._open_path(resolved["path"], kind, resolved)

        if kind == "ambiguous":
            candidates = resolved.get("candidates") or []
            lines = [f"- {c['name']} — {c['path']}" for c in candidates[:8]]
            return ToolResult.ok(
                "I found more than one match. Which one did you mean?\n"
                + "\n".join(lines),
                {"kind": "ambiguous", "candidates": candidates},
            )

        if kind == "blocked":
            return ToolResult.denied(resolved.get("reason") or "Refusing to open that.")

        return ToolResult.fail(
            resolved.get("reason")
            or f"Nothing named '{request}' was found in the allowed folders."
        )

    @staticmethod
    async def _open_path(path: str, kind: str, resolved: Dict[str, Any]) -> ToolResult:
        """Re-check containment, type and existence, then launch.

        The resolver already answered these questions; asking again immediately
        before the launch is not redundant. A search result can be replaced by
        a symlink between the scan and the open, and "verified at scan time" is
        not the same claim as "verified now".
        """
        if not is_authorized_path(path):
            return ToolResult.denied(
                f"Refusing to open '{path}': it is outside the folders Zaytoon "
                "is allowed to search."
            )
        if not os.path.exists(path):
            return ToolResult.fail(f"'{os.path.basename(path)}' no longer exists.")
        if kind == "file":
            refusal = is_refused_open_target(path)
            if refusal:
                return ToolResult.denied(refusal)
        result = await launch_target(path)
        if result.data is None:
            result.data = {}
        result.data.update(
            {"kind": kind, "path": path, "name": resolved.get("name", "")}
        )
        return result


class FindPaths(BaseTool):
    """Read-only filename/folder search over the authorized roots."""

    permission = "safe"

    def name(self) -> str:
        return "find_paths"

    def aliases(self) -> List[str]:
        return ["search_files_by_name", "locate_files", "find_file"]

    def description(self) -> str:
        return (
            "Search by NAME for files and folders in the Zaytoon workspace and "
            "the user's known folders (Desktop, Documents, Downloads, Pictures, "
            "Music, Videos) plus any ZAYTOON_SEARCH_ROOTS. Returns paths only; "
            "it never reads file contents and never opens anything. A search "
            "finds a name, not a file's text — use search_files for contents "
            "inside the workspace."
        )

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": _prop("string", "Full or partial file/folder name."),
                "kind": _prop(
                    "string",
                    "Restrict results.",
                    enum=["any", "file", "folder"],
                    default="any",
                ),
                "limit": _prop("integer", "Maximum results (1-25).", default=12),
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        query = params.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolValidationError("'query' is required")

    async def execute(
        self, query: str = "", kind: str = "any", limit: int = 12, **_: Any
    ) -> ToolResult:
        wanted = str(kind or "any").strip().lower()
        if wanted not in ("any", "file", "folder"):
            raise ToolValidationError("'kind' must be any, file or folder")
        try:
            requested = 12 if limit is None else int(limit)
        except (TypeError, ValueError):
            requested = 12
        cap = max(1, min(requested, 25))

        loop = asyncio.get_running_loop()
        try:
            results = await loop.run_in_executor(
                None,
                lambda: search_paths(query, limit=cap, max_depth=4),
            )
        except Exception as e:  # a search failure is reported, never fatal
            logger.warning("Filename search failed for %r: %s", query, e)
            return ToolResult.fail(f"Could not search for '{query}': {e}")

        if wanted == "file":
            results = [r for r in results if not r["is_dir"]]
        elif wanted == "folder":
            results = [r for r in results if r["is_dir"]]

        if not results:
            return ToolResult.ok(
                f"No {'folders' if wanted == 'folder' else 'files'} named "
                f"'{query}' were found in the allowed folders.",
                {"query": query, "results": [], "count": 0},
            )

        listing = "\n".join(
            f"{index}. {item['name']} — {item['path']}"
            for index, item in enumerate(results, 1)
        )
        return ToolResult.ok(
            f"{len(results)} match(es) for '{query}':\n{listing}",
            {"query": query, "results": results, "count": len(results)},
        )


class ListApps(BaseTool):
    """List the verified installed applications (read-only)."""

    permission = "safe"

    def name(self) -> str:
        return "list_apps"

    def aliases(self) -> List[str]:
        return ["installed_apps", "list_applications"]

    def description(self) -> str:
        return (
            "List installed applications discovered from the Start Menu, the "
            "App Paths and uninstall registry keys, PATH and ZAYTOON_APP_DIRS. "
            "Read-only: nothing is launched. Each row says whether a launch "
            "target was verified on disk; an unverified row is an application "
            "registration, not proof that it can be opened."
        )

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": _prop("string", "Optional name filter.", default=""),
                "verified_only": _prop(
                    "boolean", "Only rows whose target exists.", default=False
                ),
            },
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        # Nothing required; a malformed optional value is ignored rather than
        # turning "list my apps" into an error.
        return None

    async def execute(
        self, query: str = "", verified_only: bool = False, **_: Any
    ) -> ToolResult:
        from core.targets import normalise_text

        apps = await _installed_apps()
        needle = normalise_text(query)
        rows = [
            app
            for app in apps
            if (not verified_only or bool(getattr(app, "verified", False)))
            and (
                not needle
                or needle
                in normalise_text(
                    getattr(app, "display_name", "") or getattr(app, "name", "")
                )
            )
        ]
        rows.sort(key=lambda a: (not bool(getattr(a, "verified", False)), a.display_name.lower()))
        capped = rows[:200]
        if not capped:
            return ToolResult.ok(
                "No discovered application matches that filter.",
                {"apps": [], "count": 0},
            )
        listing = "\n".join(
            f"- {app.display_name}"
            + ("" if app.verified else "  (registration only, no verified target)")
            for app in capped
        )
        more = "" if len(rows) <= len(capped) else f"\n…and {len(rows) - len(capped)} more"
        return ToolResult.ok(
            f"{len(rows)} application(s):\n{listing}{more}",
            {
                "apps": [_app_dict(app) for app in capped],
                "count": len(rows),
                "shown": len(capped),
            },
        )


class RefreshInventory(BaseTool):
    """Rescan the application inventory (read-only)."""

    permission = "safe"

    def name(self) -> str:
        return "refresh_inventory"

    def aliases(self) -> List[str]:
        return ["rescan_apps", "refresh_apps"]

    def description(self) -> str:
        return (
            "Rescan installed applications from every discovery source and "
            "report what was found, how many targets were verified, and any "
            "source that failed. Read-only: it launches nothing and changes no "
            "settings. Use after installing or uninstalling an application."
        )

    def schema(self) -> Dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    async def execute(self, **_: Any) -> ToolResult:
        started = time.monotonic()
        try:
            apps = await _installed_apps(force=True)
        except Exception as e:
            logger.warning("Inventory refresh failed: %s", e)
            return ToolResult.fail(f"Could not rescan applications: {e}")
        elapsed_ms = int((time.monotonic() - started) * 1000)

        by_source: Dict[str, int] = {}
        for app in apps:
            source = getattr(app, "discovery_source", "") or "unknown"
            by_source[source] = by_source.get(source, 0) + 1
        verified = sum(1 for app in apps if getattr(app, "verified", False))
        label = ", ".join(f"{k}={v}" for k, v in sorted(by_source.items())) or "none"
        output = (
            f"Rescanned {len(apps)} application record(s) in {elapsed_ms} ms; "
            f"{verified} have a target verified on disk. Sources: {label}."
        )
        if not apps:
            output += (
                " Nothing was found — check that this user's Start Menu and the "
                "application registry keys are readable."
            )
        return ToolResult.ok(
            output,
            {
                "total": len(apps),
                "verified": verified,
                "by_source": by_source,
                "elapsed_ms": elapsed_ms,
            },
        )


def build_tools() -> List[BaseTool]:
    return [OpenTarget(), FindPaths(), ListApps(), RefreshInventory()]
