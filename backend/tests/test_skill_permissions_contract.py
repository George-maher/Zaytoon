"""A skill's declared permissions survive creation and are validated.

Two halves of one contract:

* **They are not dropped.** The create route accepted `permissions` from the
  client and forwarded nothing, so a skill created asking for
  `filesystem.read` came back with `permissions: []`. The Skills page renders
  exactly that field as "asks for", which meant the stated requirement — show
  a skill's requested permissions before it can be enabled — was unmet: the
  field was permanently empty, so every skill looked like it asked for nothing.

* **An unknown name is refused.** `manifest.validate` rejects a permission
  outside `core.permissions.ALL_PERMISSIONS`. That rejection is the valuable
  behaviour, and it only happens if the name actually reaches the validator
  rather than being dropped on the way in.

Declaring is never granting, and a third test pins that: a skill created asking
for `process.execute` must not thereby acquire it.
"""

from __future__ import annotations

import json

import pytest

from core.skills import registry
from core.skills.manifest import ManifestError


@pytest.fixture
def root(tmp_path, monkeypatch):
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    monkeypatch.setattr(registry, "skills_root", lambda: str(skills_root))
    return skills_root


class TestPermissionsSurviveCreation:
    def test_declared_permissions_are_written_to_the_manifest(self, root):
        created = registry.create(
            "laravel-security",
            description="Laravel review",
            permissions=["filesystem.read", "network.request"],
        )
        assert created["permissions"] == ["filesystem.read", "network.request"], (
            "the declared permissions were dropped, so the UI can never show "
            "what a skill asks for before it is enabled"
        )

        on_disk = json.loads(
            (root / "laravel-security" / "skill.json").read_text(encoding="utf-8")
        )
        assert on_disk["permissions"] == ["filesystem.read", "network.request"]

    def test_they_come_back_from_a_reload(self, root):
        registry.create("reloaded", permissions=["git.write"])
        assert registry.skill_detail("reloaded")["permissions"] == ["git.write"]

    def test_an_unknown_permission_is_refused(self, root):
        with pytest.raises(ManifestError) as excinfo:
            registry.create("escalating", permissions=["filesystem.read", "root.everything"])
        assert "root.everything" in str(excinfo.value)
        assert not (root / "escalating").exists(), (
            "a rejected manifest left a half-written skill directory behind"
        )

    def test_omitting_permissions_still_creates_a_skill(self, root):
        """Backwards compatibility: the field is optional."""
        created = registry.create("plain")
        assert created["permissions"] == []

    def test_a_blank_entry_is_ignored_not_written(self, root):
        created = registry.create("blanks", permissions=["filesystem.read", "  ", ""])
        assert created["permissions"] == ["filesystem.read"]

    def test_a_non_list_is_refused(self, root):
        with pytest.raises(ManifestError):
            registry.create("wrong-shape", permissions="filesystem.read")

    def test_duplicates_are_collapsed(self, root):
        created = registry.create(
            "dupes", permissions=["filesystem.read", "filesystem.read"]
        )
        assert created["permissions"] == ["filesystem.read"]


class TestDeclaringIsNotGranting:
    def test_a_declared_permission_does_not_change_the_tool_tier_floor(self, root):
        """The manifest is a claim; the registry's floor is the fact.

        `core.permissions.required_for` derives a tool's permissions from its
        tier, and the skill loader raises the tier to `confirm`. A manifest that
        asks for something broader must not move either — so the check is that
        creating the skill changes nothing about the gate.
        """
        from core.permissions import required_for
        from tools.registry import build_default_registry

        tool_registry = build_default_registry()
        def snapshot():
            return {
                name: (
                    tool_registry.effective_tier(name),
                    required_for(
                        name,
                        tool_registry.get(name).permissions(),
                    ),
                )
                for name in tool_registry.list()
            }

        before = snapshot()

        registry.create("broad-claims", permissions=["system.modify", "process.execute"])

        assert before == snapshot(), (
            "creating a skill that declares broad permissions changed the tool "
            "gate"
        )

    def test_the_declared_permissions_are_reported_separately_from_the_tiers(self, root):
        """The skill's claim and the tool's tier are two different answers.

        A UI that shows only one of them either hides what a skill wants or
        implies it already has it.
        """
        from core.permissions import required_for
        from tools.registry import build_default_registry

        created = registry.create("claimant", permissions=["system.modify", "filesystem.read"])
        tool_registry = build_default_registry()

        assert set(created["permissions"]) == {"system.modify", "filesystem.read"}

        # And the same tool that would carry `system.write` if the claim were a
        # grant is still at its own tier.
        calculator = tool_registry.get("calculator")
        assert "system.modify" not in required_for(
            "calculator", calculator.permissions()
        )
