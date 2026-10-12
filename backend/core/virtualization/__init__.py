"""Virtualization adapters.

Why this is its own package rather than more tools in :mod:`tools`:

A hypervisor is a *host capability* with a CLI, an install layout, a set of
lifecycle verbs and a set of failure modes that have nothing in common with
`file_write`. Keeping that behind an adapter means the tool layer can be written
once and the VMware specifics — `vmrun`, `inventory.vmls`, lock directories —
never leak into the registry, the agent prompt, or the other tools. If VirtualBox
or libvirt is added later it implements the same surface and changes nothing
above it.

Three rules this package holds to, because breaking any of them produces an
assistant that lies about what it did:

1. **State is reported from evidence, or reported as unknown.** A VM's state is
   never inferred from the fact that a command was issued, and "the window
   opened" is not boot evidence. Every :class:`VMInfo` carries the observation
   that produced its state.
2. **Nothing is assumed about installation.** Executables are resolved by
   probing, and a path that does not exist is reported as missing rather than
   attempted.
3. **Credentials never enter a command line.** The guest channel uses SSH keys
   or an agent; see :mod:`core.virtualization.guest`.
"""

from core.virtualization.base import (
    Hypervisor,
    HypervisorNotFound,
    HypervisorProgram,
    VMInfo,
    VMOperationError,
    VMState,
    resolve_program,
)
from core.virtualization.guest import GuestResult, GuestSession, GuestTarget
from core.virtualization.vmware import Readiness, VMwareWorkstation

__all__ = [
    "GuestResult",
    "GuestSession",
    "GuestTarget",
    "Hypervisor",
    "HypervisorNotFound",
    "HypervisorProgram",
    "VMInfo",
    "VMOperationError",
    "Readiness",
    "VMState",
    "VMwareWorkstation",
    "resolve_program",
]
