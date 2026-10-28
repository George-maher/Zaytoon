"""A tool's *declared* permission tier must reach the confirmation prompt.

The defect these tests exist for, and it is the most serious one found in this
audit:

`ToolRegistry.assess` reconciles two sources — the tool's own `permission`
attribute and `RiskAssessor`'s name-keyed tables — and takes the stricter. That
part was right, and its docstring records fixing exactly this class of bug for
`play_media`.

But the registry then handed the decision to `ConfirmationManager.request`,
which **re-derived the tier from scratch** with a bare ``RiskAssessor``. Those
tables are keyed on action *name*: `play_media` is in none of them, so the
manager assessed it `safe`, saw `requires_confirmation == False`, and returned
`True` — *without ever showing a prompt*.

So for every tool whose tier comes from its declaration rather than from a table:

* `play_media` — confirmed silent, despite the registry's comment about it
* `security_probe` — confirmed silent
* **every tool a skill registers** — skilled third-party code, which is the case
  that matters most

The gate existed, the registry's decision was correct, and the prompt never
reached the user. That is a bypass of the confirmation layer by construction,
not by an attacker's cleverness.

The fix is that the manager no longer guesses: `request_with_assessment` takes
the already-reconciled assessment. `request` is unchanged, because the channel
is duck-typed and widening its signature broke nineteen existing doubles.

The same bug had a **second seat, one layer up**, and the tests below pin it:
on the agent path the engine correctly asks the *registry* whether a call
reaches the gate (`AgentEngine._needs_approval`), then consulted its approval
callback — but `ToolAgent._make_approval` answered that callback via
`ConfirmationManager.request`, the very re-derivation fixed above. The engine
was told "prompt required", asked the callback, and the callback silently said
yes. `ToolAgent` now passes the registry's reconciled assessment through
`_ask_with_reconciled_tier`, and the engine refuses — rather than executes — a
gated call when no approval callback exists at all.
"""

from __future__ import annotations

import asyncio

import pytest

from agents.engine import AgentEngine, ToolCall
from agents.events import NullBus
from agents.tool_agent import ToolAgent
from tools.base import BaseTool, ToolResult
from tools.registry import ToolRegistry
from tools.safety import ConfirmationManager, RiskAssessor


class _Channel(BaseTool):
    """A tool shaped like one a skill would register: no table entry."""

    permission = "confirm"

    def __init__(self):
        #: Set by `execute`, so a test can assert the tool *did not run*
        #: rather than inferring it from the outcome's shape.
        self.ran = False

    def name(self) -> str:
        return "skill_style_tool"

    def description(self) -> str:
        return "Not named in any RiskAssessor table."

    def schema(self) -> dict:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    async def execute(self, **_kwargs) -> ToolResult:
        self.ran = True
        return ToolResult.ok("ran", {})


class _Recorder:
    """A confirmation channel that records the prompts it was shown."""

    def __init__(self, approve: bool = True):
        self.prompts: list = []
        self.approve = approve

    async def request(self, action, params=None, session_id=""):
        self.prompts.append(action)
        return self.approve

    def prompt_text(self) -> str:
        """The strings the user would have seen."""
        return " | ".join(self.prompts)


@pytest.fixture
def registry():
    reg = ToolRegistry()
    reg.register(_Channel())
    return reg


class TestTheManagerNoLongerGuesses:
    async def test_it_asks_for_a_tool_it_would_have_called_safe(self):
        """The regression, at the manager's own seam."""
        manager = ConfirmationManager(notify=None)
        # Its bare assessor sees `skill_style_tool` as safe — that is the bug.
        assert not RiskAssessor().assess("skill_style_tool", {}).requires_confirmation

        shown = []

        async def notify(pending):
            shown.append(pending.action)

        manager.set_notifier(notify)
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, lambda: manager.resolve(manager.pending_ids()[0], True))

        from tools.registry import build_default_registry
        assessment = build_default_registry().assessor.assess("file_write", {})

        decision = await manager.request_with_assessment(
            assessment, "file_write", {"path": "x"}
        )
        assert shown == ["file_write"]
        assert decision is True

    async def test_a_plain_request_keeps_its_old_behaviour(self):
        """Callers with nothing better get the old self-assessment, not a hang."""
        manager = ConfirmationManager(notify=None)
        assert await manager.request("calculator", {"expression": "1+1"}) is True


class TestTheGateAsksForDeclaredTiers:
    async def test_a_declared_confirm_tool_produces_a_prompt(self, registry):
        """The headline regression.

        Before the fix this channel was never consulted: the manager re-derived
        `safe`, returned True, and the tool ran with the user never asked.
        """
        channel = _Recorder(approve=True)
        result = await registry.execute(
            "skill_style_tool", {}, confirmation_manager=channel
        )

        assert channel.prompt_text() == "skill_style_tool", (
            "a tool that declares `confirm` ran without asking the user"
        )
        assert result["success"] is True

    async def test_the_prompt_carries_the_reconciled_reason(self, registry):
        """The dialog must say *why*, and the registry's reason is the real one.

        Both halves are asserted. Asserting only on the prompt is not enough:
        `ConfirmationManager` *swallows* a notifier that raises and denies the
        request, so a notifier with a bug in it still produces a populated
        prompt list while the tool is silently refused. This test used to do
        exactly that — it called an undefined `loop`, so the approval never
        happened and the assertion still passed. The `result` assertion is what
        makes the prompt-list assertion mean something.
        """
        seen: list = []

        async def notify(pending):
            seen.append(pending.assessment.reasons)
            asyncio.get_running_loop().call_soon(
                manager.resolve, pending.request_id, True
            )

        manager = ConfirmationManager(notify=notify, timeout=5.0)
        result = await registry.execute(
            "skill_style_tool", {}, confirmation_manager=manager
        )

        assert seen, "no prompt was shown"
        assert any("declared confirm-level" in reason for reason in seen[0]), seen[0]
        # The approval actually took effect — the notifier did not raise.
        assert result["success"] is True, result

    async def test_a_denial_still_stops_the_tool(self, registry):
        channel = _Recorder(approve=False)
        result = await registry.execute(
            "skill_style_tool", {}, confirmation_manager=channel
        )
        assert result["success"] is False
        assert result.get("cancelled") is True
        assert channel.prompt_text() == "skill_style_tool"

    async def test_no_channel_is_reported_as_needing_confirmation(self, registry):
        """Without a channel the answer is "ask me", never "go ahead"."""
        result = await registry.execute("skill_style_tool", {}, confirmation_manager=None)
        assert result["success"] is False
        assert result.get("requires_confirmation") is True

    async def test_a_duck_typed_channel_is_still_used(self, registry):
        """Test doubles implement only `request`; they must keep working.

        Widening `ConfirmationManager.request`'s signature broke nineteen of
        these, so the assessment-aware path is a separate method and this is the
        test that keeps it that way.
        """
        channel = _Recorder()
        await registry.execute("skill_style_tool", {}, confirmation_manager=channel)
        assert channel.prompt_text() == "skill_style_tool"


class TestTheFloorStillApplies:
    async def test_a_skill_floor_cannot_be_lowered_by_the_declaration(self):
        """`dangerous` stays `dangerous` however the tool re-declares itself."""
        registry = ToolRegistry()
        registry.register(_Channel(), floor="dangerous")
        assert registry.effective_tier("skill_style_tool") == "dangerous"

        channel = _Recorder()
        await registry.execute("skill_style_tool", {}, confirmation_manager=channel)
        assert channel.prompt_text() == "skill_style_tool"


@pytest.fixture
def registry_and_probe():
    """A registry holding a declared-confirm tool that records its execution."""
    reg = ToolRegistry()
    tool = _Channel()
    reg.register(tool)
    return reg, tool


def _auto_manager(Decision: bool):
    """A real ConfirmationManager whose notifier answers immediately."""
    manager = ConfirmationManager(notify=None, timeout=5.0)

    async def notify(pending):
        asyncio.get_running_loop().call_soon(
            manager.resolve, pending.request_id, Decision
        )

    manager.set_notifier(notify)
    return manager


class TestTheAgentPathAsksToo:
    """The engine seam — where production traffic actually goes.

    The engine already asked the registry (`_needs_approval`), so it knew the
    call reached the gate; the hole was the *callback's* answer, which came
    from the name-keyed re-derivation and returned True without a prompt.
    These tests drive `AgentEngine._invoke` with the real
    `ToolAgent._make_approval` callback and a real `ConfirmationManager`.
    """

    @staticmethod
    def _pair(registry):
        agent = ToolAgent(risk_assessor=registry.assessor, registry=registry)
        engine = AgentEngine(llm=None, registry=registry)
        return engine, agent

    async def test_a_declared_confirm_tool_prompts_on_the_agent_path(
        self, registry_and_probe
    ):
        registry, tool = registry_and_probe
        engine, agent = self._pair(registry)
        manager = _auto_manager(True)
        shown: list = []

        original = manager._notify

        async def notify(pending):
            shown.append(pending.action)
            await original(pending)

        manager._notify = notify

        outcome = await engine._invoke(
            ToolCall(name="skill_style_tool", params={}),
            agent._make_approval(manager, "sess_a1", "en"),
            NullBus(),
            "en",
        )

        assert shown == ["skill_style_tool"], (
            "the agent path answered the approval callback without prompting"
        )
        assert isinstance(outcome, dict) and outcome.get("success") is True, outcome
        assert tool.ran is True

    async def test_a_decline_on_the_agent_path_stops_the_tool(
        self, registry_and_probe
    ):
        registry, tool = registry_and_probe
        engine, agent = self._pair(registry)
        manager = _auto_manager(False)

        outcome = await engine._invoke(
            ToolCall(name="skill_style_tool", params={}),
            agent._make_approval(manager, "sess_a1", "en"),
            NullBus(),
            "en",
        )

        assert isinstance(outcome, str), outcome
        assert "denied" in outcome.lower() or "اتلغت" in outcome, outcome
        assert tool.ran is False, "a declined tool ran anyway"

    async def test_no_approval_callback_refuses_instead_of_running(
        self, registry_and_probe
    ):
        """`run(approval=None)`'s default used to execute gated calls bare.

        The engine now hands the registry `approval=None`, so the registry
        refuses for want of a channel — the same answer a caller with no
        manager gets.
        """
        registry, tool = registry_and_probe
        engine, _agent = self._pair(registry)

        outcome = await engine._invoke(
            ToolCall(name="skill_style_tool", params={}),
            approval=None,
            bus=NullBus(),
            language="en",
        )

        assert isinstance(outcome, dict)
        assert outcome.get("success") is False
        assert outcome.get("requires_confirmation") is True, outcome
        assert tool.ran is False, "a gated tool ran with no approval channel"

    async def test_a_safe_tool_still_needs_no_prompt_on_the_agent_path(
        self, registry_and_probe
    ):
        """The prompt is for gated calls only — `get_time` must not ask."""
        registry, _tool = registry_and_probe
        from tools.system import GetTime

        registry.register(GetTime())
        engine, agent = self._pair(registry)
        manager = _auto_manager(False)  # would refuse anything it is asked

        outcome = await engine._invoke(
            ToolCall(name="get_time", params={}),
            agent._make_approval(manager, "sess_a1", "en"),
            NullBus(),
            "en",
        )

        assert isinstance(outcome, dict) and outcome.get("success") is True, outcome

    async def test_a_duck_typed_channel_still_works_on_the_agent_path(
        self, registry_and_probe
    ):
        """Doubles implement only `request`; the closure must fall back."""
        registry, _tool = registry_and_probe
        engine, agent = self._pair(registry)
        channel = _Recorder(approve=True)

        outcome = await engine._invoke(
            ToolCall(name="skill_style_tool", params={}),
            agent._make_approval(channel, "sess_a1", "en"),
            NullBus(),
            "en",
        )

        assert channel.prompt_text() == "skill_style_tool"
        assert isinstance(outcome, dict) and outcome.get("success") is True, outcome
