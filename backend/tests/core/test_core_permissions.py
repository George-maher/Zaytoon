"""The permission vocabulary, derived from the tool tiers."""

import pytest

from core.permissions import (
    ALL_PERMISSIONS,
    DECISION_ALLOW,
    DECISION_ASK,
    DECISION_DENY,
    Permission,
    VALID_DECISIONS,
    describe,
    is_valid_decision,
    required_for,
)
from tools.registry import build_default_registry
from tools.safety import PERMISSION_CONFIRM, PERMISSION_DANGEROUS, PERMISSION_SAFE


class TestVocabulary:
    def test_the_vocabulary_is_pinned(self):
        """Thirteen names, and the count is deliberate.

        It was ten until virtualization was added, which contributed three
        rather than one: `vm.discover`, `vm.manage` and `vm.guest.exec` are
        separate grants because listing VMs, powering one on, and running a
        command *inside* one have different blast radii. A single `vm.manage`
        would let consent for the first authorise the second. Updating this
        number is the reminder to ask whether the new grant is really distinct.
        """
        assert len(ALL_PERMISSIONS) == 13

    def test_the_vm_grants_do_not_imply_each_other(self):
        from core.permissions import Permission, required_for

        assert Permission.VM_DISCOVER in required_for("vm_list", "safe")
        assert Permission.VM_MANAGE not in required_for("vm_list", "safe")
        assert Permission.VM_GUEST_EXECUTE not in required_for("vm_start", "confirm")
        assert Permission.VM_MANAGE not in required_for("vm_guest_exec", "confirm")

    def test_every_permission_is_described(self):
        rows = describe()
        assert len(rows) == len(ALL_PERMISSIONS)
        assert all(row["label"] for row in rows)

    def test_values_are_stable_strings(self):
        """A wire contract: the UI stores these."""
        assert Permission.FILESYSTEM_READ.value == "filesystem.read"
        assert Permission.GIT_DESTRUCTIVE.value == "git.destructive"

    @pytest.mark.parametrize("value", ["allow", "deny", "ask"])
    def test_decisions_validate(self, value):
        assert is_valid_decision(value) is True

    @pytest.mark.parametrize("value", ["", "maybe", "ALLOW ME", None])
    def test_a_bad_decision_is_refused(self, value):
        assert is_valid_decision(value) is False

    def test_the_valid_decisions(self):
        assert VALID_DECISIONS == (DECISION_ALLOW, DECISION_DENY, DECISION_ASK)


class TestRequiredFor:
    def test_an_unknown_action_needs_nothing(self):
        assert required_for("something_new") == frozenset()

    def test_an_empty_action_needs_nothing(self):
        assert required_for("") == frozenset()

    @pytest.mark.parametrize("alias", [
        "file_read", "read_file", "file_list", "list_files", "list_directory",
    ])
    def test_every_read_spelling_gets_read(self, alias):
        """Aliases are listed because `permission_for` can be handed one."""
        assert Permission.FILESYSTEM_READ in required_for(alias)

    @pytest.mark.parametrize("action", [
        "file_write", "write_file", "file_edit", "create_file", "create_folder",
    ])
    def test_writes_get_read_and_write(self, action):
        perms = required_for(action)
        assert Permission.FILESYSTEM_WRITE in perms
        assert Permission.FILESYSTEM_DELETE not in perms

    def test_delete_gets_the_delete_permission(self):
        perms = required_for("file_delete")
        assert Permission.FILESYSTEM_DELETE in perms
        assert Permission.FILESYSTEM_WRITE in perms

    @pytest.mark.parametrize("action", [
        "system_command", "execute_local_command", "run_code", "execute_code",
        "process_start", "run_npm", "run_tests",
    ])
    def test_execution_actions_get_execute(self, action):
        assert Permission.PROCESS_EXECUTE in required_for(action)

    def test_process_management_is_separate_from_execution(self):
        """Starting something and stopping it are different permissions, so a
        grant for one is not silently a grant for the other."""
        assert Permission.PROCESS_MANAGE in required_for("process_stop")
        assert Permission.PROCESS_MANAGE not in required_for("process_start")

    def test_inspecting_a_process_is_still_manage(self):
        """Reading a process list is management of them, so it is named."""
        assert Permission.PROCESS_MANAGE in required_for("process_list")

    def test_network_actions(self):
        for action in ("search_web", "open_url"):
            assert Permission.NETWORK_REQUEST in required_for(action)

    def test_input_injection_is_browser_control(self):
        for action in ("type_text", "key_press"):
            assert Permission.BROWSER_CONTROL in required_for(action)

    def test_registry_writes_are_system_modification(self):
        assert Permission.SYSTEM_MODIFY in required_for("registry_write")

    @pytest.mark.parametrize("verb", ["status", "log", "diff", "branch_list"])
    def test_read_only_git_is_free(self, verb):
        assert required_for(f"git_{verb}") == frozenset()

    @pytest.mark.parametrize("verb", ["add", "commit", "checkout", "push", "pull"])
    def test_writing_git_is_git_write(self, verb):
        perms = required_for(f"git_{verb}")
        assert Permission.GIT_WRITE in perms
        assert Permission.GIT_DESTRUCTIVE not in perms

    @pytest.mark.parametrize("verb", [
        "reset_hard", "clean", "force", "branch_delete", "filter_branch",
    ])
    def test_lossy_git_is_destructive(self, verb):
        """`git reset --hard` is not "a bit more git" — it discards work, and a
        blanket "allow git.write" must not cover it."""
        perms = required_for(f"git_{verb}")
        assert Permission.GIT_DESTRUCTIVE in perms

    @pytest.mark.parametrize("action", ["get_time", "system_info", "calculator"])
    def test_inert_actions_need_nothing(self, action):
        assert required_for(action) == frozenset()

    def test_extra_permissions_are_added(self):
        perms = required_for("get_time", extra=["filesystem.read"])
        assert perms == frozenset({Permission.FILESYSTEM_READ})

    def test_an_unknown_extra_is_ignored(self):
        """Not fatal — a newer build's client may send a name this one lacks."""
        assert required_for("get_time", extra=["not_a_permission"]) == frozenset()


class TestDerivedFromTiers:
    """The vocabulary is derived, not declared, so it cannot drift."""

    def test_a_confirm_tier_action_implies_write_and_execute(self):
        perms = required_for("file_write")
        assert perms >= {
            Permission.FILESYSTEM_WRITE, Permission.PROCESS_EXECUTE,
        }

    def test_a_dangerous_tier_action_implies_delete(self):
        assert Permission.FILESYSTEM_DELETE in required_for("file_delete")

    def test_nothing_acquires_a_permission_beyond_its_tier(self):
        """Every real tool's requirement must be consistent with its tier: a
        `safe` tool cannot need write access."""
        for tool in build_default_registry().tools():
            perms = required_for(tool.name())
            if tool.permission == PERMISSION_SAFE:
                mutating = {
                    Permission.FILESYSTEM_WRITE,
                    Permission.FILESYSTEM_DELETE,
                    Permission.PROCESS_EXECUTE,
                    Permission.SYSTEM_MODIFY,
                    Permission.GIT_WRITE,
                    Permission.GIT_DESTRUCTIVE,
                }
                assert not (perms & mutating), (
                    f"{tool.name()} is {tool.permission} but needs {perms & mutating}"
                )

    def test_every_registry_tool_has_a_requirement_set(self):
        """Even a safe tool gets an entry — an unmapped tool is a tool whose
        capabilities nobody has reasoned about."""
        for tool in build_default_registry().tools():
            assert isinstance(required_for(tool.name()), frozenset)


class TestConsistency:
    def test_the_tier_order_is_still_what_the_registry_uses(self):
        """This module adds a vocabulary; it does not replace the tiers the
        confirmation dialog renders."""
        assert PERMISSION_SAFE == "safe"
        assert PERMISSION_CONFIRM == "confirm"
        assert PERMISSION_DANGEROUS == "dangerous"

    def test_requirements_are_serialisable(self):
        import json

        payload = {
            "tool": "file_write",
            "permissions": sorted(p.value for p in required_for("file_write")),
        }
        assert json.loads(json.dumps(payload))["tool"] == "file_write"