"""The VM tools, and the guest channel — through the real registry.

Two things are being checked that a mocked-everything test would miss:

* **These are ordinary tools.** They are dispatched by `ToolRegistry.execute`,
  they obey the declared tiers, they prompt through the `ConfirmationManager`,
  and `vm_guest_exec` carrying a destructive command is escalated to DANGEROUS
  by the *existing* pattern table rather than by a rule invented here.
* **A missing prerequisite is reported as a missing prerequisite.** No guest
  account, no VM address, no VMware at all — each produces a message naming the
  thing to configure. None of them is reported as an empty success, and none is
  reported as a security refusal (`blocked`), because nothing refused anything.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from core.virtualization import GuestSession, GuestTarget, VMInfo, VMState
from core.virtualization.base import VMOperationError
from core.virtualization.guest import target_from_config
from tools.base import ToolValidationError

# ---------------------------------------------------------------------------
# A fake `ssh`, so the argv and the exit-code handling are real
# ---------------------------------------------------------------------------
_FAKE_SSH = '''#!{python}
"""A stand-in for `ssh`. Reads its script from $FAKE_SSH_SCRIPT."""
import json, os, sys

argv = sys.argv[1:]
if os.environ.get("FAKE_SSH_LOG"):
    with open(os.environ["FAKE_SSH_LOG"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(argv) + "\\n")

script = {{}}
path = os.environ.get("FAKE_SSH_SCRIPT", "")
if path and os.path.exists(path):
    with open(path, "r", encoding="utf-8") as fh:
        script = json.load(fh)

if script.get("sleep"):
    import time
    time.sleep(float(script["sleep"]))

sys.stdout.write(script.get("stdout", "ok\\n"))
sys.stderr.write(script.get("stderr", ""))
sys.exit(int(script.get("exit_code", 0)))
'''


@pytest.fixture
def fake_ssh(tmp_path):
    script = tmp_path / "ssh"
    script.write_text(_FAKE_SSH.format(python=sys.executable), encoding="utf-8")
    script.chmod(0o755)
    scenario = tmp_path / "ssh_scenario.json"
    log = tmp_path / "ssh_calls.log"
    scenario.write_text("{}", encoding="utf-8")

    class Fake:
        path = str(script)
        scenario_path = scenario
        log_path = log

        def set(self, **state) -> None:
            scenario.write_text(json.dumps(state), encoding="utf-8")

        def calls(self) -> list:
            if not log.exists():
                return []
            return [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]

    fake = Fake()
    os.environ["FAKE_SSH_SCRIPT"] = str(scenario)
    os.environ["FAKE_SSH_LOG"] = str(log)
    return fake


@pytest.fixture
def guest_target():
    return GuestTarget(host="10.0.0.9", user="lab", identity="/home/lab/.ssh/id_ed25519")


# ---------------------------------------------------------------------------
# The guest channel
# ---------------------------------------------------------------------------
class TestGuestTarget:
    def test_a_password_is_not_reachable_through_configuration(self):
        """Host-key verification cannot be turned off; there is no 'no'."""
        for bad in ("no", "off", "false", "disable"):
            with pytest.raises(ValueError) as exc:
                GuestTarget(host="h", user="u", host_key_policy=bad)
            assert "host-key" in str(exc.value)

    def test_accept_new_is_allowed_and_means_accept_new(self):
        target = GuestTarget(host="h", user="u", host_key_policy="accept-new")
        assert "StrictHostKeyChecking=accept-new" in target.ssh_options()

    def test_strict_is_the_default(self):
        target = GuestTarget(host="h", user="u")
        assert "StrictHostKeyChecking=yes" in target.ssh_options()

    def test_a_missing_user_is_refused_rather_than_guessed(self):
        with pytest.raises(ValueError) as exc:
            GuestTarget(host="h", user="")
        assert "user name" in str(exc.value)

    def test_a_missing_host_is_refused(self):
        with pytest.raises(ValueError):
            GuestTarget(host="  ", user="lab")

    def test_batch_mode_and_no_password_are_always_set(self):
        opts = GuestTarget(host="h", user="u").ssh_options()
        assert "BatchMode=yes" in opts
        assert "PasswordAuthentication=no" in opts
        # No option anywhere invites a password prompt.
        assert not any("sshpass" in o for o in opts)

    def test_config_without_a_user_means_not_configured(self, monkeypatch):
        import config

        monkeypatch.setattr(config.Config, "VM_GUEST_USER", "")
        assert target_from_config("10.0.0.9") is None

    def test_config_with_a_user_builds_a_target(self, monkeypatch):
        import config

        monkeypatch.setattr(config.Config, "VM_GUEST_USER", "lab")
        monkeypatch.setattr(config.Config, "VM_GUEST_IDENTITY", "/k/id")
        target = target_from_config("10.0.0.9")
        assert target is not None and target.user == "lab"


@pytest.mark.asyncio
class TestGuestSession:
    async def test_a_command_is_run_and_its_exit_code_captured(self, fake_ssh, guest_target):
        fake_ssh.set(stdout="eth0: 10.0.0.9\n", exit_code=0)
        session = GuestSession(guest_target, ssh=fake_ssh.path)
        result = await session.run("ip a")
        assert result.ok and result.exit_code == 0
        assert "eth0" in result.stdout
        assert result.duration_ms >= 0

    async def test_a_failing_command_is_not_reported_as_success(self, fake_ssh, guest_target):
        fake_ssh.set(stdout="", stderr="bash: nope: command not found", exit_code=127)
        session = GuestSession(guest_target, ssh=fake_ssh.path)
        result = await session.run("nope")
        assert result.ok is False
        assert result.exit_code == 127
        assert "command not found" in result.stderr

    async def test_the_command_reaches_the_guest_verbatim(self, fake_ssh, guest_target):
        """Pipes and redirection are legitimate guest work, not host hazards."""
        session = GuestSession(guest_target, ssh=fake_ssh.path)
        await session.run("cat /etc/os-release | head -n 2")
        argv = fake_ssh.calls()[0]
        assert "cat /etc/os-release | head -n 2" in argv
        # ...and it is one argv element, so the *host* never interprets it.
        assert argv.count("cat /etc/os-release | head -n 2") == 1

    async def test_the_destination_and_identity_are_in_the_argv(self, fake_ssh, guest_target):
        session = GuestSession(guest_target, ssh=fake_ssh.path)
        await session.run("true")
        argv = fake_ssh.calls()[0]
        assert "lab@10.0.0.9" in argv
        assert "-i" in argv and "/home/lab/.ssh/id_ed25519" in argv

    async def test_no_password_appears_anywhere_in_the_argv(self, fake_ssh, guest_target):
        session = GuestSession(guest_target, ssh=fake_ssh.path)
        await session.run("true")
        joined = " ".join(fake_ssh.calls()[0])
        assert "password" not in joined.lower() or "PasswordAuthentication=no" in joined

    async def test_refused_key_auth_explains_that_zaytoon_has_no_password(self, fake_ssh, guest_target):
        fake_ssh.set(
            stderr="lab@10.0.0.9: Permission denied (publickey).",
            exit_code=255,
        )
        session = GuestSession(guest_target, ssh=fake_ssh.path)
        result = await session.run("true")
        assert "never uses a password" in result.error
        assert "VM_GUEST_IDENTITY" in result.error

    async def test_an_untrusted_host_key_is_explained(self, fake_ssh, guest_target):
        fake_ssh.set(stderr="Host key verification failed.", exit_code=255)
        session = GuestSession(guest_target, ssh=fake_ssh.path)
        result = await session.run("true")
        assert "known_hosts" in result.error
        assert "accept-new" in result.error

    async def test_an_unreachable_guest_is_explained(self, fake_ssh, guest_target):
        fake_ssh.set(stderr="ssh: connect to host 10.0.0.9 port 22: No route to host", exit_code=255)
        session = GuestSession(guest_target, ssh=fake_ssh.path)
        result = await session.run("true")
        assert "not reachable" in result.error

    async def test_a_timeout_says_the_command_may_still_be_running(self, fake_ssh, guest_target):
        fake_ssh.set(sleep=3)
        session = GuestSession(guest_target, ssh=fake_ssh.path)
        result = await session.run("sleep 300", timeout=0.3)
        assert result.timed_out is True
        assert result.ok is False
        assert "still be booting" in result.error or "did not respond" in result.error

    async def test_an_empty_command_is_not_sent(self, fake_ssh, guest_target):
        session = GuestSession(guest_target, ssh=fake_ssh.path)
        result = await session.run("   ")
        assert result.ok is False
        assert fake_ssh.calls() == []

    async def test_the_audit_trail_cannot_contain_a_secret(self, fake_ssh, guest_target):
        session = GuestSession(guest_target, ssh=fake_ssh.path)
        result = await session.run("true")
        payload = result.to_dict()
        assert payload["command_line"].startswith("ssh") or "ssh" in payload["command_line"]
        assert "password" not in payload["command_line"].lower().replace(
            "passwordauthentication=no", ""
        )

    async def test_check_proves_execution_not_just_connectivity(self, fake_ssh, guest_target):
        fake_ssh.set(exit_code=0)
        session = GuestSession(guest_target, ssh=fake_ssh.path)
        assert (await session.check()).ok is True
        assert "true" in fake_ssh.calls()[0]

    async def test_info_reports_what_the_guest_said(self, fake_ssh, guest_target):
        fake_ssh.set(stdout="Linux parrot 6.1.0 #1 SMP x86_64 GNU/Linux\nPRETTY_NAME=\"Parrot\"")
        session = GuestSession(guest_target, ssh=fake_ssh.path)
        info = await session.info()
        assert info["ok"] is True
        assert "parrot" in info["reported"].lower()


# ---------------------------------------------------------------------------
# Registration, tiers and permissions
# ---------------------------------------------------------------------------
@pytest.fixture
def registry():
    from tools.registry import build_default_registry

    return build_default_registry()


class TestRegistrationAndTiers:
    def test_every_vm_tool_is_registered(self, registry):
        names = set(registry.list())
        expected = {
            "vm_list", "vm_status", "vm_start", "vm_stop", "vm_suspend",
            "vm_resume", "vm_open_console", "vm_guest_check", "vm_guest_exec",
        }
        assert expected <= names, expected - names

    def test_read_only_tools_do_not_prompt(self, registry):
        """A prompt on "list my VMs" is noise, and noise trains people to approve."""
        for name in ("vm_list", "vm_status"):
            assessment = registry.assess(name, {})
            assert assessment.requires_confirmation is False, name

    def test_every_state_changing_tool_prompts(self, registry):
        for name, params in (
            ("vm_start", {"vm": "U"}),
            ("vm_stop", {"vm": "U"}),
            ("vm_suspend", {"vm": "U"}),
            ("vm_resume", {"vm": "U"}),
            ("vm_open_console", {"vm": "U"}),
            ("vm_guest_check", {"vm": "U"}),
            ("vm_guest_exec", {"command": "ip a"}),
        ):
            assert registry.assess(name, params).requires_confirmation is True, name

    def test_a_hard_power_off_is_dangerous(self, registry):
        assert registry.assess("vm_stop", {"vm": "U", "mode": "soft"}).permission == "confirm"
        hard = registry.assess("vm_stop", {"vm": "U", "mode": "hard"})
        assert hard.permission == "dangerous"
        assert any("power-off" in r for r in hard.reasons)

    def test_a_destructive_guest_command_is_dangerous(self, registry):
        """Escalated by the existing pattern table, not by a VM-specific rule."""
        benign = registry.assess("vm_guest_exec", {"command": "ip a", "vm": "U"})
        assert benign.permission == "confirm"
        risky = registry.assess("vm_guest_exec", {"command": "rm -rf /", "vm": "U"})
        assert risky.permission == "dangerous"
        assert risky.reasons

    def test_the_scopes_stay_distinct(self):
        """Consent for one capability must not be consent for another."""
        from core.permissions import Permission, required_for

        discover = required_for("vm_list", "safe")
        manage = required_for("vm_start", "confirm")
        guest = required_for("vm_guest_exec", "confirm")

        # Each grants its own, and does not grant the others.
        assert Permission.VM_DISCOVER in discover
        assert Permission.VM_MANAGE not in discover
        assert Permission.VM_GUEST_EXECUTE not in discover

        assert Permission.VM_MANAGE in manage
        assert Permission.VM_GUEST_EXECUTE not in manage

        assert Permission.VM_GUEST_EXECUTE in guest
        assert Permission.VM_MANAGE not in guest

        # Guest execution reaches the network; lifecycle management does not.
        assert Permission.NETWORK_REQUEST in guest
        assert Permission.NETWORK_REQUEST not in manage

    def test_vm_status_is_read_only(self, registry):
        assessment = registry.assess("vm_status", {"vm": "Ubuntu"})
        assert assessment.requires_confirmation is False


# ---------------------------------------------------------------------------
# The tools, against a fake hypervisor
# ---------------------------------------------------------------------------
def _fake_adapter(vms, *, failing=None, tools_state="running", guest_ip="10.0.0.9"):
    """An adapter double that is honest about what it was asked to do.

    Not a stub returning canned strings: it records the calls, and it reports
    state the way the real adapter does — from a re-read, not from the request.
    """
    from core.virtualization import Readiness
    from core.virtualization.vmware import VMwareWorkstation

    calls = []

    class FakeAdapter(VMwareWorkstation):
        def __init__(self):
            super().__init__(vmrun=None, why_unavailable="fake")

        async def available(self):
            return True

        async def list_vms(self):
            calls.append("list")
            return list(vms)

        async def _resolve_live(self, reference):
            for vm in vms:
                if vm.name.lower() == str(reference).lower():
                    return vm, "matched by exact name"
            raise VMOperationError(f"No virtual machine matches '{reference}'.")

        async def resolve_and_describe(self, reference):
            return await self._resolve_live(reference)

        async def start(self, vm_ref, *, gui=True):
            # Resolve first, like the real adapter, so an unknown name produces
            # the resolution error rather than a downstream one.
            await self._resolve_live(vm_ref)
            calls.append(f"start:{vm_ref}:gui={gui}")
            if failing == "start":
                raise VMOperationError("vmrun start failed (exit 1): The VM is already locked")
            for vm in vms:
                if vm.name.lower() == str(vm_ref).lower():
                    vm.state = VMState.RUNNING
                    vm.state_evidence = "listed by `vmrun list`"
                    return vm
            raise VMOperationError("gone")

        async def stop(self, vm_ref, *, mode="soft"):
            await self._resolve_live(vm_ref)
            calls.append(f"stop:{vm_ref}:{mode}")
            for vm in vms:
                if vm.name.lower() == str(vm_ref).lower():
                    vm.state = VMState.STOPPED
                    vm.state_evidence = "not listed by `vmrun list`"
                    return vm
            raise VMOperationError("gone")

        async def suspend(self, vm_ref):
            await self._resolve_live(vm_ref)
            calls.append(f"suspend:{vm_ref}")
            for vm in vms:
                if vm.name.lower() == str(vm_ref).lower():
                    vm.state = VMState.SUSPENDED
                    return vm
            raise VMOperationError("gone")

        async def resume(self, vm_ref):
            await self._resolve_live(vm_ref)
            calls.append(f"resume:{vm_ref}")
            for vm in vms:
                if vm.name.lower() == str(vm_ref).lower():
                    vm.state = VMState.RUNNING
                    return vm
            raise VMOperationError("gone")

        async def open_console(self, vm_ref):
            calls.append(f"console:{vm_ref}")
            return f"Asked the VMware UI to show '{vm_ref}'."

        async def guest_ip(self, vm_ref, *, wait=False):
            return guest_ip

        async def tools_state(self, vm_ref):
            return tools_state

        def _find(self, reference):
            """Match by display name *or* by path, as the real adapter does.

            Getting this wrong is not hypothetical: an earlier version matched
            names only, and the tool passes the VM's `.vmx` path, so `next()`
            over an empty generator turned a ready guest into
            `RuntimeError: coroutine raised StopIteration`.
            """
            ref = str(reference)
            for vm in vms:
                if vm.name.lower() == ref.lower() or vm.path == ref:
                    return vm
            raise VMOperationError(f"No virtual machine matches '{ref}'.")

        async def readiness(self, vm_ref, *, probe=None):
            target = self._find(vm_ref)
            report = Readiness(vm=target.name, running=target.running, tools_state=tools_state)
            report.checks = [f"state: {target.state.value}"]
            if probe is not None:
                report.guest_reachable = bool(await probe())
            report.detail = (
                "guest answered" if report.ready
                else "running, but nothing confirmed the guest can run anything"
            )
            return report

    return FakeAdapter(), calls


@pytest.fixture
def patched_adapter(monkeypatch):
    """Install a fake adapter into the tool module."""
    import tools.virtualization as vt

    def install(vms, **kwargs):
        adapter, calls = _fake_adapter(vms, **kwargs)
        monkeypatch.setattr(vt, "_ADAPTER_FACTORY", lambda: adapter)
        return calls

    return install


@pytest.fixture
def ubuntu_vm():
    return VMInfo(
        name="Ubuntu", path="/vms/u/Ubuntu.vmx", state=VMState.STOPPED,
        state_evidence="not listed by `vmrun list`", guest_os="ubuntu-64",
    )


@pytest.mark.asyncio
class TestVMListTool:
    async def test_no_vmware_is_a_host_problem_not_a_refusal(self, monkeypatch):
        """`blocked` means a security decision. This is not one."""
        import tools.virtualization as vt
        from core.virtualization.vmware import VMwareWorkstation

        monkeypatch.setattr(
            vt, "adapter",
            lambda: VMwareWorkstation(why_unavailable="`vmrun` was not found ... VMWARE_VMRUN"),
        )
        result = await vt.VMList().execute()
        assert result.success is False
        assert result.blocked is False
        assert "VMWARE_VMRUN" in result.error

    async def test_it_lists_state_with_evidence(self, patched_adapter, ubuntu_vm):
        import tools.virtualization as vt

        patched_adapter([ubuntu_vm])
        result = await vt.VMList().execute()
        assert result.success is True
        assert "Ubuntu" in result.output
        assert "vmrun list" in result.output  # the evidence, not just the state
        assert result.data["count"] == 1

    async def test_nothing_found_says_where_to_look(self, patched_adapter):
        import tools.virtualization as vt

        patched_adapter([])
        result = await vt.VMList().execute()
        assert result.success is True
        assert "VM_INVENTORY_DIRS" in result.output


@pytest.mark.asyncio
class TestLifecycleTools:
    async def test_start_reports_state_and_withholds_a_boot_claim(self, patched_adapter, ubuntu_vm):
        import tools.virtualization as vt

        calls = patched_adapter([ubuntu_vm])
        result = await vt.VMStart().execute(vm="Ubuntu")
        assert result.success is True
        assert "running" in result.output
        # The important half: it does not claim the guest booted.
        assert "not boot evidence" in result.output
        assert any(c.startswith("start:") for c in calls)

    async def test_start_waiting_for_ready_fails_when_the_guest_never_answers(
        self, patched_adapter, ubuntu_vm, monkeypatch
    ):
        """`wait_for_ready` must not report success on an unverified boot."""
        import config
        import tools.virtualization as vt

        import core.virtualization.guest as guest_mod
        from core.virtualization.guest import GuestResult

        monkeypatch.setattr(config.Config, "VM_GUEST_USER", "lab")
        patched_adapter([ubuntu_vm])

        async def never(*a, **k):
            return GuestResult(target="lab@10.0.0.9", command="true", exit_code=255,
                               stderr="Permission denied (publickey).")

        monkeypatch.setattr(guest_mod.GuestSession, "check", never)
        result = await vt.VMStart().execute(vm="Ubuntu", wait_for_ready=True)
        assert result.success is False
        assert "not confirmed ready" in result.error

    async def test_start_waiting_for_ready_succeeds_when_the_guest_answers(
        self, patched_adapter, ubuntu_vm, monkeypatch
    ):
        import config
        import tools.virtualization as vt
        import core.virtualization.guest as guest_mod
        from core.virtualization.guest import GuestResult

        monkeypatch.setattr(config.Config, "VM_GUEST_USER", "lab")
        patched_adapter([ubuntu_vm])

        async def fine(*a, **k):
            return GuestResult(target="lab@10.0.0.9", command="true", exit_code=0)

        monkeypatch.setattr(guest_mod.GuestSession, "check", fine)
        result = await vt.VMStart().execute(vm="Ubuntu", wait_for_ready=True)
        assert result.success is True
        assert "ready" in result.output

    async def test_a_lifecycle_failure_is_reported_with_vmrun_words(self, patched_adapter, ubuntu_vm):
        import tools.virtualization as vt

        patched_adapter([ubuntu_vm], failing="start")
        result = await vt.VMStart().execute(vm="Ubuntu")
        assert result.success is False
        assert "already locked" in result.error

    async def test_stop_passes_the_mode_and_warns_on_hard(self, patched_adapter, ubuntu_vm):
        import tools.virtualization as vt

        ubuntu_vm.state = VMState.RUNNING
        calls = patched_adapter([ubuntu_vm])
        result = await vt.VMStop().execute(vm="Ubuntu", mode="hard")
        assert result.success is True
        assert "can lose anything" in result.output
        assert any(":hard" in c for c in calls)

    async def test_an_invalid_stop_mode_is_refused(self, patched_adapter, ubuntu_vm):
        import tools.virtualization as vt

        ubuntu_vm.state = VMState.RUNNING
        calls = patched_adapter([ubuntu_vm])
        result = await vt.VMStop().execute(vm="Ubuntu", mode="explode")
        assert result.success is False
        assert calls == []  # nothing was attempted

    async def test_an_unknown_vm_is_reported_not_guessed(self, patched_adapter, ubuntu_vm):
        import tools.virtualization as vt

        patched_adapter([ubuntu_vm])
        result = await vt.VMStart().execute(vm="Debian")
        assert result.success is False
        assert "No virtual machine matches" in result.error


@pytest.mark.asyncio
class TestGuestToolsNeedPrerequisites:
    async def test_guest_exec_without_an_account_names_the_variable(self, patched_adapter, ubuntu_vm, monkeypatch):
        """The prerequisite is reported, not invented around."""
        import config
        import tools.virtualization as vt

        monkeypatch.setattr(config.Config, "VM_GUEST_USER", "")
        ubuntu_vm.state = VMState.RUNNING
        patched_adapter([ubuntu_vm])
        result = await vt.VMGuestExec().execute(command="ip a", vm="Ubuntu")
        assert result.success is False
        assert "VM_GUEST_USER" in result.error
        assert "never uses a password" in result.error

    async def test_guest_exec_without_an_address_explains_tools(self, patched_adapter, ubuntu_vm, monkeypatch):
        import config
        import tools.virtualization as vt

        monkeypatch.setattr(config.Config, "VM_GUEST_USER", "lab")
        ubuntu_vm.state = VMState.RUNNING
        patched_adapter([ubuntu_vm], guest_ip="")  # Tools could not say
        result = await vt.VMGuestExec().execute(command="ip a", vm="Ubuntu")
        assert result.success is False
        assert "VMware Tools" in result.error

    async def test_an_explicit_host_needs_no_hypervisor_at_all(self, fake_ssh, monkeypatch):
        """A guest can be reached by address even with no VMware on the host."""
        import config
        import tools.virtualization as vt

        monkeypatch.setattr(config.Config, "VM_GUEST_USER", "lab")
        monkeypatch.setattr(config.Config, "VM_GUEST_IDENTITY", "")
        fake_ssh.set(stdout="ok\n", exit_code=0)
        monkeypatch.setattr(vt, "GuestSession", lambda t: GuestSession(t, ssh=fake_ssh.path))

        result = await vt.VMGuestExec().execute(command="uptime", host="10.0.0.9")
        assert result.success is True
        assert "where" in result.data and result.data["where"] == "guest"
        assert result.data["exit_code"] == 0

    async def test_a_failing_guest_command_returns_its_exit_code(self, fake_ssh, monkeypatch):
        import config
        import tools.virtualization as vt

        monkeypatch.setattr(config.Config, "VM_GUEST_USER", "lab")
        fake_ssh.set(stderr="nope", exit_code=2)
        monkeypatch.setattr(vt, "GuestSession", lambda t: GuestSession(t, ssh=fake_ssh.path))

        result = await vt.VMGuestExec().execute(command="false", host="10.0.0.9")
        assert result.success is False
        assert result.data["exit_code"] == 2
        assert "guest" in result.error

    async def test_an_empty_command_is_a_validation_error(self):
        import tools.virtualization as vt

        with pytest.raises(ToolValidationError):
            vt.VMGuestExec().validate({"command": "  "})

    async def test_guest_check_reports_readiness_honestly(self, fake_ssh, monkeypatch):
        import config
        import tools.virtualization as vt

        monkeypatch.setattr(config.Config, "VM_GUEST_USER", "lab")
        monkeypatch.setattr(vt, "GuestSession", lambda t: GuestSession(t, ssh=fake_ssh.path))

        fake_ssh.set(exit_code=0, stdout="Linux parrot 6.1.0\n")
        ready = await vt.VMGuestCheck().execute(host="10.0.0.9")
        assert ready.success is True and ready.data["ready"] is True

        fake_ssh.set(stderr="Permission denied (publickey).", exit_code=255)
        not_ready = await vt.VMGuestCheck().execute(host="10.0.0.9")
        assert not_ready.success is False
        assert not_ready.data["ready"] is False


@pytest.mark.asyncio
class TestTheGateStillApplies:
    async def test_a_state_changing_vm_tool_prompts_through_the_registry(self, patched_adapter, ubuntu_vm):
        """VM tools are ordinary tools: the tier gate runs, and it is real."""
        from tools.registry import ToolRegistry

        patched_adapter([ubuntu_vm])
        from tools.virtualization import VMStart

        registry = ToolRegistry()
        registry.register(VMStart())

        shown = []

        class Channel:
            async def request(self, action, params=None, session_id=""):
                shown.append(action)
                return True

        result = await registry.execute("vm_start", {"vm": "Ubuntu"}, confirmation_manager=Channel())
        assert shown == ["vm_start"], "a VM was started without prompting"
        assert result["success"] is True

    async def test_denying_the_prompt_stops_the_vm(self, patched_adapter, ubuntu_vm):
        from tools.registry import ToolRegistry
        from tools.virtualization import VMStart

        calls = patched_adapter([ubuntu_vm])
        registry = ToolRegistry()
        registry.register(VMStart())

        class Deny:
            async def request(self, action, params=None, session_id=""):
                return False

        result = await registry.execute("vm_start", {"vm": "Ubuntu"}, confirmation_manager=Deny())
        assert result["success"] is False
        assert result.get("cancelled") is True
        assert calls == [], "the VM was started despite the denial"

    async def test_a_guest_command_prompts_and_shows_the_command(self, patched_adapter, ubuntu_vm, monkeypatch):
        """The dialog must show what will run in the guest, not just which VM."""
        import tools.virtualization as vt
        from tools.registry import ToolRegistry

        ubuntu_vm.state = VMState.RUNNING
        patched_adapter([ubuntu_vm])
        registry = ToolRegistry()
        registry.register(vt.VMGuestExec())

        seen = {}

        class Channel:
            async def request(self, action, params=None, session_id=""):
                seen["action"] = action
                seen["params"] = params or {}
                return False

        await registry.execute(
            "vm_guest_exec", {"command": "cat /etc/shadow", "vm": "Ubuntu"},
            confirmation_manager=Channel(),
        )
        assert seen["action"] == "vm_guest_exec"
        assert seen["params"].get("command") == "cat /etc/shadow"
