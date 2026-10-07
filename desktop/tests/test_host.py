"""Tests for the WebView host (app/host.py).

These tests verify that the host:
- Resolves URLs correctly (dev mode vs production)
- Manages window state persistence
- Handles backend lifecycle
- Provides diagnostic information
- Enforces single-instance operation
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.config import DesktopConfig
from app.host import WebViewHost


@pytest.fixture
def config():
    """Create a test config."""
    cfg = DesktopConfig()
    cfg.repo_root = Path(__file__).resolve().parent.parent.parent
    cfg.dev_mode = False
    cfg.frontend_dist = cfg.repo_root / "frontend" / "dist"
    return cfg


@pytest.fixture
def host(config):
    """Create a WebView host for testing."""
    return WebViewHost(config)


class TestURLResolution:
    def test_production_mode_loads_local_index(self, host, config, tmp_path):
        """In production mode, the host should load the local index.html.

        The fake dist lives in tmp_path: an earlier version of this test wrote
        its mock page straight into the repository's real ``frontend/dist``,
        destroying the production build on every run.
        """
        dist = tmp_path / "dist"
        dist.mkdir(parents=True)
        (dist / "index.html").write_text("<html></html>", encoding="utf-8")
        config.frontend_dist = dist

        url = host._resolve_url()
        assert url == str(dist / "index.html")

    def test_dev_mode_loads_vite_dev_server(self, host, config):
        """In dev mode, the host should connect to the Vite dev server."""
        config.dev_mode = True
        config.dev_port = 5173

        url = host._resolve_url()
        assert url == "http://localhost:5173"

    def test_dev_mode_custom_port(self, host, config):
        """Dev mode should use the configured port."""
        config.dev_mode = True
        config.dev_port = 3000

        url = host._resolve_url()
        assert url == "http://localhost:3000"

    def test_fallback_to_dev_mode_when_no_production_build(self, host, config, tmp_path):
        """When no production build exists, fall back to dev mode."""
        config.frontend_dist = tmp_path / "nonexistent"
        config.dev_mode = False
        config.dev_port = 5173

        url = host._resolve_url()
        assert url == "http://localhost:5173"


class TestWindowState:
    def test_load_window_state_returns_empty_dict_when_no_file(self, host, tmp_path, monkeypatch):
        """When no state file exists, return an empty dict."""
        monkeypatch.setenv("ZAYTOON_HOME", str(tmp_path))
        state = host._load_window_state()
        assert state == {}

    def test_save_and_load_window_state(self, host, tmp_path, monkeypatch):
        """Window state should persist to a JSON file."""
        monkeypatch.setenv("ZAYTOON_HOME", str(tmp_path))

        # Create a mock window
        mock_window = MagicMock()
        mock_window.width = 1024
        mock_window.height = 768
        mock_window.x = 50
        mock_window.y = 50
        mock_window.maximized = False
        host.window = mock_window

        # Save state
        host._save_window_state()

        # Load state in a new host
        new_host = WebViewHost(host.config)
        state = new_host._load_window_state()

        assert state["width"] == 1024
        assert state["height"] == 768
        assert state["x"] == 50
        assert state["y"] == 50
        assert state["maximized"] is False

    def test_save_window_state_creates_parent_dirs(self, host, tmp_path, monkeypatch):
        """Saving state should create the config directory if needed."""
        config_dir = tmp_path / "nested" / "config"
        monkeypatch.setenv("ZAYTOON_HOME", str(config_dir))

        mock_window = MagicMock()
        mock_window.width = 800
        mock_window.height = 600
        mock_window.x = 0
        mock_window.y = 0
        mock_window.maximized = True
        host.window = mock_window

        host._save_window_state()

        state_file = config_dir / "window_state.json"
        assert state_file.exists()

    def test_save_window_state_handles_errors_gracefully(self, host):
        """Saving state should not raise on error."""
        host.window = None  # No window set
        host._save_window_state()  # Should not raise

    def test_load_window_state_handles_corrupt_file(self, host, tmp_path, monkeypatch):
        """A corrupt state file should return an empty dict."""
        monkeypatch.setenv("ZAYTOON_HOME", str(tmp_path))
        state_file = tmp_path / "window_state.json"
        state_file.write_text("not json", encoding="utf-8")

        state = host._load_window_state()
        assert state == {}


class TestBackendLifecycle:
    def test_host_creates_backend_process(self, host):
        """The host should create a BackendProcess."""
        assert host.backend is not None

    def test_host_run_starts_backend(self, host, monkeypatch):
        """Running the host should start the backend."""
        mock_status = MagicMock()
        mock_status.running = True
        mock_status.managed = True
        mock_status.pid = 1234

        monkeypatch.setattr(host.backend, "ensure_running", lambda: mock_status)
        # Deterministic: this test is about the happy path, not about whether
        # the machine this suite happens to run on ships WebView2.
        monkeypatch.setattr("app.host.webview2_runtime_present", lambda: True)

        # Mock webview at module level
        mock_window = MagicMock()
        mock_webview = MagicMock()
        mock_webview.create_window.return_value = mock_window

        with patch.dict("sys.modules", {"webview": mock_webview}):
            with patch("app.host.WebViewHost._start_health_check"):
                host.run()

        mock_webview.create_window.assert_called_once()
        mock_webview.start.assert_called_once()

    def test_run_fails_fast_with_a_clear_message_when_webview2_is_missing(
        self, host, monkeypatch, capsys
    ):
        """A missing runtime must be named, not surfaced as a pywebview crash."""
        monkeypatch.setattr("app.host.webview2_runtime_present", lambda: False)
        # webview must not even be imported on this path.
        monkeypatch.setitem(sys.modules, "webview", None)

        assert host.run() == 1

        err = capsys.readouterr().err
        assert "WebView2" in err
        assert "developer.microsoft.com" in err

    def test_host_shutdown_stops_backend(self, host, monkeypatch):
        """Shutting down should stop the backend."""
        mock_status = MagicMock()
        mock_status.running = True

        monkeypatch.setattr(host.backend, "ensure_running", lambda: mock_status)
        monkeypatch.setattr(host.backend, "shutdown", lambda: None)

        host._on_closed()

        assert host._shutting_down is True


class TestHealthCheck:
    def test_health_check_thread_starts(self, host):
        """The health check thread should start."""
        host._start_health_check()
        assert host._health_check_thread is not None
        assert host._health_check_thread.is_alive()
        assert host._health_check_thread.daemon is True

    def test_health_check_loop_stops_on_shutdown(self, host):
        """The health check loop should stop when shutting down."""
        host._start_health_check()
        host._shutting_down = True
        host._health_check_thread.join(timeout=2)
        assert not host._health_check_thread.is_alive()


class TestDiagnostics:
    def test_diagnose_returns_config_info(self, host):
        """Diagnostics should include config information."""
        diag = host.diagnose()
        assert "config" in diag
        assert "repo_root" in diag["config"]
        assert "backend_addr" in diag["config"]
        assert "ws_url" in diag["config"]

    def test_diagnose_returns_backend_status(self, host):
        """Diagnostics should include backend status."""
        diag = host.diagnose()
        assert "backend" in diag
        assert "running" in diag["backend"]

    def test_diagnose_returns_url(self, host):
        """Diagnostics should include the resolved URL."""
        diag = host.diagnose()
        assert "url" in diag
        assert diag["url"]  # Should be non-empty


class TestWindowStateFile:
    def test_state_file_path_uses_zaytoon_home(self, host, tmp_path, monkeypatch):
        """The state file should be in ZAYTOON_HOME/Config/window_state.json."""
        monkeypatch.setenv("ZAYTOON_HOME", str(tmp_path))
        state_file = host._get_state_file()
        assert state_file == tmp_path / "window_state.json"

    def test_state_file_path_falls_back_to_repo(self, host, monkeypatch):
        """Without ZAYTOON_HOME, the state file should be in the repo."""
        monkeypatch.delenv("ZAYTOON_HOME", raising=False)
        state_file = host._get_state_file()
        assert "Config" in str(state_file)
        assert "window_state.json" in str(state_file)


class _FakeWinreg:
    """A stand-in for the ``winreg`` module with a controllable answer."""

    HKEY_LOCAL_MACHINE = 1
    HKEY_CURRENT_USER = 2

    def __init__(self, installed: bool):
        self.installed = installed

    def OpenKey(self, root, subkey):  # noqa: N802 - mirrors the real API
        if not self.installed:
            raise FileNotFoundError(subkey)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def QueryValueEx(self, key, name):  # noqa: N802 - mirrors the real API
        return ("120.0.2000.0", 1)


class TestWebView2Detection:
    """A missing WebView2 runtime must be reported, never discovered as a crash."""

    def test_non_windows_needs_no_runtime(self, monkeypatch):
        import app.host as host_mod

        monkeypatch.setattr(host_mod.sys, "platform", "linux")
        assert host_mod.webview2_runtime_present() is True

    def test_registry_install_is_detected(self, monkeypatch):
        import app.host as host_mod

        monkeypatch.setattr(host_mod.sys, "platform", "win32")
        monkeypatch.setitem(sys.modules, "winreg", _FakeWinreg(installed=True))
        assert host_mod.webview2_runtime_present() is True

    def test_absent_runtime_reports_false(self, monkeypatch, tmp_path):
        import app.host as host_mod

        monkeypatch.setattr(host_mod.sys, "platform", "win32")
        monkeypatch.setitem(sys.modules, "winreg", _FakeWinreg(installed=False))
        # Point the filesystem fallback at an empty directory.
        monkeypatch.setenv("ProgramFiles", str(tmp_path))
        monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path))
        assert host_mod.webview2_runtime_present() is False

    def test_uninstalled_marker_is_not_mistaken_for_an_install(
        self, monkeypatch, tmp_path
    ):
        """EdgeUpdate leaves a ``pv`` of 0.0.0.0 behind after uninstalling."""
        import app.host as host_mod

        class _Uninstalled(_FakeWinreg):
            def QueryValueEx(self, key, name):  # noqa: N802
                return ("0.0.0.0", 1)

        monkeypatch.setattr(host_mod.sys, "platform", "win32")
        monkeypatch.setitem(sys.modules, "winreg", _Uninstalled(installed=True))
        monkeypatch.setenv("ProgramFiles", str(tmp_path))
        monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path))
        assert host_mod.webview2_runtime_present() is False

    def test_a_fixed_path_install_is_detected_from_disk(self, monkeypatch, tmp_path):
        import app.host as host_mod

        monkeypatch.setattr(host_mod.sys, "platform", "win32")
        monkeypatch.setitem(sys.modules, "winreg", _FakeWinreg(installed=False))
        app_dir = tmp_path / "Microsoft" / "EdgeWebView" / "Application" / "120.0.0.0"
        app_dir.mkdir(parents=True)
        monkeypatch.setenv("ProgramFiles", str(tmp_path))
        monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "absent"))
        assert host_mod.webview2_runtime_present() is True

    def test_diagnose_names_the_runtime_and_the_download_url(self, monkeypatch, capsys):
        """--diagnose must say exactly what is missing and where to get it."""
        import main as main_mod

        monkeypatch.setattr(main_mod, "webview2_runtime_present", lambda: False)
        monkeypatch.setattr(main_mod.sys, "platform", "win32")
        monkeypatch.setattr(main_mod, "backend_is_up", lambda *a, **k: True)
        monkeypatch.setattr(main_mod, "resolve_backend_command", lambda cfg: None)

        rc = main_mod._diagnose(DesktopConfig())

        out = capsys.readouterr().out
        assert rc == 1
        assert "WebView2 runtime missing" in out
        assert main_mod.WEBVIEW2_DOWNLOAD_URL in out


class TestAssetServerPort:
    """The asset-server port must sit inside the backend's CORS allowlist."""

    def test_picks_a_port_from_the_cors_range(self, monkeypatch):
        import app.host as host_mod

        monkeypatch.delenv("ZAYTOON_DESKTOP_PORT", raising=False)
        port = host_mod.pick_asset_server_port()
        assert port in host_mod.DESKTOP_PORT_RANGE

    def test_env_override_wins(self, monkeypatch):
        import app.host as host_mod

        monkeypatch.setenv("ZAYTOON_DESKTOP_PORT", "5177")
        assert host_mod.pick_asset_server_port() == 5177

    def test_bad_env_falls_back_to_the_range(self, monkeypatch):
        import app.host as host_mod

        monkeypatch.setenv("ZAYTOON_DESKTOP_PORT", "not-a-port")
        assert host_mod.pick_asset_server_port() in host_mod.DESKTOP_PORT_RANGE

    def test_returns_none_when_the_candidate_is_busy(self, monkeypatch):
        """A busy port must be *reported*, not handed to pywebview.

        bottle then dies inside its thread and pywebview still reports the
        address as healthy — a blank window with no error anywhere.
        """
        import socket as socket_mod

        import app.host as host_mod

        with socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_STREAM) as holder:
            holder.bind(("127.0.0.1", 0))
            holder.listen(1)
            monkeypatch.setenv("ZAYTOON_DESKTOP_PORT", str(holder.getsockname()[1]))
            assert host_mod.pick_asset_server_port() is None

    def test_run_passes_the_picked_port_to_pywebview(self, host, monkeypatch):
        """The whole point of picking it: webview.start must receive it."""
        import app.host as host_mod

        monkeypatch.setattr("app.host.webview2_runtime_present", lambda: True)
        monkeypatch.setattr(host.backend, "ensure_running", MagicMock())
        monkeypatch.setattr(host_mod, "pick_asset_server_port", lambda: 5176)

        mock_webview = MagicMock()
        with patch.dict("sys.modules", {"webview": mock_webview}):
            with patch("app.host.WebViewHost._start_health_check"):
                assert host.run() == 0

        mock_webview.start.assert_called_once_with(http_port=5176)


class TestWindowStateSave:
    """At ``closed``, pywebview's geometry getters unpack ``gui.get_size()``
    which returns ``None`` once the native window is gone — the state file
    must still be written instead of dying with ``cannot unpack non-iterable
    NoneType object``."""

    @staticmethod
    def _host_with_clean_state(tmp_path, monkeypatch, window):
        monkeypatch.setenv("ZAYTOON_HOME", str(tmp_path))
        fresh = WebViewHost(DesktopConfig())  # no state file -> empty previous
        fresh.window = window
        return fresh

    def test_geometry_that_raises_falls_back_to_defaults(self, tmp_path, monkeypatch):
        class _VanishingWindow:
            @property
            def width(self):
                raise TypeError("cannot unpack non-iterable NoneType object")

            @property
            def height(self):
                raise TypeError("cannot unpack non-iterable NoneType object")

            x = 10
            y = 20
            maximized = False

        fresh = self._host_with_clean_state(tmp_path, monkeypatch, _VanishingWindow())
        fresh._save_window_state()  # must not raise

        state = json.loads(
            (tmp_path / "window_state.json").read_text(encoding="utf-8")
        )
        from app.host import DEFAULT_WINDOW_HEIGHT, DEFAULT_WINDOW_WIDTH

        assert state["width"] == DEFAULT_WINDOW_WIDTH
        assert state["height"] == DEFAULT_WINDOW_HEIGHT
        assert state["x"] == 10  # readable fields survive

    def test_geometry_that_returns_none_falls_back_to_defaults(
        self, tmp_path, monkeypatch
    ):
        class _NoneGeometry:
            width = None
            height = None
            x = None
            y = None
            maximized = None

        fresh = self._host_with_clean_state(tmp_path, monkeypatch, _NoneGeometry())
        fresh._save_window_state()

        state = json.loads(
            (tmp_path / "window_state.json").read_text(encoding="utf-8")
        )
        assert state["x"] == 0
        assert state["y"] == 0
        assert state["maximized"] is False
