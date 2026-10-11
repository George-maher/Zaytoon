"""The desktop host's lifecycle promises, tested without a window.

`WebViewHost.run()` and `BackendProcess` carry the guarantees that decide whether
closing Zaytoon leaves anything behind. None of them needed a GUI to break, so
none of them need one to be verified: `webview` is faked, and the backend child is
a real process that spawns a real grandchild.

What is pinned here, in order of the defect each one answers:

1. A backend we started is killed **as a tree** — the shell's direct child is
   uvicorn, and a dev server the agent started through `process_start` is a
   grandchild that a bare `terminate()` leaves holding the port and the SQLite
   lock.
2. A backend we did **not** start is never signalled.
3. Shutdown is bounded and never raises: it runs from the window-close handler,
   and an escaping `TimeoutExpired` used to be a traceback during exit.
4. A failed start does not orphan the child: `import webview`, `create_window`
   and `webview.start()` all now sit inside a `finally` that tears down.
5. The health thread touches no UI object (its `evaluate_js` call was a
   foreign-thread UI call — survivable on WebView2, a hang or crash on GTK and
   Cocoa) and stops when asked.
6. State is saved where it can actually be written, without moving a
   development checkout's file.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import backend as backend_mod  # noqa: E402
from app import host as host_mod  # noqa: E402
from app.backend import BackendProcess  # noqa: E402
from app.config import default_state_dir  # noqa: E402


def _alive(pid: int) -> bool:
    """True when `pid` exists and is not a zombie we should not count."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - not our process
        return True
    return True


class _StubConfig:
    def __init__(self, host: str = "127.0.0.1", port: int = 59999) -> None:
        self.host = host
        self.port = port
        self.backend_addr = f"{host}:{port}"
        self.manage_backend = True
        self.backend_timeout = 1.0
        self.health_check_interval_s = 0.5
        self.state_dir = Path(os.environ.get("ZAYTOON_HOME", "/tmp"))
        self.toggle_hotkey = "ctrl+space"
        self.expand_hotkey = "ctrl+shift+space"
        self.always_on_top = True


class _ScriptedBackend(BackendProcess):
    """A real `BackendProcess` whose child is a script, not the FastAPI app.

    Subclassing rather than duck-typing matters here: the behaviour under test is
    spread across `shutdown` and its tree-signal helpers, and a stand-in object
    would exercise none of them.
    """

    def __init__(self, script: str) -> None:
        super().__init__(_StubConfig())
        self._child = subprocess.Popen(  # noqa: S603 - our own fixture script
            [sys.executable, "-c", script],
            start_new_session=(os.name != "nt"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._spawned_by_us = True
        #: `shutdown()` clears `_child`, so the pid is remembered at spawn time —
        #: these tests inspect the process after the owner has let go of it.
        self._pid = self._child.pid

    @property
    def pid(self):
        return self._pid


class TestTreeOwnership:
    @pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
    def test_a_grandchild_dies_with_the_backend(self, tmp_path):
        """The defect this exists for: SIGTERM to uvicorn left its children alive."""
        pidfile = tmp_path / "grandchild.pid"
        script = (
            "import subprocess, sys, time\n"
            f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            f"open({str(pidfile)!r}, 'w').write(str(child.pid))\n"
            "time.sleep(120)\n"
        )
        proc = _ScriptedBackend(script)
        deadline = time.monotonic() + 10
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pidfile.exists(), "the stub backend never started its child"
        grandchild = int(pidfile.read_text())
        deadline = time.monotonic() + 10
        while not _alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _alive(grandchild), "the grandchild was not running to begin with"

        child = proc._child
        proc.shutdown()

        assert child.poll() is not None, "the backend itself survived"
        deadline = time.monotonic() + 5
        while _alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(grandchild), (
            "the grandchild outlived the backend: the shell left a process holding "
            "the port and the database lock"
        )

    def test_a_backend_we_did_not_start_is_never_signalled(self):
        """`managed=False` is the safety property, not a status label."""
        proc = _ScriptedBackend("import time; time.sleep(20)")
        proc._spawned_by_us = False
        child = proc._child
        try:
            proc.shutdown()
            assert _alive(child.pid), "an unmanaged backend must not be touched"
        finally:
            child.kill()
            child.wait(timeout=10)

    @pytest.mark.skipif(
        os.name == "nt",
        reason=(
            "the POSIX signal contract: Windows cannot trap SIGTERM, and "
            "TerminateProcess is immediate, so there is no escalation to observe. "
            "The Windows branch is asserted by "
            "test_the_windows_branch_signals_the_tree, which runs everywhere."
        ),
    )
    def test_shutdown_is_bounded_and_never_raises(self, monkeypatch):
        """A SIGTERM-trapping child used to surface `TimeoutExpired` in `_on_closed`."""
        monkeypatch.setattr(backend_mod, "SHUTDOWN_GRACE_S", 0.3)
        monkeypatch.setattr(backend_mod, "KILL_WAIT_S", 0.3)
        # Traps SIGTERM so the graceful path must escalate; exits cleanly on kill.
        proc = _ScriptedBackend(
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, lambda *a: None)\n"
            "time.sleep(60)\n"
        )
        child = proc._child
        started = time.monotonic()
        proc.shutdown()  # must not raise
        elapsed = time.monotonic() - started
        assert elapsed < 15, f"shutdown took {elapsed:.1f}s; the bounds are not honoured"
        deadline = time.monotonic() + 5
        while _alive(child.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(child.pid), "a child that traps SIGTERM must still be killed"

    def test_the_windows_branch_signals_the_tree(self, monkeypatch):
        """`taskkill /T` on the polite step, `/F` only as escalation.

        Asserted from any platform by swapping the module's `subprocess` for a
        recorder: the *decision* — tree vs single child, polite vs force — is the
        thing that orphans backends when it is wrong, and a runner without a window
        can still check the argv. Reaching this code through a real signal is not
        possible on Linux, and pretending otherwise would be a fake GUI test.
        """
        recorded: list = []
        calls_kwargs: list = []

        def fake_run(argv, **kwargs):
            recorded.append(list(argv))
            calls_kwargs.append(kwargs)
            return types.SimpleNamespace(returncode=0)

        fake_subprocess = types.SimpleNamespace(
            run=fake_run,
            DEVNULL=-3,
            SubprocessError=Exception,
            Popen=subprocess.Popen,
        )
        monkeypatch.setattr(backend_mod, "subprocess", fake_subprocess)
        monkeypatch.setattr(
            backend_mod, "os", types.SimpleNamespace(name="nt", sep="\\", linesep="\r\n")
        )
        proc = backend_mod.BackendProcess(_StubConfig())
        child = types.SimpleNamespace(pid=4242)
        assert proc._signal_tree(child, force=False) is True
        assert recorded[0][:2] == ["taskkill", "/PID"] and "/T" in recorded[0]
        assert "/F" not in recorded[0], "the polite step must not force-kill"
        # The window that would otherwise flash on a desktop, and the bound that
        # keeps a hung `taskkill` from hanging window close with it.
        assert calls_kwargs[0]["timeout"], "the kill call must be bounded"
        assert proc._signal_tree(child, force=True) is True
        assert "/F" in recorded[1], "escalation has to reach the whole tree"

    def test_shutdown_swallows_any_failure_the_platform_throws(self, monkeypatch):
        """`shutdown()` is a `finally` and a window-close handler: it may not raise.

        The first version caught `(OSError, SubprocessError)`, and a `TypeError` from
        a `subprocess` object that did not match what the test thought it was — the
        exact shape of a real test artifact on the Windows leg — escaped it and
        turned a cleanup into a crash. Anything is a reason to give up signalling,
        not a reason to propagate.
        """
        def explode(*a, **k):
            raise RuntimeError("not an OS error at all")

        monkeypatch.setattr(
            backend_mod,
            "subprocess",
            types.SimpleNamespace(
                run=explode, DEVNULL=-3, SubprocessError=Exception, Popen=subprocess.Popen
            ),
        )
        proc = backend_mod.BackendProcess(_StubConfig())
        proc._child = types.SimpleNamespace(
            pid=4242, poll=lambda: None, terminate=explode, kill=explode, wait=explode
        )
        proc._spawned_by_us = True
        proc.shutdown()  # must return quietly

    def test_shutdown_is_idempotent(self):
        proc = _ScriptedBackend("import time; time.sleep(5)")
        proc.shutdown()
        proc.shutdown()  # the close handler and run()'s finally both call it
        assert proc._child is None


def _fake_webview(**overrides):
    """A `webview` module stand-in that records calls and can fail at any step."""
    class _Event:
        """The one bit of pywebview's `Event` the host uses: `+=` to attach."""

        def __init__(self) -> None:
            self.handlers: list = []

        def __iadd__(self, handler):
            self.handlers.append(handler)
            return self

    window = types.SimpleNamespace(
        events=types.SimpleNamespace(
            closed=_Event(),
            loaded=_Event(),
            minimized=_Event(),
            maximized=_Event(),
            restored=_Event(),
        ),
        # Any real attribute access is allowed; `evaluate_js` is asserted absent.
        evaluate_js=lambda *a, **k: pytest.fail("the health thread called into the UI"),
    )

    def create_window(*args, **kwargs):
        return overrides.get("window", window)

    return types.SimpleNamespace(
        create_window=create_window,
        start=overrides.get("start", lambda *a, **k: None),
    )


class _RecordingBackend:
    def __init__(self, config, *args, **kwargs) -> None:
        self.config = config
        self.shutdowns = 0
        self.spawned = True

    def ensure_running(self):
        return backend_mod.BackendStatus(running=True, managed=True, address="127.0.0.1:1")

    def status(self):
        return backend_mod.BackendStatus(running=True, address="127.0.0.1:1")

    def shutdown(self):
        self.shutdowns += 1


class TestFailedStartDoesNotOrphan:
    def _host(self, monkeypatch, tmp_path, **webview_kwargs):
        monkeypatch.setattr(host_mod, "BackendProcess", _RecordingBackend)
        monkeypatch.setenv("ZAYTOON_HOME", str(tmp_path))
        cfg = _StubConfig()
        cfg.dev_mode = True
        cfg.dev_port = 5173
        cfg.frontend_dist = tmp_path
        cfg.backend_addr = "127.0.0.1:59999"
        cfg.health_check_interval_s = 0.5
        h = host_mod.WebViewHost(cfg)
        module = webview_kwargs.pop("module", None)
        if module is not None:
            monkeypatch.setitem(sys.modules, "webview", module)
        else:
            monkeypatch.setitem(sys.modules, "webview", _fake_webview(**webview_kwargs))
        return h

    def test_a_webview_that_fails_to_import_still_shuts_the_backend_down(self, monkeypatch, tmp_path):
        """`import webview` raising is the common Linux failure (no GTK/WebKit)."""
        import builtins

        real_import = builtins.__import__

        def no_webview(name, *args, **kwargs):
            if name == "webview":
                raise ImportError("libwebkit2gtk not found")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_webview)
        h = self._host(monkeypatch, tmp_path, module=None)
        with pytest.raises(ImportError):
            h.run()
        assert h.backend.shutdowns == 1, "the spawned backend was left running"

    def test_a_window_that_cannot_be_created_still_shuts_the_backend_down(self, monkeypatch, tmp_path):
        """A `create_window` failure is the headless case: no display, no Wayland."""

        def boom(*args, **kwargs):
            raise RuntimeError("no display")

        h = self._host(
            monkeypatch,
            tmp_path,
            module=types.SimpleNamespace(create_window=boom, start=lambda *a, **k: None),
        )
        with pytest.raises(RuntimeError):
            h.run()
        assert h.backend.shutdowns == 1

    def test_a_failing_event_loop_still_shuts_the_backend_down(self, monkeypatch, tmp_path):
        def boom(*args, **kwargs):
            raise OSError("webview loop died")

        h = self._host(monkeypatch, tmp_path, start=boom)
        with pytest.raises(OSError):
            h.run()
        assert h.backend.shutdowns == 1

    def test_a_clean_close_shuts_it_down_exactly_once(self, monkeypatch, tmp_path):
        """The close handler and `run()`'s finally both tear down; no double kill."""
        h = self._host(monkeypatch, tmp_path)
        assert h.run() == 0
        assert h.backend.shutdowns == 1

    def test_run_is_single_shot(self, monkeypatch, tmp_path):
        h = self._host(monkeypatch, tmp_path)
        h.run()
        with pytest.raises(RuntimeError, match="single-shot"):
            h.run()


class TestHealthThread:
    def test_it_samples_without_touching_the_ui(self, monkeypatch, tmp_path):
        monkeypatch.setattr(host_mod, "backend_is_up", lambda host, port: True)
        cfg = _StubConfig()
        cfg.health_check_interval_s = 0.5
        h = host_mod.WebViewHost(cfg)
        # Recorded, not `pytest.fail` inside the callback: the loop wraps its body
        # in a try/except (correctly — a probe must not kill the thread), so an
        # exception raised there would be swallowed and the assertion would pass on
        # a regression. A list cannot be swallowed.
        seen: list = []
        h.window = types.SimpleNamespace(evaluate_js=lambda *a, **k: seen.append(a))
        h._start_health_check()
        time.sleep(0.7)
        assert h.backend_up is True
        assert seen == [], "the health thread reached into the webview from a foreign thread"
        h._shutting_down = True
        h._health_check_thread.join(timeout=2.0)
        assert not h._health_check_thread.is_alive(), (
            "the health thread outlived shutdown; it must observe the flag between "
            "slices, not sleep out a whole interval"
        )

    def test_the_interval_is_configuration_not_test_detection(self, monkeypatch, tmp_path):
        source = (Path(host_mod.__file__)).read_text(encoding="utf-8")
        assert '"pytest" in sys.modules' not in source, (
            "the host must not branch on whether pytest imported it; the interval "
            "is ZAYTOON_HEALTH_INTERVAL now"
        )


class TestStateLocation:
    def test_a_writable_repo_directory_keeps_the_old_behaviour(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ZAYTOON_HOME", raising=False)
        import app.config as cfg_mod

        monkeypatch.setattr(cfg_mod, "default_repo_root", lambda: tmp_path)
        assert default_state_dir() == tmp_path / "Config"

    def test_an_unusable_repo_falls_back_to_the_platform_directory(self, monkeypatch, tmp_path):
        """`<repo>/Config` is only the answer when it can *contain* a file.

        The mechanism is "something else owns the name", not "chmod says no": on
        Windows the file mode bits are advisory and the runner owns the tree, so a
        `chmod(0o500)` rehearsal of a Program-Files install succeeded on that leg
        and the test failed for a reason that had nothing to do with the product.
        A plain file where the directory should be is unwritable on all three
        platforms in the same way a real read-only install is.
        """
        import app.config as cfg_mod

        install = tmp_path / "Program Files" / "Zaytoon"
        install.mkdir(parents=True)
        (install / "Config").write_text("not a directory", encoding="utf-8")
        monkeypatch.delenv("ZAYTOON_HOME", raising=False)
        monkeypatch.setattr(cfg_mod, "default_repo_root", lambda: install)
        monkeypatch.setattr(
            cfg_mod, "sys", types.SimpleNamespace(platform="win32"), raising=False
        )
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
        assert default_state_dir() == tmp_path / "appdata" / "Zaytoon"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are advisory on Windows")
    def test_a_read_only_repo_falls_back_too(self, monkeypatch, tmp_path):
        import app.config as cfg_mod

        locked = tmp_path / "Program Files" / "Zaytoon"
        locked.mkdir(parents=True)
        locked.chmod(0o500)  # r-x: nothing can be created inside it
        try:
            monkeypatch.delenv("ZAYTOON_HOME", raising=False)
            monkeypatch.setattr(cfg_mod, "default_repo_root", lambda: locked)
            monkeypatch.setattr(
                cfg_mod, "sys", types.SimpleNamespace(platform="linux"), raising=False
            )
            monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
            assert default_state_dir() == tmp_path / "state" / "zaytoon"
        finally:
            locked.chmod(0o700)

    def test_zaytoon_home_wins_at_call_time(self, monkeypatch, tmp_path):
        cfg = _StubConfig()
        h = host_mod.WebViewHost.__new__(host_mod.WebViewHost)
        h.config = cfg
        monkeypatch.setenv("ZAYTOON_HOME", str(tmp_path / "elsewhere"))
        assert h._get_state_file() == tmp_path / "elsewhere" / "window_state.json"
