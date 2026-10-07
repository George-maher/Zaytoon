/**
 * The system panel — machine state with honest gaps.
 *
 * Two endpoints feed this panel (`/health` for configuration, `/system` for
 * the machine), and the property worth pinning is what each row does when it
 * does *not* have a number:
 *
 * - a CPU that has only taken one reading renders `SAMPLING`, never `0%` —
 *   a fabricated zero reads as an idle machine at exactly the wrong moment;
 * - network counters are not collected, and the row says so (`N/A`) with the
 *   reason in its `title`, rather than omitting the key;
 * - per-model latency is the mean of each model's own samples;
 * - a failed health refresh clears the rows behind `BACKEND UNREACHABLE`
 *   instead of leaving stale numbers looking current.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import SystemStatus from "./SystemStatus";
import * as api from "../../lib/api";

const HEALTH = {
  llm: {
    primary_model: "qwen/qwen3.8-27b:free",
    reasoning_model: "cohere/north-mini-code:free",
    chain: ["qwen/qwen3.8-27b:free", "cohere/north-mini-code:free"],
    metrics: {
      requests: 12,
      avg_latency_ms: 480,
      by_model_latency_ms: { "qwen/qwen3.8-27b:free": 420 },
    },
    model_health: { "qwen/qwen3.8-27b:free": { failures: 0, blocked: false } },
    rejected_models: [],
  },
  voice: { stt: true, tts: true },
  safety: { enabled: true, confirm_timeout: 30, always_confirm: ["file_delete"] },
  tools_loaded: 36,
  tools_by_tier: {
    safe: ["get_time"],
    confirm: ["file_write"],
    dangerous: ["run_code"],
  },
  core: {
    providers: 2,
    models: 4,
    skills: 3,
    projects: 5,
    skill_errors: 0,
    default_provider: "openrouter",
  },
  metrics: { tools: { calls: 9, failed: 0, avg_ms: 30 } },
};

const SYSTEM = {
  uptime_seconds: 3671,
  tasks: { total: 4, active: 1 },
  processes: { tracked: 2, running: ["p1"] },
  terminals: { open: 1 },
  resources: {
    cpu: { percent: null, status: "sampling" },
    memory: {
      status: "ok",
      percent: 41,
      total_bytes: 16 * 1024 ** 3,
      used_bytes: 6.6 * 1024 ** 3,
    },
    disk: {
      status: "ok",
      percent: 60,
      total_bytes: 250 * 1024 ** 3,
      free_bytes: 100 * 1024 ** 3,
      path: "C:/ws",
    },
    network: { status: "unavailable", reason: "network counters are not collected" },
  },
};

describe("SystemStatus", () => {
  beforeEach(() => {
    vi.spyOn(api, "healthCheck").mockResolvedValue(HEALTH);
    vi.spyOn(api, "getSystem").mockResolvedValue(SYSTEM);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders machine state as declared states, not zeros", async () => {
    render(<SystemStatus language="en" />);

    expect(await screen.findByText("SAMPLING")).toBeInTheDocument();
    // No row may draw a zero it did not measure — sampling is not 0%.
    expect(screen.queryByText("0%")).not.toBeInTheDocument();

    expect(screen.getByText("41% · 6.6GB/16.0GB")).toBeInTheDocument();
    expect(screen.getByText("60% · 100.0GB free")).toBeInTheDocument();

    // Network: an explicit state, with the reason attached to the row.
    const net = screen.getByText("N/A").closest(".hud-tele");
    expect(net).toHaveAttribute("title", "network counters are not collected");
  });

  it("shows per-model latency, uptime and core counts", async () => {
    render(<SystemStatus language="en" />);

    expect(await screen.findByText("qwen3.8-27b:free")).toBeInTheDocument();
    expect(screen.getByText("420ms")).toBeInTheDocument();
    expect(screen.getByText("1h 1m")).toBeInTheDocument();
    expect(screen.getByText("3 skills · 5 projects")).toBeInTheDocument();
    expect(screen.getByText("1/4")).toBeInTheDocument(); // active/total tasks
    // The fuller breakdown rides in the row's title.
    const core = screen.getByText("3 skills · 5 projects").closest(".hud-tele");
    expect(core).toHaveAttribute(
      "title",
      "2 providers · 4 models · default openrouter",
    );
  });

  it("clears the rows behind an explicit failure", async () => {
    vi.spyOn(api, "healthCheck").mockRejectedValue(new Error("ECONNREFUSED"));
    render(<SystemStatus language="en" />);

    expect(await screen.findByRole("alert")).toHaveTextContent("BACKEND UNREACHABLE");
    // Stale numbers must not survive as if they were current.
    expect(screen.queryByText("SAMPLING")).not.toBeInTheDocument();
    expect(screen.queryByText("41% · 6.6GB/16.0GB")).not.toBeInTheDocument();
  });
});
