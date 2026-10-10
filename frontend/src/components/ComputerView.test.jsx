import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import ComputerView from "./ComputerView";

/**
 * The Computer view is a *view* over capabilities that already exist: one
 * route new (`GET /computer/apps`), three reused (`/health` + `/system`,
 * `/processes`, `/files/*`). What matters here is therefore not that it
 * renders, but that it never says more than the backend said:
 *
 *  - a failed refresh clears the panel instead of leaving stale rows that
 *    read as current;
 *  - a refusal to stop a process is reported as a refusal, and the list it
 *    refused to change stays on screen;
 *  - an empty process list still carries the "only what Zaytoon started"
 *    line, because otherwise it reads as "the machine is idle";
 *  - launching a tile goes through `POST /command/open-app`, the route the
 *    assistant itself uses — this screen adds no new way in;
 *  - availability (`installed`) is shown **only** when the backend said it
 *    looked (`resolved: true`), and an unlit tile is worded as "not found
 *    here", never as "not installed";
 *  - a process's output is fetched on demand and can be refused without
 *    taking the row it belongs to with it.
 */
const CATALOG = {
  apps: [
    { id: "chrome", label: "Google Chrome", category: "browser" },
    { id: "vscode", label: "Visual Studio Code", category: "editor" },
    { id: "word", label: "Word", category: "office" },
  ],
  categories: ["browser", "editor", "office"],
  total: 3,
};

function mockFetch({ apps, processes, list, opened, stop, logs } = {}) {
  global.fetch = vi.fn(async (url, opts = {}) => {
    const u = String(url);
    const method = opts.method || "GET";
    if (u.includes("/computer/apps")) {
      if (apps?.status === 404) return { ok: false, status: 404, json: async () => ({}) };
      if (apps?.throw) throw new Error("Failed to fetch");
      return { ok: true, status: 200, json: async () => apps ?? CATALOG };
    }
    if (u.includes("/command/open-app")) {
      return { ok: true, status: 200, json: async () => opened ?? { success: true, output: "started" } };
    }
    // Before the generic `/processes` branch: the tail has its own shape.
    if (/\/processes\/[^/?]+\/logs/.test(u)) {
      if (logs?.status === 404) return { ok: false, status: 404, json: async () => ({}) };
      if (logs?.throw) throw new Error("Failed to fetch");
      return {
        ok: true,
        status: 200,
        json: async () => logs ?? { id: "p1", status: "running", lines: [] },
      };
    }
    if (u.includes("/processes") && method === "POST") {
      return { ok: true, status: 200, json: async () => stop ?? { stopped: true } };
    }
    if (u.includes("/processes")) {
      if (processes?.status === 404) return { ok: false, status: 404, json: async () => ({}) };
      return { ok: true, status: 200, json: async () => processes ?? { processes: [] } };
    }
    if (u.includes("/files/list")) {
      return {
        ok: true,
        status: 200,
        json: async () => list ?? { path: ".", entries: [], count: 0, truncated: false },
      };
    }
    if (u.includes("/files/root")) {
      return { ok: true, status: 200, json: async () => ({ root: "C:/ws", exists: true }) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  });
  return global.fetch;
}

describe("ComputerView", () => {
  beforeEach(() => {
    mockFetch();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("mounts every panel the screen promises", async () => {
    render(<ComputerView language="en" onOpenFile={() => {}} />);
    expect(await screen.findByText("APPS")).toBeInTheDocument();
    expect(screen.getByText("SYSTEM STATUS")).toBeInTheDocument();
    expect(screen.getByText("PROCESSES")).toBeInTheDocument();
    expect(screen.getByText("FILES")).toBeInTheDocument();
  });

  it("is an h2 beneath App's single h1, not a second one", () => {
    render(<ComputerView language="en" onOpenFile={() => {}} />);
    expect(screen.getByRole("heading", { level: 2, name: "COMPUTER" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 1 })).not.toBeInTheDocument();
  });

  it("groups the catalogue and launches through the existing gated route", async () => {
    const fetch = mockFetch({ opened: { success: true, output: "started" } });
    render(<ComputerView language="en" onOpenFile={() => {}} />);

    expect(await screen.findByText("BROWSERS")).toBeInTheDocument();
    expect(screen.getByText("EDITORS")).toBeInTheDocument();
    expect(screen.getByText("OFFICE")).toBeInTheDocument();

    fireEvent.click(await screen.findByRole("button", { name: "Google Chrome" }));
    const msg = await screen.findByText(/LAUNCHED Google Chrome/);
    // The outcome has to sit inside the live region, or it is announced to
    // nobody — a message that only exists once it is too late to be read.
    expect(msg.closest('[role="status"]')).not.toBeNull();

    const call = fetch.mock.calls.find(([u]) => String(u).includes("/command/open-app"));
    expect(call, "the launch must reach the allow-listed route").toBeTruthy();
    expect(JSON.parse(call[1].body).app_name).toBe("chrome");
  });

  it("shows the launcher's own refusal rather than a generic failure", async () => {
    mockFetch({
      opened: { success: false, error: "'word' could not be started" },
    });
    render(<ComputerView language="en" onOpenFile={() => {}} />);
    fireEvent.click(await screen.findByRole("button", { name: "Word" }));
    expect(await screen.findByText(/COULD NOT LAUNCH Word/)).toHaveTextContent(
      "COULD NOT LAUNCH Word: 'word' could not be started",
    );
  });

  it("clears the grid when the catalogue cannot be fetched", async () => {
    // A stale list of launchable apps looks exactly like a current one — the
    // one thing a catalogue must never be.
    mockFetch({ apps: { throw: true } });
    render(<ComputerView language="en" onOpenFile={() => {}} />);
    expect(await screen.findByText(/App fetch failed/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Google Chrome" })).not.toBeInTheDocument();
  });

  it("says so plainly when the backend predates the route", async () => {
    mockFetch({ apps: { status: 404 } });
    render(<ComputerView language="en" onOpenFile={() => {}} />);
    expect(
      await screen.findByText(/App catalogue unavailable on this backend/i),
    ).toBeInTheDocument();
  });

  it("reports what this machine has, and only when it was actually looked up", async () => {
    mockFetch({
      apps: {
        ...CATALOG,
        resolved: true,
        found: 1,
        apps: [
          { id: "chrome", label: "Google Chrome", category: "browser", installed: true, via: "path" },
          { id: "vscode", label: "Visual Studio Code", category: "editor", installed: false, via: "alias" },
          { id: "word", label: "Word", category: "office", installed: false, via: "shortcut" },
        ],
      },
    });
    render(<ComputerView language="en" onOpenFile={() => {}} />);

    // The count answers "is there anything here" without a 26-tile scan.
    expect(await screen.findByText(/FOUND ON THIS MACHINE 1 \/ 3/)).toBeInTheDocument();

    // The verdict is in the accessible name, not only in a coloured dot —
    // colour alone cannot carry a claim about the machine.
    expect(
      await screen.findByRole("button", { name: /Google Chrome.*found on this machine/ }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Word/ })).toHaveAccessibleName(
      /no launch target found here/,
    );

    // …and the legend states what the hollow dot does *not* mean, because
    // "not found" silently read as "not installed" is the error this screen
    // exists to avoid. The same sentence is in every unlit tile's accessible
    // name, so the assertion is pinned to the legend's own paragraph.
    const legend = screen
      .getAllByText(/not proof it is missing/)
      .find((el) => el.tagName === "P");
    expect(legend, "the panel must carry the legend as its own line").toBeTruthy();
    expect(legend).toHaveTextContent("found on this machine");
  });

  it("never reads an absent verdict as an absence", async () => {
    // The plain catalogue carries no `resolved` field: the probe was not
    // asked for, so there is nothing to say. Rendering a hollow dot here
    // would be the panel inventing an answer.
    mockFetch();
    render(<ComputerView language="en" onOpenFile={() => {}} />);

    const tile = await screen.findByRole("button", { name: "Google Chrome" });
    expect(tile).toHaveAttribute("title", "chrome");
    expect(screen.queryByText(/FOUND ON THIS MACHINE/)).not.toBeInTheDocument();
    expect(screen.queryByText(/not proof it is missing/)).not.toBeInTheDocument();
    expect(screen.getByText(/not a claim about what is installed/)).toBeInTheDocument();
  });

  it("withholds the whole verdict when the probe fails, rather than half of it", async () => {
    mockFetch({
      apps: {
        ...CATALOG,
        resolved: false,
        resolve_error: "probe failed",
        apps: [
          { id: "chrome", label: "Google Chrome", category: "browser" },
          { id: "word", label: "Word", category: "office" },
        ],
      },
    });
    render(<ComputerView language="en" onOpenFile={() => {}} />);
    expect(await screen.findByRole("button", { name: "Google Chrome" })).toBeInTheDocument();
    expect(screen.queryByText(/FOUND ON THIS MACHINE/)).not.toBeInTheDocument();
    expect(screen.getByText(/availability probe failed/)).toBeInTheDocument();
  });

  it("asks for a process's output only when asked for it", async () => {
    const fetch = mockFetch({
      processes: { processes: [{ id: "p1", name: "vite", status: "running", pid: 42 }] },
      logs: {
        id: "p1",
        status: "running",
        lines: ["ready in 314ms", "Local: http://localhost:5173/"],
      },
    });
    render(<ComputerView language="en" onOpenFile={() => {}} />);
    const logsButton = await screen.findByRole("button", { name: "LOGS" });

    // Mounting the panel must not pull a tail: it is the one action here
    // with no deadline and no reason to run unattended.
    expect(fetch.mock.calls.filter(([u]) => String(u).includes("/logs"))).toHaveLength(0);

    fireEvent.click(logsButton);
    expect(await screen.findByText(/ready in 314ms/)).toBeInTheDocument();
    expect(screen.getByText(/Local: http:\/\/localhost:5173\//)).toBeInTheDocument();
    expect(
      fetch.mock.calls.filter(([u]) => String(u).includes("/logs")),
    ).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: "HIDE" }));
    expect(screen.queryByText(/ready in 314ms/)).not.toBeInTheDocument();
  });

  it("reports missing output as a report, not as a broken panel", async () => {
    mockFetch({
      processes: { processes: [{ id: "p1", name: "vite", status: "running", pid: 42 }] },
      logs: { status: 404 },
    });
    render(<ComputerView language="en" onOpenFile={() => {}} />);
    fireEvent.click(await screen.findByRole("button", { name: "LOGS" }));
    expect(await screen.findByText("Output unavailable")).toBeInTheDocument();
    // The row it belongs to is untouched.
    expect(screen.getByRole("button", { name: "STOP" })).toBeInTheDocument();
  });

  it("treats a 200 refusal as a refusal, not as a quiet log", async () => {
    // The route answers `200 {"error": ...}` for an id it does not know. Read
    // as a successful empty tail, this would say "No output captured" — a
    // claim about a quiet process, for a process that was never known.
    mockFetch({
      processes: { processes: [{ id: "p1", name: "vite", status: "running", pid: 42 }] },
      logs: { error: "No such process: p1" },
    });
    render(<ComputerView language="en" onOpenFile={() => {}} />);
    fireEvent.click(await screen.findByRole("button", { name: "LOGS" }));
    expect(await screen.findByText("Output unavailable")).toBeInTheDocument();
    expect(screen.queryByText("No output captured")).not.toBeInTheDocument();
  });

  it("never lets an empty process list read as an idle machine", async () => {
    mockFetch({ processes: { processes: [] } });
    render(<ComputerView language="en" onOpenFile={() => {}} />);
    expect(await screen.findByText("No background processes")).toBeInTheDocument();
    expect(
      screen.getByText(/Only processes Zaytoon started are tracked/),
    ).toBeInTheDocument();
  });

  it("reports a refused stop as a refusal and keeps the list it did not change", async () => {
    mockFetch({
      processes: {
        processes: [{ id: "p1", name: "vite", status: "running", pid: 42, duration_ms: 5000 }],
      },
      stop: { stopped: false, error: "Only processes Zaytoon started can be stopped" },
    });
    render(<ComputerView language="en" onOpenFile={() => {}} />);
    expect(await screen.findByText("vite")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "STOP" }));
    expect(await screen.findByText(/REFUSED/)).toBeInTheDocument();
    // The refusal is about one action; it must not clear the rows.
    expect(screen.getByText("vite")).toBeInTheDocument();
  });

  it("shows the process registry's absence as information, not as an error", async () => {
    mockFetch({ processes: { status: 404 } });
    render(<ComputerView language="en" onOpenFile={() => {}} />);
    expect(
      await screen.findByText(/Process tracking unavailable on this backend/i),
    ).toBeInTheDocument();
  });

  it("browses the workspace and hands a file's own path to the editor", async () => {
    const onOpenFile = vi.fn();
    mockFetch({
      list: {
        path: "src",
        entries: [
          { name: "lib", path: "src/lib", is_dir: true, size: 0 },
          { name: "app.js", path: "src/app.js", is_dir: false, size: 128 },
        ],
        count: 2,
        truncated: false,
      },
    });
    render(<ComputerView language="en" onOpenFile={onOpenFile} />);

    expect(await screen.findByText("C:/ws")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /app\.js/ }));
    expect(onOpenFile).toHaveBeenCalledWith("src/app.js");
  });

  it("renders the screen's own labels in Arabic", async () => {
    mockFetch();
    render(<ComputerView language="ar" onOpenFile={() => {}} />);
    expect(await screen.findByRole("heading", { level: 2, name: "الحاسوب" })).toBeInTheDocument();
    expect(screen.getByText("المتصفحات")).toBeInTheDocument();
    expect(screen.getByText("العمليات")).toBeInTheDocument();
  });
});
