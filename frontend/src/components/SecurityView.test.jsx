import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import SecurityView from "./SecurityView";

/**
 * The Security Workspace is the only place an active assessment becomes
 * permitted, so the tests are about the boundary rather than the layout:
 *
 *  - a scope refusal is rendered as a refusal, in the backend's own words,
 *    and never as an empty result set — "no findings" and "not allowed to
 *    look" are different answers and must not look alike;
 *  - the probe is genuinely two-step: the first request carries no `confirm`,
 *    and the second carries the token the *backend* issued, not one the page
 *    invented. That is what keeps the confirmation real rather than cosmetic;
 *  - a refused authorisation (a whole-internet wildcard, a public suffix) is
 *    surfaced verbatim: the backend knows why it refused and the user needs
 *    that reason, not a generic failure;
 *  - an expired token is a reportable error, not a crash and not a silent
 *    success;
 *  - every finding on screen carries its evidence and remediation, because a
 *    finding without evidence is an assertion.
 */

const LIST = {
  active_count: 1,
  max_ttl_days: 30,
  operations: ["http.probe", "http.fetch", "port.probe", "local.scan"],
  authorizations: [
    {
      id: "auth_8c3bcdad7285",
      target: "127.0.0.1",
      operations: ["http.probe"],
      note: "lab",
      created_at: "2026-10-12T05:51:55+00:00",
      expires_at: null,
      revoked_at: null,
      active: true,
      expired: false,
    },
  ],
};

function jsonResponse(body, { ok = true, status = 200 } = {}) {
  return { ok, status, json: async () => body };
}

function mockFetch(handlers = {}) {
  const calls = [];
  global.fetch = vi.fn(async (url, opts = {}) => {
    const u = String(url);
    const method = opts.method || "GET";
    const body = opts.body ? JSON.parse(opts.body) : null;
    calls.push({ url: u, method, body });
    for (const [match, handler] of Object.entries(handlers)) {
      if (u.includes(match)) return handler(u, { method, body, calls });
    }
    if (u.includes("/security/authorizations")) {
      if (method === "POST") return jsonResponse({ id: "auth_new", ...body });
      if (method === "DELETE") return jsonResponse({ id: "x", revoked: true });
      return jsonResponse(LIST);
    }
    throw new Error(`unexpected fetch: ${method} ${u}`);
  });
  return calls;
}

beforeEach(() => {
  vi.restoreAllMocks();
});
afterEach(() => {
  vi.restoreAllMocks();
});

describe("SecurityView", () => {
  it("lists recorded authorisations with their operations and state", async () => {
    mockFetch();
    render(<SecurityView />);
    expect(await screen.findByText("127.0.0.1")).toBeTruthy();
    // Twice, and both are wanted: once as a checkbox in the authorise form
    // (the vocabulary the backend accepts) and once on the recorded row
    // (what this authorisation actually covers).
    expect(screen.getAllByText("http.probe")).toHaveLength(2);
    expect(screen.getByText("ACTIVE")).toBeTruthy();
  });

  it("offers every operation the backend says exists, not a hard-coded list", async () => {
    mockFetch();
    render(<SecurityView />);
    // The vocabulary comes from `GET /security/authorizations`. If the client
    // duplicated it, adding an operation server-side would produce a checkbox
    // the backend then rejects.
    expect(await screen.findByText("port.probe")).toBeTruthy();
    expect(screen.getByText("local.scan")).toBeTruthy();
  });

  it("says active testing is refused when no authorisation exists", async () => {
    mockFetch({
      "/security/authorizations": () =>
        jsonResponse({ ...LIST, active_count: 0, authorizations: [] }),
    });
    render(<SecurityView />);
    expect(await screen.findByText("None. Active testing is refused until one exists.")).toBeTruthy();
  });

  it("renders a scope refusal as a refusal, with the backend's reason", async () => {
    mockFetch({
      "/security/probe": () =>
        jsonResponse({
          refused: true,
          reason: "out_of_scope",
          message: "Refused: 'example.com' is not authorised for 'http.probe'.",
        }),
    });
    render(<SecurityView />);
    const input = await screen.findByLabelText(/URL/i);
    fireEvent.change(input, { target: { value: "http://example.com/admin" } });
    fireEvent.click(screen.getByText("RUN PROBE"));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("not authorised for 'http.probe'");
    // A refusal must not be dressed up as a completed assessment.
    expect(screen.queryByText(/HTTP 200/)).toBeNull();
  });

  it("runs the probe in two steps and sends back the token the backend issued", async () => {
    mockFetch({
      "/security/probe": (u, { body }) => {
        if (!body.confirm) {
          return jsonResponse({
            requires_confirmation: true,
            confirm: "probe_c5f66967da9e",
            host: "127.0.0.1",
            authorization_id: "auth_8c3bcdad7285",
            authorized_target: "127.0.0.1",
            message: "Sends one non-destructive GET to 127.0.0.1.",
          });
        }
        return jsonResponse({
          success: true,
          output: "Authorised assessment",
          data: {
            status: 200,
            authorization_id: "auth_8c3bcdad7285",
            findings: [
              {
                id: "header.missing.content-security-policy",
                title: "content-security-policy is not set",
                severity: "medium",
                confidence: "confirmed",
                evidence: "HTTP 200; the response headers received do not include content-security-policy.",
                remediation: "Set the content-security-policy response header.",
              },
            ],
          },
        });
      },
    });
    render(<SecurityView />);
    const input = await screen.findByLabelText(/URL/i);
    fireEvent.change(input, { target: { value: "http://127.0.0.1:8099/admin" } });
    fireEvent.click(screen.getByText("RUN PROBE"));

    await waitFor(() => expect(screen.getByText(/content-security-policy is not set/)).toBeTruthy());

    const probes = global.fetch.mock.calls.filter(([u]) => String(u).includes("/security/probe"));
    expect(probes).toHaveLength(2);
    // Step 1 asks; it must not carry an approval it was never given.
    expect(JSON.parse(probes[0][1].body).confirm).toBeUndefined();
    // Step 2 answers, with the issued token — not a value the page chose.
    expect(JSON.parse(probes[1][1].body).confirm).toBe("probe_c5f66967da9e");
  });

  it("shows a finding only with its evidence and remediation", async () => {
    mockFetch({
      "/security/probe": (u, { body }) =>
        body.confirm
          ? jsonResponse({
              success: true,
              data: {
                status: 200,
                authorization_id: "auth_8c3bcdad7285",
                findings: [
                  {
                    id: "disclosure.server",
                    title: "server discloses a software version",
                    severity: "info",
                    confidence: "confirmed",
                    evidence: "server: LabServer/1.2.3",
                    remediation: "Suppress or genericise server.",
                  },
                ],
              },
            })
          : jsonResponse({ confirm: "probe_tok", authorization_id: "auth_8c3bcdad7285" }),
    });
    render(<SecurityView />);
    fireEvent.change(await screen.findByLabelText(/URL/i), {
      target: { value: "http://127.0.0.1:8099/" },
    });
    fireEvent.click(screen.getByText("RUN PROBE"));

    expect(await screen.findByText("server: LabServer/1.2.3")).toBeTruthy();
    expect(screen.getByText("Suppress or genericise server.")).toBeTruthy();
    expect(screen.getByText("INFO")).toBeTruthy();
  });

  it("reports an expired confirmation token instead of pretending it ran", async () => {
    let n = 0;
    mockFetch({
      "/security/probe": () => {
        n += 1;
        if (n === 1) return jsonResponse({ confirm: "probe_dead", authorization_id: "auth_1" });
        return jsonResponse({ error: "Unknown or expired confirmation token. Request the probe again." }, { ok: false, status: 409 });
      },
    });
    render(<SecurityView />);
    fireEvent.change(await screen.findByLabelText(/URL/i), {
      target: { value: "http://127.0.0.1:8099/admin" },
    });
    fireEvent.click(screen.getByText("RUN PROBE"));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("expired confirmation token");
  });

  it("surfaces the reason a target was refused for authorisation, verbatim", async () => {
    mockFetch({
      "/security/authorizations": (u, { method, body }) => {
        if (method === "POST") {
          return jsonResponse(
            { error: "a wildcard for every host is not an authorization. Name the target." },
            { ok: false, status: 422 },
          );
        }
        return jsonResponse(LIST);
      },
    });
    render(<SecurityView />);
    const target = await screen.findByLabelText(/^Target$/i);
    fireEvent.change(target, { target: { value: "*" } });
    fireEvent.click(screen.getByText("AUTHORISE"));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("is not an authorization");
  });

  it("revokes by id and refreshes the list", async () => {
    let revoked = false;
    const calls = mockFetch({
      "/security/authorizations": (u, { method, calls: c }) => {
        if (method === "DELETE") {
          revoked = true;
          return jsonResponse({ id: "auth_8c3bcdad7285", revoked: true });
        }
        const list = revoked
          ? { ...LIST, active_count: 0, authorizations: [{ ...LIST.authorizations[0], active: false, revoked_at: "now" }] }
          : LIST;
        expect(c).toBeTruthy();
        return jsonResponse(list);
      },
    });
    render(<SecurityView />);
    fireEvent.click(await screen.findByText("REVOKE"));

    await waitFor(() => expect(screen.getByText("REVOKED")).toBeTruthy());
    const del = calls.find((c) => c.method === "DELETE");
    expect(del.url).toContain("auth_8c3bcdad7285");
  });

  it("will not send a probe or an authorisation it has no input for", async () => {
    mockFetch();
    render(<SecurityView />);
    await screen.findByText("127.0.0.1");
    expect(screen.getByText("RUN PROBE").disabled).toBe(true);
    expect(screen.getByText("AUTHORISE").disabled).toBe(true);
    const probes = global.fetch.mock.calls.filter(([u]) => String(u).includes("/security/probe"));
    expect(probes).toHaveLength(0);
  });

  it("switches every label to Arabic without a reload", async () => {
    mockFetch();
    const { rerender } = render(<SecurityView language="en" />);
    expect(await screen.findByText("SECURITY WORKSPACE")).toBeTruthy();
    rerender(<SecurityView language="ar" />);
    expect(screen.getByText("مساحة الأمان")).toBeTruthy();
    expect(screen.getByText("تشغيل الفحص")).toBeTruthy();
    // The probe URL stays LTR: it is a technical string, not prose.
    expect(screen.getByLabelText(/الرابط/).getAttribute("dir")).toBe("ltr");
  });

  it("states the boundary of what it checked", async () => {
    mockFetch();
    render(<SecurityView />);
    expect(
      await screen.findByText(/One non-destructive request\./),
    ).toBeTruthy();
    expect(
      screen.getByText(/Enforced in the execution layer, not in model instructions\./),
    ).toBeTruthy();
  });
});
