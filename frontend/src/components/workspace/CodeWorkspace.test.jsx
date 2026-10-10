/**
 * The editing surface.
 *
 * Two rules live here, both invisible in a screenshot and both easy to remove
 * by accident:
 *
 * 1. **A destructive action takes two clicks.** REVERT, and closing a tab that
 *    still holds unsaved work, both arm first and act second — there is no
 *    dialog infrastructure in this component (and `window.confirm` is not
 *    dependable inside a WebView), so the button itself has to say what the
 *    next click will do.
 * 2. **A refused save keeps its preview.** `onSave` returns the API result in
 *    both directions; when it returned `null` on failure, the editor's
 *    `if (result?.error)` was unreachable and the review pane was cleared as
 *    though the write had landed.
 *
 * The close control is also asserted to be a real `<button>` inside the tab:
 * it used to be `<span role="button">` inside `<button>`, which is invalid HTML
 * that browsers repair by pulling the inner element out of the click handler,
 * and it was unreachable by keyboard.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import CodeWorkspace from "./CodeWorkspace";
import * as api from "../../lib/api";

function file(over = {}) {
  return {
    path: "src/a.js",
    content: "let a = 1;",
    language: "js",
    lines: 1,
    size: 10,
    dirty: false,
    ...over,
  };
}

/** Props for a mounted editor; `openFile`/`dirty` stay consistent unless told. */
function base(over = {}) {
  const openFile = over.openFile === undefined ? file() : over.openFile;
  return {
    saving: false,
    language: "en",
    onSave: vi.fn(async () => ({ ok: true })),
    onDiscard: vi.fn(),
    onSetContent: vi.fn(),
    onError: vi.fn(),
    openFiles: [file()],
    activeFileId: "src/a.js",
    onCloseFile: vi.fn(() => true),
    onSwitchFile: vi.fn(),
    ...over,
    openFile,
    dirty: over.dirty === undefined ? Boolean(openFile && openFile.dirty) : over.dirty,
  };
}

describe("CodeWorkspace", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.clearAllMocks();
  });

  it("has nothing to show until a file is open", () => {
    render(<CodeWorkspace {...base({ openFile: null, openFiles: [] })} />);
    expect(screen.getByText(/select a file from the tree/i)).toBeInTheDocument();
  });

  it("labels an unsaved buffer with words, not only a colour dot", () => {
    render(
      <CodeWorkspace {...base({ dirty: true, openFiles: [file({ dirty: true })] })} />,
    );
    // Colour alone cannot carry "this will be lost".
    expect(screen.getByText("unsaved")).toBeInTheDocument();
    expect(screen.getByRole("tab")).toHaveAttribute("aria-selected", "true");
  });

  it("offers a close affordance from one file upward", () => {
    render(<CodeWorkspace {...base()} />);
    const tab = screen.getByRole("tab");
    // A `<button>` inside a `<button>` is invalid and browsers repair it by
    // moving the inner one out of the handler; `role="tab"` on a div is what
    // makes a nested real button legal.
    const close = within(tab).getByRole("button");
    expect(close.tagName).toBe("BUTTON");
    expect(close).toHaveAccessibleName("Close a.js");
  });

  it("arms REVERT before performing it", () => {
    const onDiscard = vi.fn();
    render(<CodeWorkspace {...base({ dirty: true, onDiscard })} />);

    fireEvent.click(screen.getByRole("button", { name: /revert/i }));
    expect(onDiscard).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: /confirm revert/i }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /confirm revert/i }));
    expect(onDiscard).toHaveBeenCalledTimes(1);
  });

  it("arms the close of an unsaved tab, but closes a clean one at once", () => {
    const onCloseFile = vi.fn(() => true);
    render(
      <CodeWorkspace
        {...base({
          dirty: true,
          openFiles: [file({ dirty: true }), file({ path: "src/b.js" })],
          onCloseFile,
        })}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Close a.js" }));
    expect(onCloseFile).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: "Confirm closing a.js" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Confirm closing a.js" }));
    expect(onCloseFile).toHaveBeenCalledWith("src/a.js");
  });

  it("does not claim a close that the store refused", () => {
    // The hook refuses a dirty buffer outright. If this component ever forgot
    // to ask first, `armed` would clear and the tab would *look* dismissed
    // while remaining open — a control lying about its own effect.
    const onCloseFile = vi.fn(() => false);
    render(
      <CodeWorkspace {...base({ dirty: true, openFiles: [file({ dirty: true })], onCloseFile })} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Close a.js" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm closing a.js" }));
    expect(onCloseFile).toHaveBeenCalledTimes(1);
    expect(
      screen.getByRole("button", { name: "Confirm closing a.js" }),
    ).toBeInTheDocument();
  });

  it("answers Ctrl+S only while there is something to save", async () => {
    const onSave = vi.fn(async () => ({ ok: true }));
    const { rerender } = render(<CodeWorkspace {...base({ onSave })} />);

    // Clean buffer: the editor owns the chord and swallows it rather than
    // letting it fall through to a browser's save-page.
    fireEvent.keyDown(window, { key: "s", ctrlKey: true });
    expect(onSave).not.toHaveBeenCalled();

    // Nothing open: left alone entirely.
    rerender(<CodeWorkspace {...base({ onSave, openFile: null, openFiles: [] })} />);
    fireEvent.keyDown(window, { key: "s", ctrlKey: true });
    expect(onSave).not.toHaveBeenCalled();

    rerender(
      <CodeWorkspace
        {...base({ onSave, dirty: true, openFiles: [file({ dirty: true })] })}
      />,
    );
    fireEvent.keyDown(window, { key: "s", ctrlKey: true });
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
  });

  it("keeps the review pane when the write is refused", async () => {
    const onSave = vi.fn(async () => ({ error: "Disk is read-only" }));
    const onError = vi.fn();
    vi.spyOn(api, "diffFile").mockResolvedValue({
      diff: "-let a = 1;\n+let a = 2;",
      changed: true,
      added_lines: 1,
      removed_lines: 1,
    });
    render(
      <CodeWorkspace {...base({ dirty: true, openFiles: [file({ dirty: true })], onSave, onError })} />,
    );

    fireEvent.click(screen.getByRole("button", { name: /review/i }));
    await waitFor(() =>
      expect(screen.getByText("+1 / −1")).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    await waitFor(() =>
      expect(onError).toHaveBeenCalledWith("Disk is read-only"),
    );

    // The save did not happen, so the preview of what it would write still
    // describes reality. Clearing it here was the whole bug.
    expect(screen.getByText("+1 / −1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /save/i })).toBeEnabled();
  });

  it("holds the write actions still while a write is in flight", () => {
    render(
      <CodeWorkspace
        {...base({ dirty: true, saving: true, openFiles: [file({ dirty: true })] })}
      />,
    );
    expect(screen.getByRole("button", { name: /save/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /review/i })).toBeDisabled();
    // …and a second save cannot be queued behind the first.
    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    expect(screen.getByText("…")).toBeInTheDocument();
  });

  it("renders the screen's own labels in Arabic", () => {
    render(
      <CodeWorkspace
        {...base({ language: "ar", dirty: true, openFiles: [file({ dirty: true })] })}
      />,
    );
    expect(screen.getByRole("button", { name: "حفظ" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "تراجع" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "معاينة التغيير" })).toBeInTheDocument();
    expect(screen.getByText("غير محفوظ")).toBeInTheDocument();
  });
});
