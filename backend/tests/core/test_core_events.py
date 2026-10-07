"""The core event bus: fan-out, ordering, and a subscriber that cannot break a turn."""

import asyncio

import pytest

from core.events import STATUS_EVENTS, CoreEvent, CoreEventType, EventBus, NullBus


class TestFanOut:
    async def test_a_subscriber_receives_events(self):
        bus = EventBus()
        seen = []

        def collect(event):
            seen.append(event)

        bus.subscribe(collect)
        await bus.emit(CoreEventType.TASK_CREATED, task={"id": 1})

        assert len(seen) == 1
        assert seen[0].type is CoreEventType.TASK_CREATED
        assert seen[0].data["task"] == {"id": 1}

    async def test_async_subscribers_are_awaited(self):
        bus = EventBus()
        seen = []

        async def collect(event):
            await asyncio.sleep(0)
            seen.append(event.type)

        bus.subscribe(collect)
        await bus.emit(CoreEventType.TOOL_STARTED, name="file_read")

        assert seen == [CoreEventType.TOOL_STARTED]

    async def test_subscribers_run_in_registration_order(self):
        """Order matters: a transport relying on it can write to one socket."""
        bus = EventBus()
        order = []
        bus.subscribe(lambda e: order.append("first"))
        bus.subscribe(lambda e: order.append("second"))
        bus.subscribe(lambda e: order.append("third"))

        await bus.emit(CoreEventType.MODEL_STARTED)

        assert order == ["first", "second", "third"]

    async def test_unsubscribe_stops_delivery(self):
        bus = EventBus()
        seen = []

        remove = bus.subscribe(lambda e: seen.append(e))
        await bus.emit(CoreEventType.TASK_UPDATED)
        remove()
        await bus.emit(CoreEventType.TASK_UPDATED)

        assert len(seen) == 1

    async def test_unsubscribe_twice_is_harmless(self):
        bus = EventBus()
        remove = bus.subscribe(lambda e: None)
        remove()
        remove()
        assert bus.subscriber_count == 0

    async def test_unsubscribe_all(self):
        bus = EventBus()
        bus.subscribe(lambda e: None)
        bus.subscribe(lambda e: None)
        bus.unsubscribe_all()
        assert bus.subscriber_count == 0

    async def test_emit_with_no_subscribers_is_fine(self):
        await EventBus().emit(CoreEventType.TASK_CREATED)


class TestIsolation:
    async def test_a_raising_subscriber_does_not_stop_the_others(self):
        """A progress display must never be able to abort the work producing it."""
        bus = EventBus()
        seen = []

        bus.subscribe(lambda e: (_ for _ in ()).throw(RuntimeError("display is on fire")))
        bus.subscribe(lambda e: seen.append("still ran"))

        await bus.emit(CoreEventType.TOOL_COMPLETED, name="file_write")

        assert seen == ["still ran"]

    async def test_a_slow_subscriber_does_not_block_the_next(self):
        bus = EventBus()
        order = []

        async def slow(event):
            await asyncio.sleep(0.01)
            order.append("slow")

        bus.subscribe(slow)
        bus.subscribe(lambda e: order.append("fast"))

        await bus.emit(CoreEventType.TASK_STEP)

        # Sequential delivery is the documented contract; the point is that the
        # event completes and both ran, not that they overlapped.
        assert order == ["slow", "fast"]

    async def test_a_subscriber_added_mid_emit_waits_for_the_next_one(self):
        """The subscriber list is snapshotted per emit.

        So "late" joins after the first emit and does not receive it — mutating
        the list while iterating it is how a bus ends up skipping or double-
        delivering to somebody.
        """
        bus = EventBus()
        seen = []

        def subscribe_during(event):
            seen.append("first")
            bus.subscribe(lambda e: seen.append("late"))

        bus.subscribe(subscribe_during)
        await bus.emit(CoreEventType.TASK_CREATED)
        assert seen == ["first"]

        await bus.emit(CoreEventType.TASK_UPDATED)
        assert seen == ["first", "first", "late"]


class TestSerialisation:
    def test_to_dict_flattens_type_and_data(self):
        event = CoreEvent(CoreEventType.TOOL_STARTED, {"name": "file_read"})
        payload = event.to_dict()
        assert payload["type"] == "tool.started"
        assert payload["name"] == "file_read"
        # The envelope (event_id, ts) travels with every event — see
        # TestEnvelope — so exact-equality against the payload alone is no
        # longer the contract.

    def test_status_classification(self):
        status = CoreEvent(CoreEventType.TASK_UPDATED)
        content = CoreEvent(CoreEventType.TOOL_OUTPUT, {"text": "..."})
        assert status.is_status is True
        assert content.is_status is False

    def test_status_events_exclude_content(self):
        assert CoreEventType.TOOL_OUTPUT not in STATUS_EVENTS
        assert CoreEventType.MODEL_TOKEN not in STATUS_EVENTS
        assert CoreEventType.TASK_STEP in STATUS_EVENTS


class TestEnvelope:
    def test_every_event_carries_an_id_and_timestamp(self):
        event = CoreEvent(CoreEventType.TASK_CREATED, {"task": {"id": 1}})
        payload = event.to_dict()
        assert payload["event_id"]
        assert payload["ts"] > 0
        assert payload["task"] == {"id": 1}

    def test_ids_are_unique_per_event(self):
        ids = {CoreEvent(CoreEventType.TASK_UPDATED).event_id for _ in range(50)}
        assert len(ids) == 50

    def test_the_envelope_wins_a_key_collision(self):
        # A payload carrying its own `event_id`/`ts` must not shadow the
        # observation's identity: two winners for one field, decided by which
        # key the client happened to read, is what an envelope exists to
        # prevent.
        event = CoreEvent(CoreEventType.TASK_LOG, {"event_id": "spoofed", "ts": 0})
        payload = event.to_dict()
        assert payload["event_id"] == event.event_id
        assert payload["event_id"] != "spoofed"
        assert payload["ts"] == event.ts


class TestRecent:
    async def test_recent_keeps_a_tail(self):
        bus = EventBus()
        for index in range(5):
            await bus.emit(CoreEventType.TASK_UPDATED, n=index)

        recent = bus.recent(limit=3)
        assert [e.data["n"] for e in recent] == [2, 3, 4]

    async def test_recent_is_capped_by_the_buffer(self):
        bus = EventBus()
        for index in range(150):
            await bus.emit(CoreEventType.TASK_UPDATED, n=index)
        assert len(bus._recent) == 100  # noqa: SLF001 - asserting the bound


class TestPublish:
    async def test_publish_from_async_code_reaches_subscribers(self):
        bus = EventBus()
        seen = asyncio.Event()

        async def collect(event):
            seen.set()

        bus.subscribe(collect)
        bus.publish(CoreEventType.FILE_CHANGED, path="a.txt")
        await asyncio.wait_for(seen.wait(), timeout=1.0)

    def test_publish_without_a_loop_is_dropped_not_raised(self):
        """A store update during startup must not raise."""
        bus = EventBus()
        bus.publish(CoreEventType.PROVIDER_CHANGED, provider_id="x")

    async def test_publish_delivers_the_payload(self):
        bus = EventBus()
        got = []

        bus.subscribe(lambda e: got.append(e.data))
        bus.publish(CoreEventType.SKILL_CHANGED, skill="laravel")
        await asyncio.sleep(0)

        assert got == [{"skill": "laravel"}]


class TestNullBus:
    async def test_emit_is_a_no_op(self):
        await NullBus().emit(CoreEventType.TASK_CREATED)

    def test_publish_is_a_no_op(self):
        NullBus().publish(CoreEventType.TASK_CREATED)

    async def test_it_still_works_as_a_subscriber_target(self):
        bus = NullBus()
        bus.subscribe(lambda e: pytest.fail("NullBus must not deliver"))
        await bus.emit(CoreEventType.ERROR)


class TestConvenience:
    async def test_task_helpers_carry_the_right_shape(self):
        bus = EventBus()
        seen = []
        bus.subscribe(lambda e: seen.append(e))

        await bus.task_created({"id": 7})
        await bus.task_updated({"id": 7, "status": "executing"})
        await bus.task_step(7, {"title": "Run tests", "status": "running"})
        await bus.task_log(7, "info", "started")

        assert [e.type for e in seen] == [
            CoreEventType.TASK_CREATED,
            CoreEventType.TASK_UPDATED,
            CoreEventType.TASK_STEP,
            CoreEventType.TASK_LOG,
        ]
        assert seen[2].data["task_id"] == 7

    async def test_tool_helpers(self):
        bus = EventBus()
        seen = []
        bus.subscribe(lambda e: seen.append(e.to_dict()))

        await bus.tool_started("file_read", task_id=3)
        await bus.tool_completed("file_read", True, summary="read 40 lines")
        await bus.tool_failed("file_write", "disk full")
        await bus.file_changed("src/app.py", "changed")

        assert seen[0]["type"] == "tool.started"
        assert seen[1]["summary"] == "read 40 lines"
        assert seen[2]["error"] == "disk full"
        assert seen[3]["path"] == "src/app.py"