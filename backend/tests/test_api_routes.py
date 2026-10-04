"""The REST surface.

`/health` and `/tools` are the two endpoints the UI and the agent prompt both
depend on, and neither had a test. A one-line edit to `health()` once deleted
the `get_llm_client()` call it depends on, and the suite stayed green because
nothing exercised the route.
"""

import os
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
        assert body["status"] in ("ok", "degraded")
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
    def test_get_reports_the_current_root(self, client):
        body = client.get("/settings/workspace").json()
        assert body["root"]
        assert "entries" in body

    def test_post_rejects_an_empty_path(self, client):
        response = client.post("/settings/workspace", json={"root": ""})
        assert response.status_code == 400

    def test_post_rejects_an_unusable_path(self, client):
        response = client.post("/settings/workspace", json={"root": "X:/no/such/drive"})
        assert response.status_code == 400
        assert "error" in response.json()


class TestReminderRoutes:
    def test_list_requires_no_arguments(self, client):
        response = client.get("/tasks")
        assert response.status_code == 200
        assert "tasks" in response.json()

    def test_create_rejects_a_missing_time(self, client):
        response = client.post("/tasks", json={"task": "x"})
        assert response.status_code == 400

    def test_create_rejects_a_malformed_time(self, client):
        response = client.post("/tasks", json={"task": "x", "time": "not-a-time"})
        assert response.status_code == 400

    def test_missing_task_is_404(self, client):
        assert client.get("/tasks/999999").status_code == 404


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
        response = client.get("/tasks/999999")
        assert response.status_code == 404
        body = response.json()
        assert set(body) == {"error"}, body
        assert body["error"] == "not found"

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
