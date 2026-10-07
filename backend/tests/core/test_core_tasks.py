"""The task lifecycle: what moves where, and what refuses to move."""

import pytest

from core.tasks.models import (
    IllegalTransition,
    Task,
    TaskStatus,
    TaskStep,
    TaskStepStatus,
    new_task_id,
    progress_for_status,
)


class TestTransitions:
    @pytest.mark.parametrize("target", [
        TaskStatus.ANALYZING,
        TaskStatus.PLANNING,
        TaskStatus.EXECUTING,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    ])
    def test_received_can_start(self, target):
        assert TaskStatus.RECEIVED.can_transition_to(target) is True

    def test_received_cannot_jump_to_completed(self):
        """The whole point of validating: a task log that lies about its own
        history is worse than an error."""
        assert TaskStatus.RECEIVED.can_transition_to(TaskStatus.COMPLETED) is False

    def test_the_happy_path_is_legal(self):
        path = [
            TaskStatus.ANALYZING,
            TaskStatus.PLANNING,
            TaskStatus.WAITING_FOR_PERMISSION,
            TaskStatus.EXECUTING,
            TaskStatus.VERIFYING,
            TaskStatus.COMPLETED,
        ]
        status = TaskStatus.RECEIVED
        for target in path:
            assert status.can_transition_to(target), f"{status} -> {target}"
            status = target

    def test_executing_may_skip_verification(self):
        """A read-only task has nothing to verify; inventing a step is theatre."""
        assert TaskStatus.EXECUTING.can_transition_to(TaskStatus.COMPLETED) is True

    def test_a_conversational_turn_can_finish_from_analysing(self):
        """Answered from context, with no tools and nothing to verify."""
        assert TaskStatus.ANALYZING.can_transition_to(TaskStatus.COMPLETED) is True

    def test_a_plan_that_needed_no_tools_can_finish(self):
        assert TaskStatus.PLANNING.can_transition_to(TaskStatus.COMPLETED) is True

    def test_a_waiting_task_cannot_finish_without_being_allowed_to(self):
        """It is blocked on a human, not on work, so completing it would be a lie."""
        assert TaskStatus.WAITING_FOR_PERMISSION.can_transition_to(TaskStatus.COMPLETED) is False
        assert TaskStatus.WAITING_FOR_USER.can_transition_to(TaskStatus.COMPLETED) is False

    @pytest.mark.parametrize("terminal", [
        TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED,
    ])
    def test_terminal_states_are_final(self, terminal):
        for target in TaskStatus:
            assert terminal.can_transition_to(target) is False, f"{terminal} -> {target}"

    @pytest.mark.parametrize("non_terminal", [
        TaskStatus.RECEIVED, TaskStatus.ANALYZING, TaskStatus.PLANNING,
        TaskStatus.WAITING_FOR_PERMISSION, TaskStatus.WAITING_FOR_USER,
        TaskStatus.EXECUTING, TaskStatus.VERIFYING,
    ])
    def test_every_live_state_can_fail_or_cancel(self, non_terminal):
        assert non_terminal.can_transition_to(TaskStatus.FAILED) is True
        assert non_terminal.can_transition_to(TaskStatus.CANCELLED) is True

    def test_verifying_cannot_go_back_to_analyzing(self):
        assert TaskStatus.VERIFYING.can_transition_to(TaskStatus.ANALYZING) is False

    def test_analysing_can_wait_for_the_user(self):
        assert TaskStatus.ANALYZING.can_transition_to(TaskStatus.WAITING_FOR_USER) is True

    def test_waiting_for_user_can_resume_planning(self):
        assert TaskStatus.WAITING_FOR_USER.can_transition_to(TaskStatus.PLANNING) is True


class TestTaskTransition:
    def test_transition_moves_and_stamps_the_start(self):
        task = Task(title="Fix the tests")
        assert task.started_at is None

        task.transition(TaskStatus.ANALYZING)

        assert task.status is TaskStatus.ANALYZING
        assert task.started_at is not None

    def test_a_terminal_transition_stamps_the_end_and_a_duration(self):
        task = Task(title="Fix the tests")
        task.transition(TaskStatus.ANALYZING)
        task.transition(TaskStatus.EXECUTING)
        task.transition(TaskStatus.COMPLETED)

        assert task.ended_at is not None
        assert task.duration_ms is not None and task.duration_ms >= 0

    def test_an_illegal_transition_raises_and_changes_nothing(self):
        task = Task(title="x")
        with pytest.raises(IllegalTransition) as caught:
            task.transition(TaskStatus.COMPLETED)

        assert caught.value.current is TaskStatus.RECEIVED
        assert caught.value.target is TaskStatus.COMPLETED
        assert task.status is TaskStatus.RECEIVED

    def test_illegal_transition_is_a_value_error(self):
        """Broad handlers in the codebase catch ValueError; this must not escape them."""
        with pytest.raises(ValueError):
            Task(title="x").transition(TaskStatus.COMPLETED)

    def test_transitioning_to_the_same_state_is_a_no_op(self):
        task = Task(title="x")
        task.transition(TaskStatus.ANALYZING)
        before = task.started_at
        task.transition(TaskStatus.ANALYZING)
        assert task.started_at == before

    def test_a_string_status_is_accepted(self):
        task = Task(title="x")
        task.transition("analyzing")
        assert task.status is TaskStatus.ANALYZING

    def test_progress_advances_on_a_terminal_failure(self):
        """A failed task reached the end of its life; freezing the bar at the
        failure point reads as a hang."""
        task = Task(title="x")
        task.transition(TaskStatus.ANALYZING)
        task.transition(TaskStatus.EXECUTING)
        task.transition(TaskStatus.FAILED)
        assert task.progress == 1.0


class TestClassification:
    def test_terminal_states(self):
        for status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            assert status.is_terminal is True
        assert TaskStatus.EXECUTING.is_terminal is False

    def test_active_states(self):
        for status in (TaskStatus.ANALYZING, TaskStatus.PLANNING,
                       TaskStatus.EXECUTING, TaskStatus.VERIFYING):
            assert status.is_active is True
        assert TaskStatus.WAITING_FOR_PERMISSION.is_active is False

    def test_every_status_has_a_description(self):
        for status in TaskStatus:
            assert status.describe(), f"{status} has no description"

    def test_status_progress_is_monotonic(self):
        ordered = [
            TaskStatus.RECEIVED, TaskStatus.ANALYZING, TaskStatus.PLANNING,
            TaskStatus.EXECUTING, TaskStatus.VERIFYING, TaskStatus.COMPLETED,
        ]
        values = [progress_for_status(s) for s in ordered]
        assert values == sorted(values)


class TestProgress:
    def test_no_steps_falls_back_to_the_status_floor(self):
        task = Task(title="x")
        task.transition(TaskStatus.EXECUTING)
        assert task.refresh_progress() >= 0.5

    def test_steps_drive_the_progress(self):
        task = Task(title="x")
        task.steps = [
            TaskStep(title="a", seq=0, status=TaskStepStatus.DONE),
            TaskStep(title="b", seq=1, status=TaskStepStatus.DONE),
            TaskStep(title="c", seq=2, status=TaskStepStatus.RUNNING),
            TaskStep(title="d", seq=3, status=TaskStepStatus.PENDING),
        ]
        # 2 done + 0.5 running + 0 + 0 = 2.5 of 4
        assert task.refresh_progress() == 0.625

    def test_progress_never_moves_backwards(self):
        """A revised plan must not rewind the bar the user is watching."""
        task = Task(title="x")
        task.progress = 0.9
        task.steps = [TaskStep(title="a", seq=0, status=TaskStepStatus.PENDING)]
        assert task.refresh_progress() == 0.9

    def test_a_failed_step_counts_as_finished_work(self):
        """Reporting it as 0 would freeze the bar where things went wrong."""
        task = Task(title="x")
        task.steps = [
            TaskStep(title="a", seq=0, status=TaskStepStatus.FAILED),
            TaskStep(title="b", seq=1, status=TaskStepStatus.PENDING),
        ]
        assert task.refresh_progress() == 0.5

    def test_a_running_step_is_half_done(self):
        task = Task(title="x")
        task.steps = [TaskStep(title="a", seq=0, status=TaskStepStatus.RUNNING)]
        assert task.refresh_progress() == 0.5


class TestPlanAndSteps:
    def test_set_plan_creates_numbered_steps(self):
        task = Task(title="x")
        task.set_plan(["Inspect the project", "Write the fix", "Run the tests"])
        assert [s.seq for s in task.steps] == [0, 1, 2]
        assert [s.title for s in task.steps] == [
            "Inspect the project", "Write the fix", "Run the tests",
        ]
        assert all(s.status is TaskStepStatus.PENDING for s in task.steps)

    def test_blank_plan_entries_are_dropped(self):
        task = Task(title="x")
        task.set_plan(["real", "", "   ", "also real"])
        assert task.plan == ["real", "also real"]

    def test_step_lookup_by_sequence(self):
        task = Task(title="x")
        task.set_plan(["a", "b"])
        assert task.step(1).title == "b"
        assert task.step(9) is None

    def test_an_empty_plan_is_allowed(self):
        task = Task(title="x")
        task.set_plan([])
        assert task.steps == []


class TestSerialisation:
    def test_to_dict_is_json_safe(self):
        task = Task(title="Fix auth", request="the login flow is broken")
        task.set_plan(["Read the handler", "Fix it"])
        task.files_changed = ["src/auth.ts"]
        task.tools_used = ["file_read"]
        task.provider = "local-ollama"
        task.model = "qwen3.5:9b"

        payload = task.to_dict()

        import json
        json.dumps(payload)  # must not raise
        assert payload["title"] == "Fix auth"
        assert payload["steps"][0]["title"] == "Read the handler"
        assert payload["files_changed"] == ["src/auth.ts"]
        assert payload["is_terminal"] is False

    def test_from_row_round_trips(self):
        task = Task(title="x", id=4, status=TaskStatus.EXECUTING, progress=0.6)
        task.plan = ["a", "b"]
        payload = {
            "id": task.id, "title": task.title, "request": "",
            "status": task.status.value, "progress": task.progress,
            "plan": '["a", "b"]', "files_changed": "[]", "tools_used": "[]",
        }
        rebuilt = Task.from_row(payload)
        assert rebuilt.id == 4
        assert rebuilt.status is TaskStatus.EXECUTING
        assert rebuilt.plan == ["a", "b"]

    def test_a_corrupt_plan_cell_does_not_lose_the_task(self):
        """Losing the plan text is a smaller failure than losing the record."""
        rebuilt = Task.from_row({"id": 1, "title": "x", "plan": "{not json"})
        assert rebuilt.plan == []

    def test_a_dict_plan_cell_becomes_keys(self):
        rebuilt = Task.from_row({"id": 1, "title": "x", "plan": '{"a": 1, "b": 2}'})
        assert sorted(rebuilt.plan) == ["a", "b"]


class TestIds:
    def test_new_task_id_is_unique_and_prefixed(self):
        first, second = new_task_id(), new_task_id()
        assert first != second
        assert first.startswith("task_")