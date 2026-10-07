"""The orchestrator: one turn, one task record, one place that sequences it.

The seam these tests pin is the important one. Before it, the turn sequence lived
in ``api/websocket.py``, which meant the task system could not see a turn, and a
second entry point would have meant a second copy of the sequence. So the
assertions here are about *sequencing and the record*, not about language
detection (already covered by the arabic suite) or agent behaviour (covered by
the engine suite).
"""

import asyncio

import pytest

from agents.events import AgentEventType
from core.events import EventBus, CoreEventType
from core.orchestrator import Orchestrator
from core.tasks.models import TaskStatus


class FakeRouter:
    """A router that records what it was asked."""

    def __init__(self, intent="tools"):
        self.intent = intent
        self.seen = []

    async def route(self, message, context):
        self.seen.append(message)
        return {"intent": self.intent}


class FakeAgents:
    """An agent service that records what it was asked and answers on demand."""

    def __init__(self, reply="done", error=None):
        self.reply = reply
        self.error = error
        self.seen = []
        self.streams = []

    async def dispatch(self, intent, message, context, language, session_id, **kwargs):
        self.seen.append({"intent": intent, "message": message, "language": language})
        stream = kwargs.get("stream_callback")
        if stream is not None:
            self.streams.append(stream)
            for token in ("all ", "done"):
                await stream(token)
        if self.error is not None:
            raise self.error
        return self.reply


class RecordingAgents(FakeAgents):
    """An agent service that fires the engine's tool events on the bus it is
    handed — the exact seam the turn recorder subscribes to.

    The engine's ``TOOL_FINISHED`` carries ``params``/``mutates`` (the tool's
    own declaration plus the arguments it ran with); this fake reproduces
    that contract so the recorder can be tested without a model.
    """

    def __init__(self, events=(), **kwargs):
        super().__init__(**kwargs)
        self.events = list(events)

    async def dispatch(self, intent, message, context, language, session_id, **kwargs):
        bus = kwargs.get("event_bus")
        for event in self.events:
            if bus is not None:
                await bus.emit(AgentEventType.TOOL_FINISHED, **event)
        return await super().dispatch(
            intent, message, context, language, session_id, **kwargs
        )


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def rig(bus):
    def build(agents=None, router=None):
        router = router or FakeRouter()
        agents = agents or FakeAgents()
        o = Orchestrator(
            agents, router, memory_agent=None, task_manager=None, event_bus=bus
        )
        return o, agents, router

    return build


class TestTheHappyPath:
    async def test_produces_a_reply(self, rig, temp_core_db, temp_workspace):
        o, _, _ = rig()
        result = await o.run_turn("run the tests", "s1")

        assert result.reply == "done"
        assert result.ok is True
        assert result.intent == "tools"

    async def test_creates_a_task_record(self, rig, temp_core_db):
        o, _, _ = rig()
        result = await o.run_turn("run the tests", "s1")

        assert result.task is not None
        assert result.task.id is not None
        assert result.task.status is TaskStatus.COMPLETED
        assert result.task.result == "done"
        assert result.task.intent == "tools"

    async def test_the_task_walks_the_lifecycle(self, rig, temp_core_db, bus):
        seen = []
        bus.subscribe(lambda e: seen.append(e))

        o, _, _ = rig()
        await o.run_turn("fix it", "s1")

        statuses = [
            e.data["task"]["status"]
            for e in seen
            if e.type is CoreEventType.TASK_UPDATED
        ]
        # Analyzing → Planning → Verifying → Completed. Not asserted as an exact
        # list: the point is that it *moved*, and that the final state is the
        # truth rather than a hard-coded "done".
        assert statuses[0] == "analyzing"
        assert statuses[-1] == "completed"
        assert "analyzing" in statuses and "planning" in statuses

    async def test_no_task_record_when_asked_not_to_create_one(
        self, rig, temp_core_db
    ):
        # A greeting is not a task. Creating one would put "hi" in the task list.
        o, _, _ = rig()
        result = await o.run_turn("hello", "s1", create_task=False)

        assert result.task is None
        assert result.reply == "done"

    async def test_streams_tokens_to_the_caller(self, rig, temp_core_db):
        o, agents, _ = rig()
        chunks = []

        async def stream(token):
            chunks.append(token)

        await o.run_turn("x", "s1", stream_callback=stream)
        assert chunks == ["all ", "done"]

    async def test_language_is_detected_and_forwarded(self, rig, temp_core_db):
        o, agents, _ = rig()
        await o.run_turn("كم الساعة دلوقتي", "s1")
        assert agents.seen[0]["language"] == "ar"

    async def test_a_language_hint_wins_over_detection(self, rig, temp_core_db):
        # Whisper's verdict beats sniffing the transcript, matching the
        # WebSocket handler's existing precedence.
        o, agents, _ = rig()
        await o.run_turn("what time is it", "s1", language_hint="ar")
        assert agents.seen[0]["language"] == "ar"


class TestPreamble:
    async def test_project_context_reaches_the_agent(
        self, rig, temp_core_db, temp_workspace
    ):
        import json

        with open(os.path.join(temp_workspace, "package.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "demo", "scripts": {"test": "vitest"}}, f)

        from core.projects import register

        project = await register(temp_workspace)

        o, agents, _ = rig()
        await o.run_turn("run the tests", "s1", project_id=project["id"])

        assert "PROJECT:" in agents.seen[0]["message"]
        assert "npm run test" in agents.seen[0]["message"]

    async def test_project_context_reaches_the_router_too(
        self, rig, temp_core_db, temp_workspace
    ):
        """Routing is a decision that depends on what the project is.

        "Run the tests" means `pytest` in one project and `npm test` in another,
        and a router told *after* it has chosen has already chosen.
        """
        import json

        with open(os.path.join(temp_workspace, "package.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "demo"}, f)

        from core.projects import register

        project = await register(temp_workspace)
        o, _, router = rig()
        await o.run_turn("run the tests", "s1", project_id=project["id"])

        assert "PROJECT:" in router.seen[0]

    async def test_no_project_means_no_preamble(self, rig, temp_core_db):
        o, agents, _ = rig()
        await o.run_turn("hello", "s1")
        assert agents.seen[0]["message"] == "hello"

    async def test_an_open_file_is_named(self, rig, temp_core_db):
        o, agents, _ = rig()
        await o.run_turn("fix this", "s1", active_file="src/app.py")
        assert "src/app.py" in agents.seen[0]["message"]

    async def test_the_preamble_is_not_stored_on_the_task(
        self, rig, temp_core_db, temp_workspace
    ):
        """A stale profile must not leak into a later turn."""
        from core.projects import register

        project = await register(temp_workspace)
        o, _, _ = rig()
        result = await o.run_turn("x", "s1", project_id=project["id"])

        assert result.task.request == "x"
        assert "PROJECT:" not in (result.task.request or "")


class TestFailure:
    async def test_a_failing_turn_marks_the_task_failed(
        self, rig, temp_core_db, bus
    ):
        o, _, _ = rig(agents=FakeAgents(error=ValueError("the tool refused")))
        result = await o.run_turn("do the thing", "s1")

        assert result.task.status is TaskStatus.FAILED
        assert result.task.error, "a failure must say something"
        assert "the tool refused" not in result.task.error, (
            "the client message must not be the raw exception"
        )

    async def test_a_failure_emits_the_update(self, rig, temp_core_db, bus):
        seen = []
        bus.subscribe(lambda e: seen.append(e))
        o, _, _ = rig(agents=FakeAgents(error=ValueError("x")))
        await o.run_turn("x", "s1")

        failures = [
            e for e in seen
            if e.type is CoreEventType.TASK_UPDATED
            and e.data["task"]["status"] == "failed"
        ]
        assert len(failures) == 1

    async def test_a_raising_progress_callback_does_not_break_the_turn(
        self, rig, temp_core_db
    ):
        # A progress display is not the turn.
        o, _, _ = rig()

        async def bad(kind, payload):
            raise RuntimeError("the display crashed")

        result = await o.run_turn("x", "s1", event_callback=bad)
        assert result.ok is True


class TestCancellation:
    async def test_cancelling_before_dispatch_stops_the_turn(self, rig, temp_core_db):
        o, agents, _ = rig()
        # Cancel the session, then ask the orchestrator what it should do.
        o.cancel_session("s1")
        result = await o.run_turn("x", "s1")

        # A fresh turn resets the session, so it runs — which is correct: the
        # cancel belonged to the *previous* turn.
        assert result.ok is True

    async def test_cancel_mid_turn_raises_and_marks_cancelled(self, rig, temp_core_db):
        started = asyncio.Event()

        class SlowAgents(FakeAgents):
            async def dispatch(self, *args, **kwargs):
                started.set()
                await asyncio.sleep(30)
                return "never"

        o, _, _ = rig(agents=SlowAgents())
        # `run_turn_in_session` is the entry point a transport uses: it registers
        # the handle that `cancel_session` reaches.
        handle = await o.run_turn_in_session("s1", "long thing")
        await started.wait()
        o.cancel_session("s1")

        with pytest.raises(asyncio.CancelledError):
            await handle

    async def test_cancelling_marks_the_task_cancelled(self, rig, temp_core_db):
        from core.tasks import get_manager

        started = asyncio.Event()

        class SlowAgents(FakeAgents):
            async def dispatch(self, *args, **kwargs):
                started.set()
                await asyncio.sleep(30)
                return "never"

        o, _, _ = rig(agents=SlowAgents())
        handle = await o.run_turn_in_session("s1", "x")
        await started.wait()

        await o.cancel_session_task("s1")
        with pytest.raises(asyncio.CancelledError):
            await handle

        # The record and the work agree — which is the property that matters.
        task = await get_manager().active_for_session("s1")
        assert task is None, "a cancelled task is terminal and must not read as active"

    async def test_cancelling_nothing_reports_false(self, rig, temp_core_db):
        o, _, _ = rig()
        assert await o.cancel_session_task("idle") is False


class TestSessions:
    async def test_each_session_gets_its_own_cancel_event(self, rig, temp_core_db):
        o, _, _ = rig()
        first = o.cancel_event_for("a")
        second = o.cancel_event_for("b")

        o.cancel_session("a")
        assert first.is_set() is True
        assert second.is_set() is False

    async def test_a_new_turn_resets_the_cancel_event(self, rig, temp_core_db):
        o, _, _ = rig()
        o.cancel_session("s1")
        assert o.is_cancelled("s1") is True

        # A *new* turn clears it: the cancel belonged to the previous one, and
        # carrying it over would cancel a turn the user never asked to stop.
        await o.run_turn("next thing", "s1")
        assert o.is_cancelled("s1") is False

    async def test_is_cancelled_reports_the_state(self, rig, temp_core_db):
        o, _, _ = rig()
        assert o.is_cancelled("s1") is False
        # The event is created on demand, so cancelling a session that has not
        # started yet still records the intent — otherwise a cancel racing a
        # turn's first await is silently lost.
        o.cancel_session("s1")
        assert o.is_cancelled("s1") is True


class TestMemory:
    async def test_memory_is_optional(self, rig, temp_core_db):
        # A caller with no memory agent gets an empty context rather than a
        # crash: the orchestrator is constructible without one.
        o, _, _ = rig()
        assert o._context.__self__ is o
        assert await o._context("s1") == []

    async def test_context_is_loaded_when_memory_is_present(self, rig, temp_core_db):
        class FakeMemory:
            def __init__(self):
                self.calls = []

            async def get_recent_context(self, session_id):
                self.calls.append(session_id)
                return [{"role": "user", "content": "earlier"}]

        memory = FakeMemory()
        o, agents, _ = rig()
        o.memory = memory
        await o.run_turn("x", "s1")
        assert memory.calls == ["s1"]

    async def test_a_memory_failure_is_not_a_turn_failure(self, rig, temp_core_db):
        class BrokenMemory:
            async def get_recent_context(self, session_id):
                raise RuntimeError("the database is locked")

        o, _, _ = rig()
        o.memory = BrokenMemory()
        result = await o.run_turn("x", "s1")
        assert result.ok is True, "context is an optimisation, not a prerequisite"


class TestTitle:
    def test_the_title_is_the_first_line(self):
        from core.orchestrator import _title_for

        assert _title_for("run the tests") == "run the tests"

    def test_a_multiline_request_uses_the_first_line(self):
        from core.orchestrator import _title_for

        assert _title_for("run the tests\nand fix them") == "run the tests"

    def test_a_long_request_is_trimmed(self):
        from core.orchestrator import _title_for

        assert len(_title_for("x" * 200)) <= 81

    def test_an_empty_request_still_names_the_task(self):
        from core.orchestrator import _title_for

        assert _title_for("") == "Task"


class TestResult:
    def test_a_result_serialises(self):
        from core.orchestrator import TurnResult

        payload = TurnResult(reply="done", intent="tools", duration_ms=12).to_dict()
        import json

        json.dumps(payload)
        assert payload["reply"] == "done"

    def test_ok_is_false_for_a_failure_or_a_cancellation(self):
        from core.orchestrator import TurnResult

        assert TurnResult(reply="x").ok is True
        assert TurnResult(reply="", error="failed").ok is False
        assert TurnResult(reply="", cancelled=True).ok is False


class TestRunningAnExistingTask:
    """``task=`` — the seam ``POST /tasks/{id}/run`` and ``/retry`` run on."""

    async def test_it_reuses_the_record_instead_of_creating_one(
        self, rig, temp_core_db
    ):
        o, agents, _ = rig()
        record = await o.tasks.create(title="Fix the bug", request="fix the bug")

        result = await o.run_turn("fix the bug", "s1", task=record)

        assert result.reply == "done"
        assert result.task is record
        assert record.status is TaskStatus.COMPLETED
        assert record.result == "done"
        assert record.intent == "tools"
        # One record in, one record out: a bound run must not mint a second.
        assert len(await o.tasks.list(limit=500)) == 1

    async def test_a_finished_record_is_refused_and_stays_finished(
        self, rig, temp_core_db
    ):
        o, agents, _ = rig()
        record = await o.tasks.create(title="Old", request="x")
        await o.tasks.transition(record, TaskStatus.FAILED)

        result = await o.run_turn("x", "s1", task=record)

        assert result.error is not None
        assert "finished" in result.error
        assert agents.seen == []                   # nothing was dispatched
        assert record.status is TaskStatus.FAILED  # the history survives
        # And no second completion was stamped over the first.
        assert len(await o.tasks.list(limit=500)) == 1

    async def test_a_record_parked_mid_flight_re_enters_the_chain(
        self, rig, temp_core_db
    ):
        # EXECUTING is what a process that died mid-turn leaves behind. The
        # re-run must continue from there instead of crashing on
        # `EXECUTING → ANALYZING`, which the transition table rightly refuses.
        o, agents, _ = rig()
        record = await o.tasks.create(title="Stuck", request="x")
        await o.tasks.transition(record, TaskStatus.EXECUTING)

        result = await o.run_turn("x", "s1", task=record)

        assert result.ok is True
        assert agents.seen                          # the work did run
        assert record.status is TaskStatus.COMPLETED

    async def test_pause_stops_the_turn_before_dispatch(self, rig, temp_core_db):
        o, agents, _ = rig()
        record = await o.tasks.create(title="Later", request="x")
        await o.tasks.pause(record)

        result = await o.run_turn("x", "s1", task=record)

        assert agents.seen == []                    # nothing executed
        assert result.ok is True                    # a pause is not an error
        assert result.reply == ""
        fresh = await o.tasks.get(record.id)
        assert fresh.paused is True
        # Parked where it stopped — not terminal, not pretending to be done.
        assert fresh.status is TaskStatus.ANALYZING

    async def test_pause_is_read_from_the_row_not_the_local_copy(
        self, rig, temp_core_db
    ):
        # REST pauses through its own manager instance on a different object
        # for the same row. The running copy never sees that assignment — if
        # the checkpoint trusted it, the pause would silently not apply.
        o, agents, _ = rig()
        record = await o.tasks.create(title="Later", request="x")
        other_copy = await o.tasks.get(record.id)
        await o.tasks.pause(other_copy)

        assert record.paused is False               # the running copy missed it…

        result = await o.run_turn("x", "s1", task=record)

        assert agents.seen == []                    # …and the row still stopped it
        assert result.ok is True

    async def test_a_pause_landing_during_routing_stops_before_dispatch(
        self, rig, temp_core_db
    ):
        # Checkpoint 2: the intent is known (routing happened) but nothing ran.
        o, agents, _ = rig()
        record = await o.tasks.create(title="R", request="x")

        class PausingRouter(FakeRouter):
            async def route(self, message, context):
                answer = await super().route(message, context)
                await o.tasks.pause(await o.tasks.get(record.id))
                return answer

        o.router = PausingRouter()

        result = await o.run_turn("x", "s1", task=record)

        assert result.intent == "tools"             # routing did happen…
        assert agents.seen == []                    # …and dispatch did not
        fresh = await o.tasks.get(record.id)
        assert fresh.paused is True

    async def test_the_pause_leaves_an_explanation_in_the_task_log(
        self, rig, temp_core_db
    ):
        o, _, _ = rig()
        record = await o.tasks.create(title="Quiet", request="x")
        await o.tasks.pause(record)

        await o.run_turn("x", "s1", task=record)

        fresh = await o.tasks.get(record.id)
        assert any("Paused" in line["message"] for line in fresh.logs)


class TestToolRecording:
    """What the turn recorder adds to the record.

    The seam is the agent bus the dispatch is given: ``TOOL_FINISHED`` events
    on it become ``tools_used`` on the task and — for a tool that declares it
    changes files — a ``files_changed`` entry plus a ``file.changed`` event,
    which is what the file panel's "changed by the assistant" marker is built
    on. Without this, ``file.changed`` was a pipeline with no producer and the
    task's tool list stayed empty for every ad-hoc tool turn.
    """

    async def test_every_completed_tool_is_recorded_on_the_task(
        self, rig, temp_core_db
    ):
        agents = RecordingAgents([
            {"name": "file_read", "success": True, "summary": "ok",
             "params": {"path": "a.txt"}},
            # Failed still counts as *used* — finish_step records a failed
            # step's tool too — but it must not claim a file change.
            {"name": "file_delete", "success": False, "summary": "declined",
             "params": {"path": "b.txt"}, "mutates": True},
        ])
        o, _, _ = rig(agents=agents)

        result = await o.run_turn("do things", "s1")

        assert result.task is not None
        assert set(result.task.tools_used) == {"file_read", "file_delete"}
        assert result.task.files_changed == []

    async def test_a_mutating_tool_records_its_paths_and_emits_file_changed(
        self, rig, temp_core_db, bus
    ):
        seen = []
        bus.subscribe(lambda e: seen.append(e))
        agents = RecordingAgents([
            {"name": "file_write", "success": True, "summary": "wrote",
             "params": {"path": "src/app.py", "content": "x = 1"},
             "mutates": True},
        ])
        o, _, _ = rig(agents=agents)

        result = await o.run_turn("write the file", "s1")

        assert result.task.files_changed == ["src/app.py"]
        changed = [e for e in seen if e.type is CoreEventType.FILE_CHANGED]
        assert changed, "the UI's indicator has no event to render"
        assert changed[0].data["path"] == "src/app.py"

    async def test_a_move_records_source_and_destination(self, rig, temp_core_db):
        agents = RecordingAgents([
            {"name": "file_move", "success": True, "summary": "moved",
             "params": {"path": "old.py", "destination": "new.py", "copy": False},
             "mutates": True},
        ])
        o, _, _ = rig(agents=agents)

        result = await o.run_turn("move it", "s1")

        assert result.task.files_changed == ["old.py", "new.py"]

    async def test_the_recorder_unsubscribes_when_the_turn_ends(
        self, rig, temp_core_db, bus
    ):
        before = bus.subscriber_count
        agents = RecordingAgents([{"name": "get_time", "success": True, "summary": "3pm"}])
        o, _, _ = rig(agents=agents)

        result = await o.run_turn("what time", "s1")

        # Back to baseline. A transport bus outlives the turn, and a stale
        # recorder would attribute the *next* turn's tools to this task.
        assert bus.subscriber_count == before

        # Delivered late — nobody is listening any more, and it must stay
        # that way.
        await bus.emit(
            AgentEventType.TOOL_FINISHED, name="file_write", success=True,
            mutates=True, params={"path": "late.txt"},
        )
        assert result.task.files_changed == []


import os  # noqa: E402 - used by the preamble tests above