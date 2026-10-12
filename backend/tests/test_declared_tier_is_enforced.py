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
"""

from __future__ import annotations

import asyncio

import pytest

from tools.base import BaseTool, ToolResult
from tools.registry import ToolRegistry
from tools.safety import ConfirmationManager, RiskAssessor


class _Channel(BaseTool):
    """A tool shaped like one a skill would register: no table entry."""

    permission = "confirm"

    def name(self) -> str:
        return "skill_style_tool"

    def description(self) -> str:
        return "Not named in any RiskAssessor table."

    def schema(self) -> dict:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    async def execute(self, **_kwargs) -> ToolResult:
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
