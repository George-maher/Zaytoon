"""Hashes and file structure, computed from bytes already on disk.

The deliberate omissions are the point: no `openssl` subprocess (that is executing
a program, and it also means whatever `openssl` is on PATH decides), no
`cryptography` import (not a dependency of this app, and a skill must not acquire
one quietly), and no writes of any kind — a "fix permissions" convenience here
would make an audit tool the thing that changed the machine.

Because certificates cannot be decoded without one of those two, that check
**reports itself as skipped**. A skipped check printed as "no problems found" is
how an audit tool lies.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any, Dict, List, Optional

from tools.base import BaseTool, ToolResult, ToolValidationError, _prop
from tools.sandbox import resolve_workspace

MAX_HASH_BYTES = 64 * 1024 * 1024
MAX_FILES = 25
_CHUNK = 1024 * 1024

PEM_CERT = re.compile(rb"-----BEGIN CERTIFICATE-----")
MAGIC = {
    b"\x7fELF": "ELF binary",
    b"MZ": "Windows PE executable",
    b"\xcf\xfa\xed\xfe": "Mach-O binary",
    b"\xca\xfe\xba\xbe": "Mach-O fat binary",
    b"PK\x03\x04": "ZIP container",
    b"\x1f\x8b": "gzip stream",
}


def _resolve(path: str) -> str:
    """The workspace-rooted absolute path, or ``""`` when it escapes.

    Two jobs, and the first is easy to get wrong: a relative ``payload.bin`` has
    to be read *from the workspace*, not from the process cwd — resolving for the
    containment check and then opening the original string reads a different file
    than the one that was checked. Containment itself is decided on `realpath`, so
    a symlink aimed at `/etc/shadow` is judged by what it reaches, not its name.
    """
    return resolve_workspace(path)


def _digest(path: str, algorithm: str) -> Optional[str]:
    try:
        hasher = hashlib.new(algorithm)
    except ValueError:
        return None
    try:
        read = 0
        with open(path, "rb") as handle:
            while True:
                block = handle.read(_CHUNK)
                if not block:
                    break
                hasher.update(block)
                read += len(block)
                if read > MAX_HASH_BYTES:
                    return "over-size"
    except OSError:
        return None
    return hasher.hexdigest()


def _sniff(path: str) -> str:
    try:
        with open(path, "rb") as handle:
            head = handle.read(8)
            handle.seek(0)
            if PEM_CERT.search(handle.read(4096)):
                return "PEM certificate (not decoded: no crypto library is available to this skill)"
    except OSError:
        return "unreadable"
    for magic, label in MAGIC.items():
        if head.startswith(magic):
            return label
    return "text" if not any(byte < 9 for byte in head) or b"\x00" not in head else "binary"


class SecurityHashFiles(BaseTool):
    permission = "safe"

    def name(self) -> str:
        return "security_hash_files"

    def description(self) -> str:
        return (
            "Hash files inside the workspace (sha256/sha512/sha1/md5) and, if an "
            "expected digest is given, say whether each matches. Read-only."
        )

    def permissions(self) -> List[str]:
        return ["filesystem.read"]

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "paths": _prop("array", "Files to hash."),
                "algorithm": _prop("string", "sha256 by default.", default="sha256"),
                "expected": _prop("string", "Optional digest to compare against."),
            },
            "required": ["paths"],
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        paths = params.get("paths")
        if not isinstance(paths, (list, tuple)) or not paths:
            raise ToolValidationError("'paths' must be a non-empty list of files")

    async def execute(self, paths: Optional[List[str]] = None, algorithm: str = "sha256",
                      expected: str = "", **_: Any) -> ToolResult:
        lines: List[str] = []
        rows: List[Dict[str, Any]] = []
        refused: List[str] = []
        for raw in list(paths or [])[:MAX_FILES]:
            given = str(raw)
            path = _resolve(given)
            if not path:
                refused.append(given)
                continue
            if not os.path.isfile(path):
                lines.append(f"{given}  not a file")
                continue
            size = os.path.getsize(path)
            value = _digest(path, str(algorithm).strip().lower() or "sha256")
            if value is None:
                lines.append(f"{path}  unreadable or unknown algorithm {algorithm!r}")
                continue
            if value == "over-size":
                lines.append(f"{path}  refused: larger than {MAX_HASH_BYTES} bytes")
                continue
            verdict = ""
            if expected:
                want = str(expected).strip().lower()
                verdict = " MATCH" if want in (value, f"{algorithm}:{value}") else " MISMATCH"
            lines.append(f"{given}  {algorithm}:{value}  {size} bytes{verdict}")
            rows.append({"path": given, "resolved": path, "algorithm": algorithm,
                         "digest": value,
                         "bytes": size, "verdict": verdict.strip().lower()})

        head = f"{len(rows)} file(s) hashed"
        if refused:
            head += f"; {len(refused)} refused as outside the workspace"
        if len(list(paths or [])) > MAX_FILES:
            head += f"; only the first {MAX_FILES} were used"
        return ToolResult.ok(
            head + ("\n" + "\n".join(lines) if lines else ""),
            {"files": rows, "refused": refused},
        )


class SecurityInspectPaths(BaseTool):
    permission = "safe"

    def name(self) -> str:
        return "security_inspect_paths"

    def description(self) -> str:
        return (
            "Report mode and structure for files inside the workspace: octal "
            "permissions, world-writable, setuid/setgid, executable, symlink "
            "targets that escape the workspace, and a content-type sniff. Changes "
            "nothing."
        )

    def permissions(self) -> List[str]:
        return ["filesystem.read"]

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {"paths": _prop("array", "Files or folders to inspect.")},
            "required": ["paths"],
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        paths = params.get("paths")
        if not isinstance(paths, (list, tuple)) or not paths:
            raise ToolValidationError("'paths' must be a non-empty list")

    async def execute(self, paths: Optional[List[str]] = None, **_: Any) -> ToolResult:
        lines: List[str] = []
        rows: List[Dict[str, Any]] = []
        for raw in list(paths or [])[:MAX_FILES]:
            given = str(raw)
            path = _resolve(given)
            if not path:
                lines.append(f"{given}  REFUSED: outside the workspace")
                continue
            try:
                info = os.stat(path)
            except OSError as exc:
                lines.append(f"{given}  unreadable ({exc.strerror or exc})")
                continue
            mode = info.st_mode
            flags: List[str] = []
            if mode & 0o002:
                flags.append("world-writable")
            if mode & 0o02000:
                flags.append("setgid")
            if mode & 0o04000:
                flags.append("setuid")
            if mode & 0o111:
                flags.append("executable")
            is_link = os.path.islink(path)
            if is_link:
                target = os.path.realpath(path)
                if not _resolve(target):
                    flags.append("symlink-leaves-workspace")
            kind = "symlink" if is_link else ("directory" if os.path.isdir(path) else _sniff(path))
            notes = f"  {'; '.join(flags)}" if flags else ""
            lines.append(
                f"{given}  {oct(mode & 0o7777)}  {kind}  {info.st_size}B{notes}"
            )
            rows.append({"path": given, "resolved": path,
                         "mode": oct(mode & 0o7777), "flags": flags,
                         "kind": kind, "bytes": info.st_size,
                         "owner_uid": info.st_uid, "mtime": int(info.st_mtime)})

        risky = sum(1 for row in rows if row["flags"])
        return ToolResult.ok(
            f"{len(rows)} path(s) inspected, {risky} with a flag worth a look\n"
            + "\n".join(lines)
            + "\nA flag is a question, not a verdict: world-writable is normal for a "
              "temp directory and alarming for an SSH key.",
            {"paths": rows},
        )


def build_tools() -> List[BaseTool]:
    return [SecurityHashFiles(), SecurityInspectPaths()]
