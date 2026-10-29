"""Tests for the native bridge (app/bridge.py).

These tests verify that the bridge:
- Exposes only explicitly registered methods (no arbitrary Python execution)
- Handles window controls correctly
- Provides OS integration (reveal, open, notify)
- Manages backend status
- Handles clipboard operations
- Degrades gracefully when the window is not available
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.bridge import NativeBridge
from app.backend import BackendProcess
from app.config import DesktopConfig


@pytest.fixture
def config():
    """Create a test config."""
    cfg = DesktopConfig()
    cfg.repo_root = Path(__file__).resolve().parent.parent.parent
    return cfg


@pytest.fixture
def backend(config):
    """Create a backend process for testing."""
    return BackendProcess(config)


@pytest.fixture
def bridge(config, backend):
    """Create a native bridge for testing."""
    return NativeBridge(config, backend)


@pytest.fixture
def mock_window():
    """Create a mock window object."""
    window = MagicMock()
    window.maximized = False
    window.width = 1280
    window.height = 800
    window.x = 100
    window.y = 100
    return window


class TestBridgeInitialization:
    def test_bridge_holds_config_and_backend(self, bridge, config, backend):
        assert bridge.config is config
        assert bridge.backend is backend

    def test_bridge_window_initially_none(self, bridge):
        assert bridge._window is None

    def test_set_window_stores_reference(self, bridge, mock_window):
        bridge.set_window(mock_window)
        assert bridge._window is mock_window


class TestWindowControls:
    def test_minimize_calls_window_minimize(self, bridge, mock_window):
        bridge.set_window(mock_window)
        bridge.minimize()
        mock_window.minimize.assert_called_once()

    def test_maximize_calls_window_maximize(self, bridge, mock_window):
        bridge.set_window(mock_window)
        bridge.maximize()
        mock_window.maximize.assert_called_once()

    def test_restore_calls_window_restore(self, bridge, mock_window):
        bridge.set_window(mock_window)
        bridge.restore()
        mock_window.restore.assert_called_once()

    def test_close_calls_window_destroy(self, bridge, mock_window):
        bridge.set_window(mock_window)
        bridge.close()
        mock_window.destroy.assert_called_once()

    def test_is_maximized_returns_false_when_not_maximized(self, bridge, mock_window):
        bridge.set_window(mock_window)
        mock_window.maximized = False
        assert bridge.is_maximized() is False

    def test_is_maximized_returns_true_when_maximized(self, bridge, mock_window):
        bridge.set_window(mock_window)
        mock_window.maximized = True
        assert bridge.is_maximized() is True

    def test_window_controls_noop_when_no_window(self, bridge):
        # Should not raise
        bridge.minimize()
        bridge.maximize()
        bridge.restore()
        bridge.close()
        assert bridge.is_maximized() is False

    def test_get_window_state_returns_empty_when_no_window(self, bridge):
        assert bridge.get_window_state() == {}

    def test_get_window_state_returns_geometry(self, bridge, mock_window):
        bridge.set_window(mock_window)
        state = bridge.get_window_state()
        assert state["width"] == 1280
        assert state["height"] == 800
        assert state["x"] == 100
        assert state["y"] == 100
        assert state["maximized"] is False


class TestFileSystemOperations:
    def test_reveal_in_os_returns_false_for_empty_path(self, bridge):
        assert bridge.reveal_in_os("") is False

    def test_reveal_in_os_returns_false_for_nonexistent_path(self, bridge):
        assert bridge.reveal_in_os("/nonexistent/path/12345") is False

    def test_open_path_returns_false_for_empty_path(self, bridge):
        assert bridge.open_path("") is False

    def test_open_path_returns_false_for_nonexistent_path(self, bridge):
        assert bridge.open_path("/nonexistent/path/12345") is False

    def test_open_folder_dialog_returns_none_when_no_window(self, bridge):
        assert bridge.open_folder_dialog() is None

    def test_open_file_dialog_returns_none_when_no_window(self, bridge):
        assert bridge.open_file_dialog() is None


class TestSystemInfo:
    def test_get_system_info_returns_required_keys(self, bridge):
        info = bridge.get_system_info()
        assert "platform" in info
        assert "os" in info
        assert "os_version" in info
        assert "architecture" in info
        assert "python_version" in info
        assert "hostname" in info

    def test_get_app_path_returns_repo_root(self, bridge, config):
        assert bridge.get_app_path() == str(config.repo_root)

    def test_get_backend_url_returns_ws_url(self, bridge, config):
        assert bridge.get_backend_url() == config.ws_url


class TestNotifications:
    def test_notify_returns_true_on_success(self, bridge):
        # Mock the platform-specific notification
        with patch.object(bridge, "_notify_windows", return_value=None):
            with patch("sys.platform", "win32"):
                assert bridge.notify("Test", "Message") is True

    def test_notify_returns_false_on_failure(self, bridge):
        with patch.object(bridge, "_notify_windows", side_effect=Exception("fail")):
            with patch("sys.platform", "win32"):
                assert bridge.notify("Test", "Message") is False


class TestBackendManagement:
    def test_get_backend_status_returns_dict(self, bridge):
        status = bridge.get_backend_status()
        assert isinstance(status, dict)
        assert "running" in status
        assert "managed" in status
        assert "hint" in status
        assert "address" in status
        assert "pid" in status

    def test_restart_backend_returns_status(self, bridge):
        with patch.object(bridge.backend, "shutdown"):
            with patch.object(bridge.backend, "ensure_running") as mock_ensure:
                mock_ensure.return_value = MagicMock(as_dict=lambda: {"running": True})
                status = bridge.restart_backend()
                assert status["running"] is True


class TestClipboard:
    def test_get_clipboard_returns_string_on_failure(self, bridge):
        # Should return empty string on failure, not raise
        result = bridge.get_clipboard()
        assert isinstance(result, str)

    def test_set_clipboard_returns_bool(self, bridge):
        # Should return True or False, not raise
        result = bridge.set_clipboard("test")
        assert isinstance(result, bool)

    @pytest.mark.skipif(sys.platform != "win32", reason="real clipboard test")
    def test_clipboard_round_trip_preserves_the_text(self, bridge):
        """Set then get must return the same text.

        The type-only tests above stayed green while ``set_clipboard`` wrote
        *uninitialised memory* to the clipboard and ``get_clipboard`` read
        CF_TEXT as if it were UTF-16 — both were broken in opposite ways, so
        only a round trip can catch the pair.
        """
        original = bridge.get_clipboard()
        marker = "Zaytoon round trip — مرحبًا 🙂"
        try:
            if not bridge.set_clipboard(marker):
                # The OS refused the clipboard (locked or a non-interactive
                # session). That is an environment fact, not a content bug —
                # a wrong write would still be caught below whenever the
                # clipboard opens at all.
                pytest.skip("clipboard not available in this session")
            assert bridge.get_clipboard() == marker
        finally:
            if original:
                bridge.set_clipboard(original)


class TestNotificationSafety:
    """The notification fallbacks must not let text become code."""

    def test_ps_single_quote_escapes_the_only_special_character(self):
        from app.bridge import _ps_single_quote

        assert _ps_single_quote("it's") == "it''s"
        # Everything else is literal inside a single-quoted PS string.
        assert _ps_single_quote('$(`")') == '$(`")'

    def test_windows_fallback_sends_an_encoded_command_not_a_command_line(
        self, bridge, monkeypatch
    ):
        """A quote in a notification must not become PowerShell syntax.

        The fallback used to build ``-Command f'...{message}...'`` directly,
        so ``"); <code>; ("`` in a title was executable. The payload now
        travels as base64 ``-EncodedCommand`` and is escaped for a
        single-quoted literal on the way in.
        """
        import ctypes
        import subprocess as sp

        class _BrokenWindll:
            user32 = None  # attribute access on None raises -> fallback path

        # `raising=False` because `ctypes.windll` is a Windows-only attribute:
        # without it this test of a Windows code path errored on Linux and macOS
        # instead of exercising the fallback it describes.
        monkeypatch.setattr(ctypes, "windll", _BrokenWindll(), raising=False)

        captured: list[list[str]] = []
        monkeypatch.setattr(sp, "Popen", lambda argv, **kw: captured.append(argv))

        payload = 'x"); Remove-Item C:\\*; it\'s ("'
        bridge._notify_windows(payload, "title")

        assert captured, "the PowerShell fallback was never invoked"
        argv = captured[0]
        assert argv[0] == "powershell"
        assert "-EncodedCommand" in argv
        # No user text may appear on the command line at all.
        assert not any(payload in str(part) for part in argv)

        encoded = argv[argv.index("-EncodedCommand") + 1]
        script = base64.b64decode(encoded).decode("utf-16-le")
        # The payload sits inside a single-quoted literal, apostrophes doubled.
        assert "Remove-Item" in script
        assert "it''s" in script
        # The injection's quote pair is data, never a string terminator:
        # exactly one quoted literal follows the ::Show( call.
        after_show = script.split("::Show(", 1)[1]
        assert after_show.startswith("'")

    def test_macos_notification_escapes_quotes(self, bridge, monkeypatch):
        import subprocess as sp

        captured: list[list[str]] = []
        monkeypatch.setattr(sp, "Popen", lambda argv, **kw: captured.append(argv))
        bridge._notify_macos('He said "hi"', "title")
        assert captured
        script = captured[0][captured[0].index("-e") + 1]
        # A raw quote would terminate the AppleScript string; it must be \".
        assert '\\"hi\\"' in script


class TestSecurity:
    """Security-critical tests: the bridge must not expose arbitrary execution."""

    def test_no_eval_or_exec_in_bridge(self):
        """The bridge must not contain eval or exec."""
        import inspect

        source = inspect.getsource(NativeBridge)
        assert "eval(" not in source
        assert "exec(" not in source
        assert "__import__" not in source

    def test_no_subprocess_shell_true_in_bridge(self):
        """The bridge must not use shell=True in subprocess calls."""
        import inspect

        source = inspect.getsource(NativeBridge)
        # Check that shell=True is not used
        assert "shell=True" not in source

    def test_bridge_methods_are_explicitly_defined(self):
        """Every public method must be explicitly defined on the class."""
        import inspect

        methods = [
            name
            for name, _ in inspect.getmembers(NativeBridge, predicate=inspect.isfunction)
            if not name.startswith("_")
        ]
        expected = {
            "set_window",
            "minimize",
            "maximize",
            "restore",
            "close",
            "is_maximized",
            "set_always_on_top",
            "get_window_state",
            "open_folder_dialog",
            "open_file_dialog",
            "reveal_in_os",
            "open_path",
            "get_system_info",
            "get_app_path",
            "get_backend_url",
            "notify",
            "open_url",
            "get_backend_status",
            "restart_backend",
            "get_clipboard",
            "set_clipboard",
            "search_items",
            "get_indexed_item",
            "get_indexed_roots",
            "get_indexed_exclusions",
            "launch_item",
            "set_host",
            "get_host_state",
        }
        assert set(methods) == expected

    def test_no_arbitrary_python_execution(self):
        """The bridge must not accept Python code as a string parameter."""
        import inspect

        for name, method in inspect.getmembers(NativeBridge, predicate=inspect.isfunction):
            if name.startswith("_"):
                continue
            sig = inspect.signature(method)
            for param_name, param in sig.parameters.items():
                # No parameter should be named in a way that suggests code execution
                assert param_name not in ("code", "expression", "script", "command"), (
                    f"Bridge method {name} has suspicious parameter: {param_name}"
                )
