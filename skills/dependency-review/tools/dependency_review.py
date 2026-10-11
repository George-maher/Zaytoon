"""Supply-chain review of the files a project already has.

This is deliberately **not** `npm audit`. An advisory database needs the network,
and the network is outside this skill's policy. What can be answered offline is
still the majority of real incidents: *where does this package come from, is the
version pinned, and does installing it run code?* A plain `http://` registry, a
`git+ssh://…` dependency at a branch name, and a `postinstall` script are each a
finding a registry lookup would not improve.

Pinned: only the shapes above, each with the file and line attached.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from tools.base import BaseTool, ToolResult, _prop
from tools.sandbox import resolve_workspace

MAX_FILE_BYTES = 8 * 1024 * 1024

#: Lockfiles and manifests worth reading, in the order a reader wants them.
TARGETS: Tuple[str, ...] = (
    "package.json", "package-lock.json", "yarn.lock",
    "requirements.txt", "pyproject.toml", "Pipfile", "Pipfile.lock",
    "go.mod", "Gemfile", "composer.json",
)

NPM_HOOKS = ("preinstall", "install", "postinstall", "prepare", "preuninstall")

_UNPINNED_PY = re.compile(r"^\s*[A-Za-z0-9._\-]+\s*(?:\[[^\]]*\])?\s*(?:[<>=!~]=|[<>])?\s*$")
_GIT_SOURCE = re.compile(r"(?:git\+|github:|git://|ssh://)", re.I)
_PLAIN_HTTP = re.compile(r"\bhttp://", re.I)
_STAR_RANGE = re.compile(r"(?:^\*|:?\s*latest\b|^\s*x$)", re.I)


def _read(path: str) -> Optional[str]:
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _find_lines(text: str, pattern) -> List[Tuple[int, str]]:
    out = []
    for number, line in enumerate(text.splitlines(), 1):
        if pattern.search(line):
            out.append((number, line.strip()[:160]))
    return out


def _review_npm(name: str, text: str) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    try:
        data = json.loads(text)
    except ValueError:
        for number, line in _find_lines(text, re.compile(r"(postinstall|preinstall)")):
            findings.append({"file": name, "line": number, "label": "unparsable-json",
                             "detail": line})
        return findings

    scripts = data.get("scripts") or {}
    if isinstance(scripts, dict):
        for hook in NPM_HOOKS:
            if hook in scripts:
                findings.append({
                    "file": name, "label": "install-script-hook",
                    "detail": f"{hook}: {str(scripts[hook])[:120]}",
                    "why": "npm runs this during install; a compromised dependency "
                           "gets code execution before anything is verified",
                })

    packages = data.get("packages") or data.get("dependencies") or {}
    if isinstance(packages, dict):
        for spec, meta in list(packages.items())[:400]:
            fields = meta if isinstance(meta, dict) else {"version": str(meta)}
            resolved = str(fields.get("resolved") or fields.get("version") or "")
            label = None
            if _GIT_SOURCE.search(resolved):
                label = "git-source"
            elif resolved.startswith("http://"):
                label = "plain-http-registry"
            elif not str(fields.get("integrity") or "") and "lockfileVersion" in data and spec != "":
                label = "no-integrity-hash"
            if label:
                findings.append({
                    "file": name, "label": label,
                    "detail": f"{spec} → {resolved[:120]}",
                })
    return findings


def _review_python(name: str, text: str) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    for number, line in _find_lines(text, _GIT_SOURCE):
        findings.append({"file": name, "line": number, "label": "git-source", "detail": line})
    for number, line in _find_lines(text, _PLAIN_HTTP):
        findings.append({"file": name, "line": number, "label": "plain-http-registry",
                         "detail": line})
    if name.startswith("requirements"):
        unpinned = 0
        for number, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "-")):
                continue
            if _UNPINNED_PY.match(stripped) and "==" not in stripped and "@ " not in stripped:
                unpinned += 1
                if unpinned <= 12:
                    findings.append({"file": name, "line": number, "label": "unpinned-version",
                                     "detail": stripped[:120]})
        if unpinned > 12:
            findings.append({"file": name, "label": "unpinned-version",
                             "detail": f"…{unpinned - 12} more unpinned requirement(s)"})
    return findings


def _review_generic(name: str, text: str) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    for number, line in _find_lines(text, _GIT_SOURCE):
        findings.append({"file": name, "line": number, "label": "git-source", "detail": line})
    for number, line in _find_lines(text, _PLAIN_HTTP):
        findings.append({"file": name, "line": number, "label": "plain-http-registry",
                         "detail": line})
    if name == "go.mod":
        for number, line in _find_lines(text, re.compile(r"//\s*replace\b")):
            findings.append({"file": name, "line": number, "label": "replace-directive",
                             "detail": line,
                             "why": "a `replace` can point a module at a fork "
                                    "nobody reviewed"})
    return findings


class SecurityReviewDependencies(BaseTool):
    permission = "safe"

    def name(self) -> str:
        return "security_review_dependencies"

    def description(self) -> str:
        return (
            "Read the project's lockfiles/manifests and report unpinned versions, "
            "git or plain-http sources, missing integrity hashes and npm install "
            "hooks. Offline: no registry is contacted, no package manager is run, "
            "and no advisory database is consulted."
        )

    def permissions(self) -> List[str]:
        return ["filesystem.read"]

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": _prop("string", "Project root to inspect. Defaults to the workspace."),
            },
            "additionalProperties": False,
        }

    async def execute(self, path: str = "", **_: Any) -> ToolResult:
        root = resolve_workspace(path or ".")
        if not root or not os.path.isdir(root):
            return ToolResult.fail(
                "That path is outside the workspace or missing, and this skill does "
                "not read outside the workspace."
            )

        found: Dict[str, str] = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d not in {"node_modules", ".git", "__pycache__", ".venv", "dist", "build"}
            ]
            for name in TARGETS:
                if name in filenames:
                    full = os.path.join(dirpath, name)
                    rel = os.path.relpath(full, root)
                    text = _read(full)
                    if text is not None:
                        found[rel] = text
            if len(found) >= 12:
                break

        if not found:
            return ToolResult.ok(
                "No lockfiles or manifests found in the workspace, so there is "
                "nothing to review. This means 'no files', not 'no problems'.",
                {"files_read": 0, "findings": []},
            )

        findings: List[Dict[str, Any]] = []
        for rel, text in found.items():
            base = os.path.basename(rel)
            if base.startswith(("package", "yarn")):
                findings.extend(_review_npm(rel, text))
            elif base.startswith(("requirements", "pyproject", "Pipfile")):
                findings.extend(_review_python(rel, text))
            else:
                findings.extend(_review_generic(rel, text))

        counts: Dict[str, int] = {}
        for item in findings:
            counts[item["label"]] = counts.get(item["label"], 0) + 1

        lines = [
            f"{item['file']}"
            + (f":{item['line']}" if item.get("line") else "")
            + f"  {item['label']}  {item['detail'][:140]}"
            + (f"  — {item['why']}" if item.get("why") else "")
            for item in findings[:60]
        ]
        if len(findings) > 60:
            lines.append(f"(…{len(findings) - 60} more finding(s) not listed)")
        lines.append(
            "Offline review: versions were not checked against any advisory "
            "database, and an unknown package is not by itself a bad one."
        )
        return ToolResult.ok(
            f"{len(findings)} finding(s) in {len(found)} file(s): "
            + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no risky shapes found")
            + "\n" + "\n".join(lines),
            {"files_read": len(found), "findings": findings[:60], "counts": counts},
        )


def build_tools() -> List[BaseTool]:
    return [SecurityReviewDependencies()]
