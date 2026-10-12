"""The ``skill.json`` manifest.

A skill is a directory:

```text
skills/laravel/
├── skill.json
├── instructions.md
├── tools/           (optional — Python modules registered into the tool registry)
└── resources/       (optional — reference material)
```

The manifest is small on purpose. A skill that needs a rich schema is a plugin
system, and a plugin system is what we said we would not build. Five fields
carry the whole contract:

```json
{
  "name": "laravel",
  "version": "1.0.0",
  "description": "Laravel project conventions and security review",
  "instructions": "instructions.md",
  "tools": [],
  "permissions": ["filesystem.write", "process.execute"],
  "enabled": true
}
```

``permissions`` is what the skill *asks for*, declared from the same
named vocabulary the tool tiers derive from (see :mod:`core.permissions`) —
which is why the count is not written down here: it grew when virtualization
added its three grants, and a number in prose is a number that will be wrong.
It is inspection, not enforcement: the loader's tier floor (``confirm``) is
what actually restrains a skill's tools, because a manifest is a claim and
the floor is a fact. An unknown name is rejected here so the declaration
cannot rot into something nothing reads.

Validation is strict and the errors name the field, because a skill that fails
to load with "invalid manifest" is a skill nobody can debug. Every failure is
reported as :class:`ManifestError` carrying the path and the reason.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.permissions import ALL_PERMISSIONS

#: Manifest filename. Fixed, so a skill directory is self-describing.
MANIFEST_NAME = "skill.json"

#: Cap on the manifest file. A skill manifest is a few hundred bytes; anything
#: larger is a mistake or an attack.
MAX_MANIFEST_BYTES = 64 * 1024

#: Cap on instructions. These are injected into the model's context, so an
#: unbounded file would crowd out the user's actual request.
MAX_INSTRUCTIONS_CHARS = 8000

INSTRUCTIONS_NAME = "instructions.md"


class ManifestError(ValueError):
    """A ``skill.json`` is missing, unreadable, or invalid."""


@dataclass
class SkillManifest:
    """A validated skill manifest."""

    name: str
    path: str
    description: str = ""
    version: str = "0.0.0"
    instructions_file: str = INSTRUCTIONS_NAME
    tools: List[str] = field(default_factory=list)
    permissions: List[str] = field(default_factory=list)
    enabled: bool = True
    tags: List[str] = field(default_factory=list)
    author: str = ""

    @property
    def instructions_path(self) -> str:
        return os.path.join(self.path, self.instructions_file)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "description": self.description,
            "version": self.version,
            "instructions": self.instructions_file,
            "tools": list(self.tools),
            "permissions": list(self.permissions),
            "enabled": self.enabled,
            "tags": list(self.tags),
            "author": self.author,
            "has_instructions": os.path.isfile(self.instructions_path),
        }


def validate(data: Any, path: str) -> SkillManifest:
    """Turn parsed JSON into a manifest, or raise with a precise reason."""
    if not isinstance(data, dict):
        raise ManifestError(f"{path}: manifest must be a JSON object")

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ManifestError(f"{path}: 'name' is required and must be a non-empty string")
    name = name.strip()

    if not _safe_name(name):
        raise ManifestError(
            f"{path}: 'name' must contain only letters, digits, '-' and '_' "
            f"(got {name!r}). It is used as a directory name."
        )

    version = data.get("version", "0.0.0")
    if not isinstance(version, str) or not version.strip():
        raise ManifestError(f"{path}: 'version' must be a non-empty string")

    description = data.get("description", "")
    if not isinstance(description, str):
        raise ManifestError(f"{path}: 'description' must be a string")

    instructions_file = data.get("instructions", INSTRUCTIONS_NAME)
    if not isinstance(instructions_file, str) or not instructions_file.strip():
        raise ManifestError(f"{path}: 'instructions' must be a filename")
    # A skill must not be able to point its instructions at, say,
    # ../../../../etc/passwd and have it injected into the model's context.
    #
    # Both separators are checked explicitly: on Windows `os.path.sep` is a
    # backslash, so splitting a manifest that writes `../../secrets.md` with
    # forward slashes yields a single element and the traversal walks straight
    # through. A skill manifest is user-authored and could be written on any
    # platform, so it must be read as either.
    if (
        os.path.isabs(instructions_file)
        or instructions_file.startswith(("/", "\\"))
        or ":" in instructions_file  # C:\... or ADS
        or ".." in re.split(r"[\\/]", instructions_file)
    ):
        raise ManifestError(
            f"{path}: 'instructions' must be a relative path inside the skill "
            f"directory (got {instructions_file!r})"
        )

    raw_tools = data.get("tools", [])
    if not isinstance(raw_tools, list) or any(not isinstance(t, str) for t in raw_tools):
        raise ManifestError(f"{path}: 'tools' must be a list of filenames")

    raw_permissions = data.get("permissions", [])
    if not isinstance(raw_permissions, list) or any(
        not isinstance(p, str) for p in raw_permissions
    ):
        raise ManifestError(f"{path}: 'permissions' must be a list of permission names")
    known_permissions = sorted(p.value for p in ALL_PERMISSIONS)
    permissions: List[str] = []
    for raw in raw_permissions:
        name_permission = raw.strip()
        if not name_permission:
            continue
        if name_permission not in known_permissions:
            raise ManifestError(
                f"{path}: unknown permission {name_permission!r} in 'permissions'. "
                f"Known: {', '.join(known_permissions)}"
            )
        if name_permission not in permissions:
            permissions.append(name_permission)

    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ManifestError(f"{path}: 'enabled' must be true or false")

    tags = data.get("tags", [])
    if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
        raise ManifestError(f"{path}: 'tags' must be a list of strings")

    return SkillManifest(
        name=name,
        path=path,
        description=description.strip(),
        version=str(version).strip(),
        instructions_file=instructions_file,
        tools=[t for t in raw_tools if t.strip()],
        permissions=permissions,
        enabled=enabled,
        tags=[t for t in tags if t.strip()],
        author=str(data.get("author", "") or ""),
    )


def _safe_name(name: str) -> bool:
    return all(c.isalnum() or c in "-_" for c in name) and not name.startswith("-")


def load(skill_dir: str) -> SkillManifest:
    """Read and validate the manifest in ``skill_dir``."""
    manifest_path = os.path.join(skill_dir, MANIFEST_NAME)
    if not os.path.isfile(manifest_path):
        raise ManifestError(f"No {MANIFEST_NAME} in {skill_dir}")
    try:
        size = os.path.getsize(manifest_path)
    except OSError as exc:
        raise ManifestError(f"Cannot read {manifest_path}: {exc.strerror}") from exc
    if size > MAX_MANIFEST_BYTES:
        raise ManifestError(
            f"{manifest_path} is {size} bytes; the limit is {MAX_MANIFEST_BYTES}"
        )
    try:
        with open(manifest_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{manifest_path} is not valid JSON: {exc.msg} (line {exc.lineno})") from exc
    except OSError as exc:
        raise ManifestError(f"Cannot read {manifest_path}: {exc.strerror}") from exc
    return validate(data, skill_dir)


def read_instructions(manifest: SkillManifest) -> str:
    """The skill's instructions, capped. ``""`` when there is no file.

    Capped rather than rejected: a long document is more useful truncated than
    not at all, and the cap is reported in the text so the model knows it is
    looking at a fragment.
    """
    path = manifest.instructions_path
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(MAX_INSTRUCTIONS_CHARS + 1)
    except OSError:
        return ""
    if len(text) > MAX_INSTRUCTIONS_CHARS:
        return text[:MAX_INSTRUCTIONS_CHARS] + "\n[... truncated ...]"
    return text


def default_manifest(name: str, description: str = "") -> Dict[str, Any]:
    """The manifest :func:`core.skills.registry.create` writes."""
    return {
        "name": name,
        "version": "1.0.0",
        "description": description or f"Custom Zaytoon skill: {name}",
        "instructions": INSTRUCTIONS_NAME,
        "tools": [],
        "permissions": [],
        "enabled": True,
        "tags": [],
    }


__all__ = [
    "INSTRUCTIONS_NAME",
    "MANIFEST_NAME",
    "MAX_INSTRUCTIONS_CHARS",
    "MAX_MANIFEST_BYTES",
    "ManifestError",
    "SkillManifest",
    "default_manifest",
    "load",
    "read_instructions",
    "validate",
]