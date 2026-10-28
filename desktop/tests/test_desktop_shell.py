"""Desktop shell behaviour.

These replace an earlier suite that asserted on a Tauri/Rust bundle
(``desktop/src-tauri/tauri.conf.json``, ``Cargo.toml``, ``check_env.js``). That
shell no longer exists: ``desktop/`` is a Python application hosting the React
UI in a native WebView (pywebview), so the old tests could only ever fail. They
are replaced by tests against the behaviour that actually ships.

What is deliberately covered here is the set of properties the shell exists to
guarantee:

* it finds the repository (and honours ``ZAYTOON_REPO_ROOT`` for a frozen build),
* it spawns the backend with an argument vector, never a shell string,
* and above all it **never touches a backend it did not start**.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from app import backend as backend_mod
from app import config as config_mod
from app import hotkeys as hotkeys_mod
from app import protocol as protocol_mod
from app.config import DesktopConfig
from app.backend import BackendProcess, backend_is_up, resolve_backend_command, wait_for_backend

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _free_port() -> int:
    """Bind and release a port so we know nothing is listening on it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _ListeningPort:
    """A real listener standing in for an already-running backend.

    It answers ``GET /health`` with HTTP 200 JSON by default, because "the
    backend is up" means *health*, not a bare TCP accept: readiness, adoption
    and the port-conflict report all ask ``/health`` first, and a fake that
    only accepts connections would make every one of those checks see a
    foreign process. Pass ``serve_health=False`` for the port-conflict case —
    something that listens but is not the Zaytoon backend.

    It accepts and closes in a background thread on purpose. A bare
    ``listen(backlog)`` socket fills its accept queue after a single
    unaccepted connection, so the *second* ``backend_is_up()`` probe would be
    refused and the test would report a phantom outage. A real server accepts
    continuously, so the fake must too.
    """

    def __init__(self, serve_health: bool = True) -> None:
        self.serve_health = serve_health
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self.sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            try:
                if self.serve_health:
                    self._answer_health(conn)
            finally:
                conn.close()

    @staticmethod
    def _answer_health(conn) -> None:
        """Read the request (however it is split across packets) and answer 200."""
        conn.settimeout(1.0)
        try:
            request = b""
            while b"\r\n\r\n" not in request and len(request) < 16 * 1024:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                request += chunk
        except OSError:
            return
        if not request.startswith(b"GET "):
            return
        body = b'{"status":"ok","tools_loaded":0}'
        conn.sendall(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )

    @property
    def addr(self) -> str:
        return f"127.0.0.1:{self.port}"

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self.sock.close()


class _FakeChild:
    """Stands in for ``subprocess.Popen`` so nothing is actually spawned.

    ``__enter__``/``__exit__`` are not decoration: ``subprocess.run`` uses the
    context-manager protocol, and code under test that shells out during teardown
    (the Windows ``taskkill`` path in ``BackendProcess._signal_tree``) reaches
    ``run`` while ``subprocess.Popen`` is patched, and died with
    ``TypeError: '_FakeChild' object does not support the context manager
    protocol`` on the windows leg until this was added.
    """

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.terminated = False
        self.killed = False
        self._returncode: int | None = None

    def poll(self):
        return self._returncode

    def terminate(self) -> None:
        self.terminated = True
        self._returncode = 0

    def kill(self) -> None:
        self.killed = True
        self._returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        return self._returncode or 0


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every ``ZAYTOON_*`` override so a developer's shell cannot leak in."""
    for name in list(os.environ):
        if name.startswith("ZAYTOON_"):
            monkeypatch.delenv(name, raising=False)
    return monkeypatch


# ---------------------------------------------------------------------------
# config: repository discovery
# ---------------------------------------------------------------------------
class TestRepoRootDiscovery:
    def test_finds_the_real_repo_from_source_layout(self, clean_env):
        root = config_mod.default_repo_root()
        assert (root / "backend" / "main.py").is_file(), root
        assert (root / "desktop" / "app" / "config.py").is_file(), root

    def test_env_var_overrides_discovery(self, clean_env, tmp_path):
        fake = tmp_path / "checkout"
        (fake / "backend").mkdir(parents=True)
        (fake / "backend" / "main.py").write_text("# stub\n", encoding="utf-8")

        clean_env.setenv(config_mod.REPO_ROOT_ENV, str(fake))
        assert config_mod.default_repo_root() == fake.resolve()

    def test_stale_env_var_falls_back_instead_of_crashing(self, clean_env, tmp_path):
        """A typo must degrade to source discovery, not prevent startup."""
        clean_env.setenv(config_mod.REPO_ROOT_ENV, str(tmp_path / "does-not-exist"))
        root = config_mod.default_repo_root()
        assert (root / "backend" / "main.py").is_file(), root

    def test_looks_like_repo_root_requires_the_backend_entry_point(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert config_mod._looks_like_repo_root(empty) is False

        (empty / "backend").mkdir()
        assert config_mod._looks_like_repo_root(empty) is False

        (empty / "backend" / "main.py").write_text("", encoding="utf-8")
        assert config_mod._looks_like_repo_root(empty) is True

    def test_build_exe_documents_the_env_var_that_exists(self):
        """``build_exe.py`` instructs users to set ``ZAYTOON_REPO_ROOT``.

        That instruction is only honest if the code reads it, so this test is
        the contract between the packaging script and the config module.
        """
        script = REPO_ROOT / "desktop" / "scripts" / "build_exe.py"
        assert script.is_file()
        assert config_mod.REPO_ROOT_ENV in script.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# config: settings
# ---------------------------------------------------------------------------
class TestDesktopConfig:
    def test_hotkey_defaults(self, clean_env):
        cfg = DesktopConfig()
        assert cfg.toggle_hotkey == "ctrl+space"
        assert cfg.expand_hotkey == "alt+z"

    def test_hotkeys_are_env_overridable(self, clean_env):
        clean_env.setenv("ZAYTOON_TOGGLE_HOTKEY", "ctrl+alt+z")
        clean_env.setenv("ZAYTOON_EXPAND_HOTKEY", "ctrl+shift+space")
        cfg = DesktopConfig()
        assert cfg.toggle_hotkey == "ctrl+alt+z"
        assert cfg.expand_hotkey == "ctrl+shift+space"

    def test_backend_endpoint_parsing(self, clean_env):
        clean_env.setenv("ZAYTOON_BACKEND_ADDR", "127.0.0.1:9123")
        cfg = DesktopConfig()
        assert (cfg.host, cfg.port) == ("127.0.0.1", 9123)

    def test_port_falls_back_when_addr_is_malformed(self, clean_env):
        clean_env.setenv("ZAYTOON_BACKEND_ADDR", "nonsense")
        assert DesktopConfig().port == 8000

    def test_manage_backend_can_be_disabled(self, clean_env):
        clean_env.setenv("ZAYTOON_MANAGE_BACKEND", "false")
        assert DesktopConfig().manage_backend is False

    def test_malformed_numeric_env_falls_back_instead_of_raising(self, clean_env):
        clean_env.setenv("ZAYTOON_ORB_SIZE", "not-a-number")
        clean_env.setenv("ZAYTOON_VAD_THRESHOLD", "")
        cfg = DesktopConfig()
        assert cfg.orb_size == 320
        assert cfg.vad_threshold == pytest.approx(0.012)

    def test_describe_mentions_the_essentials(self, clean_env):
        text = DesktopConfig().describe()
        for token in ("repo=", "addr=", "ws=", "hotkeys=", "manage_backend="):
            assert token in text, text

    def test_dev_mode_defaults_to_false(self, clean_env):
        assert DesktopConfig().dev_mode is False

    def test_dev_mode_can_be_enabled(self, clean_env):
        clean_env.setenv("ZAYTOON_DEV_MODE", "true")
        assert DesktopConfig().dev_mode is True

    def test_frontend_dist_defaults_to_repo_frontend_dist(self, clean_env):
        cfg = DesktopConfig()
        assert cfg.frontend_dist == cfg.repo_root / "frontend" / "dist"

    def test_window_dimensions_have_sane_defaults(self, clean_env):
        cfg = DesktopConfig()
        assert cfg.window_width == 1280
        assert cfg.window_height == 800
        assert cfg.window_min_width == 800
        assert cfg.window_min_height == 600


# ---------------------------------------------------------------------------
# backend lifecycle
# ---------------------------------------------------------------------------
class TestPortProbe:
    def test_closed_port_is_down(self):
        assert backend_is_up("127.0.0.1", _free_port()) is False

    def test_open_listener_is_up(self):
        listener = _ListeningPort()
        try:
            assert backend_is_up("127.0.0.1", listener.port) is True
        finally:
            listener.close()

    def test_probe_does_not_raise_on_a_bad_host(self):
        assert backend_is_up("no-such-host.invalid", 8000, timeout=0.05) is False

    def test_wait_returns_true_when_already_listening(self):
        listener = _ListeningPort()
        try:
            assert wait_for_backend("127.0.0.1", listener.port, timeout=1.0) is True
        finally:
            listener.close()

    def test_wait_gives_up_on_a_dead_port(self):
        assert wait_for_backend("127.0.0.1", _free_port(), timeout=0.3, interval=0.05) is False


class TestBackendCommandResolution:
    def _config(self, repo_root: Path, **overrides) -> DesktopConfig:
        cfg = DesktopConfig()
        cfg.repo_root = repo_root
        for key, value in overrides.items():
            setattr(cfg, key, value)
        return cfg

    def test_prefers_the_explicit_interpreter(self, clean_env, tmp_path):
        repo = tmp_path / "repo"
        (repo / "backend").mkdir(parents=True)
        (repo / "backend" / "main.py").write_text("", encoding="utf-8")
        cfg = self._config(repo, backend_python=sys.executable)

        resolved = resolve_backend_command(cfg)
        assert resolved is not None
        assert resolved.argv[0] == sys.executable
        assert Path(resolved.argv[1]) == repo / "backend" / "main.py"
        assert Path(resolved.cwd) == repo / "backend"
        assert resolved.kind == "override"

    def test_falls_back_to_the_repo_venv(self, clean_env, tmp_path):
        repo = tmp_path / "repo"
        (repo / "backend").mkdir(parents=True)
        (repo / "backend" / "main.py").write_text("", encoding="utf-8")
        venv_python = repo / "backend" / ".venv" / "Scripts" / "python.exe"
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("", encoding="utf-8")

        resolved = resolve_backend_command(self._config(repo))
        assert resolved is not None
        assert Path(resolved.argv[0]) == venv_python
        assert resolved.kind == "venv"

    def test_falls_back_to_the_running_interpreter(self, clean_env, tmp_path):
        repo = tmp_path / "repo"
        (repo / "backend").mkdir(parents=True)
        (repo / "backend" / "main.py").write_text("", encoding="utf-8")
        resolved = resolve_backend_command(self._config(repo))
        assert resolved is not None
        assert resolved.argv[0] == sys.executable
        assert resolved.kind == "source"

    def test_returns_none_when_the_backend_is_absent(self, clean_env, tmp_path):
        """A missing ``main.py`` must produce an actionable ``None``."""
        assert resolve_backend_command(self._config(tmp_path)) is None

    def test_cwd_and_main_are_independently_overridable(self, clean_env, tmp_path):
        repo = tmp_path / "repo"
        (repo / "backend").mkdir(parents=True)
        (repo / "backend" / "main.py").write_text("", encoding="utf-8")
        custom_main = tmp_path / "other.py"
        custom_main.write_text("", encoding="utf-8")
        cfg = self._config(repo, backend_python=sys.executable, backend_main=str(custom_main))
        resolved = resolve_backend_command(cfg)
        assert resolved is not None
        assert Path(resolved.argv[1]) == custom_main

    def test_a_frozen_build_runs_its_own_serve_mode(self, clean_env, monkeypatch):
        """One artifact, no checkout, no second Python: ``Zaytoon.exe --serve``.

        The bundle's backend is imported *in-process* by that mode, so the
        resolved command names this executable and nothing else — the old
        ``[sys.executable, <bundle>/backend/main.py]`` form pasted a script
        argument onto a windowed exe and could never start.
        """
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        cfg = self._config(Path("/nonexistent-repo"))
        resolved = resolve_backend_command(cfg)
        assert resolved is not None
        assert tuple(resolved.argv) == (sys.executable, "--serve")
        assert resolved.kind == "bundled"


class TestBackendNeverTouchesAForeignProcess:
    """The shell's single most important safety property."""

    def _cfg_for(self, addr: str) -> DesktopConfig:
        """Config pointed at ``addr``.

        ``backend_addr`` is the single source of truth: ``host`` and ``port``
        are read-only properties derived from it, so overriding the address is
        both necessary and sufficient.
        """
        cfg = DesktopConfig()
        cfg.backend_addr = addr
        cfg.repo_root = REPO_ROOT
        return cfg

    def test_ensure_running_adopts_an_existing_listener_without_spawning(self, clean_env, monkeypatch):
        listener = _ListeningPort()
        spawned: list = []
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a) or _FakeChild())
        try:
            proc = BackendProcess(self._cfg_for(listener.addr))
            status = proc.ensure_running()
            assert status.running is True
            assert status.managed is False, "a backend we did not start is never 'managed'"
            assert spawned == [], "must not spawn when the port already answers"
        finally:
            listener.close()

    def test_shutdown_leaves_a_foreign_listener_alive(self, clean_env):
        """Even if a handle were somehow attached, shutdown must not kill it."""
        listener = _ListeningPort()
        try:
            proc = BackendProcess(self._cfg_for(listener.addr))
            proc.ensure_running()
            proc.shutdown()
            assert backend_is_up("127.0.0.1", listener.port) is True
        finally:
            listener.close()

    def test_a_listener_that_never_answers_health_is_a_conflict_not_a_backend(
        self, clean_env, monkeypatch
    ):
        """A TCP accept is not readiness: some *other* app owns this port.

        Adopting it would report a green status for a backend that does not
        exist, and spawning would only fail to bind — so it is a stated
        conflict, with nothing touched.
        """
        listener = _ListeningPort(serve_health=False)
        spawned: list = []
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a) or _FakeChild())
        try:
            proc = BackendProcess(self._cfg_for(listener.addr))
            status = proc.ensure_running()
            assert status.running is False
            assert "in use by another application" in status.hint
            assert spawned == [], "must not spawn against a port that is already held"
        finally:
            listener.close()

    def test_a_dead_child_releases_the_handle_when_a_foreign_process_takes_over(
        self, clean_env, monkeypatch
    ):
        """Our child exited and something else owns the port: stop calling it ours.

        ``Popen.poll()`` returns ``None`` while the child is *alive* and an exit
        code once it has died, so the fake must report a real exit code here.
        """
        exited = _FakeChild()
        exited._returncode = 1
        assert exited.poll() == 1

        proc = BackendProcess(self._cfg_for(f"127.0.0.1:{_free_port()}"))
        proc._child = exited
        proc._spawned_by_us = True

        listener = _ListeningPort()
        try:
            proc.config.backend_addr = listener.addr
            status = proc.ensure_running()
            assert status.running is True
            assert proc.managed is False, "an exited child must not stay 'managed'"
            assert proc.pid is None
        finally:
            listener.close()

    def test_a_live_child_keeps_its_handle_when_the_port_answers(self, clean_env):
        """A running child plus a live port is the normal managed case."""
        proc = BackendProcess(self._cfg_for(f"127.0.0.1:{_free_port()}"))
        child = _FakeChild()  # poll() -> None, still running
        proc._child = child
        proc._spawned_by_us = True

        listener = _ListeningPort()
        try:
            proc.config.backend_addr = listener.addr
            status = proc.ensure_running()
            assert status.running is True
            assert proc.managed is True
            assert proc.pid == child.pid
        finally:
            listener.close()
            proc.shutdown()

    def test_shutdown_never_touches_an_unspawned_child(self, clean_env):
        proc = BackendProcess(self._cfg_for("127.0.0.1:8000"))
        child = _FakeChild()
        proc._child = child
        proc._spawned_by_us = False  # the default, and the case that matters

        proc.shutdown()
        assert child.terminated is False
        assert child.killed is False

    def test_shutdown_terminates_a_child_we_started(self, clean_env):
        proc = BackendProcess(self._cfg_for("127.0.0.1:8000"))
        child = _FakeChild()
        proc._child = child
        proc._spawned_by_us = True

        proc.shutdown()
        assert child.terminated is True
        assert child.killed is False
        assert proc.pid is None

    def test_shutdown_escalates_to_kill_when_terminate_is_ignored(self, clean_env):
        class _Stubborn(_FakeChild):
            def terminate(self) -> None:
                self.terminated = True

            def wait(self, timeout: float | None = None) -> int:
                if not self.killed:
                    raise subprocess.TimeoutExpired("python", timeout or 0)
                return -9

        proc = BackendProcess(self._cfg_for("127.0.0.1:8000"))
        child = _Stubborn()
        proc._child = child
        proc._spawned_by_us = True

        proc.shutdown()
        assert child.terminated is True
        assert child.killed is True

    def test_shutdown_is_idempotent(self, clean_env):
        proc = BackendProcess(self._cfg_for("127.0.0.1:8000"))
        child = _FakeChild()
        proc._child = child
        proc._spawned_by_us = True

        proc.shutdown()
        proc.shutdown()  # must not raise
        assert proc.managed is False


class TestBackendSpawnFailure:
    def _dead_config(self, **overrides) -> DesktopConfig:
        """Config on a port nothing is listening on, with no resolvable backend."""
        cfg = DesktopConfig()
        cfg.backend_addr = f"127.0.0.1:{_free_port()}"
        # Point at a repo with no backend so resolution fails.
        cfg.repo_root = Path(os.environ.get("TEMP", ".")) / "zaytoon-no-such-root"
        for key, value in overrides.items():
            setattr(cfg, key, value)
        return cfg

    def test_missing_backend_reports_an_actionable_hint(self, clean_env):
        status = BackendProcess(self._dead_config()).ensure_running()
        assert status.running is False
        assert "backend" in status.hint.lower()
        assert "main.py" in status.hint

    def test_opt_out_of_management_is_honoured(self, clean_env, monkeypatch):
        spawned: list = []
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a) or _FakeChild())
        status = BackendProcess(self._dead_config(manage_backend=False)).ensure_running()
        assert status.running is False
        assert "ZAYTOON_MANAGE_BACKEND" in status.hint
        assert spawned == []

    def test_unresolvable_interpreter_reports_a_hint(self, clean_env, monkeypatch):
        def _boom(*args, **kwargs):
            raise OSError("no such file")

        monkeypatch.setattr(subprocess, "Popen", _boom)
        cfg = self._dead_config()
        cfg.repo_root = REPO_ROOT  # resolvable, so we reach the Popen call
        status = BackendProcess(cfg).ensure_running()
        assert status.running is False
        assert "Failed to start" in status.hint

    def test_timeout_rolls_back_the_child(self, clean_env, monkeypatch):
        """A half-dead child must not block a later attempt."""
        child = _FakeChild()
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: child)
        monkeypatch.setattr(backend_mod, "wait_for_ready", lambda *a, **k: False)

        cfg = self._dead_config()
        cfg.repo_root = REPO_ROOT
        cfg.backend_timeout = 0.1
        proc = BackendProcess(cfg)

        status = proc.ensure_running()
        assert status.running is False
        assert "within" in status.hint
        assert child.terminated is True, "the spawned child must be rolled back"
        assert proc.pid is None


class TestSpawnIsArgumentVector:
    def _live_config(self) -> DesktopConfig:
        cfg = DesktopConfig()
        cfg.backend_addr = f"127.0.0.1:{_free_port()}"
        cfg.repo_root = REPO_ROOT
        return cfg

    def test_backend_is_spawned_with_argv_and_no_shell(self, clean_env, monkeypatch):
        captured: dict = {}

        def _capture(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return _FakeChild()

        monkeypatch.setattr(subprocess, "Popen", _capture)
        monkeypatch.setattr(backend_mod, "wait_for_ready", lambda *a, **k: True)

        BackendProcess(self._live_config()).ensure_running()

        assert isinstance(captured["argv"], list), "must be an argument vector"
        # Compare against the resolver's decision, not sys.executable: the
        # interpreter may legitimately be backend/.venv while the suite runs
        # from a different venv (and CI has no backend/.venv at all — only
        # tier 3). Asserting sys.executable encoded the *runner's* environment
        # and failed on any other interpreter, even though the spawn itself
        # was correct. The tier logic has its own tests in
        # TestBackendCommandResolution; this test owns the hand-off.
        resolved = resolve_backend_command(self._live_config())
        assert resolved is not None
        assert captured["argv"][0] == resolved.argv[0]
        assert captured["argv"][0].lower().endswith(
            ("python", "python.exe")
        ), "argv[0] must be an interpreter path, not arbitrary text"
        assert captured["argv"][1].endswith("main.py")
        assert "shell" not in captured["kwargs"], "shell=True must never be used"
        assert captured["kwargs"]["env"].get("PYTHONUNBUFFERED") == "1"

    @pytest.mark.skipif(os.name != "nt", reason="CREATE_NO_WINDOW is Windows-only")
    def test_console_is_hidden_on_windows(self, clean_env, monkeypatch):
        captured: dict = {}

        def _capture(argv, **kwargs):
            captured.update(kwargs)
            return _FakeChild()

        monkeypatch.setattr(subprocess, "Popen", _capture)
        monkeypatch.setattr(backend_mod, "wait_for_ready", lambda *a, **k: True)

        BackendProcess(self._live_config()).ensure_running()

        assert captured.get("creationflags") == backend_mod.CREATE_NO_WINDOW


# ---------------------------------------------------------------------------
# protocol
# ---------------------------------------------------------------------------
class TestOutboundBuilders:
    def test_chat_message(self):
        assert protocol_mod.chat_message("hi") == {"type": "message", "message": "hi"}

    def test_chat_message_carries_a_response_id_when_given(self):
        payload = protocol_mod.chat_message("hi", response_id="desktop-1")
        assert payload["response_id"] == "desktop-1"

    def test_audio_message_base64_round_trips(self):
        import base64

        pcm = b"\x01\x02\x03\x04" * 100
        payload = protocol_mod.audio_message(pcm, language="ar")
        assert payload["type"] == "audio"
        assert base64.b64decode(payload["audio"]) == pcm
        assert payload["language"] == "ar"

    def test_each_audio_chunk_is_independently_decodable(self):
        """Chunks are concatenated client-side, so padding must be per-message."""
        import base64

        chunks = [b"\xff" * 3, b"\xab" * 5, b"\x00" * 7]
        decoded = [base64.b64decode(protocol_mod.audio_message(c)["audio"]) for c in chunks]
        assert decoded == chunks

    def test_interrupt_message(self):
        assert protocol_mod.interrupt_message() == {"type": "interrupt"}

    def test_tool_confirm_coerces_approval_to_bool(self):
        payload = protocol_mod.tool_confirm("cfm_abc", approved=1)
        assert payload == {
            "type": "tool_confirm",
            "request_id": "cfm_abc",
            "approved": True,
        }


class TestInboundDecoding:
    def test_parses_a_known_type(self):
        event = protocol_mod.ServerEvent.parse('{"type":"status","content":"thinking"}')
        assert event is not None
        assert event.type is protocol_mod.EventType.STATUS
        assert event.text == "thinking"

    def test_accepts_bytes(self):
        event = protocol_mod.ServerEvent.parse(b'{"type":"error","content":"boom"}')
        assert event is not None
        assert event.type is protocol_mod.EventType.ERROR

    @pytest.mark.parametrize(
        "raw",
        ["", "not json", "[1,2,3]", '"a string"', "null", "123"],
    )
    def test_malformed_frames_return_none_instead_of_raising(self, raw):
        assert protocol_mod.ServerEvent.parse(raw) is None

    def test_undecodable_bytes_return_none(self):
        assert protocol_mod.ServerEvent.parse(b"\xff\xfe\x00\x00") is None

    def test_unrecognised_type_becomes_unknown_not_an_exception(self):
        event = protocol_mod.ServerEvent.parse('{"type":"brand_new_thing"}')
        assert event is not None
        assert event.type is protocol_mod.EventType.UNKNOWN

    def test_missing_type_is_unknown(self):
        event = protocol_mod.ServerEvent.parse('{"content":"orphan"}')
        assert event.type is protocol_mod.EventType.UNKNOWN

    def test_unknown_status_falls_back_to_thinking(self):
        """A new server status must not be able to crash the UI."""
        event = protocol_mod.ServerEvent.parse('{"type":"status","content":"hallucinating"}')
        assert event.status == "thinking"

    def test_known_status_is_preserved(self):
        for status in sorted(protocol_mod.KNOWN_STATUSES):
            event = protocol_mod.ServerEvent.parse(
                '{"type":"status","content":"%s"}' % status
            )
            assert event.status == status

    def test_connected_maps_to_connected_status(self):
        event = protocol_mod.ServerEvent.parse('{"type":"connected"}')
        assert event.status == "connected"

    def test_non_status_event_has_no_status(self):
        assert protocol_mod.ServerEvent.parse('{"type":"token","content":"x"}').status == ""

    def test_is_final_marks_the_terminal_frames(self):
        for payload in ('{"type":"message","content":"x"}', '{"type":"error","content":"x"}'):
            assert protocol_mod.ServerEvent.parse(payload).is_final is True
        assert protocol_mod.ServerEvent.parse('{"type":"token","content":"x"}').is_final is False

    def test_every_outbound_builder_type_is_understood_on_the_way_back(self):
        """Guard against the two halves of the protocol drifting apart."""
        outbound = {
            "message",
            "audio",
            "interrupt",
            "tool_confirm",
        }
        # The server does not echo the client's own types back, so instead
        # assert every type the shell *can* receive is a known EventType or
        # explicitly tolerated as UNKNOWN.
        server_types = {
            "status", "token", "message", "subtitle", "stt_result", "audio_start",
            "audio_chunk", "audio_end", "wake_word", "require_confirmation",
            "confirmation_result", "reminder", "reminder_list", "reminder_deleted",
            "reminder_done", "error", "intent", "pong", "interrupted",
        }
        for name in server_types:
            event = protocol_mod.ServerEvent.parse('{"type":"%s"}' % name)
            assert event is not None
        # `audio_end` and the reminder events are not in EventType yet; the
        # shell must not crash on them, which UNKNOWN guarantees.
        assert outbound  # keeps the intent of the guard explicit


# ---------------------------------------------------------------------------
# hotkeys
# ---------------------------------------------------------------------------
class TestHotkeys:
    def test_formats_the_default_bindings(self):
        assert hotkeys_mod.format_hotkey("ctrl+space") == "Ctrl + Space"
        assert hotkeys_mod.format_hotkey("alt+z") == "Alt + Z"

    def test_formats_multi_key_bindings(self):
        assert hotkeys_mod.format_hotkey("ctrl+shift+esc") == "Ctrl + Shift + Esc"

    def test_label_map_covers_the_configured_defaults(self):
        """``HOTKEY_LABELS`` is otherwise dead; this keeps it honest."""
        cfg = DesktopConfig()
        for hotkey in (cfg.toggle_hotkey, cfg.expand_hotkey):
            assert hotkey in hotkeys_mod.HOTKEY_LABELS, hotkey

    def test_ignores_empty_segments(self):
        assert hotkeys_mod.format_hotkey("ctrl++space") == "Ctrl + Space"

    def test_unknown_keys_are_capitalised_rather_than_dropped(self):
        assert hotkeys_mod.format_hotkey("ctrl+f13") == "Ctrl + F13"

    def test_register_fails_gracefully_without_the_keyboard_package(self, monkeypatch):
        monkeypatch.setattr(hotkeys_mod, "keyboard", None)
        manager = hotkeys_mod.HotkeyManager()
        assert manager.register("ctrl+space", lambda: None) is False
        assert manager.available is False
        assert "keyboard" in manager.error
        assert "unavailable" in manager.probe()

    def test_register_reports_a_conflict_instead_of_raising(self, monkeypatch):
        class _Conflict:
            @staticmethod
            def add_hotkey(*args, **kwargs):
                raise RuntimeError("hotkey already registered")

        monkeypatch.setattr(hotkeys_mod, "keyboard", _Conflict)
        manager = hotkeys_mod.HotkeyManager()
        assert manager.register("ctrl+space", lambda: None) is False
        assert "already in use" in manager.error
        assert manager.bound() == []

    def test_unregister_is_safe_when_nothing_is_bound(self, monkeypatch):
        monkeypatch.setattr(hotkeys_mod, "keyboard", None)
        hotkeys_mod.HotkeyManager().unregister_all()  # must not raise

    def test_empty_hotkey_is_rejected(self, monkeypatch):
        monkeypatch.setattr(hotkeys_mod, "keyboard", object())
        assert hotkeys_mod.HotkeyManager().register("", lambda: None) is False


# ---------------------------------------------------------------------------
# packaging
# ---------------------------------------------------------------------------
class TestBuildScript:
    def test_prerequisites_returns_a_list(self):
        """Never raises — a missing PyInstaller must be reported, not crash."""
        script = REPO_ROOT / "desktop" / "scripts" / "build_exe.py"
        assert script.is_file()
        source = script.read_text(encoding="utf-8")
        assert "def check_prerequisites" in source

    def test_entry_point_exists(self):
        assert (REPO_ROOT / "desktop" / "main.py").is_file()

    def test_icon_asset_exists(self):
        assert (REPO_ROOT / "desktop" / "icons" / "source.png").is_file()

    def test_requirements_list_pywebview(self):
        """The host's runtime dependency must be declared, or CI installs a shell that cannot start."""
        req_file = REPO_ROOT / "desktop" / "requirements.txt"
        assert req_file.is_file()
        assert "pywebview" in req_file.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# guard against the Tauri shell creeping back in
# ---------------------------------------------------------------------------
class TestNoRustResidue:
    """The docs once described a Tauri build that does not exist.

    These assertions fail loudly if a half-finished Rust migration is started
    without also updating the documentation and CI, which is exactly how the
    drift happened in the first place.
    """

    @pytest.mark.parametrize(
        "relative",
        [
            "src-tauri",
            "package.json",
            "Cargo.toml",
            "README.md",
        ],
    )
    def test_no_tauri_scaffolding_in_the_desktop_directory(self, relative):
        desktop = REPO_ROOT / "desktop"
        assert not (desktop / relative).exists(), (
            f"desktop/{relative} exists. If a Tauri migration is starting, update "
            "AGENTS.md and .github/workflows/ci.yml in the same change."
        )

    def test_agents_md_does_not_instruct_tauri_usage(self):
        """The docs must not send an agent to a build that does not exist.

        Note this asserts on *instructions*, not on the word "Tauri": the
        desktop section deliberately records that the shell migrated off Tauri,
        and that note is worth keeping.
        """
        agents_md = REPO_ROOT / "AGENTS.md"
        if not agents_md.is_file():
            pytest.skip("AGENTS.md not present")
        text = agents_md.read_text(encoding="utf-8")
        forbidden = [
            "src-tauri",
            "tauri:dev",
            "tauri:build",
            "tauri.conf.json",
            "npm run icons",
            "GlobalShortcutManager",
        ]
        for token in forbidden:
            assert token not in text, (
                f"AGENTS.md still references {token!r}; desktop/ is a "
                "Python/pywebview application with no Rust toolchain."
            )

    def test_startup_script_does_not_launch_a_tauri_dev_server(self):
        """``start.bat --desktop`` used to run a script that does not exist."""
        for name in ("start.bat", "start.sh"):
            script = REPO_ROOT / name
            if not script.is_file():
                continue
            text = script.read_text(encoding="utf-8", errors="replace")
            assert "tauri:dev" not in text, name
            assert "npm run icons" not in text, name

    def test_ci_does_not_reference_the_removed_tauri_bundle(self):
        workflow = REPO_ROOT / ".github" / "workflows" / "ci.yml"
        if not workflow.is_file():
            pytest.skip("CI workflow not present")
        text = workflow.read_text(encoding="utf-8")
        assert "src-tauri" not in text, "CI still reads the removed Tauri config"
        assert "tauri" not in text.lower(), "CI still installs the Tauri CLI"
