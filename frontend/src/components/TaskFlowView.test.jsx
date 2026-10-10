/**
 * The Tasks destination.
 *
 * Two things are worth pinning here, and neither is about styling:
 *
 * 1. **It is a real screen.** The heading is an `h2` — App's header owns the
 *    document's single `h1` — and both panels it advertises actually render
 *    their content. A destination in the nav that mounts an empty shell is the
 *    "fake UI" this system forbids, and `navItems()` already trusts this screen
 *    to exist, so the trust has to be earned here rather than in viewState.
 * 2. **It does not claim to be live.** The execution log carries no status chip
 *    of its own, because `activity` is only cleared with the transcript: a
 *    "running" marker derived from it would still be lit after the turn ended.
 *    The rows inside already say RUNNING / DONE / FAILED individually.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import TaskFlowView from "./TaskFlowView";
import * as api from "../lib/api";

vi.mock("../lib/api", () => ({
  getTasks: vi.fn(async () => ({ tasks: [], count: 0, active: 0 })),
  getTask: vi.fn(async () => ({})),
  cancelTask: vi.fn(async () => ({})),
}));

const PERSISTED = {
  id: 12,
  title: "Shipped yesterday",
  status: "completed",
  is_terminal: true,
  duration_ms: 900,
};

describe("TaskFlowView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getTasks.mockResolvedValue({ tasks: [], count: 0, active: 0 });
  });

  it("is an h2 beneath App's single h1, not a second one", () => {
    render(<TaskFlowView language="en" />);
    expect(
      screen.getByRole("heading", { level: 2, name: "TASKS" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 1 })).not.toBeInTheDocument();
  });

  it("names both halves of what the screen answers", () => {
    render(<TaskFlowView language="en" />);
    expect(screen.getByText("EXECUTION LOG")).toBeInTheDocument();
    // Once as the screen's own section heading, once as TaskPanel's frame
    // header — the panel is shared with the workspace inspector on purpose,
    // so its title does not change with where it is mounted.
    expect(screen.getAllByText("TASKS").length).toBeGreaterThanOrEqual(2);
  });

  it("says nothing has run instead of leaving an empty frame", () => {
    render(<TaskFlowView language="en" />);
    expect(screen.getByText("Nothing has run yet")).toBeInTheDocument();
    // No status chip of its own: with no events there is nothing to be
    // "running", and inventing an idle/working state from `activity.length`
    // is the kind of claim this screen has no way to back.
    expect(screen.queryByText("IN PROGRESS")).not.toBeInTheDocument();
    expect(screen.queryByText("IDLE")).not.toBeInTheDocument();
  });

  it("shows the plan and one row per tool the turn used", () => {
    render(
      <TaskFlowView
        language="en"
        plan={["Create the folder"]}
        activeTool={{ name: "run_npm", label: "Running tests" }}
        activity={[
          { kind: "started", name: "run_npm", label: "Running tests", at: 1 },
          { kind: "progress", name: "run_npm", message: "42 of 60 passed" },
        ]}
      />,
    );
    expect(screen.getByText("Create the folder")).toBeInTheDocument();
    expect(screen.getByText("Running tests")).toBeInTheDocument();
    expect(screen.getByText("42 of 60 passed")).toBeInTheDocument();
    expect(screen.queryByText("Nothing has run yet")).not.toBeInTheDocument();
    // The claim the screen refuses to make is absent even while something
    // genuinely *is* running — the row carries it instead.
    expect(screen.queryByText("IN PROGRESS")).not.toBeInTheDocument();
  });

  it("hands the persisted records to the task panel", async () => {
    api.getTasks.mockResolvedValue({
      tasks: [PERSISTED],
      count: 1,
      active: 0,
    });
    render(<TaskFlowView language="en" />);
    expect(await screen.findByText("Shipped yesterday")).toBeInTheDocument();
  });

  it("gives the task panel a working Stop for the task in flight", async () => {
    const onCancel = vi.fn();
    const running = { id: 30, title: "Run the tests", status: "executing", is_terminal: false };
    api.getTasks.mockResolvedValue({ tasks: [], count: 0, active: 0 });
    render(
      <TaskFlowView
        language="en"
        tasks={[running]}
        activeTask={running}
        onCancel={onCancel}
      />,
    );
    // Two live regions are in play — the log's idle note and the promoted
    // task — so the query has to pick the one that actually holds the task,
    // rather than assuming there is only one.
    const regions = await screen.findAllByRole("status");
    const region = regions.find((el) => el.textContent.includes("Run the tests"));
    expect(region).toBeTruthy();
    fireEvent.click(within(region).getByRole("button", { name: "STOP" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("renders its own labels in Arabic", () => {
    render(<TaskFlowView language="ar" />);
    expect(
      screen.getByRole("heading", { level: 2, name: "المهام" }),
    ).toBeInTheDocument();
    expect(screen.getByText("سجل التنفيذ")).toBeInTheDocument();
    expect(screen.getByText("لم يُنفَّذ شيء بعد")).toBeInTheDocument();
  });
});
