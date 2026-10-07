"""Recognising what a project is, from what is on disk.

The point of this is stated in the spec as "Zaytoon should not repeatedly
rediscover the same project information", so the detection is *cheap* and
*shallow*: it stats a fixed list of marker files at the root and reads at most
one small manifest. It never walks the tree. A detection pass that scans a
large monorepo would be slower than the thing it is trying to save.

Every field it produces is a **default**, not a fact. ``test_command`` for a Node
project is read from ``package.json`` when that file is small and parseable, and
otherwise guessed from which lockfile is present — npm, pnpm, yarn. A guess is
labelled as one so the user can correct it, because a confidently wrong
``npm test`` in a pnpm project produces a confusing failure much later.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Zaytoon.Core.Projects.Detect")

#: Marker files, in the order they are checked. The order matters only for the
#: ``kind`` verdict when several are present (a repo with both ``package.json``
#: and ``requirements.txt`` is reported as ``fullstack``).
_MARKERS: List[tuple] = [
    ("package.json", "node"),
    ("pyproject.toml", "python"),
    ("requirements.txt", "python"),
    ("setup.py", "python"),
    ("Pipfile", "python"),
    ("composer.json", "php"),
    ("pom.xml", "java"),
    ("build.gradle", "java"),
    ("build.gradle.kts", "java"),
    ("Cargo.toml", "rust"),
    ("go.mod", "go"),
    ("Gemfile", "ruby"),
    ("CMakeLists.txt", "cpp"),
    ("Makefile", "make"),
]

#: Directories that identify a language's source root. Used for the context
#: block, not for scanning.
_SOURCE_HINTS = {
    "python": ("src", "app", "lib"),
    "node": ("src", "app", "packages"),
}

#: Checked when no language was identified.
COMMON_SOURCE_DIRS = ("src", "app", "lib")

#: Never traverse these. Present so a future recursive helper has one answer.
SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    "target", ".next", ".idea", ".vscode", ".mypy_cache", ".pytest_cache",
    "vendor", ".tox",
})

#: ``package.json`` larger than this is not parsed. A monorepo manifest can be
#: megabytes, and the detection pass is on the request path.
MAX_MANIFEST_BYTES = 200_000


@dataclass
class ProjectProfile:
    """What was detected about one directory."""

    path: str
    kind: str = "unknown"
    language: Optional[str] = None
    package_manager: Optional[str] = None
    framework: Optional[str] = None
    run_command: Optional[str] = None
    test_command: Optional[str] = None
    build_command: Optional[str] = None
    entry_points: List[str] = field(default_factory=list)
    is_git: bool = False
    branch: Optional[str] = None
    markers: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "language": self.language,
            "package_manager": self.package_manager,
            "framework": self.framework,
            "run_command": self.run_command,
            "test_command": self.test_command,
            "build_command": self.build_command,
            "entry_points": list(self.entry_points),
            "is_git": self.is_git,
            "branch": self.branch,
            "markers": list(self.markers),
            "notes": list(self.notes),
        }


def _exists(root: str, name: str) -> bool:
    try:
        return os.path.isfile(os.path.join(root, name))
    except OSError:
        return False


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        if os.path.getsize(path) > MAX_MANIFEST_BYTES:
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _node_package_manager(root: str) -> Optional[str]:
    """npm, pnpm, yarn or bun — decided by lockfile, in that order of certainty."""
    if _exists(root, "pnpm-lock.yaml"):
        return "pnpm"
    if _exists(root, "yarn.lock"):
        return "yarn"
    if _exists(root, "bun.lockb"):
        return "bun"
    if _exists(root, "package-lock.json"):
        return "npm"
    return "npm" if _exists(root, "package.json") else None


def _node_manifest(root: str) -> Dict[str, Any]:
    """``package.json`` as a dict, or ``{}``."""
    return _read_json(os.path.join(root, "package.json")) or {}


def _node_scripts(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """The ``scripts`` block, or ``{}``."""
    scripts = manifest.get("scripts")
    return scripts if isinstance(scripts, dict) else {}


def _node_framework(manifest: Dict[str, Any], root: str) -> Optional[str]:
    """Best-effort framework guess from declared dependencies."""
    deps: Dict[str, Any] = {}
    for key in ("dependencies", "devDependencies"):
        block = manifest.get(key)
        if isinstance(block, dict):
            deps.update(block)
    if "next" in deps:
        return "next"
    if "nuxt" in deps:
        return "nuxt"
    if "vite" in deps and "react" in deps:
        return "react-vite"
    if "vite" in deps and "vue" in deps:
        return "vue-vite"
    if "express" in deps:
        return "express"
    if _exists(root, "vite.config.js") or _exists(root, "vite.config.ts"):
        return "vite"
    return None


def detect(root: str) -> ProjectProfile:
    """Profile a directory. Never raises — an unreadable dir is ``unknown``."""
    profile = ProjectProfile(path=os.path.abspath(root))

    if not os.path.isdir(root):
        profile.notes.append("Not a directory")
        return profile

    for marker, _kind in _MARKERS:
        if _exists(root, marker):
            profile.markers.append(marker)

    kinds = {kind for marker, kind in _MARKERS if _exists(root, marker)}
    if not kinds:
        profile.kind = "unknown"
        profile.notes.append("No recognised project marker found")
    elif len(kinds) == 1:
        profile.kind = next(iter(kinds))
    else:
        profile.kind = "fullstack"
        profile.notes.append(f"Multiple project types: {', '.join(sorted(kinds))}")

    profile.language = _primary_language(kinds)

    if "node" in kinds:
        manifest = _node_manifest(root)
        scripts = _node_scripts(manifest)
        profile.package_manager = _node_package_manager(root)
        profile.framework = _node_framework(manifest, root)
        profile.run_command = _script_command(profile.package_manager, scripts, "dev")
        profile.test_command = _script_command(profile.package_manager, scripts, "test")
        profile.build_command = _script_command(profile.package_manager, scripts, "build")
        if not profile.run_command:
            profile.notes.append("No dev/start script in package.json")
    elif "python" in kinds:
        profile.package_manager = "pip"
        if _exists(root, "pyproject.toml"):
            profile.framework = "poetry" if _has_poetry(root) else "python"
        profile.run_command = "uvicorn main:app --reload" if _exists(root, "main.py") else None
        profile.test_command = "pytest" if (
            _exists(root, "pytest.ini") or _exists(root, "pyproject.toml")
            or os.path.isdir(os.path.join(root, "tests"))
        ) else None
    elif "php" in kinds:
        profile.package_manager = "composer"
        profile.test_command = "php artisan test" if os.path.isdir(os.path.join(root, "tests")) else None
    elif "rust" in kinds:
        profile.package_manager = "cargo"
        profile.run_command = "cargo run"
        profile.test_command = "cargo test"
    elif "go" in kinds:
        profile.package_manager = "go"
        profile.test_command = "go test ./..."
    elif "java" in kinds:
        profile.package_manager = "maven" if _exists(root, "pom.xml") else "gradle"
        profile.test_command = "mvn test" if profile.package_manager == "maven" else "./gradlew test"
    elif "ruby" in kinds:
        profile.package_manager = "bundler"
        profile.test_command = "bundle exec rspec" if os.path.isdir(os.path.join(root, "spec")) else "bundle exec rake test"

    # Reported for any project with a recognised language, and for an
    # unrecognised one too — `src/` is `src/` whether or not a marker file
    # identified the stack, and a directory of source the assistant does not know
    # about is exactly the thing it would otherwise have to go looking for.
    hints = _SOURCE_HINTS.get(profile.language or "", COMMON_SOURCE_DIRS)
    profile.entry_points = [
        name for name in hints if os.path.isdir(os.path.join(root, name))
    ]

    git_dir = os.path.join(root, ".git")
    if os.path.isdir(git_dir) or _exists(root, ".git") or os.path.isfile(git_dir):
        profile.is_git = True
        profile.branch = _current_branch(root)

    return profile


def _has_poetry(root: str) -> bool:
    try:
        path = os.path.join(root, "pyproject.toml")
        if not os.path.isfile(path):
            return False
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return "[tool.poetry" in handle.read(20_000)
    except OSError:
        return False


def _script_command(manager: Optional[str], scripts: Dict[str, Any], name: str) -> Optional[str]:
    """``npm run dev`` — or the pnpm/yarn/bun equivalent, if the script exists.

    The ``run`` subcommand is used for *every* script including ``test``, even
    though ``npm test`` and ``pnpm test`` are both shorter and work. The bare
    form is not uniform across package managers, and ``bun test`` in particular
    runs bun's own built-in test runner rather than the package.json script — so
    a detected default that quietly runs a different test suite than the project
    has is worse than a slightly longer command.
    """
    if name not in scripts:
        return None
    runner = {
        "pnpm": "pnpm",
        "yarn": "yarn",
        "bun": "bun run",
        "npm": "npm run",
    }.get(manager or "npm", "npm run")
    return f"{runner} {name}"


def _current_branch(root: str) -> Optional[str]:
    """Read ``.git/HEAD`` directly.

    Deliberately not ``git rev-parse``: spawning a process on a detection pass
    that runs on the request path is a subprocess per project open, and the
    answer is two lines of file. Falls back to ``None`` on a worktree, where
    HEAD is a path rather than a ref.
    """
    head_path = os.path.join(root, ".git", "HEAD")
    try:
        with open(head_path, "r", encoding="utf-8") as handle:
            head = handle.read().strip()
    except OSError:
        return None
    if head.startswith("ref:"):
        return head.split("/", 2)[-1]
    return head[:12] or None


def _primary_language(kinds) -> Optional[str]:
    """One language for a possibly-multi-language project."""
    for candidate in ("python", "node", "rust", "go", "java", "php", "ruby", "cpp"):
        if candidate in kinds:
            return candidate
    return None


def is_within(root: str, candidate: str) -> bool:
    """True when ``candidate`` is inside ``root``.

    Uses ``commonpath`` rather than ``startswith``, so ``app_evil`` is not
    treated as being inside ``app``. Same rule as ``tools.sandbox``; duplicated
    here only because project registry validation must not depend on the tool
    layer being importable.
    """
    try:
        root_abs = os.path.realpath(os.path.abspath(root))
        cand_abs = os.path.realpath(os.path.abspath(candidate))
    except (OSError, ValueError):
        return False
    if cand_abs == root_abs:
        return True
    try:
        return os.path.commonpath([cand_abs, root_abs]) == root_abs
    except ValueError:  # different drives
        return False


__all__ = ["MAX_MANIFEST_BYTES", "ProjectProfile", "SKIP_DIRS", "detect", "is_within"]