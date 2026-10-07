"""Projects: detection, registry, and the context block fed to the model."""

from core.projects.context import build_context, context_from_path, workspace_summary
from core.projects.detect import ProjectProfile, detect, is_within
from core.projects.store import (
    ProjectRejected,
    get,
    get_by_path,
    list_projects,
    project_id_for,
    recent,
    register,
    remove,
    set_pinned,
    update_commands,
    validate_path,
)

__all__ = [
    "ProjectProfile",
    "ProjectRejected",
    "build_context",
    "context_from_path",
    "detect",
    "get",
    "get_by_path",
    "is_within",
    "list_projects",
    "project_id_for",
    "recent",
    "register",
    "remove",
    "set_pinned",
    "update_commands",
    "validate_path",
    "workspace_summary",
]