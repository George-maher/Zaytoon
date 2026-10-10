/**
 * The workspace buffer store.
 *
 * This hook is where unsaved work lives, so the tests below are about one
 * question: **can a user lose an edit without being told?** Three separate
 * bugs could each answer yes, and each has a case here that fails on the old
 * code:
 *
 * 1. `open()` re-read on every click, so re-opening a file — which looks
 *    exactly like navigation — replaced the buffer with what is on disk.
 * 2. The tab list was written *before* the new entry existed, so a buffer's
 *    `content` was frozen at load time. Editing, switching away and coming
 *    back reverted the file silently, while the SAVE button still glowed.
 * 3. `discard()` only cleared the dirty flag, leaving the edited text on
 *    screen with SAVE disabled — the change could be neither saved nor seen.
 *
 * The dirty mark is asserted as *derived* (`content !== baseline`) rather than
 * stored, because a flag maintained by hand in five places is wrong in the
 * sixth.
 */
import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../lib/api";
import useWorkspace from "./useWorkspace";

vi.mock("../lib/api", () => ({
  getFileRoot: vi.fn(),
  getProjects: vi.fn(),
  getFileTree: vi.fn(),
  getTerminals: vi.fn(),
  readFile: vi.fn(),
  writeFile: vi.fn(),
  openProject: vi.fn(),
  openTerminal: vi.fn(),
  writeTerminal: vi.fn(),
  closeTerminal: vi.fn(),
  getTerminalOutput: vi.fn(),
}));

/** An in-memory disk, so open → edit → open again is a real round trip. */
let disk;

function mount() {
  return renderHook(() => useWorkspace());
}

async function openAt(view, path) {
  let result;
  await act(async () => {
    result = await view.result.current.open(path);
  });
  return result;
}

async function edit(view, content) {
  await act(async () => {
    view.result.current.setContent(content);
  });
}

describe("useWorkspace — the editing loop", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    disk = { "a.txt": "alpha", "b.txt": "bravo" };
    api.getFileRoot.mockResolvedValue({ root: "/ws" });
    api.getProjects.mockResolvedValue({ projects: [] });
    api.getFileTree.mockResolvedValue({ entries: [], truncated: false });
    api.getTerminals.mockResolvedValue({ sessions: [] });
    api.readFile.mockImplementation(async (path) =>
      disk[path] === undefined
        ? { error: `No such file: ${path}` }
        : {
            path,
            content: disk[path],
            language: "txt",
            lines: 1,
            size: disk[path].length,
          },
    );
    api.writeFile.mockImplementation(async (path, content) => {
      disk[path] = content;
      return { path };
    });
  });

  it("does not throw the work away when a dirty file is opened again", async () => {
    const view = mount();
    await openAt(view, "a.txt");
    await edit(view, "alpha EDITED");

    // The click that looks exactly like navigation.
    await openAt(view, "a.txt");

    expect(view.result.current.openFile.content).toBe("alpha EDITED");
    expect(view.result.current.dirty).toBe(true);
    expect(view.result.current.openFiles).toHaveLength(1);
  });

  it("refreshes a clean buffer, so an unedited copy never goes stale", async () => {
    const view = mount();
    await openAt(view, "a.txt");

    disk["a.txt"] = "alpha v2"; // changed underneath — the assistant, or git
    await openAt(view, "a.txt");

    expect(view.result.current.openFile.content).toBe("alpha v2");
    expect(view.result.current.dirty).toBe(false);
  });

  it("keeps a dirty flag per buffer, not one for the editor", async () => {
    const view = mount();
    await openAt(view, "a.txt");
    await edit(view, "alpha EDITED");
    expect(view.result.current.dirty).toBe(true);

    await openAt(view, "b.txt");
    expect(view.result.current.dirty).toBe(false);
    expect(view.result.current.openFile.content).toBe("bravo");

    // Back to the first tab: the work and the mark that says it exists both
    // survived the round trip.
    await act(async () => view.result.current.switchFile("a.txt"));
    expect(view.result.current.dirty).toBe(true);
    expect(view.result.current.openFile.content).toBe("alpha EDITED");
  });

  it("does not let typing in one file write into another", async () => {
    const view = mount();
    await openAt(view, "a.txt");
    await openAt(view, "b.txt");
    await edit(view, "bravo EDITED");

    await act(async () => view.result.current.switchFile("a.txt"));
    expect(view.result.current.openFile.content).toBe("alpha");
    expect(view.result.current.dirty).toBe(false);
  });

  it("derives dirty from the baseline, so undoing an edit clears it", async () => {
    const view = mount();
    await openAt(view, "a.txt");

    await edit(view, "alpha EDITED");
    expect(view.result.current.dirty).toBe(true);

    await edit(view, "alpha"); // the user typed it back
    expect(view.result.current.dirty).toBe(false);
  });

  it("refuses to close a buffer with work in it, and closes a clean one", async () => {
    const view = mount();
    await openAt(view, "a.txt");
    await openAt(view, "b.txt");
    await act(async () => view.result.current.switchFile("a.txt"));
    await edit(view, "alpha EDITED");

    let closed;
    act(() => {
      closed = view.result.current.closeFile("a.txt");
    });
    expect(closed).toBe(false);
    expect(view.result.current.openFiles.map((f) => f.path)).toEqual([
      "a.txt",
      "b.txt",
    ]);

    act(() => {
      closed = view.result.current.closeFile("b.txt");
    });
    expect(closed).toBe(true);
    expect(view.result.current.openFiles.map((f) => f.path)).toEqual(["a.txt"]);
    // The closed tab was the active one, so the previous buffer takes over
    // rather than leaving an active id that points at nothing.
    expect(view.result.current.activeFileId).toBe("a.txt");
    expect(view.result.current.openFile.path).toBe("a.txt");
  });

  it("empties the editor when the last buffer closes", async () => {
    const view = mount();
    await openAt(view, "a.txt");
    act(() => {
      view.result.current.closeFile("a.txt");
    });
    expect(view.result.current.openFiles).toEqual([]);
    expect(view.result.current.activeFileId).toBeNull();
    expect(view.result.current.openFile).toBeNull();
    expect(view.result.current.dirty).toBe(false);
  });

  it("reports a refused save instead of pretending it landed", async () => {
    const view = mount();
    await openAt(view, "a.txt");
    await edit(view, "alpha EDITED");
    api.writeFile.mockResolvedValueOnce({ error: "Disk is read-only" });

    let result;
    await act(async () => {
      result = await view.result.current.save();
    });

    // Returned, not swallowed: the editor's `if (result?.error)` used to be
    // unreachable, so a failure cleared the review pane as a success would.
    expect(result?.error).toBe("Disk is read-only");
    expect(view.result.current.dirty).toBe(true);
    expect(view.result.current.error).toBe("Disk is read-only");
    expect(disk["a.txt"]).toBe("alpha"); // and the disk is untouched
  });

  it("moves the baseline on a successful save so the buffer reads as clean", async () => {
    const view = mount();
    await openAt(view, "a.txt");
    await edit(view, "alpha EDITED");

    await act(async () => {
      await view.result.current.save();
    });

    expect(disk["a.txt"]).toBe("alpha EDITED");
    expect(view.result.current.dirty).toBe(false);
    expect(view.result.current.error).toBeNull();
    expect(view.result.current.openFile.content).toBe("alpha EDITED");
  });

  it("reverts to what is on disk, and picks up changes made underneath", async () => {
    const view = mount();
    await openAt(view, "a.txt");
    await edit(view, "alpha EDITED");
    disk["a.txt"] = "alpha from git"; // something else changed the file

    await act(async () => {
      await view.result.current.discard();
    });

    expect(view.result.current.openFile.content).toBe("alpha from git");
    expect(view.result.current.dirty).toBe(false);
    expect(api.readFile).toHaveBeenCalledTimes(2);
  });

  it("leaves the tabs alone when a read fails", async () => {
    const view = mount();

    let result;
    await act(async () => {
      result = await view.result.current.open("missing.txt");
    });

    expect(result).toBeNull();
    expect(view.result.current.openFiles).toEqual([]);
    expect(view.result.current.openFile).toBeNull();
    expect(view.result.current.error).toBeTruthy();
  });

  it("keeps the buffer when a revert cannot read the file", async () => {
    const view = mount();
    await openAt(view, "a.txt");
    await edit(view, "alpha EDITED");
    api.readFile.mockResolvedValueOnce({ error: "file is locked" });

    let ok;
    await act(async () => {
      ok = await view.result.current.discard();
    });

    expect(ok).toBe(false);
    // A revert that could not happen must not leave the buffer looking
    // untouched — that is the difference between "I failed" and "it worked
    // and there is nothing to see".
    expect(view.result.current.openFile.content).toBe("alpha EDITED");
    expect(view.result.current.dirty).toBe(true);
    expect(view.result.current.error).toBe("file is locked");
  });

  it("reports one saving flag around the write, and nothing after", async () => {
    const view = mount();
    await openAt(view, "a.txt");
    await edit(view, "alpha EDITED");
    expect(view.result.current.saving).toBe(false);

    let promise;
    act(() => {
      promise = view.result.current.save();
    });
    expect(view.result.current.saving).toBe(true);

    await act(async () => {
      await promise;
    });
    expect(view.result.current.saving).toBe(false);
  });
});
