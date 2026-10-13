"""Skills: packaged capability, as a directory with a ``skill.json``."""

from core.skills.manifest import (
    ManifestError,
    SkillManifest,
    default_manifest,
    load,
    read_instructions,
    validate,
)
from core.skills.registry import (
    MAX_ENABLED,
    SkillExists,
    SkillLimitExceeded,
    SkillOutsideRoot,
    build_skill_context,
    create,
    discover,
    enabled_names,
    install,
    list_skills,
    remove,
    set_enabled,
    skill_detail,
    skills_root,
    sync_to_db,
)
from core.skills.tool_loader import load_skill_tools, test_skill

__all__ = [
    "MAX_ENABLED",
    "ManifestError",
    "SkillExists",
    "SkillLimitExceeded",
    "SkillOutsideRoot",
    "SkillManifest",
    "build_skill_context",
    "create",
    "default_manifest",
    "discover",
    "enabled_names",
    "install",
    "list_skills",
    "load",
    "load_skill_tools",
    "read_instructions",
    "remove",
    "set_enabled",
    "skill_detail",
    "skills_root",
    "sync_to_db",
    "test_skill",
    "validate",
]