"""The agent loop: extraction, observations, bounding, and recovery.

The loop is the part of the product that can run away, so the tests here are
mostly about the ways it could: an unbounded loop, a model that keeps asking
for the same thing, a tool that hangs, a declined confirmation, and — most
importantly — the guarantee that intermediate model output never reaches the
user as if it were the answer.
"""

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.engine import (  # noqa: E402
    DEFAULT_MAX_ITERATIONS,
    AgentEngine,
    LoopResult,
    build_system_prompt,
    extract_tool_calls,
    render_observation,
    tool_label,
)
from agents.events import AgentEvent, AgentEventType, EventBus, NullBus  # noqa: E402
from tools.base import BaseTool, ToolResult  # noqa: E402
from tools.registry import ToolRegistry  # noqa: E402
from tools import sandbox  # noqa: E402


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------
class ScriptedLLM:
    """Replays a fixed list of responses, recording every request.

    ``None`` in the script simulates a provider failure.
    """

    tier = "fast"

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.calls = 0

    async def complete(self, messages, **kwargs):
        self.requests.append(list(messages))
        self.calls += 1
        if not self.script:
            return "Nothing left to say."
        return self.script.pop(0)


class _Recorder(BaseTool):
    """Records invocations; always succeeds."""

    permission = "safe"

    def __init__(self, name="recorder", tier="safe"):
        self._name = name
        self.permission = tier
        self.calls = []

    def name(self):
        return self._name

    def description(self):
        return "Records that it ran."

    def schema(self):
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        return ToolResult.ok(f"{self._name} ran")


class _Failing(BaseTool):
    permission = "safe"

    def __init__(self, name="broken"):
        self._name = name

    def name(self):
        return self._name

    def description(self):
        return "Always fails."

    def schema(self):
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        return ToolResult.fail("something went wrong")


class _Hanging(BaseTool):
    permission = "safe"

    def name(self):
        return "hang"

    def description(self):
        return "Never returns."

    def schema(self):
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        await asyncio.sleep(3600)


@pytest.fixture
def registry():
    reg = ToolRegistry()
    reg.register(_Recorder("get_time"))
    reg.register(_Recorder("file_write", tier="confirm"))
    reg.register(_Failing())
    return reg


def collector():
    """An EventBus plus the list of events it captured."""
    bus = EventBus()
    seen = []
    bus.subscribe(lambda event: seen.append(event))
    return bus, seen


def call(action, **params):
    return json.dumps({"action": action, **params})


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
class TestExtractToolCalls:
    def test_no_json_returns_the_text(self):
        calls, prose = extract_tool_calls("Sure, I opened Spotify for you.")
        assert calls == []
        assert "Spotify" in prose

    def test_flat_form(self):
        calls, prose = extract_tool_calls('{"action": "open_app", "app": "spotify"}')
        assert len(calls) == 1
        assert calls[0].name == "open_app"
        assert calls[0].params == {"app": "spotify"}
        assert prose == ""

    def test_nested_params_form(self):
        calls, _ = extract_tool_calls(
            '{"action": "file_write", "params": {"path": "a.txt", "content": "x"}}'
        )
        assert calls[0].params == {"path": "a.txt", "content": "x"}

    def test_several_calls_on_separate_lines(self):
        text = (
            '{"action": "get_time"}\n'
            '{"action": "file_write", "path": "a.txt", "content": "x"}'
        )
        calls, _ = extract_tool_calls(text)
        assert [c.name for c in calls] == ["get_time", "file_write"]

    def test_prose_around_a_call_is_preserved(self):
        calls, prose = extract_tool_calls(
            'Let me check. {"action": "get_time"} Done.'
        )
        assert len(calls) == 1
        assert "Let me check" in prose
        assert "Done" in prose

    def test_fenced_json_is_handled(self):
        calls, _ = extract_tool_calls(
            '```json\n{"action": "get_time"}\n```'
        )
        assert len(calls) == 1

    def test_alternate_action_keys(self):
        for key in ("action", "name", "tool", "tool_name"):
            calls, _ = extract_tool_calls(json.dumps({key: "get_time"}))
            assert len(calls) == 1, key

    def test_malformed_json_is_not_a_call(self):
        calls, prose = extract_tool_calls('{"action": "get_time", oops}')
        assert calls == []
        assert prose  # left as prose rather than swallowed

    def test_json_without_an_action_key_is_not_a_call(self):
        calls, _ = extract_tool_calls('{"path": "a.txt", "content": "x"}')
        assert calls == []

    def test_empty_input(self):
        assert extract_tool_calls("") == ([], "")

    def test_describe_is_short(self):
        """A long parameter must not be echoed wholesale into the transcript."""
        calls, _ = extract_tool_calls(
            json.dumps({"action": "system_command", "command": "x" * 500})
        )
        assert calls
        assert len(calls[0].describe()) < 90

    def test_truncated_json_does_not_hang(self):
        """A mid-stream cut-off object must not be scanned forever."""
        calls, _ = extract_tool_calls('{"action": "file_write", "params": {"path":')
        assert calls == []


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------
class TestRenderObservation:
    def _call(self):
        calls, _ = extract_tool_calls('{"action": "get_time"}')
        return calls[0]

    def test_success(self):
        text = render_observation(self._call(), {"success": True, "output": "12:00"})
        assert "12:00" in text

    def test_failure_is_visible_in_full(self):
        """The point of feeding observations back is that the model can see
        what went wrong and change course."""
        text = render_observation(self._call(), {"success": False, "error": "no such file"})
        assert "FAILED" in text
        assert "no such file" in text

    def test_sandbox_refusal_is_labelled(self):
        text = render_observation(
            self._call(), {"success": False, "blocked": True, "error": "outside workspace"}
        )
        assert "SECURITY SANDBOX" in text

    def test_declined_action_is_labelled(self):
        text = render_observation(
            self._call(), {"success": False, "cancelled": True, "error": "user said no"}
        )
        assert "NOT RUN" in text
        assert "declined" in text

    def test_needs_confirmation_is_labelled(self):
        text = render_observation(
            self._call(), {"success": False, "requires_confirmation": True, "error": "x"}
        )
        assert "NOT RUN" in text

    def test_long_output_is_truncated(self):
        text = render_observation(
            self._call(), {"success": True, "output": "x" * 20_000}
        )
        assert "truncated" in text
        assert len(text) < 6_000


# ---------------------------------------------------------------------------
# Prompts and labels
# ---------------------------------------------------------------------------
class TestPrompts:
    def test_english_prompt_lists_the_real_tools(self, registry):
        prompt = build_system_prompt("get_time, file_write", "en")
        assert "get_time" in prompt
        assert "file_write" in prompt

    def test_arabic_prompt_is_used_for_arabic(self, registry):
        arabic = build_system_prompt("get_time", "ar")
        assert any("؀" <= ch <= "ۿ" for ch in arabic)

    def test_catalogue_shows_required_parameters(self, registry):
        engine = AgentEngine(ScriptedLLM([]), registry)
        catalogue = engine._catalogue("en")
        assert "file_write" in catalogue
        assert "get_time" in catalogue

    def test_labels_are_user_facing_not_model_output(self):
        """The label is what the UI shows in place of the model's reasoning."""
        assert tool_label("create_project", "en") == "Creating the project"
        assert tool_label("run_tests", "en") == "Running tests"

    def test_arabic_labels_exist_for_every_english_one(self):
        from agents.engine import _LABELS_AR, _LABELS_EN

        assert set(_LABELS_AR) == set(_LABELS_EN)

    def test_unknown_tool_gets_a_readable_label(self):
        assert tool_label("some_new_tool", "en") == "some new tool"


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------
class TestLoopHappyPath:
    async def test_answers_immediately_when_no_tool_is_needed(self, registry):
        llm = ScriptedLLM(["The time is 3pm."])
        result = await AgentEngine(llm, registry).run("what time is it")

        assert result.text == "The time is 3pm."
        assert result.iterations == 1
        assert result.stopped_reason == "answered"
        assert not result.used_tools

    async def test_runs_a_tool_then_answers(self, registry):
        llm = ScriptedLLM([
            call("get_time"),
            "It is 3pm.",
        ])
        result = await AgentEngine(llm, registry).run("what time is it")

        assert result.text == "It is 3pm."
        assert result.iterations == 2
        assert result.tool_calls == ["get_time"]

    async def test_observations_are_fed_back(self, registry):
        """The second request must contain what the first tool did."""
        llm = ScriptedLLM([call("get_time"), "Done."])
        await AgentEngine(llm, registry).run("what time is it")

        second_request = llm.requests[1]
        blob = json.dumps(second_request)
        assert "get_time ran" in blob

    async def test_several_tools_in_one_round(self, registry):
        reg = registry
        reg.register(_Recorder("other"), replace=True)
        llm = ScriptedLLM([
            call("get_time") + "\n" + call("other"),
            "Both done.",
        ])
        result = await AgentEngine(llm, reg).run("do two things")
        assert set(result.tool_calls) == {"get_time", "other"}

    async def test_chained_dependencies_work(self, registry, monkeypatch, tmp_path):
        """create-then-write, the shape the product spec describes."""
        root = tmp_path / "ws"
        root.mkdir()
        monkeypatch.setattr(sandbox, "workspace_root", lambda: str(root))

        from tools.filesystem import CreateFolder, FileWrite

        reg = ToolRegistry()
        reg.register(CreateFolder())
        reg.register(FileWrite())

        llm = ScriptedLLM([
            '{"action": "create_folder", "path": "proj"}',
            '{"action": "file_write", "path": "proj/a.txt", "content": "hi"}',
            "Created proj with a.txt in it.",
        ])
        result = await AgentEngine(llm, reg).run("make a folder called proj and put a file in it")

        assert result.tool_calls == ["create_folder", "file_write"]
        assert (root / "proj" / "a.txt").read_text() == "hi"


class TestLoopBounding:
    async def test_stops_at_the_iteration_budget(self, registry):
        """A model that keeps calling tools must not spin forever."""
        llm = ScriptedLLM([call("get_time")] * 50)
        engine = AgentEngine(llm, registry, max_iterations=3)

        result = await engine.run("loop forever")

        assert result.iterations == 3
        assert result.stopped_reason == "max_iterations"
        assert llm.calls <= 4  # budget + one summary request

    async def test_budget_default_is_bounded(self):
        assert 1 <= DEFAULT_MAX_ITERATIONS <= 20

    async def test_zero_budget_is_clamped_to_one(self, registry):
        llm = ScriptedLLM([call("get_time"), "done"])
        result = await AgentEngine(llm, registry, max_iterations=0).run("x")
        assert result.iterations >= 1

    async def test_exhausted_budget_still_produces_an_answer(self, registry):
        llm = ScriptedLLM([call("get_time")] * 10 + ["Here is where I got to."])
        result = await AgentEngine(llm, registry, max_iterations=2).run("loop")
        assert result.text
        assert result.stopped_reason == "max_iterations"

    async def test_a_hanging_tool_does_not_wedge_the_loop(self, registry, monkeypatch):
        import agents.engine as engine_mod

        monkeypatch.setattr(engine_mod, "TOOL_TIMEOUT_S", 0.05)
        reg = ToolRegistry()
        reg.register(_Hanging())

        llm = ScriptedLLM([call("hang"), "That took too long."])
        result = await AgentEngine(llm, reg).run("hang please")

        assert result.text == "That took too long."
        assert result.errors


class TestLoopRecovery:
    async def test_a_failed_tool_is_reported_and_the_model_can_retry(self, registry):
        llm = ScriptedLLM([
            call("broken"),
            call("get_time"),
            "Recovered.",
        ])
        result = await AgentEngine(llm, registry).run("try something")

        # Both are recorded: the full activity trail, and what failed.
        assert result.tool_calls == ["broken", "get_time"]
        assert result.failed_calls == ["broken"]
        assert result.errors
        # The failure was visible to the model on the retry turn.
        assert "something went wrong" in json.dumps(llm.requests[1])

    async def test_an_unknown_tool_is_reported_not_crashed(self, registry):
        llm = ScriptedLLM([call("no_such_tool"), "I could not do that."])
        result = await AgentEngine(llm, registry).run("do something impossible")
        assert result.text == "I could not do that."
        assert "Unknown tool" in json.dumps(llm.requests[1])

    async def test_a_provider_failure_is_reported_not_swallowed(self, registry):
        class _Dead:
            tier = "fast"

            async def complete(self, messages, **kwargs):
                raise RuntimeError("OpenRouter is unreachable")

        _bus, seen = collector()
        result = await AgentEngine(_Dead(), registry).run("hello", bus=_bus)

        assert result.stopped_reason == "llm_unavailable"
        assert result.errors
        assert any(e.type is AgentEventType.ERROR for e in seen)

    async def test_a_provider_failure_mid_loop_still_reports(self, registry):
        llm = ScriptedLLM([call("get_time"), None])
        result = await AgentEngine(llm, registry).run("go")
        assert result.stopped_reason == "llm_unavailable"


class TestLoopApproval:
    async def test_approval_is_requested_before_a_confirm_tier_tool(self, registry):
        asked = []

        async def approve(name, params):
            asked.append(name)
            return True

        llm = ScriptedLLM([
            '{"action": "file_write", "path": "a.txt", "content": "x"}',
            "Written.",
        ])
        await AgentEngine(llm, registry).run("write a file", approval=approve)
        assert asked == ["file_write"]

    async def test_a_safe_tool_does_not_ask(self, registry):
        asked = []

        async def approve(name, params):
            asked.append(name)
            return True

        llm = ScriptedLLM([call("get_time"), "Done."])
        await AgentEngine(llm, registry).run("what time", approval=approve)
        assert asked == []

    async def test_a_declined_action_is_fed_back_to_the_model(self, registry):
        async def decline(name, params):
            return "The user declined this action."

        llm = ScriptedLLM([
            '{"action": "file_write", "path": "a.txt", "content": "x"}',
            "Understood, I did not write it.",
        ])
        result = await AgentEngine(llm, registry).run("write", approval=decline)

        assert "declined" in json.dumps(llm.requests[1]).lower()
        assert "did not write" in result.text

    async def test_an_approval_channel_error_denies(self, registry):
        async def broken(name, params):
            raise RuntimeError("channel exploded")

        llm = ScriptedLLM([
            '{"action": "file_write", "path": "a.txt", "content": "x"}',
            "I could not do that.",
        ])
        result = await AgentEngine(llm, registry).run("write", approval=broken)
        assert "could not" in result.text.lower()

    async def test_no_approval_callback_means_a_blocked_tool_is_refused(self, registry):
        """With no gate at all, a confirm-tier tool must not run."""
        reg = ToolRegistry()
        reg.register(_Recorder("file_write", tier="confirm"))

        llm = ScriptedLLM(['{"action": "file_write", "path": "a"}', "I cannot write files."])
        result = await AgentEngine(llm, reg).run("write")

        assert "cannot" in result.text.lower() or "not" in result.text.lower()


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
class TestLoopEvents:
    async def test_emits_the_expected_sequence(self, registry):
        llm = ScriptedLLM([call("get_time"), "It is 3pm."])
        bus, seen = collector()
        await AgentEngine(llm, registry).run("what time", bus=bus)

        kinds = [e.type for e in seen]
        assert kinds[0] is AgentEventType.THINKING
        assert AgentEventType.TOOL_STARTED in kinds
        assert AgentEventType.TOOL_FINISHED in kinds
        assert kinds[-1] is AgentEventType.COMPLETED

    async def test_a_multi_tool_opening_emits_a_plan(self, registry):
        reg = registry
        reg.register(_Recorder("other"))
        llm = ScriptedLLM([
            call("get_time") + "\n" + call("other"),
            "Done.",
        ])
        bus, seen = collector()
        await AgentEngine(llm, reg).run("do two things", bus=bus)

        plans = [e for e in seen if e.type is AgentEventType.PLANNING]
        assert plans
        assert plans[0].data["steps"]

    async def test_a_single_tool_emits_no_plan(self, registry):
        llm = ScriptedLLM([call("get_time"), "Done."])
        bus, seen = collector()
        await AgentEngine(llm, registry).run("what time", bus=bus)
        assert not [e for e in seen if e.type is AgentEventType.PLANNING]

    async def test_tool_events_carry_the_name_and_label(self, registry):
        llm = ScriptedLLM([call("get_time"), "Done."])
        bus, seen = collector()
        await AgentEngine(llm, registry).run("what time", bus=bus)

        started = next(e for e in seen if e.type is AgentEventType.TOOL_STARTED)
        assert started.data["name"] == "get_time"
        assert started.data["label"] == "Checking the time"

        finished = next(e for e in seen if e.type is AgentEventType.TOOL_FINISHED)
        assert finished.data["success"] is True

    async def test_only_prose_is_a_response_chunk(self, registry):
        """The guarantee that keeps private reasoning off the wire."""
        llm = ScriptedLLM([call("get_time"), "The time is 3pm."])
        bus, seen = collector()
        await AgentEngine(llm, registry).run("what time", bus=bus)

        chunks = [e for e in seen if e.type is AgentEventType.RESPONSE_CHUNK]
        assert len(chunks) == 1
        assert chunks[0].data["content"] == "The time is 3pm."

        # The tool-call JSON must not appear in any user-facing event.
        user_facing = "".join(
            e.data.get("content", "") for e in seen if e.is_user_facing
        )
        assert "action" not in user_facing
        assert "get_time" not in user_facing

    async def test_no_bus_means_no_events(self, registry):
        llm = ScriptedLLM(["Hello."])
        result = await AgentEngine(llm, registry).run("hi", bus=NullBus())
        assert result.text == "Hello."


class TestEventBus:
    async def test_delivers_to_every_subscriber(self):
        bus = EventBus()
        a, b = [], []
        bus.subscribe(lambda e: a.append(e))
        bus.subscribe(lambda e: b.append(e))
        await bus.thinking()
        assert len(a) == 1 and len(b) == 1

    async def test_unsubscribe(self):
        bus = EventBus()
        seen = []
        off = bus.subscribe(lambda e: seen.append(e))
        await bus.thinking()
        off()
        await bus.thinking()
        assert len(seen) == 1

    async def test_a_failing_subscriber_does_not_break_the_others(self):
        """A progress display must never be able to abort the turn."""
        bus = EventBus()
        seen = []

        def _boom(event):
            raise RuntimeError("subscriber exploded")

        bus.subscribe(_boom)
        bus.subscribe(lambda e: seen.append(e))
        await bus.thinking()
        assert len(seen) == 1

    async def test_async_subscribers_are_awaited(self):
        bus = EventBus()
        seen = []

        async def _slow(event):
            await asyncio.sleep(0)
            seen.append(event)

        bus.subscribe(_slow)
        await bus.thinking()
        assert len(seen) == 1

    async def test_status_events_exclude_prose(self):
        from agents.events import STATUS_EVENTS

        assert AgentEventType.RESPONSE_CHUNK not in STATUS_EVENTS
        assert AgentEventType.THINKING in STATUS_EVENTS
        assert AgentEventType.TOOL_STARTED in STATUS_EVENTS
