"""The context block: what the model is told about a project.

This exists because of a specific failure. Without it, "run the tests" on a
React project either runs the wrong command or asks the user, and either way the
model has rediscovered the same three facts on every single turn. The
detection pass is cheap but *repeated* detection is not free, and worse, an
inconsistent answer ("npm test" once, "pytest" the next time, because the model
guessed) is worse than no answer.

So the block is assembled once, from the stored profile, and it is **explicit
about what is known versus inferred**. A model that is told "test command:
pytest (inferred)" can say so; one told "test command: pytest" will state it as
fact and be wrong when the guess was wrong. That distinction is the difference
between a correct answer and a confidently incorrect one, and it is why every
field carries a confidence marker.

Kept short on purpose. A long context block crowds out the user's actual
request in a small model's window — which is the failure mode the engine's
honesty rules already exist to fight.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from core.projects.detect import detect

#: Hard cap. A context block that does not fit in a 3B model's window is not a
#: context block.
MAX_CHARS = 1200

#: Entry-point directories reported, so the model does not have to guess where
#: the source is.
MAX_ENTRY_POINTS = 4


def build_context(
    project: Optional[Dict[str, Any]],
    *,
    active_file: Optional[str] = None,
    extra_notes: Optional[list] = None,
) -> str:
    """Render a project profile as a compact context block.

    Returns ``""`` for no project, which is correct: a turn with no project open
    should not carry a header saying so.
    """
    if not project:
        return ""

    path = project.get("path") or ""
    lines = [f"PROJECT: {project.get('name') or os.path.basename(path)} ({path})"]

    detected = bool(project.get("kind")) and project.get("kind") != "unknown"
    kind = project.get("kind") or "unknown"
    lines.append(f"Type: {kind}{'' if detected else ' (not recognised — ask before assuming)'}")

    facts = []
    if project.get("language"):
        facts.append(f"language={project['language']}")
    if project.get("framework"):
        facts.append(f"framework={project['framework']}")
    if project.get("package_manager"):
        facts.append(f"package manager={project['package_manager']}")
    if project.get("is_git"):
        branch = project.get("branch") or "unknown branch"
        facts.append(f"git repo on {branch}")
    if facts:
        lines.append("Detected: " + ", ".join(facts))

    commands = []
    for label, key in (("run", "run_command"), ("test", "test_command"), ("build", "build_command")):
        value = project.get(key)
        if value:
            commands.append(f"  {label}: {value}")
    if commands:
        lines.append("Commands (detected defaults — correct them if they look wrong):")
        lines.extend(commands)

    entry_points = [e for e in (project.get("entry_points") or []) if e][:MAX_ENTRY_POINTS]
    if entry_points:
        lines.append("Source directories: " + ", ".join(entry_points))

    if active_file:
        lines.append(f"Currently open: {active_file}")

    notes = [n for n in (project.get("notes") or []) if n]
    if notes:
        lines.append("Notes: " + "; ".join(notes))

    block = "\n".join(lines)
    if len(block) > MAX_CHARS:
        block = block[: MAX_CHARS - 3] + "..."
    return block


def context_from_path(path: str, *, active_file: Optional[str] = None) -> str:
    """Detect on the fly and build the block. For paths not yet registered."""
    return build_context(detect(path).to_dict(), active_file=active_file)


def workspace_summary(root: str, limit: int = 12) -> str:
    """A one-line description of the workspace root.

    Used when no project is open, so "what is here" is answerable without
    registering anything.
    """
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return ""
    visible = [e for e in entries if not e.startswith(".")][:limit]
    if not visible:
        return f"Workspace {root} is empty."
    more = len(entries) - len(visible)
    suffix = f" (+{more} more)" if more > 0 else ""
    return f"Workspace {root} contains: {', '.join(visible)}{suffix}"


__all__ = ["MAX_CHARS", "build_context", "context_from_path", "workspace_summary"]