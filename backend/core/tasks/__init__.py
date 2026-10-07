"""Tasks: durable records, a validated lifecycle, and dependency graphs."""

from core.tasks.graph import (
    CycleError,
    GraphContext,
    Node,
    NodeResult,
    TaskGraph,
    UnknownDependencyError,
    build_graph,
)
from core.tasks.manager import TaskManager, get_manager
from core.tasks.models import (
    IllegalTransition,
    Task,
    TaskStatus,
    TaskStep,
    TaskStepStatus,
)

__all__ = [
    "CycleError",
    "GraphContext",
    "IllegalTransition",
    "Node",
    "NodeResult",
    "Task",
    "TaskGraph",
    "TaskManager",
    "TaskStatus",
    "TaskStep",
    "TaskStepStatus",
    "UnknownDependencyError",
    "build_graph",
    "get_manager",
]