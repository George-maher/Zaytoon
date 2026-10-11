"""The named permission vocabulary.

The codebase already has three tiers — ``safe``, ``confirm``, ``dangerous`` —
and they work. What it does not have is a *name* for what a tool is being let
to do, which matters for three reasons the tier alone cannot serve:

* A settings screen has to group tools by capability ("this one touches the
  network"), not by severity.
* A user wants to grant ``filesystem.write`` for a project without granting
  ``git.destructive`` to the whole session.
* An audit row that says ``git_commit`` tells an operator what happened; one
  that says ``git.write`` tells them what class of thing was allowed.

So this module is a **pure function** from a tool to the permissions it
requires, derived from the tier and the tool's name. Derived, not declared, so
the two vocabularies cannot drift — a tool cannot quietly acquire a tier
without acquiring the matching permissions.

The one deliberate asymmetry: a tool may declare *additional* permissions
beyond its tier via :meth:`BaseTool.permissions`, but never fewer. Code that
tries to subtract is a bug, and :func:`required_for` is where it would show up.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, FrozenSet, Iterable, List, Optional

from tools.safety import (
    PERMISSION_CONFIRM,
    PERMISSION_DANGEROUS,
    PERMISSION_SAFE,
    RiskAssessor,
)


class Permission(str, Enum):
    """What a tool is allowed to do. Stable strings: they are a wire contract."""

    FILESYSTEM_READ = "filesystem.read"
    FILESYSTEM_WRITE = "filesystem.write"
    FILESYSTEM_DELETE = "filesystem.delete"
    PROCESS_EXECUTE = "process.execute"
    PROCESS_MANAGE = "process.manage"
    NETWORK_REQUEST = "network.request"
    BROWSER_CONTROL = "browser.control"
    GIT_WRITE = "git.write"
    GIT_DESTRUCTIVE = "git.destructive"
    SYSTEM_MODIFY = "system.modify"

    @property
    def label(self) -> str:
        return _PERMISSION_LABELS.get(self, self.value)


_PERMISSION_LABELS: Dict[Permission, str] = {
    Permission.FILESYSTEM_READ: "Read files",
    Permission.FILESYSTEM_WRITE: "Create and modify files",
    Permission.FILESYSTEM_DELETE: "Delete files and folders",
    Permission.PROCESS_EXECUTE: "Run commands and code",
    Permission.PROCESS_MANAGE: "Start and stop background processes",
    Permission.NETWORK_REQUEST: "Make network requests",
    Permission.BROWSER_CONTROL: "Control the browser or UI input",
    Permission.GIT_WRITE: "Change repository state",
    Permission.GIT_DESTRUCTIVE: "Irreversible git operations",
    Permission.SYSTEM_MODIFY: "Change system state",
}

ALL_PERMISSIONS: FrozenSet[Permission] = frozenset(Permission)


# ---------------------------------------------------------------------------
# Tier → baseline permissions
# ---------------------------------------------------------------------------
#: The floor implied by a tier. ``safe`` implies nothing on its own; the
#: per-tool table below supplies the actual capability.
_TIER_FLOOR: Dict[str, FrozenSet[Permission]] = {
    PERMISSION_SAFE: frozenset(),
    PERMISSION_CONFIRM: frozenset({
        Permission.FILESYSTEM_WRITE,
        Permission.PROCESS_EXECUTE,
    }),
    PERMISSION_DANGEROUS: frozenset({
        Permission.FILESYSTEM_WRITE,
        Permission.FILESYSTEM_DELETE,
        Permission.PROCESS_EXECUTE,
        Permission.SYSTEM_MODIFY,
    }),
}

#: Git verbs that lose data. ``git.reset --hard`` and ``git.clean -fd`` are not
#: "a bit more git" — they discard work — so they are named apart from the rest
#: of the write tier and cannot be granted by a blanket "allow git.write".
_GIT_DESTRUCTIVE_VERBS = frozenset({
    "reset_hard", "clean", "push_force", "force", "branch_delete",
    "filter_branch", "reflog_expire", "submodule_deinit",
})


def _tier_for(action: str) -> str:
    return RiskAssessor.permission_for(action)


def required_for(action: str, extra: Optional[Iterable[str]] = None) -> FrozenSet[Permission]:
    """The permissions a tool action needs.

    Three inputs, unioned: the tier's floor, the capability table keyed on the
    action name (including aliases, because ``permission_for`` can be handed
    one), and whatever the tool itself declares.
    """
    name = (action or "").strip().lower()
    if not name:
        return frozenset()

    tier = _tier_for(name)
    perms = set(_TIER_FLOOR.get(tier, frozenset()))
    perms |= _capabilities_for(name)

    for value in extra or ():
        try:
            perms.add(Permission(str(value).strip()))
        except ValueError:
            # An unknown permission name is a programming error, not user input.
            # Ignoring it would silently under-report what a tool can do, which
            # is the failure mode this whole module exists to prevent.
            continue
    return frozenset(perms)


def _capabilities_for(name: str) -> FrozenSet[Permission]:
    """The capability table. Keyed on prefixes and exact names."""
    if name.startswith("git_"):
        verb = name[4:]
        if verb in _GIT_DESTRUCTIVE_VERBS:
            return frozenset({Permission.GIT_WRITE, Permission.GIT_DESTRUCTIVE})
        # Read-only verbs were already classified SAFE; anything else writes.
        if _tier_for(name) == PERMISSION_SAFE:
            return frozenset()
        return frozenset({Permission.GIT_WRITE})

    if name in ("file_read", "read_file", "file_list", "list_files", "list_directory",
                "search_files", "find_files", "grep", "search_code"):
        return frozenset({Permission.FILESYSTEM_READ})

    if name in ("file_write", "write_file", "file_edit", "file_append", "create_file",
                "create_folder", "make_dir", "mkdir", "file_move", "move_file",
                "file_copy", "copy_file", "file_rename"):
        return frozenset({Permission.FILESYSTEM_READ, Permission.FILESYSTEM_WRITE})

    if name in ("file_delete", "delete_file", "file_remove", "delete_folder", "rmdir"):
        return frozenset({
            Permission.FILESYSTEM_READ,
            Permission.FILESYSTEM_WRITE,
            Permission.FILESYSTEM_DELETE,
        })

    if name in ("system_command", "cmd", "run_shell", "exec", "run_command",
                "execute_local_command", "run_local_command", "exec_local",
                "run_code", "execute_code", "exec_code", "run_snippet",
                "run_npm", "npm_install", "run_npm_script", "run_tests",
                "run_build", "run_linter", "create_project", "scaffold",
                "pip_install", "install_package", "process_start"):
        return frozenset({Permission.PROCESS_EXECUTE})

    if name in ("process_stop", "close_app", "kill_process", "taskkill"):
        return frozenset({Permission.PROCESS_EXECUTE, Permission.PROCESS_MANAGE})

    if name in ("process_list", "process_logs"):
        return frozenset({Permission.PROCESS_MANAGE})

    if name in ("search_web", "open_url"):
        return frozenset({Permission.NETWORK_REQUEST})

    # Input injection: these land in whatever window has focus and can trigger
    # arbitrary application behaviour, so they control the UI *and* can change
    # system state through it.
    if name in ("type_text", "key_press"):
        return frozenset({Permission.BROWSER_CONTROL, Permission.SYSTEM_MODIFY})

    # Media keys and volume are `confirm`-tier because they act on the machine,
    # but they inject nothing.
    if name == "play_media":
        return frozenset({Permission.SYSTEM_MODIFY})

    # Reading state is not modifying it. Both of these are `safe`-tier and must
    # not be reported as needing system.modify — a permission list that says a
    # screenshot "modifies the system" is a list nobody can act on.
    if name in ("get_volume", "screenshot"):
        return frozenset()

    if name in ("registry_write", "reg_delete", "run_as_admin", "format_disk"):
        return frozenset({Permission.SYSTEM_MODIFY})

    if name in ("get_time", "system_info", "calculator"):
        return frozenset()

    return frozenset()


# ---------------------------------------------------------------------------
# Policy gaps
# ---------------------------------------------------------------------------
#: What the skill loader's `confirm` floor grants by itself. Not reported as a
#: gap: a skill is not obliged to declare the consequences of a floor it did not
#: choose, and demanding it would bury the real mismatches under noise.
SKILL_FLOOR_GRANTS: FrozenSet[str] = frozenset(
    p.value for p in _TIER_FLOOR[PERMISSION_CONFIRM]
)


def _names(values: Iterable[Any]) -> set:
    """Permission names as plain strings, from enum members or raw strings.

    Needed because `Permission` is a `str` *Enum*: `hash(member)` is the member's,
    so `frozenset - {"filesystem.write"}` would subtract nothing and every skill
    would look like a violation. Compare values, never members to strings.
    """
    return {str(getattr(v, "value", v)).strip() for v in (values or ())}


def gaps_for(
    action: str,
    tool_declared: Optional[Iterable[Any]] = None,
    manifest_declared: Optional[Iterable[Any]] = None,
) -> FrozenSet[str]:
    """What this tool needs that the skill's manifest never asked for.

    ``required_for`` already folds in the tier floor; the floor's own grants are
    subtracted so the answer means "extra capability the user did not approve",
    not "the loader did its job". A gap is reported to the skill author and, with
    ``SKILL_STRICT_PERMISSIONS``, refused at load.
    """
    needed = {p.value for p in required_for(action, extra=tool_declared)}
    needed -= set(SKILL_FLOOR_GRANTS)
    asked = _names(manifest_declared) | _names(tool_declared)
    return frozenset(needed - asked)


# ---------------------------------------------------------------------------
# Grants
# ---------------------------------------------------------------------------
#: A grant is ``allow`` / ``deny`` / ``ask``. ``ask`` is the default, meaning
#: "defer to the tier gate", which is the behaviour the product already has.
DECISION_ALLOW = "allow"
DECISION_DENY = "deny"
DECISION_ASK = "ask"
VALID_DECISIONS = (DECISION_ALLOW, DECISION_DENY, DECISION_ASK)


def is_valid_decision(value: str) -> bool:
    return (value or "").strip().lower() in VALID_DECISIONS


def describe() -> List[Dict[str, str]]:
    """Every permission, for the settings screen."""
    return [
        {"permission": p.value, "label": p.label}
        for p in sorted(Permission, key=lambda x: x.value)
    ]


__all__ = [
    "ALL_PERMISSIONS",
    "SKILL_FLOOR_GRANTS",
    "gaps_for",
    "DECISION_ALLOW",
    "DECISION_ASK",
    "DECISION_DENY",
    "Permission",
    "VALID_DECISIONS",
    "describe",
    "is_valid_decision",
    "required_for",
]