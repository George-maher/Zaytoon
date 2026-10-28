"""Configuration for the Zaytoon desktop shell.

Every value can be overridden with an environment variable, which keeps the
launcher script and PyInstaller build free of hard-coded paths. The defaults
assume the repository layout (``<repo>/backend`` and ``<repo>/desktop``), so a
fresh checkout works with no configuration at all.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_BACKEND_ADDR = "127.0.0.1:8000"
DEFAULT_WS_URL = "ws://127.0.0.1:8000/ws"

#: ``ctrl+space`` toggles the orb from any application; ``alt+z`` switches
#: between the compact orb and the expanded panel.
DEFAULT_TOGGLE_HOTKEY = "ctrl+space"
DEFAULT_EXPAND_HOTKEY = "alt+z"

#: How long to wait for FastAPI to answer after spawning it. Whisper's model
#: load happens lazily on first use, so the API itself comes up quickly.
DEFAULT_BACKEND_TIMEOUT = 30.0


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


#: Env var that overrides repository-root discovery. Kept for support and for
#: running the *frozen* shell against a checkout on purpose; a packaged install
#: no longer needs it — the backend and the production frontend are bundled.
REPO_ROOT_ENV = "ZAYTOON_REPO_ROOT"


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle rather than source."""
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path | None:
    """The PyInstaller extraction/bundle directory, or ``None`` in source mode.

    ``sys._MEIPASS`` points at the directory the bundle's data files land in:
    ``<exe>/_internal`` for an ``--onedir`` build, a per-launch ``%TEMP%`` folder
    for ``--onefile``. It is resolved at *runtime* — never baked at build time —
    because the onedir location moves when the user moves the install, and the
    onedir/onefile choice is the packager's, not the code's.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass) if meipass else None


def _looks_like_repo_root(candidate: Path) -> bool:
    """True when ``candidate`` is plausibly the repository root.

    A directory only counts if it actually contains the backend entry point, so
    a typo in the env var fails with a clear message instead of a confusing
    "cannot find main.py" further down.
    """
    return (candidate / "backend" / "main.py").is_file()


def _is_writable(directory: Path) -> bool:
    """True when ``directory`` exists-and-can-be-written, or can be created.

    Walking up to the nearest existing ancestor is the whole trick: `mkdir -p`
    succeeds only if some ancestor is writable, and asking that question here —
    without creating anything — is what lets a *constructor* stay side-effect
    free while still choosing the right directory.
    """
    target = directory
    while not target.exists():
        parent = target.parent
        if parent == target:
            return False
        target = parent
    if not target.is_dir():
        # Something else already owns the name — a plain file at `<repo>/Config`
        # passes an `os.access(W_OK)` test and then cannot contain a file. A
        # directory that *exists* is only usable if we can write into it.
        return False
    return os.access(target, os.W_OK)


def default_state_dir() -> Path:
    """Where the shell persists state, per platform convention.

    ``ZAYTOON_HOME`` wins. Then, for a *development* checkout that already has a
    ``Config/`` directory with a saved window state in it, that directory keeps
    being used — a launcher must not relocate a user's existing state as a side
    effect of an upgrade. Only a fresh install falls through to the platform dir
    (``%LOCALAPPDATA%\\Zaytoon`` / ``~/Library/Application Support/Zaytoon`` /
    ``$XDG_STATE_HOME/zaytoon``), because that is the one location that is
    guaranteed writable when the app is installed for all users.
    """
    override = os.environ.get("ZAYTOON_HOME")
    if override:
        return Path(override).expanduser()

    # A frozen build never consults the checkout: `__file__` then points into
    # the bundle (`_internal`, or a %TEMP% extraction folder for --onefile),
    # whose "Config" is either read-only (an install under Program Files) or
    # wiped on the next launch. The platform directory below is the only honest
    # answer there — and it is where main() also points ZAYTOON_HOME, so the
    # shell and its backend child agree on one writable home.
    if not is_frozen():
        repo_dir = default_repo_root() / "Config"
        if _is_writable(repo_dir):
            # A checkout, or anywhere the repo-adjacent directory can be written:
            # keep the state where it has always been, so an upgrade does not move
            # a user's saved geometry out from under them.
            return repo_dir

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Zaytoon"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Zaytoon"
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg).expanduser() / "zaytoon"
    return Path.home() / ".local" / "state" / "zaytoon"


def default_repo_root() -> Path:
    """Resolve the application root.

    Resolution order:

    1. ``ZAYTOON_REPO_ROOT`` when it is set and contains ``backend/main.py`` —
       a support/debugging override; a packaged install never needs it.
    2. A **frozen** build: the directory holding the executable. This used to
       walk up from ``sys._MEIPASS`` looking for a checkout-shaped neighbour,
       which silently bound an installed app to whatever repository-like folder
       happened to sit beside it (in practice: the ``dist`` directory the
       build script had copied a whole virtualenv into). The application root
       of an installed app is its own install directory, by definition.
    3. ``<repo>/desktop/app/config.py`` -> ``<repo>`` (the source checkout),
       with ``sys._MEIPASS`` as a last resort for a frozen build run from
       somewhere unusual.
    """
    override = os.getenv(REPO_ROOT_ENV)
    if override:
        candidate = Path(override).expanduser().resolve()
        if _looks_like_repo_root(candidate):
            return candidate
        # Deliberately not fatal: fall through to the other strategies so a
        # stale value degrades to source-checkout discovery rather than
        # preventing the shell from starting.

    if is_frozen():
        executable = getattr(sys, "executable", "")
        if executable:
            return Path(executable).resolve().parent

    here = Path(__file__).resolve()
    # <repo>/desktop/app/config.py -> <repo>
    derived = here.parent.parent.parent
    if _looks_like_repo_root(derived):
        return derived

    # A frozen build whose layout defies both rules above: PyInstaller unpacks
    # the bundle under _MEIPASS, so look there before giving up.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass and _looks_like_repo_root(Path(meipass)):
        return Path(meipass)

    return derived


def default_frontend_dist() -> Path:
    """Where the production frontend build lives.

    1. ``ZAYTOON_FRONTEND_DIST`` — a deliberate override.
    2. Frozen: ``<bundle>/frontend`` — the assets the packaging script ships
       into the executable with ``--add-data "<frontend/dist>;frontend"``.
       Resolved from ``sys._MEIPASS`` at *runtime*, never baked at build time:
       an onedir install may be moved anywhere, and a onefile build extracts to
       a fresh folder per launch. When the bundle's assets are missing the same
       path is still returned, so diagnostics point at the location the running
       application actually looks in rather than at a development checkout that
       does not exist on this machine.
    3. Source: ``<repo>/frontend/dist``.
    """
    override = os.getenv("ZAYTOON_FRONTEND_DIST")
    if override:
        return Path(override).expanduser()
    bundle = bundle_dir()
    if bundle is not None:
        return bundle / "frontend"
    return default_repo_root() / "frontend" / "dist"


@dataclass
class DesktopConfig:
    """Resolved settings for the desktop shell."""

    repo_root: Path = field(default_factory=default_repo_root)

    # --- backend service -------------------------------------------------
    backend_addr: str = field(
        default_factory=lambda: _env("ZAYTOON_BACKEND_ADDR", DEFAULT_BACKEND_ADDR)
    )
    ws_url: str = field(
        default_factory=lambda: _env("ZAYTOON_BACKEND_WS", DEFAULT_WS_URL)
    )
    backend_python: str = field(
        default_factory=lambda: os.getenv("ZAYTOON_BACKEND_PYTHON", "")
    )
    backend_main: str = field(
        default_factory=lambda: os.getenv("ZAYTOON_BACKEND_MAIN", "")
    )
    backend_cwd: str = field(
        default_factory=lambda: os.getenv("ZAYTOON_BACKEND_CWD", "")
    )
    backend_timeout: float = field(
        default_factory=lambda: _env_float("ZAYTOON_BACKEND_TIMEOUT", DEFAULT_BACKEND_TIMEOUT)
    )
    #: When true the shell starts and stops the backend itself. Set false when
    #: a user-managed backend should never be touched.
    manage_backend: bool = field(
        default_factory=lambda: _env_bool("ZAYTOON_MANAGE_BACKEND", True)
    )

    # --- hotkeys ---------------------------------------------------------
    toggle_hotkey: str = field(
        default_factory=lambda: _env("ZAYTOON_TOGGLE_HOTKEY", DEFAULT_TOGGLE_HOTKEY)
    )
    expand_hotkey: str = field(
        default_factory=lambda: _env("ZAYTOON_EXPAND_HOTKEY", DEFAULT_EXPAND_HOTKEY)
    )

    # --- geometry --------------------------------------------------------
    orb_size: int = field(default_factory=lambda: _env_int("ZAYTOON_ORB_SIZE", 320))
    panel_width: int = field(default_factory=lambda: _env_int("ZAYTOON_PANEL_WIDTH", 820))
    panel_height: int = field(default_factory=lambda: _env_int("ZAYTOON_PANEL_HEIGHT", 620))
    screen_margin: int = field(default_factory=lambda: _env_int("ZAYTOON_SCREEN_MARGIN", 24))
    always_on_top: bool = field(
        default_factory=lambda: _env_bool("ZAYTOON_ALWAYS_ON_TOP", True)
    )

    # --- audio -----------------------------------------------------------
    sample_rate: int = field(default_factory=lambda: _env_int("ZAYTOON_SAMPLE_RATE", 16000))
    input_device: str = field(default_factory=lambda: _env("ZAYTOON_INPUT_DEVICE", ""))
    output_device: str = field(default_factory=lambda: _env("ZAYTOON_OUTPUT_DEVICE", ""))
    #: RMS above which a frame counts as speech.
    vad_threshold: float = field(default_factory=lambda: _env_float("ZAYTOON_VAD_THRESHOLD", 0.012))
    #: Consecutive silent frames (~33ms each) before an utterance is closed.
    vad_silence_frames: int = field(
        default_factory=lambda: _env_int("ZAYTOON_VAD_SILENCE_FRAMES", 25)
    )
    #: An utterance shorter than this is discarded as a cough or a knock.
    min_speech_ms: int = field(default_factory=lambda: _env_int("ZAYTOON_MIN_SPEECH_MS", 400))
    #: Hard cap on one utterance, so a stuck VAD cannot record forever.
    max_utterance_s: float = field(default_factory=lambda: _env_float("ZAYTOON_MAX_UTTERANCE_S", 30.0))
    #: The desktop shell does not own the wake-word gate; the backend does.
    send_wake_word: bool = field(
        default_factory=lambda: _env_bool("ZAYTOON_SEND_WAKE_WORD", False)
    )

    # --- WebView host ----------------------------------------------------
    #: When true, connect to the Vite dev server instead of loading the
    #: production build. Useful during development.
    dev_mode: bool = field(
        default_factory=lambda: _env_bool("ZAYTOON_DEV_MODE", False)
    )
    #: Port for the Vite dev server (dev mode only).
    dev_port: int = field(default_factory=lambda: _env_int("ZAYTOON_DEV_PORT", 5173))
    #: Path to the frontend production build directory.
    frontend_dist: Path = field(default_factory=default_frontend_dist)
    #: Default window width.
    window_width: int = field(default_factory=lambda: _env_int("ZAYTOON_WINDOW_WIDTH", 1280))
    #: Default window height.
    window_height: int = field(default_factory=lambda: _env_int("ZAYTOON_WINDOW_HEIGHT", 800))
    #: Minimum window width.
    window_min_width: int = field(default_factory=lambda: _env_int("ZAYTOON_WINDOW_MIN_WIDTH", 800))
    #: Minimum window height.
    window_min_height: int = field(default_factory=lambda: _env_int("ZAYTOON_WINDOW_MIN_HEIGHT", 600))

    # --- state -----------------------------------------------------------
    #: Where `window_state.json` (and anything else the shell must persist) goes.
    #: It used to be ``<repo>/Config``, which is fine in a checkout and wrong in an
    #: installed application: a per-machine Windows install lives under
    #: ``Program Files``, where a standard user cannot write, so the window
    #: geometry silently stopped being remembered (the failure was swallowed into a
    #: log line). The platform conventions below are writable by the user always.
    state_dir: Path = field(default_factory=lambda: default_state_dir())

    #: Seconds between backend health samples. A knob rather than a
    #: ``"pytest" in sys.modules`` check, which made production code branch on the
    #: identity of whatever interpreter happened to import it.
    health_check_interval_s: float = field(
        default_factory=lambda: _env_float("ZAYTOON_HEALTH_INTERVAL", 5.0)
    )

    # --- derived ---------------------------------------------------------
    @property
    def host(self) -> str:
        return self.backend_addr.rsplit(":", 1)[0] if ":" in self.backend_addr else self.backend_addr

    @property
    def port(self) -> int:
        _, _, port = self.backend_addr.rpartition(":")
        try:
            return int(port)
        except ValueError:
            return 8000

    def describe(self) -> str:
        return (
            f"repo={self.repo_root} addr={self.backend_addr} ws={self.ws_url} "
            f"hotkeys=({self.toggle_hotkey}, {self.expand_hotkey}) "
            f"manage_backend={self.manage_backend}"
        )
