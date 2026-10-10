/**
 * The task panel.
 *
 * A task row is not a spinner. What is worth pinning is what the panel *shows*
 * and what it *does*:
 *
 * - a finished task says what it changed ("Changed 2 files"), not just "Done" —
 *   that line is the answer to "what did you just do" without opening anything;
 * - the in-flight task is promoted with a working Stop control;
 * - a failed refresh lands in the panel (`role="alert"`) instead of leaving the
 *   previous list looking current;
 * - expanding a row fetches the full detail (logs, result, model) rather than
 *   pretending the list payload carries it.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within, act } from "@testing-library/react";
import TaskPanel from "./TaskPanel";
import * as api from "../../lib/api";

const DONE = {
  id: 7,
  title: "Create the project",
  status: "completed",
  files_changed: ["demo/app.js", "demo/package.json"],
  tools_used: ["create_folder", "file_write"],
  is_terminal: true,
  duration_ms: 1234,
};

const RUNNING = {
  id: 8,
  title: "Run the tests",
  status: "executing",
  steps: [{ seq: 1, title: "Collect tests", tool: "run_tests", status: "running" }],
  is_terminal: false,
};

function mockApi(stubs = {}) {
  vi.spyOn(api, "getTasks").mockImplementation(stubs.getTasks || (async () => ({})));
  vi.spyOn(api, "getTask").mockImplementation(stubs.getTask || (async () => ({})));
  vi.spyOn(api, "cancelTask").mockImplementation(stubs.cancelTask || (async () => ({})));
}

describe("TaskPanel", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("shows an empty state and refills on refresh", async () => {
    const getTasks = vi.fn(async () => ({}));
    mockApi({ getTasks });
    render(<TaskPanel tasks={[]} language="en" />);

    expect(await screen.findByText("No tasks yet")).toBeInTheDocument();
    expect(getTasks).toHaveBeenCalledWith({ limit: 50 });

    fireEvent.click(screen.getByTitle("Refresh"));
    await waitFor(() => expect(getTasks).toHaveBeenCalledTimes(2));
  });

  it("says what a finished task changed, not just that it finished", async () => {
    mockApi();
    const { container } = render(<TaskPanel tasks={[DONE]} language="en" />);

    expect(await screen.findByText("Create the project")).toBeInTheDocument();
    expect(screen.getByText(/Done · Changed 2 files/)).toBeInTheDocument();
    expect(screen.getByText("1234ms")).toBeInTheDocument();
    // Terminal tasks have no Stop control — there is nothing left to stop.
    expect(screen.queryByText("STOP")).not.toBeInTheDocument();

    // Expanding reveals the *evidence* behind the summary line.
    fireEvent.click(within(container).getAllByRole("button", { name: /Details/ })[0]);
    expect(screen.getByText(/Modified: demo\/app\.js, demo\/package\.json/)).toBeInTheDocument();
    expect(screen.getByText(/Tools: create_folder, file_write/)).toBeInTheDocument();
  });

  it("promotes the in-flight task with a working Stop control", async () => {
    const getTasks = vi.fn(async () => ({}));
    mockApi({ getTasks });
    const onCancel = vi.fn();
    render(<TaskPanel tasks={[RUNNING]} activeTask={RUNNING} onCancel={onCancel} language="en" />);

    // The promoted region, not the list row: two Stop buttons exist, and only
    // the promoted one is the one a user reaches for first.
    const region = await screen.findByRole("status");
    expect(within(region).getByText("Run the tests")).toBeInTheDocument();
    expect(within(region).getByText(/60%/)).toBeInTheDocument();

    fireEvent.click(within(region).getByRole("button", { name: "STOP" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
    // Cancelling through the socket still refills the list afterwards.
    await waitFor(() => expect(getTasks).toHaveBeenCalledTimes(2));
  });

  it("surfaces a failed refresh instead of leaving stale rows", async () => {
    mockApi({ getTasks: async () => ({ error: "permission denied" }) });
    render(<TaskPanel tasks={[DONE]} language="en" />);

    expect(await screen.findByRole("alert")).toHaveTextContent("permission denied");
  });

  it("fetches full detail when a row is opened", async () => {
    const getTask = vi.fn(async () => ({
      ...DONE,
      result: "Created demo with 2 files",
      model: "qwen3.8-27b:free",
      provider: "openrouter",
    }));
    mockApi({ getTask });
    render(<TaskPanel tasks={[DONE]} language="en" />);

    // Detail renders inside the expanded row: expand first, then open.
    fireEvent.click(within(screen.getByRole("list")).getAllByRole("button", { name: /Details/ })[0]);
    fireEvent.click(await screen.findByRole("button", { name: /Create the project/ }));
    expect(getTask).toHaveBeenCalledWith(7);
    expect(await screen.findByText("Created demo with 2 files")).toBeInTheDocument();
    expect(screen.getByText(/Model: openrouter \/ qwen3\.8-27b:free/)).toBeInTheDocument();
  });

  it("labels the panel in Arabic when asked", async () => {
    mockApi();
    render(<TaskPanel tasks={[]} language="ar" />);

    expect(await screen.findByText("المهام")).toBeInTheDocument();
    expect(screen.getByText("لا مهام بعد")).toBeInTheDocument();
  });

  // -- the merge: socket for what is running, store for what happened -------

  it("shows persisted tasks the socket was never there to see", async () => {
    // The socket only knows about this session. Before the fetch result was
    // actually kept, a second day's open panel said "No tasks yet" above a
    // database full of them — the panel's only job, unperformed.
    mockApi({
      getTasks: async () => ({
        tasks: [
          { id: 1, title: "Yesterday's build", status: "completed", is_terminal: true, duration_ms: 42 },
        ],
        count: 1,
        active: 0,
      }),
    });
    render(<TaskPanel tasks={[]} language="en" />);

    expect(await screen.findByText("Yesterday's build")).toBeInTheDocument();
    expect(screen.queryByText(/no tasks yet/i)).not.toBeInTheDocument();
  });

  it("keeps a task to one row, with the socket's copy winning", async () => {
    // The same task arrives from both sources: the store's snapshot was taken
    // before it started, the socket's while it ran. Two rows would read as two
    // tasks, and the stale one would say "queued" about work that is running.
    mockApi({
      getTasks: async () => ({
        tasks: [
          { id: 8, title: "Run the tests", status: "queued", is_terminal: false },
          { id: 1, title: "Old task", status: "completed", is_terminal: true },
        ],
        count: 2,
        active: 0,
      }),
    });
    render(<TaskPanel tasks={[RUNNING]} language="en" />);

    expect(await screen.findByText("Run the tests")).toBeInTheDocument();
    expect(screen.getAllByText("Run the tests")).toHaveLength(1);
    expect(screen.getByText(/Running ·/)).toBeInTheDocument();
    expect(screen.getByText("Old task")).toBeInTheDocument();
  });

  it("says it is loading rather than claiming there are no tasks", async () => {
    let resolveFetch;
    mockApi({
      getTasks: () =>
        new Promise((resolve) => {
          resolveFetch = resolve;
        }),
    });
    render(<TaskPanel tasks={[]} language="en" />);

    expect(screen.getByText("Loading tasks…")).toBeInTheDocument();
    expect(screen.queryByText(/no tasks yet/i)).not.toBeInTheDocument();

    await act(async () => {
      resolveFetch({ tasks: [], count: 0, active: 0 });
    });
    expect(await screen.findByText("No tasks yet")).toBeInTheDocument();
  });

  it("does not claim an empty store when the load itself failed", async () => {
    // The alert and the empty state would contradict each other, and only one
    // of them is backed by the response.
    mockApi({ getTasks: async () => ({ tasks: [], error: "permission denied" }) });
    render(<TaskPanel tasks={[]} language="en" />);

    expect(await screen.findByRole("alert")).toHaveTextContent("permission denied");
    expect(screen.queryByText(/no tasks yet/i)).not.toBeInTheDocument();
  });
});
