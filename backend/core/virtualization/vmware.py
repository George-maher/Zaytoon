"""VMware Workstation / Player, driven through `vmrun`.

Everything VMware-specific lives here. The tool layer above imports this module
and nothing else, so a second hypervisor later is a sibling file rather than a
branch in every tool.

What this adapter will not do
-----------------------------
It will not guess which VM you meant, and it will not guess what state a VM is
in. Both are the difference between an assistant that safely does what you asked
and one that powers on the wrong lab machine and tells you it is ready.

* **Name resolution refuses ambiguity.** An exact name match wins. A *unique*
  substring match is accepted and the report says that is what happened. Two or
  more candidates is an error listing them — never a silent pick of the first,
  which is how "start Ubuntu" ends up booting `Ubuntu-16.04-OLD`.
* **State comes from an observation.** `vmrun list` for running; a `.vmss`
  sibling for suspended; the file's absence for unavailable. If `vmrun` is not
  installed the state is ``unknown``, because without the CLI there is no
  evidence — and calling that "stopped" would be a fabrication.
* **Opening a window is not booting.** `start(gui=True)` reports
  ``running`` (that *is* what vmrun tells us) and separately says that the
  guest is not thereby known to be up. Readiness is
  :meth:`VMwareWorkstation.readiness`, which asks the guest.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from core.virtualization.base import (
    CommandOutcome,
    Hypervisor,
    HypervisorProgram,
    VMInfo,
    VMOperationError,
    VMState,
    looks_like_vmx,
    resolve_program,
    run_command,
)

logger = logging.getLogger("Zaytoon.Virtualization.VMware")

#: Executable basenames. `vmplayer` is a different UI over the same `vmrun`.
_VMRUN_NAMES = ("vmrun", "vmrun.exe")
_UI_NAMES = ("vmware", "vmware.exe", "vmplayer", "vmplayer.exe", "vmware-workstation")

#: Directories probed when the program is not on `PATH`. These are *candidates*,
#: not assumptions: each one is stat-ed and must be a regular executable file.
#: A hardcoded single path is how a tool "works" on one machine and lies on
#: every other.
_WINDOWS_DIRS = (
    r"C:\Program Files (x86)\VMware\VMware Workstation",
    r"C:\Program Files\VMware\VMware Workstation",
    r"C:\Program Files (x86)\VMware\VMware Player",
    r"C:\Program Files\VMware\VMware Player",
)
_POSIX_DIRS = (
    "/usr/bin",
    "/usr/local/bin",
    "/usr/lib/vmware/bin",
    "/opt/vmware/bin",
)
_MACOS_DIRS = (
    "/Applications/VMware Fusion.app/Contents/Library",
    "/Applications/VMware Fusion.app/Contents/Public",
    "/Applications/VMware Fusion.app/Contents/MacOS",
)

#: `vmrun list` prints a count line and then one absolute path per running VM.
_RUNNING_HEADER = re.compile(r"^total\s+running\s+vms\s*:\s*(\d+)\s*$", re.IGNORECASE)

#: `inventory.vmls` is a flat keyed file:
#:     vmlist1.displayName = "Ubuntu 24.04"
#:     vmlist1.config = "C:\VMs\Ubuntu\Ubuntu.vmx"
#: The index pairs a display name with its config path, so names come from the
#: hypervisor's own records rather than from directory names.
_INV_ENTRY = re.compile(
    r'^vmlist(?P<index>\d+)\.(?P<key>displayName|config)\s*=\s*"(?P<value>.*)"\s*$'
)

_STATE_CONFIRM_DELAY = 0.0  #: overridden in tests to keep them fast

#: The `-T` flag tells vmrun which product it is talking to. Wrong values fail
#: loudly ("Invalid type"), which is the outcome we want: a silent retry with a
#: different type would be the adapter guessing at the host's installation.
DEFAULT_VM_TYPE = "ws"
VALID_VM_TYPES = ("ws", "player", "fusion", "server")


@dataclass
class Readiness:
    """What is actually known about a running VM's guest.

    Cars this deliberately keeps apart:

    * ``tools_state`` — what `vmrun checkToolsState` said (`running`,
      `installed`, `notInstalled`, `unknown`). A host-side signal that VMware
      Tools is alive inside the guest, which is good evidence the guest OS
      booted. It is *not* evidence that a network exists or that SSH is up.
    * ``guest_reachable`` — an authenticated command succeeded inside the guest.
      Only this proves the guest can execute something for us.

    ``ready`` is true only on the second. Reporting the first as readiness is
    exactly the mistake the brief calls out.
    """

    vm: str
    running: bool
    tools_state: str = "unknown"
    guest_reachable: bool = False
    detail: str = ""
    checks: List[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.checks is None:
            self.checks = []

    @property
    def ready(self) -> bool:
        return self.running and self.guest_reachable

    def to_dict(self) -> Dict[str, Any]:
        return {
            "vm": self.vm,
            "running": self.running,
            "tools_state": self.tools_state,
            "guest_reachable": self.guest_reachable,
            "ready": self.ready,
            "detail": self.detail,
            "checks": list(self.checks or []),
        }


def search_dirs() -> Tuple[str, ...]:
    """The install directories worth probing on this platform."""
    if os.name == "nt":
        return _WINDOWS_DIRS
    if sys.platform == "darwin":
        return _MACOS_DIRS
    return _POSIX_DIRS


def _inventory_candidates() -> List[str]:
    """Where VMware keeps its list of known VMs, most specific first."""
    out: List[str] = []
    appdata = os.getenv("APPDATA")
    if appdata:
        out.append(os.path.join(appdata, "VMware", "inventory.vmls"))
    home = os.path.expanduser("~")
    if home and home != "~":
        out.append(os.path.join(home, ".vmware", "inventory.vmls"))
        # Documents\Virtual Machines is the default VM directory on Windows;
        # ~/vmware is the Linux convention. Both are only *scanned* — an absent
        # directory is simply not a source.
        out.append(os.path.join(home, "Documents", "Virtual Machines"))
        out.append(os.path.join(home, "vmware"))
    return out


class VMwareWorkstation(Hypervisor):
    """Adapter for VMware Workstation / Player / Fusion."""

    name = "vmware"

    def __init__(
        self,
        *,
        vmrun: Optional[HypervisorProgram] = None,
        ui: Optional[HypervisorProgram] = None,
        vm_type: str = DEFAULT_VM_TYPE,
        display_name: str = "",
        why_unavailable: str = "",
    ):
        # Discovery is injectable so tests never depend on the host's VMware
        # installation, and so a caller can hand in a validator's own result.
        self._vmrun = vmrun
        self._ui = ui
        self._vm_type = vm_type if vm_type in VALID_VM_TYPES else DEFAULT_VM_TYPE
        self._display_name = display_name
        self._why = why_unavailable
        self._discovered = vmrun is not None

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------
    @classmethod
    def discover(cls, cfg: Any = None) -> "VMwareWorkstation":
        """Locate VMware on this host, or record precisely why not.

        Never raises: "VMware is not installed" is a normal state that the tool
        layer reports as such, not an exception to be caught and re-worded by
        whoever happened to call first.
        """
        from config import Config

        cfg = cfg or Config
        dirs = [d for d in str(getattr(cfg, "VMWARE_BIN_DIRS", "") or "").split(os.pathsep) if d]
        dirs.extend(search_dirs())

        vmrun = resolve_program(
            _VMRUN_NAMES, dirs, explicit=str(getattr(cfg, "VMWARE_VMRUN", "") or ""), roles=("vmrun",)
        )
        if vmrun is None:
            reason = (
                "`vmrun` was not found on PATH or in the usual VMware install "
                "directories. VMware Workstation/Player/Fusion does not appear to be "
                "installed, or its bin directory is not on PATH. Set VMWARE_VMRUN to "
                "the full path of vmrun to point at a specific installation."
            )
            logger.info("VMware unavailable: %s", reason)
            return cls(why_unavailable=reason)

        ui = resolve_program(
            _UI_NAMES, dirs, explicit=str(getattr(cfg, "VMWARE_UI", "") or ""), roles=("ui",)
        )
        return cls(vmrun=vmrun, ui=ui)

    @property
    def vmrun(self) -> Optional[HypervisorProgram]:
        return self._vmrun

    @property
    def ui(self) -> Optional[HypervisorProgram]:
        return self._ui

    async def available(self) -> bool:
        return self._vmrun is not None

    async def why_unavailable(self) -> str:
        if self._vmrun is not None:
            return ""
        return self._why or "VMware was not found on this host."

    def _require_vmrun(self) -> HypervisorProgram:
        if self._vmrun is None:
            raise VMOperationError(self._why or "VMware is not available on this host.")
        return self._vmrun

    # ------------------------------------------------------------------
    # CLI plumbing
    # ------------------------------------------------------------------
    def _argv(self, *args: str) -> List[str]:
        program = self._require_vmrun()
        # `-T ws` before the verb, which is where vmrun wants it.
        return [program.path, "-T", self._vm_type, *[str(a) for a in args]]

    async def _run(self, *args: str, timeout: float = 60.0) -> CommandOutcome:
        return await run_command(self._argv(*args), timeout=timeout)

    async def _run_checked(self, *args: str, timeout: float = 60.0) -> CommandOutcome:
        """Run a lifecycle verb, raising with the CLI's own output on failure."""
        outcome = await self._run(*args, timeout=timeout)
        if outcome.timed_out:
            raise VMOperationError(
                f"`vmrun {args[0] if args else ''}` did not finish within {timeout:.0f}s.",
                command=outcome.argv,
                output=outcome.output,
            )
        if not outcome.ok:
            raise VMOperationError(
                f"`vmrun {args[0] if args else ''}` failed (exit {outcome.returncode}): "
                f"{outcome.output or 'no output'}",
                command=outcome.argv,
                output=outcome.output,
            )
        return outcome

    # ------------------------------------------------------------------
    # Inventory
    # ------------------------------------------------------------------
    async def _running_paths(self) -> Tuple[Optional[set], CommandOutcome]:
        """Paths `vmrun list` reports as running, and the raw outcome.

        ``None`` for the set means the question could not be answered — the
        caller must then say ``unknown`` rather than ``stopped``.
        """
        outcome = await self._run("list", timeout=30.0)
        if not outcome.ok:
            return None, outcome
        paths: set = set()
        for line in outcome.stdout.splitlines():
            line = line.strip()
            if not line or _RUNNING_HEADER.match(line):
                continue
            # `vmrun list` sometimes annotates the path; the path itself is
            # everything up to the `.vmx`.
            idx = line.lower().find(".vmx")
            if idx != -1:
                paths.add(os.path.normcase(os.path.abspath(line[: idx + 4])))
                continue
            paths.add(os.path.normcase(os.path.abspath(line)))
        return paths, outcome

    def _inventory_entries(self) -> List[Tuple[str, str]]:
        """``(display_name, vmx_path)`` from VMware's own inventory file."""
        entries: List[Tuple[str, str]] = []
        for candidate in _inventory_candidates():
            if not os.path.isfile(candidate) or looks_like_vmx(candidate):
                continue
            try:
                with open(candidate, "r", encoding="utf-8", errors="replace") as handle:
                    text = handle.read()
            except OSError as exc:
                logger.debug("Could not read %s: %s", candidate, exc)
                continue
            found: Dict[str, Dict[str, str]] = {}
            for line in text.splitlines():
                match = _INV_ENTRY.match(line.strip())
                if not match:
                    continue
                slot = found.setdefault(match.group("index"), {})
                # Windows paths inside the file use backslashes regardless of
                # the host we are parsing on.
                slot[match.group("key")] = match.group("value").replace("\\\\", "\\")
            for slot in found.values():
                path = slot.get("config", "")
                if path and looks_like_vmx(path):
                    entries.append((slot.get("displayName", ""), path))
        return entries

    def _scan_dirs(self) -> List[str]:
        """Directories to scan for `*.vmx`, from config then the conventions."""
        from config import Config

        configured = [
            d for d in str(getattr(Config, "VM_INVENTORY_DIRS", "") or "").split(os.pathsep) if d
        ]
        if not configured:
            configured = [c for c in _inventory_candidates() if not c.endswith(".vmls")]
        return configured

    def _scan_entries(self, max_depth: int = 3) -> List[Tuple[str, str]]:
        """`*.vmx` files under the scan roots, bounded in depth.

        Bounded because walking a whole filesystem to answer "what VMs do you
        have?" would be both slow and a privacy problem: it would read paths far
        outside anything the user pointed us at.
        """
        out: List[Tuple[str, str]] = []
        for root in self._scan_dirs():
            if not os.path.isdir(root):
                continue
            base_depth = root.rstrip(os.sep).count(os.sep)
            for dirpath, dirnames, filenames in os.walk(root):
                if dirpath.count(os.sep) - base_depth >= max_depth:
                    dirnames[:] = []
                # Never descend into a running VM's lock tree.
                dirnames[:] = [d for d in dirnames if not d.endswith(".lck")]
                for filename in filenames:
                    if filename.lower().endswith(".vmx"):
                        out.append((os.path.splitext(filename)[0], os.path.join(dirpath, filename)))
        return out

    async def list_vms(self) -> List[VMInfo]:
        """Every VM this host knows about, each with the evidence for its state."""
        running, list_outcome = await self._running_paths()

        merged: Dict[str, Tuple[str, str, str]] = {}

        def remember(name: str, path: str, source: str) -> None:
            key = os.path.normcase(os.path.abspath(path))
            existing = merged.get(key)
            # An inventory display name beats a filename, and either beats the
            # bare path `vmrun list` gives (which carries no name at all).
            if existing is None:
                merged[key] = (name, path, source)
            elif not existing[0] and name:
                merged[key] = (name, path, existing[2])
            elif source == "inventory" and existing[2] != "inventory":
                merged[key] = (name or existing[0], path, "inventory")

        if running:
            for path in running:
                remember("", path, "running")
        for name, path in self._inventory_entries():
            remember(name, path, "inventory")
        for name, path in self._scan_entries():
            remember(name, path, "scan")

        vms: List[VMInfo] = []
        for key, (name, path, source) in sorted(merged.items(), key=lambda kv: kv[1][0].lower()):
            vms.append(self._classify(name, path, source, running, list_outcome))
        return vms

    def _classify(
        self,
        name: str,
        path: str,
        source: str,
        running: Optional[set],
        list_outcome: CommandOutcome,
    ) -> VMInfo:
        """Decide a VM's state from observations only, and record which."""
        display = name or os.path.splitext(os.path.basename(path))[0]
        norm = os.path.normcase(os.path.abspath(path))

        if not os.path.isfile(path):
            return VMInfo(
                name=display,
                path=path,
                state=VMState.UNAVAILABLE,
                state_evidence=f"the definition file was not found at {path}",
                source=source,
            )

        if running is None:
            return VMInfo(
                name=display,
                path=path,
                state=VMState.UNKNOWN,
                state_evidence=(
                    "`vmrun list` could not be run, so the state was not determined"
                    + (f" ({list_outcome.stderr.strip()})" if list_outcome.stderr.strip() else "")
                ),
                source=source,
            )

        if norm in running:
            return VMInfo(
                name=display,
                path=path,
                state=VMState.RUNNING,
                state_evidence="listed by `vmrun list`",
                source=source,
                guest_os=self._guest_os_hint(path),
                extra=self._lock_hint(path),
            )

        suspension = os.path.splitext(path)[0] + ".vmss"
        if os.path.isfile(suspension):
            return VMInfo(
                name=display,
                path=path,
                state=VMState.SUSPENDED,
                state_evidence=f"suspension state file present at {suspension}",
                source=source,
                guest_os=self._guest_os_hint(path),
            )

        return VMInfo(
            name=display,
            path=path,
            state=VMState.STOPPED,
            state_evidence="not listed by `vmrun list`, and no `.vmss` suspension file",
            source=source,
            guest_os=self._guest_os_hint(path),
            extra=self._lock_hint(path),
        )

    @staticmethod
    def _lock_hint(path: str) -> Dict[str, Any]:
        """A `.lck` directory means *someone* has it open — reported as a hint.

        It is not used to decide the state: a stale lock left by a crash looks
        identical to a live one, so promoting it to "running" would invent
        information. It is worth surfacing because it explains a later failure.
        """
        lock = path + ".lck"
        if os.path.isdir(lock):
            return {"lock_present": lock}
        return {}

    @staticmethod
    def _guest_os_hint(path: str) -> str:
        """`guestOS` out of the `.vmx`, or empty. Never guessed from the name."""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.strip().lower().startswith("guestos"):
                        _, _, value = line.partition("=")
                        return value.strip().strip('"')
                    # guestOS sits near the top; stop after the first block so a
                    # large .vmx is not read in full for one field.
                    if line.strip().startswith("memsize"):
                        break
        except OSError:
            return ""
        return ""

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------
    def resolve(self, reference: str, vms: Sequence[VMInfo]) -> Tuple[VMInfo, str]:
        """Turn a user's words into exactly one VM, or refuse.

        Returns ``(vm, how)`` where ``how`` explains the match so a report can
        say "matched 'Ubuntu' to 'Ubuntu 24.04 LTS'" rather than implying the
        user named it exactly.

        Raises :class:`VMOperationError` when the answer is not unique, listing
        the candidates. There is no "closest match" branch here by design.
        """
        ref = (reference or "").strip()
        if not ref:
            raise VMOperationError("No virtual machine was named.")

        # An explicit path is unambiguous, and only accepted if it is a VM.
        if os.path.sep in ref or (os.path.altsep and os.path.altsep in ref) or looks_like_vmx(ref):
            if not looks_like_vmx(ref):
                raise VMOperationError(
                    f"'{ref}' looks like a path but not a virtual machine definition "
                    f"(.vmx). Give a VM name or the full path to its .vmx file."
                )
            norm = os.path.normcase(os.path.abspath(ref))
            for vm in vms:
                if os.path.normcase(os.path.abspath(vm.path)) == norm:
                    return vm, "matched by path"
            if not os.path.isfile(ref):
                raise VMOperationError(
                    f"No virtual machine definition exists at '{ref}'."
                )
            info = self._classify("", ref, "path", None, CommandOutcome([], 0))
            return info, "matched by path"

        lowered = ref.lower()
        exact = [vm for vm in vms if vm.name.lower() == lowered]
        if len(exact) == 1:
            return exact[0], "matched by exact name"
        if len(exact) > 1:
            raise VMOperationError(
                "More than one virtual machine is named "
                f"'{ref}': {self._describe(exact)}. Name one by its full path."
            )

        # A VM whose .vmx stem matches is an exact match in every practical
        # sense — that is the name VMware shows and the one users type.
        stems = [
            vm for vm in vms
            if os.path.splitext(os.path.basename(vm.path))[0].lower() == lowered
        ]
        if len(stems) == 1:
            return stems[0], "matched by its .vmx file name"
        if len(stems) > 1:
            raise VMOperationError(
                f"More than one virtual machine file is named '{ref}': "
                f"{self._describe(stems)}. Name one by its full path."
            )

        partial = [vm for vm in vms if lowered in vm.name.lower()]
        if len(partial) == 1:
            return partial[0], f"matched '{ref}' to the single VM '{partial[0].name}'"
        if len(partial) > 1:
            raise VMOperationError(
                f"'{ref}' matches {len(partial)} virtual machines and picking one "
                f"would be a guess: {self._describe(partial)}. Use the exact name "
                f"or the full path to the .vmx file."
            )

        if not vms:
            raise VMOperationError(
                f"No virtual machines were found on this host, so '{ref}' could not "
                f"be matched. Check VM_INVENTORY_DIRS if they live outside the usual "
                f"locations."
            )
        raise VMOperationError(
            f"No virtual machine matches '{ref}'. Known VMs: {self._describe(vms)}."
        )

    @staticmethod
    def _describe(vms: Iterable[VMInfo]) -> str:
        return "; ".join(f"'{vm.name}' ({vm.state.value})" for vm in vms)

    async def _resolve_live(self, reference: str) -> Tuple[VMInfo, str]:
        return self.resolve(reference, await self.list_vms())

    async def status(self, vm_ref: str) -> VMInfo:
        vm, _how = await self._resolve_live(vm_ref)
        return vm

    async def resolve_and_describe(self, vm_ref: str) -> Tuple[VMInfo, str]:
        return await self._resolve_live(vm_ref)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def _state_after(self, vm: VMInfo) -> VMInfo:
        """Re-read the state rather than assuming the verb worked.

        `vmrun start` returning 0 means the request was accepted. What the VM is
        doing *now* is a separate question, and it is the one being asked.
        """
        try:
            vms = await self.list_vms()
        except VMOperationError:
            return vm
        norm = os.path.normcase(os.path.abspath(vm.path))
        for candidate in vms:
            if os.path.normcase(os.path.abspath(candidate.path)) == norm:
                return candidate
        return vm

    async def start(self, vm_ref: str, *, gui: bool = True) -> VMInfo:
        vm, _how = await self._resolve_live(vm_ref)
        if vm.state is VMState.RUNNING:
            # Not an error: asking for what is already true is reasonable, and
            # starting it again would be a second power-on request.
            return vm
        if not os.path.isfile(vm.path):
            raise VMOperationError(
                f"Cannot start '{vm.name}': its definition file is missing ({vm.path})."
            )
        from config import Config

        await self._run_checked(
            "start", vm.path, "gui" if gui else "nogui",
            timeout=float(getattr(Config, "VM_OPERATION_TIMEOUT", 60.0)),
        )
        return await self._state_after(vm)

    async def stop(self, vm_ref: str, *, mode: str = "soft") -> VMInfo:
        if mode not in ("soft", "hard"):
            raise VMOperationError(f"Unknown stop mode '{mode}': use 'soft' or 'hard'.")
        vm, _how = await self._resolve_live(vm_ref)
        if vm.state is not VMState.RUNNING:
            return vm
        from config import Config

        await self._run_checked(
            "stop", vm.path, mode,
            timeout=float(getattr(Config, "VM_OPERATION_TIMEOUT", 60.0)),
        )
        return await self._state_after(vm)

    async def suspend(self, vm_ref: str) -> VMInfo:
        vm, _how = await self._resolve_live(vm_ref)
        if not vm.running:
            raise VMOperationError(
                f"Cannot suspend '{vm.name}': it is {vm.state.value} "
                f"({vm.state_evidence})."
            )
        await self._run_checked("suspend", vm.path)
        return await self._state_after(vm)

    async def resume(self, vm_ref: str) -> VMInfo:
        vm, _how = await self._resolve_live(vm_ref)
        if vm.state is VMState.RUNNING:
            return vm
        if vm.state is not VMState.SUSPENDED:
            raise VMOperationError(
                f"Cannot resume '{vm.name}': it is not suspended, it is "
                f"{vm.state.value} ({vm.state_evidence}). Start it instead."
            )
        await self._run_checked("start", vm.path, "gui")
        return await self._state_after(vm)

    async def open_console(self, vm_ref: str) -> str:
        """Bring up the VMware UI on this VM.

        Uses `vmrun start gui` when the VM is not running — that is the
        supported way to get a console, and it is idempotent for a running VM.
        Falls back to launching the UI binary with the `.vmx` only when `vmrun`
        is unavailable, and says which route was taken so the caller can report
        it instead of asserting "the window opened".
        """
        vm, _how = await self._resolve_live(vm_ref)
        if vm.state is VMState.RUNNING:
            if self._ui is None:
                return (
                    f"'{vm.name}' is running, but the VMware UI executable was not "
                    f"found, so a console could not be opened."
                )
            await run_command([self._ui.path, vm.path], timeout=20.0)
            return f"Asked the VMware UI to show '{vm.name}' ({self._ui.path})."
        if self._vmrun is None:
            raise VMOperationError(await self.why_unavailable())
        from config import Config

        await self._run_checked(
            "start", vm.path, "gui",
            timeout=float(getattr(Config, "VM_OPERATION_TIMEOUT", 60.0)),
        )
        return (
            f"Started '{vm.name}' with a GUI console. The window appearing is not "
            f"evidence that the guest has finished booting — use vm_guest_check."
        )

    # ------------------------------------------------------------------
    # Readiness
    # ------------------------------------------------------------------
    async def tools_state(self, vm_ref: str) -> str:
        """`vmrun checkToolsState` — a host-side signal, not a readiness claim."""
        vm, _how = await self._resolve_live(vm_ref)
        outcome = await self._run("checkToolsState", vm.path, timeout=30.0)
        if not outcome.ok:
            return "unknown"
        text = outcome.stdout.strip().lower()
        for known in ("running", "installed", "notinstalled", "unknown"):
            if known in text:
                return {"notinstalled": "notInstalled"}.get(known, known)
        return "unknown"

    async def guest_ip(self, vm_ref: str, *, wait: bool = False) -> str:
        """The guest's IP as reported by VMware Tools, or ``""``.

        Asks ``vmrun getGuestIPAddress``, which requires Tools to be running in
        the guest. An empty string means Tools could not tell us — and that must
        be reported as "unknown", never filled in with a guess like the first
        address on the subnet. ``wait`` lets Tools block until it has an address,
        which is useful immediately after a boot.
        """
        vm, _how = await self._resolve_live(vm_ref)
        args = ["getGuestIPAddress", vm.path]
        if wait:
            args.append("-wait")
        outcome = await self._run(*args, timeout=60.0)
        if not outcome.ok:
            return ""
        # Tools prints the address, sometimes with a trailing note.
        for token in outcome.stdout.replace("\n", " ").split():
            if token.count(".") == 3 and all(part.isdigit() for part in token.split(".")):
                return token
        return outcome.stdout.strip().split("\n")[0].strip() if outcome.stdout.strip() else ""

    async def readiness(self, vm_ref: str, *, probe=None) -> Readiness:
        """Collect every signal that can be checked without touching the guest.

        ``probe`` is an awaitable returning ``True`` when an authenticated guest
        command succeeded — supplied by the guest channel so this module stays
        free of SSH details. Without it, ``ready`` is false however healthy the
        VM looks, because nothing has confirmed anything inside the guest.
        """
        vm, how = await self._resolve_live(vm_ref)
        report = Readiness(vm=vm.name, running=vm.running, detail=how)
        report.checks.append(f"state: {vm.state.value} ({vm.state_evidence})")

        if not vm.running:
            report.detail = (
                f"'{vm.name}' is {vm.state.value} ({vm.state_evidence}), so there is "
                f"no guest to be ready."
            )
            return report

        tools = await self.tools_state(vm.path)
        report.tools_state = tools
        if tools == "running":
            report.checks.append(
                "VMware Tools reports running: the guest OS has booted (host-side signal)"
            )
        elif tools == "installed":
            report.checks.append(
                "VMware Tools is installed but not running — the guest may still be "
                "booting, or Tools is stopped inside it"
            )
        elif tools == "notInstalled":
            report.checks.append(
                "VMware Tools is not installed in this guest, so Tools cannot be used "
                "as a boot signal"
            )
        else:
            report.checks.append("VMware Tools state could not be read")

        if probe is not None:
            try:
                reachable = await probe()
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                report.checks.append(f"guest command failed: {exc}")
                reachable = False
            report.guest_reachable = bool(reachable)
            report.checks.append(
                "guest answered an authenticated command"
                if reachable
                else "guest did not answer an authenticated command"
            )

        if report.ready:
            report.detail = f"'{vm.name}' is running and its guest answered a command."
        elif not report.guest_reachable:
            report.detail = (
                f"'{vm.name}' is running, but nothing has confirmed the guest can run "
                f"anything. A running VM process and an open console window are not "
                f"boot evidence; configure the guest SSH channel (VM_GUEST_USER, "
                f"VM_GUEST_IDENTITY) to establish one."
            )
        return report
