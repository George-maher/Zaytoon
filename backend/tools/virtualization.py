"""Virtual machine tools: VMware discovery, lifecycle, and guest execution.

These are ordinary tools. They go through the same registry, the same
permission tiers, the same `ConfirmationManager` and the same audit log as
`file_write` — there is no separate privileged path for virtual machines, and a
skill cannot reach one.

Tiers, and why each is where it is
----------------------------------
``vm_list`` / ``vm_status``      safe       Read-only. Starting no VM and
                                           contacting no guest, so a prompt
                                           would be noise.
``vm_start`` / ``vm_open_console`` confirm   Powers a machine on. It consumes
                                           host resources and boots an OS, so
                                           the user says yes first.
``vm_suspend`` / ``vm_resume``   confirm    Changes a machine's state.
``vm_stop``                      confirm    Escalates to **dangerous** for
                                           ``hard``: a hard power-off discards
                                           whatever the guest has not written.
``vm_guest_exec``                confirm    Runs a command inside another OS.
``vm_guest_check``               confirm    Runs a command inside another OS
                                           (``true``) to prove readiness.

The four scopes the brief asks to keep distinct stay distinct: ``vm.discover``,
``vm.manage``, ``vm.guest.exec``, and desktop automation's
``browser.control``/``system.modify`` are separate named permissions, so
consent for one is not consent for another.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from core.virtualization import (
    GuestSession,
    GuestTarget,
    HypervisorNotFound,
    VMOperationError,
    VMState,
    VMwareWorkstation,
)
from core.virtualization.guest import target_from_config
from tools.base import BaseTool, ToolResult

logger = logging.getLogger("Zaytoon.Tools.Virtualization")

#: Injected by tests, and by a future adapter for another hypervisor. Kept as a
#: module-level factory rather than a direct call so that `tools/virtualization`
#: never has to know that VMware exists.
_ADAPTER_FACTORY = None


def adapter() -> VMwareWorkstation:
    """The configured hypervisor adapter for this process."""
    if _ADAPTER_FACTORY is not None:  # pragma: no cover - test seam
        return _ADAPTER_FACTORY()
    return VMwareWorkstation.discover()


def _unavailable(reason: str) -> ToolResult:
    """A failure that is about the host, not about the request.

    Not ``blocked``: nothing refused this, and no approval would change it. The
    distinction matters in the UI, where "blocked" reads as a security decision.
    """
    return ToolResult(
        success=False,
        output="",
        error=(
            f"{reason}\n\nGuest command execution additionally needs a configured "
            f"account and an SSH key for that guest (VM_GUEST_USER, "
            f"VM_GUEST_IDENTITY)."
        ),
    )


class VMList(BaseTool):
    """Discover virtual machines and their states."""

    permission = "safe"

    def name(self) -> str:
        return "vm_list"

    def aliases(self) -> List[str]:
        return ["list_vms", "list_virtual_machines"]

    def description(self) -> str:
        return (
            "List virtual machines known to this host with their current state. "
            "Read-only: starts nothing and contacts no guest."
        )

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "running_only": {
                    "type": "boolean",
                    "description": "Only show VMs that are currently running.",
                }
            },
            "required": [],
            "additionalProperties": False,
        }

    async def execute(self, running_only: bool = False, **_: Any) -> ToolResult:
        vmw = adapter()
        if not await vmw.available():
            return _unavailable(await vmw.why_unavailable())

        vms = await vmw.list_vms()
        if running_only:
            vms = [vm for vm in vms if vm.running]

        if not vms:
            return ToolResult.ok(
                "No virtual machines were found. If they exist, point "
                "VM_INVENTORY_DIRS at the directories holding the .vmx files.",
                {"vms": [], "count": 0, "hypervisor": "vmware"},
            )

        lines = []
        for vm in vms:
            lines.append(f"  {vm.name} — {vm.state.value}  ({vm.state_evidence})")
            lines.append(f"      {vm.path}")
        unknown = [vm.name for vm in vms if vm.state is VMState.UNKNOWN]
        extra = ""
        if unknown:
            extra = (
                f"\n\nState could not be determined for: {', '.join(unknown)}. "
                f"`vmrun list` did not answer, so these are reported as unknown "
                f"rather than stopped."
            )
        return ToolResult.ok(
            f"{len(vms)} virtual machine(s):\n" + "\n".join(lines) + extra,
            {"vms": [vm.to_dict() for vm in vms], "count": len(vms), "hypervisor": "vmware"},
        )


class VMStatus(BaseTool):
    """One VM's state, with the evidence behind it."""

    permission = "safe"

    def name(self) -> str:
        return "vm_status"

    def aliases(self) -> List[str]:
        return ["virtual_machine_status"]

    def description(self) -> str:
        return (
            "Report one virtual machine's state and the observation that determined "
            "it. Read-only."
        )

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "vm": {"type": "string", "description": "VM name, or the full path to its .vmx file."}
            },
            "required": ["vm"],
            "additionalProperties": False,
        }

    async def execute(self, vm: str = "", **_: Any) -> ToolResult:
        vmw = adapter()
        if not await vmw.available():
            return _unavailable(await vmw.why_unavailable())
        info, how = await vmw.resolve_and_describe(vm)
        detail = (
            f"{info.name}: {info.state.value} ({info.state_evidence}). "
            f"Resolved by {how}."
        )
        if info.state is VMState.RUNNING:
            detail += (
                " A running VM process is not evidence that the guest has booted — "
                "use vm_guest_check to establish that."
            )
        if info.guest_os:
            detail += f" The .vmx declares guestOS={info.guest_os}."
        return ToolResult.ok(detail, info.to_dict())


class _VMTool(BaseTool):
    """Shared resolution for the single-VM lifecycle tools."""

    permission = "confirm"

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "vm": {"type": "string", "description": "VM name, or the full path to its .vmx file."}
            },
            "required": ["vm"],
            "additionalProperties": False,
        }

    def risk_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"vm": params.get("vm")}

    async def _resolve(self, vm: str):
        vmw = adapter()
        if not await vmw.available():
            raise HypervisorNotFound(await vmw.why_unavailable())
        return vmw


class VMStart(_VMTool):
    """Power a VM on."""

    def name(self) -> str:
        return "vm_start"

    def aliases(self) -> List[str]:
        return ["start_vm", "start_virtual_machine"]

    def description(self) -> str:
        return (
            "Start a virtual machine, optionally waiting until its guest answers on "
            "the configured SSH channel. Returns the state observed after the "
            "command, which is not the same as the command succeeding."
        )

    def schema(self) -> Dict[str, Any]:
        base = super().schema()
        base["properties"]["wait_for_ready"] = {
            "type": "boolean",
            "description": (
                "Wait until the guest answers a command before returning. Requires "
                "the guest SSH channel to be configured."
            ),
        }
        base["properties"]["gui"] = {
            "type": "boolean",
            "description": "Show the VM's console window (default true).",
        }
        return base

    async def execute(
        self, vm: str = "", wait_for_ready: bool = False, gui: bool = True, **_: Any
    ) -> ToolResult:
        try:
            vmw = await self._resolve(vm)
        except HypervisorNotFound as exc:
            return _unavailable(str(exc))

        try:
            info = await vmw.start(vm, gui=bool(gui))
        except VMOperationError as exc:
            return ToolResult.fail(str(exc), output=str(exc))

        message = f"'{info.name}' is now {info.state.value} ({info.state_evidence})."
        payload: Dict[str, Any] = {"vm": info.to_dict()}

        if wait_for_ready:
            report = await _readiness_for(vmw, info)
            payload["readiness"] = report.to_dict()
            if report.ready:
                message += f"\nThe guest answered a command — it is ready. {report.detail}"
            else:
                return ToolResult(
                    success=False,
                    output=message,
                    error=(
                        f"'{info.name}' was started, but it is not confirmed ready. "
                        f"{report.detail} Checks: {'; '.join(report.checks or [])}"
                    ),
                    data=payload,
                )
        else:
            message += (
                "\nThe guest has not been confirmed to have booted: a running VM "
                "process and a console window are not boot evidence."
            )
        return ToolResult.ok(message, payload)


class VMStop(_VMTool):
    """Stop a VM, softly or by power cut."""

    def name(self) -> str:
        return "vm_stop"

    def aliases(self) -> List[str]:
        return ["stop_vm", "shutdown_vm"]

    def description(self) -> str:
        return (
            "Stop a virtual machine. 'soft' asks the guest to shut down (safe when "
            "it works); 'hard' cuts power and can lose unsaved data."
        )

    def schema(self) -> Dict[str, Any]:
        base = super().schema()
        base["properties"]["mode"] = {
            "type": "string",
            "enum": ["soft", "hard"],
            "description": "soft = guest shutdown request; hard = immediate power off.",
        }
        return base

    def risk_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"vm": params.get("vm"), "mode": params.get("mode", "soft")}

    async def execute(self, vm: str = "", mode: str = "soft", **_: Any) -> ToolResult:
        mode = (mode or "soft").strip().lower()
        if mode not in ("soft", "hard"):
            return ToolResult.fail(f"Unknown stop mode '{mode}': use 'soft' or 'hard'.")
        try:
            vmw = await self._resolve(vm)
        except HypervisorNotFound as exc:
            return _unavailable(str(exc))

        try:
            info = await vmw.stop(vm, mode=mode)
        except VMOperationError as exc:
            return ToolResult.fail(str(exc), output=str(exc))

        note = (
            "A hard power-off can lose anything the guest had not written to disk."
            if mode == "hard"
            else "The guest was asked to shut down."
        )
        return ToolResult.ok(
            f"'{info.name}' is now {info.state.value} ({info.state_evidence}). {note}",
            {"vm": info.to_dict(), "mode": mode},
        )


class VMSuspend(_VMTool):
    """Suspend a running VM, preserving guest memory."""

    def name(self) -> str:
        return "vm_suspend"

    def aliases(self) -> List[str]:
        return ["suspend_vm"]

    def description(self) -> str:
        return "Suspend a running virtual machine, preserving its memory state to disk."

    async def execute(self, vm: str = "", **_: Any) -> ToolResult:
        try:
            vmw = await self._resolve(vm)
        except HypervisorNotFound as exc:
            return _unavailable(str(exc))
        try:
            info = await vmw.suspend(vm)
        except VMOperationError as exc:
            return ToolResult.fail(str(exc), output=str(exc))
        return ToolResult.ok(
            f"'{info.name}' is now {info.state.value} ({info.state_evidence}).",
            {"vm": info.to_dict()},
        )


class VMResume(_VMTool):
    """Resume a suspended VM."""

    def name(self) -> str:
        return "vm_resume"

    def aliases(self) -> List[str]:
        return ["resume_vm"]

    def description(self) -> str:
        return "Resume a suspended virtual machine. Refuses a VM that is not suspended."

    async def execute(self, vm: str = "", **_: Any) -> ToolResult:
        try:
            vmw = await self._resolve(vm)
        except HypervisorNotFound as exc:
            return _unavailable(str(exc))
        try:
            info = await vmw.resume(vm)
        except VMOperationError as exc:
            return ToolResult.fail(str(exc), output=str(exc))
        return ToolResult.ok(
            f"'{info.name}' is now {info.state.value} ({info.state_evidence}).",
            {"vm": info.to_dict()},
        )


class VMOpenConsole(_VMTool):
    """Open a VM's console window."""

    def name(self) -> str:
        return "vm_open_console"

    def aliases(self) -> List[str]:
        return ["open_vm_console", "open_vmware"]

    def description(self) -> str:
        return (
            "Open the console window for a virtual machine, starting it if needed. "
            "Opening a window is not evidence that the guest has booted."
        )

    async def execute(self, vm: str = "", **_: Any) -> ToolResult:
        try:
            vmw = await self._resolve(vm)
        except HypervisorNotFound as exc:
            return _unavailable(str(exc))
        try:
            detail = await vmw.open_console(vm)
        except VMOperationError as exc:
            return ToolResult.fail(str(exc), output=str(exc))
        return ToolResult.ok(detail, {"detail": detail})


# ---------------------------------------------------------------------------
# Guest execution
# ---------------------------------------------------------------------------
async def _readiness_for(vmw: VMwareWorkstation, info) -> Any:
    """Build a readiness report, wiring in the guest probe when configured."""
    target = target_from_config(await _guest_host(vmw, info))
    if target is None:
        return await vmw.readiness(info.path)

    async def probe() -> bool:
        session = GuestSession(target)
        return (await session.check()).ok

    return await vmw.readiness(info.path, probe=probe)


async def _guest_host(vmw: VMwareWorkstation, info) -> str:
    """Where to reach the guest: VMware Tools' reported address, or nothing."""
    if not info.running:
        return ""
    return await vmw.guest_ip(info.path)


def _guest_error(target: Optional[GuestTarget], vm_name: str = "") -> ToolResult:
    """Explain precisely which prerequisite for guest execution is missing."""
    where = f"'{vm_name}'" if vm_name else "the guest"
    if target is None:
        from config import Config

        user = str(getattr(Config, "VM_GUEST_USER", "") or "").strip()
        if not user:
            return ToolResult.fail(
                f"Cannot run commands inside {where}: no guest account is configured. "
                f"Zaytoon can start the VM and open its console, but running commands "
                f"inside it needs an SSH account. Set VM_GUEST_USER to the account in "
                f"the guest, and VM_GUEST_IDENTITY to a private key authorised for it "
                f"(Zaytoon never uses a password)."
            )
        return ToolResult.fail(
            f"Cannot reach {where}: no address is known for it. VMware Tools did not "
            f"report an IP (it may not be installed or running in the guest), and no "
            f"host was given. Pass host= to name the guest explicitly, or check that "
            f"the guest has finished booting and has a network."
        )
    return ToolResult.fail(f"Cannot reach {where} at {target.describe()}.")


class VMGuestCheck(BaseTool):
    """Prove whether a guest can actually execute something."""

    permission = "confirm"

    def name(self) -> str:
        return "vm_guest_check"

    def aliases(self) -> List[str]:
        return ["guest_check", "check_guest_ready"]

    def description(self) -> str:
        return (
            "Check whether a running VM's guest is actually ready, by opening an "
            "authenticated SSH session and running a harmless command in it. This is "
            "the only readiness signal Zaytoon treats as proof."
        )

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "vm": {"type": "string", "description": "VM name (used to find the guest's address)."},
                "host": {
                    "type": "string",
                    "description": "Guest address, if it is known. Overrides discovery.",
                },
                "wait_seconds": {
                    "type": "number",
                    "description": "Poll until ready, up to this many seconds.",
                },
            },
            "required": [],
            "additionalProperties": False,
        }

    def risk_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"vm": params.get("vm"), "host": params.get("host")}

    async def execute(
        self, vm: str = "", host: str = "", wait_seconds: float = 0, **_: Any
    ) -> ToolResult:
        import asyncio

        from config import Config

        # The hypervisor is only needed to *find* the guest. Given an explicit
        # address, this tool is a pure SSH check and works on a host with no
        # VMware installed at all — the same rule `vm_guest_exec` follows. The
        # first version of this required the adapter unconditionally, which
        # meant an address-only check failed with "vmrun was not found".
        vmw = None
        info = None
        resolved_host = (host or "").strip()
        if vm.strip() or not resolved_host:
            vmw = adapter()
            if not await vmw.available():
                return _unavailable(await vmw.why_unavailable())
        if vm.strip():
            try:
                info, _how = await vmw.resolve_and_describe(vm)
            except VMOperationError as exc:
                return ToolResult.fail(str(exc))
            if not resolved_host:
                resolved_host = await _guest_host(vmw, info)

        target = target_from_config(resolved_host)
        if target is None:
            return _guest_error(target, info.name if info else vm)

        session = GuestSession(target)
        budget = float(wait_seconds or 0)
        deadline_seconds = budget if budget > 0 else float(
            getattr(Config, "VM_READY_TIMEOUT", 180.0)
        )
        poll = max(1.0, float(getattr(Config, "VM_READY_POLL", 5.0)))

        result = await session.check()
        waited = 0.0
        # Only poll when the caller asked for it. A check that silently waits
        # three minutes is a check nobody can put in a loop.
        while budget > 0 and not result.ok and waited < deadline_seconds:
            await asyncio.sleep(poll)
            waited += poll
            result = await session.check()

        if result.ok:
            info_out = await session.info()
            return ToolResult.ok(
                f"Guest at {target.describe()} answered an authenticated command — it "
                f"is ready."
                + (f" Reported: {info_out['reported'].splitlines()[0]}" if info_out.get("reported") else ""),
                {
                    "ready": True,
                    "target": target.describe(),
                    "waited_seconds": round(waited, 1),
                    "guest": info_out,
                    "vm": info.to_dict() if info else None,
                },
            )
        return ToolResult(
            success=False,
            output="",
            error=(
                f"Guest at {target.describe()} did not answer: "
                f"{result.error or f'exit code {result.exit_code}'}"
                + (f" (waited {waited:.0f}s)" if waited else "")
            ),
            data={
                "ready": False,
                "target": target.describe(),
                "waited_seconds": round(waited, 1),
                "exit_code": result.exit_code,
            },
        )


class VMGuestExec(BaseTool):
    """Run one command inside a guest."""

    permission = "confirm"

    def name(self) -> str:
        return "vm_guest_exec"

    def aliases(self) -> List[str]:
        return ["guest_exec", "run_in_guest", "vm_run"]

    def description(self) -> str:
        return (
            "Run a command inside a virtual machine's guest over authenticated SSH, "
            "and report its stdout, stderr, exit code and duration. This runs on the "
            "guest, not on this host."
        )

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command to run inside the guest."},
                "vm": {"type": "string", "description": "VM name (used to find the guest's address)."},
                "host": {"type": "string", "description": "Guest address, if known."},
                "timeout": {
                    "type": "number",
                    "description": "Seconds to allow. Defaults to VM_GUEST_TIMEOUT.",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        super().validate(params)
        if not str(params.get("command") or "").strip():
            from tools.base import ToolValidationError

            raise ToolValidationError("vm_guest_exec: 'command' is required")

    def risk_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        # The command itself, so the assessor's pattern table can judge it and
        # escalate a destructive one. Declaring only the VM here would make
        # every guest command look like a lifecycle operation.
        return {"command": params.get("command"), "vm": params.get("vm")}

    async def execute(
        self,
        command: str = "",
        vm: str = "",
        host: str = "",
        timeout: Optional[float] = None,
        **_: Any,
    ) -> ToolResult:
        vmw = adapter()
        info = None
        resolved_host = (host or "").strip()
        if vm.strip():
            if not await vmw.available():
                return _unavailable(await vmw.why_unavailable())
            try:
                info, _how = await vmw.resolve_and_describe(vm)
            except VMOperationError as exc:
                return ToolResult.fail(str(exc))
            if not resolved_host:
                resolved_host = await _guest_host(vmw, info)

        target = target_from_config(resolved_host)
        if target is None:
            return _guest_error(target, info.name if info else vm)

        session = GuestSession(target)
        result = await session.run(command, timeout=timeout)

        payload = result.to_dict()
        payload["vm"] = info.to_dict() if info else None
        payload["where"] = "guest"
        if result.timed_out:
            return ToolResult(
                success=False,
                output=result.stdout,
                error=(
                    f"The command did not finish within its timeout; it may still be "
                    f"running in the guest. Output so far:\n{result.output[:2000]}"
                ),
                data=payload,
            )
        if not result.ok:
            return ToolResult(
                success=False,
                output=result.stdout,
                error=(
                    f"Exit code {result.exit_code} in the guest. "
                    f"{result.error or (result.stderr.strip()[:2000])}"
                ),
                data=payload,
            )
        return ToolResult.ok(
            f"Ran in the guest at {target.describe()} (exit 0, "
            f"{result.duration_ms:.0f}ms):\n{result.output or '(no output)'}",
            payload,
        )


def build_tools() -> List[BaseTool]:
    """Every virtualization tool. Registered by :func:`build_default_registry`."""
    return [
        VMList(),
        VMStatus(),
        VMStart(),
        VMStop(),
        VMSuspend(),
        VMResume(),
        VMOpenConsole(),
        VMGuestCheck(),
        VMGuestExec(),
    ]
