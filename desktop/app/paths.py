"""Which paths the desktop shell is allowed to hand to the operating system.

The bridge exposes `open_path`, `reveal_in_os` and `open_url` to whatever script
runs inside the WebView, and all three end in an OS call that *executes* things:
`os.startfile` on Windows runs an `.exe`/`.bat`/`.lnk` exactly as a double-click
does, `open` on macOS launches an `.app`, and `xdg-open` on Linux will run a
`.desktop` file or a shell script in the user's default handler. Before this
module, the only check was `Path.exists()` — so any code with access to
`window.pywebview.api` (an XSS in the HUD, a malicious string rendered as HTML,
a compromised skill's output) could point one of those at a file it had persuaded
the agent to write, and get it executed as the user.

The rule is therefore: a path must *exist inside a granted root*, and it must not
be something the OS would run rather than open. The roots come from the backend's
own workspace (`GET /system` → `workspace`) so there is one policy, not two; the
desktop's state directory is included because the HUD reveals log files there.
`ZAYTOON_OPEN_ROOTS` widens the list explicitly, and an explicitly widened grant
is the point — the default is not.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

#: Windows: extensions `os.startfile` executes instead of opening. `.js`, `.vbs`
#: and `.lnk` are in here deliberately — they are documents to a user and
#: programs to the shell.
EXECUTABLE_SUFFIXES = frozenset(
    {
        ".exe", ".bat", ".cmd", ".com", ".scr", ".msi", ".msix", ".pif", ".hta",
        ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".lnk",
        ".jar", ".reg", ".cpl", ".application",
    }
)

#: POSIX types the desktop environment runs when a file manager or `xdg-open` is
#: handed them, whatever the execute bit says. A `.desktop` file is an application
#: launcher with an `Exec=` line in it.
POSIX_UNSAFE_SUFFIXES = frozenset({".desktop", ".application", ".desktopfile"})

#: Schemes `open_url` may hand to the default browser. Anything else is a
#: protocol handler (`file:`, `search-ms:`, a custom scheme an installer
#: registered) and is not what "open this link in the browser" means.
ALLOWED_URL_SCHEMES = frozenset({"http", "https"})

_ENV_ROOTS = "ZAYTOON_OPEN_ROOTS"
_TIMEOUT_S = 0.6


def _roots_from_env() -> list[Path]:
    raw = os.environ.get(_ENV_ROOTS, "")
    return [Path(part).expanduser() for part in raw.split(os.pathsep) if part.strip()]


def _backend_workspace(config) -> Path | None:
    """Ask the running backend where the workspace is, so the two agree.

    Cached on the config object: this runs per bridge call and the answer cannot
    change without a restart. A backend that is down is not a reason to refuse
    every reveal, so failure is `None` and the caller falls back to the defaults.
    """
    cached = getattr(config, "_workspace_cache", "unset")
    if cached != "unset":
        return cached
    root: Path | None = None
    try:
        url = f"http://{config.backend_addr}/system"
        with urllib.request.urlopen(url, timeout=_TIMEOUT_S) as response:  # noqa: S310 - literal 127.0.0.1 host:port
            payload = json.loads(response.read().decode("utf-8", "replace"))
        workspace = payload.get("workspace") or (payload.get("config") or {}).get("workspace")
        if workspace:
            root = Path(str(workspace)).expanduser()
    except Exception as exc:  # noqa: BLE001 - a probe, never a failure
        logger.debug("could not read the backend workspace root: %s", exc)
    try:
        config._workspace_cache = root
    except Exception:  # pragma: no cover - frozen dataclasses, if any
        pass
    return root


def default_roots(config) -> list[Path]:
    """Directories the shell may open or reveal inside of."""
    override = _roots_from_env()
    if override:
        return [path for path in override if path.exists()] or [override[0]]

    roots: list[Path] = []
    workspace = _backend_workspace(config)
    if workspace is not None:
        roots.append(workspace)
    # The fallbacks mirror `backend/config.py::_default_workspace_root` (a
    # portable install keeps `Workspaces/` beside `Application/`, a checkout uses
    # `~/ZaytoonWorkspace`) because the desktop cannot import the backend's
    # config: different interpreter, different venv. `tests/test_bridge_paths.py`
    # pins the two against each other so the copy cannot drift.
    repo_root = Path(getattr(config, "repo_root", ".")).expanduser()
    for candidate in (
        Path.home() / "ZaytoonWorkspace",
        repo_root.parent / "Workspaces",
        repo_root / "ZaytoonWorkspace",
    ):
        if candidate not in roots:
            roots.append(candidate)
    state_dir = getattr(config, "state_dir", None)
    if state_dir is not None:
        roots.append(Path(state_dir))
    return [root for root in roots] or [Path.home() / "ZaytoonWorkspace"]


def classify(path: str, config) -> tuple[bool, str]:
    """`(allowed, reason)` for a path handed to the OS.

    `reason` is for the log and the developer, never for the page: the bridge's
    public contract is a bool, and keeping it that way means a refusal cannot leak
    whether some other user's file exists.
    """
    if not path or not str(path).strip():
        return False, "empty path"
    raw = Path(str(path)).expanduser()
    try:
        # resolve() before containment, never after: `workspace/link -> /etc/passwd`
        # is inside the root by string and outside it by meaning.
        target = raw.resolve()
    except OSError as exc:  # pragma: no cover - unreadable parents
        return False, f"cannot resolve: {exc}"

    if not target.exists():
        return False, "path does not exist"

    roots = default_roots(config)
    allowed_root = None
    for root in roots:
        try:
            resolved_root = root.resolve()
        except OSError:
            continue
        if target == resolved_root or resolved_root in target.parents:
            allowed_root = resolved_root
            break
    if allowed_root is None:
        return False, (
            f"outside every granted root ({', '.join(str(r) for r in roots)}); "
            f"set {_ENV_ROOTS} to widen it"
        )

    # A *bundle* on macOS (.app) is a directory the user means to launch from the
    # file manager, and `reveal` never executes anything; only plain files are
    # checked for "would the OS run this".
    if target.is_file():
        if os.name == "nt":
            if target.suffix.lower() in EXECUTABLE_SUFFIXES:
                return False, f"refusing to open the executable type {target.suffix}"
        else:
            if target.suffix.lower() in POSIX_UNSAFE_SUFFIXES:
                return False, "refusing to open a file the desktop environment would launch"
            if os.access(target, os.X_OK):
                return False, "refusing to open a file the OS would execute"

    return True, str(allowed_root)


def classify_url(url: str) -> tuple[bool, str]:
    """`(allowed, reason)` for a URL handed to the default browser."""
    from urllib.parse import urlsplit

    if not url or not str(url).strip():
        return False, "empty url"
    try:
        parts = urlsplit(str(url).strip())
    except ValueError as exc:
        return False, f"unparseable url: {exc}"
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_URL_SCHEMES:
        return False, f"scheme {scheme or '(none)'} is not http/https"
    if not parts.netloc:
        return False, "no host"
    return True, scheme


def describe_platform() -> str:
    return sys.platform
