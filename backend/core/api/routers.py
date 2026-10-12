"""The core API routers, mounted by ``api/main.py``."""

from core.api.files_api import router as files_router
from core.api.git_api import router as git_router
from core.api.projects_api import router as projects_router
from core.api.registry_api import (
    providers_router,
    security_router,
    skills_router,
    system_router,
    terminal_router,
)
from core.api.tasks_api import router as tasks_router
from core.api.virtualization_api import router as virtualization_router

__all__ = [
    "files_router",
    "git_router",
    "projects_router",
    "providers_router",
    "security_router",
    "skills_router",
    "system_router",
    "tasks_router",
    "terminal_router",
    "virtualization_router",
]