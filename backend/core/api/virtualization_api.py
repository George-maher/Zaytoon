"""The virtualization REST surface.

Nine VM tools existed in the registry with no way for the interface to see or
drive any of them: the capability was reachable only by asking the assistant in
chat. This is the missing half — the Virtual Machines workspace reads from here.

Two kinds of route, and the difference between them is the point.

**Read-only routes** (``GET``) go through the registry with *no* confirmation
manager. That is not an oversight: a `safe`-tier tool never asks, and a tool
that somehow did ask would be refused, because there is no channel here to
approve anything on. A read-only route therefore cannot become a way to run
something consequential — it fails closed by construction.

**Acting routes** (``POST``) are two-step, and the token is *bound to the exact
operation that was previewed*. Step 1 resolves the VM and records
``(tool, params)`` against the token; step 2 runs **the recorded params**, not
whatever the second request happens to contain. This matters more here than it
did for the security probe, where the target had to be re-authorised anyway. A
VM approval is answered by an automatic ``resolve()`` — the human's gesture was
the *first* call — so if the second call could name its own operation, an
approval to "start Ubuntu" could be spent on "stop --hard Parrot". It cannot:
the second request carries only the token, and the operation it was issued for
is the operation that runs.

Actions are refused rather than guessed when the VM is ambiguous, which is the
same rule the tools apply; the preview shows the resolved machine, so the user
approves a named VM and not a search string.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Tuple

from fastapi import APIRouter, Body, HTTPException, Query

from core.api.approvals import ApprovalTokens, run_approved_tool, tier_of

logger = logging.getLogger("Zaytoon.API.Virtualization")

router = APIRouter()

#: Approval tokens for VM operations. A separate store from the security
#: probe's, so a token minted for one surface cannot be spent on the other.
_VM_APPROVALS = ApprovalTokens(ttl=120.0, cap=256, prefix="vm")

#: token -> (tool_name, params, action). The operation a token authorises,
#: fixed at preview time. Swept against the token store, so it cannot outlive
#: it. The action name is part of the record for the same reason the params
#: are: the response must describe what *ran*, and echoing the verb out of the
#: second request would let a caller be told "stop" about something that
#: started. The binding was right; the report was not, and the test that caught
#: it asserted the call *and* the answer.
_PENDING: Dict[str, Tuple[str, Dict[str, Any], str]] = {}

#: The lifecycle verbs this surface exposes, mapped to the tool that performs
#: them. `open_console` is here rather than under desktop automation because
#: what it opens is a hypervisor-managed console window.
_ACTIONS: Dict[str, str] = {
    "start": "vm_start",
    "stop": "vm_stop",
    "suspend": "vm_suspend",
    "resume": "vm_resume",
    "open_console": "vm_open_console",
}


def _sweep_pending() -> None:
    """Drop pending operations whose token is gone (expired or evicted)."""
    live = _VM_APPROVALS.tokens
    for token in list(_PENDING):
        if token not in live:
            _PENDING.pop(token, None)


async def _registry_read(tool: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Run a read-only tool through the registry, with no way to approve.

    ``confirmation_manager`` is deliberately omitted. A `safe` tool runs; a tool
    that wanted approval finds no channel and is refused. This is the property
    that keeps ``GET`` from being a way to act.
    """
    from api import main as api_main

    return await api_main.tool_executor.registry.execute(tool, params)


# ---------------------------------------------------------------------------
# Discovery and inventory — read-only
# ---------------------------------------------------------------------------
@router.get("/virtualization/hypervisors")
async def virtualization_hypervisors() -> Dict[str, Any]:
    """Which hypervisors this host actually has, and the reason when it has none.

    Reports the resolved executable paths. They are not secrets, and they are
    the first thing anyone needs to debug "it says VMware is missing".
    """
    from core.virtualization import VMwareWorkstation

    adapters = []
    try:
        vmware = VMwareWorkstation.discover()
        available = await vmware.available()
        reason = "" if available else await vmware.why_unavailable()
        programs = {}
        if vmware.vmrun is not None:
            programs["vmrun"] = vmware.vmrun.to_dict()
        if vmware.ui is not None:
            programs["ui"] = vmware.ui.to_dict()
        adapters.append(
            {
                "id": "vmware",
                "label": "VMware Workstation",
                "available": available,
                "reason": reason,
                "programs": programs,
                # Said once, here, so no screen has to remember to say it:
                # nothing about this host has been verified against a real
                # VMware install unless a path appears above.
                "platform_note": (
                    "Driven through `vmrun`. Workstation, Player and Fusion are "
                    "the supported editions; the edition in use is not verified "
                    "here, only the presence of the command-line tools."
                ),
            }
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Hypervisor discovery failed: %s", type(exc).__name__)
        adapters.append(
            {
                "id": "vmware",
                "label": "VMware Workstation",
                "available": False,
                "reason": f"Discovery failed: {type(exc).__name__}",
                "programs": {},
            }
        )

    from core.virtualization.guest import guest_config_report

    return {
        "hypervisors": adapters,
        "any_available": any(a["available"] for a in adapters),
        "guest": guest_config_report(),
        "supported": ["vmware"],
        "unsupported_note": (
            "VirtualBox, libvirt and Hyper-V have no adapter. `core/virtualization` "
            "is written against an adapter contract, so adding one is a new module "
            "rather than a change to the tools or this API."
        ),
    }


@router.get("/virtualization/vms")
async def virtualization_vms(running_only: bool = False) -> Dict[str, Any]:
    """Every VM known to the host, each with the observation behind its state."""
    result = await _registry_read("vm_list", {"running_only": bool(running_only)})
    data = result.get("data") or {}
    return {
        "success": bool(result.get("success")),
        "output": result.get("output", ""),
        "vms": data.get("vms", []),
        "count": data.get("count", 0),
        "hypervisor": data.get("hypervisor"),
        "error": result.get("error"),
    }


@router.get("/virtualization/vms/one")
async def virtualization_vm(ref: str = Query(..., description="VM name or .vmx path")) -> Dict[str, Any]:
    """One VM's state.

    Takes the reference as a query parameter rather than a path segment because
    a reference is often ``C:\\Users\\me\\Documents\\Virtual Machines\\u.vmx``
    — a path segment would have to survive slashes, backslashes and a drive
    colon, and the version that looks tidy is the one that breaks on Windows.
    """
    result = await _registry_read("vm_status", {"vm": ref})
    # `vm_status` returns the machine as its data payload (its own fields are
    # the payload), unlike `vm_list`, which nests a list under "vms". Matching
    # the tool's actual shape here rather than reshaping either one keeps a
    # single description of a VM in the system.
    return {
        "success": bool(result.get("success")),
        "output": result.get("output", ""),
        "vm": (result.get("data") or None),
        "error": result.get("error"),
    }


@router.get("/virtualization/guest")
async def virtualization_guest() -> Dict[str, Any]:
    """Whether a guest channel is configured — never any credential material."""
    from core.virtualization.guest import guest_config_report

    return guest_config_report()


# ---------------------------------------------------------------------------
# Lifecycle — two-step, bound to what was previewed
# ---------------------------------------------------------------------------
@router.post("/virtualization/vms/action")
async def virtualization_action(data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Start, stop, suspend, resume or open a VM's console.

    Step 1 resolves and describes; step 2 performs **that** operation. See the
    module docstring for why the token carries the operation rather than just
    proving that some approval happened.
    """
    action = str(data.get("action") or "").strip().lower()
    if action not in _ACTIONS:
        raise HTTPException(
            422,
            f"Unknown action '{action}'. Known actions: {', '.join(sorted(_ACTIONS))}.",
        )
    ref = str(data.get("ref") or "").strip()
    if not ref:
        raise HTTPException(422, "'ref' is required: name the virtual machine.")
    confirm = str(data.get("confirm") or "").strip()

    if confirm:
        _sweep_pending()
        if not _VM_APPROVALS.consume(confirm):
            raise HTTPException(
                409, "Unknown or expired confirmation token. Preview the action again."
            )
        pending = _PENDING.pop(confirm, None)
        if pending is None:
            raise HTTPException(
                409, "This approval is no longer attached to an operation. Preview it again."
            )
        tool, recorded, recorded_action = pending
        # The recorded params, never the request body: the operation that runs
        # is the operation that was shown.
        result = await run_approved_tool(tool, recorded)
        if isinstance(result, dict):
            # Overwritten, not defaulted: the answer must name the operation
            # that ran. A caller that asked for "stop" and got "start" needs
            # to be told exactly that.
            result["action"] = recorded_action
            result["vm_ref"] = recorded.get("vm")
        return result

    # -- Step 1. Resolve first, so the preview names a specific machine rather
    #    than echoing the string the user typed. Resolution refuses ambiguity,
    #    so this is also where "which Ubuntu?" is answered or refused.
    status = await _registry_read("vm_status", {"vm": ref})
    if not status.get("success"):
        return {
            "success": False,
            "error": status.get("error") or "The virtual machine could not be resolved.",
            "output": status.get("output", ""),
        }
    vm = status.get("data") or {}

    params: Dict[str, Any] = {"vm": vm.get("path") or ref}
    if action == "start":
        params["gui"] = bool(data.get("gui", True))
        params["wait_for_ready"] = bool(data.get("wait_for_ready", False))
    elif action == "stop":
        mode = str(data.get("mode") or "soft").strip().lower()
        if mode not in ("soft", "hard"):
            raise HTTPException(422, f"Unknown stop mode '{mode}': use 'soft' or 'hard'.")
        params["mode"] = mode
    elif action != "open_console":
        pass

    tool = _ACTIONS[action]
    # `issue` prunes first, so the sweep must follow it: sweeping before the
    # prune would leave a just-expired operation in the map until the *next*
    # preview happened to run.
    token = _VM_APPROVALS.issue()
    _sweep_pending()
    _PENDING[token] = (tool, params, action)

    # Assessed with the params, so `stop --hard` is reported as the dangerous
    # action it is rather than as a repeat of `stop --soft`.
    tier = tier_of(tool, params)
    describes = {
        "start": f"Power on {vm.get('name') or ref}",
        "stop": f"Stop {vm.get('name') or ref} ({params.get('mode', 'soft')})",
        "suspend": f"Suspend {vm.get('name') or ref}",
        "resume": f"Resume {vm.get('name') or ref}",
        "open_console": f"Open the console window for {vm.get('name') or ref}",
    }
    warnings = []
    if action == "stop" and params.get("mode") == "hard":
        warnings.append(
            "A hard stop pulls the power. Anything the guest has not written to "
            "disk is lost, and the disk image can be left inconsistent."
        )
    if action == "start" and params.get("wait_for_ready"):
        warnings.append(
            "Waiting for readiness opens an authenticated session in the guest; it "
            "succeeds only if the guest account and key are already configured."
        )
    return {
        "requires_confirmation": True,
        "confirm": token,
        "action": action,
        "tool": tool,
        "tier": tier,
        "dangerous": tier == "dangerous",
        "vm": vm,
        "params": params,
        "message": describes[action],
        "warnings": warnings,
        "note": (
            "Powering a machine on is not proof that its guest booted; the state "
            "reported afterwards is what `vmrun` observed."
        ),
    }


# ---------------------------------------------------------------------------
# Guest execution — two-step, and it says what is missing
# ---------------------------------------------------------------------------
@router.post("/virtualization/guest/exec")
async def virtualization_guest_exec(data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Run one command inside a guest over the authenticated SSH channel.

    Consequential, so two-step. The command is the operation, so the token is
    bound to the exact command string: approving ``ip a`` cannot run something
    else.
    """
    confirm = str(data.get("confirm") or "").strip()
    command = str(data.get("command") or "").strip()
    vm = str(data.get("vm") or "").strip()
    host = str(data.get("host") or "").strip()

    if confirm:
        _sweep_pending()
        if not _VM_APPROVALS.consume(confirm):
            raise HTTPException(
                409, "Unknown or expired confirmation token. Preview the command again."
            )
        pending = _PENDING.pop(confirm, None)
        if pending is None:
            raise HTTPException(
                409, "This approval is no longer attached to a command. Preview it again."
            )
        tool, recorded, _recorded_action = pending
        result = await run_approved_tool(tool, recorded)
        if isinstance(result, dict):
            result["where"] = "guest"
            result["command"] = recorded.get("command")
        return result

    if not command:
        raise HTTPException(422, "'command' is required.")

    from core.virtualization.guest import guest_config_report

    report = guest_config_report()
    if not report["configured"]:
        # Refused before any network call, with the prerequisite named. A 4xx
        # here is the honest answer: there is no account to run as, and the
        # failure is configuration rather than a command that went wrong.
        return {
            "success": False,
            "configured": False,
            "error": (
                "No guest account is configured, so no command can be run inside a "
                "VM. Set VM_GUEST_USER and use a key or agent; a password is never "
                "accepted."
            ),
            "problems": report["problems"],
        }
    if not vm and not host:
        return {
            "success": False,
            "configured": True,
            "error": (
                "Name the virtual machine ('vm') or give the guest's address ('host'). "
                "An address can be read from VMware Tools for a running VM, but only "
                "when Tools is actually running inside it."
            ),
            "problems": report["problems"],
        }

    params: Dict[str, Any] = {"command": command}
    if vm:
        params["vm"] = vm
    if host:
        params["host"] = host
    if data.get("timeout") is not None:
        params["timeout"] = float(data["timeout"])

    token = _VM_APPROVALS.issue()
    _sweep_pending()
    _PENDING[token] = ("vm_guest_exec", params, "guest_exec")

    return {
        "requires_confirmation": True,
        "confirm": token,
        "command": command,
        "vm": vm or None,
        "host": host or None,
        "tier": tier_of("vm_guest_exec"),
        "message": f"Run in the guest: {command}",
        "guest": report,
        "note": (
            "The command runs inside the guest as the configured account. It is not "
            "bounded by this host's sandbox, so the approval covers the exact command "
            "shown here and nothing else."
        ),
    }


@router.get("/virtualization/actions")
async def virtualization_actions() -> Dict[str, Any]:
    """The verbs this surface offers, with the tier each one runs at."""
    return {
        "actions": [
            {"action": name, "tool": tool, "tier": tier_of(tool)}
            for name, tool in _ACTIONS.items()
        ],
        "guest_tool": {"tool": "vm_guest_exec", "tier": tier_of("vm_guest_exec")},
    }
