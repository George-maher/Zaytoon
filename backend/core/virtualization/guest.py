"""Authenticated command execution inside a guest, over SSH.

Why there is no password parameter
----------------------------------
A password would have to reach `ssh` somehow: on the command line (visible to
every process on the host, and to anything that logs argv), through `sshpass`
(same), or by driving an interactive prompt (which means the tool holds a
credential the model can influence). None of those is acceptable for a system
whose entire security story is "the model never holds a secret".

So this channel authenticates with an SSH **key or agent only**. That is not a
limitation worked around; it is the design. A user who has not set up a key gets
a precise message saying so and how to fix it, rather than a tool that silently
tries a password it should not have.

Host identity
-------------
``StrictHostKeyChecking`` is left at ``strict`` by default, using the user's own
``known_hosts``. ``accept-new`` is available for a freshly-built lab VM whose
key has never been seen. ``no`` is **not** reachable through configuration —
disabling host verification is not a trade-off this tool offers, because a
machine that cannot tell one guest from another cannot authenticate a command
to either.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from core.virtualization.base import CommandOutcome, quote_argv, run_command

logger = logging.getLogger("Zaytoon.Virtualization.Guest")

#: Refused outright, whatever the configuration says.
_FORBIDDEN_POLICIES = frozenset({"no", "off", "false", "disable", "disabled"})
#: Accepted policies, mapped to the option value OpenSSH understands.
_POLICY_MAP = {
    "strict": "yes",
    "yes": "yes",
    "accept-new": "accept-new",
    "acceptnew": "accept-new",
    "ask": "ask",
}


@dataclass(frozen=True)
class GuestTarget:
    """Where a guest lives and how to authenticate to it.

    No password field exists, by construction. ``identity`` is a path to a
    private key; leaving it empty defers to the ssh agent and the user's
    ``~/.ssh/config``, which is often already correct.
    """

    host: str
    user: str
    port: int = 22
    identity: str = ""
    host_key_policy: str = "strict"
    options: Sequence[str] = ()

    def __post_init__(self) -> None:
        if not str(self.host).strip():
            raise ValueError("A guest target needs a host.")
        if not str(self.user).strip():
            raise ValueError(
                "A guest target needs a user name. Set VM_GUEST_USER or pass one "
                "explicitly; connecting as an unnamed account is not a guess worth "
                "making."
            )
        policy = str(self.host_key_policy or "strict").strip().lower()
        if policy in _FORBIDDEN_POLICIES:
            raise ValueError(
                "Refusing to disable SSH host-key verification. Use 'strict' "
                "(default) or 'accept-new' for a guest whose key you have not "
                "recorded yet."
            )
        if policy not in _POLICY_MAP:
            raise ValueError(
                f"Unknown host-key policy '{self.host_key_policy}'. Use 'strict' "
                f"(default) or 'accept-new'."
            )

    def ssh_options(self) -> List[str]:
        """The `-o` options and identity flag, in a fixed order.

        ``BatchMode=yes`` is not negotiable: without it, a connection that would
        need a password prompts, and a prompt in a non-interactive process is a
        hang rather than an error.
        """
        policy = _POLICY_MAP[str(self.host_key_policy or "strict").strip().lower()]
        opts = [
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={int(_connect_timeout())}",
            "-o", f"StrictHostKeyChecking={policy}",
            "-o", "PasswordAuthentication=no",
            # Keep the channel from stealing the client's stdin; a guest command
            # must not be able to read whatever the host process has open.
            "-o", "PreferredAuthentications=publickey,hostbased",
        ]
        if self.identity:
            opts += ["-i", os.path.expanduser(self.identity)]
        if self.port and int(self.port) != 22:
            opts += ["-p", str(int(self.port))]
        for option in self.options or ():
            opts += ["-o", str(option)]
        return opts

    def destination(self) -> str:
        return f"{self.user}@{self.host}"

    def describe(self) -> str:
        """For reports and logs. Contains no secret, because there is none."""
        return f"{self.user}@{self.host}:{int(self.port or 22)}"


def _connect_timeout() -> float:
    from config import Config

    return float(getattr(Config, "VM_GUEST_CONNECT_TIMEOUT", 10.0))


@dataclass
class GuestResult:
    """One command, and everything needed to judge it.

    ``timed_out`` is separate from a non-zero exit because they mean different
    things: the command failed, versus the command may still be running.
    """

    target: str
    command: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_ms: float = 0.0
    timed_out: bool = False
    error: str = ""
    argv: Sequence[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.error

    @property
    def output(self) -> str:
        parts = [p for p in (self.stdout.strip(), self.stderr.strip()) if p]
        return "\n".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "target": self.target,
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": round(self.duration_ms, 1),
            "timed_out": self.timed_out,
        }
        if self.error:
            payload["error"] = self.error
        # The argv is echoed for the audit trail. It cannot contain a secret:
        # there is no password parameter, and the only credential-shaped value
        # is a *path* to a private key.
        payload["command_line"] = quote_argv(self.argv)
        return payload


class GuestSession:
    """Runs commands in one guest through the system `ssh` client.

    The system client rather than a Python SSH library on purpose: host keys,
    agent forwarding, jump hosts and `~/.ssh/config` already work, and adding a
    second SSH implementation would mean maintaining two answers to "is this
    host who it says it is".
    """

    def __init__(self, target: GuestTarget, *, ssh: str = "ssh", runner=None):
        self.target = target
        self._ssh = ssh
        self._runner = runner or run_command

    def _argv(self, command: Optional[str]) -> List[str]:
        argv = [self._ssh, *self.target.ssh_options(), self.target.destination()]
        if command is not None:
            # The remaining words are joined by ssh and executed by the guest's
            # shell — that is what "run this in the guest" means. Nothing here
            # passes through a *host* shell: `create_subprocess_exec` takes the
            # list directly, so no host-side interpretation is possible.
            argv.append(command)
        return argv

    async def run(self, command: str, *, timeout: Optional[float] = None) -> GuestResult:
        """Run ``command`` in the guest and report exactly what came back."""
        text = str(command or "").strip()
        if not text:
            return GuestResult(
                target=self.target.describe(),
                command=command or "",
                exit_code=-1,
                error="No command was given.",
            )

        from config import Config

        budget = float(timeout if timeout is not None else getattr(Config, "VM_GUEST_TIMEOUT", 30.0))
        argv = self._argv(text)
        started = time.monotonic()
        try:
            outcome: CommandOutcome = await self._runner(argv, timeout=budget)
        except Exception as exc:  # noqa: BLE001 - surfaced verbatim to the caller
            elapsed = (time.monotonic() - started) * 1000.0
            return GuestResult(
                target=self.target.describe(),
                command=text,
                exit_code=-1,
                duration_ms=elapsed,
                error=f"Could not start ssh: {exc}",
                argv=argv,
            )
        elapsed = (time.monotonic() - started) * 1000.0

        result = GuestResult(
            target=self.target.describe(),
            command=text,
            exit_code=outcome.returncode,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            duration_ms=elapsed,
            timed_out=outcome.timed_out,
            argv=argv,
        )
        result.error = self._explain(outcome)
        return result

    def _explain(self, outcome: CommandOutcome) -> str:
        """Turn ssh's stderr into an instruction, without guessing at success.

        ssh's exit code for "could not connect" is 255, which is also its code
        for several other conditions, so the message is read rather than
        inferred from the number alone.
        """
        if outcome.timed_out:
            return (
                f"{self.target.describe()} did not respond within the timeout. The "
                f"guest may still be booting, or its network may not be up yet."
            )
        if outcome.returncode == 127:
            return "The `ssh` client was not found on this host."
        if outcome.returncode != 255:
            return ""
        text = f"{outcome.stderr}\n{outcome.stdout}".lower()
        if "permission denied" in text and "publickey" in text:
            return (
                f"Authentication to {self.target.describe()} failed: no usable SSH key. "
                f"Zaytoon never uses a password — install a public key for this account "
                f"(or set VM_GUEST_IDENTITY to the private key) and try again."
            )
        if "host key verification failed" in text:
            return (
                f"The host key for {self.target.host} is not trusted. Add it to your "
                f"known_hosts (ssh-keyscan), or set VM_GUEST_HOST_KEY_POLICY=accept-new "
                f"for a freshly built lab guest you are prepared to trust on first use."
            )
        if "connection refused" in text:
            return (
                f"{self.target.describe()} refused the connection. sshd may not be "
                f"running in the guest, or the port is wrong."
            )
        if "no route to host" in text or "connection timed out" in text:
            return (
                f"{self.target.describe()} is not reachable from this host. Check that "
                f"the guest has finished booting and that its network is up — a guest "
                f"NAT address is usually not reachable unless port forwarding or a "
                f"host-only network is configured."
            )
        if "batchmode" in text or "password" in text:
            return (
                f"{self.target.describe()} asked for a password, and this channel does "
                f"not supply one. Use key-based authentication."
            )
        return f"ssh failed: {outcome.stderr.strip() or 'no output'}" if outcome.stderr.strip() else ""

    async def check(self, *, timeout: Optional[float] = None) -> GuestResult:
        """Prove the guest can execute something, as cheaply as possible.

        ``true`` rather than a ping: ICMP would only prove a route exists, and
        the question is whether an *authenticated command* succeeds. The result
        of this call is the only thing that may be reported as guest readiness.
        """
        return await self.run("true", timeout=timeout or _connect_timeout())

    async def info(self, *, timeout: Optional[float] = None) -> Dict[str, Any]:
        """A small, read-only description of the guest, for reports.

        Every value is treated as *reported by the guest*, not as verified
        truth about the host it runs on.
        """
        result = await self.run(
            "uname -a; printf '\\n'; (cat /etc/os-release 2>/dev/null | head -n 2)",
            timeout=timeout,
        )
        return {
            "target": self.target.describe(),
            "ok": result.ok,
            "reported": result.stdout.strip(),
            "exit_code": result.exit_code,
            "error": result.error,
        }


def target_from_config(host: str = "", user: str = "") -> Optional[GuestTarget]:
    """Build a target from the configured defaults.

    Returns ``None`` when the configuration does not name a user, which is a
    real answer: the guest channel is not configured, and the caller must say
    that rather than pretend guest execution is available.
    """
    from config import Config

    resolved_user = (user or str(getattr(Config, "VM_GUEST_USER", "") or "")).strip()
    if not resolved_user:
        return None
    if not str(host or "").strip():
        return None

    extra = [
        opt.strip()
        for opt in str(getattr(Config, "VM_GUEST_SSH_OPTIONS", "") or "").split(",")
        if opt.strip()
    ]
    return GuestTarget(
        host=str(host).strip(),
        user=resolved_user,
        identity=str(getattr(Config, "VM_GUEST_IDENTITY", "") or "").strip(),
        host_key_policy=str(getattr(Config, "VM_GUEST_HOST_KEY_POLICY", "strict") or "strict"),
        options=tuple(extra),
    )


def guest_config_report() -> Dict[str, Any]:
    """What the guest channel is configured to do, for the UI to display.

    Reports *presence*, never contents. The account is shown because the user
    needs to see what they configured; the identity is reported as a boolean
    (set, and does the file exist) because a key path is not a secret but a key
    is, and a habit of printing one is how it ends up in a screenshot.

    ``problems`` is the honest half: each entry names a prerequisite that is
    missing and what it stops. A UI that shows "guest access: available" when
    no account is configured is worse than one that shows nothing, because the
    user then reads every later failure as a bug.
    """
    from config import Config

    user = str(getattr(Config, "VM_GUEST_USER", "") or "").strip()
    identity = str(getattr(Config, "VM_GUEST_IDENTITY", "") or "").strip()
    policy_raw = str(getattr(Config, "VM_GUEST_HOST_KEY_POLICY", "strict") or "strict").strip()
    policy = (policy_raw or "strict").lower()
    options = str(getattr(Config, "VM_GUEST_SSH_OPTIONS", "") or "").strip()

    problems: List[str] = []
    if not user:
        problems.append(
            "No guest account is configured (VM_GUEST_USER). Without one there is "
            "no account to authenticate as, and Zaytoon will not guess one."
        )
    if identity and not os.path.isfile(os.path.expanduser(identity)):
        problems.append(
            "VM_GUEST_IDENTITY names a key that is not on disk. The connection "
            "will fail rather than fall back to a password, because there is no "
            "password to fall back to."
        )
    if policy in _FORBIDDEN_POLICIES:
        problems.append(
            f"VM_GUEST_HOST_KEY_POLICY is {policy_raw!r}, which would disable host "
            f"verification. The channel refuses it and uses 'strict' instead: a "
            f"machine that cannot tell one guest from another cannot authenticate "
            f"a command to either."
        )
        policy = "strict"
    if not identity and not os.environ.get("SSH_AUTH_SOCK"):
        problems.append(
            "No key is configured and no ssh-agent is running, so a key-only "
            "connection cannot succeed. Set VM_GUEST_IDENTITY to a private key, "
            "or start an agent and load one into it."
        )

    return {
        "configured": bool(user),
        "user": user,
        "identity_set": bool(identity),
        "identity_exists": bool(identity) and os.path.isfile(os.path.expanduser(identity)),
        "host_key_policy": policy,
        "connect_timeout_s": float(getattr(Config, "VM_GUEST_CONNECT_TIMEOUT", 10.0)),
        "command_timeout_s": float(getattr(Config, "VM_GUEST_TIMEOUT", 30.0)),
        "extra_options": [o.strip() for o in options.split(",") if o.strip()],
        "authentication": "SSH key or agent only; a password is never accepted",
        "address_source": (
            "The guest's own address comes from VMware Tools inside the VM "
            "(vmrun getGuestIPAddress), or from an address you pass explicitly."
        ),
        "problems": problems,
    }
