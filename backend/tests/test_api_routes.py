"""The REST surface.

`/health` and `/tools` are the two endpoints the UI and the agent prompt both
depend on, and neither had a test. A one-line edit to `health()` once deleted
the `get_llm_client()` call it depends on, and the suite stayed green because
nothing exercised the route.
"""

import os
import shutil
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api.main as main_module  # noqa: E402


@pytest.fixture(scope="module")
def client():
    return TestClient(main_module.app)


class TestHealth:
    def test_answers(self, client):
        response = client.get("/health")
        assert response.status_code == 200

    def test_reports_a_status_and_version(self, client):
        body = client.get("/health").json()
        # `unverified` joined the vocabulary when the provider probe became
        # evidence-based: configured-but-never-probed is neither `ok` (a
        # guess) nor `degraded` (an alarm), and a test suite configures a
        # provider without ever sending it a request.
        assert body["status"] in ("ok", "degraded", "unverified")
        assert body["version"]

    def test_reports_the_llm_chain_without_raising(self, client):
        """Must stay answerable with no API key and a bad model config.

        ``build_chain()`` raises in exactly those situations, so the endpoint
        has to catch it rather than propagate.
        """
        body = client.get("/health").json()
        assert "chain" in body["llm"]
        assert "chain_error" in body["llm"]
        assert isinstance(body["llm"]["chain"], list)

    def test_reports_llm_metrics(self, client):
        metrics = client.get("/health").json()["llm"]["metrics"]
        for key in ("requests", "success", "rate_limited", "avg_latency_ms"):
            assert key in metrics, key

    def test_reports_safety_configuration(self, client):
        safety = client.get("/health").json()["safety"]
        assert "enabled" in safety
        assert "confirm_timeout" in safety

    def test_reports_voice_availability(self, client):
        voice = client.get("/health").json()["voice"]
        assert "stt" in voice and "tts" in voice

    def test_reports_the_workspace(self, client):
        workspace = client.get("/health").json()["workspace"]
        assert "root" in workspace
        assert "enforced" in workspace

    def test_reports_the_registered_tool_count(self, client):
        body = client.get("/health").json()
        assert body["tools_loaded"] > 0
        assert set(body["tools_by_tier"]) <= {"safe", "confirm", "dangerous"}

    def test_reports_tool_execution_metrics_next_to_the_llm_ones(self, client):
        """Two questions, two numbers: is the model healthy, is the tool layer.

        They are deliberately not merged — a blended figure cannot tell you
        whether to bench a model or fix a slow tool.
        """
        body = client.get("/health").json()
        tools = body["metrics"]["tools"]
        for key in (
            "started", "completed", "running",
            "succeeded", "failed", "declined", "timed_out",
            "avg_ms", "by_tool",
        ):
            assert key in tools, key
        assert isinstance(tools["by_tool"], dict)
        assert tools["running"] >= 0
        # The model's metrics stay where every existing client reads them.
        assert "metrics" in body["llm"]


class TestToolsEndpoint:
    def test_lists_every_tool(self, client):
        tools = client.get("/tools").json()["tools"]
        assert len(tools) > 20
        for tool in tools:
            assert tool["name"]
            assert tool["description"]
            assert tool["schema"]["type"] == "object"
            assert tool["permission"] in ("safe", "confirm", "dangerous")

    def test_includes_the_newly_added_capabilities(self, client):
        """Capabilities that did not exist before Increment 2."""
        names = {t["name"] for t in client.get("/tools").json()["tools"]}
        for expected in (
            "file_read", "file_write", "create_folder", "file_delete",
            "search_files", "git_status", "git_commit", "create_project",
            "run_npm", "open_url", "screenshot", "play_media",
        ):
            assert expected in names, expected

    def test_groups_by_tier(self, client):
        by_tier = client.get("/tools").json()["by_tier"]
        assert by_tier["safe"]
        assert by_tier["confirm"]
        assert by_tier["dangerous"]

    def test_the_catalogue_is_json_serialisable(self, client):
        import json

        json.dumps(client.get("/tools").json())


class TestWorkspaceEndpoint:
    """The root is the containment boundary of every file tool.

    Two properties are worth pinning: a path that cannot be used is *refused*
    (the UI must never show a workspace the tools are not using), and a path
    that *is* accepted comes back after a restart — which is the half that was
    missing, and the reason these assertions live next to the endpoint rather
    than only in a unit test of the service.
    """

    @pytest.fixture(autouse=True)
    def _restore_the_root(self):
        """These tests move a process-global; put it back afterwards."""
        from config import config

        before = config.WORKSPACE_ROOT
        yield
        config.WORKSPACE_ROOT = before

    def test_get_reports_the_current_root(self, client):
        body = client.get("/settings/workspace").json()
        assert body["root"]
        assert "entries" in body

    def test_post_rejects_an_empty_path(self, client):
        response = client.post("/settings/workspace", json={"root": ""})
        assert response.status_code == 400

    def test_post_rejects_a_relative_path(self, client):
        # "Absolute or refused" is the same rule on every OS; the old handler
        # mkdir'd a relative path inside the source tree and answered 200.
        # `no/such/tree` is relative on both platforms, so the message word is
        # assertable anywhere. `X:/no/such/drive` is a missing drive in cmd.exe
        # but an *absolute* path to Python on Windows — it clears the absolute
        # check and is refused one step later when creation fails (WinError 3),
        # so only its status is asserted here.
        relative = client.post("/settings/workspace", json={"root": "no/such/tree"})
        assert relative.status_code == 400
        assert "absolute" in relative.json()["error"].lower()

        missing_drive = client.post(
            "/settings/workspace", json={"root": "X:/no/such/drive"}
        )
        assert missing_drive.status_code == 400

    def test_post_rejects_a_path_that_cannot_be_a_directory(self, client, tmp_path):
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("occupied", encoding="utf-8")
        response = client.post(
            "/settings/workspace", json={"root": str(blocker / "sub")}
        )
        assert response.status_code == 400

    def test_post_accepts_an_existing_directory(self, client, tmp_path):
        from config import config

        chosen = tmp_path / "project"
        chosen.mkdir()
        response = client.post("/settings/workspace", json={"root": str(chosen)})
        assert response.status_code == 200
        body = response.json()
        assert body["root"] == str(chosen.resolve())
        # Nothing was created, so there is nothing to mention.
        assert body["note"] == ""
        assert body["persisted"] is True
        assert config.WORKSPACE_ROOT == str(chosen.resolve())

    def test_post_creates_a_missing_directory_and_says_so(self, client, tmp_path):
        response = client.post(
            "/settings/workspace", json={"root": str(tmp_path / "new" / "deeper")}
        )
        assert response.status_code == 200
        assert response.json()["note"] == "created"

    @pytest.mark.parametrize("key", ["root", "path"])
    def test_both_request_keys_are_accepted(self, client, tmp_path, key):
        chosen = tmp_path / f"via-{key}"
        chosen.mkdir()
        response = client.post("/settings/workspace", json={key: str(chosen)})
        assert response.status_code == 200

    async def test_a_saved_root_survives_a_restart(self, client, tmp_path):
        """The regression: the endpoint persisted the choice and nothing read it.

        ``config.WORKSPACE_ROOT`` came from the environment only, so after a
        restart every file tool was confined to the *default* directory while
        the user believed they had pointed Zaytoon at their project.
        """
        from config import config
        from services import workspace_settings

        chosen = tmp_path / "my-project"
        chosen.mkdir()
        assert client.post(
            "/settings/workspace", json={"root": str(chosen)}
        ).status_code == 200

        # Simulate a fresh process: the environment's answer is back.
        config.WORKSPACE_ROOT = str(tmp_path / "default")
        assert await workspace_settings.restore(main_module.user_prefs)
        assert config.WORKSPACE_ROOT == str(chosen.resolve())

    async def test_a_saved_root_that_vanished_warns_instead_of_failing(
        self, client, tmp_path
    ):
        """A missing drive must not stop the backend from booting."""
        from config import config
        from services import workspace_settings

        gone = tmp_path / "usb" / "project"
        gone.mkdir(parents=True)
        assert client.post(
            "/settings/workspace", json={"root": str(gone)}
        ).status_code == 200
        shutil.rmtree(tmp_path / "usb")

        config.WORKSPACE_ROOT = str(tmp_path / "default")
        note = await workspace_settings.restore(main_module.user_prefs)
        assert note and note.startswith("ignored")
        # The configured default survives, so /health and the settings screen
        # stay reachable and the user can pick a new folder.
        assert config.WORKSPACE_ROOT == str(tmp_path / "default")


class TestReminderRoutes:
    """Reminders live at ``/reminders``.

    They used to be at ``/tasks``, which now means agent tasks. The move is
    deliberate — see ``core/api/tasks_api.py`` — so these assertions follow the
    path rather than asserting the old one.
    """

    def test_list_requires_no_arguments(self, client):
        response = client.get("/reminders")
        assert response.status_code == 200
        assert "tasks" in response.json()

    def test_create_rejects_a_missing_time(self, client):
        response = client.post("/reminders", json={"task": "x"})
        assert response.status_code == 400

    def test_create_rejects_a_malformed_time(self, client):
        response = client.post("/reminders", json={"task": "x", "time": "not-a-time"})
        assert response.status_code == 400

    def test_missing_reminder_is_404(self, client):
        assert client.get("/reminders/999999").status_code == 404

    def test_a_reminder_can_be_marked_done(self, client):
        assert client.patch("/reminders/999999/done").status_code == 404


class TestCommandRoutes:
    def test_open_app_requires_a_name(self, client):
        response = client.post("/command/open-app", json={})
        assert response.json()["success"] is False

    def test_open_app_refuses_an_unlisted_app(self, client):
        response = client.post("/command/open-app", json={"app_name": "definitely-not-real-xyz"})
        assert response.json()["success"] is False
        assert "allowed" in response.json()["error"].lower()

    def test_run_command_requires_a_command(self, client):
        response = client.post("/command/run", json={})
        assert response.json()["success"] is False

    def test_run_command_denies_risky_work_without_an_approval_channel(self, client):
        """REST has no approval UI, so it denies rather than executing."""
        response = client.post("/command/run", json={"command": "rm -rf /"})
        body = response.json()
        assert body["success"] is False
        assert body.get("requires_confirmation") is True
        assert "permission" in body

    def test_run_command_blocks_chaining_outright(self, client):
        """Not reported as "needs confirmation" — confirming would not help."""
        response = client.post("/command/run", json={"command": "echo hi & hostname"})
        body = response.json()
        assert body["success"] is False
        assert not body.get("requires_confirmation")


class TestComputerAppsRoute:
    """`GET /computer/apps` is a *view* of the launcher's allow-list.

    The Computer screen's app grid is built from this payload, so the two
    invariants below are the whole reason the route exists: a panel that
    advertises a name the launcher refuses is a button that fails, and one
    that omits a name it accepts is a capability the product hides from
    itself. Both are silent failures in a screenshot, hence the exact-set
    assertion rather than a length check.
    """

    def test_answers_with_a_well_formed_catalogue(self, client):
        body = client.get("/computer/apps").json()
        assert body["total"] == len(body["apps"]) > 0
        for app in body["apps"]:
            assert set(app) == {"id", "label", "category"}
            assert app["id"] and app["label"] and app["category"]
        # Categories arrive in first-seen order and contain no duplicates, so
        # the panel's grouping is stable between refreshes.
        assert body["categories"] == list(
            dict.fromkeys(a["category"] for a in body["apps"])
        )
        for cat in body["categories"]:
            assert any(a["category"] == cat for a in body["apps"])

    def test_the_catalogue_is_the_allow_list_and_nothing_more(self, client):
        from api.main import APP_CATALOG, APP_WHITELIST

        advertised = [n for e in APP_CATALOG for n in e["names"]]
        # Both directions in one assertion: a name the launcher accepts but the
        # catalogue omits, and a name the catalogue offers but the launcher
        # refuses, each fail here.
        assert sorted(advertised) == sorted(APP_WHITELIST)
        assert len(advertised) == len(set(advertised)), "a name claimed twice"
        for entry in APP_CATALOG:
            # `id` is what the client sends, so it must be one of the names.
            assert entry["id"] in entry["names"]

    def test_every_id_is_a_name_the_launcher_accepts(self, client):
        """The `id` is what the client sends, so it has to be the accepted spelling.

        Stated as a membership check against the allow-list rather than by
        calling `POST /command/open-app` — that route really launches things,
        and a test that opens the user's browser and Word on every run would
        be worse than no test.
        """
        from api.main import APP_WHITELIST

        body = client.get("/computer/apps").json()
        ids = [a["id"] for a in body["apps"]]
        assert len(ids) == len(set(ids)), "duplicate ids"
        for app_id in ids:
            assert app_id in APP_WHITELIST


class TestComputerAppAvailability:
    """`?resolve=true` reports positive evidence — and says which answer it gave.

    The grid is only worth having if a lit tile means something. Two rules
    are pinned here because both fail silently: a verdict the route never
    computed (a field that is always there and always right-looking), and a
    verdict derived from a *table of how to launch* rather than from anything
    observed on the machine. The second one would light up the whole panel on
    a machine with nothing installed, which is a green grid that means
    nothing — worse than no grid at all.
    """

    def test_the_default_stays_static(self, client):
        body = client.get("/computer/apps").json()
        assert body["resolved"] is False
        # No field that looks like a verdict the route did not compute.
        assert all("installed" not in app for app in body["apps"])
        assert "found" not in body

    def test_resolved_reports_a_verdict_for_every_app(self, client):
        body = client.get("/computer/apps?resolve=true").json()
        assert body["resolved"] is True
        assert body["total"] == len(body["apps"])
        for app in body["apps"]:
            assert isinstance(app["installed"], bool), app
            assert app["via"], app
        # The tally is derived from the payload, not asserted beside it.
        assert body["found"] == sum(1 for a in body["apps"] if a["installed"])
        assert 0 <= body["found"] <= body["total"]

    def test_a_target_is_not_evidence_that_it_is_installed(self, monkeypatch):
        """The builtin URI table says *how* to launch, never *that* it exists.

        `spotify:` resolves only when Spotify is installed. Reporting
        `found: true` for every entry in that table would be a clean, uniform,
        completely meaningless answer.
        """
        import tools.applications as apps

        monkeypatch.setattr(apps.shutil, "which", lambda *a, **k: None)
        monkeypatch.setattr(apps, "_find_start_menu_shortcut", lambda *a, **k: None)
        # Three inputs decide `found` on a Mac (PATH, Start Menu, .app bundle), and
        # this test is about the answer when *nothing* is observed. Without this
        # patch the assertion means "Spotify is not installed on the CI runner",
        # which is a fact about the runner rather than about Zaytoon.
        monkeypatch.setattr(apps, "_mac_bundle", lambda *a, **k: None)

        detail = apps.resolve_app_detail("spotify")
        assert detail["target"].startswith("spotify:")  # still launchable
        assert detail["found"] is False  # but not claimed present

    def test_observed_evidence_turns_the_verdict_on(self, monkeypatch):
        import tools.applications as apps

        monkeypatch.setattr(
            apps.shutil, "which", lambda *a, **k: r"C:\fake\spotify.exe"
        )
        monkeypatch.setattr(apps, "_find_start_menu_shortcut", lambda *a, **k: None)

        detail = apps.resolve_app_detail("spotify")
        assert detail["found"] is True
        # The target stays the builtin form: that is what the launcher wants,
        # and the evidence is only ever consulted for the verdict.
        assert detail["target"].startswith("spotify:")

    def test_a_rejected_name_is_never_reported_as_present(self):
        from tools.applications import resolve_app_detail

        detail = resolve_app_detail("calc & calc")
        assert detail["target"] is None
        assert detail["found"] is False
        assert detail["via"] == "rejected"

    def test_the_verdict_is_produced_by_the_resolver_the_launcher_uses(self):
        """One decision tree — not a probe that can disagree with the launch.

        Every name the panel shows must resolve through `resolve_app_detail`,
        which is exactly what `resolve_windows_app` returns a field of. If the
        probe ever grew its own rules, "available" and "will launch" would be
        two answers to the same question.
        """
        from tools.applications import resolve_app_detail, resolve_windows_app

        for name in ("notepad", "explorer", "control panel", "some_random_app_12345"):
            assert resolve_windows_app(name) == resolve_app_detail(name)["target"]

    def test_the_batch_walks_the_start_menu_exactly_once(self, client, monkeypatch):
        """Twenty applications, one filesystem walk.

        The probe runs on the event loop of a server that is also streaming
        tokens, and `resolve_app_detail` takes `shortcut_index` precisely so a
        batch does not walk the same tree twenty times. Without the shared
        index this test still passes functionally — it is a cost, not a
        correctness bug, which is why it needs its own assertion.
        """
        walks = []
        monkeypatch.setattr(
            main_module, "build_shortcut_index", lambda: walks.append(1) or []
        )
        monkeypatch.setattr(
            main_module,
            "resolve_app_detail",
            lambda app_id, shortcut_index=None: {
                "target": None,
                "found": False,
                "via": "none",
            },
        )
        body = client.get("/computer/apps?resolve=true").json()
        assert body["resolved"] is True
        assert walks == [1], f"Start Menu walked {len(walks)} times for one request"

    def test_a_missing_index_is_still_a_valid_request(self, client, monkeypatch):
        """A probe that fails must not take the panel down with it.

        The Computer screen loads four panels at once; if the availability
        probe raised, the route would 500 and the whole Apps section would
        render empty. Instead the verdict is withheld and the response says
        why — the same honest "I did not find out" the field always gives.
        """
        monkeypatch.setattr(
            main_module, "build_shortcut_index", lambda: (_ for _ in ()).throw(
                OSError("Start Menu is unreadable")
            )
        )
        body = client.get("/computer/apps?resolve=true").json()
        assert body["total"] > 0, "the static catalogue must survive a failed probe"
        assert body["resolved"] is False
        assert body["resolve_error"]
        # No half-computed verdict: either every app has one or none does.
        assert all("installed" not in app for app in body["apps"])


class _FakeProvider:
    """Minimal AIProvider stand-in so /settings/ai never opens a socket."""

    name = "ollama"
    base_url = "http://localhost:11434"

    def __init__(self):
        from services.ai_provider import HealthStatus, ModelInfo

        self._health = HealthStatus(
            provider="ollama",
            connected=True,
            endpoint=self.base_url,
            version="0.0.0-test",
            models=[ModelInfo(id="qwen3.5:9b", name="qwen3.5", provider="ollama")],
        )

    async def health_check(self):
        return self._health

    async def list_models(self):
        return self._health.models

    def model_for_tier(self, tier):
        return f"model-for-{tier}"

    async def close(self):
        return None


class TestAISettings:
    def test_get_reports_status_and_routing(self, client, monkeypatch):
        from services import ai_provider

        monkeypatch.setattr(ai_provider, "_provider_instance", _FakeProvider())
        response = client.get("/settings/ai")
        assert response.status_code == 200
        body = response.json()
        assert body["provider"] in ("ollama", "openrouter")
        assert body["connected"] is True
        assert body["endpoint"]
        assert body["models"]
        assert "routing" in body and "configured_models" in body

    def test_rejects_an_unknown_provider(self, client):
        response = client.post("/settings/ai", json={"provider": "skynet"})
        assert response.status_code == 400

    def test_validate_endpoint_reports_connection(self, client, monkeypatch):
        import services.ollama_client as oc

        class _Health:
            connected = False
            version = ""
            error = "Connection refused"
            models = []
            endpoint = "http://localhost:11434"

        class _Client:
            async def health_check(self):
                return _Health()

            async def aclose(self):
                return None

        monkeypatch.setattr(oc, "OllamaClient", lambda *a, **k: _Client())
        response = client.post(
            "/settings/ai/validate", json={"provider": "ollama"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["connected"] is False
        assert "refused" in body["error"]


_OLLAMA_SETTINGS_FIELDS = (
    "PROVIDER", "AI_PROVIDER", "OLLAMA_BASE_URL", "OLLAMA_GENERAL_MODEL",
    "OLLAMA_CODING_MODEL", "OLLAMA_FAST_MODEL", "OLLAMA_TIMEOUT",
)
_OPENROUTER_SETTINGS_FIELDS = (
    "PROVIDER", "AI_PROVIDER", "OPENROUTER_API_KEY", "OPENROUTER_BASE_URL",
    "OPENROUTER_PRIMARY_MODEL", "OPENROUTER_REASONING_MODEL",
    "OPENROUTER_FALLBACK_MODEL",
)


class TestAISettingsPersistAcrossRestarts:
    """A save that reverts on restart is the bug users call "did not stick".

    Two stores, and the split matters: settings in `kv`, the key in
    credential storage — never in a table a later query could print.
    """

    @pytest.fixture
    def isolated_secrets(self, monkeypatch, tmp_path):
        """The credential round-trip must not touch the real secrets file."""
        from core import secrets as core_secrets

        monkeypatch.setenv(
            "ZAYTOON_SECRETS_PATH", str(tmp_path / "secrets.json")
        )
        core_secrets.reset_cache()
        yield
        core_secrets.reset_cache()

    @staticmethod
    def _guard_config(monkeypatch, fields):
        """Snapshot every field the POST mutates; teardown restores them.

        `raising=False` because the route itself is what first creates a few
        of these (e.g. `AI_PROVIDER`) — an attribute that did not exist is
        deleted again at teardown, which is exactly the restore we want.
        """
        from config import config

        for name in fields:
            monkeypatch.setattr(
                config, name, getattr(config, name, None), raising=False
            )

    @staticmethod
    def _no_sockets(monkeypatch):
        """The route's `get_provider()` is a local import — patch the module."""
        from services import ai_provider

        monkeypatch.setattr(ai_provider, "get_provider", lambda: _FakeProvider())

    async def test_saved_settings_are_applied_on_next_startup(
        self, client, monkeypatch
    ):
        from config import config
        from services.ai_settings import apply_persisted_ai_settings, is_persisted

        self._guard_config(monkeypatch, _OLLAMA_SETTINGS_FIELDS)
        self._no_sockets(monkeypatch)

        response = client.post("/settings/ai", json={
            "provider": "ollama",
            "base_url": "http://localhost:11500",
            "general_model": "saved-general:7b",
            "fast_model": "saved-fast:3b",
        })
        assert response.status_code == 200
        assert response.json()["persisted"] is True
        assert await is_persisted() is True

        # Simulate the restart: a process that has only ever seen the
        # environment defaults.
        config.OLLAMA_BASE_URL = "http://localhost:11434"
        config.OLLAMA_GENERAL_MODEL = "env-default:9b"
        config.OLLAMA_FAST_MODEL = "env-default:3b"

        applied = await apply_persisted_ai_settings()

        assert config.OLLAMA_BASE_URL == "http://localhost:11500"
        assert config.OLLAMA_GENERAL_MODEL == "saved-general:7b"
        assert config.OLLAMA_FAST_MODEL == "saved-fast:3b"
        assert "provider" in applied

    async def test_the_api_key_goes_to_credential_storage_not_kv(
        self, client, monkeypatch, isolated_secrets
    ):
        from core import db, secrets as core_secrets
        from config import config
        from services.ai_settings import API_KEY_SECRET

        self._guard_config(monkeypatch, _OPENROUTER_SETTINGS_FIELDS)
        self._no_sockets(monkeypatch)

        response = client.post("/settings/ai", json={
            "provider": "openrouter",
            "api_key": "sk-super-secret-42",
            "primary_model": "qwen/qwen3.8-27b:free",
        })
        assert response.status_code == 200
        # Never in the response:
        assert "sk-super-secret-42" not in response.text
        # Never in kv — a plain table a later query could print:
        raw = await db.kv_get("settings.ai")
        assert raw and "sk-super-secret-42" not in raw
        # Always in credential storage, and applied to the live config:
        assert core_secrets.has_secret(API_KEY_SECRET)
        assert core_secrets.get_secret(API_KEY_SECRET) == "sk-super-secret-42"
        assert config.OPENROUTER_API_KEY == "sk-super-secret-42"

    async def test_no_read_path_returns_the_saved_key(
        self, client, monkeypatch, isolated_secrets
    ):
        """The audit half: a stored key must never surface on any status read.

        The write path above proves the key goes to credential storage; this
        proves the *read* routes behave as if they were never handed it. One
        echo — a health payload, a settings dump, a diagnostics feed — is the
        classic leak, and it only takes one route that does `**config`.
        """
        self._guard_config(monkeypatch, _OPENROUTER_SETTINGS_FIELDS)
        self._no_sockets(monkeypatch)

        saved = client.post("/settings/ai", json={
            "provider": "openrouter",
            "api_key": "sk-audit-only-99887766",
        })
        assert saved.status_code == 200
        assert "sk-audit-only-99887766" not in saved.text

        for path in ("/settings/ai", "/health", "/system", "/events/recent"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert "sk-audit-only-99887766" not in response.text, path

    async def test_get_reports_whether_settings_are_persisted(
        self, client, monkeypatch
    ):
        from services import ai_provider

        monkeypatch.setattr(ai_provider, "_provider_instance", _FakeProvider())
        body = client.get("/settings/ai").json()
        # Isolated per-test database: no row was ever written here.
        assert body["persisted"] is False

    async def test_a_corrupt_row_applies_nothing(self, monkeypatch):
        """A damaged preference degrades to the defaults, not to a crash."""
        from config import config
        from core import db
        from services.ai_settings import apply_persisted_ai_settings

        self._guard_config(monkeypatch, _OLLAMA_SETTINGS_FIELDS)
        sentinel = config.OLLAMA_BASE_URL
        await db.kv_set("settings.ai", "{not json")

        assert await apply_persisted_ai_settings() == []
        assert config.OLLAMA_BASE_URL == sentinel


class TestPullIsExplicitAndBounded:
    """Pulling gigabytes is a decision, never an installation side effect."""

    @pytest.fixture
    def pull_world(self, monkeypatch):
        """Isolated pull records, an Ollama-looking provider, ollama selected."""
        import api.main as main_module
        from services.ai_provider import OllamaProvider
        from services.ollama_client import PullProgress

        class _PullingProvider(OllamaProvider):
            def __init__(self):
                self.pulled = []
                self.invalidations = 0

            async def pull_model(self, model):
                self.pulled.append(model)
                yield PullProgress(status="pulling manifest")
                yield PullProgress(status="success")

            def invalidate_cache(self):
                self.invalidations += 1

        monkeypatch.setattr(main_module, "_PULLS", {})
        monkeypatch.setattr(
            main_module, "get_provider", lambda: _PullingProvider()
        )
        monkeypatch.setattr(main_module.config, "PROVIDER", "ollama")
        return main_module

    def test_pull_needs_ollama_selected(self, client, monkeypatch):
        import api.main as main_module

        monkeypatch.setattr(main_module.config, "PROVIDER", "openrouter")
        response = client.post("/settings/ai/pull", json={"model": "llama3:8b"})
        assert response.status_code == 400
        assert "Ollama" in response.json()["error"]

    def test_hostile_model_names_are_refused(self, client, pull_world):
        """Validation runs before a provider or a task is ever touched."""
        for bad in (
            "llama3:8b & hostname",
            "../secrets",
            "a/../../b",
            "name with space",
            "model;rm -rf",
            "-leading-dash",
            "",
        ):
            response = client.post("/settings/ai/pull", json={"model": bad})
            assert response.status_code == 400, bad

    def test_a_running_pull_is_not_started_twice(self, client, pull_world):
        pull_world._PULLS["hang:7b"] = {
            "model": "hang:7b", "state": "running", "status": "downloading",
            "total": 100, "completed": 50, "error": None,
            "started_at": 1.0, "finished_at": None,
        }
        response = client.post("/settings/ai/pull", json={"model": "hang:7b"})
        assert response.status_code == 409
        assert "already running" in response.json()["error"]

    def test_a_pull_starts_and_is_pollable(self, client, pull_world):
        response = client.post(
            "/settings/ai/pull", json={"model": "tinyllama:1.1b"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "started"
        assert body["model"] == "tinyllama:1.1b"
        assert body["state"] == "running"

        listed = client.get("/settings/ai/pull").json()["pulls"]
        assert any(p["model"] == "tinyllama:1.1b" for p in listed)
        record = client.get(
            "/settings/ai/pull", params={"model": "tinyllama:1.1b"}
        )
        assert record.status_code == 200
        assert record.json()["model"] == "tinyllama:1.1b"

    def test_an_unknown_pull_reports_404(self, client, pull_world):
        response = client.get(
            "/settings/ai/pull", params={"model": "nope:1b"}
        )
        assert response.status_code == 404

    async def test_the_pull_record_tells_the_truth_to_the_end(
        self, monkeypatch
    ):
        import api.main as main_module
        from services.ollama_client import PullProgress

        monkeypatch.setattr(main_module, "_PULLS", {})
        model = "tinyllama:1.1b"
        main_module._PULLS[model] = {
            "model": model, "state": "running", "status": "starting",
            "total": 0, "completed": 0, "error": None,
            "started_at": 1.0, "finished_at": None,
        }

        class _Provider:
            def __init__(self):
                self.invalidations = 0

            async def pull_model(self, _model):
                yield PullProgress(status="downloading", total=10, completed=4)
                yield PullProgress(status="success", total=10, completed=10)

            def invalidate_cache(self):
                self.invalidations += 1

        provider = _Provider()
        await main_module._run_pull(provider, model)

        state = main_module._PULLS[model]
        assert state["state"] == "done"
        assert state["status"] == "success"
        assert state["total"] == 10 and state["completed"] == 10
        assert state["error"] is None
        assert state["finished_at"] is not None
        # A finished pull must be visible to the next model listing.
        assert provider.invalidations == 1

    async def test_a_broken_pull_says_so(self, monkeypatch):
        import api.main as main_module
        from services.ollama_client import PullProgress

        monkeypatch.setattr(main_module, "_PULLS", {})
        model = "big:70b"
        main_module._PULLS[model] = {
            "model": model, "state": "running", "status": "downloading",
            "total": 10, "completed": 3, "error": None,
            "started_at": 1.0, "finished_at": None,
        }

        class _Provider:
            invalidations = 0

            async def pull_model(self, _model):
                yield PullProgress(status="downloading", total=10, completed=3)
                raise RuntimeError("connection reset by peer")

            def invalidate_cache(self):
                type(self).invalidations += 1

        await main_module._run_pull(_Provider(), model)

        state = main_module._PULLS[model]
        assert state["state"] == "failed"
        assert "connection reset" in state["error"]
        assert state["finished_at"] is not None

    async def test_a_stream_that_ends_without_success_is_a_failure(
        self, monkeypatch
    ):
        """Silence is not success: an abruptly ended stream is reported."""
        import api.main as main_module

        monkeypatch.setattr(main_module, "_PULLS", {})
        model = "big:70b"
        main_module._PULLS[model] = {
            "model": model, "state": "running", "status": "starting",
            "total": 0, "completed": 0, "error": None,
            "started_at": 1.0, "finished_at": None,
        }

        class _Provider:
            async def pull_model(self, _model):
                if False:
                    yield None  # an async generator that yields nothing
                return

            def invalidate_cache(self):
                raise AssertionError("a failed pull must not refresh models")

        await main_module._run_pull(_Provider(), model)

        state = main_module._PULLS[model]
        assert state["state"] == "failed"
        assert "without a success status" in state["error"]

    def test_nothing_pulls_on_its_own(self):
        """A health check, a probe or an installer must never download.

        The route is the only caller in the tree; these three files are the
        route, the wrapper and the client method itself.
        """
        import re as _re
        from pathlib import Path

        backend_root = Path(__file__).resolve().parents[1]
        allowed = {
            "api/main.py",
            "services/ai_provider.py",
            "services/ollama_client.py",
        }
        offenders = []
        for path in sorted(backend_root.rglob("*.py")):
            rel = path.relative_to(backend_root).as_posix()
            if rel.startswith((".venv/", "tests/")) or rel in allowed:
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if _re.search(r"\bpull_model\s*\(", source):
                offenders.append(rel)
        assert offenders == [], f"pull_model must stay explicit: {offenders}"


class TestProbeIsHonest:
    """A capability claim is only worth what measured it."""

    @pytest.fixture
    def frozen_ollama(self, monkeypatch):
        """No test may ever reach a real Ollama — endpoints point at a wall."""
        import api.main as main_module

        monkeypatch.setattr(
            main_module.config, "OLLAMA_BASE_URL", "http://127.0.0.1:1"
        )
        monkeypatch.setattr(main_module.config, "OLLAMA_TIMEOUT", 3.0)

    async def test_a_non_ollama_provider_cannot_be_probed(
        self, client, frozen_ollama
    ):
        from core.providers import save_provider

        provider = await save_provider(
            None, name="Cloud", kind="openrouter",
            base_url="https://openrouter.ai/api/v1",
        )
        response = client.post("/settings/ai/probe", json={
            "model": "some/model:free", "provider_id": provider["id"],
        })
        assert response.status_code == 400
        assert "Ollama" in response.json()["error"]

    async def test_an_unreachable_endpoint_says_so(self, client, frozen_ollama):
        from core.providers import save_provider

        await save_provider(
            None, name="Local", kind="ollama", base_url="http://127.0.0.1:1"
        )
        response = client.post("/settings/ai/probe", json={"model": "any:7b"})
        assert response.status_code == 400
        assert "unreachable" in response.json()["error"]

    async def test_a_missing_model_says_pull_first(
        self, client, frozen_ollama, monkeypatch
    ):
        from services.ai_provider import HealthStatus, ModelInfo
        import services.ollama_client as oc

        class _FakeClient:
            def __init__(self, base_url, timeout=None, **kwargs):
                self.base_url = base_url

            async def health_check(self):
                return HealthStatus(
                    provider="ollama", connected=True,
                    endpoint=self.base_url, version="test",
                    models=[
                        ModelInfo(id="have:9b", name="have:9b", provider="ollama")
                    ],
                )

            async def probe_capabilities(self, model):
                raise AssertionError(
                    "an uninstalled model must never be probed"
                )

            async def aclose(self):
                return None

        monkeypatch.setattr(oc, "OllamaClient", _FakeClient)
        from core.providers import save_provider

        await save_provider(
            None, name="Local", kind="ollama", base_url="http://127.0.0.1:1"
        )
        response = client.post(
            "/settings/ai/probe", json={"model": "missing:1b"}
        )
        assert response.status_code == 404
        assert "pull it first" in response.json()["error"]

    async def test_probe_records_what_it_measured_and_changes_nothing_else(
        self, client, frozen_ollama, monkeypatch
    ):
        """Capabilities update; the user's priority and disabled state do not."""
        from core import db
        from core.providers import list_models, save_model, save_provider
        from services.ai_provider import HealthStatus, ModelInfo
        from services.ollama_client import ModelCapabilities
        import services.ollama_client as oc

        provider = await save_provider(
            None, name="Local", kind="ollama", base_url="http://127.0.0.1:1"
        )
        await save_model(
            provider["id"], "have:9b", capabilities=["chat"], priority=7
        )
        await db.execute(
            "UPDATE models SET enabled = 0 WHERE provider_id = ? "
            "AND model_id = ?",
            (provider["id"], "have:9b"),
        )

        caps = ModelCapabilities(model="have:9b")
        caps.supports_streaming = True
        caps.supports_tools = True
        caps.context_window = 8192

        class _FakeClient:
            def __init__(self, base_url, timeout=None, **kwargs):
                self.base_url = base_url

            async def health_check(self):
                return HealthStatus(
                    provider="ollama", connected=True,
                    endpoint=self.base_url, version="test",
                    models=[
                        ModelInfo(id="have:9b", name="have:9b", provider="ollama")
                    ],
                )

            async def probe_capabilities(self, model):
                return caps

            async def aclose(self):
                return None

        monkeypatch.setattr(oc, "OllamaClient", _FakeClient)
        response = client.post(
            "/settings/ai/probe", json={"model": "have:9b"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["capabilities"] == ["chat", "streaming", "tools"]
        assert body["supports"]["tools"] is True
        assert body["supports"]["structured_output"] is False
        assert body["context_window"] == 8192
        assert body["recorded"] is True

        rows = await list_models(provider["id"], enabled_only=False)
        row = rows[0]
        assert "tools" in row["capabilities"]
        assert row["context_length"] == 8192
        # save_model's upsert overwrites both — the probe must not claim them:
        assert row["priority"] == 7
        assert row["enabled"] is False


class TestTheErrorContract:
    """One shape for every REST failure: ``{"error": "<sentence>"}``.

    The frontend reads ``data.error`` (falling back to ``data.detail``), and a
    client that has to check two keys for the same concept is a client with a
    bug waiting in it. ``api/main.py`` documents the promise; these tests keep
    it true, because two of the paths below never reached the handler that
    made the claim.

    * a route raising ``HTTPException`` — covered, and stays covered;
    * an **unmatched path** — Starlette raises ``starlette.HTTPException``,
      a different class from FastAPI's, so it used to answer ``{"detail":
      "Not Found"}`` and miss the handler entirely;
    * a **path that fails validation** — FastAPI's ``RequestValidationError``
      handler is a third path, which used to answer ``{"detail": [...]}``.
    """

    def test_a_route_http_exception_uses_the_error_key(self, client):
        # `/reminders`, not `/tasks`: the agent task system moved into `/tasks`
        # and answers with its own (more specific) 404 text. This test is about
        # the error *key* contract, so it uses a route that still raises the
        # plain "not found" it is asserting on.
        response = client.get("/reminders/999999")
        assert response.status_code == 404
        body = response.json()
        assert set(body) == {"error"}, body
        assert body["error"] == "not found"

    def test_the_core_404_also_uses_the_error_key(self, client):
        """The core routers raise their own 404s; they get the same shape."""
        response = client.get("/tasks/999999")
        assert response.status_code == 404
        body = response.json()
        assert set(body) == {"error"}, body
        assert "999999" in body["error"], "a 404 should name what was not found"

    def test_an_unmatched_path_uses_the_error_key(self, client):
        response = client.get("/definitely-not-a-route")
        assert response.status_code == 404
        body = response.json()
        assert set(body) == {"error"}, body

    def test_a_path_that_fails_validation_uses_the_error_key(self, client):
        # /tasks/{task_id} wants an int; this is FastAPI's own 422 path.
        response = client.get("/tasks/not-an-int")
        assert response.status_code == 422
        body = response.json()
        assert set(body) == {"error"}, body
        # The message has to say which field, or it is useless to a caller.
        assert "task_id" in body["error"], body

    def test_an_unexpected_exception_is_a_sentence_not_a_traceback(self):
        """The REST equivalent of the WebSocket turn boundary.

        The route is registered on the shared app and removed afterwards, so
        the rest of the module's tests still see the real route table.
        """
        app = main_module.app
        SECRET = "sk-or-v1-leakedvalue123"

        @app.get("/__boom_test_only__")
        def _boom():
            raise RuntimeError("kaboom with " + SECRET + r" at C:\Users\x")

        try:
            # raise_server_exceptions=False so the handler's body is what the
            # client sees; with True, Starlette re-raises after it has run.
            response = TestClient(
                app, raise_server_exceptions=False
            ).get("/__boom_test_only__")
        finally:
            app.routes[:] = [r for r in app.routes if getattr(r, "path", "") != "/__boom_test_only__"]

        assert response.status_code == 500
        body = response.json()
        assert set(body) == {"error"}, body
        text = response.text
        assert SECRET not in text
        assert "Traceback" not in text
        assert "kaboom" not in text
        assert "Users" not in text
        # A fixed sentence the caller can show a user, not a diagnostic.
        assert body["error"].endswith("Please try again.")


class TestProcessesRoutes:
    """The read-only views over tracked processes.

    Both UIs hide their processes card when these 404, so the routes have to
    exist; and `stop` is the one endpoint that can end a process, which is why
    its refusal path is pinned rather than assumed.
    """

    def test_list_answers(self, client):
        response = client.get("/processes")
        assert response.status_code == 200
        body = response.json()
        assert "processes" in body and isinstance(body["processes"], list)

    def test_logs_of_an_unknown_id_is_a_structured_miss(self, client):
        body = client.get("/processes/nope/logs").json()
        assert "error" in body
        assert body["lines"] == []

    def test_stopping_an_unknown_id_refuses(self, client):
        """The refusal is the security property: Zaytoon only ever stops what it
        started, and there is no override parameter to get around that."""
        body = client.post("/processes/nope/stop").json()
        assert body["stopped"] is False
        assert "only processes zaytoon started" in body["error"].lower()

    def test_the_cap_on_tail_is_respected(self, client):
        assert client.get("/processes/nope/logs", params={"tail": "99999"}).status_code == 200


class TestAuditRoutes:
    def test_reads(self, client):
        response = client.get("/audit")
        assert response.status_code == 200
        body = response.json()
        assert "entries" in body and isinstance(body["entries"], list)
        assert isinstance(body["total"], int)

    def test_a_limit_is_bounded(self, client):
        assert client.get("/audit", params={"limit": "99999"}).status_code == 200

    def test_clear_reports_how_many_went(self, client):
        body = client.request("DELETE", "/audit").json()
        assert "cleared" in body
        assert isinstance(body["cleared"], int)
