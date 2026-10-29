"""Local file and application indexer for the Zaytoon desktop shell.

Scans configured roots for files and launchable applications, applying
exclusion patterns so build caches, hidden directories and databases are not
indexed. Results are returned as plain dicts so they cross the pywebview
bridge to the React frontend as JSON.

The indexer is read-only: it never writes to indexed roots, and every path it
returns is checked against the path policy (`app/paths.classify`) before the
bridge hands it to the OS for opening.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import platform
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths as path_policy

logger = logging.getLogger(__name__)

#: Maximum number of items to return from a single search query.
MAX_SEARCH_RESULTS = 100

#: Maximum depth to recurse into directories when indexing.
MAX_INDEX_DEPTH = 6


@dataclass
class IndexedItem:
    """One indexed file or application."""

    id: str
    name: str
    path: str
    kind: str  # "file", "folder", "app"
    mime_type: str | None = None
    size: int | None = None
    modified: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "path": self.path,
            "kind": self.kind,
            "mime_type": self.mime_type,
            "size": self.size,
            "modified": self.modified,
        }


class Indexer:
    """Scans roots and answers search queries against the resulting catalog.

    The catalog is built lazily on first search and can be refreshed with
    :meth:`rebuild`. Roots and exclusions come from :class:`DesktopConfig`.
    """

    def __init__(self, config) -> None:
        self.config = config
        self._items: list[IndexedItem] = []
        self._built = False

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _roots(self) -> list[Path]:
        """Resolve the roots to index: config index_roots plus path-policy roots."""
        roots: list[Path] = []
        for raw in self.config.index_roots:
            try:
                p = Path(raw).expanduser().resolve()
                if p.exists() and p.is_dir():
                    roots.append(p)
            except OSError:
                continue
        # Also index the path-policy roots so files the user can open are
        # discoverable.
        try:
            for root in path_policy.default_roots(self.config):
                if root not in roots:
                    roots.append(root)
        except Exception:  # pragma: no cover - defensive
            logger.debug("could not read path-policy roots", exc_info=True)
        return roots

    def _exclusions(self) -> list[str]:
        """Return the exclusion glob patterns."""
        return list(self.config.index_exclusions)

    def _is_excluded(self, path: Path) -> bool:
        """True when any component of ``path`` matches an exclusion pattern."""
        name = path.name
        for pat in self._exclusions():
            if fnmatch.fnmatch(name, pat):
                return True
        return False

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def rebuild(self) -> int:
        """Re-scan all roots and rebuild the catalog. Returns the item count."""
        items: list[IndexedItem] = []
        seen: set[str] = set()

        for root in self._roots():
            try:
                items.extend(self._scan_root(root, seen))
            except OSError as exc:
                logger.warning("indexer could not scan %s: %s", root, exc)

        # Platform applications (Start Menu, .desktop, .app bundles).
        try:
            items.extend(self._scan_applications(seen))
        except Exception:  # pragma: no cover - platform-specific
            logger.debug("application scan failed", exc_info=True)

        self._items = items
        self._built = True
        logger.info("indexer rebuilt: %d items across %d roots", len(items), len(self._roots()))
        return len(items)

    def _scan_root(self, root: Path, seen: set[str]) -> list[IndexedItem]:
        """Walk a single root, collecting non-excluded files and folders."""
        items: list[IndexedItem] = []
        root_str = str(root)

        try:
            walk = os.walk(root)
        except OSError:
            return items

        for depth, (dirpath, dirnames, filenames) in enumerate(walk):
            if depth >= MAX_INDEX_DEPTH:
                dirnames.clear()
                continue

            # Prune excluded directories in-place so walk does not descend.
            dirnames[:] = [
                d for d in dirnames if not self._is_excluded(Path(dirpath) / d)
            ]

            dir_path = Path(dirpath)
            if dir_path != root:
                dir_id = str(dir_path)
                if dir_id not in seen:
                    seen.add(dir_id)
                    items.append(self._make_item(dir_path, "folder"))

            for filename in filenames:
                if self._is_excluded(Path(filename)):
                    continue
                file_path = dir_path / filename
                try:
                    file_path.resolve(strict=True)
                except (OSError, FileNotFoundError):
                    continue
                file_id = str(file_path)
                if file_id in seen:
                    continue
                seen.add(file_id)
                items.append(self._make_item(file_path, "file"))

        return items

    def _make_item(self, path: Path, kind: str) -> IndexedItem:
        stat = None
        try:
            stat = path.stat()
        except OSError:
            pass

        mime = None
        if kind == "file":
            mime = self._mime_for(path)

        return IndexedItem(
            id=str(path),
            name=path.name,
            path=str(path),
            kind=kind,
            mime_type=mime,
            size=stat.st_size if stat else None,
            modified=stat.st_mtime if stat else None,
        )

    @staticmethod
    def _mime_for(path: Path) -> str | None:
        """Best-effort MIME type from extension (no magic dependency)."""
        ext = path.suffix.lower()
        table = {
            ".txt": "text/plain",
            ".md": "text/markdown",
            ".py": "text/x-python",
            ".js": "application/javascript",
            ".ts": "application/typescript",
            ".jsx": "text/jsx",
            ".tsx": "text/tsx",
            ".json": "application/json",
            ".html": "text/html",
            ".css": "text/css",
            ".pdf": "application/pdf",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".svg": "image/svg+xml",
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav",
            ".mp4": "video/mp4",
            ".zip": "application/zip",
            ".doc": "application/msword",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".xls": "application/vnd.ms-excel",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }
        return table.get(ext)

    # ------------------------------------------------------------------
    # Applications
    # ------------------------------------------------------------------

    def _scan_applications(self, seen: set[str]) -> list[IndexedItem]:
        """Discover launchable applications for the current platform."""
        system = platform.system()
        if system == "Windows":
            return self._scan_windows_apps(seen)
        if system == "Darwin":
            return self._scan_macos_apps(seen)
        return self._scan_linux_apps(seen)

    def _scan_windows_apps(self, seen: set[str]) -> list[IndexedItem]:
        """Scan Windows Start Menu shortcuts and the Apps folder."""
        items: list[IndexedItem] = []
        try:
            import winreg  # type: ignore[import-not-found]
        except ImportError:
            return items

        # Start Menu programs for the current user and all users.
        start_roots: list[Path] = []
        try:
            appdata = os.environ.get("APPDATA", "")
            if appdata:
                start_roots.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
        except Exception:
            pass
        try:
            progdata = os.environ.get("PROGRAMDATA", "")
            if progdata:
                start_roots.append(Path(progdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
        except Exception:
            pass

        for start_root in start_roots:
            if not start_root.exists():
                continue
            for lnk in start_root.rglob("*.lnk"):
                if str(lnk) in seen:
                    continue
                seen.add(str(lnk))
                items.append(
                    IndexedItem(
                        id=str(lnk),
                        name=lnk.stem,
                        path=str(lnk),
                        kind="app",
                        mime_type="application/x-ms-shortcut",
                    )
                )
        return items

    def _scan_macos_apps(self, seen: set[str]) -> list[IndexedItem]:
        """Scan /Applications and ~/Applications for .app bundles."""
        items: list[IndexedItem] = []
        for app_root in (Path("/Applications"), Path.home() / "Applications"):
            if not app_root.exists():
                continue
            for app in app_root.glob("*.app"):
                if str(app) in seen:
                    continue
                seen.add(str(app))
                items.append(
                    IndexedItem(
                        id=str(app),
                        name=app.name[:-4],  # strip .app
                        path=str(app),
                        kind="app",
                        mime_type="application/x-apple-bundle",
                    )
                )
        return items

    def _scan_linux_apps(self, seen: set[str]) -> list[IndexedItem]:
        """Scan .desktop files in standard application directories."""
        items: list[IndexedItem] = []
        desktop_dirs = [
            Path("/usr/share/applications"),
            Path.home() / ".local" / "share" / "applications",
        ]
        for desk_dir in desktop_dirs:
            if not desk_dir.exists():
                continue
            for desktop in desk_dir.glob("*.desktop"):
                if str(desktop) in seen:
                    continue
                seen.add(str(desktop))
                items.append(
                    IndexedItem(
                        id=str(desktop),
                        name=desktop.stem,
                        path=str(desktop),
                        kind="app",
                        mime_type="application/x-desktop",
                    )
                )
        return items

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, query: str, limit: int = MAX_SEARCH_RESULTS) -> list[dict[str, Any]]:
        """Return items whose name matches ``query`` (case-insensitive substring)."""
        if not self._built:
            self.rebuild()
        if not query:
            return [i.to_dict() for i in self._items[:limit]]

        q = query.lower()
        results = [
            i.to_dict()
            for i in self._items
            if q in i.name.lower()
        ][:limit]
        return results

    def all(self, limit: int = MAX_SEARCH_RESULTS) -> list[dict[str, Any]]:
        """Return every indexed item (capped)."""
        if not self._built:
            self.rebuild()
        return [i.to_dict() for i in self._items[:limit]]

    def get(self, item_id: str) -> dict[str, Any] | None:
        """Return one item by id, or None."""
        if not self._built:
            self.rebuild()
        for item in self._items:
            if item.id == item_id:
                return item.to_dict()
        return None
