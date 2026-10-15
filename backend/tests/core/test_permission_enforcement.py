"""Permission grants are enforced, not merely stored.

`core/permissions.py` has always had the vocabulary and the three decisions;
`core/api/registry_api.py` has always had routes that persist them. Nothing on
the execution path read them, so `deny` was a word in a database.

These tests are about the *gate* — the single place a tool call passes through —
and they are deliberately written against the real `ToolRegistry`, the real
SQLite table and (in the last class) the real HTTP route, because the defect
being fixed was precisely a gap between those three pieces: each worked, and the
connection between them did not exist.

Two properties deserve naming, because they are the ones a future edit is most
likely to break:

* **`deny` is checked before the approval prompt.** A prompt for something the
  user has already forbidden asks them to overrule themselves.
* **`allow` does not skip the prompt.** Nothing in this system converts a stored
  preference into "run without asking" — see `TestAllowIsNotPreApproval`.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core import grants
from core.permissions import Permission
from tools.base import BaseTool, ToolResult
from tools.registry import ToolRegistry


class ProbeTool(BaseTool):
    """A registered tool that records whether it was ever reached.

    Not a mock: `ToolRegistry.execute` refuses at the gate, so a call count of
    zero is direct evidence the gate held, and a count of one is evidence the
    gate let it through. A stub that raised would be ambiguous — a raise from
    the tool looks the same as a raise from the gate.
    """

    def __init__(self, name: str, permission: str = "safe", extra=("filesystem.read",)):
        self._name = name
        self.permission = permission
        self._extra = extra
        self.calls = 0

    def name(self) -> str:
        return self._name

    def description(self) -> str:
        return "probe"

    def permissions(self):
        return list(self._extra)

    async def execute(self, **kwargs) -> ToolResult:
        self.calls += 1
        return ToolResult(success=True, output="ran", data={"ran": True})


async def _deny(permission) -> None:
    """Record a `deny` the way the API does, then let the gate see it."""
    from core import db

    await db.execute(
        "INSERT INTO permission_grants (permission, decision, updated_at) "
        "VALUES (?,?,?) ON CONFLICT(permission) DO UPDATE SET decision=excluded.decision",
        (permission.value if isinstance(permission, Permission) else str(permission),
         "deny", "2026-01-01T00:00:00+00:00"),
    )
    grants.invalidate()


async def _decision(permission, decision: str) -> None:
    from core import db

    name = permission.value if isinstance(permission, Permission) else str(permission)
    if decision == "ask":
        await db.execute("DELETE FROM permission_grants WHERE permission = ?", (name,))
    else:
        await db.execute(
            "INSERT INTO permission_grants (permission, decision, updated_at) "
            "VALUES (?,?,?) ON CONFLICT(permission) DO UPDATE SET decision=excluded.decision",
            (name, decision, "2026-01-01T00:00:00+00:00"),
        )
    grants.invalidate()


@pytest.fixture
def registry():
    return ToolRegistry()


class TestDenyRefuses:
    async def test_a_denied_permission_stops_the_tool_before_it_runs(self, registry, temp_core_db):
        tool = registry.register(ProbeTool("probe_read"))
        await _deny(Permission.FILESYSTEM_READ)

        result = await registry.execute("probe_read", {})

        assert result["success"] is False
        assert result["denied"] is True
        assert tool.calls == 0, "the gate let a denied tool through"

    async def test_the_refusal_names_the_permission_and_where_to_undo_it(
        self, registry, temp_core_db
    ):
        registry.register(ProbeTool("probe_read"))
        await _deny(Permission.FILESYSTEM_READ)

        result = await registry.execute("probe_read", {})

        assert "filesystem.read" in result["error"]
        assert "Settings" in result["error"]
        # Machine-readable too: a UI groups by permission, not by parsing prose.
        assert result["permissions"] == ["filesystem.read"]

    async def test_deny_is_checked_before_the_approval_prompt(self, registry, temp_core_db):
        """No `requires_confirmation` — the answer is no, not "shall I ask?"."""
        registry.register(ProbeTool("probe_exec", permission="confirm", extra=()))
        await _deny(Permission.PROCESS_EXECUTE)

        result = await registry.execute("probe_exec", {})

        assert result.get("requires_confirmation") is None
        assert result.get("cancelled") is None
        assert result["denied"] is True

    async def test_deny_survives_an_approval_someone_else_already_obtained(
        self, registry, temp_core_db
    ):
        """`approval=True` is a claim by the caller, not an override.

        `agents.tool_agent.ToolAgent` runs its own gate and passes the decision
        through. It must not be able to hand the registry a yes for something
        the user has forbidden — otherwise the strongest-sounding argument wins
        and the setting is decorative.
        """
        tool = registry.register(ProbeTool("probe_exec", permission="confirm", extra=()))
        await _deny(Permission.PROCESS_EXECUTE)

        result = await registry.execute("probe_exec", {}, approval=True)

        assert result["denied"] is True
        assert tool.calls == 0

    async def test_only_the_denied_permission_is_refused(self, registry, temp_core_db):
        registry.register(ProbeTool("probe_read", extra=("filesystem.read",)))
        registry.register(ProbeTool("probe_other", extra=("filesystem.write",)))
        await _deny(Permission.FILESYSTEM_READ)

        blocked = await registry.execute("probe_read", {})
        allowed = await registry.execute("probe_other", {})

        assert blocked["denied"] is True
        assert allowed["success"] is True


class TestAllowIsNotPreApproval:
    """`allow` is recorded and displayed; it never silences the prompt.

    This is the property that keeps the feature from removing an authorisation
    control. If a future change makes `allow` skip the gate, these fail.
    """

    async def test_allow_still_prompts_for_a_confirm_tier_action(
        self, registry, temp_core_db
    ):
        tool = registry.register(ProbeTool("probe_exec", permission="confirm", extra=()))
        await _decision(Permission.PROCESS_EXECUTE, "allow")

        result = await registry.execute("probe_exec", {})

        assert result["requires_confirmation"] is True
        assert tool.calls == 0
        assert result.get("denied") is None

    async def test_allow_still_prompts_for_a_dangerous_tier_action(
        self, registry, temp_core_db
    ):
        tool = registry.register(
            ProbeTool("probe_rm", permission="dangerous", extra=("filesystem.delete",))
        )
        await _decision(Permission.FILESYSTEM_DELETE, "allow")

        result = await registry.execute("probe_rm", {})

        assert result["requires_confirmation"] is True
        assert tool.calls == 0

    async def test_ask_is_the_default_so_absence_of_a_row_changes_nothing(
        self, registry, temp_core_db
    ):
        tool = registry.register(ProbeTool("probe_read"))
        assert await grants.decision_for(Permission.FILESYSTEM_READ) == "ask"

        result = await registry.execute("probe_read", {})

        assert result["success"] is True
        assert tool.calls == 1


class TestTurningItBackOff:
    async def test_removing_the_decision_restores_the_previous_behaviour(
        self, registry, temp_core_db
    ):
        tool = registry.register(ProbeTool("probe_read"))
        await _deny(Permission.FILESYSTEM_READ)
        assert (await registry.execute("probe_read", {}))["denied"] is True

        await _decision(Permission.FILESYSTEM_READ, "ask")

        assert (await registry.execute("probe_read", {}))["success"] is True
        assert tool.calls == 1

    async def test_a_decision_is_live_without_a_restart(self, registry, temp_core_db):
        """The gate's cache must not outlive the write that changed the policy.

        A settings screen whose switch takes effect "next time you restart" is
        the same defect as one that does nothing, just slower to notice.
        """
        tool = registry.register(ProbeTool("probe_read"))
        assert (await registry.execute("probe_read", {}))["success"] is True

        await _deny(Permission.FILESYSTEM_READ)

        assert (await registry.execute("probe_read", {}))["denied"] is True
        assert tool.calls == 1


class TestTheGateAndTheApiShareOneStore:
    """The claim this whole change rests on: one table, read by both sides.

    Written over HTTP rather than against the store directly, because the
    failure mode being guarded against is a *second* store appearing beside the
    first — the API writing to one table while the gate reads another, each
    internally consistent and together useless.
    """

    @pytest.fixture
    def client(self, temp_core_db, temp_secrets, temp_workspace):
        from api.main import app

        with TestClient(app) as test_client:
            yield test_client

    def test_a_decision_posted_to_the_api_is_enforced_by_the_gate(
        self, client, registry
    ):
        import asyncio

        tool = registry.register(ProbeTool("probe_read", extra=("filesystem.read",)))

        assert client.post(
            "/permissions/filesystem.read", json={"decision": "deny"}
        ).status_code == 200

        result = asyncio.run(registry.execute("probe_read", {}))

        assert result["denied"] is True
        assert tool.calls == 0

    def test_the_route_says_whether_the_decision_is_enforced(self, client):
        """The response distinguishes the decision that limits from the one that does not."""
        denied = client.post(
            "/permissions/filesystem.read", json={"decision": "deny"}
        ).json()
        allowed = client.post(
            "/permissions/filesystem.write", json={"decision": "allow"}
        ).json()

        assert denied["enforced"] is True
        assert "refused" in denied["note"]
        assert allowed["enforced"] is False
        assert "tier rules" in allowed["note"]

    def test_the_listing_shows_the_decision_the_gate_will_read(self, client):
        client.post("/permissions/filesystem.delete", json={"decision": "deny"})

        payload = client.get("/permissions").json()

        assert payload["grants"]["filesystem.delete"] == "deny"
        assert payload["default_decision"] == "ask"
        # Every permission the vocabulary names is one the gate can check: the
        # listing and the enforcement share `core.permissions.Permission`.
        listed = {row["permission"] for row in payload["vocabulary"]}
        assert listed == {p.value for p in Permission}
