/**
 * The provider registry section and the first-run wizard — the two surfaces
 * that talk to `/providers`.
 *
 * What is worth testing here is the *honesty* seams, because each one is a
 * way the UI could lie:
 *
 * - the verification badge must come from the two stamps the backend keeps
 *   (`chat_verified_at` beats `verified_at`, and neither is "connected") —
 *   a wizard that completes on a reachability probe is a setup that reports
 *   success for a key that cannot generate;
 * - a provider payload must never put key material on screen — the registry
 *   carries `has_api_key` only, and a stray `api_key` in a payload is not
 *   rendered either (write-only, in both directions);
 * - a model refused by policy shows its reason and **no toggle** — the store
 *   writes such rows disabled whatever `enabled` says, so a switch there
 *   could never stay on;
 * - every failure is rendered verbatim, and a chat-test 404 (a backend that
 *   predates the route) reads as "unavailable", not as a broken provider;
 * - the wizard renders nothing at all unless `setup_required === true` —
 *   `null` (the value `getSetupStatus` returns on any failure) is not
 *   evidence that setup is missing;
 * - "Skip for now" writes `zaytoon.setup.dismissed` to sessionStorage and
 *   hides the wizard for this session only.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import ProvidersSection from "./ProvidersSection";
import SetupWizard from "./SetupWizard";
import * as api from "../../lib/api";

/**
 * The key "Skip for now" writes. Pinned as a literal rather than imported:
 * the component deliberately does not export it (a non-component export next
 * to a component trips react-refresh's only-export-components rule, and zero
 * lint warnings is the bar), so the string is the contract.
 */
const SETUP_DISMISSED_KEY = "zaytoon.setup.dismissed";

const OLLAMA = {
  id: "local-ollama",
  name: "Ollama (local)",
  kind: "ollama",
  base_url: "http://localhost:11434",
  requires_key: false,
  has_api_key: false,
  enabled: true,
  is_default: true,
  verified_at: null,
  chat_verified_at: null,
};

const CLOUD = {
  id: "openrouter",
  name: "OpenRouter",
  kind: "openrouter",
  base_url: "https://openrouter.ai/api/v1",
  requires_key: true,
  has_api_key: true,
  enabled: true,
  is_default: false,
  verified_at: "2026-01-01T00:00:00Z",
  chat_verified_at: null,
};

const FREE_MODEL = {
  id: "qwen/qwen3.8-27b:free",
  provider_id: "openrouter",
  name: "qwen/qwen3.8-27b:free",
  capabilities: ["chat", "tools"],
  context_length: 131072,
  enabled: true,
  priority: 0,
  metadata: { pricing: { prompt: "0", completion: "0" } },
  policy_reason: null,
  discovered_at: "2026-01-01T00:00:00Z",
};

const PAID_MODEL = {
  id: "openai/gpt-4o",
  provider_id: "openrouter",
  name: "openai/gpt-4o",
  capabilities: ["chat"],
  context_length: null,
  enabled: false,
  priority: 10,
  metadata: { pricing: { prompt: "0.0000025", completion: "0.00001" } },
  policy_reason: "Billable model on a free-only registry",
  discovered_at: "2026-01-01T00:00:00Z",
};

function respond(overrides = {}) {
  return {
    getProviders: vi.fn(async () => ({ providers: [OLLAMA, CLOUD] })),
    getSetupStatus: vi.fn(async () => ({ setup_required: false })),
    saveProvider: vi.fn(async (payload) => ({ ...OLLAMA, ...payload })),
    deleteProvider: vi.fn(async () => ({ ok: true })),
    testProvider: vi.fn(async () => ({ connected: true, latency_ms: 12 })),
    testChatProvider: vi.fn(async () => ({
      ok: true,
      connected: true,
      provider_id: "openrouter",
      provider: "OpenRouter",
      model: "qwen/qwen3.8-27b:free",
      latency_ms: 420,
      reply: "OK",
    })),
    refreshModels: vi.fn(async () => ({
      discovered: 3,
      added: 2,
      refused_by_policy: 1,
      skipped: 0,
    })),
    getModels: vi.fn(async () => ({ models: [FREE_MODEL, PAID_MODEL] })),
    saveModel: vi.fn(async () => ({ status: "saved" })),
    ...overrides,
  };
}

function mockApi(stubs) {
  vi.spyOn(api, "getProviders").mockImplementation(stubs.getProviders);
  vi.spyOn(api, "getSetupStatus").mockImplementation(stubs.getSetupStatus);
  vi.spyOn(api, "saveProvider").mockImplementation(stubs.saveProvider);
  vi.spyOn(api, "deleteProvider").mockImplementation(stubs.deleteProvider);
  vi.spyOn(api, "testProvider").mockImplementation(stubs.testProvider);
  vi.spyOn(api, "testChatProvider").mockImplementation(stubs.testChatProvider);
  vi.spyOn(api, "refreshModels").mockImplementation(stubs.refreshModels);
  vi.spyOn(api, "getModels").mockImplementation(stubs.getModels);
  vi.spyOn(api, "saveModel").mockImplementation(stubs.saveModel);
}

beforeEach(() => {
  vi.restoreAllMocks();
  window.sessionStorage.clear();
});

// ---------------------------------------------------------------------------
describe("ProvidersSection", () => {
  it("shows verification from the two stamps, and never a key value", async () => {
    const stubs = respond();
    mockApi(stubs);
    const { container } = render(<ProvidersSection language="en" />);

    await screen.findByText("Ollama (local)");
    expect(screen.getByText("UNVERIFIED")).toBeInTheDocument();
    expect(screen.getByText("REACHABLE")).toBeInTheDocument();
    expect(screen.getByText("KEY SET")).toBeInTheDocument();
    expect(screen.getByText("DEFAULT")).toBeInTheDocument();

    // `has_api_key` is the proof; the value is not in the payload at all.
    expect(container.textContent).not.toMatch(/sk-[A-Za-z0-9]/);
  });

  it("renders a stray api_key from a payload as nothing", async () => {
    // The backend has no route that returns key material, but the contract
    // that this UI cannot display one has to hold even if one arrived.
    const leaky = { ...CLOUD, api_key: "sk-should-never-be-rendered" };
    mockApi(respond({ getProviders: vi.fn(async () => ({ providers: [leaky] })) }));
    const { container } = render(<ProvidersSection language="en" />);

    await screen.findByText("OpenRouter");
    expect(container.textContent).not.toContain("sk-should-never-be-rendered");
  });

  it("marks a chat-verified provider VERIFIED, above REACHABLE", async () => {
    const proven = { ...CLOUD, chat_verified_at: "2026-01-02T00:00:00Z" };
    mockApi(respond({ getProviders: vi.fn(async () => ({ providers: [proven] })) }));
    render(<ProvidersSection language="en" />);

    await screen.findByText("VERIFIED");
    expect(screen.queryByText("REACHABLE")).not.toBeInTheDocument();
  });

  it("refuses a badge for an enabled=false provider", async () => {
    mockApi(
      respond({ getProviders: vi.fn(async () => ({ providers: [{ ...OLLAMA, enabled: false }] })) }),
    );
    render(<ProvidersSection language="en" />);

    await screen.findByText("DISABLED");
  });

  it("shows a refused model's reason and gives it no toggle", async () => {
    // One provider in the fixture: the row's own buttons must be unambiguous.
    mockApi(respond({ getProviders: vi.fn(async () => ({ providers: [CLOUD] })) }));
    render(<ProvidersSection language="en" />);
    fireEvent.click(await screen.findByRole("button", { name: "MODELS" }));

    // The refused row: reason verbatim, no switch — the store forces it off.
    await screen.findByText(/Billable model on a free-only registry/);
    expect(screen.queryByLabelText("Enable openai/gpt-4o")).not.toBeInTheDocument();
    // The allowed row next to it does get one, so the absence above is a
    // decision and not a missing render.
    expect(screen.getByLabelText("Enable qwen/qwen3.8-27b:free")).toBeInTheDocument();
  });

  it("shows the refresh summary in the backend's own numbers", async () => {
    const stubs = respond({ getProviders: vi.fn(async () => ({ providers: [OLLAMA] })) });
    mockApi(stubs);
    render(<ProvidersSection language="en" />);
    fireEvent.click(await screen.findByRole("button", { name: "REFRESH MODELS" }));

    await waitFor(() =>
      expect(screen.getByText(/discovered 3, recorded 2, 1 refused by policy/)).toBeInTheDocument(),
    );
    expect(stubs.refreshModels).toHaveBeenCalledWith("local-ollama");
  });

  it("renders a chat-test failure verbatim", async () => {
    const stubs = respond({
      getProviders: vi.fn(async () => ({ providers: [OLLAMA] })),
      testChatProvider: vi.fn(async () => ({
        ok: false,
        connected: false,
        error: "No models recorded for this provider — refresh its models first.",
      })),
    });
    mockApi(stubs);
    render(<ProvidersSection language="en" />);
    fireEvent.click(await screen.findByRole("button", { name: "TEST CHAT" }));

    expect(
      await screen.findByText("No models recorded for this provider — refresh its models first."),
    ).toBeInTheDocument();
  });

  it("reads a chat-test 404 as unavailable, not as a broken provider", async () => {
    mockApi(
      respond({
        getProviders: vi.fn(async () => ({ providers: [OLLAMA] })),
        testChatProvider: vi.fn(async () => ({ ok: false, connected: false, error: "HTTP 404" })),
      }),
    );
    render(<ProvidersSection language="en" />);
    fireEvent.click(await screen.findByRole("button", { name: "TEST CHAT" }));

    expect(await screen.findByText(/Chat test unavailable/)).toBeInTheDocument();
    expect(screen.queryByText("HTTP 404")).not.toBeInTheDocument();
  });

  it("re-saves the whole row when toggling the default, key included by id only", async () => {
    const stubs = respond();
    mockApi(stubs);
    render(<ProvidersSection language="en" />);
    fireEvent.click(await screen.findByRole("button", { name: "SET DEFAULT" }));

    await waitFor(() => expect(stubs.saveProvider).toHaveBeenCalled());
    const payload = stubs.saveProvider.mock.calls[0][0];
    // The full row, because save_provider's defaults are "not keep".
    expect(payload).toMatchObject({
      id: "openrouter",
      name: "OpenRouter",
      kind: "openrouter",
      base_url: "https://openrouter.ai/api/v1",
      requires_key: true,
      enabled: true,
      is_default: true,
    });
    // And never the credential: an absent key means "keep the stored one".
    expect(payload).not.toHaveProperty("api_key");
  });

  it("deletes only on the second click", async () => {
    const stubs = respond({ getProviders: vi.fn(async () => ({ providers: [OLLAMA] })) });
    mockApi(stubs);
    render(<ProvidersSection language="en" />);
    const del = await screen.findByRole("button", { name: "DELETE" });

    fireEvent.click(del);
    expect(stubs.deleteProvider).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "CONFIRM DELETE" }));
    await waitFor(() => expect(stubs.deleteProvider).toHaveBeenCalledWith("local-ollama"));
  });

  it("says nothing when the setup status could not be read", async () => {
    // `null` is the failure value: unreadable is not "setup required".
    mockApi(
      respond({ getSetupStatus: vi.fn(async () => ({ setup_required: null, error: "HTTP 404" })) }),
    );
    render(<ProvidersSection language="en" />);

    await screen.findByText("Ollama (local)");
    expect(screen.queryByText(/First-run setup is not complete/)).not.toBeInTheDocument();
  });

  it("shows the setup note only when setup is genuinely required", async () => {
    mockApi(
      respond({ getSetupStatus: vi.fn(async () => ({ setup_required: true })) }),
    );
    render(<ProvidersSection language="en" />);

    expect(await screen.findByText(/First-run setup is not complete/)).toBeInTheDocument();
  });

  it("surfaces a provider-list failure verbatim instead of an empty list", async () => {
    mockApi(
      respond({ getProviders: vi.fn(async () => ({ providers: [], error: "HTTP 500" })) }),
    );
    render(<ProvidersSection language="en" />);

    expect(await screen.findByRole("alert")).toHaveTextContent("HTTP 500");
  });
});

// ---------------------------------------------------------------------------
describe("SetupWizard", () => {
  it("renders nothing when setup is complete", async () => {
    mockApi(respond());
    const { container } = render(<SetupWizard language="en" />);
    await waitFor(() => expect(api.getSetupStatus).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when the status could not be read", async () => {
    // The failure contract: getSetupStatus returns null, never true.
    mockApi(
      respond({ getSetupStatus: vi.fn(async () => ({ setup_required: null, error: "HTTP 404" })) }),
    );
    const { container } = render(<SetupWizard language="en" />);
    await waitFor(() => expect(api.getSetupStatus).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when already skipped this session", async () => {
    window.sessionStorage.setItem(SETUP_DISMISSED_KEY, "1");
    mockApi(
      respond({ getSetupStatus: vi.fn(async () => ({ setup_required: true })) }),
    );
    const { container } = render(<SetupWizard language="en" />);
    await waitFor(() => expect(api.getSetupStatus).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
    // And it does not even ask, because the flag is read before the request.
    expect(screen.queryByText("FIRST-RUN SETUP")).not.toBeInTheDocument();
  });

  it("offers the three paths when setup is required", async () => {
    mockApi(respond({ getSetupStatus: vi.fn(async () => ({ setup_required: true })) }));
    render(<SetupWizard language="en" />);

    expect(await screen.findByText("FIRST-RUN SETUP")).toBeInTheDocument();
    expect(screen.getByText("Local Ollama")).toBeInTheDocument();
    expect(screen.getByText("OpenRouter")).toBeInTheDocument();
    expect(screen.getByText("Custom endpoint")).toBeInTheDocument();
  });

  it("remembers a skip for this session only", async () => {
    mockApi(respond({ getSetupStatus: vi.fn(async () => ({ setup_required: true })) }));
    const { container } = render(<SetupWizard language="en" />);
    fireEvent.click(await screen.findByRole("button", { name: "Skip for now" }));

    expect(window.sessionStorage.getItem(SETUP_DISMISSED_KEY)).toBe("1");
    expect(container).toBeEmptyDOMElement();
  });

  it("updates an existing provider of the same kind instead of duplicating it", async () => {
    const stubs = respond({
      getSetupStatus: vi.fn(async () => ({ setup_required: true })),
      getProviders: vi.fn(async () => ({ providers: [CLOUD] })),
    });
    mockApi(stubs);
    render(<SetupWizard language="en" />);
    fireEvent.click(await screen.findByRole("button", { name: /OpenRouter/ }));

    // Prefilled from the existing row, key field included but empty.
    expect(await screen.findByLabelText("BASE URL")).toHaveValue("https://openrouter.ai/api/v1");
    fireEvent.change(screen.getByLabelText("API KEY (never shown again)"), {
      target: { value: "sk-test-key" },
    });
    fireEvent.click(screen.getByRole("button", { name: "SAVE & CONTINUE" }));

    await waitFor(() => expect(stubs.saveProvider).toHaveBeenCalled());
    const payload = stubs.saveProvider.mock.calls[0][0];
    expect(payload).toMatchObject({ id: "openrouter", kind: "openrouter" });
    // The key travels once; the form never holds a stored one.
    expect(payload.api_key).toBe("sk-test-key");
  });

  it("completes only on a real chat test, and says so with the reply", async () => {
    const stubs = respond({ getSetupStatus: vi.fn(async () => ({ setup_required: true })) });
    mockApi(stubs);
    render(<SetupWizard language="en" />);

    fireEvent.click(await screen.findByRole("button", { name: /Local Ollama/ }));
    fireEvent.change(screen.getByLabelText("NAME"), { target: { value: "Ollama (local)" } });
    fireEvent.click(screen.getByRole("button", { name: "SAVE & CONTINUE" }));

    // Verify step: refresh first (the chat test needs a recorded model)…
    fireEvent.click(await screen.findByRole("button", { name: "VERIFY" }));
    await waitFor(() => expect(stubs.refreshModels).toHaveBeenCalled());
    await waitFor(() => expect(stubs.testChatProvider).toHaveBeenCalled());

    // …then the completion itself is the gate.
    expect(await screen.findByText("Done — provider verified")).toBeInTheDocument();
    // The done step names what actually answered — provider, model, latency
    // and the reply itself — because "verified" alone is a claim.
    expect(screen.getByText(/420ms — «OK»/)).toBeInTheDocument();
  });

  it("stays on the verify step when the chat test fails, verbatim", async () => {
    const stubs = respond({
      getSetupStatus: vi.fn(async () => ({ setup_required: true })),
      testChatProvider: vi.fn(async () => ({
        ok: false,
        connected: false,
        error: "MODEL NOT INSTALLED: 'qwen3:9b' — install it with `ollama pull qwen3:9b`",
      })),
    });
    mockApi(stubs);
    render(<SetupWizard language="en" />);

    fireEvent.click(await screen.findByRole("button", { name: /Local Ollama/ }));
    fireEvent.click(screen.getByRole("button", { name: "SAVE & CONTINUE" }));
    fireEvent.click(await screen.findByRole("button", { name: "VERIFY" }));

    expect(
      await screen.findByText(/MODEL NOT INSTALLED: 'qwen3:9b' — install it with `ollama pull qwen3:9b`/),
    ).toBeInTheDocument();
    expect(screen.queryByText("Done — provider verified")).not.toBeInTheDocument();
    // And it offers the honest retry rather than pretending to finish.
    expect(screen.getByRole("button", { name: "RETEST" })).toBeInTheDocument();
  });

  it("shows a save rejection verbatim and keeps the form", async () => {
    mockApi(
      respond({
        getSetupStatus: vi.fn(async () => ({ setup_required: true })),
        saveProvider: vi.fn(async () => ({
          error: "'Ollama (local)' needs an API key. Provide one, or clear 'requires_key'.",
        })),
      }),
    );
    render(<SetupWizard language="en" />);

    fireEvent.click(await screen.findByRole("button", { name: /Local Ollama/ }));
    fireEvent.click(screen.getByRole("button", { name: "SAVE & CONTINUE" }));

    expect(
      await screen.findByText(/needs an API key\. Provide one, or clear 'requires_key'\./),
    ).toBeInTheDocument();
    // Still on step 2: the user can fix the key without starting over.
    expect(screen.getByLabelText("NAME")).toBeInTheDocument();
  });
});
