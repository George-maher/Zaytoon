"""The portable layout: one answer per location, and honest answers.

The installer's promise is that an application update replaces ``Application/``
and nothing else. That promise is only kept if the code *finds* ``Data/`` and
``Config/`` in a deployed install, and — just as importantly — keeps its existing
behaviour in a development checkout. Those are different worlds and the resolver
has to tell them apart without being told which one it is in.

So the assertions here are about **precedence** and about the ``None`` answers,
not about paths in the abstract. ``None`` is meaningful: a checkout has no
separate ``Config/``, and a function that returned a plausible-looking path
instead would send someone looking for a file that does not exist.
"""

from __future__ import annotations

import os

import pytest

from core import paths


@pytest.fixture
def portable(tmp_path, monkeypatch):
    """Pretend to be installed under ``ZAYTOON_HOME``."""
    home = tmp_path / "Zaytoon"
    home.mkdir()
    for name in paths.LAYOUT:
        (home / name).mkdir()
    monkeypatch.setenv("ZAYTOON_HOME", str(home))
    return home


@pytest.fixture
def checkout(monkeypatch):
    """Pretend to be a source checkout: no ``ZAYTOON_HOME``."""
    monkeypatch.delenv("ZAYTOON_HOME", raising=False)
    monkeypatch.delenv("ZAYTOON_DATA_DIR", raising=False)
    monkeypatch.delenv("ZAYTOON_CONFIG_DIR", raising=False)
    return None


class TestDetection:
    def test_no_home_means_a_checkout(self, checkout):
        assert paths.zaytoon_home() is None

    def test_home_is_found_when_set(self, portable):
        assert paths.zaytoon_home() == portable.resolve()

    def test_home_is_expanded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ZAYTOON_HOME", "$SOMEWHERE/Zaytoon")
        monkeypatch.setenv("SOMEWHERE", str(tmp_path))
        assert paths.zaytoon_home() is not None
        assert str(paths.zaytoon_home()).startswith(str(tmp_path))

    def test_an_empty_home_is_no_home(self, monkeypatch):
        # An exported-but-empty variable is what a launcher's `${VAR:-}` leaves
        # behind on some shells. Treating it as an install root would point
        # everything at the current directory.
        monkeypatch.setenv("ZAYTOON_HOME", "")
        assert paths.zaytoon_home() is None

    def test_the_layout_is_the_documented_set(self):
        assert set(paths.LAYOUT) == {
            "Config", "Data", "Models", "Skills", "Workspaces", "Logs", "Cache",
        }
        # Application/ is where the code lives, not something the app looks for.
        assert "Application" not in paths.LAYOUT


class TestPrecedence:
    def test_an_explicit_override_beats_the_layout(self, portable, monkeypatch, tmp_path):
        chosen = tmp_path / "elsewhere"
        monkeypatch.setenv("ZAYTOON_DATA_DIR", str(chosen))
        assert paths.data_dir() == chosen

    def test_the_layout_beats_the_checkout_default(self, portable):
        assert paths.data_dir() == portable / "Data"

    def test_the_checkout_default_is_beside_the_backend(self, checkout):
        # Not the cwd: the desktop shell spawns the backend with its own cwd,
        # so a cwd-based path would give a second, empty data directory.
        assert paths.data_dir().name == "data"
        assert paths.data_dir().parent.name == "backend"

    def test_data_dir_is_a_path_not_a_string(self, portable):
        assert isinstance(paths.data_dir(), type(portable))


class TestHonestNones:
    def test_a_checkout_has_no_config_directory(self, checkout):
        assert paths.config_dir() is None

    def test_a_portable_install_does(self, portable):
        assert paths.config_dir() == portable / "Config"

    def test_a_checkout_has_no_separate_workspace_default(self, checkout):
        # The checkout default is ~/ZaytoonWorkspace, decided by config.py. A
        # path returned here would silently change it.
        assert paths.workspaces_dir() is None

    def test_a_portable_install_has_one(self, portable):
        assert paths.workspaces_dir() == portable / "Workspaces"

    def test_a_checkout_has_no_install_wide_skills_dir(self, checkout):
        assert paths.skills_dir() is None

    def test_a_portable_install_does(self, portable):
        assert paths.skills_dir() == portable / "Skills"

    def test_logs_and_models_always_resolve(self, checkout, portable):
        # These two have no "none" answer: a checkout logs next to its data.
        assert paths.logs_dir() and paths.models_dir()
        assert paths.logs_dir() == portable / "Logs"


class TestSideEffects:
    def test_resolution_creates_nothing(self, checkout, tmp_path, monkeypatch):
        """An import-time health check must not touch the filesystem."""
        monkeypatch.setenv("ZAYTOON_DATA_DIR", str(tmp_path / "never-made"))
        assert paths.data_dir() == tmp_path / "never-made"
        assert not (tmp_path / "never-made").exists()

    def test_ensure_creates(self, tmp_path):
        target = tmp_path / "made" / "deeper"
        assert paths.ensure(target) == target
        assert target.is_dir()

    def test_ensure_is_idempotent(self, tmp_path):
        target = paths.ensure(tmp_path / "x")
        (target / "file").write_text("x", encoding="utf-8")
        paths.ensure(target)
        assert (target / "file").exists(), "ensure must not clear the directory"


class TestDescribe:
    def test_it_reports_a_portable_install(self, portable):
        described = paths.describe()
        assert described["portable"] is True
        assert described["home"] == str(portable.resolve())
        assert described["data"] == str(portable / "Data")

    def test_it_reports_a_checkout(self, checkout):
        described = paths.describe()
        assert described["portable"] is False
        assert described["home"] is None
        # None, not a guess.
        assert described["config"] is None

    def test_every_path_is_a_string_or_none(self, portable):
        # `/system/status` is JSON, and this is what a user reads to find their
        # data. No exception, no missing key. `portable` is the one deliberate
        # boolean — the rest are paths.
        described = paths.describe()
        assert isinstance(described["portable"], bool)
        for key, value in described.items():
            if key == "portable":
                continue
            assert value is None or isinstance(value, str), key


class TestConsumers:
    def test_the_core_database_follows_the_layout(self, portable, monkeypatch):
        monkeypatch.delenv("CORE_DB_PATH", raising=False)
        from core import db

        assert db.db_path() == os.path.join(str(portable / "Data"), "core.db")

    def test_a_database_override_still_wins(self, tmp_path, monkeypatch):
        chosen = tmp_path / "explicit.db"
        monkeypatch.setenv("CORE_DB_PATH", str(chosen))
        from core import db

        assert db.db_path() == str(chosen)

    def test_the_checkout_database_is_still_beside_the_backend(self, checkout, monkeypatch):
        monkeypatch.delenv("CORE_DB_PATH", raising=False)
        from core import db

        assert db.db_path().endswith(os.path.join("backend", "data", "core.db"))

    def test_the_audit_database_follows_the_layout(self, portable, monkeypatch):
        monkeypatch.delenv("AUDIT_DB_PATH", raising=False)
        from common import audit

        assert audit._db_path().endswith(os.path.join("Data", "audit.db"))

    def test_the_audit_database_does_not_join_the_core_one(self, portable, monkeypatch):
        """The lesson `common/audit.py` already learned, applied to the layout.

        Different files, different connections. Sharing one would reintroduce the
        `database is locked` failure that made the audit page unusable.
        """
        monkeypatch.delenv("AUDIT_DB_PATH", raising=False)
        monkeypatch.delenv("CORE_DB_PATH", raising=False)
        from common import audit
        from core import db

        assert audit._db_path() != db.db_path()

    def test_secrets_live_in_config_on_a_portable_install(self, portable, monkeypatch):
        monkeypatch.delenv("ZAYTOON_SECRETS_PATH", raising=False)
        from core import secrets

        assert secrets.secrets_path() == str(portable / "Config" / "secrets.json")

    def test_secrets_keep_their_checkout_location(self, checkout, monkeypatch):
        """Not relocated: an existing install must not appear to lose its keys."""
        monkeypatch.delenv("ZAYTOON_SECRETS_PATH", raising=False)
        from core import secrets

        assert secrets.secrets_path().endswith(
            os.path.join("backend", "data", "secrets.json")
        )

    def test_skills_follow_the_layout(self, portable, monkeypatch):
        monkeypatch.delenv("ZAYTOON_SKILLS_DIR", raising=False)
        from core.skills import registry

        assert registry.skills_root() == str(portable / "Skills")

    def test_a_skills_override_still_wins(self, tmp_path, monkeypatch):
        chosen = tmp_path / "my-skills"
        monkeypatch.setenv("ZAYTOON_SKILLS_DIR", str(chosen))
        from core.skills import registry

        assert registry.skills_root() == str(chosen)

    def test_the_install_has_a_workspaces_directory(self, portable):
        """The directory `config` defaults to when `WORKSPACE_ROOT` is unset.

        Asserted here rather than by reloading `config`, because a reload
        re-executes the module: it rebuilds the model registry, and every other
        suite's free-model tests then fail against a registry that no longer has
        the entries they expect. A cross-test side effect from a layout test is
        exactly the kind of bug this repo keeps paying for.
        """
        assert (portable / "Workspaces").is_dir()
        assert paths.workspaces_dir() == portable / "Workspaces"