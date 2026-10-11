"""`POST /chat` can say where an answer came from, and a session leaves nothing behind.

Both items are small and both were "the REST path is a second, divergent copy of
the turn" — the audit's phrase for this class of defect. A `sources` frame reaching
a WebSocket while a REST client got prose alone meant the same turn had two levels
of accountability; per-session state that outlived a disconnect meant the same
orchestrator that serves one user accumulated another's work.

No model is needed for either: the fake agent emits the engine's own events on the
bus it is handed, which is the seam `run_turn` subscribes to.
"""

import asyncio

import pytest

from agents.events import AgentEventType
from core.events import EventBus
from core.orchestrator import Orchestrator, TurnResult

from test_orchestrator import FakeRouter  # same fakes, one idiom (tests/core is not a package)

SOURCE = {
    "n": 1,
    "kind": "page",
    "url": "https://example.com/a",
    "host": "example.com",
    "title": "A page",
    "retrieved_at": "2026-10-11T07:00:00Z",
}


class SourceAgents:
    """Dispatch that behaves like a research turn: tool events, then a `sources` event."""

    def __init__(self, events=("sources", "tool")):
        self.events = list(events)
        self.buses = []

    async def dispatch(self, intent, message, context, language, session_id, **kwargs):
        bus = kwargs.get("event_bus")
        self.buses.append(bus)
        if bus is not None:
            if "tool" in self.events:
                await bus.emit(
                    AgentEventType.TOOL_FINISHED, name="fetch_url", success=True, summary="read"
                )
            if "sources" in self.events:
                await bus.sources([SOURCE])
        return "Python 3.13 was released in October [1]."


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def rig(bus):
    def build(agents=None):
        agents = agents or SourceAgents()
        return Orchestrator(agents, FakeRouter(), memory_agent=None, task_manager=None, event_bus=bus), agents

    return build


class TestSourcesReachTheTurnResult:
    async def test_a_sourced_turn_carries_its_sources(self, rig, temp_core_db, temp_workspace):
        o, _ = rig()
        result = await o.run_turn("what is the latest python", "s1")
        assert result.sources == [SOURCE], result.sources
        # The field is part of the dict shape too, or every caller re-implements it.
        assert result.to_dict()["sources"] == [SOURCE]

    async def test_a_turn_that_read_nothing_reports_an_empty_list(self, rig, temp_core_db, temp_workspace):
        o, _ = rig(SourceAgents(events=("tool",)))
        result = await o.run_turn("what time is it", "s1")
        assert result.sources == []

    async def test_the_turn_detaches_its_own_recorders_and_nothing_else(
        self, rig, temp_core_db, temp_workspace
    ):
        """The leak this could easily have been, and the over-correction beside it.

        A transport bus outlives the turn — `api/websocket.py` builds one per
        session and hands it to every turn — so a recorder left subscribed would
        attribute the *next* turn's sources to the previous one, and grow by one
        per message forever. The opposite mistake is worse and easier to make:
        `unsubscribe_all()` would take the session's `_ProgressRelay` with it, so
        every turn after the first went silent. Both halves are pinned by counting
        on a bus that had a subscriber of its own before the turn started.
        """
        from agents.events import EventBus as AgentBus

        agent_bus = AgentBus()
        relay = agent_bus.subscribe(lambda event: None)
        assert agent_bus.subscriber_count == 1

        o, _ = rig()
        await o.run_turn("first", "s1", event_bus=agent_bus)
        assert agent_bus.subscriber_count == 1, "the turn left a recorder behind"
        await o.run_turn("second", "s1", event_bus=agent_bus)
        assert agent_bus.subscriber_count == 1, "the count grows per turn"
        relay()

    async def test_a_failing_turn_still_detaches_them(self, rig, temp_core_db, temp_workspace):
        """The `finally` is what detaches them; an exception must not skip it."""
        from agents.events import EventBus as AgentBus

        class Boom(SourceAgents):
            async def dispatch(self, *args, **kwargs):
                raise RuntimeError("provider exploded")

        agent_bus = AgentBus()
        relay = agent_bus.subscribe(lambda event: None)
        o, _ = rig(Boom())
        result = await o.run_turn("x", "s1", event_bus=agent_bus)
        assert result.error  # the turn boundary turns it into a stated error
        assert agent_bus.subscriber_count == 1
        relay()


class TestTurnResultShape:
    def test_sources_defaults_to_empty_and_survives_to_dict(self):
        assert TurnResult().sources == []
        assert TurnResult(reply="x").to_dict()["sources"] == []

    def test_a_sourceless_turn_still_reports_the_key(self):
        """Additive, but always present: a client must not branch on the key's existence."""
        payload = TurnResult(reply="x", sources=[SOURCE]).to_dict()
        assert payload["sources"][0]["url"] == "https://example.com/a"
