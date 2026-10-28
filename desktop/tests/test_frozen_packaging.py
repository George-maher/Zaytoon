"""The packaged application must work on a machine with no checkout.

These tests pin the pieces that only exist for the frozen build:

* ``Zaytoon.exe --serve`` — the backend-worker role. The bundle is ONE
  executable with two roles, and the backend must be imported by **path**
  (a plain ``import main`` would resolve to this very file when the shell
  runs as a script).
* ``_bootstrap_frozen_home`` — the per-user state layout applied before any
  backend module reads the environment, so an installed app never writes into
  its own (read-only) install directory.
* ``_diagnose`` — a verdict that fails only on real failures: FAIL sections
  produce exit 1, warnings do not.
* The packaging script's guarantees — the bundle carries the backend source
  and the production frontend, and never the checkout's ``.venv``, ``.env``
  or databases.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import os
import sys
import types
from pathlib import Path

import pytest

import main as main_mod
from app.config import DesktopConfig

DESKTOP_DIR = Path(__file__).resolve().parent.parent
BUILD_SCRIPT = DESKTOP_DIR / "scripts" / "build_exe.py"


def _load_build_script():
    """Import the build script directly (it uses only the stdlib at import)."""
    spec = importlib.util.spec_from_file_location("zaytoon_build_exe", BUILD_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# _bootstrap_frozen_home
# ---------------------------------------------------------------------------
class TestFrozenHomeBootstrap:
    def test_source_mode_changes_nothing(self, monkeypatch, tmp_path):
        """A development checkout keeps its ``backend/data`` behaviour."""
        monkeypatch.setattr(main_mod, "is_frozen", lambda: False)
        monkeypatch.setattr(main_mod, "default_state_dir", lambda: tmp_path)
        monkeypatch.delenv("ZAYTOON_HOME", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)

        main_mod._bootstrap_frozen_home()

        assert "ZAYTOON_HOME" not in os.environ
        assert "DATABASE_URL" not in os.environ

    def test_frozen_mode_points_state_at_the_platform_directory(self, monkeypatch, tmp_path):
        monkeypatch.setattr(main_mod, "is_frozen", lambda: True)
        monkeypatch.setattr(main_mod, "default_state_dir", lambda: tmp_path / "Zaytoon")
        monkeypatch.delenv("ZAYTOON_HOME", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)

        main_mod._bootstrap_frozen_home()

        assert os.environ["ZAYTOON_HOME"] == str(tmp_path / "Zaytoon")
        # Absolute on purpose: backend config resolves a relative SQLite URL
        # against the *process cwd*, which differs between the shell and the
        # worker it spawns.
        url = os.environ["DATABASE_URL"]
        assert url.startswith("sqlite:///")
        assert Path(url[len("sqlite:///"):]).is_absolute()
        assert Path(url[len("sqlite:///"):]).parent == tmp_path / "Zaytoon" / "Data"

    def test_it_is_idempotent_and_never_overrides_an_explicit_setting(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(main_mod, "is_frozen", lambda: True)
        monkeypatch.setattr(main_mod, "default_state_dir", lambda: tmp_path / "Zaytoon")
        monkeypatch.setenv("ZAYTOON_HOME", str(tmp_path / "deliberate"))
        monkeypatch.setenv("DATABASE_URL", "sqlite:///deliberate.db")

        main_mod._bootstrap_frozen_home()
        main_mod._bootstrap_frozen_home()  # a second call must be a no-op

        assert os.environ["ZAYTOON_HOME"] == str(tmp_path / "deliberate")
        assert os.environ["DATABASE_URL"] == "sqlite:///deliberate.db"


# ---------------------------------------------------------------------------
# --serve: the backend-worker role
# ---------------------------------------------------------------------------
_BACKEND_STUB = '''
class _Config:
    BACKEND_HOST = "127.0.0.1"
    BACKEND_PORT = 9999


config = _Config()
app = "the-app-object"
'''

_BACKEND_STUB_NO_CONFIG = 'app = "the-app-object"\n'


def _repo_with_backend(tmp_path: Path, source: str = _BACKEND_STUB) -> Path:
    repo = tmp_path / "repo"
    (repo / "backend").mkdir(parents=True)
    (repo / "backend" / "main.py").write_text(source, encoding="utf-8")
    return repo


@pytest.fixture
def fake_uvicorn(monkeypatch):
    """A stand-in for ``uvicorn`` that records the ``run`` call instead of serving."""
    module = types.ModuleType("uvicorn")
    calls: dict = {}

    def run(app, **kwargs):
        calls["app"] = app
        calls.update(kwargs)

    module.run = run
    monkeypatch.setitem(sys.modules, "uvicorn", module)
    return calls


@pytest.fixture
def restore_cwd():
    """``_serve_backend`` chdirs into the backend directory; put it back."""
    original = os.getcwd()
    yield
    os.chdir(original)


@pytest.fixture(autouse=True)
def _restore_frozen_env():
    """``_bootstrap_frozen_home`` writes ZAYTOON_HOME/DATABASE_URL straight into
    ``os.environ`` (deliberately, before the backend reads it), so a frozen-mode
    test would otherwise leak those into every later test in the session —
    ``test_host.py``'s state-file fallback then resolves to the leaked dir."""
    saved = {k: os.environ.get(k) for k in ("ZAYTOON_HOME", "DATABASE_URL")}
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


class TestServeBackend:
    def test_the_backend_is_imported_by_path_not_by_name(
        self, monkeypatch, tmp_path, fake_uvicorn, restore_cwd
    ):
        """A plain ``import main`` would resolve to the shell's own ``main.py``.

        The worker runs while the shell module is already named ``main``, so
        loading the backend by path is what keeps the two apart — the stub's
        ``app`` object proves which file was executed.
        """
        repo = _repo_with_backend(tmp_path)
        monkeypatch.setattr(main_mod, "is_frozen", lambda: False)
        monkeypatch.setattr(main_mod, "default_repo_root", lambda: repo)
        monkeypatch.delenv("ZAYTOON_BACKEND_ADDR", raising=False)

        rc = main_mod._serve_backend()

        assert rc == 0
        assert fake_uvicorn["app"] == "the-app-object"
        assert fake_uvicorn["host"] == "127.0.0.1"
        assert fake_uvicorn["port"] == 9999
        assert fake_uvicorn["reload"] is False

    def test_a_missing_backend_entry_point_is_reported_not_crashed(
        self, monkeypatch, tmp_path, restore_cwd
    ):
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setattr(main_mod, "is_frozen", lambda: False)
        monkeypatch.setattr(main_mod, "default_repo_root", lambda: empty)

        assert main_mod._serve_backend() == 3

    def test_a_shell_address_override_reaches_the_backend_environment(
        self, monkeypatch, tmp_path, fake_uvicorn, restore_cwd
    ):
        """The shell may sit on a non-default port; the worker must follow."""
        repo = _repo_with_backend(tmp_path, _BACKEND_STUB_NO_CONFIG)
        monkeypatch.setattr(main_mod, "is_frozen", lambda: False)
        monkeypatch.setattr(main_mod, "default_repo_root", lambda: repo)
        monkeypatch.setenv("ZAYTOON_BACKEND_ADDR", "127.0.0.1:9123")
        monkeypatch.delenv("BACKEND_HOST", raising=False)
        monkeypatch.delenv("BACKEND_PORT", raising=False)

        rc = main_mod._serve_backend()

        assert rc == 0
        # The stub declares no config, so the fallback path is exercised: the
        # translated environment is what the worker falls back to.
        assert os.environ["BACKEND_HOST"] == "127.0.0.1"
        assert os.environ["BACKEND_PORT"] == "9123"
        assert fake_uvicorn["port"] == 8000  # the stub's default, not the env

    def test_an_explicit_backend_port_still_wins_over_the_translation(
        self, monkeypatch, tmp_path, fake_uvicorn, restore_cwd
    ):
        repo = _repo_with_backend(tmp_path, _BACKEND_STUB_NO_CONFIG)
        monkeypatch.setattr(main_mod, "is_frozen", lambda: False)
        monkeypatch.setattr(main_mod, "default_repo_root", lambda: repo)
        monkeypatch.setenv("ZAYTOON_BACKEND_ADDR", "127.0.0.1:9123")
        monkeypatch.setenv("BACKEND_PORT", "7777")  # set deliberately: it wins

        main_mod._serve_backend()

        assert os.environ["BACKEND_PORT"] == "7777"

    def test_frozen_worker_reads_the_bundle_backend(
        self, monkeypatch, tmp_path, fake_uvicorn, restore_cwd
    ):
        bundle = tmp_path / "_internal"
        (bundle / "backend").mkdir(parents=True)
        (bundle / "backend" / "main.py").write_text(_BACKEND_STUB, encoding="utf-8")
        monkeypatch.setattr(main_mod, "is_frozen", lambda: True)
        monkeypatch.setattr(main_mod, "bundle_dir", lambda: bundle)

        rc = main_mod._serve_backend()

        assert rc == 0
        assert fake_uvicorn["app"] == "the-app-object"


# ---------------------------------------------------------------------------
# _diagnose verdict logic
# ---------------------------------------------------------------------------
def _quiet_config(tmp_path: Path, *, repo_root: Path, frontend_dist: Path) -> DesktopConfig:
    """A DesktopConfig with every filesystem location pinned under tmp_path."""
    cfg = DesktopConfig()
    cfg.repo_root = repo_root
    cfg.frontend_dist = frontend_dist
    cfg.state_dir = tmp_path / "state"
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.backend_addr = "127.0.0.1:8000"
    return cfg


@pytest.fixture
def healthy_environment(monkeypatch):
    """Everything that is not the thing under test answers as healthy."""
    monkeypatch.setattr(main_mod, "webview2_runtime_present", lambda: True)
    monkeypatch.setattr(main_mod, "backend_is_up", lambda *a, **k: False)
    monkeypatch.setattr(main_mod, "_probe_health", lambda *a, **k: None)
    monkeypatch.delenv("ZAYTOON_DEV_MODE", raising=False)


class TestDiagnoseVerdict:
    def test_warnings_do_not_fail_the_run(self, monkeypatch, tmp_path, healthy_environment, capsys):
        """A backend that is merely DOWN (but resolvable) is a warning, not a verdict."""
        repo = _repo_with_backend(tmp_path)
        cfg = _quiet_config(tmp_path, repo_root=repo, frontend_dist=tmp_path / "no-dist")

        rc = main_mod._diagnose(cfg)

        out = capsys.readouterr().out
        assert rc == 0
        assert "verdict: ready with" in out
        assert "warning" in out

    def test_a_failed_check_fails_the_run(self, monkeypatch, tmp_path, healthy_environment, capsys):
        """No backend anywhere: the installed app could not start itself."""
        empty = tmp_path / "empty"
        empty.mkdir()
        cfg = _quiet_config(tmp_path, repo_root=empty, frontend_dist=tmp_path / "no-dist")

        rc = main_mod._diagnose(cfg)

        out = capsys.readouterr().out
        assert rc == 1
        assert "verdict: NOT READY" in out

    def test_a_start_backend_failure_is_reported_and_still_fails(
        self, monkeypatch, tmp_path, healthy_environment, capsys
    ):
        """--start-backend that cannot produce a healthy backend is a FAIL."""
        repo = _repo_with_backend(tmp_path)
        cfg = _quiet_config(tmp_path, repo_root=repo, frontend_dist=tmp_path / "no-dist")
        cfg.backend_timeout = 0.1

        class _Never:
            def ensure_running(self):
                from app.backend import BackendStatus

                return BackendStatus(running=False, hint="spawn refused (test)")

            def shutdown(self):
                pass

            log_path = tmp_path / "backend.log"

        monkeypatch.setattr(main_mod, "BackendProcess", lambda config: _Never())

        rc = main_mod._diagnose(cfg, start_backend=True)

        out = capsys.readouterr().out
        assert rc == 1
        assert "failed to start" in out
        assert "spawn refused (test)" in out


# ---------------------------------------------------------------------------
# _check_frontend_assets — the blank-window detector
# ---------------------------------------------------------------------------
class TestFrontendAssetCheck:
    def _write_dist(self, tmp_path: Path, html: str) -> Path:
        dist = tmp_path / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text(html, encoding="utf-8")
        return dist

    def test_a_complete_build_passes(self, tmp_path):
        dist = self._write_dist(
            tmp_path,
            '<html><script src="/assets/app.js"></script>'
            '<link href="/assets/style.css" rel="stylesheet"></html>',
        )
        (dist / "assets" / "app.js").write_text("", encoding="utf-8")
        (dist / "assets" / "style.css").write_text("", encoding="utf-8")

        status, text = main_mod._check_frontend_assets(dist, frozen=True)

        assert status == "PASS"
        assert "production build" in text

    def test_a_missing_referenced_asset_is_caught(self, tmp_path):
        """The window renders blank with no error anywhere — this must FAIL."""
        dist = self._write_dist(
            tmp_path, '<html><script src="/assets/app-abc123.js"></script></html>'
        )

        status, text = main_mod._check_frontend_assets(dist, frozen=True)

        assert status == "FAIL"
        assert "app-abc123.js" in text
        assert "blank window" in text

    def test_external_and_inline_refs_are_not_files(self, tmp_path):
        dist = self._write_dist(
            tmp_path,
            '<html><script src="https://cdn.example.com/x.js"></script>'
            '<link href="data:text/css,"><a href="#top"></a></html>',
        )

        status, _ = main_mod._check_frontend_assets(dist, frozen=True)

        assert status == "PASS"

    def test_absent_dist_is_a_warning_in_source_mode_and_a_failure_frozen(self, tmp_path):
        missing = tmp_path / "no-dist"

        assert main_mod._check_frontend_assets(missing, frozen=False)[0] == "WARN"
        assert main_mod._check_frontend_assets(missing, frozen=True)[0] == "FAIL"

    def test_a_dist_without_index_html_is_not_a_build(self, tmp_path):
        empty = tmp_path / "empty-dist"
        empty.mkdir()

        status, text = main_mod._check_frontend_assets(empty, frozen=True)

        assert status == "FAIL"
        assert "index.html" in text


# ---------------------------------------------------------------------------
# the packaging script's guarantees (static — they run on any host)
# ---------------------------------------------------------------------------
class TestBuildScriptGuarantees:
    def test_the_wholesale_backend_copy_is_gone(self):
        """It copied .venv (0.4 GB) and .env (the API key) into the bundle."""
        source = BUILD_SCRIPT.read_text(encoding="utf-8")
        assert "_ensure_backend_and_frontend_in_dist" not in source

    def test_the_frontend_is_built_by_the_script(self):
        """Shipping whatever happened to be in dist/ was the blank-window bug."""
        source = BUILD_SCRIPT.read_text(encoding="utf-8")
        assert "npm run build" in source
        assert "npm ci" in source

    def test_data_payloads_are_added_to_the_bundle(self):
        source = BUILD_SCRIPT.read_text(encoding="utf-8")
        assert "--add-data" in source
        assert "backend" in source and "frontend" in source

    def test_the_bundle_carries_a_marker_and_checks_for_leaks(self):
        source = BUILD_SCRIPT.read_text(encoding="utf-8")
        assert "zaytoon_bundle.json" in source
        assert "LEAK" in source

    def test_onedir_is_the_default(self, tmp_path):
        """A onefile build re-extracts hundreds of MB on every launch."""
        module = _load_build_script()
        assert inspect.signature(module.build).parameters["onedir"].default is True

    def test_the_checkout_secrets_and_interpreter_are_excluded(self):
        module = _load_build_script()
        assert ".venv" in module.BACKEND_SKIP_TOP
        assert "data" in module.BACKEND_SKIP_TOP, "backend/data holds the user's databases"
        assert ".env" in module.SKIP_FILES
        assert "secrets.json" in module.SKIP_FILES
        assert ".pyc" in module.SKIP_SUFFIXES

    def test_hidden_imports_stay_the_app_closure(self):
        """Third-party entries live in their own list (see test_packaging_imports)."""
        module = _load_build_script()
        for name in module.HIDDEN_IMPORTS:
            assert name == "webview" or name == "app" or name.startswith("app."), name

    def test_stage_tree_actually_filters(self, tmp_path):
        """The exclusion rules must apply to a real tree, not just exist."""
        module = _load_build_script()
        src = tmp_path / "backend"
        (src / ".venv" / "Lib").mkdir(parents=True)
        (src / ".venv" / "Lib" / "heavy.py").write_text("", encoding="utf-8")
        (src / "data").mkdir()
        (src / "data" / "zaytoon.db").write_bytes(b"sqlite")
        (src / "data" / "secrets.json").write_text("{}", encoding="utf-8")
        (src / "__pycache__").mkdir()
        (src / "__pycache__" / "mod.cpython-311.pyc").write_bytes(b"\x00")
        (src / ".env").write_text("OPENROUTER_API_KEY=secret", encoding="utf-8")
        (src / "sub").mkdir()
        (src / "sub" / "__pycache__").mkdir()
        (src / "sub" / "__pycache__" / "x.pyc").write_bytes(b"\x00")
        (src / "main.py").write_text("app = object()\n", encoding="utf-8")
        (src / "sub" / "helper.py").write_text("", encoding="utf-8")

        dst = tmp_path / "staged"
        count = module.stage_tree(src, dst, skip_top=module.BACKEND_SKIP_TOP)

        staged = sorted(str(p.relative_to(dst)) for p in dst.rglob("*") if p.is_file())
        assert staged == ["main.py", str(Path("sub") / "helper.py")]
        assert count == 2
        # The secrets are not merely unreferenced: they were never copied.
        assert not (dst / ".env").exists()
        assert not (dst / ".venv").exists()
        assert not (dst / "data").exists()

    def test_the_env_var_the_docs_mention_is_actually_read(self):
        """`ZAYTOON_REPO_ROOT` is a support override; the build must honour it."""
        source = BUILD_SCRIPT.read_text(encoding="utf-8")
        assert "ZAYTOON_REPO_ROOT" in source

    def test_the_spec_is_syntactically_valid_python(self):
        ast.parse(BUILD_SCRIPT.read_text(encoding="utf-8"))
