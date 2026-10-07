/**
 * The models panel: tier pins, capability probes, and an explicit pull.
 *
 * What is worth testing here is the *honesty* seams, because each one is a
 * way the UI could lie:
 *
 * - a tier dropdown must send split ids (and `null,null` to clear) — a
 *   silently malformed pin would steer the router to nothing;
 * - a backend without the tiers route must hide the section, not raise a
 *   red banner at a working router;
 * - probe buttons exist only where probing can work (ollama), and a
 *   refusal ("pull it first") is shown as the instruction it is;
 * - a pull is started only by a real submit, polls until terminal, and a
 *   409 arrives as an answer rather than a retry.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import ModelsPanel from "./ModelsPanel";
import * as api from "../../lib/api";

const OLLAMA = {
  id: "local",
  name: "Local Ollama",
  kind: "ollama",
  base_url: "http://localhost:11434",
  is_default: true,
  has_api_key: false,
};
const CLOUD = {
  id: "cloud",
  name: "Cloud",
  kind: "openai_compatible",
  base_url: "https://api.example.com",
  is_default: false,
  has_api_key: false,
};
const LOCAL_MODEL = {
  id: "qwen3:9b",
  provider_id: "local",
  name: "qwen3:9b",
  capabilities: ["chat", "tools"],
  enabled: true,
  priority: 100,
  context_length: null,
  discovered_at: null,
};
const CLOUD_MODEL = {
  id: "vendor/remote:free",
  provider_id: "cloud",
  name: "vendor/remote:free",
  capabilities: ["chat"],
  enabled: true,
  priority: 100,
  context_length: null,
  discovered_at: null,
};

function respond(overrides = {}) {
  return {
    getProviders: vi.fn(async () => ({ providers: [OLLAMA] })),
    getModels: vi.fn(async () => ({ models: [LOCAL_MODEL] })),
    getTierAssignments: vi.fn(async () => ({
      tiers: { fast: null, coding: null, reasoning: null, default: null },
      available: ["fast", "coding", "reasoning", "default"],
    })),
    getRoutingPreview: vi.fn(async () => ({
      model: "qwen3:9b",
      source: "priority",
      candidates: [],
    })),
    saveProvider: vi.fn(async () => ({})),
    testProvider: vi.fn(async () => ({ connected: true })),
    refreshModels: vi.fn(async () => ({})),
    deleteProvider: vi.fn(async () => ({})),
    saveModel: vi.fn(async () => ({})),
    setTierAssignment: vi.fn(async () => ({ status: "saved" })),
    probeModel: vi.fn(async () => ({ capabilities: ["chat", "tools"] })),
    pullModel: vi.fn(async () => ({
      status: "started",
      model: "qwen3:9b",
      state: "running",
    })),
    getPullStatus: vi.fn(async () => ({
      model: "qwen3:9b",
      state: "done",
      status: "success",
      total: 0,
      completed: 0,
      error: null,
    })),
    ...overrides,
  };
}

function mockApi(stubs) {
  vi.spyOn(api, "getProviders").mockImplementation(stubs.getProviders);
  vi.spyOn(api, "getModels").mockImplementation(stubs.getModels);
  vi.spyOn(api, "getTierAssignments").mockImplementation(stubs.getTierAssignments);
  vi.spyOn(api, "getRoutingPreview").mockImplementation(stubs.getRoutingPreview);
  vi.spyOn(api, "saveProvider").mockImplementation(stubs.saveProvider);
  vi.spyOn(api, "testProvider").mockImplementation(stubs.testProvider);
  vi.spyOn(api, "refreshModels").mockImplementation(stubs.refreshModels);
  vi.spyOn(api, "deleteProvider").mockImplementation(stubs.deleteProvider);
  vi.spyOn(api, "saveModel").mockImplementation(stubs.saveModel);
  vi.spyOn(api, "setTierAssignment").mockImplementation(stubs.setTierAssignment);
  vi.spyOn(api, "probeModel").mockImplementation(stubs.probeModel);
  vi.spyOn(api, "pullModel").mockImplementation(stubs.pullModel);
  vi.spyOn(api, "getPullStatus").mockImplementation(stubs.getPullStatus);
}

describe("ModelsPanel tier assignments", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("shows one dropdown per tier the backend knows", async () => {
    mockApi(respond());
    render(<ModelsPanel />);
    expect(await screen.findByText("TIER ASSIGNMENTS")).toBeInTheDocument();
    expect(screen.getByLabelText("Fast chat")).toBeInTheDocument();
    expect(screen.getByLabelText("Coding")).toBeInTheDocument();
    expect(screen.getByLabelText("Reasoning")).toBeInTheDocument();
    expect(screen.getByLabelText("Default")).toBeInTheDocument();
    // "auto" is the default and the common case — present on every dropdown.
    expect(screen.getByLabelText("Fast chat")).toHaveValue("");
  });

  it("sends split ids when a tier is pinned", async () => {
    const stubs = respond();
    mockApi(stubs);
    render(<ModelsPanel />);
    const select = await screen.findByLabelText("Fast chat");

    fireEvent.change(select, { target: { value: "local|qwen3:9b" } });

    await waitFor(() =>
      expect(stubs.setTierAssignment).toHaveBeenCalledWith(
        "fast",
        "local",
        "qwen3:9b",
      ),
    );
  });

  it("clears a pin back to auto with no ids", async () => {
    const stubs = respond({
      getTierAssignments: vi.fn(async () => ({
        tiers: {
          fast: { provider_id: "local", model_id: "qwen3:9b", exists: true, enabled: true },
          coding: null,
          reasoning: null,
          default: null,
        },
        available: ["fast", "coding", "reasoning", "default"],
      })),
    });
    mockApi(stubs);
    render(<ModelsPanel />);
    const select = await screen.findByLabelText("Fast chat");
    expect(select).toHaveValue("local|qwen3:9b");

    fireEvent.change(select, { target: { value: "" } });

    await waitFor(() =>
      expect(stubs.setTierAssignment).toHaveBeenCalledWith("fast", null, null),
    );
  });

  it("hides the section when the backend has no tiers route", async () => {
    // An unpinned router is not an error — no banner, no section, no red.
    mockApi(respond({
      getTierAssignments: vi.fn(async () => ({
        tiers: {},
        available: [],
        error: "HTTP 404",
      })),
    }));
    render(<ModelsPanel />);
    await screen.findByText("MODELS");
    expect(screen.queryByText("TIER ASSIGNMENTS")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("flags a pin whose model no longer exists", async () => {
    mockApi(respond({
      getTierAssignments: vi.fn(async () => ({
        tiers: {
          fast: { provider_id: "local", model_id: "gone:1b", exists: false, enabled: false },
          coding: null,
          reasoning: null,
          default: null,
        },
        available: ["fast", "coding", "reasoning", "default"],
      })),
    }));
    render(<ModelsPanel />);
    expect(await screen.findByText("missing")).toBeInTheDocument();
  });
});

describe("ModelsPanel probing", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("offers probe only where probing can work", async () => {
    const stubs = respond({
      getProviders: vi.fn(async () => ({ providers: [OLLAMA, CLOUD] })),
      getModels: vi.fn(async () => ({ models: [LOCAL_MODEL, CLOUD_MODEL] })),
    });
    mockApi(stubs);
    render(<ModelsPanel />);
    await screen.findByText("TIER ASSIGNMENTS");

    // One button: the ollama model. The openai_compatible row gets none,
    // because the backend would refuse it — a button that always 400s
    // teaches people to ignore buttons.
    const probes = await screen.findAllByRole("button", { name: /Probe capabilities/i });
    expect(probes).toHaveLength(1);

    fireEvent.click(probes[0]);
    await waitFor(() =>
      expect(stubs.probeModel).toHaveBeenCalledWith("qwen3:9b", "local"),
    );
  });

  it("shows a refusal as the instruction it is", async () => {
    const stubs = respond({
      probeModel: vi.fn(async () => ({
        error: "Model nope:1b is not installed on http://localhost:11434 — pull it first with POST /settings/ai/pull",
      })),
    });
    mockApi(stubs);
    render(<ModelsPanel />);
    const probe = await screen.findByRole("button", { name: /Probe capabilities/i });

    fireEvent.click(probe);

    expect(await screen.findByRole("alert")).toHaveTextContent(/pull it first/);
  });
});

describe("ModelsPanel pulling", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("starts a pull on submit and polls it to a terminal state", async () => {
    const stubs = respond();
    mockApi(stubs);
    render(<ModelsPanel />);
    const input = await screen.findByPlaceholderText("qwen3:9b");

    fireEvent.change(input, { target: { value: "qwen3:9b" } });
    fireEvent.click(screen.getByRole("button", { name: "PULL" }));

    // The seeded record appears immediately, as "running"…
    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent(/qwen3:9b/);
    // …and the poll carries it to its terminal state, where it stops.
    await waitFor(() => expect(status).toHaveTextContent(/done/), {
      timeout: 3000,
    });
    expect(stubs.getPullStatus).toHaveBeenCalled();
  });

  it("shows a 409 as an answer rather than retrying", async () => {
    const stubs = respond({
      pullModel: vi.fn(async () => ({
        error: "A pull of qwen3:9b is already running",
      })),
    });
    mockApi(stubs);
    render(<ModelsPanel />);
    const input = await screen.findByPlaceholderText("qwen3:9b");

    fireEvent.change(input, { target: { value: "qwen3:9b" } });
    fireEvent.click(screen.getByRole("button", { name: "PULL" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/already running/);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(stubs.getPullStatus).not.toHaveBeenCalled();
  });

  it("an empty submission pulls nothing", async () => {
    const stubs = respond();
    mockApi(stubs);
    render(<ModelsPanel />);
    const button = await screen.findByRole("button", { name: "PULL" });

    fireEvent.click(button);

    expect(stubs.pullModel).not.toHaveBeenCalled();
  });
});
