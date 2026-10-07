"""The core REST surface, driven over HTTP against the real app.

These tests use the same ``TestClient`` the existing route suite uses, so they
exercise the actual app — routers, middleware and the unified ``{"error": ...}``
error shape — rather than calling the route functions directly.
"""

import asyncio
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from config import config


@pytest.fixture
def client(temp_core_db, temp_secrets, temp_workspace):
    """The real app, with the core's storage redirected.

    The workspace is redirected rather than mocked because containment is the
    thing under test: a mock would assert against a fiction.
    """
    from api.main import app

    with TestClient(app) as test_client:
        yield test_client


class TestTasks:
    def test_create_and_read(self, client):
        created = client.post("/tasks", json={"title": "Fix the failing tests"})
        assert created.status_code == 201
        task_id = created.json()["id"]

        fetched = client.get(f"/tasks/{task_id}")
        assert fetched.status_code == 200
        assert fetched.json()["title"] == "Fix the failing tests"

    def test_a_task_needs_a_title(self, client):
        response = client.post("/tasks", json={"request": "no title here"})
        assert response.status_code == 400
        assert "error" in response.json()

    def test_list_returns_a_tasks_key(self, client):
        client.post("/tasks", json={"title": "One"})
        payload = client.get("/tasks").json()
        assert "tasks" in payload
        assert payload["count"] >= 1

    def test_an_unknown_status_is_a_400_with_the_valid_ones(self, client):
        response = client.get("/tasks", params={"status": "nonsense"})
        assert response.status_code == 400
        assert "completed" in response.json()["error"]

    def test_an_unknown_task_is_a_404(self, client):
        assert client.get("/tasks/999999").status_code == 404

    def test_a_non_integer_task_id_is_a_422(self, client):
        response = client.get("/tasks/not-an-int")
        assert response.status_code == 422
        assert "error" in response.json()

    def test_delete(self, client):
        task_id = client.post("/tasks", json={"title": "Temporary"}).json()["id"]
        assert client.delete(f"/tasks/{task_id}").status_code == 200
        assert client.get(f"/tasks/{task_id}").status_code == 404

    def test_deleting_twice_is_a_404_the_second_time(self, client):
        task_id = client.post("/tasks", json={"title": "Temporary"}).json()["id"]
        client.delete(f"/tasks/{task_id}")
        assert client.delete(f"/tasks/{task_id}").status_code == 404

    def test_cancel_requires_a_real_task(self, client):
        assert client.post("/tasks/999999/cancel").status_code == 404

    def test_stats_report_counts_by_status(self, client):
        client.post("/tasks", json={"title": "One"})
        stats = client.get("/tasks/stats").json()
        assert stats["total"] >= 1
        assert "received" in stats["by_status"]


class TestBackgroundExecution:
    """/tasks/{id}/run and /tasks/{id}/retry — the background executor.

    The turn itself is replaced by a recorder: these tests are about the
    route's *decisions* (status codes, lineage, the running guard), not about
    the orchestrator, which has its own suite for the turn.
    """

    @pytest.fixture
    def silent_turn(self, monkeypatch):
        """A turn that records its task id and returns immediately."""
        import core.api.tasks_api as tasks_api

        ran = []

        async def fake_turn(task):
            ran.append(task.id)

        monkeypatch.setattr(tasks_api, "_run_task_turn", fake_turn)
        return ran

    @pytest.fixture
    def slow_turn(self, monkeypatch):
        """A turn that stays running long enough to trip the running guard."""
        import core.api.tasks_api as tasks_api

        ran = []

        async def fake_turn(task):
            ran.append(task.id)
            await asyncio.sleep(0.3)

        monkeypatch.setattr(tasks_api, "_run_task_turn", fake_turn)
        return ran

    @staticmethod
    def _await(predicate, timeout=2.0):
        """Poll until a scheduled background turn has run — it executes on the
        app's loop thread, not on the request that scheduled it."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()

    def test_run_starts_a_non_terminal_task(self, client, silent_turn):
        task_id = client.post(
            "/tasks", json={"title": "Do it", "request": "do it"}
        ).json()["id"]

        response = client.post(f"/tasks/{task_id}/run")

        assert response.status_code == 202
        assert response.json()["status"] == "running"
        assert self._await(lambda: task_id in silent_turn)

    def test_run_on_a_missing_task_is_a_404(self, client):
        assert client.post("/tasks/999999/run").status_code == 404

    def test_run_after_finish_points_at_retry(self, client, silent_turn):
        task_id = client.post("/tasks", json={"title": "Gone"}).json()["id"]
        client.post(f"/tasks/{task_id}/cancel")

        response = client.post(f"/tasks/{task_id}/run")

        assert response.status_code == 409
        assert "retry" in response.json()["error"].lower()
        assert silent_turn == []           # refused means not run

    def test_run_while_paused_points_at_resume(self, client, silent_turn):
        task_id = client.post("/tasks", json={"title": "Paused"}).json()["id"]
        client.post(f"/tasks/{task_id}/pause")

        response = client.post(f"/tasks/{task_id}/run")

        assert response.status_code == 409
        assert "resume" in response.json()["error"].lower()
        assert silent_turn == []

    def test_a_second_run_of_the_same_task_is_refused(self, client, slow_turn):
        task_id = client.post("/tasks", json={"title": "Once"}).json()["id"]
        assert client.post(f"/tasks/{task_id}/run").status_code == 202

        # The handle is registered before the request returns, so this does
        # not depend on the background turn reaching its first await.
        assert client.post(f"/tasks/{task_id}/run").status_code == 409

        # Let the background turn finish so it does not outlive the test.
        self._await(lambda: task_id in slow_turn, timeout=1.0)
        time.sleep(0.4)
        assert slow_turn == [task_id]

    def test_retry_mints_a_child_with_lineage(self, client, silent_turn):
        task_id = client.post(
            "/tasks", json={"title": "Broken", "request": "the original request"}
        ).json()["id"]
        client.post(f"/tasks/{task_id}/cancel")

        response = client.post(f"/tasks/{task_id}/retry")

        assert response.status_code == 202
        payload = response.json()
        child = payload["task"]
        assert payload["retry_of"] == task_id
        assert child["parent_task_id"] == task_id
        assert child["request"] == "the original request"
        assert self._await(lambda: child["id"] in silent_turn)
        # The original keeps the history that explains why a retry exists.
        assert client.get(f"/tasks/{task_id}").json()["status"] == "cancelled"

    def test_retry_of_a_live_task_points_at_run(self, client):
        task_id = client.post("/tasks", json={"title": "Live"}).json()["id"]

        response = client.post(f"/tasks/{task_id}/retry")

        assert response.status_code == 409
        assert "run" in response.json()["error"].lower()

    def test_retry_of_a_missing_task_is_a_404(self, client):
        assert client.post("/tasks/999999/retry").status_code == 404


class TestPauseIsDurable:
    """A pause has to survive the reader it was written for.

    The flag lives on the row; before it was persisted, ``pause()`` mutated
    only the caller's object and the next ``GET`` read a row that had never
    heard of it — so a paused task ran anyway on the next dispatch.
    """

    def test_pausing_is_visible_to_the_next_reader(self, client):
        task_id = client.post("/tasks", json={"title": "P"}).json()["id"]

        client.post(f"/tasks/{task_id}/pause")
        assert client.get(f"/tasks/{task_id}").json()["paused"] is True

        client.post(f"/tasks/{task_id}/resume")
        assert client.get(f"/tasks/{task_id}").json()["paused"] is False


class TestRemindersMoved:
    """`/tasks` used to mean reminders. They now live at `/reminders`."""

    def test_reminders_is_the_reminder_list(self, client):
        response = client.get("/reminders")
        assert response.status_code == 200
        assert "tasks" in response.json()

    def test_reminders_accepts_both_spellings(self, client):
        payload = client.get("/reminders").json()
        assert payload["reminders"] == payload["tasks"]

    def test_creating_a_reminder_without_a_time_is_refused(self, client):
        response = client.post("/reminders", json={"task": "no time"})
        assert response.status_code == 400
        assert "scheduled_time" in response.json()["error"]

    def test_an_unparseable_time_is_refused(self, client):
        response = client.post("/reminders", json={"task": "x", "time": "not-a-time"})
        assert response.status_code == 400
        assert "ISO 8601" in response.json()["error"]

    def test_tasks_no_longer_creates_a_reminder(self, client):
        """The old behaviour would have silently created a reminder from
        `{"task": ..., "time": ...}`."""
        response = client.post("/tasks", json={"task": "x", "time": "2030-01-01T10:00:00"})
        assert response.status_code == 400
        assert "title" in response.json()["error"]


class TestReminderVisibility:
    """A reminder must outlive the conversation that created it.

    The WS socket id is `sess_<timestamp>_<ip>` — a *different value on every
    reconnect* — and listing filtered on it, while the firing path
    (`get_due_reminders`) never did. The result was the strangest possible
    failure: the turn said "Reminder set", the scheduler would have fired it,
    and every view of the panel said there were none. `session_id` records
    *who asked*; it is not a visibility partition.
    """

    def _create(self, client, session_id):
        response = client.post("/reminders", json={
            "task": "e2e visibility probe",
            "time": "2031-01-01T09:00:00",
            "session_id": session_id,
        })
        assert response.status_code == 201, response.text
        reminder = response.json()
        return reminder.get("reminder_id") or reminder.get("id")

    @staticmethod
    def _ids(payload):
        return [
            r.get("reminder_id") or r.get("id")
            for r in (payload.get("reminders") or payload.get("tasks") or [])
        ]

    def test_any_sessions_reminder_appears_in_the_plain_list(self, client):
        reminder_id = self._create(client, "sess_999999_1")
        try:
            plain = client.get("/reminders")
            assert plain.status_code == 200
            assert reminder_id in self._ids(plain.json()), (
                "a reminder created from a chat session is invisible to the panel"
            )

            # An explicit session filter still filters — attribution queries
            # keep working.
            mine = client.get("/reminders", params={"session_id": "sess_999999_1"})
            assert reminder_id in self._ids(mine.json())
            other = client.get("/reminders", params={"session_id": "sess_111111_2"})
            assert reminder_id not in self._ids(other.json())
        finally:
            client.delete(f"/reminders/{reminder_id}")

    def test_get_by_id_finds_a_conversation_reminder(self, client):
        """`ReminderService.get()` listed through the same default, so the
        single-reminder read 404'd on anything not created under "default"."""
        reminder_id = self._create(client, "sess_999999_1")
        try:
            response = client.get(f"/reminders/{reminder_id}")
            assert response.status_code == 200
            assert response.json().get("session_id") == "sess_999999_1"
        finally:
            client.delete(f"/reminders/{reminder_id}")


class TestProjects:
    def test_open_and_list(self, client, temp_workspace):
        with open(os.path.join(temp_workspace, "package.json"), "w", encoding="utf-8") as handle:
            handle.write('{"name": "demo", "scripts": {"test": "vitest"}}')

        created = client.post("/projects", json={"path": "."})
        assert created.status_code == 201
        project = created.json()
        assert project["kind"] == "node"
        assert project["test_command"] == "npm run test"

        listed = client.get("/projects").json()
        assert listed["count"] >= 1

    def test_an_empty_path_is_refused(self, client):
        assert client.post("/projects", json={"path": "  "}).status_code == 400

    def test_a_path_outside_the_workspace_names_the_boundary(self, client, temp_workspace):
        """The containment message must say where the boundary is, or the user
        is left guessing."""
        response = client.post("/projects", json={"path": os.path.join(temp_workspace, "..", "..")})
        assert response.status_code == 400
        assert temp_workspace in response.json()["error"]

    def test_detect_does_not_register(self, client, temp_workspace):
        response = client.post("/projects/detect", json={"path": "."})
        assert response.status_code == 200
        assert client.get("/projects").json()["count"] == 0

    def test_context_shows_what_the_model_would_be_told(self, client, temp_workspace):
        """A context block the user cannot inspect is a source of confident
        mistakes they cannot correct."""
        project_id = client.post("/projects", json={"path": "."}).json()["id"]
        context = client.get(f"/projects/{project_id}/context").json()
        assert "PROJECT:" in context["context"]

    def test_commands_can_be_corrected(self, client):
        project_id = client.post("/projects", json={"path": "."}).json()["id"]
        response = client.patch(
            f"/projects/{project_id}/commands", json={"test_command": "pnpm vitest"}
        )
        assert response.status_code == 200
        assert response.json()["test_command"] == "pnpm vitest"

    def test_pinning(self, client):
        project_id = client.post("/projects", json={"path": "."}).json()["id"]
        assert client.post(f"/projects/{project_id}/pin").json()["pinned"] is True
        assert client.post(
            f"/projects/{project_id}/pin", json={"pinned": False}
        ).json()["pinned"] is False

    def test_an_unknown_project_is_a_404(self, client):
        assert client.get("/projects/nope").status_code == 404
        assert client.post("/projects/nope/pin").status_code == 404
        assert client.get("/projects/nope/context").status_code == 404
        assert client.delete("/projects/nope").status_code == 404

    def test_recent_is_available(self, client):
        client.post("/projects", json={"path": "."})
        assert "projects" in client.get("/projects/recent").json()


class TestFiles:
    def test_root(self, client, temp_workspace):
        payload = client.get("/files/root").json()
        assert payload["root"] == temp_workspace

    def test_list_and_tree(self, client, temp_workspace):
        with open(os.path.join(temp_workspace, "a.py"), "w", encoding="utf-8") as handle:
            handle.write("print(1)")

        listing = client.get("/files/list").json()
        assert any(e["name"] == "a.py" for e in listing["entries"])

        tree = client.get("/files/tree", params={"depth": 2}).json()
        assert tree["path"] == "."

    def test_read_write_and_stat(self, client, temp_workspace):
        written = client.post("/files/write", json={"path": "a.py", "content": "print(1)"})
        assert written.status_code == 200

        read = client.get("/files/read", params={"path": "a.py"}).json()
        assert read["content"] == "print(1)"
        assert read["language"] == "python"

        stat = client.get("/files/stat", params={"path": "a.py"}).json()
        assert stat["name"] == "a.py"

    def test_an_escape_is_a_403_not_a_404(self, client, temp_workspace):
        """The distinction is what lets the UI say "that is not yours to touch"
        rather than "check the path"."""
        escape = os.path.join(temp_workspace, "..", "..")
        assert client.get("/files/read", params={"path": escape}).status_code == 403
        assert client.get("/files/stat", params={"path": escape}).status_code == 403
        assert client.post(
            "/files/write", json={"path": escape, "content": "x"}
        ).status_code == 403

    def test_a_missing_file_is_a_404(self, client):
        assert client.get("/files/read", params={"path": "absent"}).status_code == 404

    def test_a_directory_read_is_a_400(self, client, temp_workspace):
        os.makedirs(os.path.join(temp_workspace, "d"))
        assert client.get("/files/read", params={"path": "d"}).status_code == 400

    def test_malformed_writes_are_422(self, client):
        assert client.post("/files/write", json={"content": "x"}).status_code == 422
        assert client.post("/files/write", json={"path": "a"}).status_code == 422
        assert client.post(
            "/files/write", json={"path": "a", "content": 123}
        ).status_code == 422

    def test_mkdir_rename_move_copy_delete(self, client, temp_workspace):
        assert client.post("/files/mkdir", json={"path": "dir"}).status_code == 200

        client.post("/files/write", json={"path": "dir/a.txt", "content": "x"})
        assert client.post(
            "/files/rename", json={"path": "dir/a.txt", "name": "b.txt"}
        ).status_code == 200

        assert client.post(
            "/files/move", json={"source": "dir/b.txt", "destination": "dir/c.txt"}
        ).status_code == 200
        assert client.post(
            "/files/copy", json={"source": "dir/c.txt", "destination": "dir/d.txt"}
        ).status_code == 200
        assert client.post("/files/delete", json={"path": "dir/d.txt"}).status_code == 200

    def test_search(self, client, temp_workspace):
        with open(os.path.join(temp_workspace, "a.py"), "w", encoding="utf-8") as handle:
            handle.write("needle\n")
        response = client.get("/files/search", params={"q": "needle"})
        assert response.status_code == 200
        assert response.json()["count"] == 1

    def test_search_needs_a_query(self, client):
        response = client.get("/files/search", params={"q": ""})
        assert response.status_code in (404, 422)

    def test_diff_does_not_write(self, client, temp_workspace):
        client.post("/files/write", json={"path": "a.py", "content": "original"})
        diff = client.get("/files/diff", params={
            "path": "a.py", "content": "proposed",
        }).json()
        assert diff["changed"] is True
        assert "+proposed" in diff["diff"]
        assert client.get("/files/read", params={"path": "a.py"}).json()["content"] == "original"


class TestTerminal:
    def test_open_list_and_close(self, client):
        opened = client.post("/terminal", json={"command": "python -c pass"})
        assert opened.status_code == 201
        session_id = opened.json()["id"]

        assert any(
            s["id"] == session_id for s in client.get("/terminal").json()["sessions"]
        )
        assert client.delete(f"/terminal/{session_id}").status_code == 200

    def test_an_empty_command_is_refused(self, client):
        assert client.post("/terminal", json={"command": "  "}).status_code == 422

    def test_command_chaining_is_a_403(self, client):
        """A sandbox refusal is a policy decision: approving it would not help."""
        response = client.post("/terminal", json={"command": "python -c pass & hostname"})
        assert response.status_code == 403
        assert "chaining" in response.json()["error"]

    def test_an_unknown_session_is_a_404(self, client):
        assert client.delete("/terminal/term_nope").status_code == 404
        assert client.get("/terminal/term_nope").status_code == 404

    def test_input_needs_data(self, client):
        session_id = client.post("/terminal", json={"command": "python -c pass"}).json()["id"]
        assert client.post(
            f"/terminal/{session_id}/input", json={}
        ).status_code == 422

    def test_env_and_timeout_pass_through(self, client):
        opened = client.post("/terminal", json={
            "command": "python -c pass",
            "env": {"ZAY_API_TEST": "sup3r-s3cret-value"},
            "timeout": 45,
        })
        assert opened.status_code == 201
        payload = opened.json()
        assert payload["timeout_s"] == 45
        assert "ZAY_API_TEST" in payload["env_keys"]
        # Key names are the answer to "did my overlay apply"; values are a
        # credential leak wearing a debugging feature.
        assert "sup3r-s3cret-value" not in str(payload)
        client.delete(f"/terminal/{payload['id']}")

    def test_an_invalid_env_key_is_a_400(self, client):
        response = client.post(
            "/terminal", json={"command": "python -c pass", "env": {"BAD KEY": "v"}}
        )
        assert response.status_code == 400

    def test_an_absurd_timeout_is_a_400(self, client):
        response = client.post(
            "/terminal", json={"command": "python -c pass", "timeout": 9_999_999}
        )
        assert response.status_code == 400


class TestSkills:
    def test_create_list_and_remove(self, client, temp_skills_dir):
        created = client.post("/skills", json={
            "name": "laravel-security", "description": "Laravel security review",
        })
        assert created.status_code == 201
        assert created.json()["name"] == "laravel-security"

        listed = client.get("/skills").json()
        assert any(s["name"] == "laravel-security" for s in listed["skills"])

        assert client.delete("/skills/laravel-security").status_code == 200

    def test_creating_twice_is_a_409(self, client, temp_skills_dir):
        client.post("/skills", json={"name": "laravel"})
        assert client.post("/skills", json={"name": "laravel"}).status_code == 409

    def test_an_unsafe_name_is_refused(self, client, temp_skills_dir):
        response = client.post("/skills", json={"name": "../escape"})
        assert response.status_code == 422
        assert "letters" in response.json()["error"]

    def test_a_name_is_required(self, client, temp_skills_dir):
        assert client.post("/skills", json={}).status_code == 422

    def test_enable_and_disable(self, client, temp_skills_dir):
        client.post("/skills", json={"name": "laravel"})
        assert client.post("/skills/laravel/enable").json()["enabled"] is True
        assert client.post(
            "/skills/laravel/enable", json={"enabled": False}
        ).json()["enabled"] is False

    def test_exceeding_the_limit_is_a_409(self, client, temp_skills_dir):
        from core.skills import MAX_ENABLED

        # Fill the quota. Creating a skill leaves it *disabled*, so the enables
        # are what fill it — a new skill is not active until it is switched on.
        for index in range(MAX_ENABLED):
            client.post("/skills", json={"name": f"skill{index}", "enabled": False})
            client.post(f"/skills/skill{index}/enable")

        client.post("/skills", json={"name": "one-too-many"})
        assert client.post("/skills/one-too-many/enable").status_code == 409

    def test_a_created_skill_starts_disabled(self, client, temp_skills_dir):
        """Creating is not enabling.

        A user who installs fifteen skills has not asked for fifteen skills'
        instructions in every prompt.
        """
        client.post("/skills", json={"name": "laravel-security"})
        listing = client.get("/skills").json()
        entry = next(s for s in listing["skills"] if s["name"] == "laravel-security")
        assert entry["enabled"] is False

    def test_inspect_shows_the_instructions(self, client, temp_skills_dir):
        client.post("/skills", json={
            "name": "laravel", "instructions": "Check for SQL injection.",
        })
        detail = client.get("/skills/laravel").json()
        assert "SQL injection" in detail["content"]

    def test_an_unknown_skill_is_a_404(self, client):
        assert client.get("/skills/absent").status_code == 404
        assert client.delete("/skills/absent").status_code == 404

    def test_install_needs_a_source(self, client):
        assert client.post("/skills/install", json={}).status_code == 422


class TestProviders:
    def test_the_builtin_ollama_provider_exists(self, client):
        """Local-first: something must work out of the box with no account."""
        providers = client.get("/providers").json()["providers"]
        assert any(p["kind"] == "ollama" for p in providers)

    def test_no_response_carries_key_material(self, client, temp_secrets):
        """The load-bearing property of the whole secrets design."""
        from core.secrets import set_secret

        set_secret("local-ollama", "sk-secret-value-123456")

        rendered = client.get("/providers").text
        rendered += client.get("/providers/local-ollama").text
        assert "sk-secret-value-123456" not in rendered
        # The *presence* is reported; the value never is.
        assert '"has_api_key":true' in rendered.replace(" ", "")

    def test_no_route_anywhere_returns_a_secret(self, client, temp_secrets):
        """Sweeps every provider-shaped endpoint, not just the two above.

        A leak through a route nobody thought to check is exactly what this
        design exists to prevent, so the assertion is over the whole surface.
        """
        from core.secrets import set_secret

        client.post("/providers", json={
            "name": "Cloud", "kind": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "requires_key": True, "api_key": "sk-secret-value-123456",
        })
        set_secret("local-ollama", "sk-secret-value-123456")

        paths = ["/providers", "/providers/cloud", "/providers/local-ollama",
                 "/models", "/models/routing", "/health", "/system",
                 "/tools", "/permissions", "/skills", "/projects", "/tasks"]
        for path in paths:
            body = client.get(path).text
            assert "sk-secret-value-123456" not in body, f"{path} leaked the key"

    def test_create_and_delete(self, client):
        created = client.post("/providers", json={
            "name": "My Local LLM", "kind": "openai_compatible",
            "base_url": "http://localhost:1234/v1",
        })
        assert created.status_code == 201
        provider_id = created.json()["id"]
        assert client.delete(f"/providers/{provider_id}").status_code == 200

    def test_an_invalid_kind_is_422(self, client):
        response = client.post("/providers", json={
            "name": "X", "kind": "magic", "base_url": "http://x",
        })
        assert response.status_code == 422
        assert "ollama" in response.json()["error"]

    def test_a_url_without_a_scheme_is_422(self, client):
        response = client.post("/providers", json={
            "name": "X", "kind": "ollama", "base_url": "localhost:11434",
        })
        assert response.status_code == 422

    def test_credentials_in_the_url_are_refused(self, client):
        response = client.post("/providers", json={
            "name": "X", "kind": "ollama", "base_url": "http://user:pass@host",
        })
        assert response.status_code == 422

    def test_a_key_bearing_provider_needs_a_key(self, client):
        response = client.post("/providers", json={
            "name": "Cloud", "kind": "openrouter",
            "base_url": "https://openrouter.ai/api/v1", "requires_key": True,
        })
        assert response.status_code == 422
        assert "needs an API key" in response.json()["error"]

    def test_a_key_is_accepted_and_not_returned(self, client):
        created = client.post("/providers", json={
            "name": "Cloud", "kind": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "requires_key": True, "api_key": "sk-secret-value-123456",
        })
        assert created.status_code == 201
        assert created.json()["has_api_key"] is True
        assert "sk-secret-value-123456" not in created.text

    def test_an_unknown_provider_is_a_404(self, client):
        assert client.get("/providers/nope").status_code == 404
        assert client.delete("/providers/nope").status_code == 404


class TestModels:
    def test_record_and_list(self, client):
        client.post("/providers", json={
            "name": "Local", "kind": "ollama", "base_url": "http://localhost:11434",
        })
        created = client.post("/models", json={
            "provider_id": "local", "id": "qwen3.5:9b",
            "capabilities": ["chat", "coding"],
        })
        assert created.status_code == 201

        listed = client.get("/models").json()
        assert any(m["id"] == "qwen3.5:9b" for m in listed["models"])

    def test_capabilities_are_documented(self, client):
        payload = client.get("/models").json()
        assert any(c["capability"] == "tools" for c in payload["capabilities"])

    def test_filtering_by_capability(self, client):
        client.post("/providers", json={
            "name": "Local", "kind": "ollama", "base_url": "http://localhost:11434",
        })
        client.post("/models", json={
            "provider_id": "local", "id": "coder:7b", "capabilities": ["chat", "coding"],
        })
        client.post("/models", json={
            "provider_id": "local", "id": "chatty:3b", "capabilities": ["chat"],
        })

        filtered = client.get("/models", params={"capabilities": "coding"}).json()
        assert [m["id"] for m in filtered["models"]] == ["coder:7b"]

    def test_a_billable_openrouter_model_is_refused(self, client):
        """The zero-cost policy must hold through the API too."""
        client.post("/providers", json={
            "name": "Cloud", "kind": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "requires_key": True, "api_key": "sk-secret-value-123456",
        })
        response = client.post("/models", json={
            "provider_id": "cloud", "id": "openai/gpt-4o",
        })
        assert response.status_code == 422
        assert "free" in response.json()["error"]

    def test_routing_preview_explains_itself(self, client):
        # The provider is named explicitly rather than relying on the default:
        # the app's lifespan installs Ollama as the default, and a preview
        # aimed at the wrong provider would report no candidates at all.
        client.post("/providers", json={
            "id": "preview-provider", "name": "Preview", "kind": "ollama",
            "base_url": "http://localhost:11434",
        })
        client.post("/models", json={
            "provider_id": "preview-provider", "id": "chatty:3b",
            "capabilities": ["chat"],
        })

        preview = client.get("/models/routing", params={
            "capabilities": "tools", "provider_id": "preview-provider",
        }).json()
        reasons = {c["model"]: c["reason"] for c in preview["candidates"]}
        assert "lacks required capabilities: tools" in reasons["chatty:3b"]

    def test_delete(self, client):
        client.post("/providers", json={
            "name": "Local", "kind": "ollama", "base_url": "http://localhost:11434",
        })
        client.post("/models", json={"provider_id": "local", "id": "gone:1b"})
        assert client.delete("/models/local/gone:1b").status_code == 200


class TestTierAssignmentApi:
    """GET/POST /models/tiers — pin, report, clear, and refuse."""

    def test_all_four_tiers_are_reported_even_when_unset(self, client):
        payload = client.get("/models/tiers").json()
        assert payload["available"] == ["fast", "coding", "reasoning", "default"]
        assert set(payload["tiers"]) == {"fast", "coding", "reasoning", "default"}
        assert all(v is None for v in payload["tiers"].values())

    def test_pin_then_report_then_clear(self, client):
        client.post("/providers", json={
            "id": "tier-provider", "name": "Tiered", "kind": "ollama",
            "base_url": "http://localhost:11434",
        })
        client.post("/models", json={
            "provider_id": "tier-provider", "id": "coder:7b",
            "capabilities": ["chat", "coding"],
        })

        saved = client.post("/models/tiers", json={
            "tier": "coding", "provider_id": "tier-provider", "model_id": "coder:7b",
        })
        assert saved.status_code == 200
        assert saved.json()["status"] == "saved"

        reported = client.get("/models/tiers").json()["tiers"]["coding"]
        assert reported["model_id"] == "coder:7b"
        assert reported["provider"] == "Tiered"
        assert reported["exists"] is True
        assert reported["enabled"] is True

        cleared = client.post("/models/tiers", json={"tier": "coding"})
        assert cleared.json()["status"] == "cleared"
        assert client.get("/models/tiers").json()["tiers"]["coding"] is None

    def test_an_unknown_tier_is_a_422(self, client):
        response = client.post("/models/tiers", json={
            "tier": "turbo", "provider_id": "x", "model_id": "m",
        })
        assert response.status_code == 422
        assert "tier" in response.json()["error"]

    def test_an_unrecorded_model_is_a_422(self, client):
        client.post("/providers", json={
            "id": "tier-provider", "name": "Tiered", "kind": "ollama",
            "base_url": "http://localhost:11434",
        })
        response = client.post("/models/tiers", json={
            "tier": "fast", "provider_id": "tier-provider", "model_id": "ghost:1b",
        })
        assert response.status_code == 422
        assert "not recorded" in response.json()["error"]

    def test_a_billable_model_cannot_be_pinned(self, client):
        """Free-only holds through the pin API as well."""
        client.post("/providers", json={
            "id": "cloud", "name": "Cloud", "kind": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "requires_key": True, "api_key": "sk-secret-value-123456",
        })
        response = client.post("/models/tiers", json={
            "tier": "fast", "provider_id": "cloud", "model_id": "openai/gpt-4o",
        })
        assert response.status_code == 422
        assert "free" in response.json()["error"]

    def test_the_routing_preview_shows_the_pin_in_use(self, client):
        client.post("/providers", json={
            "id": "tier-provider", "name": "Tiered", "kind": "ollama",
            "base_url": "http://localhost:11434",
        })
        client.post("/models", json={
            "provider_id": "tier-provider", "id": "aaa:7b",
            "capabilities": ["chat"], "priority": 1,
        })
        client.post("/models", json={
            "provider_id": "tier-provider", "id": "zzz:3b",
            "capabilities": ["chat"], "priority": 99,
        })
        client.post("/models/tiers", json={
            "tier": "fast", "provider_id": "tier-provider", "model_id": "zzz:3b",
        })

        preview = client.get("/models/routing", params={
            "tier": "fast", "provider_id": "tier-provider",
        }).json()
        assert preview["model"] == "zzz:3b"
        assert preview["source"] == "tier"


class TestPermissions:
    def test_the_vocabulary_is_listed(self, client):
        payload = client.get("/permissions").json()
        assert len(payload["vocabulary"]) == 10
        assert any(v["permission"] == "filesystem.delete" for v in payload["vocabulary"])

    def test_every_tool_is_mapped(self, client):
        tools = client.get("/permissions").json()["tools"]
        assert len(tools) >= 30
        file_write = next(t for t in tools if t["name"] == "file_write")
        assert "filesystem.write" in file_write["permissions"]

    def test_a_decision_is_stored(self, client):
        response = client.post("/permissions/filesystem.write", json={"decision": "deny"})
        assert response.status_code == 200
        assert client.get("/permissions").json()["grants"]["filesystem.write"] == "deny"

    def test_an_invalid_decision_is_422(self, client):
        response = client.post("/permissions/filesystem.write", json={"decision": "maybe"})
        assert response.status_code == 422
        assert "allow" in response.json()["error"]


class TestSystem:
    def test_reports_the_environment(self, client):
        payload = client.get("/system").json()
        assert payload["platform"]["system"]
        assert "tasks" in payload
        assert "tools" in payload
        # A duration, not a clock reading: a panel cannot render epoch time
        # as "how long has the backend been up".
        assert payload["uptime_seconds"] >= 0

    def test_resources_are_explicit_states_not_zeros(self, client):
        """The status panel must never render a fabricated `0%` or a bare dash.

        Each of the four blocks carries a status (`ok` / `sampling` /
        `unavailable` + reason), so the UI can say *why* a figure is missing.
        """
        resources = client.get("/system").json()["resources"]
        assert set(resources) == {"cpu", "memory", "disk", "network"}
        for name in ("cpu", "memory", "disk"):
            block = resources[name]
            assert block["status"] in ("ok", "sampling", "unavailable")
            if block["status"] == "unavailable":
                assert block["reason"], f"{name} unavailable without a reason"
        if resources["memory"]["status"] == "ok":
            assert 0 <= resources["memory"]["percent"] <= 100
            assert resources["memory"]["total_bytes"] > 0
        if resources["disk"]["status"] == "ok":
            assert resources["disk"]["total_bytes"] > 0
            assert resources["disk"]["path"]
        # Network is a declared state: counters are not collected, and the
        # payload says so rather than omitting the key.
        assert resources["network"]["status"] == "unavailable"
        assert resources["network"]["reason"]

    def test_health_includes_the_core(self, client):
        """Reporting only `llm` made the whole workspace look absent when it was
        merely on a different code path."""
        payload = client.get("/health").json()
        assert "core" in payload
        assert payload["core"]["providers"] >= 1

    def test_health_still_answers_with_no_models_configured(self, client):
        assert client.get("/health").status_code == 200


class TestEventFeed:
    """`GET /events/recent` — the bus's ring buffer as diagnostics (§33).

    The endpoint's whole value is that it is *bounded*, *redacted* and
    *answers*: a debugger who pulls the plug on a failing turn still gets the
    last events, and a filter for something that has not happened yet is a
    normal query rather than an error.
    """

    def test_the_feed_reports_shape_and_subscribers(self, client):
        payload = client.get("/events/recent").json()
        assert set(payload) == {"events", "count", "subscribers"}
        assert payload["count"] == len(payload["events"])
        assert payload["subscribers"] >= 0

    def test_a_task_creation_reaches_the_feed_with_its_envelope(self, client):
        created = client.post("/tasks", json={"title": "Diagnose me"}).json()
        feed = client.get("/events/recent?type=task.created").json()

        matching = [
            event
            for event in feed["events"]
            if (event.get("task") or {}).get("id") == created["id"]
        ]
        assert matching, "creating a task must appear in the event feed"
        event = matching[-1]
        assert event["type"] == "task.created"
        # The envelope every transport can rely on: identity and observation
        # time, assigned by CoreEvent and not derivable from the payload.
        assert event["event_id"]
        assert event["ts"] > 0

    def test_limit_is_clamped_rather_than_rejected(self, client):
        # Out-of-range limits are clamped by EventBus.recent, not 4xx'd: the
        # feed is a diagnostic dial, not a validated form.
        for limit in (0, -5, 99999):
            response = client.get(f"/events/recent?limit={limit}")
            assert response.status_code == 200
            assert response.json()["count"] <= 100

        tight = client.get("/events/recent?limit=1").json()
        assert tight["count"] <= 1

    def test_an_unknown_type_filter_is_an_empty_feed_not_an_error(self, client):
        response = client.get("/events/recent?type=nothing.emits.this")
        assert response.status_code == 200
        assert response.json() == {
            "events": [],
            "count": 0,
            "subscribers": response.json()["subscribers"],
        }

    def test_event_payloads_are_redacted_on_the_way_out(self, client):
        """A `task.created` payload can quote a title that carried a key."""
        key = "sk-live-abcdef123456"
        client.post("/tasks", json={"title": f"Run {key} once"})
        feed = client.get("/events/recent").json()

        assert key not in json.dumps(feed)


class TestErrorShapeIsUnified:
    """Every failing response is `{"error": ...}`, whatever raised it.

    The handlers in `api/main.py` exist because FastAPI's default is
    `{"detail": ...}`, and two shapes in one API means every client writes
    `body.error || body.detail` forever. Each probe below exercises a
    different failure *class* — a manual 404, a manual 400, an HTTPException
    422, and a real `RequestValidationError` FastAPI raises on its own — plus
    two of the endpoints added in Phases 4/7 that were never probed.
    """

    def test_every_failure_class_answers_with_error_not_detail(self, client):
        probes = {
            "manual 404": client.get("/tasks/999999"),
            "manual 400": client.post("/settings/ai", json={"provider": "skynet"}),
            "manual 422": client.post("/models/tiers", json={"tier": "turbo"}),
            "request validation": client.get("/tasks", params={"limit": "abc"}),
            "run a missing task": client.post("/tasks/999999/run"),
            "poll a pull that never ran": client.get(
                "/settings/ai/pull", params={"model": "nope:1b"}
            ),
        }
        for label, response in probes.items():
            assert response.status_code >= 400, f"{label}: {response.status_code}"
            body = response.json()
            assert "error" in body, f"{label} -> {body}"
            assert "detail" not in body, f"{label} leaked FastAPI's default shape"
            assert isinstance(body["error"], str) and body["error"]