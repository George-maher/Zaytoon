import { describe, it, expect } from "vitest";
import {
  TASK_LIFECYCLE,
  TASK_TONES,
  isTaskActive,
  taskLabel,
  taskPercent,
  taskSummary,
  taskTone,
} from "./taskState";

describe("taskTone", () => {
  it("maps the live statuses to 'active'", () => {
    for (const status of ["analyzing", "planning", "executing", "verifying"]) {
      expect(taskTone(status)).toBe("active");
    }
  });

  it("maps both waiting statuses to 'blocked'", () => {
    // A task waiting on a person is not doing anything, but it is very much in
    // flight — it needs a Stop control and the user's attention.
    expect(taskTone("waiting_for_permission")).toBe("blocked");
    expect(taskTone("waiting_for_user")).toBe("blocked");
  });

  it("maps the terminal statuses", () => {
    expect(taskTone("completed")).toBe("done");
    expect(taskTone("failed")).toBe("failed");
    expect(taskTone("cancelled")).toBe("cancelled");
    expect(taskTone("received")).toBe("pending");
  });

  it("grades an unknown status up, never down", () => {
    // The dangerous direction would be showing an unrecognised state as
    // routine. A state this build has never heard of is the one a user most
    // needs to look at.
    expect(taskTone("some_future_state")).toBe("high");
    expect(TASK_TONES).toContain("high");
  });

  it("handles a missing status", () => {
    expect(taskTone(undefined)).toBe("high");
  });
});

describe("taskLabel", () => {
  it("labels every lifecycle status in English", () => {
    for (const status of TASK_LIFECYCLE) {
      expect(taskLabel(status, "en")).toBeTruthy();
      expect(taskLabel(status, "en")).not.toBe(status);
    }
  });

  it("labels in Arabic", () => {
    expect(taskLabel("executing", "ar")).toBe("بشغّل");
    expect(taskLabel("completed", "ar")).toBe("خلص");
  });

  it("shows an unknown status verbatim rather than guessing", () => {
    // "some_future_state" is more useful to whoever is debugging than a generic
    // word, and it is not a lie.
    expect(taskLabel("some_future_state", "en")).toBe("some_future_state");
  });

  it("falls back to a dash for nothing", () => {
    expect(taskLabel("", "en")).toBe("—");
  });
});

describe("taskPercent", () => {
  it("prefers the server's progress", () => {
    expect(taskPercent({ status: "executing", progress: 0.42 })).toBe(42);
  });

  it("derives progress from the lifecycle when there is none", () => {
    expect(taskPercent({ status: "received" })).toBe(0);
    expect(taskPercent({ status: "completed" })).toBe(100);
    // Strictly between, and monotonic across the chain.
    const middle = taskPercent({ status: "executing" });
    expect(middle).toBeGreaterThan(0);
    expect(middle).toBeLessThan(100);
  });

  it("is monotonic across the lifecycle", () => {
    const values = TASK_LIFECYCLE.map((status) => taskPercent({ status }));
    expect(values).toEqual([...values].sort((a, b) => a - b));
  });

  it("clamps an out-of-range value", () => {
    expect(taskPercent({ status: "executing", progress: 5 })).toBe(100);
    expect(taskPercent({ status: "executing", progress: -1 })).toBe(0);
  });

  it("gives a failed task a full bar", () => {
    // It finished — as a failure. A bar frozen where things went wrong reads
    // as a hang.
    expect(taskPercent({ status: "failed" })).toBe(100);
  });

  it("handles no task", () => {
    expect(taskPercent(null)).toBe(0);
  });
});

describe("isTaskActive", () => {
  it("is true for work in flight and for work blocked on a person", () => {
    expect(isTaskActive({ status: "executing" })).toBe(true);
    expect(isTaskActive({ status: "waiting_for_permission" })).toBe(true);
    expect(isTaskActive({ status: "waiting_for_user" })).toBe(true);
  });

  it("is false for finished and not-yet-started tasks", () => {
    expect(isTaskActive({ status: "completed" })).toBe(false);
    expect(isTaskActive({ status: "received" })).toBe(false);
    expect(isTaskActive(null)).toBe(false);
  });
});

describe("taskSummary", () => {
  it("reports a failure's own error", () => {
    expect(taskSummary({ status: "failed", error: "the tool was refused" })).toBe(
      "the tool was refused",
    );
  });

  it("says a cancelled task did not finish", () => {
    expect(taskSummary({ status: "cancelled" }, "en")).toBe("Cancelled before it finished");
  });

  it("reports what a completed task changed", () => {
    const summary = taskSummary({
      status: "completed",
      files_changed: ["a.py", "b.py"],
    });
    expect(summary).toBe("Changed 2 files");
  });

  it("singularises one file", () => {
    expect(taskSummary({ status: "completed", files_changed: ["a.py"] })).toBe("Changed 1 file");
  });

  it("falls back to tools when nothing was changed", () => {
    expect(taskSummary({ status: "completed", tools_used: ["get_time"] })).toBe("Used 1 tool");
  });

  it("names the running step", () => {
    expect(
      taskSummary({
        status: "executing",
        steps: [
          { title: "Read the project", status: "done" },
          { title: "Write the fix", status: "running" },
        ],
      }),
    ).toBe("Write the fix");
  });

  it("falls back to the status word when there is nothing better", () => {
    expect(taskSummary({ status: "planning" })).toBe("Planning");
  });

  it("reports in Arabic", () => {
    expect(taskSummary({ status: "completed", files_changed: ["a.py"] }, "ar")).toBe("غيّر 1 ملف");
  });

  it("handles no task", () => {
    expect(taskSummary(null)).toBe("");
  });
});