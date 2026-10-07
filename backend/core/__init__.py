"""The Zaytoon core runtime.

Everything a task, project, skill, provider or file operation needs lives under
this package. It is **additive**: the agent stack, the tool registry and the
safety gate are not re-implemented here, they are called from here. That is the
whole point — a task and an agent tool must not be able to disagree about what
a path means or what a confirmation means, so there is one of each.

Layout::

    core/
    ├── paths.py         the portable directory layout (one answer per location)
    ├── db.py            SQLite schema + migrations
    ├── secrets.py       credential storage that never reaches a response
    ├── events.py        task.* / tool.* / model.* / permission.* vocabulary
    ├── permissions.py   the named permission vocabulary
    ├── tasks/           task records, lifecycle, dependency graph
    ├── projects/        project detection, registry, model context
    ├── skills/          skill.json packages: discover, install, create
    ├── providers/       provider + model registry and capability routing
    └── workspace/       file service, terminal sessions, and git
"""

__all__ = ["db", "events", "paths", "permissions", "secrets"]