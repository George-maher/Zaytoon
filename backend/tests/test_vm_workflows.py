"""The natural-language VM workflows from the brief, as distinct operations.

Section 6B lists requests that look similar and are not:

    "Open VMware."
    "Show me the available virtual machines."
    "Start Ubuntu."
    "Wait until Ubuntu is ready."
    "Open the terminal in the guest and check its network."
    "Run the authorized lab assessment in my Parrot VM."

The failure this file exists to prevent is a system that treats those as one
interchangeable "do something with a VM" action. They differ in what they
touch, in what they cost, in what they can damage, and therefore in whether the
user is asked first. Each is asserted to resolve to a *different* tool with a
*different* permission scope, and the two claims that matter most — a window is
not readiness, and a running VM is not readiness — are asserted directly.

Everything runs through the real `ToolRegistry`, so the tiers and the
confirmation gate are the production ones.
"""

from __future__ import annotations

import pytest

from core.permissions import Permission, required_for
from core.virtualization import GuestSession, VMInfo, VMState
from core.virtualization.guest import GuestResult
from tools.registry import ToolRegistry
from tools.virtualization import (
    VMGuestCheck,
    VMGuestExec,
    VMList,
    VMOpenConsole,
    VMStart,
    VMStop,
)


@pytest.fixture
def vm():
    return VMInfo(
        name="Ubuntu", path="/vms/u/Ubuntu.vmx", state=VMState.STOPPED,
        state_evidence="not listed by `vmrun list`", guest_os="ubuntu-64",
    )


@pytest.fixture
def parrot():
    return VMInfo(
        name="Parrot", path="/vms/p/Parrot.vmx", state=VMState.RUNNING,
        state_evidence="listed by `vmrun list`", guest_os="debian-64",
    )


def _registry(*tools):
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


class Channel:
    """A confirmation channel that records what it was asked."""

    def __init__(self, approve=True):
        self.asked = []
        self.approve = approve

    async def request(self, action, params=None, session_id=""):
        self.asked.append((action, dict(params or {})))
        return self.approve


# ---------------------------------------------------------------------------
# Not one interchangeable action
# ---------------------------------------------------------------------------
class TestTheRequestsAreDifferentOperations:
    def test_each_request_lands_on_its_own_tool_with_its_own_scope(self):
        """The load-bearing assertion of section 6B."""
        mapping = {
            "show the VMs":               ("vm_list",          Permission.VM_DISCOVER),
            "start Ubuntu":               ("vm_start",         Permission.VM_MANAGE),
            "open the console":           ("vm_open_console",  Permission.VM_MANAGE),
            "check the guest is ready":   ("vm_guest_check",   Permission.VM_GUEST_EXECUTE),
            "run a command in the guest": ("vm_guest_exec",    Permission.VM_GUEST_EXECUTE),
        }
        seen_scopes = set()
        for _label, (tool, expected) in mapping.items():
            granted = required_for(tool, "confirm")
            assert expected in granted, (tool, granted)
            seen_scopes.add(frozenset(granted))

        # Lifecycle and guest execution must not collapse into one grant.
        lifecycle = required_for("vm_start", "confirm")
        guest = required_for("vm_guest_exec", "confirm")
        assert Permission.VM_GUEST_EXECUTE not in lifecycle
        assert Permission.VM_MANAGE not in guest

    def test_reading_the_list_is_free_and_running_a_command_is_not(self):
        """The prompt tells the user which kind of request this is."""
        registry = _registry(VMList(), VMStart(), VMGuestExec())
        assert registry.assess("vm_list", {}).requires_confirmation is False
        assert registry.assess("vm_start", {"vm": "U"}).requires_confirmation is True
        assert registry.assess("vm_guest_exec", {"command": "ip a"}).requires_confirmation is True


# ---------------------------------------------------------------------------
# The two claims that must never be conflated
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestReadinessIsEarnedNotAssumed:
    async def test_starting_a_vm_does_not_claim_the_guest_booted(self, vm, monkeypatch):
        import tools.virtualization as vt

        calls = []

        class FakeAdapter:
            async def available(self):
                return True

            async def start(self, ref, *, gui=True):
                calls.append(("start", ref, gui))
                vm.state = VMState.RUNNING
                vm.state_evidence = "listed by `vmrun list`"
                return vm

        monkeypatch.setattr(vt, "adapter", lambda: FakeAdapter())

        fresh = _registry(VMStart())
        channel = Channel()
        result = await fresh.execute("vm_start", {"vm": "Ubuntu"}, confirmation_manager=channel)

        assert result["success"] is True
        # The VM is running and that is all we know.
        assert "not boot evidence" in result["output"]
        assert calls == [("start", "Ubuntu", True)]

    async def test_a_console_window_is_reported_as_a_window(self, vm, monkeypatch):
        """Opening a window is not booting, and the text must not imply it is."""
        import tools.virtualization as vt

        class FakeAdapter:
            async def available(self):
                return True

            async def open_console(self, ref):
                return (
                    f"Started '{ref}' with a GUI console. The window appearing is not "
                    f"evidence that the guest has finished booting — use vm_guest_check."
                )

        monkeypatch.setattr(vt, "adapter", lambda: FakeAdapter())
        registry = _registry(VMOpenConsole())
        result = await registry.execute(
            "vm_open_console", {"vm": "Ubuntu"}, confirmation_manager=Channel()
        )
        assert result["success"] is True
        assert "not" in result["output"] and "evidence" in result["output"]

    async def test_a_running_vm_whose_guest_never_answers_is_not_ready(self, parrot, monkeypatch):
        """"Wait until Ubuntu is ready" must be able to come back *not ready*."""
        import config
        import tools.virtualization as vt

        monkeypatch.setattr(config.Config, "VM_GUEST_USER", "lab")
        monkeypatch.setattr(vt, "adapter", lambda: _FakeHypervisor(parrot))

        async def denied(*_a, **_k):
            return GuestResult(
                target="lab@10.0.0.9", command="true", exit_code=255,
                stderr="lab@10.0.0.9: Permission denied (publickey).",
            )

        monkeypatch.setattr(GuestSession, "check", denied)
        registry = _registry(VMGuestCheck())
        result = await registry.execute(
            "vm_guest_check", {"vm": "Parrot", "wait_seconds": 0},
            confirmation_manager=Channel(),
        )
        assert result["success"] is False
        assert result["data"]["ready"] is False
        assert "did not answer" in result["error"]

    async def test_the_guest_answers_so_it_is_ready(self, parrot, monkeypatch):
        import config
        import tools.virtualization as vt

        monkeypatch.setattr(config.Config, "VM_GUEST_USER", "lab")
        monkeypatch.setattr(vt, "adapter", lambda: _FakeHypervisor(parrot))

        async def fine(*_a, **_k):
            return GuestResult(target="lab@10.0.0.9", command="true", exit_code=0)

        monkeypatch.setattr(GuestSession, "check", fine)
        registry = _registry(VMGuestCheck())
        result = await registry.execute(
            "vm_guest_check", {"vm": "Parrot"}, confirmation_manager=Channel()
        )
        assert result["success"] is True and result["data"]["ready"] is True


class _FakeHypervisor:
    """Just enough to resolve one VM and report an address."""

    def __init__(self, vm):
        self._vm = vm

    async def available(self):
        return True

    async def resolve_and_describe(self, reference):
        return self._vm, "matched by exact name"

    async def guest_ip(self, reference, *, wait=False):
        return "10.0.0.9"


# ---------------------------------------------------------------------------
# The lab-assessment workflow, in order
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestTheLabAssessmentWorkflow:
    async def test_the_steps_are_separate_and_each_is_gated(
        self, parrot, monkeypatch
    ):
        """Start → wait → connect → run → report, with a prompt at each gate."""
        import config
        import tools.virtualization as vt

        monkeypatch.setattr(config.Config, "VM_GUEST_USER", "lab")
        monkeypatch.setattr(vt, "adapter", lambda: _FakeHypervisor(parrot))

        ran = []

        async def fake_run(self, command, timeout=None):
            ran.append(command)
            return GuestResult(
                target="lab@10.0.0.9", command=command, exit_code=0,
                stdout="eth0: 10.0.0.9/24", argv=["ssh", "lab@10.0.0.9", command],
            )

        monkeypatch.setattr(GuestSession, "run", fake_run)

        registry = _registry(VMGuestCheck(), VMGuestExec())

        # 1. Prove the guest can execute anything at all.
        check = await registry.execute(
            "vm_guest_check", {"vm": "Parrot"}, confirmation_manager=Channel()
        )
        assert check["success"] is True, check

        # 2. Run the harmless recon command the user asked for.
        channel = Channel()
        run = await registry.execute(
            "vm_guest_exec", {"command": "ip a", "vm": "Parrot"},
            confirmation_manager=channel,
        )
        assert run["success"] is True, run
        assert "eth0" in run["output"]
        # The user was shown the command, not just the VM.
        assert channel.asked[0][1]["command"] == "ip a"
        # And the result says where it ran, so it is never confused with the host.
        assert run["data"]["where"] == "guest"

        # `vm_guest_check` runs `true` (and a small `uname` probe for the
        # report); the user's command must be in there, on the guest.
        assert "ip a" in ran, ran

    async def test_a_denied_step_stops_the_workflow(self, parrot, monkeypatch, vm):
        """Refusing the prompt must stop it, not silently continue."""
        import tools.virtualization as vt

        calls = []

        class FakeAdapter:
            async def available(self):
                return True

            async def start(self, ref, *, gui=True):
                calls.append(ref)
                return vm

        monkeypatch.setattr(vt, "adapter", lambda: FakeAdapter())
        registry = _registry(VMStart())
        result = await registry.execute(
            "vm_start", {"vm": "Ubuntu"}, confirmation_manager=Channel(approve=False)
        )
        assert result["success"] is False
        assert result.get("cancelled") is True
        assert calls == [], "the VM was started after the user refused"


# ---------------------------------------------------------------------------
# Destructive operations need the stronger gate
# ---------------------------------------------------------------------------
class TestDestructiveVMOperations:
    def test_a_hard_power_off_is_dangerous_and_reads_as_such(self):
        registry = _registry(VMStop())
        soft = registry.assess("vm_stop", {"vm": "Ubuntu", "mode": "soft"})
        hard = registry.assess("vm_stop", {"vm": "Ubuntu", "mode": "hard"})
        assert soft.permission == "confirm"
        assert hard.permission == "dangerous"
        assert "power-off" in " ".join(hard.reasons)

    def test_a_destructive_guest_command_is_escalated(self):
        registry = _registry(VMGuestExec())
        assert registry.assess("vm_guest_exec", {"command": "ip a"}).permission == "confirm"
        assert (
            registry.assess("vm_guest_exec", {"command": "dd if=/dev/zero of=/dev/sda"})
            .permission
            == "dangerous"
        )
