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
