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


#: Env var that overrides repository-root discovery. Required for a PyInstaller
#: build, because ``__file__`` then points into a temporary extraction folder
#: rather than at the checkout, and ``scripts/build_exe.py`` instructs the user
#: to set exactly this variable.
REPO_ROOT_ENV = "ZAYTOON_REPO_ROOT"


def _looks_like_repo_root(candidate: Path) -> bool:
    """True when ``candidate`` is plausibly the repository root.

    A directory only counts if it actually contains the backend entry point, so
    a typo in the env var fails with a clear message instead of a confusing
    "cannot find main.py" further down.
    """
    return (candidate / "backend" / "main.py").is_file()


def default_repo_root() -> Path:
    """Resolve the repository root.

    Resolution order:

    1. ``ZAYTOON_REPO_ROOT`` when it is set and contains ``backend/main.py``.
    2. ``<repo>/desktop/app/config.py`` -> ``<repo>`` (the source checkout).
    3. ``sys._MEIPASS`` for a frozen build, which PyInstaller sets to the
       extraction directory.
    """
    override = os.getenv(REPO_ROOT_ENV)
    if override:
        candidate = Path(override).expanduser().resolve()
        if _looks_like_repo_root(candidate):
            return candidate
        # Deliberately not fatal: fall through to the other strategies so a
        # stale value degrades to source-checkout discovery rather than
        # preventing the shell from starting.

    here = Path(__file__).resolve()
    # <repo>/desktop/app/config.py -> <repo>
    derived = here.parent.parent.parent
    if _looks_like_repo_root(derived):
        return derived

    # Frozen build: PyInstaller unpacks the bundle under _MEIPASS, and the
    # bundle may have been dropped next to a checkout.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        for base in (Path(meipass), Path(meipass).parent, Path(meipass).parent.parent):
            if _looks_like_repo_root(base):
                return base

    return derived


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
    frontend_dist: Path = field(
        default_factory=lambda: Path(_env("ZAYTOON_FRONTEND_DIST", str(default_repo_root() / "frontend" / "dist")))
    )
    #: Default window width.
    window_width: int = field(default_factory=lambda: _env_int("ZAYTOON_WINDOW_WIDTH", 1280))
    #: Default window height.
    window_height: int = field(default_factory=lambda: _env_int("ZAYTOON_WINDOW_HEIGHT", 800))
    #: Minimum window width.
    window_min_width: int = field(default_factory=lambda: _env_int("ZAYTOON_WINDOW_MIN_WIDTH", 800))
    #: Minimum window height.
    window_min_height: int = field(default_factory=lambda: _env_int("ZAYTOON_WINDOW_MIN_HEIGHT", 600))

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
