"""The workspace root: one validation rule, used by the UI *and* at startup.

The endpoint wrote the chosen directory into preferences and its docstring
promised a restart would restore it. Nothing read it back — `config.WORKSPACE_ROOT`
is populated from the environment alone — so every restart silently dropped the
user's chosen project directory and reset the containment boundary of every file
tool to the default. "It did not stick" was the bug; the missing half was the
cause.

There is one rule on purpose. Validating on the write path and re-validating
differently on the read path is how two modules end up disagreeing about what a
path means, which is the failure this codebase already paid for once in
``tools/sandbox``. Both callers go through :func:`resolve_root`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional, Tuple

from config import config

logger = logging.getLogger("Zaytoon.Services.WorkspaceSettings")

#: The preferences row holding the chosen root (written by the UI).
PREF_KEY = "***"


class WorkspaceRejected(ValueError):
    """The path cannot be used as a workspace root, with the reason to show."""


def resolve_root(raw: Optional[str], *, create: bool = True) -> Tuple[Path, str]:
    """Validate ``raw`` and return ``(usable_root, note)``.

    Raises :class:`WorkspaceRejected` instead of reporting the problem in the
    response body, so a caller cannot forget to check.

    ``create`` separates the two callers' intents, which genuinely differ. When
    a *person* picks a folder, an absent one is a normal choice — they meant to
    make it — and the note reports that we did. When the process is *restoring*
    a saved choice, an absent folder means the drive is gone or the project was
    deleted, and recreating an empty one at that path would put the file tools
    inside a workspace that looks like the user's project and is not.

    The root must be **absolute after expansion**. A relative root is not merely
    untidy: it resolves against the process cwd, and the backend must be launched
    from ``backend/``, so ``"X:/no/such/drive"`` on POSIX is not a missing drive
    at all — it is a *relative* path that mkdir happily creates inside the
    source tree, which then becomes the assistant's view of the filesystem.
    Windows rejects it (no such drive) and POSIX accepted it; that asymmetry is
    the bug, and "absolute or refused" is the answer on every platform.

    ``note`` is empty on success, or a short hint the UI can show — a created
    directory is worth mentioning, because "I set it and it was empty" is a
    different situation from "I set it and it has my project in it".
    """
    text = (raw or "").strip()
    if not text:
        raise WorkspaceRejected("No workspace path provided")

    candidate = Path(os.path.expandvars(text)).expanduser()
    if not candidate.is_absolute():
        raise WorkspaceRejected(
            "The workspace path must be absolute "
            f"(received {str(candidate)!r}, which would resolve against "
            f"{os.getcwd()!r})"
        )

    note = ""
    try:
        if candidate.is_symlink() and not candidate.exists():
            # A dangling link is neither "exists" nor "creatable"; mkdir would
            # write *through* it to wherever it points.
            raise WorkspaceRejected(f"{candidate} is a broken symbolic link")
        if candidate.exists():
            if not candidate.is_dir():
                raise WorkspaceRejected(f"{candidate} is not a directory")
        elif create:
            candidate.mkdir(parents=True, exist_ok=True)
            note = "created"
        else:
            raise WorkspaceRejected(f"{candidate} does not exist")
        resolved = candidate.resolve()
    except WorkspaceRejected:
        raise
    except OSError as exc:
        raise WorkspaceRejected(f"Cannot use that directory: {exc}") from exc

    return resolved, note


def apply_root(resolved: Path) -> None:
    """Point every file tool at ``resolved``. In-memory; callers persist."""
    config.WORKSPACE_ROOT = str(resolved)


async def persist(resolved: Path, preferences) -> bool:
    """Save the chosen root. Best-effort, like every other preference write."""
    try:
        await preferences.set(PREF_KEY, str(resolved))
        return True
    except Exception as exc:  # preferences must never fail a user-visible action
        logger.warning("Workspace root not persisted: %s", exc)
        return False


async def restore(preferences) -> Optional[str]:
    """Apply the saved root at startup. Returns a note for the log, or ``None``.

    Never raises. A saved directory that no longer exists — an unplugged drive,
    a deleted project, a machine that has never seen this path — is a warning and
    the configured default, not a backend that refuses to boot: /health and the
    settings screen have to stay reachable so the user can *fix* it there.
    """
    try:
        saved = await preferences.get(PREF_KEY)
    except Exception as exc:
        logger.warning("Could not read the saved workspace root: %s", exc)
        return None
    if not saved or not str(saved).strip():
        return None

    try:
        resolved, _note = resolve_root(str(saved), create=False)
    except WorkspaceRejected as exc:
        logger.warning("Ignored the saved workspace root (%s): %s", saved, exc)
        return f"ignored: {exc}"

    if str(resolved) == str(config.WORKSPACE_ROOT):
        return None
    apply_root(resolved)
    logger.info("Restored workspace root from preferences: %s", resolved)
    return f"restored: {resolved}"


__all__ = [
    "PREF_KEY",
    "WorkspaceRejected",
    "apply_root",
    "persist",
    "resolve_root",
    "restore",
]
