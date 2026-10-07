"""The portable layout: one place that knows where Zaytoon keeps things.

The spec asks for a portable directory layout (§32) — application code in one
place, everything the user owns somewhere else, so an **application update
replaces ``Application/`` and nothing else**. That is only true if the code can
find the other directories, which means the answer to "where is the database?"
has to be a function rather than an ``os.getcwd()`` sprinkled across five
modules. This is that function.

Resolution order for every location:

1. an explicit environment variable (``CORE_DB_PATH``, ``ZAYTOON_SKILLS_DIR`` …)
   — always wins, because a test or a deliberate override is more specific than
   a layout;
2. ``ZAYTOON_HOME``/``Data``, ``ZAYTOON_HOME``/``Config`` … — the portable
   layout, when the app was installed by ``install.sh``/``install.ps1``;
3. ``<backend>/data`` etc. — a development checkout, which is where the memory
   and audit databases already live.

The ordering is the whole design. Without (2), the installer's ``Data/``
directory would exist and never be used, and the user's projects would be
scattered between the checkout and the install directory depending on which
module asked first — which is how a "portable" install ends up needing the
source tree.

**This module never creates anything.** A caller that needs a directory to
exist asks for it explicitly (``ensure``). Resolution staying side-effect-free
matters because it is called from import-time health checks, and an import
should not touch the filesystem.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

#: The directories a portable install provides. `Application` is deliberately
#: absent: it is where the code lives, not where the app looks for things, and
#: listing it here would suggest the other seven are peers of it.
LAYOUT = ("Config", "Data", "Models", "Skills", "Workspaces", "Logs", "Cache")


def zaytoon_home() -> Optional[Path]:
    """The install root, or ``None`` when this is a development checkout.

    ``ZAYTOON_HOME`` is set by the generated launchers. It is read from the
    **environment** rather than inferred, because inferring it means searching
    upwards from the code — which finds the source tree on a development
    machine and the install directory on a deployed one, which is right, but
    fails silently the first time someone moves a directory.
    """
    raw = os.getenv("ZAYTOON_HOME")
    if not raw:
        return None
    try:
        return Path(os.path.expandvars(raw)).expanduser().resolve()
    except (OSError, RuntimeError):  # pragma: no cover - unresolvable path
        return None


def _portable(name: str) -> Optional[Path]:
    """``ZAYTOON_HOME/<name>`` when a portable install is in play."""
    home = zaytoon_home()
    return None if home is None else home / name


def _dev_data_dir() -> Path:
    """The checkout's ``backend/data`` — where the databases already live."""
    # ``backend/core/paths.py`` -> ``backend``. Not ``os.getcwd()``: the backend
    # must be launched from ``backend/`` (AGENTS.md), so a cwd-based path makes
    # the data directory depend on *how* it was started, and running the desktop
    # shell — which spawns the backend with its own cwd — silently writes to a
    # second, empty data directory.
    return Path(__file__).resolve().parent.parent / "data"


def data_dir() -> Path:
    """Where databases and other mutable state live."""
    override = os.getenv("ZAYTOON_DATA_DIR")
    if override:
        return Path(os.path.expandvars(override)).expanduser()
    portable = _portable("Data")
    return portable if portable is not None else _dev_data_dir()


def config_dir() -> Optional[Path]:
    """Where ``.env`` and the encrypted secrets file live, or ``None``.

    ``None`` for a checkout, which has no separate configuration directory: its
    ``.env`` sits beside ``config.py`` and is loaded by ``load_dotenv()`` there.
    Returning ``backend/config`` (which is where this sentence would go if it
    were a directory) would be a plausible-looking wrong answer.
    """
    override = os.getenv("ZAYTOON_CONFIG_DIR")
    if override:
        return Path(os.path.expandvars(override)).expanduser()
    return _portable("Config")


def logs_dir() -> Path:
    """Where log files go, when a deployment writes any."""
    portable = _portable("Logs")
    return portable if portable is not None else data_dir() / "logs"


def models_dir() -> Path:
    """Where locally downloaded model artefacts live.

    Not created on import, and not created by Zaytoon either: a multi-gigabyte
    download is a decision the user makes, not something an app does behind
    their back. The installer says so explicitly.
    """
    portable = _portable("Models")
    return portable if portable is not None else _dev_data_dir() / "models"


def workspaces_dir() -> Optional[Path]:
    """Default workspace root for a portable install, or ``None``.

    ``None`` for a development checkout is the meaningful answer, not a
    convenience: the checkout's default is ``~/ZaytoonWorkspace``
    (``config.DEFAULT_WORKSPACE_DIRNAME``), which deliberately keeps the
    assistant out of its own source tree. Returning a *path* here would silently
    change that default — and this function is consulted precisely when
    ``WORKSPACE_ROOT`` is unset, so a bogus answer is one nobody would notice.
    """
    return _portable("Workspaces")


def skills_dir() -> Optional[Path]:
    """Default skills root for a portable install, or ``None``.

    Outside the workspace, unlike the checkout default: installed skills are
    meant to be available to *every* project, and a portable layout keeps them
    with the install rather than in whichever project was opened first. A
    checkout has no such directory, so ``None`` — ``registry.skills_root()``
    falls back to the workspace.
    """
    return _portable("Skills")





def ensure(path: Path) -> Path:
    """Create ``path`` if needed and return it. The only writing function here."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # pragma: no cover - surfaced by the caller
        raise RuntimeError(f"Cannot create {path}: {exc.strerror or exc}") from exc
    return path


def describe() -> dict:
    """The resolved layout, for ``GET /health``.

    Reported so a user can answer "where is it keeping my data?" without reading
    the source. Paths are the only thing here — a health endpoint that echoed
    credentials to be helpful would be a worse bug than not having one.
    """
    home = zaytoon_home()
    config_path = config_dir()
    return {
        "portable": home is not None,
        "home": str(home) if home else None,
        # `None` rather than a guessed path: "there is no separate config
        # directory" is a real answer, and a plausible-looking wrong one would
        # send someone looking for a file that does not exist.
        "config": str(config_path) if config_path else None,
        "data": str(data_dir()),
        "logs": str(logs_dir()),
        "models": str(models_dir()),
        "workspaces": str(workspaces_dir()) if workspaces_dir() else None,
        "skills": str(skills_dir()) if skills_dir() else None,
    }