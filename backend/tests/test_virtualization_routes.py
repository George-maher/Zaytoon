"""The virtualization REST surface.

The backend had nine VM tools and no way for the interface to reach any of
them. These tests are about the properties that make exposing them safe, not
about the JSON shape:

* **A read-only route cannot act.** `GET` handlers dispatch with no
  confirmation manager, so a tool that asks is refused rather than approved.
  This is asserted directly against the helper, because the property is what
  stops a future edit from "helpfully" adding a channel to it.

* **An approval is bound to the operation that was previewed.** This is the
  one that matters most. The second request carries only a token — if the body
  could name its own operation, a preview of *start Ubuntu* could be spent on
  *stop --hard Parrot*, and the whole two-step dance would be theatre. The
  fake adapter records what it was actually asked to do, so the assertion is
  about the call that went out, not about the response text.

* **Nothing is guessed.** An ambiguous name is refused with the candidates
  listed; a missing guest account is refused with the variable to set; a
  resolved-but-unsatisfiable request never returns a token.

* **A token is a gesture.** Single use, expiring at use, and a replay is 409.
"""

from __future__ import annotations

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api.main as main_module  # noqa: E402
import tools.virtualization as vt  # noqa: E402
from core.api import virtualization_api  # noqa: E402
from core.virtualization import VMInfo, VMState  # noqa: E402
from core.virtualization.base import VMOperationError  # noqa: E402


def _vm(name: str, path: str, state: VMState = VMState.STOPPED, guest_os: str = "") -> VMInfo:
    return VMInfo(
        name=name,
        path=path,
        state=state,
        state_evidence="not listed by `vmrun list`",
        guest_os=guest_os,
        hypervisor="vmware",
    )


@pytest.fixture
def client():
    return TestClient(main_module.app)


@pytest.fixture(autouse=True)
def _clean_tokens():
    virtualization_api._VM_APPROVALS.clear()
    virtualization_api._PENDING.clear()
    yield
    virtualization_api._VM_APPROVALS.clear()
    virtualization_api._PENDING.clear()


class _FakeAdapter:
    """Records the calls it receives, so the *operation* can be asserted.

    Resolves by exact name only. The real adapter also matches a `.vmx` stem and
    a unique substring; what these tests need is a resolver that is predictable,
    and the ambiguity rules themselves are covered where they live.
    """

    def __init__(self, vms):
        self.vms = list(vms)
        self.calls = []

    async def available(self):
        return True

    async def why_unavailable(self):
        return ""

    async def list_vms(self):
        return list(self.vms)

    def _find(self, ref):
        """Resolve by display name, `.vmx` path, or `.vmx` stem.

        All three, because the real adapter resolves all three and step 2
        deliberately passes a *path* — a fake that only understood names would
        make the bound-to-the-preview property untestable, or worse, look like
        it had been tested.
        """
        wanted = str(ref).strip().lower()
        for vm in self.vms:
            base = vm.path.rsplit("/", 1)[-1]
            if wanted in (vm.name.lower(), vm.path.lower(), base.lower()):
                return vm
        raise VMOperationError(
            f"No virtual machine matches '{ref}'. Known machines: "
            + ", ".join(v.name for v in self.vms)
        )

    async def _resolve_live(self, ref):
        return self._find(ref), "matched by exact name"

    async def resolve_and_describe(self, ref):
        return await self._resolve_live(ref)

    async def status(self, ref):
        return self._find(ref)

    async def start(self, ref, *, gui=True):
        vm = self._find(ref)
        self.calls.append(f"start:{vm.name}:gui={gui}")
        return _vm(vm.name, vm.path, VMState.RUNNING)

    async def stop(self, ref, *, mode="soft"):
        vm = self._find(ref)
        self.calls.append(f"stop:{vm.name}:{mode}")
        return _vm(vm.name, vm.path, VMState.STOPPED)

    async def suspend(self, ref):
        vm = self._find(ref)
        self.calls.append(f"suspend:{vm.name}")
        return _vm(vm.name, vm.path, VMState.SUSPENDED)

    async def resume(self, ref):
        vm = self._find(ref)
        self.calls.append(f"resume:{vm.name}")
        return _vm(vm.name, vm.path, VMState.RUNNING)

    async def open_console(self, ref):
        vm = self._find(ref)
        self.calls.append(f"console:{vm.name}")
        return f"Asked the VMware UI to show '{vm.name}'."

    async def guest_ip(self, ref, *, wait=False):
        """Only meaningful for a running VM with Tools; the fake stands in."""
        self._find(ref)
        return "10.0.0.9"


@pytest.fixture
def fake_adapter(monkeypatch):
    """Two VMs, and a handle on the calls the tools actually made."""
    adapter = _FakeAdapter(
        [
            # Ubuntu runs (so it can have a guest address and be exec'd into);
            # Parrot is stopped, which is the state a start preview acts on.
            _vm("Ubuntu", "/vms/u/Ubuntu.vmx", VMState.RUNNING, guest_os="ubuntu-64"),
            _vm("Parrot", "/vms/p/Parrot.vmx", VMState.STOPPED, guest_os="debian-64"),
        ]
    )
    monkeypatch.setattr(vt, "_ADAPTER_FACTORY", lambda: adapter)
    return adapter


# ---------------------------------------------------------------------------
# The read-only routes cannot act
# ---------------------------------------------------------------------------
class TestReadOnlyRoutesCannotAct:
    async def test_a_read_only_dispatch_has_no_approval_channel(self, fake_adapter):
        """The helper the GET handlers use must fail closed on a gated tool."""
        result = await virtualization_api._registry_read("vm_stop", {"vm": "Ubuntu"})
        assert result["success"] is False
        assert result.get("requires_confirmation") is True
        # And nothing was actually stopped.
        assert fake_adapter.calls == []

    async def test_listing_works_without_any_approval(self, client, fake_adapter):
        body = client.get("/virtualization/vms").json()
        assert body["success"] is True
        assert body["count"] == 2
        assert {v["name"] for v in body["vms"]} == {"Ubuntu", "Parrot"}
        assert fake_adapter.calls == []

    async def test_status_needs_no_approval(self, client, fake_adapter):
        body = client.get("/virtualization/vms/one", params={"ref": "Ubuntu"}).json()
        assert body["success"] is True
        assert body["vm"]["name"] == "Ubuntu"

    def test_the_hypervisor_report_names_the_missing_prerequisite(self, client, monkeypatch):
        """With no vmrun anywhere, the answer is a reason and a remedy."""
        import config

        monkeypatch.setattr(config.Config, "VMWARE_VMRUN", "")
        monkeypatch.setattr(config.Config, "VMWARE_BIN_DIRS", "")
        body = client.get("/virtualization/hypervisors").json()
        entry = next(h for h in body["hypervisors"] if h["id"] == "vmware")
        if entry["available"]:  # a developer machine really has VMware
            pytest.skip("VMware is installed on this host")
        assert "vmrun" in entry["reason"]
        assert "VMWARE_VMRUN" in entry["reason"]
        assert body["any_available"] is False

    def test_the_guest_report_never_carries_key_material(self, client, monkeypatch):
        import config

        monkeypatch.setattr(config.Config, "VM_GUEST_USER", "")
        body = client.get("/virtualization/guest").json()
        assert body["configured"] is False
        assert body["identity_set"] is False
        assert any("VM_GUEST_USER" in p for p in body["problems"])
        # No field may carry key material or a credential. `host_key_policy`
        # is a policy name ("strict"), which is configuration rather than a
        # secret, so the check is for the fields that would hold one.
        assert not any("password" in k.lower() for k in body)
        assert not any("private" in k.lower() for k in body)
        assert body.get("identity") in (None, "", False)
        assert isinstance(body["identity_set"], bool)


# ---------------------------------------------------------------------------
# An approval is bound to the previewed operation
# ---------------------------------------------------------------------------
class TestTheApprovalIsBoundToWhatWasPreviewed:
    def test_the_preview_names_the_resolved_machine(self, client, fake_adapter):
        body = client.post(
            "/virtualization/vms/action", json={"action": "start", "ref": "Ubuntu"}
        ).json()
        assert body["requires_confirmation"] is True
        assert body["confirm"].startswith("vm_")
        assert body["vm"]["name"] == "Ubuntu"
        assert body["vm"]["path"] == "/vms/u/Ubuntu.vmx"
        assert body["message"] == "Power on Ubuntu"
        assert body["tier"] == "confirm"
        # The preview must not have powered anything on.
        assert fake_adapter.calls == []

    def test_the_body_of_the_second_call_cannot_change_the_operation(self, client, fake_adapter):
        """The whole point of the two-step: approve one thing, run that thing."""
        preview = client.post(
            "/virtualization/vms/action", json={"action": "start", "ref": "Ubuntu"}
        ).json()
        token = preview["confirm"]

        # A second call that *claims* to be something else entirely.
        response = client.post(
            "/virtualization/vms/action",
            json={
                "action": "stop",
                "ref": "Parrot",
                "mode": "hard",
                "confirm": token,
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True, body
        # What ran is what was previewed: a start of Ubuntu, and only that.
        assert fake_adapter.calls == ["start:Ubuntu:gui=True"]
        assert "stop" not in " ".join(fake_adapter.calls)
        # Both halves. The call that went out was a start; and the *answer*
        # says start rather than repeating the verb the caller claimed.
        assert body["action"] == "start"
        assert body["vm_ref"] == "/vms/u/Ubuntu.vmx"
        assert body["data"]["vm"]["state"] == "running"

    def test_the_preview_resolves_the_name_to_a_path(self, client, fake_adapter):
        """Step 2 acts on the path, so a name that later resolves differently
        cannot redirect it."""
        preview = client.post(
            "/virtualization/vms/action", json={"action": "suspend", "ref": "Parrot"}
        ).json()
        assert preview["params"]["vm"] == "/vms/p/Parrot.vmx"

    def test_a_hard_stop_is_previewed_as_dangerous_with_a_warning(self, client, fake_adapter):
        body = client.post(
            "/virtualization/vms/action",
            json={"action": "stop", "ref": "Ubuntu", "mode": "hard"},
        ).json()
        assert body["tier"] == "dangerous"
        assert body["dangerous"] is True
        assert any("pulls the power" in w for w in body["warnings"])

    def test_a_soft_stop_is_not_dangerous(self, client, fake_adapter):
        body = client.post(
            "/virtualization/vms/action", json={"action": "stop", "ref": "Ubuntu"}
        ).json()
        assert body["tier"] == "confirm"
        assert body["dangerous"] is False

    def test_the_second_call_reports_the_prompt_it_answered(self, client, fake_adapter):
        """The receipt: proof the gate ran rather than being skipped."""
        token = client.post(
            "/virtualization/vms/action", json={"action": "start", "ref": "Ubuntu"}
        ).json()["confirm"]
        body = client.post(
            "/virtualization/vms/action", json={"action": "start", "ref": "Ubuntu", "confirm": token}
        ).json()
        assert body.get("approved_request_id", "").startswith("cfm_")


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------
class TestTokens:
    def test_a_token_is_single_use(self, client, fake_adapter):
        token = client.post(
            "/virtualization/vms/action", json={"action": "start", "ref": "Ubuntu"}
        ).json()["confirm"]
        first = client.post(
            "/virtualization/vms/action", json={"action": "start", "ref": "Ubuntu", "confirm": token}
        )
        assert first.status_code == 200
        assert first.json()["success"] is True, first.json()
        replay = client.post(
            "/virtualization/vms/action", json={"action": "start", "ref": "Ubuntu", "confirm": token}
        )
        assert replay.status_code == 409
        # And the replay did not power anything on a second time.
        assert fake_adapter.calls == ["start:Ubuntu:gui=True"]

    def test_an_unknown_token_is_refused(self, client, fake_adapter):
        response = client.post(
            "/virtualization/vms/action",
            json={"action": "start", "ref": "Ubuntu", "confirm": "vm_deadbeefdead"},
        )
        assert response.status_code == 409
        assert fake_adapter.calls == []

    def test_an_expired_token_is_refused(self, client, fake_adapter):
        import time

        token = client.post(
            "/virtualization/vms/action", json={"action": "start", "ref": "Ubuntu"}
        ).json()["confirm"]
        virtualization_api._VM_APPROVALS.tokens[token] = time.monotonic() - 10_000
        response = client.post(
            "/virtualization/vms/action", json={"action": "start", "ref": "Ubuntu", "confirm": token}
        )
        assert response.status_code == 409
        assert fake_adapter.calls == []

    def test_a_token_from_the_security_probe_cannot_be_spent_here(self, client, fake_adapter):
        """Separate stores: one surface's approval is not another's."""
        from core.api import registry_api

        probe_token = registry_api._PROBE_APPROVALS.issue()
        response = client.post(
            "/virtualization/vms/action",
            json={"action": "start", "ref": "Ubuntu", "confirm": probe_token},
        )
        assert response.status_code == 409
        assert fake_adapter.calls == []

    def test_a_pending_operation_does_not_outlive_its_token(self, client, fake_adapter):
        import time

        token = client.post(
            "/virtualization/vms/action", json={"action": "start", "ref": "Ubuntu"}
        ).json()["confirm"]
        assert token in virtualization_api._PENDING
        virtualization_api._VM_APPROVALS.tokens[token] = time.monotonic() - 10_000
        # The next preview sweeps: an operation whose token is gone is dropped,
        # so the mapping cannot grow without bound.
        client.post("/virtualization/vms/action", json={"action": "start", "ref": "Parrot"})
        assert token not in virtualization_api._PENDING


# ---------------------------------------------------------------------------
# Nothing is guessed
# ---------------------------------------------------------------------------
class TestNothingIsGuessed:
    def test_an_unknown_machine_is_refused_with_the_candidates(self, client, fake_adapter):
        body = client.post(
            "/virtualization/vms/action", json={"action": "start", "ref": "Fedora"}
        ).json()
        assert body["success"] is False
        assert "Ubuntu" in body["error"] and "Parrot" in body["error"]
        # No token is issued for something that could not be resolved.
        assert "confirm" not in body
        assert fake_adapter.calls == []

    @pytest.mark.parametrize("action", ["", "delete", "snapshot-revert", "poweroff", "shutdown"])
    def test_an_unsupported_action_is_a_422(self, client, fake_adapter, action):
        response = client.post(
            "/virtualization/vms/action", json={"action": action, "ref": "Ubuntu"}
        )
        assert response.status_code == 422

    def test_shape_differences_in_the_input_are_tolerated(self, client, fake_adapter):
        """`" START "` is still a start. Being strict about case here would
        reject a caller for no safety gain, so it is accepted and normalised."""
        body = client.post(
            "/virtualization/vms/action", json={"action": " START ", "ref": "Ubuntu"}
        ).json()
        assert body["requires_confirmation"] is True
        assert body["action"] == "start"

    def test_a_missing_reference_is_a_422(self, client, fake_adapter):
        assert client.post("/virtualization/vms/action", json={"action": "start"}).status_code == 422

    def test_a_bad_stop_mode_is_a_422(self, client, fake_adapter):
        response = client.post(
            "/virtualization/vms/action",
            json={"action": "stop", "ref": "Ubuntu", "mode": "hammer"},
        )
        assert response.status_code == 422

    def test_the_advertised_actions_match_what_is_implemented(self, client):
        body = client.get("/virtualization/actions").json()
        assert {a["action"] for a in body["actions"]} == {
            "start",
            "stop",
            "suspend",
            "resume",
            "open_console",
        }
        # Every advertised action resolves to a tool that exists.
        from api import main as api_main

        registry = api_main.tool_executor.registry
        for entry in body["actions"]:
            assert registry.get(entry["tool"]) is not None, entry

    def test_waiting_for_readiness_is_previewed_as_a_guest_dependency(self, client, fake_adapter):
        body = client.post(
            "/virtualization/vms/action",
            json={"action": "start", "ref": "Ubuntu", "wait_for_ready": True},
        ).json()
        assert any("guest account" in w for w in body["warnings"])


# ---------------------------------------------------------------------------
# Guest execution
# ---------------------------------------------------------------------------
class TestGuestExecutionRoutes:
    def test_it_refuses_before_any_network_call_without_an_account(
        self, client, monkeypatch, fake_adapter
    ):
        import config

        monkeypatch.setattr(config.Config, "VM_GUEST_USER", "")
        body = client.post(
            "/virtualization/guest/exec", json={"command": "ip a", "vm": "Ubuntu"}
        ).json()
        assert body["success"] is False
        assert body["configured"] is False
        assert any("VM_GUEST_USER" in p for p in body["problems"])
        # Refused, so no token and no adapter traffic.
        assert "confirm" not in body
        assert fake_adapter.calls == []

    def test_it_asks_for_a_target_rather_than_picking_one(self, client, monkeypatch, fake_adapter):
        import config

        monkeypatch.setattr(config.Config, "VM_GUEST_USER", "lab")
        body = client.post("/virtualization/guest/exec", json={"command": "ip a"}).json()
        assert body["success"] is False
        assert "Name the virtual machine" in body["error"]

    def test_a_configured_account_is_previewed_with_the_exact_command(
        self, client, monkeypatch, fake_adapter
    ):
        import config

        monkeypatch.setattr(config.Config, "VM_GUEST_USER", "lab")
        monkeypatch.setattr(config.Config, "VM_GUEST_IDENTITY", "")
        body = client.post(
            "/virtualization/guest/exec", json={"command": "ip -brief addr", "vm": "Ubuntu"}
        ).json()
        assert body["requires_confirmation"] is True
        assert body["confirm"].startswith("vm_")
        assert body["message"] == "Run in the guest: ip -brief addr"
        assert body["tier"] == "confirm"
        # The command is recorded against the token, not read back from the body.
        tool, params, action = virtualization_api._PENDING[body["confirm"]]
        assert tool == "vm_guest_exec"
        assert action == "guest_exec"
        assert params["command"] == "ip -brief addr"

    def test_the_approved_command_cannot_be_swapped_after_the_fact(
        self, client, monkeypatch, fake_adapter
    ):
        import config

        monkeypatch.setattr(config.Config, "VM_GUEST_USER", "lab")
        monkeypatch.setattr(config.Config, "VM_GUEST_IDENTITY", "")
        token = client.post(
            "/virtualization/guest/exec", json={"command": "ip a", "vm": "Ubuntu"}
        ).json()["confirm"]

        seen = {}

        async def fake_run(self, command, *, timeout=None):
            from core.virtualization.guest import GuestResult

            seen["command"] = command
            return GuestResult(
                target="lab@10.0.0.9", command=command, exit_code=0, stdout="ok\n"
            )

        from core.virtualization import guest as guest_mod

        monkeypatch.setattr(guest_mod.GuestSession, "run", fake_run)
        monkeypatch.setattr(vt, "GuestSession", guest_mod.GuestSession)

        body = client.post(
            "/virtualization/guest/exec",
            json={"command": "rm -rf / --no-preserve-root", "vm": "Ubuntu", "confirm": token},
        ).json()
        # The approved command ran; the substitute was ignored.
        assert seen.get("command") == "ip a", (body, seen)
        assert body.get("where") == "guest"

    def test_a_missing_command_is_a_422(self, client, fake_adapter):
        assert client.post("/virtualization/guest/exec", json={}).status_code == 422
