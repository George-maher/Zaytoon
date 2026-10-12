"""Bringing an enabled skill's tools into the one registry.

A skill may ship ``tools/*.py`` modules that define ``build_tools()``.
Three properties make this safe rather than merely useful:

* **Contained import.** The module file is resolved with ``realpath`` and must
  sit inside its own skill directory, so a symlink cannot make a skill load
  code from somewhere else on disk. Module names are namespaced per skill, so
  two skills cannot collide in ``sys.modules``.
* **Reported, never fatal.** A module that raises on import disables that
  module — not the skill, not the app. Every failure lands in the returned
  report and in one log line, because a skill's tool that silently does not
  load is the same failure as a skill that silently does not load.
* **A permission floor.** A third-party tool's declared tier is raised to
  ``confirm`` at registration: a skill cannot self-declare silent access to
  the machine. ``confirm`` stays ``confirm``, ``dangerous`` stays
  ``dangerous`` — only the too-quiet ``safe`` (and any unrecognised value)
  is raised.

A fourth property is checked but not assumed: **a tool may not need more than
the skill declared**. :func:`core.permissions.gaps_for` compares what each tool
requires with the manifest's ``permissions``, the loader reports the difference,
and ``SKILL_STRICT_PERMISSIONS`` turns the report into a refusal. This stays
*inspection with teeth* rather than a sandbox — the loader cannot know what
arbitrary Python does, only what the tool admits to and what its tier implies.

The contract lives in the *directory*, not in the manifest's ``tools`` list:
``README.md`` inside a created skill says modules in ``tools/`` are picked up,
and a list that must be edited in step with the folder is a list that goes
stale. The manifest field remains informational.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from typing import Any, Dict, List, Optional

from common.reporting import redact, safe_error
from core.permissions import gaps_for
from tools.base import BaseTool
from tools.safety import PERMISSION_CONFIRM, PERMISSION_DANGEROUS, PERMISSION_SAFE

logger = logging.getLogger("Zaytoon.Core.Skills.Tools")

#: Same ordering the registry reconciles with; a skill's tool is floored at
#: ``confirm``.
_TIER_RANK = {PERMISSION_SAFE: 0, PERMISSION_CONFIRM: 1, PERMISSION_DANGEROUS: 2}


def _tool_files(skill_dir: str) -> List[str]:
    """Python modules in the skill's ``tools/`` dir, contained and sorted.

    Files starting with ``_`` are skipped (``__init__.py`` and private
    helpers). Containment is checked with ``realpath`` + ``commonpath`` —
    a symlink or junction resolving outside the skill directory is refused.
    """
    tools_dir = os.path.join(skill_dir, "tools")
    if not os.path.isdir(tools_dir):
        return []
    base = os.path.realpath(tools_dir)
    files: List[str] = []
    for entry in sorted(os.listdir(tools_dir)):
        if not entry.endswith(".py") or entry.startswith("_"):
            continue
        path = os.path.realpath(os.path.join(tools_dir, entry))
        try:
            inside = os.path.commonpath([path, base]) == base
        except ValueError:
            inside = False  # different drives can never be contained
        if not inside:
            logger.warning(
                "Skill tool %s resolves outside its skill directory; skipped",
                os.path.basename(path),
            )
            continue
        files.append(path)
    return files


def _load_module(skill: str, path: str) -> Any:
    """Import one file under a per-skill namespaced module name."""
    stem = os.path.splitext(os.path.basename(path))[0]
    module_name = f"zaytoon_skill_{skill}_{stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create an import spec for {os.path.basename(path)}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # A module that failed halfway must not linger half-initialised in
        # sys.modules: the next load (after the user fixes the file) would
        # find the broken entry instead of re-importing.
        sys.modules.pop(module_name, None)
        raise
    return module


def _build(module: Any) -> List[BaseTool]:
    """``build_tools()`` from the module, validated item by item."""
    builder = getattr(module, "build_tools", None)
    if builder is None:
        # The classic authoring mistake: a module that defines tool classes
        # but never the factory would contribute nothing, silently. A plain
        # helper module (no tool classes) is fine.
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and issubclass(value, BaseTool)
                and value is not BaseTool
            ):
                raise AttributeError(
                    "defines a tool class but no build_tools()"
                )
        return []
    tools = builder()
    if not isinstance(tools, (list, tuple)):
        raise TypeError("build_tools() must return a list of tools")
    result: List[BaseTool] = []
    for item in tools:
        if not isinstance(item, BaseTool):
            raise TypeError(
                f"build_tools() returned {type(item).__name__}, expected a BaseTool"
            )
        result.append(item)
    return result


def _declared_permissions(tool: BaseTool) -> List[str]:
    """``tool.permissions()`` (or a plain ``permissions`` attribute), defensively.

    Third-party code, so the shape is not trusted: a method that raises or
    returns a string instead of a list must not become a startup failure. The
    empty answer is the conservative one — it can only report *more* gaps.
    """
    try:
        value = tool.permissions
        if callable(value):
            value = value()
        if isinstance(value, (str, bytes)):
            value = [value]
        return [str(item) for item in (value or [])]
    except Exception:  # noqa: BLE001 - a broken declaration is not a crash
        return []


def _raise_floor(tool: BaseTool) -> None:
    """Never quieter than ``confirm`` — see the module docstring."""
    declared = str(getattr(tool, "permission", PERMISSION_CONFIRM))
    if _TIER_RANK.get(declared, -1) < _TIER_RANK[PERMISSION_CONFIRM]:
        tool.permission = PERMISSION_CONFIRM


def _loaded_map(registry: Any) -> Dict[str, List[str]]:
    """Tools this loader registered on ``registry``, keyed by skill name.

    Kept on the registry rather than in a module global: callers (and tests)
    build a fresh registry per run, and a global map would have one run's tool
    names "removed" from another run's registry. If the registry will not take
    the attribute, tracking degrades to nothing and resync silently does not
    happen — the loader's contract is that it never raises.
    """
    existing = getattr(registry, "_skill_tools", None)
    if isinstance(existing, dict):
        return existing
    created: Dict[str, List[str]] = {}
    try:
        registry._skill_tools = created  # noqa: SLF001 — see docstring
    except Exception:  # noqa: BLE001
        return created
    return created


async def load_skill_tools(
    registry: Any, root: Optional[str] = None
) -> Dict[str, Any]:
    """Register every enabled skill's tools into ``registry``.

    Returns ``{"skills": N, "modules": N, "tools": [names], "removed": [names],
    "errors": [...]}``. Never raises: an unreadable skills directory or a
    failing module is an entry in ``errors``, because the caller is startup and
    the requirement is that a broken skill cannot stop the app from booting.

    This is a **sync**, not a first-time load: a second call replaces what the
    first registered for each skill (an edited module's new code wins), and
    takes the tools of a skill that is no longer enabled — disabled and deleted
    skills must not stay callable until a restart. Only the *skill loader's own*
    tools are replaced: a name that collides with a built-in is still refused,
    because the built-in was never registered by this loader.
    """
    from core.skills.registry import enabled_names, skills_root

    report: Dict[str, Any] = {
        "skills": 0,
        "modules": 0,
        "tools": [],
        "errors": [],
        #: Tools unregistered because their skill was disabled, deleted or is
        #: being reloaded. A sync has to say what it took out, or "nothing
        #: changed" and "everything was replaced" are indistinguishable.
        "removed": [],
        #: Tools whose needs exceed their skill's declaration. Advisory unless
        #: ``SKILL_STRICT_PERMISSIONS``; kept out of ``errors`` in that case so a
        #: caller's "did anything break" check keeps meaning what it used to.
        "policy": [],
    }
    base = root or skills_root()

    try:
        names = await enabled_names()
    except Exception as e:  # noqa: BLE001 — startup path: report, never raise
        safe_error(e, operation="skill.tools.list", component="core.skills.loader")
        report["errors"].append(
            {"skill": "*", "file": "", "error": "could not read enabled skills"}
        )
        return report

    from config import config

    strict = bool(getattr(config, "SKILL_STRICT_PERMISSIONS", False))

    loaded = _loaded_map(registry)
    unregister = getattr(registry, "unregister", None)

    def _forget(skill: str) -> None:
        """Drop the tools this loader previously registered for ``skill``."""
        for tool_name in loaded.pop(skill, []):
            if unregister is None:
                continue
            try:
                unregister(tool_name)
            except Exception:  # noqa: BLE001 — one stale tool is not an error
                continue
            report["removed"].append(f"{skill}.{tool_name}")

    # Disabled or deleted since the last load: its tools leave with it.
    for stale in [s for s in list(loaded) if s not in names]:
        _forget(stale)

    for name in names:
        # Replace, don't collide: these are the skill's *own* previous tools,
        # and without dropping them a reload of an even an unchanged skill
        # would trip `register(replace=False)` on its own name — an edited
        # module's code would never reach the registry.
        _forget(name)
        skill_dir = os.path.join(base, name)
        if not os.path.isdir(skill_dir):
            # Enabled in the database but gone from disk: the user needs to
            # know, not to wonder why the skill never does anything.
            report["errors"].append(
                {"skill": name, "file": "", "error": "skill directory not found"}
            )
            continue
        files = _tool_files(skill_dir)
        if not files:
            continue
        report["skills"] += 1
        # The manifest is the approved policy, so the comparison needs it. A
        # skill that got this far was validated at discovery, so a failure here
        # means the file changed underneath us: report it and fall back to an
        # empty declaration, which can only ever *widen* the reported gaps.
        try:
            from core.skills.manifest import ManifestError, load as load_manifest

            declared = list(load_manifest(skill_dir).permissions)
        except (ManifestError, OSError) as exc:
            # No tools at all, not "assume nothing was declared": with no
            # manifest there is no approved policy to check against, and loading
            # third-party code that nobody has approved since it last validated is
            # the case the check exists for.
            report["errors"].append(
                {"skill": name, "file": "skill.json", "error": redact(str(exc))[:200]}
            )
            continue
        for path in files:
            report["modules"] += 1
            try:
                tools = _build(_load_module(name, path))
            except Exception as e:  # noqa: BLE001 — one bad file, not one bad boot
                safe_error(
                    e,
                    operation=f"skill.{name}.tools",
                    component="core.skills.loader",
                )
                report["errors"].append(
                    {
                        "skill": name,
                        "file": os.path.basename(path),
                        "error": redact(str(e))[:200],
                    }
                )
                continue
            for tool in tools:
                try:
                    # Both halves of the floor. `_raise_floor` makes the tool
                    # honest about itself for anything that reads the object (the
                    # LLM's tool list, an audit row); `floor=` makes the registry
                    # independent of that honesty, so a tool that mutates its own
                    # tier later still cannot run unprompted. See
                    # `ToolRegistry.register`.
                    _raise_floor(tool)
                    gap = sorted(
                        gaps_for(
                            tool.name(),
                            _declared_permissions(tool),
                            declared,
                        )
                    )
                    if gap and strict:
                        raise ValueError(
                            "needs " + ", ".join(gap)
                            + " which this skill's manifest does not declare"
                        )
                    if gap:
                        report["policy"].append(
                            {
                                "skill": name,
                                "tool": tool.name(),
                                "undeclared": gap,
                                "note": "loaded; set SKILL_STRICT_PERMISSIONS to refuse",
                            }
                        )
                    registry.register(tool, replace=False, floor=PERMISSION_CONFIRM)
                except Exception as e:  # noqa: BLE001 - one bad tool, not one bad boot
                    # A skill may not overwrite a built-in (or another skill's
                    # tool): the built-in keeps its name and its behaviour, and
                    # the refusal is reported rather than silently swallowed.
                    report["errors"].append(
                        {
                            "skill": name,
                            "file": os.path.basename(path),
                            "error": redact(str(e))[:200],
                        }
                    )
                else:
                    report["tools"].append(f"{name}.{tool.name()}")
                    loaded.setdefault(name, []).append(tool.name())

    for entry in report["policy"]:
        logger.warning(
            "Skill tool %s.%s needs undeclared permission(s) %s",
            entry["skill"],
            entry["tool"],
            ", ".join(entry["undeclared"]),
        )
    if report["tools"]:
        logger.info(
            "Registered %d skill tool(s): %s",
            len(report["tools"]),
            ", ".join(report["tools"]),
        )
    for entry in report["errors"]:
        logger.warning("Skill tool not loaded: %s", entry)
    return report


__all__ = ["load_skill_tools"]
