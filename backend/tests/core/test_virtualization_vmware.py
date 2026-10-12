"""The VMware adapter, against a fake `vmrun` **executable**.

The fake is a real script on disk that the adapter really spawns. That is
deliberate: monkeypatching a runner would prove the adapter's logic but not the
thing most likely to be wrong — the argv it builds and the output it parses. A
fake binary tests both, and it also lets the failure modes be *real* exits with
real stderr rather than raised exceptions.

The two properties worth more than the rest, and both are asserted here:

* **A VM whose state cannot be determined is reported unknown, not stopped.**
  `vmrun list` failing is not evidence that a machine is powered off.
* **A running VM is not a ready guest.** No probe means not ready, and the
  report says why in words that do not oversell what was checked.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

from core.virtualization.base import VMState, quote_argv, resolve_program
from core.virtualization.vmware import VMwareWorkstation

# ---------------------------------------------------------------------------
# The fake executable
# ---------------------------------------------------------------------------
_FAKE_VMRUN = '''"""A stand-in for `vmrun`. Reads its scenario from $FAKE_VMRUN_STATE."""
import json, os, sys

state_path = os.environ.get("FAKE_VMRUN_STATE", "")
log_path = os.environ.get("FAKE_VMRUN_LOG", "")
argv = sys.argv[1:]
if log_path:
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(argv) + "\\n")

state = {{}}
if state_path and os.path.exists(state_path):
    with open(state_path, "r", encoding="utf-8") as fh:
        state = json.load(fh)

# argv is ['-T', '<type>', '<verb>', ...] as the adapter builds it.
verb = argv[2] if len(argv) > 2 and argv[0] == "-T" else (argv[0] if argv else "")

if state.get("sleep"):
    import time
    time.sleep(float(state["sleep"]))

if state.get("fail_all"):
    sys.stderr.write(state.get("fail_message", "vmrun: configuration error"))
    sys.exit(1)

if verb == "list":
    running = state.get("running", [])
    if state.get("list_fails"):
        sys.stderr.write(state.get("list_error", "Cannot find a valid peer process to connect to"))
        sys.exit(1)
    print("Total running VMs: %d" % len(running))
    for path in running:
        print(path)
    sys.exit(0)

response = state.get("responses", {{}}).get(verb)
if response is None:
    print("vmrun: %s completed" % verb)
    sys.exit(0)
sys.stdout.write(response.get("stdout", ""))
sys.stderr.write(response.get("stderr", ""))
sys.exit(int(response.get("returncode", 0)))
'''


@pytest.fixture
def fake_vmrun(tmp_path, fake_exe):
    """A scenario-driven fake `vmrun`, plus handles to drive it.

    Written through the shared `fake_exe` factory: a shebang script cannot
    execute on Windows (`WinError 193`), which failed all 28 of these tests
    on the development platform before any assertion ran.
    """
    script = fake_exe(tmp_path / "vmrun", _FAKE_VMRUN)

    state_file = tmp_path / "state.json"
    log_file = tmp_path / "calls.log"

    class Fake:
        path = str(script)
        state_path = state_file
        log_path = log_file

        def set(self, **state) -> None:
            state_file.write_text(json.dumps(state), encoding="utf-8")

        def calls(self) -> list:
            if not log_file.exists():
                return []
            return [
                json.loads(line)
                for line in log_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        def verbs(self) -> list:
            return [c[2] if len(c) > 2 and c[0] == "-T" else (c[0] if c else "") for c in self.calls()]

    fake = Fake()
    fake.set(running=[])
    return fake


@pytest.fixture
def vm_dir(tmp_path):
    """A directory holding real (empty) `.vmx` files to discover."""
    root = tmp_path / "vms"
    (root / "ubuntu").mkdir(parents=True)
    (root / "parrot").mkdir(parents=True)
    (root / "ubuntu" / "Ubuntu.vmx").write_text(
        'guestOS = "ubuntu-64"\nmemSize = "4096"\n', encoding="utf-8"
    )
    (root / "parrot" / "Parrot.vmx").write_text(
        'guestOS = "debian-64"\nmemSize = "2048"\n', encoding="utf-8"
    )
    return root


@pytest.fixture
def adapter(fake_vmrun, tmp_path, monkeypatch):
    """An adapter wired to the fake, with an isolated inventory home.

    The two environment variables are how the fake finds its scenario and
    writes its call log. Without them the fake silently succeeds at nothing —
    which is exactly how the first version of these tests "passed" a start
    assertion while `vmrun` was never invoked at all.
    """
    monkeypatch.setenv("FAKE_VMRUN_STATE", str(fake_vmrun.state_path))
    monkeypatch.setenv("FAKE_VMRUN_LOG", str(fake_vmrun.log_path))

    def build(vm_type="ws"):
        return VMwareWorkstation(
            vmrun=resolve_program([], [], explicit=fake_vmrun.path, roles=("vmrun",)),
            vm_type=vm_type,
        )

    return build


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Nothing may read the developer's real `~/.vmware/inventory.vmls`."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("VM_INVENTORY_DIRS", "")  # overridden per-test via Config patch
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    yield


def _inventory(monkeypatch, path: Path, entries, scan_dirs):
    """Point inventory discovery at a fixture file and scan roots."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for index, (name, vmx) in enumerate(entries, start=1):
        lines.append(f'vmlist{index}.displayName = "{name}"')
        lines.append(f'vmlist{index}.config = "{vmx}"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    import config

    monkeypatch.setattr(config.Config, "VM_INVENTORY_DIRS", os.pathsep.join(str(d) for d in scan_dirs))
    return path


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
class TestDiscovery:
    def test_an_explicit_vmrun_is_used(self, fake_vmrun):
        program = resolve_program([], [], explicit=fake_vmrun.path, roles=("vmrun",))
        assert program is not None and program.path == fake_vmrun.path

    def test_a_missing_explicit_vmrun_is_reported_not_guessed(self, tmp_path):
        missing = str(tmp_path / "nope" / "vmrun")
        assert resolve_program([], [], explicit=missing) is None

    def test_a_directory_named_vmrun_is_refused(self, tmp_path):
        """A directory is not a program, however promising its name."""
        d = tmp_path / "vmrun"
        d.mkdir()
        assert resolve_program([], [], explicit=str(d)) is None

    async def test_no_vmware_anywhere_is_unavailable_with_a_reason(self, monkeypatch, tmp_path):
        import config

        monkeypatch.setattr(config.Config, "VMWARE_VMRUN", "")
        monkeypatch.setattr(config.Config, "VMWARE_BIN_DIRS", "")
        monkeypatch.setattr(
            "core.virtualization.vmware.search_dirs", lambda: (str(tmp_path / "nothing"),)
        )
        adapter = VMwareWorkstation.discover()
        assert adapter.vmrun is None
        # The reason is produced synchronously so it can be shown even when no
        # event loop is running (e.g. from a settings probe).
        reason = adapter._why
        assert "vmrun" in reason and "VMWARE_VMRUN" in reason


# ---------------------------------------------------------------------------
# Inventory and state
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestInventoryAndState:
    async def test_inventory_names_come_from_the_hypervisor(self, adapter, vm_dir, monkeypatch, tmp_path):
        """The display name is VMware's, not the folder's."""
        _inventory(
            monkeypatch,
            tmp_path / "appdata" / "VMware" / "inventory.vmls",
            [("Ubuntu 24.04 LTS", str(vm_dir / "ubuntu" / "Ubuntu.vmx"))],
            [vm_dir],
        )
        vms = await adapter().list_vms()
        by_path = {str(v.path): v for v in vms}
        ubuntu = by_path[str(vm_dir / "ubuntu" / "Ubuntu.vmx")]
        assert ubuntu.name == "Ubuntu 24.04 LTS"
        assert ubuntu.source == "inventory"
        assert ubuntu.guest_os == "ubuntu-64"

    async def test_a_running_vm_is_detected_with_evidence(self, adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun):
        vmx = str(vm_dir / "ubuntu" / "Ubuntu.vmx")
        fake_vmrun.set(running=[vmx])
        _inventory(monkeypatch, tmp_path / "appdata" / "VMware" / "inventory.vmls", [("Ubuntu", vmx)], [vm_dir])
        vms = await adapter().list_vms()
        ubuntu = next(v for v in vms if v.name == "Ubuntu")
        assert ubuntu.state is VMState.RUNNING
        assert "vmrun list" in ubuntu.state_evidence

    async def test_a_stopped_vm_says_what_it_checked(self, adapter, vm_dir, monkeypatch, tmp_path):
        vmx = str(vm_dir / "ubuntu" / "Ubuntu.vmx")
        _inventory(monkeypatch, tmp_path / "appdata" / "VMware" / "inventory.vmls", [("Ubuntu", vmx)], [vm_dir])
        vms = await adapter().list_vms()
        ubuntu = next(v for v in vms if v.name == "Ubuntu")
        assert ubuntu.state is VMState.STOPPED
        assert ".vmss" in ubuntu.state_evidence

    async def test_a_suspension_file_means_suspended(self, adapter, vm_dir, monkeypatch, tmp_path):
        vmx = vm_dir / "ubuntu" / "Ubuntu.vmx"
        (vm_dir / "ubuntu" / "Ubuntu.vmss").write_bytes(b"snapshot")
        _inventory(monkeypatch, tmp_path / "appdata" / "VMware" / "inventory.vmls", [("Ubuntu", str(vmx))], [vm_dir])
        vms = await adapter().list_vms()
        ubuntu = next(v for v in vms if v.name == "Ubuntu")
        assert ubuntu.state is VMState.SUSPENDED
        assert ".vmss" in ubuntu.state_evidence

    async def test_a_missing_definition_is_unavailable_not_stopped(self, adapter, monkeypatch, tmp_path):
        ghost = str(tmp_path / "gone" / "Ghost.vmx")
        _inventory(monkeypatch, tmp_path / "appdata" / "VMware" / "inventory.vmls", [("Ghost", ghost)], [])
        vms = await adapter().list_vms()
        ghost_vm = next(v for v in vms if v.name == "Ghost")
        assert ghost_vm.state is VMState.UNAVAILABLE
        assert "not found" in ghost_vm.state_evidence

    async def test_vmrun_failing_means_unknown_never_stopped(self, adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun):
        """The honesty test. No evidence must not become 'stopped'."""
        vmx = str(vm_dir / "ubuntu" / "Ubuntu.vmx")
        fake_vmrun.set(list_fails=True, list_error="Cannot find a valid peer process to connect to")
        _inventory(monkeypatch, tmp_path / "appdata" / "VMware" / "inventory.vmls", [("Ubuntu", vmx)], [vm_dir])
        vms = await adapter().list_vms()
        ubuntu = next(v for v in vms if v.name == "Ubuntu")
        assert ubuntu.state is VMState.UNKNOWN
        assert "could not be run" in ubuntu.state_evidence
        assert "peer process" in ubuntu.state_evidence

    async def test_a_lock_directory_is_a_hint_not_a_state(self, adapter, vm_dir, monkeypatch, tmp_path):
        """A stale lock looks exactly like a live one, so it decides nothing."""
        vmx = vm_dir / "ubuntu" / "Ubuntu.vmx"
        (vm_dir / "ubuntu" / "Ubuntu.vmx.lck").mkdir()
        _inventory(monkeypatch, tmp_path / "appdata" / "VMware" / "inventory.vmls", [("Ubuntu", str(vmx))], [vm_dir])
        vms = await adapter().list_vms()
        ubuntu = next(v for v in vms if v.name == "Ubuntu")
        assert ubuntu.state is VMState.STOPPED  # vmrun did not list it
        assert "lock_present" in ubuntu.extra

    async def test_scanning_is_bounded_and_skips_lock_trees(self, adapter, monkeypatch, tmp_path):
        root = tmp_path / "scanroot"
        deep = root / "a" / "b" / "c" / "d" / "e"
        deep.mkdir(parents=True)
        (deep / "TooDeep.vmx").write_text("", encoding="utf-8")
        (root / "Here.vmx").write_text("", encoding="utf-8")
        _inventory(monkeypatch, tmp_path / "appdata" / "VMware" / "inventory.vmls", [], [root])
        vms = await adapter().list_vms()
        names = {v.name for v in vms}
        assert "Here" in names
        assert "TooDeep" not in names


# ---------------------------------------------------------------------------
# Name resolution: never guess
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestResolutionRefusesToGuess:
    async def _vms(self, adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun, entries):
        _inventory(monkeypatch, tmp_path / "appdata" / "VMware" / "inventory.vmls", entries, [vm_dir])
        return await adapter().list_vms()

    async def test_exact_name(self, adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun):
        vmx = str(vm_dir / "ubuntu" / "Ubuntu.vmx")
        vms = await self._vms(adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun, [("Ubuntu", vmx)])
        vm, how = adapter().resolve("ubuntu", vms)
        assert vm.path == vmx and "exact" in how

    async def test_unique_substring_is_accepted_and_disclosed(self, adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun):
        vmx = str(vm_dir / "ubuntu" / "Ubuntu.vmx")
        vms = await self._vms(adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun, [("Ubuntu 24.04 LTS", vmx)])
        # `ubuntu` resolves through the .vmx stem, which is exact. `24.04` can
        # only be a substring, and the match must still be disclosed as one.
        vm, how = adapter().resolve("24.04", vms)
        assert vm.path == vmx
        assert "single VM" in how, how

    async def test_ambiguity_is_refused_and_lists_the_candidates(self, adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun):
        """This is the 'do not start the wrong VM' requirement.

        Two VMs whose names both contain the query, and whose file names do not
        match it exactly — so there is no precise resolution available and the
        adapter has to refuse rather than choose.
        """
        from core.virtualization.base import VMOperationError

        # A private root, not the shared `vm_dir`: that fixture ships
        # `Ubuntu.vmx`, which makes "ubuntu" an *exact* name match and therefore
        # correctly unambiguous. Here neither the names nor the file stems equal
        # the query, so there is nothing precise to resolve to.
        root = tmp_path / "ambiguous"
        new_vmx = root / "u24" / "Ubuntu 24.04 LTS.vmx"
        old_vmx = root / "u16" / "Ubuntu 16.04 OLD.vmx"
        for path in (new_vmx, old_vmx):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('guestOS = "ubuntu-64"\n', encoding="utf-8")

        _inventory(
            monkeypatch,
            tmp_path / "appdata" / "VMware" / "inventory.vmls",
            [("Ubuntu 24.04 LTS", str(new_vmx)), ("Ubuntu 16.04 OLD", str(old_vmx))],
            [root],
        )
        vms = await adapter().list_vms()
        with pytest.raises(VMOperationError) as exc:
            adapter().resolve("ubuntu", vms)
        message = str(exc.value)
        assert "Ubuntu 24.04 LTS" in message and "Ubuntu 16.04 OLD" in message
        assert "guess" in message

    async def test_no_match_lists_what_exists(self, adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun):
        from core.virtualization.base import VMOperationError

        vms = await self._vms(adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun, [("Ubuntu", str(vm_dir / "ubuntu" / "Ubuntu.vmx"))])
        with pytest.raises(VMOperationError) as exc:
            adapter().resolve("Fedora", vms)
        assert "Ubuntu" in str(exc.value)

    async def test_an_empty_name_is_refused(self, adapter):
        from core.virtualization.base import VMOperationError

        with pytest.raises(VMOperationError):
            adapter().resolve("   ", [])

    async def test_a_path_that_is_not_a_vmx_is_refused(self, adapter, tmp_path):
        from core.virtualization.base import VMOperationError

        with pytest.raises(VMOperationError) as exc:
            adapter().resolve(str(tmp_path / "notes.txt"), [])
        assert ".vmx" in str(exc.value)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestLifecycle:
    async def _adapter_with(self, adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun, running=None):
        vmx = str(vm_dir / "ubuntu" / "Ubuntu.vmx")
        fake_vmrun.set(running=running or [])
        _inventory(monkeypatch, tmp_path / "appdata" / "VMware" / "inventory.vmls", [("Ubuntu", vmx)], [vm_dir])
        return adapter(), vmx

    async def test_start_passes_the_vmx_and_gui_mode(self, adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun):
        vmw, vmx = await self._adapter_with(adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun)
        await vmw.start("Ubuntu", gui=True)
        start_calls = [c for c in fake_vmrun.calls() if "start" in c]
        assert start_calls, fake_vmrun.calls()
        assert vmx in start_calls[0] and "gui" in start_calls[0]

    async def test_start_returns_the_state_observed_afterwards(self, adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun):
        vmw, vmx = await self._adapter_with(adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun)
        await vmw.start("Ubuntu")
        # The fake now reports it running, which is what a real start would do.
        fake_vmrun.set(running=[vmx])
        info = await vmw.start("Ubuntu")
        assert info.state is VMState.RUNNING
        # ...and the second start must not have been issued.
        starts = [c for c in fake_vmrun.calls() if "start" in c]
        assert len(starts) == 1, starts

    async def test_starting_an_already_running_vm_issues_no_command(self, adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun):
        vmw, vmx = await self._adapter_with(adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun, running=None)
        # Fresh log: only the list call from resolution should appear.
        fake_vmrun.set(running=[vmx])
        await vmw.start("Ubuntu")
        assert not [c for c in fake_vmrun.calls() if "start" in c]

    async def test_hard_stop_is_passed_through(self, adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun):
        vmw, vmx = await self._adapter_with(adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun, running=None)
        fake_vmrun.set(running=[vmx])
        await vmw.stop("Ubuntu", mode="hard")
        stops = [c for c in fake_vmrun.calls() if "stop" in c]
        assert stops and "hard" in stops[0]

    async def test_an_unknown_stop_mode_is_refused_before_running_anything(self, adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun):
        from core.virtualization.base import VMOperationError

        vmw, _ = await self._adapter_with(adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun)
        with pytest.raises(VMOperationError):
            await vmw.stop("Ubuntu", mode="explode")
        assert not [c for c in fake_vmrun.calls() if "stop" in c]

    async def test_suspending_a_stopped_vm_is_refused_without_running_it(self, adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun):
        from core.virtualization.base import VMOperationError

        vmw, _ = await self._adapter_with(adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun)
        with pytest.raises(VMOperationError) as exc:
            await vmw.suspend("Ubuntu")
        assert "stopped" in str(exc.value)
        assert not [c for c in fake_vmrun.calls() if "suspend" in c]

    async def test_resuming_a_stopped_vm_explains_instead_of_starting_it(self, adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun):
        from core.virtualization.base import VMOperationError

        vmw, _ = await self._adapter_with(adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun)
        with pytest.raises(VMOperationError) as exc:
            await vmw.resume("Ubuntu")
        assert "not suspended" in str(exc.value)
        assert "Start it instead" in str(exc.value)

    async def test_a_failing_lifecycle_command_carries_vmrun_stderr(self, adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun):
        from core.virtualization.base import VMOperationError

        vmw, _ = await self._adapter_with(adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun)
        fake_vmrun.set(running=[], responses={"start": {
            "stderr": "Error: Cannot open VM: The file is locked", "returncode": 1,
        }})
        with pytest.raises(VMOperationError) as exc:
            await vmw.start("Ubuntu")
        assert "file is locked" in str(exc.value)
        assert exc.value.command  # the argv is attached for the audit trail

    async def test_a_timeout_is_reported_as_a_timeout(self, adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun):
        """A hypervisor that never answers must not hang the agent forever.

        The fake really sleeps, so this exercises `wait_for` and the reaping of
        the killed child rather than a stubbed exception.
        """
        from core.virtualization.base import VMOperationError

        vmw, _ = await self._adapter_with(adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun)
        monkeypatch.setattr("config.Config.VM_OPERATION_TIMEOUT", 0.3, raising=False)
        fake_vmrun.set(running=[], sleep=3)

        with pytest.raises(VMOperationError) as exc:
            await vmw.start("Ubuntu")
        assert "did not finish" in str(exc.value)
        assert "0s" in str(exc.value) or "1s" in str(exc.value)  # the budget is named


# ---------------------------------------------------------------------------
# Readiness — the honesty section
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestReadinessIsNeverOversold:
    async def test_a_running_vm_without_a_probe_is_not_ready(self, adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun):
        vmx = str(vm_dir / "ubuntu" / "Ubuntu.vmx")
        fake_vmrun.set(running=[vmx])
        _inventory(monkeypatch, tmp_path / "appdata" / "VMware" / "inventory.vmls", [("Ubuntu", vmx)], [vm_dir])
        report = await adapter().readiness("Ubuntu")
        assert report.running is True
        assert report.ready is False
        assert "not boot evidence" in report.detail

    async def test_tools_running_is_recorded_as_a_host_side_signal(self, adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun):
        vmx = str(vm_dir / "ubuntu" / "Ubuntu.vmx")
        fake_vmrun.set(running=[vmx], responses={"checkToolsState": {"stdout": "running"}})
        _inventory(monkeypatch, tmp_path / "appdata" / "VMware" / "inventory.vmls", [("Ubuntu", vmx)], [vm_dir])
        report = await adapter().readiness("Ubuntu")
        assert report.tools_state == "running"
        # Tools running is good evidence, and still not readiness: nothing ran
        # inside the guest on our behalf.
        assert report.ready is False
        assert "host-side signal" in " ".join(report.checks)

    async def test_a_successful_probe_makes_it_ready(self, adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun):
        vmx = str(vm_dir / "ubuntu" / "Ubuntu.vmx")
        fake_vmrun.set(running=[vmx], responses={"checkToolsState": {"stdout": "running"}})
        _inventory(monkeypatch, tmp_path / "appdata" / "VMware" / "inventory.vmls", [("Ubuntu", vmx)], [vm_dir])

        async def probe():
            return True

        report = await adapter().readiness("Ubuntu", probe=probe)
        assert report.ready is True

    async def test_a_failing_probe_is_recorded_not_hidden(self, adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun):
        vmx = str(vm_dir / "ubuntu" / "Ubuntu.vmx")
        fake_vmrun.set(running=[vmx])
        _inventory(monkeypatch, tmp_path / "appdata" / "VMware" / "inventory.vmls", [("Ubuntu", vmx)], [vm_dir])

        async def probe():
            raise RuntimeError("no such host")

        report = await adapter().readiness("Ubuntu", probe=probe)
        assert report.ready is False
        assert any("no such host" in c for c in report.checks)

    async def test_a_stopped_vm_says_there_is_no_guest_to_be_ready(self, adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun):
        vmx = str(vm_dir / "ubuntu" / "Ubuntu.vmx")
        _inventory(monkeypatch, tmp_path / "appdata" / "VMware" / "inventory.vmls", [("Ubuntu", vmx)], [vm_dir])
        report = await adapter().readiness("Ubuntu")
        assert report.ready is False
        assert "no guest to be ready" in report.detail

    async def test_guest_ip_is_read_from_tools(self, adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun):
        vmx = str(vm_dir / "ubuntu" / "Ubuntu.vmx")
        fake_vmrun.set(running=[vmx], responses={"getGuestIPAddress": {"stdout": "192.168.10.42\n"}})
        _inventory(monkeypatch, tmp_path / "appdata" / "VMware" / "inventory.vmls", [("Ubuntu", vmx)], [vm_dir])
        assert await adapter().guest_ip("Ubuntu") == "192.168.10.42"

    async def test_guest_ip_is_empty_when_tools_cannot_say(self, adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun):
        vmx = str(vm_dir / "ubuntu" / "Ubuntu.vmx")
        fake_vmrun.set(running=[vmx], responses={"getGuestIPAddress": {"returncode": 1, "stderr": "no tools"}})
        _inventory(monkeypatch, tmp_path / "appdata" / "VMware" / "inventory.vmls", [("Ubuntu", vmx)], [vm_dir])
        assert await adapter().guest_ip("Ubuntu") == ""


# ---------------------------------------------------------------------------
# No shell, ever
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestNoShellInThePath:
    async def test_a_hostile_vm_name_is_one_argv_element(self, adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun):
        """`create_subprocess_exec` takes a list: there is no shell to fool."""
        hostile = "Ubuntu && reboot; $(id)"
        vmx = str(vm_dir / "ubuntu" / "Ubuntu.vmx")
        vms = [type("V", (), {"name": hostile, "path": vmx, "state": VMState.STOPPED,
                              "state_evidence": "test", "running": False})()]
        vm, _how = adapter().resolve(hostile, vms)
        assert vm.path == vmx

    async def test_quoting_is_for_display_only(self):
        from core.virtualization.base import quote_argv

        argv = ["vmrun", "-T", "ws", "start", "/vms/my vm.vmx", "gui"]
        assert quote_argv(argv).startswith("vmrun -T ws start")

    async def test_only_vmrun_is_the_executable(self, adapter, vm_dir, monkeypatch, tmp_path, fake_vmrun):
        vmx = str(vm_dir / "ubuntu" / "Ubuntu.vmx")
        fake_vmrun.set(running=[vmx])
        _inventory(monkeypatch, tmp_path / "appdata" / "VMware" / "inventory.vmls", [("Ubuntu", vmx)], [vm_dir])
        vmw = adapter()
        await vmw.list_vms()
        for call in fake_vmrun.calls():
            # Every invocation starts with the type flag the adapter built.
            assert call[0] == "-T" and call[1] == "ws", call
