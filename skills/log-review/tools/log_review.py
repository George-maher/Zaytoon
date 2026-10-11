"""Passive log review: read what is on disk, report what it says.

Two rules hold this together. Nothing here can *act*: the only file opens are for
reading (the shipped-skill test fails the build otherwise), and every finding is a
pattern match with a file and a line attached, so the model quotes evidence rather
than concluding "you were breached". Secrets are masked in the output — a log
review that pastes the key it found has turned an audit into a leak.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from tools.base import BaseTool, ToolResult, ToolValidationError, _prop
from tools.sandbox import resolve_workspace

#: Skip files bigger than this; a log review is not a disk-full event.
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_FILES = 25
MAX_MATCHES_PER_LABEL = 12

#: label -> pattern. Ordered: the first match on a line wins, so the specific
#: secret shapes sit before the generic ones.
PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = tuple(
    (label, re.compile(rx, re.I))
    for label, rx in (
        ("aws-access-key", r"\b(AKIA[0-9A-Z]{16})\b"),
        ("private-key-block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        ("jwt", r"\b(eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,})\b"),
        ("bearer-token", r"\b[Bb]earer\s+([A-Za-z0-9\-._~+/]{16,})"),
        ("password-in-url", r"[?&]?(?:password|passwd|pwd)=([^&\s\"']{3,})"),
        ("basic-auth-in-url", r"://[A-Za-z0-9_.\-]+:([^@/\s]+)@"),
        ("auth-failure", r"(authentication failure|failed password|invalid user|"
                         r"auth failed|401 unauthorized|permission denied)"),
        ("crash-signature", r"(traceback \(most recent call last\)|panic:|fatal error|"
                            r"segmentation fault|core dumped)"),
        ("tls-problem", r"(certificate verify failed|SSL:|handshake failure|"
                        r"certificate has expired)"),
        ("injection-shape", r"(UNION SELECT |' OR 1=1|<script>alert|\.\./\.\./\.\./)"),
    )
)


def _mask(value: str) -> str:
    """Keep the shape, lose the secret: `AKIA…W5LO`."""
    value = value.strip()
    if len(value) <= 10:
        return value[:2] + "…"
    return f"{value[:4]}…{value[-4:]}"


def _candidates(root: str, given: List[str]) -> List[str]:
    if given:
        out = []
        for raw in given:
            resolved = resolve_workspace(raw, root)
            if resolved and os.path.isfile(resolved):
                out.append(resolved)
        return out
    found: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in {".git", "node_modules", "__pycache__", ".venv", "dist", "build"}
        ]
        for name in filenames:
            lowered = name.lower()
            if lowered.endswith((".log", ".jsonl", ".out", ".err")) or lowered in (
                "syslog", "auth.log", "messages",
            ):
                found.append(os.path.join(dirpath, name))
    return sorted(found)[:MAX_FILES]


def _scan_file(path: str) -> List[Dict[str, Any]]:
    hits: List[Dict[str, Any]] = []
    per_label: Dict[str, int] = {}
    try:
        size = os.path.getsize(path)
        if size > MAX_FILE_BYTES:
            return [{"skipped": f"larger than {MAX_FILE_BYTES} bytes", "file": path}]
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle, 1):
                for label, pattern in PATTERNS:
                    match = pattern.search(line)
                    if not match:
                        continue
                    if per_label.get(label, 0) >= MAX_MATCHES_PER_LABEL:
                        continue
                    per_label[label] = per_label.get(label, 0) + 1
                    secret = match.group(1) if match.groups() else ""
                    excerpt = _mask(secret) if secret else line.strip()[:160]
                    hits.append({
                        "file": path, "line": number, "label": label,
                        "excerpt": excerpt,
                    })
                    break
    except OSError as exc:
        return [{"unreadable": str(exc), "file": path}]
    return hits


class SecurityScanLogs(BaseTool):
    #: Honest capability: reading. The skill loader still floors every skill tool
    #: at `confirm` (see `ToolRegistry.register`) — an exception list for
    #: repo-vetted skills is exactly the hole a floor must not have.
    permission = "safe"

    def name(self) -> str:
        return "security_scan_logs"

    def description(self) -> str:
        return (
            "Scan log files in the workspace for auth failures, exposed secrets, "
            "crash and injection shapes. Read-only, no network, no execution. "
            "Returns file:line evidence; secrets are masked."
        )

    def permissions(self) -> List[str]:
        return ["filesystem.read"]

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "paths": _prop(
                    "array", "Log files to read. Omit to find *.log/*.jsonl/syslog "
                    "in the workspace.",
                ),
                "only": _prop("string", "Restrict to one label, e.g. 'aws-access-key'."),
            },
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        paths = params.get("paths")
        if paths is not None and not isinstance(paths, (list, tuple)):
            raise ToolValidationError("'paths' must be a list of paths")
        only = params.get("only")
        if only is not None and str(only).strip() and str(only).strip() not in dict(PATTERNS):
            raise ToolValidationError(
                "'only' must be one of: " + ", ".join(label for label, _ in PATTERNS)
            )

    async def execute(self, paths: Optional[List[str]] = None, only: str = "", **_: Any) -> ToolResult:
        root = resolve_workspace(".") or os.getcwd()
        targets = _candidates(root, list(paths or []))
        if not targets:
            return ToolResult.ok(
                "No log files found in the workspace. Name a file explicitly if it "
                "lives elsewhere you are allowed to read.",
                {"files_read": 0, "findings": []},
            )

        wanted = (only or "").strip()
        hits: List[Dict[str, Any]] = []
        notes: List[str] = []
        for path in targets:
            for hit in _scan_file(path):
                if "skipped" in hit or "unreadable" in hit:
                    notes.append(f"{os.path.basename(path)}: {hit.get('skipped') or hit.get('unreadable')}")
                    continue
                if wanted and hit["label"] != wanted:
                    continue
                hits.append(hit)

        counts: Dict[str, int] = {}
        for hit in hits:
            counts[hit["label"]] = counts.get(hit["label"], 0) + 1

        lines = []
        for hit in hits[:80]:
            rel = os.path.relpath(hit["file"], root)
            lines.append(f"{rel}:{hit['line']}  {hit['label']}  {hit['excerpt']}")
        summary = (
            f"{len(hits)} finding(s) across {len(targets)} file(s): "
            + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing matched")
        )
        if len(hits) > 80:
            lines.append(f"(…{len(hits) - 80} more findings not listed)")
        if notes:
            lines.append("Notes: " + "; ".join(notes[:5]))
        lines.append(
            "Pattern matches, not conclusions: a line here says the text appeared, "
            "not that anything succeeded."
        )
        return ToolResult.ok(
            summary + "\n" + "\n".join(lines),
            {"files_read": len(targets), "findings": hits[:80], "counts": counts},
        )


def build_tools() -> List[BaseTool]:
    return [SecurityScanLogs()]
