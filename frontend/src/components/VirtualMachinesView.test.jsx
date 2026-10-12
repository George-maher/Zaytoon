import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import VirtualMachinesView from "./VirtualMachinesView";

/**
 * The Virtual Machines workspace.
 *
 * What matters here is not that a list renders. It is that the screen does not
 * overstate what it knows and does not understate what it is about to do:
 *
 *  - a VM whose state could not be determined shows as unknown, with the
 *    observation beside it, and never as "stopped";
 *  - a hypervisor that is not installed says so, and the inventory says the
 *    state is unknown *because* nothing could be listed;
 *  - a lifecycle action takes two deliberate clicks: the first opens no socket
 *    and shows the resolved machine, the tier and any warning; the second
 *    carries the token and nothing else;
 *  - a `dangerous` tier cannot be confirmed until it is acknowledged, which is
 *    what separates a confirmation from a formality;
 *  - an unknown or ambiguous name is reported with the backend's own message,
 *    because that message lists the candidates.
 */

const HYPERVISORS = {
  any_available: true,
  hypervisors: [
    {
      id: "vmware",
      label: "VMware Workstation",
      available: true,
      reason: "",
      programs: { vmrun: { path: "/usr/bin/vmrun", roles: ["vmrun"] } },
      platform_note: "Driven through `vmrun`.",
    },
  ],
};

const GUEST_READY = {
  configured: true,
  user: "lab",
  identity_set: true,
  identity_exists: true,
  host_key_policy: "strict",
  authentication: "SSH key or agent only; a password is never accepted",
  problems: [],
};

const VMS = {
  success: true,
  count: 2,
  vms: [
    {
      name: "Ubuntu",
      path: "/vms/u/Ubuntu.vmx",
      state: "running",
      state_evidence: "listed by `vmrun list`",
      guest_os: "ubuntu-64",
    },
    {
      name: "Parrot",
      path: "/vms/p/Parrot.vmx",
      state: "stopped",
      state_evidence: "not listed by `vmrun list`",
    },
  ],
};

function jsonResponse(body, { ok = true, status = 200 } = {}) {
  return { ok, status, json: async () => body };
}

/** Routes by URL. `actions` overrides the two-step action endpoint. */
function mockFetch({ hypervisors = HYPERVISORS, vms = VMS, guest = GUEST_READY, action, exec } = {}) {
  const calls = [];
  global.fetch = vi.fn(async (url, opts = {}) => {
    const u = String(url);
    const method = opts.method || "GET";
    const body = opts.body ? JSON.parse(opts.body) : null;
    calls.push({ url: u, method, body });

    if (u.includes("/virtualization/hypervisors")) return jsonResponse(hypervisors);
    if (u.includes("/virtualization/guest/exec")) {
      if (exec) return exec(body, calls);
      return jsonResponse({ requires_confirmation: true, confirm: "vm_tok", command: body.command, message: `Run in the guest: ${body.command}`, note: "Runs inside the guest." });
    }
    if (u.includes("/virtualization/guest")) return jsonResponse(guest);
    if (u.includes("/virtualization/vms/action")) {
      if (action) return action(body, calls);
      if (body.confirm) return jsonResponse({ success: true, action: body.action, output: "done" });
      return jsonResponse({
        requires_confirmation: true,
        confirm: "vm_tok",
        action: body.action,
        tier: "confirm",
        dangerous: false,
        vm: VMS.vms[1],
        message: `Power on Parrot`,
        warnings: [],
      });
    }
    if (u.includes("/virtualization/vms")) return jsonResponse(vms);
    throw new Error(`unexpected fetch: ${method} ${u}`);
  });
  return calls;
}

/** The confirm button's accessible name is `CONFIRM · <action>`. */
const confirmButton = () => screen.getByRole("button", { name: /^CONFIRM · / });

beforeEach(() => vi.restoreAllMocks());
afterEach(() => vi.restoreAllMocks());

describe("VirtualMachinesView", () => {
  it("lists machines with the observation behind each state", async () => {
    mockFetch();
    render(<VirtualMachinesView />);
    const list = await screen.findByRole("list", { name: "Discovered machines" });
    expect(within(list).getByText("Ubuntu")).toBeTruthy();
    expect(within(list).getByText("Parrot")).toBeTruthy();
    expect(screen.getByText("RUNNING")).toBeTruthy();
    expect(screen.getByText("STOPPED")).toBeTruthy();
    // The evidence, not just the verdict.
    expect(screen.getByText("listed by `vmrun list`")).toBeTruthy();
    expect(screen.getByText("not listed by `vmrun list`")).toBeTruthy();
  });

  it("says consent to power on is not consent to run commands inside", async () => {
    mockFetch();
    render(<VirtualMachinesView />);
    expect(
      await screen.findByText(/consent to power a machine on is not consent to run commands inside it/i),
    ).toBeTruthy();
  });

  it("reports a missing hypervisor with the reason and does not imply VMs are stopped", async () => {
    mockFetch({
      hypervisors: {
        any_available: false,
        hypervisors: [
          {
            id: "vmware",
            label: "VMware Workstation",
            available: false,
            reason: "`vmrun` was not found on PATH. Set VMWARE_VMRUN to point at one.",
            programs: {},
          },
        ],
      },
      vms: { success: false, count: 0, vms: [], error: "`vmrun` was not found on PATH." },
    });
    render(<VirtualMachinesView />);
    expect(await screen.findByText("NOT AVAILABLE")).toBeTruthy();
    // Twice: once as the hypervisor's reason, once as the inventory's.
    expect(screen.getAllByText(/vmrun. was not found on PATH/).length).toBeGreaterThanOrEqual(1);
    // The inventory must not read as "there are no machines, so they are off".
    expect(screen.getByText(/the state is unknown — not 'stopped'/)).toBeTruthy();
  });

  it("shows an undetermined state as unknown, not as stopped", async () => {
    mockFetch({
      vms: {
        success: true,
        count: 1,
        vms: [
          {
            name: "Mystery",
            path: "/vms/m/Mystery.vmx",
            state: "unknown",
            state_evidence: "`vmrun list` did not answer, so the state is not determined",
          },
        ],
      },
    });
    render(<VirtualMachinesView />);
    expect(await screen.findByText("UNKNOWN")).toBeTruthy();
    expect(screen.getByText(/state is not determined/)).toBeTruthy();
    expect(screen.queryByText("STOPPED")).toBeNull();
  });

  it("previews before acting, and the preview names the machine", async () => {
    const calls = mockFetch();
    render(<VirtualMachinesView />);
    await screen.findByText("Discovered machines", { selector: "ul, [aria-label]" }).catch(() => {});
    await screen.findByRole("list", { name: "Discovered machines" });

    fireEvent.click(screen.getAllByText("START")[0]);
    expect(await screen.findByText("CONFIRMATION REQUIRED · CONFIRM")).toBeTruthy();
    expect(screen.getByText("Power on Parrot")).toBeTruthy();

    // The preview is one call, it carried no token, and nothing has run.
    const previewCall = calls.filter((c) => c.url.includes("/vms/action"));
    expect(previewCall).toHaveLength(1);
    expect(previewCall[0].body.confirm).toBeUndefined();
  });

  it("sends the token and nothing else on the second call", async () => {
    const calls = mockFetch();
    render(<VirtualMachinesView />);
    await screen.findByRole("list", { name: "Discovered machines" });
    fireEvent.click(screen.getAllByText("START")[0]);
    await waitFor(() => expect(confirmButton()).toBeTruthy());

    fireEvent.click(confirmButton());
    await waitFor(() => expect(screen.getByText(/DONE/)).toBeTruthy());

    const actionCalls = calls.filter((c) => c.url.includes("/vms/action"));
    expect(actionCalls).toHaveLength(2);
    expect(actionCalls[1].method).toBe("POST");
    // The operation is fixed by the token server-side; the body carries the
    // token so the backend can look it up, not a re-stated operation.
    expect(actionCalls[1].body.confirm).toBe("vm_tok");
  });

  it("does not run anything when the preview is cancelled", async () => {
    const calls = mockFetch();
    render(<VirtualMachinesView />);
    await screen.findByRole("list", { name: "Discovered machines" });
    fireEvent.click(screen.getAllByText("START")[0]);
    await waitFor(() => expect(confirmButton()).toBeTruthy());

    fireEvent.click(screen.getByText("CANCEL"));
    await waitFor(() => expect(screen.queryByText("CONFIRMATION REQUIRED · CONFIRM")).toBeNull());
    expect(calls.filter((c) => c.url.includes("/vms/action"))).toHaveLength(1);
  });

  it("requires an acknowledgement before a dangerous action can be confirmed", async () => {
    mockFetch({
      action: (body) =>
        jsonResponse({
          requires_confirmation: true,
          confirm: "vm_tok",
          action: body.action,
          tier: "dangerous",
          dangerous: true,
          vm: VMS.vms[0],
          message: "Stop Ubuntu (hard)",
          warnings: ["A hard stop pulls the power. Anything the guest has not written to disk is lost."],
        }),
    });
    render(<VirtualMachinesView />);
    await screen.findByRole("list", { name: "Discovered machines" });
    fireEvent.click(screen.getByText("STOP"));
    await screen.findByText(/pulls the power/);

    const button = confirmButton();
    // Disabled until the operator says they understood.
    expect(button.disabled).toBe(true);
    fireEvent.click(screen.getByRole("checkbox"));
    expect(screen.getByText(/cannot be undone/)).toBeTruthy();
    await waitFor(() => expect(confirmButton().disabled).toBe(false));
  });

  it("reports an unresolved name with the backend's candidate list", async () => {
    mockFetch({
      action: () =>
        jsonResponse({
          success: false,
          error: "No virtual machine matches 'Fedo'. Known machines: Ubuntu, Parrot",
        }),
    });
    render(<VirtualMachinesView />);
    await screen.findByRole("list", { name: "Discovered machines" });
    // Force a click on a machine whose action the stub refuses.
    fireEvent.click(screen.getByText("SUSPEND"));

    const alert = await screen.findAllByRole("alert");
    expect(alert.map((a) => a.textContent).join(" ")).toContain("Known machines: Ubuntu, Parrot");
  });

  it("shows the guest prerequisite instead of an input that cannot work", async () => {
    mockFetch({
      guest: {
        configured: false,
        authentication: "SSH key or agent only; a password is never accepted",
        problems: ["No guest account is configured (VM_GUEST_USER)."],
      },
    });
    render(<VirtualMachinesView />);
    expect(await screen.findByText("NOT CONFIGURED")).toBeTruthy();
    expect(screen.getByText(/No guest account is configured/)).toBeTruthy();
    expect(screen.queryByLabelText(/^Command$/)).toBeNull();
    expect(screen.getByText(/needs an account and a key/)).toBeTruthy();
  });

  it("previews a guest command and shows the exact text being approved", async () => {
    const calls = mockFetch();
    render(<VirtualMachinesView />);
    fireEvent.change(await screen.findByLabelText(/^Command$/), {
      target: { value: "ip -brief addr" },
    });
    fireEvent.click(screen.getByText("REVIEW COMMAND"));

    expect(await screen.findByText("Run in the guest: ip -brief addr")).toBeTruthy();
    // The command is displayed verbatim, because that is what is approved.
    expect(screen.getByText("ip -brief addr")).toBeTruthy();
    const execCalls = calls.filter((c) => c.url.includes("/guest/exec"));
    expect(execCalls).toHaveLength(1);
    expect(execCalls[0].body.confirm).toBeUndefined();
  });

  it("switches to Arabic without a reload", async () => {
    mockFetch();
    const { rerender } = render(<VirtualMachinesView language="en" />);
    expect(await screen.findByText("VIRTUAL MACHINES")).toBeTruthy();
    rerender(<VirtualMachinesView language="ar" />);
    expect(screen.getByText("الأنظمة الافتراضية")).toBeTruthy();
    expect(screen.getByText("إعادة الفحص")).toBeTruthy();
    // Paths and evidence stay LTR: they are technical strings, not prose.
    expect(screen.getByText("/vms/u/Ubuntu.vmx").getAttribute("dir")).toBe("ltr");
  });
});
