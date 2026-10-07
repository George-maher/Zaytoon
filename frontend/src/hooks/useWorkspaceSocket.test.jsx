/**
 * The core runtime over the socket.
 *
 * `useWebSocket` is the whole client state machine, and the core runtime arrives
 * on the same frames as everything else — a second socket per concern would
 * double the connections and let two of them disagree about ordering. These
 * tests drive the reducer directly, which is why they matter: they pin the
 * behaviour of frames the desktop shell also depends on.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import useWebSocket from "./useWebSocket";

/** A minimal WebSocket double that records what was sent. */
class FakeSocket {
  static OPEN = 1;
  static CLOSED = 3;
  constructor(url) {
    this.url = url;
    this.readyState = FakeSocket.OPEN;
    this.sent = [];
    FakeSocket.instances.push(this);
  }
  send(data) {
    this.sent.push(JSON.parse(data));
  }
  close() {
    this.readyState = FakeSocket.CLOSED;
    this.onclose?.();
  }
  /** Deliver a frame to the hook. */
  emit(frame) {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }
}
FakeSocket.instances = [];

/**
 * Mount the hook and hand back the socket **the hook itself** created.
 *
 * Instantiating a FakeSocket here and using it would be a second, disconnected
 * socket: the hook constructs its own on mount, so frames delivered to a
 * different instance reach nothing. `FakeSocket.instances` is how the hook's own
 * socket is reached.
 */
function mount() {
  FakeSocket.instances = [];
  global.WebSocket = FakeSocket;
  const view = renderHook(() => useWebSocket());
  const socket = FakeSocket.instances.at(-1);
  if (!socket) throw new Error("the hook did not open a socket");
  act(() => {
    socket.onopen?.();
  });
  return { view, hook: view.result.current, socket };
}

/**
 * The hook's current value.
 *
 * `renderHook` returns `{ result }`, and `result` is a mutable ref-like object
 * whose `.current` updates on every render — reading it through a helper keeps
 * the assertions from each having to remember that.
 */
function latest(view) {
  return view.result.current;
}

describe("core runtime frames", () => {
  beforeEach(() => {
    FakeSocket.instances = [];
    global.WebSocket = FakeSocket;
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("records a created task", () => {
    const { view, socket } = mount();
    act(() => {
      socket.emit({
        type: "task.created",
        task: { id: 1, title: "Fix the tests", status: "received" },
      });
    });
    expect(latest(view).tasks).toHaveLength(1);
    expect(latest(view).tasks[0].title).toBe("Fix the tests");
    expect(latest(view).activeTask?.id).toBe(1);
  });

  it("updates an existing task rather than duplicating it", () => {
    const { view, socket } = mount();
    act(() => {
      socket.emit({ type: "task.created", task: { id: 1, title: "x", status: "received" } });
    });
    act(() => {
      socket.emit({
        type: "task.updated",
        task: { id: 1, title: "x", status: "executing" },
      });
    });
    expect(latest(view).tasks).toHaveLength(1);
    expect(latest(view).activeTask?.status).toBe("executing");
  });

  it("clears the active task only when it reaches a terminal state", () => {
    const { view, socket } = mount();
    act(() => {
      socket.emit({ type: "task.created", task: { id: 1, title: "x", status: "received" } });
    });
    act(() => {
      socket.emit({
        type: "task.updated",
        task: { id: 1, title: "x", status: "verifying", is_terminal: false },
      });
    });
    expect(latest(view).activeTask).not.toBeNull();

    act(() => {
      socket.emit({
        type: "task.updated",
        task: { id: 1, title: "x", status: "completed", is_terminal: true },
      });
    });
    expect(latest(view).activeTask).toBeNull();
    // Still in the list: a finished task is the answer to "what did you do".
    expect(latest(view).tasks).toHaveLength(1);
  });

  it("merges a step into its task", () => {
    const { view, socket } = mount();
    act(() => {
      socket.emit({
        type: "task.created",
        task: { id: 7, title: "x", status: "executing", steps: [{ seq: 0, title: "Read", status: "pending" }] },
      });
    });
    act(() => {
      socket.emit({
        type: "task.step",
        task_id: 7,
        step: { seq: 0, title: "Read", status: "done" },
      });
    });
    expect(latest(view).tasks.find((t) => t.id === 7).steps[0].status).toBe("done");
  });

  it("ignores a plan announcement, which carries no step", () => {
    const { view, socket } = mount();
    act(() => {
      socket.emit({ type: "task.created", task: { id: 1, title: "x", steps: [] } });
    });
    act(() => {
      socket.emit({ type: "task.step", task_id: 1, step: null, plan: ["a", "b"] });
    });
    expect(latest(view).tasks[0].steps).toEqual([]);
  });

  it("records a file change, most recent first and de-duplicated", () => {
    const { view, socket } = mount();
    act(() => {
      socket.emit({ type: "file.changed", path: "a.py", action: "changed" });
      socket.emit({ type: "file.changed", path: "b.py", action: "changed" });
    });
    expect(latest(view).fileChanges.map((f) => f.path)).toEqual(["b.py", "a.py"]);

    act(() => {
      socket.emit({ type: "file.changed", path: "a.py", action: "changed" });
    });
    expect(latest(view).fileChanges).toHaveLength(2);
    expect(latest(view).fileChanges[0].path).toBe("a.py");
  });

  it("collects terminal output per session", () => {
    const { view, socket } = mount();
    act(() => {
      socket.emit({
        type: "terminal_output",
        session_id: "term_a",
        stream: "stdout",
        text: "Vite started",
      });
    });
    expect(latest(view).terminalLines.term_a).toHaveLength(1);
    expect(latest(view).terminalLines.term_a[0].text).toBe("Vite started");
  });

  it("keeps terminal output bounded per session", () => {
    const { view, socket } = mount();
    act(() => {
      for (let i = 0; i < 2100; i++) {
        socket.emit({
          type: "terminal_output",
          session_id: "term_a",
          stream: "stdout",
          text: `line ${i}`,
        });
      }
    });
    // A flooding dev server must not grow the React state without limit.
    expect(latest(view).terminalLines.term_a.length).toBeLessThanOrEqual(2000);
    // And the newest line is the one that survived.
    expect(latest(view).terminalLines.term_a.at(-1).text).toBe("line 2099");
  });

  it("does not put a log line in the transcript", () => {
    const { view, socket } = mount();
    act(() => {
      socket.emit({ type: "task.log", task_id: 1, level: "error", message: "tool refused" });
    });
    // The transcript is user-facing prose; a diagnostic line is not.
    expect(latest(view).messages).toHaveLength(0);
  });

  it("ignores an unrecognised frame rather than crashing", () => {
    // A newer backend adding an event must not break an older client.
    const { view, socket } = mount();
    expect(() =>
      act(() => {
        socket.emit({ type: "some.future.event", payload: 1 });
      }),
    ).not.toThrow();
    expect(latest(view).isConnected).toBe(true);
  });

  it("reports an error frame from a task request", () => {
    const { view, socket } = mount();
    act(() => {
      socket.emit({ type: "task_result", cancelled: false, error: "No active task." });
    });
    expect(latest(view).messages.some((m) => /No active task/.test(m.content))).toBe(true);
  });
});

describe("task cancellation", () => {
  beforeEach(() => {
    FakeSocket.instances = [];
    global.WebSocket = FakeSocket;
  });

  it("sends a task message, not an interrupt", () => {
    // A task can outlive its turn — a test suite keeps running after the answer
    // was delivered — so firing an interrupt and hoping is how a test suite
    // ends up orphaned.
    const { hook, socket } = mount();
    let ok = false;
    act(() => {
      ok = hook.cancelTask();
    });
    expect(ok).toBe(true);
    const sent = socket.sent.at(-1);
    expect(sent).toEqual({ type: "task" });
  });

  it("reports failure when the socket is down", () => {
    const { hook, socket } = mount();
    socket.readyState = FakeSocket.CLOSED;
    let ok = true;
    act(() => {
      ok = hook.cancelTask();
    });
    expect(ok).toBe(false);
  });

  it("subscribes to one terminal session", () => {
    const { hook, socket } = mount();
    let ok = false;
    act(() => {
      ok = hook.watchTerminal("term_a");
    });
    expect(ok).toBe(true);
    expect(socket.sent.at(-1)).toEqual({ type: "terminal", session_id: "term_a" });
  });

  it("does not send a subscription for no session", () => {
    const { hook, socket } = mount();
    const before = socket.sent.length;
    let ok = true;
    act(() => {
      ok = hook.watchTerminal(null);
    });
    expect(ok).toBe(false);
    expect(socket.sent.length).toBe(before);
  });
});