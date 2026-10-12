# Virtual machines (VMware) and guest command execution

Zaytoon drives VMware through `vmrun` and reaches inside Linux guests over
authenticated SSH. This page is the setup, the environment variables, and — more
importantly — what has and has not been verified.

> **Verification status.** VMware is **not installed in the environment where
> this was developed**, so no real VM was started and no real guest was
> connected to. Everything below is implemented and covered by tests that use a
> **fake `vmrun` executable** and a **fake `ssh`**: real subprocesses, real argv,
> real exit codes, scripted responses. That is a strong test of the adapter and
> a weak test of VMware itself. **Treat the real-VMware path as unverified until
> you run it on a host that has VMware.**

## Tools

| Tool | Tier | Permission | What it does |
|---|---|---|---|
| `vm_list` | safe | `vm.discover` | Lists VMs and their state, with the evidence for each state |
| `vm_status` | safe | `vm.discover` | One VM's state, and what determined it |
| `vm_start` | confirm | `vm.manage` | Powers a VM on; can wait for guest readiness |
| `vm_stop` | confirm → **dangerous** for `hard` | `vm.manage` | Soft shutdown, or a power cut |
| `vm_suspend` / `vm_resume` | confirm | `vm.manage` | Suspend to disk, resume |
| `vm_open_console` | confirm | `vm.manage` | Opens the VM's console window |
| `vm_guest_check` | confirm | `vm.guest.exec` + `network.request` | Proves the guest can execute something |
| `vm_guest_exec` | confirm → **dangerous** for destructive commands | `vm.guest.exec` + `network.request` | Runs one command inside the guest |

`vm.manage` and `vm.guest.exec` are deliberately separate grants: consent to
power a machine on is not consent to run commands inside it. `vm_guest_exec`
carrying a command that matches the existing hazard patterns (`rm -rf`, `dd
if=… of=/dev/…`, `shutdown`, …) escalates to **dangerous** through the same
table that already guards `execute_local_command` — there is no VM-specific
safety rule to drift out of sync.

## Reaching it: the Virtual Machines screen and the HTTP routes

You do not have to ask in chat. **Virtual Machines** is a destination in the
sidebar (`◫`), backed by these routes:

| Route | Kind | What it does |
|---|---|---|
| `GET /virtualization/hypervisors` | read-only | Whether each hypervisor's programs were found, and where |
| `GET /virtualization/vms` | read-only | Inventory, with `state_evidence` per VM; `?running_only=` |
| `GET /virtualization/vms/one?ref=` | read-only | One VM |
| `GET /virtualization/guest` | read-only | What guest access is configured — never the secret itself |
| `GET /virtualization/actions` | read-only | The action catalogue and each action's tier |
| `POST /virtualization/vms/action` | **two-step** | Preview a lifecycle action, then confirm it |
| `POST /virtualization/guest/exec` | **two-step** | Preview a guest command, then confirm it |

The two acting routes never accept the operation on the second call. Step 1
returns a single-use `confirm` token and *stores the operation*; step 2 ignores
the request body and runs what was stored, then reports it. So a token issued for
`start Ubuntu` cannot be spent on `stop Parrot --hard`, and the reply names what
actually ran. A preview says what the tier is before anything happens: `hard`
stops come back `dangerous`, and the screen will not let one be confirmed until
its acknowledgement box is ticked.

If no guest account is configured, `POST /virtualization/guest/exec` refuses
*before any socket is opened* and tells you which variable is missing — see
`GET /virtualization/guest`.

## Finding VMware

Nothing assumes an install path. The adapter probes, in order:

1. `VMWARE_VMRUN` / `VMWARE_UI`, if set (must exist — a typo is an error, not a
   silent fallback to a different installation).
2. `vmrun` and the Workstation UI binary on `PATH`.
3. The conventional directories for the platform: `C:\Program Files
   (x86)\VMware\VMware Workstation` and siblings on Windows,
   `/usr/bin` · `/usr/lib/vmware/bin` · `/opt/vmware/bin` on Linux,
   `/Applications/VMware Fusion.app/Contents/...` on macOS.

Each candidate is stat-ed and must be a **regular executable file**, so a
directory named `vmrun` is not mistaken for the program.

## Environment variables

All of these are read by `backend/config.py`. Defaults are shown.

| Variable | Default | Meaning |
|---|---|---|
| `VMWARE_VMRUN` | *(discovered)* | Full path to `vmrun` |
| `VMWARE_UI` | *(discovered)* | Full path to the Workstation/Fusion UI binary |
| `VMWARE_BIN_DIRS` | *(platform defaults)* | Extra directories to probe, `os.pathsep`-separated |
| `VMWARE_TYPE` | `ws` | The `vmrun -T` product: `ws`, `player`, `fusion`, `server` |
| `VM_INVENTORY_DIRS` | *(conventional dirs)* | Where to scan for `*.vmx`. Empty = read VMware's own inventory plus the usual per-user directories |
| `VM_OPERATION_TIMEOUT` | `60.0` | Seconds allowed for one lifecycle command |
| `VM_READY_TIMEOUT` | `180.0` | Ceiling for "wait until ready" |
| `VM_READY_POLL` | `5.0` | Poll interval while waiting |
| `VM_GUEST_TIMEOUT` | `30.0` | Ceiling for one command inside a guest |
| `VM_GUEST_USER` | *(empty)* | Account to use in the guest. **Unset means guest execution is not configured** |
| `VM_GUEST_IDENTITY` | *(empty)* | Path to the private key for that account |
| `VM_GUEST_CONNECT_TIMEOUT` | `10.0` | SSH connect timeout |
| `VM_GUEST_HOST_KEY_POLICY` | `strict` | `strict` or `accept-new`. `no` is refused |
| `VM_GUEST_SSH_OPTIONS` | *(empty)* | Extra `-o` options, comma-separated (jump host, odd port) |

VM discovery also reads VMware's own `inventory.vmls`
(`%APPDATA%\VMware\inventory.vmls` on Windows, `~/.vmware/inventory.vmls`
elsewhere), so VMs are named the way VMware names them rather than by folder.

## Setting up guest access

Guest execution needs an SSH account **in the guest**, reachable from the host.
Zaytoon does not configure this for you, and it will not offer to weaken
anything to make it work.

```bash
# 1. In the guest: make sure sshd is running and reachable.
#    A host-only or NAT-with-port-forward network is typical.

# 2. On the host: create a key if you have not already.
ssh-keygen -t ed25519 -f ~/.ssh/zaytoon_lab

# 3. Authorise it in the guest.
ssh-copy-id -i ~/.ssh/zaytoon_lab.pub lab@<guest-ip>

# 4. Record the guest's host key the first time (or use accept-new once).
ssh-keyscan -H <guest-ip> >> ~/.ssh/known_hosts

# 5. Point Zaytoon at it.
export VM_GUEST_USER=lab
export VM_GUEST_IDENTITY=~/.ssh/zaytoon_lab
```

### Why there is no password option

A password would have to reach `ssh` on the command line (readable by every
process on the host, and by anything that logs argv), through `sshpass` (same),
or via an interactive prompt Zaytoon would have to hold. All three put a
credential where the model can influence it. So the channel authenticates by
**key or agent only**, `BatchMode=yes` is always set, and
`PasswordAuthentication=no` is passed explicitly. A missing key produces a
message telling you how to install one — not a prompt you cannot answer.

Host-key verification is likewise not optional: `StrictHostKeyChecking` is
`yes` by default and `accept-new` is offered for a guest you have just built.
There is no setting that turns it off.

## What "ready" means

Two different questions, and they are answered separately:

* **Is the VM running?** From `vmrun list`. This is a host-side fact and it does
  not mean the guest booted.
* **Is the guest ready?** Only from an authenticated command succeeding inside
  it (`vm_guest_check`). VMware Tools reporting `running` is recorded as a
  *host-side* signal that the guest OS booted; it is not treated as readiness,
  because it says nothing about the network or about SSH.

Consequently: **an open console window is not boot evidence, and a running VM
process is not boot evidence.** `vm_start` without `wait_for_ready` says so in
its own output.

## Discovered state is evidenced, or unknown

Every `VMInfo` carries `state_evidence`. The mapping:

| State | How it is decided |
|---|---|
| `running` | The path is in `vmrun list` output |
| `suspended` | A `.vmss` file sits beside the `.vmx` |
| `stopped` | The `.vmx` exists, `vmrun list` ran successfully without it, and no `.vmss` |
| `unavailable` | The `.vmx` is missing or unreadable |
| `unknown` | `vmrun list` could not be run at all |

A `.lck` directory is reported as a hint (`lock_present`) but never decides the
state: a lock left behind by a crash is indistinguishable from a live one, so
promoting it to "running" would be inventing information.

## Ambiguous names are refused

`vm_start Ubuntu` resolves in this order: exact display name → exact `.vmx`
file stem → **unique** substring. If two or more VMs match at the substring
stage, the call fails and lists them. There is no closest-match fallback: that
is how you boot `Ubuntu-16.04-OLD` when you meant `Ubuntu 24.04 LTS`.

## Not implemented

* **Snapshots.** No `vm_snapshot` tool. Reverting a snapshot is on the brief's
  list of operations needing strong confirmation, and it needs its own design
  rather than a thin `vmrun snapshot` wrapper.
* **Networking and resource changes.** No tool changes VM network modes, disk
  or RAM allocation. The brief lists these as confirmation-required; they are
  simply not exposed yet.
* **Deleting a VM or virtual disk.** Not exposed at all.
* **Guest access on Windows guests.** The channel is SSH, which covers Linux
  guests with `sshd`. Windows guests are not supported by this adapter.
* **`vmrun` on macOS (Fusion)** is wired through the same code and the same
  discovery, but was never run on a Mac.

## Running the tests

```bash
# Backend: the adapter, the tools, the workflows, and the HTTP surface.
cd backend && PYTHONPATH=../.github python -m pytest \
  tests/core/test_virtualization_vmware.py \
  tests/test_vm_tools.py \
  tests/test_vm_workflows.py \
  tests/test_virtualization_routes.py -q          # 126 passed

# Frontend: the screen.
cd frontend && npx vitest run src/components/VirtualMachinesView.test.jsx   # 12 passed
```

These use fake executables and need no VMware, no VM and no network. A grep for
`vmware`/`vmrun` in the test files will confirm there is no real installation
being touched. `tests/test_virtualization_routes.py` installs its fake adapter
through `monkeypatch.setattr(vt, "_ADAPTER_FACTORY", …)`, so the routes,
registry, confirmation manager and approval-token map under test are the real
ones.
