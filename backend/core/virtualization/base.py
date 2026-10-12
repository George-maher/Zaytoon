"""The hypervisor contract, and the vocabulary for VM state.

The state vocabulary is deliberately wider than running/stopped. A hypervisor
that cannot be asked — because it is not installed, or the CLI refused — must be
able to say so, and the honest answer for "is it running?" when the tool that
would tell you is missing is *unknown*, not *stopped*. Collapsing those two is
how a report ends up asserting a machine is off when nobody checked.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import stat
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence

logger = logging.getLogger("Zaytoon.Virtualization")


class VMState(str, Enum):
    """What a virtual machine is doing, as far as anyone can tell.

    ``UNAVAILABLE`` and ``UNKNOWN`` are different claims:

    * ``UNAVAILABLE`` — the definition exists but is unusable (the file is gone
      or unreadable). Something is wrong and it can be named.
    * ``UNKNOWN`` — the question could not be put to the platform (no CLI, or
      output that could not be parsed). Nothing is wrong with the VM as such;
      the tool just has no evidence.
    """

    RUNNING = "running"
    STOPPED = "stopped"
    SUSPENDED = "suspended"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"

    @property
    def is_live(self) -> bool:
        """True only for states that mean the guest may be executing."""
        return self is VMState.RUNNING


@dataclass(frozen=True)
class HypervisorProgram:
    """A resolved executable, with the evidence that it is really there.

    ``path`` is what will be executed. ``roles`` records which roles this one
    binary fills (``vmrun`` and the UI are separate binaries that may live in
    different directories, and on some installs the UI is a shell script).
    """

    path: str
    roles: Sequence[str] = ()

    def has(self, role: str) -> bool:
        return role in self.roles

    def to_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "roles": list(self.roles)}


class HypervisorNotFound(RuntimeError):
    """No usable hypervisor was found, with the reason it was looked for."""


class VMOperationError(RuntimeError):
    """A lifecycle command ran and failed, with its output attached.

    Carries the command's own words. A lifecycle failure that reports only
    "failed" is unactionable, and the hypervisor's stderr is usually the whole
    answer (``Cannot open VM: file not found``, ``The VM is already running``).
    """

    def __init__(self, message: str, *, command: Sequence[str] = (), output: str = ""):
        super().__init__(message)
        self.command = list(command)
        self.output = output


@dataclass
class VMInfo:
    """One virtual machine, as discovered.

    ``state_evidence`` is not decoration. It is the observation that produced
    ``state``, recorded so a caller can tell the difference between "`vmrun
    list` did not mention it" and "we could not run `vmrun` at all" — which are
    the same state string only if you are not paying attention.
    """

    name: str
    path: str
    state: VMState
    state_evidence: str
    hypervisor: str = "vmware"
    #: Guest OS string from the `.vmx`, when one is present. Free text, shown
    #: as-is: it is what the VM's own configuration says, not a detection.
    guest_os: str = ""
    #: Where the entry came from: `inventory`, `scan`, or `running`.
    source: str = "unknown"
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def running(self) -> bool:
        return self.state is VMState.RUNNING

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "name": self.name,
            "path": self.path,
            "state": self.state.value,
            "state_evidence": self.state_evidence,
            "hypervisor": self.hypervisor,
            "source": self.source,
        }
        if self.guest_os:
            payload["guest_os"] = self.guest_os
        if self.extra:
            payload["extra"] = dict(self.extra)
        return payload


def is_executable_file(path: str) -> bool:
    """Whether ``path`` is a file this process could actually execute.

    On Windows the execute bit is not meaningful, so only existence and
    file-ness are checked there; on POSIX the ``X_OK`` bit is required. A
    directory named ``vmware`` must never be returned as the VMware binary,
    which is the mistake this function exists to prevent.
    """
    if not path:
        return False
    try:
        st = os.stat(path)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    if os.name == "nt":
        return True
    return os.access(path, os.X_OK)


def resolve_program(
    names: Sequence[str],
    search_dirs: Sequence[str] = (),
    explicit: str = "",
    roles: Sequence[str] = (),
    which: Callable[[str], Optional[str]] = None,
) -> Optional[HypervisorProgram]:
    """Locate a program by explicit path, ``PATH``, then known directories.

    Order matters: an operator's explicit setting wins over discovery, and
    discovery wins over the search list. Guessing from the search list first
    would let a stale installation elsewhere on disk silently take precedence
    over the one that was configured.
    """
    if which is None:
        import shutil

        which = shutil.which

    if explicit:
        if is_executable_file(explicit):
            return HypervisorProgram(explicit, roles)
        logger.warning("Configured program is not executable: %s", explicit)
        return None

    for name in names:
        found = which(name)
        if found and is_executable_file(found):
            return HypervisorProgram(found, roles)

    for directory in search_dirs:
        if not directory:
            continue
        for name in names:
            for candidate in (os.path.join(directory, name), os.path.join(directory, name + ".exe")):
                if is_executable_file(candidate):
                    return HypervisorProgram(candidate, roles)
    return None


def looks_like_vmx(path: str) -> bool:
    """Cheap check that a path is a VM definition rather than any old file."""
    return str(path).lower().endswith(".vmx")


def quote_argv(argv: Sequence[str]) -> str:
    """Render an argv for a human to read. **Display only, never execution.**

    Execution always goes through ``create_subprocess_exec`` with the list
    itself, so quoting can never be the thing that keeps a parameter from being
    reinterpreted by a shell.
    """
    return " ".join(shlex.quote(str(a)) for a in argv)


class Hypervisor(ABC):
    """What every hypervisor adapter offers the tool layer.

    Read-only methods are separated from lifecycle methods on purpose: the tool
    layer maps the first to a `safe` tier and the second to `confirm`, and that
    mapping is only defensible if the adapter itself keeps the two apart.
    """

    #: Stable identifier used in tool output and errors.
    name: str = "hypervisor"

    @abstractmethod
    async def available(self) -> bool:
        """Whether this hypervisor can be driven at all on this host."""

    @abstractmethod
    async def why_unavailable(self) -> str:
        """A sentence explaining the last discovery failure. Never empty."""

    @abstractmethod
    async def list_vms(self) -> List[VMInfo]:
        """Every VM this adapter can see, with evidenced state."""

    @abstractmethod
    async def status(self, vm_ref: str) -> VMInfo:
        """One VM's state. Raises :class:`VMOperationError` if unresolvable."""

    @abstractmethod
    async def start(self, vm_ref: str, *, gui: bool = True) -> VMInfo:
        """Power on. Returns the state *after* the command, not the intent."""

    @abstractmethod
    async def stop(self, vm_ref: str, *, mode: str = "soft") -> VMInfo:
        """``soft`` asks the guest; ``hard`` is a power cut."""

    @abstractmethod
    async def suspend(self, vm_ref: str) -> VMInfo:  # pragma: no cover - trivial
        """Suspend, preserving guest memory."""

    @abstractmethod
    async def resume(self, vm_ref: str) -> VMInfo:  # pragma: no cover - trivial
        """Resume a suspended VM."""

    @abstractmethod
    async def open_console(self, vm_ref: str) -> str:
        """Ask the hypervisor's UI to show this VM. Returns what was launched."""


@dataclass
class CommandOutcome:
    """The result of one hypervisor CLI invocation."""

    argv: Sequence[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def output(self) -> str:
        """Combined output, for error messages and evidence lines."""
        parts = [p for p in (self.stdout.strip(), self.stderr.strip()) if p]
        return "\n".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command": quote_argv(self.argv),
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
        }


async def run_command(
    argv: Sequence[str], *, timeout: float = 60.0, env: Optional[Dict[str, str]] = None
) -> CommandOutcome:
    """Run a CLI tool, capturing output, never raising on a non-zero exit.

    An argv list and ``create_subprocess_exec``: there is no shell in this path,
    so nothing in a VM name or a path can be reinterpreted as a command. The
    caller decides what a failure means — this only reports what happened,
    which keeps "the tool is missing" from being reported as "the VM failed".
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError as exc:
        return CommandOutcome(argv, 127, stderr=f"{exc.strerror or 'not found'}: {argv[0]}")
    except PermissionError as exc:
        return CommandOutcome(argv, 126, stderr=f"not executable: {exc}")

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        # Reap it: a killed child that is never awaited becomes a zombie, and
        # on a long-running server that is a leak per failed VM operation.
        try:
            await proc.wait()
        except Exception:  # pragma: no cover - defensive
            pass
        return CommandOutcome(argv, -1, timed_out=True)

    def _decode(raw: bytes) -> str:
        return (raw or b"").decode("utf-8", errors="replace").strip()

    return CommandOutcome(
        argv,
        proc.returncode if proc.returncode is not None else -1,
        stdout=_decode(stdout),
        stderr=_decode(stderr),
    )
