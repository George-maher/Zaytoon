"""The task manager: lifecycle through the store, and cancellation."""

import asyncio

import pytest

from core.events import CoreEventType, EventBus
from core.tasks.manager import TaskManager
from core.tasks.models import TaskStatus


@pytest.fixture
def manager():
    return TaskManager(event_bus=EventBus())


class TestCreate:
    async def test_create_persists_and_returns_an_id(self, manager, temp_core_db):
        task = await manager.create("Fix the failing tests", request="pytest is red")
        assert task.id is not None
        assert task.status is TaskStatus.RECEIVED

        reloaded = await manager.get(task.id)
        assert reloaded is not None
        assert reloaded.title == "Fix the failing tests"

    async def test_create_emits_exactly_one_event(self, manager, temp_core_db):
        seen = []
        manager._bus.subscribe(lambda e: seen.append(e))  # noqa: SLF001

        await manager.create("Do a thing")

        created = [e for e in seen if e.type is CoreEventType.TASK_CREATED]
        assert len(created) == 1

    async def test_an_empty_title_becomes_task(self, manager, temp_core_db):
        task = await manager.create("")
        assert task.title == "Task"

    async def test_a_very_long_title_is_trimmed(self, manager, temp_core_db):
        task = await manager.create("x" * 500)
        assert len(task.title) <= 200

    async def test_metadata_is_stored(self, manager, temp_core_db):
        task = await manager.create(
            "x", intent="tools", language="ar",
            project_id="proj-1", session_id="sess-1",
            provider="local-ollama", model="qwen3.5:9b",
        )
        reloaded = await manager.get(task.id)
        assert reloaded.intent == "tools"
        assert reloaded.language == "ar"
        assert reloaded.project_id == "proj-1"
        assert reloaded.model == "qwen3.5:9b"


class TestLifecycle:
    async def test_the_happy_path(self, manager, temp_core_db):
        task = await manager.create("Implement auth")
        await manager.transition(task, TaskStatus.ANALYZING)
        await manager.set_plan(task, ["Read the models", "Add the endpoint"])
        await manager.transition(task, TaskStatus.PLANNING)
        await manager.transition(task, TaskStatus.EXECUTING)
        await manager.start_step(task, 0)
        await manager.finish_step(task, 0, success=True)
        await manager.transition(task, TaskStatus.VERIFYING)
        await manager.transition(task, TaskStatus.COMPLETED, result="Done")

        reloaded = await manager.get(task.id)
        assert reloaded.status is TaskStatus.COMPLETED
        assert reloaded.progress == 1.0
        assert reloaded.result == "Done"
        assert reloaded.steps[0].status.value == "done"

    async def test_an_illegal_transition_raises(self, manager, temp_core_db):
        task = await manager.create("x")
        from core.tasks.models import IllegalTransition

        with pytest.raises(IllegalTransition):
            await manager.transition(task, TaskStatus.COMPLETED)

    async def test_a_transition_reports_the_previous_status(self, manager, temp_core_db):
        seen = []
        manager._bus.subscribe(  # noqa: SLF001
            lambda e: seen.append(e) if e.type is CoreEventType.TASK_UPDATED else None
        )
        task = await manager.create("x")
        await manager.transition(task, TaskStatus.ANALYZING)

        assert seen[-1].data["previous"] == "received"
        assert seen[-1].data["task"]["status"] == "analyzing"

    async def test_a_failed_task_reaches_full_progress(self, manager, temp_core_db):
        task = await manager.create("x")
        await manager.transition(task, TaskStatus.ANALYZING)
        await manager.transition(task, TaskStatus.EXECUTING)
        await manager.transition(task, TaskStatus.FAILED, error="the tool was refused")

        assert task.progress == 1.0
        assert task.error == "the tool was refused"
        assert task.duration_ms is not None


class TestSteps:
    async def test_start_and_finish_a_step(self, manager, temp_core_db):
        task = await manager.create("x")
        await manager.set_plan(task, ["Read", "Write"])

        await manager.start_step(task, 0, tool="file_read")
        assert task.step(0).status.value == "running"
        assert task.step(0).tool == "file_read"
        assert task.step(0).started_at is not None

        await manager.finish_step(task, 0, success=True, detail="read 40 lines")
        assert task.step(0).status.value == "done"
        assert task.step(0).detail == "read 40 lines"
        assert task.step(0).ended_at is not None

    async def test_a_failing_step_is_marked_failed(self, manager, temp_core_db):
        task = await manager.create("x")
        await manager.set_plan(task, ["Run tests"])
        await manager.start_step(task, 0)
        await manager.finish_step(task, 0, success=False, detail="exit 1")

        assert task.step(0).status.value == "failed"
        assert task.step(0).detail == "exit 1"

    async def test_finishing_a_step_records_the_tool(self, manager, temp_core_db):
        task = await manager.create("x")
        await manager.set_plan(task, ["Run tests"])
        await manager.finish_step(task, 0, success=True, tool="execute_local_command")

        reloaded = await manager.get(task.id)
        assert "execute_local_command" in reloaded.tools_used

    async def test_an_out_of_range_step_is_a_no_op(self, manager, temp_core_db):
        task = await manager.create("x")
        await manager.set_plan(task, ["only"])
        assert await manager.start_step(task, 9) is None
        assert await manager.finish_step(task, 9) is None
        assert await manager.skip_step(task, 9, "nope") is None

    async def test_skip_records_the_reason(self, manager, temp_core_db):
        task = await manager.create("x")
        await manager.set_plan(task, ["a", "b"])
        await manager.skip_step(task, 1, "dependency failed")

        assert task.step(1).status.value == "skipped"
        assert task.step(1).detail == "dependency failed"

    async def test_steps_survive_a_reload(self, manager, temp_core_db):
        task = await manager.create("x")
        await manager.set_plan(task, ["one", "two"])
        await manager.finish_step(task, 0, success=True)

        reloaded = await manager.get(task.id)
        assert [s.title for s in reloaded.steps] == ["one", "two"]
        assert reloaded.steps[0].status.value == "done"


class TestInstrumentation:
    async def test_log_lines_are_persisted(self, manager, temp_core_db):
        task = await manager.create("x")
        await manager.log(task, "starting")
        await manager.log(task, "it went wrong", level="error")

        reloaded = await manager.get(task.id)
        assert len(reloaded.logs) == 2
        assert reloaded.logs[1]["level"] == "error"
        assert reloaded.logs[1]["message"] == "it went wrong"

    async def test_an_unknown_log_level_falls_back_to_info(self, manager, temp_core_db):
        task = await manager.create("x")
        await manager.log(task, "something", level="chatty")
        assert task.logs[-1]["level"] == "info"

    async def test_a_blank_log_line_is_dropped(self, manager, temp_core_db):
        task = await manager.create("x")
        await manager.log(task, "   ")
        assert task.logs == []

    async def test_file_changes_are_deduplicated(self, manager, temp_core_db):
        task = await manager.create("x")
        await manager.note_file_change(task, "src/app.py")
        await manager.note_file_change(task, "src/app.py")

        assert task.files_changed == ["src/app.py"]

    async def test_a_file_change_emits_an_event(self, manager, temp_core_db):
        seen = []
        manager._bus.subscribe(  # noqa: SLF001
            lambda e: seen.append(e) if e.type is CoreEventType.FILE_CHANGED else None
        )
        task = await manager.create("x")
        await manager.note_file_change(task, "src/app.py")

        assert seen[-1].data["path"] == "src/app.py"

    async def test_note_tool_deduplicates(self, manager, temp_core_db):
        task = await manager.create("x")
        await manager.note_tool(task, "file_read")
        await manager.note_tool(task, "file_read")
        assert task.tools_used == ["file_read"]


class TestCancellation:
    async def test_cancel_stops_the_work_and_the_record(self, manager, temp_core_db):
        task = await manager.create("Long running thing")
        await manager.transition(task, TaskStatus.ANALYZING)
        await manager.transition(task, TaskStatus.EXECUTING)

        started = asyncio.Event()

        async def forever():
            started.set()
            await asyncio.sleep(60)

        handle = asyncio.create_task(forever())
        await started.wait()
        manager.register_handle(task.id, handle)

        await manager.cancel(task, "user pressed stop")

        assert handle.cancelled() or handle.done()
        assert task.status is TaskStatus.CANCELLED
        assert task.ended_at is not None

    async def test_cancel_records_the_reason(self, manager, temp_core_db):
        task = await manager.create("x")
        await manager.transition(task, TaskStatus.EXECUTING)
        await manager.cancel(task, "the user changed their mind")
        assert any("changed their mind" in line["message"] for line in task.logs)

    async def test_cancelling_a_finished_task_does_not_raise(self, manager, temp_core_db):
        """The work stopped; refusing to write the record would be less accurate."""
        task = await manager.create("x")
        await manager.transition(task, TaskStatus.ANALYZING)
        await manager.transition(task, TaskStatus.COMPLETED)

        await manager.cancel(task)
        assert task.status is TaskStatus.COMPLETED

    async def test_cancel_with_no_handle_still_records(self, manager, temp_core_db):
        task = await manager.create("x")
        await manager.transition(task, TaskStatus.EXECUTING)
        await manager.cancel(task)
        assert task.status is TaskStatus.CANCELLED

    async def test_handles_can_be_registered_and_released(self, manager):
        async def noop():
            return None

        handle = asyncio.get_running_loop().create_task(noop())
        manager.register_handle(1, handle)
        assert 1 in manager._handles  # noqa: SLF001
        manager.unregister_handle(1)
        assert 1 not in manager._handles  # noqa: SLF001
        handle.cancel()


class TestPause:
    async def test_pause_is_a_flag_not_a_status(self, manager, temp_core_db):
        """Modelling pause as a status would have made "what was this doing
        before the pause" unrecoverable from the status column alone."""
        task = await manager.create("x")
        await manager.transition(task, TaskStatus.EXECUTING)

        await manager.pause(task)
        assert task.paused is True
        assert task.status is TaskStatus.EXECUTING

        await manager.resume(task)
        assert task.paused is False
        assert task.status is TaskStatus.EXECUTING


class TestRun:
    async def test_run_returns_the_result(self, manager, temp_core_db):
        task = await manager.create("x")

        async def work():
            return "the answer"

        assert await manager.run(task, work) == "the answer"
        assert task.status is TaskStatus.RECEIVED

    async def test_run_marks_the_task_failed_and_reraises(self, manager, temp_core_db):
        """Recorded *and* re-raised: swallowing would report FAILED and leave the
        caller thinking it succeeded."""
        task = await manager.create("x")

        async def work():
            raise ValueError("the tool refused")

        with pytest.raises(ValueError):
            await manager.run(task, work)

        assert task.status is TaskStatus.FAILED
        assert task.logs[-1]["level"] == "error"

    async def test_run_marks_cancellation(self, manager, temp_core_db):
        task = await manager.create("x")
        await manager.transition(task, TaskStatus.EXECUTING)

        started = asyncio.Event()

        async def work():
            started.set()
            await asyncio.sleep(60)

        async def stopper():
            await started.wait()
            # Cancels the *outer* await, which is what a user pressing stop or a
            # WebSocket interrupt actually does. A handle cancellation alone
            # would leave this test asserting something the runtime never does.
            return None

        async def runner():
            return await manager.run(task, work)

        handle = asyncio.create_task(runner())
        await started.wait()
        handle.cancel()

        with pytest.raises(asyncio.CancelledError):
            await handle

        assert task.status is TaskStatus.CANCELLED
        assert task.ended_at is not None

    async def test_run_releases_the_handle(self, manager, temp_core_db):
        task = await manager.create("x")

        async def work():
            return 1

        await manager.run(task, work)
        assert task.id not in manager._handles  # noqa: SLF001


class TestQueries:
    async def test_list_filters_by_status(self, manager, temp_core_db):
        done = await manager.create("done")
        await manager.transition(done, TaskStatus.ANALYZING)
        await manager.transition(done, TaskStatus.COMPLETED)
        await manager.create("pending")

        active = await manager.list(status="completed")
        assert [t.title for t in active] == ["done"]

    async def test_list_filters_by_session(self, manager, temp_core_db):
        await manager.create("a", session_id="sess-1")
        await manager.create("b", session_id="sess-2")

        found = await manager.list(session_id="sess-1")
        assert [t.title for t in found] == ["a"]

    async def test_list_is_newest_first(self, manager, temp_core_db):
        await manager.create("first")
        await manager.create("second")
        found = await manager.list()
        assert [t.title for t in found] == ["second", "first"]

    async def test_active_for_session_finds_the_running_one(self, manager, temp_core_db):
        finished = await manager.create("finished", session_id="s")
        await manager.transition(finished, TaskStatus.ANALYZING)
        await manager.transition(finished, TaskStatus.COMPLETED)
        live = await manager.create("live", session_id="s")
        await manager.transition(live, TaskStatus.EXECUTING)

        found = await manager.active_for_session("s")
        assert found is not None
        assert found.id == live.id

    async def test_active_for_session_returns_none_when_idle(self, manager, temp_core_db):
        assert await manager.active_for_session("nobody") is None

    async def test_delete_removes_the_row_and_children(self, manager, temp_core_db):
        task = await manager.create("x")
        await manager.set_plan(task, ["a"])
        await manager.log(task, "line")

        assert await manager.delete(task.id) is True
        assert await manager.get(task.id) is None

        from core.tasks import store
        assert await store.load_steps(task.id) == []
        assert await store.load_logs(task.id) == []


class TestStoreTrimming:
    async def test_logs_are_capped(self, manager, temp_core_db):
        from core.tasks import store
        from core.tasks.store import MAX_LOGS_PER_TASK

        task = await manager.create("x")
        for index in range(MAX_LOGS_PER_TASK + 25):
            await store.append_log(task.id, "info", f"line {index}")

        assert await store.count_logs(task.id) == MAX_LOGS_PER_TASK

    async def test_the_newest_lines_survive_the_trim(self, manager, temp_core_db):
        from core.tasks import store
        from core.tasks.store import MAX_LOGS_PER_TASK

        task = await manager.create("x")
        for index in range(MAX_LOGS_PER_TASK + 5):
            await store.append_log(task.id, "info", f"line {index}")

        rows = await store.load_logs(task.id)
        assert f"line {MAX_LOGS_PER_TASK + 4}" in {r["message"] for r in rows}

    async def test_clear_terminal_keeps_the_newest(self, manager, temp_core_db):
        for index in range(5):
            task = await manager.create(f"task {index}")
            await manager.transition(task, TaskStatus.ANALYZING)
            await manager.transition(task, TaskStatus.COMPLETED)

        from core.tasks import store
        removed = await store.clear_terminal(keep=2)

        assert removed == 3
        assert len(await manager.list()) == 2