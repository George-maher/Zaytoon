"""Turn bounds: how many calls, no repeats, and no unqualified "Done!".

`max_iterations` bounded *replies*. Nothing bounded what one reply could contain,
or what a second identical reply would do:

* one completion could dispatch an unlimited number of actions — and when those
  reach the approval gate, the user is asked to approve a hundred of them;
* a model that re-emits an already-successful call would run it again, so "open
  spotify" answered by a second observation of the first launch became two
  launches. This is the same class of bug the reminder reschedule was (§8 of
  `docs/PHASE0_BASELINE.md`): a read that mutates, an action with two owners;
* a reply of "Done ✅" after three FAILED observations used to be delivered as the
  answer, because nothing compared the claim to the record.

The refusals below are all *reported to the model* rather than dropped. Silence is
what makes a model repeat itself.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents import engine as engine_mod  # noqa: E402
from agents.engine import (  # noqa: E402
    MAX_CALLS_PER_ITERATION,
    MAX_CALLS_PER_TURN,
    AgentEngine,
)
from tools.base import BaseTool, ToolResult  # noqa: E402
from tools.registry import ToolRegistry  # noqa: E402
from tools.safety import PERMISSION_SAFE  # noqa: E402


@pytest.fixture(autouse=True)
def _model_path_only(monkeypatch):
    """Disable the P4 fast path for the whole module.

    Not a workaround for a bug: `"open spotify"` is exactly what
    `agents/fast_path.py` answers without reaching the loop, so a bounds test that
    posted it would be asserting that the shortcut respects limits it never sees.
    `tests/test_agent_engine.py` disables it for the same reason. The shortcut's own
    suite covers its side of the contract.
    """
    from config import config

    monkeypatch.setattr(config, "FAST_PATH_ENABLED", False)


class _Counter(BaseTool):
    """A tool that records every execution, so "refused" is provable."""

    permission = PERMISSION_SAFE

    def __init__(self, name: str, *, succeed: bool = True):
        super().__init__()
        self._name = name
        self.succeed = succeed
        self.seen: list[dict] = []

    @property
    def calls(self) -> int:
        return len(self.seen)

    def name(self) -> str:
        return self._name

    def description(self) -> str:
        return "Counts executions."

    def schema(self) -> dict:
        return {"type": "object", "properties": {}, "additionalProperties": True}

    async def execute(self, **params) -> ToolResult:
        self.seen.append(dict(params))
        if not self.succeed:
            return ToolResult(success=False, output="", error="device busy")
        return ToolResult(success=True, output=f"ran {self._name}")


def _build(replies, *tools):
    """An engine whose model says exactly `replies`, one per iteration."""
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    eng = AgentEngine(llm=None, registry=registry, metrics=None)
    log = {"messages": []}
    queue = list(replies)

    async def _ask(messages, *args, **kwargs):
        log["messages"].append([dict(m) for m in messages])
        if not queue:
            return "All done."
        return queue.pop(0)

    eng._ask = _ask  # noqa: SLF001
    return eng, log


def _calls(name: str, count: int, offset: int = 0) -> str:
    """A single reply containing `count` tool calls, each with distinct args.

    `offset` keeps calls distinct *across* replies; without it a multi-reply test
    would be testing the duplicate guard instead of the budget it means to test.
    """
    return "\n".join(
        json.dumps({"action": name, "params": {"i": index}})
        for index in range(offset, offset + count)
    )


class TestCallsPerReply:
    async def test_a_reply_may_not_dispatch_more_than_the_limit(self):
        tool = _Counter("open_app")
        eng, log = _build([_calls("open_app", MAX_CALLS_PER_ITERATION + 4)],
                          tool)
        await eng.run("do a lot of things")

        assert tool.calls == MAX_CALLS_PER_ITERATION, (
            f"{tool.calls} launched, limit is {MAX_CALLS_PER_ITERATION}: the bound "
            "is decorative if the excess still executes"
        )
        feedback = log["messages"][1][-1]["content"]
        assert "at most 8 calls" in feedback
        assert "Send the rest in your next step" in feedback

    async def test_refused_calls_have_no_side_effect(self):
        """The point of bounding, asserted directly: nothing extra ran."""
        tool = _Counter("open_app")
        eng, _ = _build([_calls("open_app", 40)], tool)
        await eng.run("launch everything")
        assert tool.calls == MAX_CALLS_PER_ITERATION
        assert len(tool.seen) == MAX_CALLS_PER_ITERATION

    async def test_the_limit_is_a_number_and_not_a_vibe(self):
        """Exactly at the limit, everything runs: no off-by-one attrition."""
        tool = _Counter("open_app")
        eng, _ = _build([_calls("open_app", MAX_CALLS_PER_ITERATION)], tool)
        await eng.run("open the apps")
        assert tool.calls == MAX_CALLS_PER_ITERATION

    def test_a_multi_call_reply_really_produces_multiple_calls(self):
        """Guard for the harness itself.

        Every test above would pass vacuously if the parser only ever returned one
        call from a reply like `_calls(...)`: the assertion would then be "1 == 1".
        """
        from agents.engine import extract_tool_calls

        parsed, _ = extract_tool_calls(_calls("open_app", 3))
        assert len(parsed) == 3


class TestTurnBudget:
    async def test_a_turn_cannot_dispatch_forever(self):
        tool = _Counter("open_app")
        # Six calls per reply is under the per-reply cap, so only the per-turn
        # budget can stop it — that separation is what makes this a real test.
        replies = [_calls("open_app", 6, offset=k * 6) for k in range(6)]
        eng, _ = _build(replies, tool)
        await eng.run("keep going")
        assert tool.calls == MAX_CALLS_PER_TURN, tool.calls

    async def test_the_model_is_told_the_budget_is_spent(self):
        tool = _Counter("open_app")
        replies = [_calls("open_app", 6, offset=k * 6) for k in range(6)]
        eng, log = _build(replies, tool)
        await eng.run("keep going")
        joined = "\n".join(m["content"] for msgs in log["messages"] for m in msgs)
        assert "budget" in joined


class TestDuplicateSideEffects:
    async def test_an_identical_action_is_not_run_twice(self):
        once = json.dumps({"action": "open_app", "params": {"app": "spotify"}})
        tool = _Counter("open_app")
        eng, log = _build([once, once], tool)
        await eng.run("open spotify")

        assert tool.calls == 1, "a duplicate launch happened"
        # The refusal is written into whatever the model reads next, so it is
        # looked for in the whole transcript rather than a guessed index.
        joined = "\n".join(m["content"] for msgs in log["messages"] for m in msgs)
        assert "already ran" in joined
        assert "spotify" in joined

    async def test_argument_order_does_not_hide_a_duplicate(self):
        first = json.dumps({"action": "write", "params": {"a": 1, "b": 2}})
        again = json.dumps({"action": "write", "params": {"b": 2, "a": 1}})
        tool = _Counter("write")
        eng, _ = _build([first, again], tool)
        await eng.run("write it")
        assert tool.calls == 1

    async def test_a_duplicate_inside_one_reply_collapses(self):
        twice = "\n".join(
            json.dumps({"action": "open_app", "params": {"app": "spotify"}})
            for _ in range(2)
        )
        tool = _Counter("open_app")
        eng, _ = _build([twice], tool)
        await eng.run("open spotify")
        assert tool.calls == 1

    async def test_different_arguments_are_not_a_duplicate(self):
        two = "\n".join(
            json.dumps({"action": "open_app", "params": {"app": app}})
            for app in ("spotify", "code")
        )
        tool = _Counter("open_app")
        eng, _ = _build([two], tool)
        await eng.run("open spotify and code")
        assert tool.calls == 2
        assert [p["app"] for p in tool.seen] == ["spotify", "code"]

    async def test_read_only_tools_may_be_polled(self):
        """`process_logs` twice is a legitimate loop, not a repeat side effect."""
        poll = json.dumps({"action": "get_time", "params": {}})
        tool = _Counter("get_time")
        eng, _ = _build([poll, poll], tool)
        await eng.run("what time is it, twice")
        assert tool.calls == 2, "a read-only poll was blocked"

    async def test_a_failed_action_can_be_retried(self):
        """Only *successes* are memorised, or one error becomes permanent."""
        once = json.dumps({"action": "open_app", "params": {"app": "spotify"}})
        tool = _Counter("open_app", succeed=False)
        eng, _ = _build([once, once], tool)
        await eng.run("open spotify")
        assert tool.calls == 2

    def test_the_repeat_safe_set_is_an_explicit_allow_list(self):
        """Deny-by-default is the whole design; this keeps it that way.

        An unknown tool must be treated as effectful. If someone inverts this to a
        deny-list of "dangerous" names, every future tool silently inherits the
        assumption that repeating it is harmless.
        """
        assert "open_app" not in engine_mod.REPEAT_SAFE_TOOLS
        assert "process_logs" in engine_mod.REPEAT_SAFE_TOOLS
        # Pinned as a decision, not an accident: re-reading a page is allowed
        # because a stale truncated copy is a worse answer than a second request.
        assert "fetch_url" in engine_mod.REPEAT_SAFE_TOOLS
        assert engine_mod.REPEAT_SAFE_TOOLS.isdisjoint({"open_app", "screenshot"})


class TestClaimVerification:
    async def test_a_success_claim_over_failed_steps_is_qualified(self):
        call = json.dumps({"action": "open_app", "params": {"app": "x"}})
        tool = _Counter("open_app", succeed=False)
        eng, _ = _build([call, "Done ✅ everything worked."], tool)
        result = await eng.run("open two things")
        assert "did not complete" in result.text
        assert "open_app" in result.text
        assert any("success claim" in e for e in result.errors)

    async def test_the_qualifier_is_in_the_users_language(self):
        call = json.dumps({"action": "open_app", "params": {"app": "x"}})
        tool = _Counter("open_app", succeed=False)
        eng, _ = _build([call, "تم كل شيء"], tool)
        result = await eng.run("افتح الحاجات", language="ar")
        assert "ملاحظة" in result.text
        assert "خطوة" in result.text

    async def test_an_honest_answer_is_left_completely_alone(self):
        """No false positives: an answer that does not claim success is untouched."""
        call = json.dumps({"action": "open_app", "params": {"app": "x"}})
        tool = _Counter("open_app", succeed=False)
        eng, _ = _build([call, "I could not open it — the device was busy."], tool)
        result = await eng.run("open x")
        assert result.text == "I could not open it — the device was busy."
        # The raw failure is still reported; what must NOT appear is a
        # verification note, because nothing was overclaimed.
        assert not any("success claim" in e for e in result.errors)

    async def test_a_clean_turn_gets_no_caveat(self):
        call = json.dumps({"action": "open_app", "params": {"app": "x"}})
        tool = _Counter("open_app")
        eng, _ = _build([call, "Done — opened."], tool)
        result = await eng.run("open x")
        assert "did not complete" not in result.text
        assert tool.calls == 1

    def test_the_claim_pattern_covers_both_languages(self):
        for text in ("Done.", "Saved successfully", "I finished that", "تم"):
            assert engine_mod._SUCCESS_CLAIM.search(text), text  # noqa: SLF001
        for text in ("I could not do it", "nothing happened", "it failed"):
            assert not engine_mod._SUCCESS_CLAIM.search(text), text  # noqa: SLF001
