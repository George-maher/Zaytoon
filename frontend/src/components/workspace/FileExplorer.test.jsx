/**
 * The file explorer.
 *
 * The behaviour worth testing is **what the panel does with the server**,
 * because every operation here is a request the backend may refuse: a 403
 * outside the workspace, a 409 on an existing path, a directory that can only
 * be deleted recursively. Each refusal has to land in the panel in the
 * backend's own words — a file manager that swallows them looks identical
 * whether it worked or not.
 *
 * And the destructive path: delete is a two-step arm-then-confirm inside the
 * panel (no `window.confirm`), the confirm row names the exact entry, and a
 * directory sends `recursive: true` because that is what the server needs.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent, act } from "@testing-library/react";
import FileExplorer from "./FileExplorer";
import * as api from "../../lib/api";

const TREE = {
  truncated: false,
  entries: [
    {
      name: "src",
      path: "src",
      is_dir: true,
      children: [{ name: "app.js", path: "src/app.js", is_dir: false, language: "javascript" }],
    },
    { name: "notes.md", path: "notes.md", is_dir: false, language: "markdown" },
  ],
};

function respond(overrides = {}) {
  return {
    statFile: vi.fn(async () => ({
      path: "notes.md",
      name: "notes.md",
      is_dir: false,
      size: 2048,
      modified: "2026-10-01T12:34:00",
      language: "markdown",
    })),
    createFolder: vi.fn(async () => ({ path: "demo", created: true })),
    writeFile: vi.fn(async () => ({ path: "new.txt", bytes: 0 })),
    renameEntry: vi.fn(async () => ({ path: "notes.md" })),
    moveEntry: vi.fn(async () => ({ path: "notes.md" })),
    copyEntry: vi.fn(async () => ({ path: "notes.md" })),
    deleteEntry: vi.fn(async () => ({ path: "notes.md" })),
    searchFiles: vi.fn(async () => ({ matches: [], count: 0, truncated: false })),
    listDirectory: vi.fn(async () => ({
      path: ".",
      entries: [
        {
          name: ".env",
          path: ".env",
          is_dir: false,
          size: 12,
          modified: "2026-09-30T08:00:00",
          language: "plaintext",
        },
      ],
      count: 1,
      truncated: false,
    })),
    ...overrides,
  };
}

/** Stub every API function the panel imports, so a missing one is explicit. */
function mockApi(stubs) {
  vi.spyOn(api, "statFile").mockImplementation(stubs.statFile);
  vi.spyOn(api, "createFolder").mockImplementation(stubs.createFolder);
  vi.spyOn(api, "writeFile").mockImplementation(stubs.writeFile);
  vi.spyOn(api, "renameEntry").mockImplementation(stubs.renameEntry);
  vi.spyOn(api, "moveEntry").mockImplementation(stubs.moveEntry);
  vi.spyOn(api, "copyEntry").mockImplementation(stubs.copyEntry);
  vi.spyOn(api, "deleteEntry").mockImplementation(stubs.deleteEntry);
  vi.spyOn(api, "searchFiles").mockImplementation(stubs.searchFiles);
  vi.spyOn(api, "listDirectory").mockImplementation(stubs.listDirectory);
}

function renderPanel(props = {}) {
  const handlers = {
    onToggle: vi.fn(),
    onOpen: vi.fn(),
    onRefresh: vi.fn(),
  };
  render(
    <FileExplorer
      tree={TREE}
      expanded={new Set()}
      openPath={null}
      language="en"
      {...handlers}
      {...props}
    />,
  );
  return handlers;
}

describe("FileExplorer", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("lists the tree and opens a file on click", async () => {
    mockApi(respond());
    const { onOpen } = renderPanel();
    expect(await screen.findByRole("treeitem", { name: "notes.md" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "notes.md" }));
    expect(onOpen).toHaveBeenCalledWith("notes.md");
  });

  it("creates a folder from the inline row and refreshes", async () => {
    mockApi(respond());
    const { onRefresh } = renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "New folder" }));
    const input = await screen.findByLabelText("New folder name");
    fireEvent.change(input, { target: { value: "demo" } });
    fireEvent.submit(input.closest("form"));
    await waitFor(() => expect(onRefresh).toHaveBeenCalled());
    expect(api.createFolder).toHaveBeenCalledWith("demo");
    await waitFor(() =>
      expect(screen.queryByLabelText("New folder name")).not.toBeInTheDocument(),
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("renders the server's refusal when a folder cannot be created", async () => {
    mockApi(
      respond({
        createFolder: vi.fn(async () => ({
          error: "'..\\evil' is outside the workspace (C:/ws)",
        })),
      }),
    );
    const { onRefresh } = renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "New folder" }));
    const input = await screen.findByLabelText("New folder name");
    fireEvent.change(input, { target: { value: "evil" } });
    fireEvent.submit(input.closest("form"));
    // The backend's own sentence, in the panel — not a paraphrase.
    expect(await screen.findByRole("alert")).toHaveTextContent("outside the workspace");
    // The row stays open, with the typed name, so it can be corrected.
    expect(screen.getByLabelText("New folder name")).toHaveValue("evil");
    expect(onRefresh).not.toHaveBeenCalled();
  });

  it("creates an empty file when nothing exists at that path", async () => {
    mockApi(
      respond({
        statFile: vi.fn(async () => ({ error: "Not found: new.txt" })),
      }),
    );
    const { onRefresh } = renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "New file" }));
    const input = await screen.findByLabelText("New file name");
    fireEvent.change(input, { target: { value: "new.txt" } });
    fireEvent.submit(input.closest("form"));
    await waitFor(() => expect(onRefresh).toHaveBeenCalled());
    // The API has no create-file endpoint, and `writeFile` would silently
    // overwrite — so creation is an existence check plus an empty write.
    expect(api.writeFile).toHaveBeenCalledWith("new.txt", "");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("refuses to create a file over an existing one", async () => {
    mockApi(respond());
    const { onRefresh } = renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "New file" }));
    const input = await screen.findByLabelText("New file name");
    fireEvent.change(input, { target: { value: "notes.md" } });
    fireEvent.submit(input.closest("form"));
    expect(await screen.findByRole("alert")).toHaveTextContent("notes.md already exists");
    expect(api.writeFile).not.toHaveBeenCalled();
    expect(onRefresh).not.toHaveBeenCalled();
  });

  it("renames an entry through the inline input", async () => {
    mockApi(respond());
    const { onRefresh } = renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "notes.md" }));
    fireEvent.click(await screen.findByRole("button", { name: "Rename" }));
    const input = await screen.findByLabelText("New name");
    expect(input).toHaveValue("notes.md");
    fireEvent.change(input, { target: { value: "better.md" } });
    fireEvent.submit(input.closest("form"));
    await waitFor(() => expect(onRefresh).toHaveBeenCalled());
    expect(api.renameEntry).toHaveBeenCalledWith("notes.md", "better.md");
  });

  it("shows a refused rename with the server's message and keeps the input", async () => {
    mockApi(
      respond({
        renameEntry: vi.fn(async () => ({
          error: "The new name must be a bare filename. Use move to change directories.",
        })),
      }),
    );
    const { onRefresh } = renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "notes.md" }));
    fireEvent.click(await screen.findByRole("button", { name: "Rename" }));
    const input = await screen.findByLabelText("New name");
    fireEvent.change(input, { target: { value: "nested/name.md" } });
    fireEvent.submit(input.closest("form"));
    expect(await screen.findByRole("alert")).toHaveTextContent("bare filename");
    expect(screen.getByLabelText("New name")).toHaveValue("nested/name.md");
    expect(onRefresh).not.toHaveBeenCalled();
  });

  it("arms a directory delete, then sends it recursive on the second click", async () => {
    mockApi(
      respond({
        statFile: vi.fn(async () => ({
          path: "src",
          name: "src",
          is_dir: true,
          size: 0,
          modified: "2026-10-01T12:34:00",
        })),
      }),
    );
    const { onRefresh } = renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "src" }));
    fireEvent.click(await screen.findByRole("button", { name: "Delete src" }));

    // Armed, not executed: the confirm row names the exact entry and says the
    // delete is recursive, because that is what the request will send.
    expect(api.deleteEntry).not.toHaveBeenCalled();
    expect(await screen.findByText('Delete "src"?')).toBeInTheDocument();
    expect(screen.getByText(/recursive/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Confirm delete src" }));
    await waitFor(() => expect(onRefresh).toHaveBeenCalled());
    expect(api.deleteEntry).toHaveBeenCalledWith("src", { recursive: true });
  });

  it("deletes a file without the recursive flag", async () => {
    mockApi(respond());
    const { onRefresh } = renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "notes.md" }));
    fireEvent.click(await screen.findByRole("button", { name: "Delete notes.md" }));
    // A file has nothing inside it — the confirm row must not claim otherwise.
    expect(screen.queryByText(/recursive/i)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Confirm delete notes.md" }));
    await waitFor(() => expect(onRefresh).toHaveBeenCalled());
    expect(api.deleteEntry).toHaveBeenCalledWith("notes.md", { recursive: false });
  });

  it("searches the server after a pause in typing and renders the results", async () => {
    vi.useFakeTimers();
    mockApi(
      respond({
        searchFiles: vi.fn(async () => ({
          matches: [{ path: "README.md", name: "README.md", line: 4, column: 2, text: "docs" }],
          count: 1,
          truncated: false,
        })),
      }),
    );
    const { onOpen } = renderPanel();
    const box = screen.getByRole("searchbox", { name: "Search files" });
    fireEvent.change(box, { target: { value: "readme" } });
    // Nothing yet: one request per keystroke is how the explorer gets slow.
    expect(api.searchFiles).not.toHaveBeenCalled();
    act(() => {
      vi.advanceTimersByTime(250);
    });
    await act(async () => {});

    expect(api.searchFiles).toHaveBeenCalledWith("readme", { path: "." });
    expect(screen.getByText("README.md")).toBeInTheDocument();
    // Which field matched: a line number is content, `line: 0` is the name.
    expect(screen.getByText("content line 4")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /README\.md/ }));
    expect(onOpen).toHaveBeenCalledWith("README.md");
    expect(api.statFile).toHaveBeenCalledWith("README.md");
  });

  it("answers a one-character query locally, without a server round trip", async () => {
    vi.useFakeTimers();
    mockApi(respond());
    renderPanel();
    fireEvent.change(screen.getByRole("searchbox", { name: "Search files" }), {
      target: { value: "r" },
    });
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    await act(async () => {});
    expect(api.searchFiles).not.toHaveBeenCalled();
    expect(screen.getByText("1 match")).toBeInTheDocument();
  });

  it("returns to the tree when the search box is cleared", async () => {
    vi.useFakeTimers();
    mockApi(respond());
    renderPanel();
    const box = screen.getByRole("searchbox", { name: "Search files" });
    fireEvent.change(box, { target: { value: "zz" } });
    act(() => {
      vi.advanceTimersByTime(250);
    });
    await act(async () => {});
    expect(screen.getByRole("list", { name: "Search results" })).toBeInTheDocument();
    expect(screen.queryByRole("tree")).not.toBeInTheDocument();

    fireEvent.change(box, { target: { value: "" } });
    await act(async () => {});
    expect(screen.getByRole("tree")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Search results" })).not.toBeInTheDocument();
  });

  it("lists hidden entries on demand, passing show_hidden through", async () => {
    mockApi(respond());
    renderPanel();
    const toggle = await screen.findByRole("button", { name: "Show hidden files" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");

    fireEvent.click(toggle);
    await waitFor(() => expect(api.listDirectory).toHaveBeenCalledWith(".", true));
    expect(await screen.findByText(".env")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show hidden files" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    // Back off returns to the tree, which never contains dotfiles.
    fireEvent.click(screen.getByRole("button", { name: "Show hidden files" }));
    await waitFor(() => expect(screen.queryByText(".env")).not.toBeInTheDocument());
    expect(screen.getByRole("treeitem", { name: "notes.md" })).toBeInTheDocument();
  });

  it("re-reads the current directory on refresh", async () => {
    mockApi(respond());
    const { onRefresh } = renderPanel();

    // Default view: the tree behind the panel is what gets re-read.
    fireEvent.click(await screen.findByRole("button", { name: "Refresh" }));
    expect(onRefresh).toHaveBeenCalledTimes(1);
    expect(api.listDirectory).not.toHaveBeenCalled();

    // Hidden view: the listing *is* the current directory — re-read it too.
    fireEvent.click(screen.getByRole("button", { name: "Show hidden files" }));
    await waitFor(() => expect(api.listDirectory).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(2));
    expect(api.listDirectory).toHaveBeenCalledTimes(2);
    expect(api.listDirectory).toHaveBeenLastCalledWith(".", true);
  });

  it("shows size, type and modified time for the selected entry", async () => {
    mockApi(respond());
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "notes.md" }));
    expect(await screen.findByText("2.0 KB")).toBeInTheDocument();
    expect(screen.getByText("markdown")).toBeInTheDocument();
    expect(screen.getByText("2026-10-01 12:34")).toBeInTheDocument();
    expect(api.statFile).toHaveBeenCalledWith("notes.md");
  });

  it("reports a failed stat in the details line instead of failing the panel", async () => {
    mockApi(
      respond({
        statFile: vi.fn(async () => ({ error: "Not found: gone.txt" })),
      }),
    );
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "notes.md" }));
    expect(await screen.findByText("Not found: gone.txt")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("treeitem", { name: "notes.md" })).toBeInTheDocument();
  });

  it("renders Arabic labels when the language is ar", async () => {
    mockApi(respond());
    render(
      <FileExplorer
        tree={TREE}
        expanded={new Set()}
        onToggle={vi.fn()}
        onOpen={vi.fn()}
        language="ar"
      />,
    );
    expect(await screen.findByText("الملفات")).toBeInTheDocument();
    expect(screen.getByLabelText("بحث في الملفات")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "إظهار الملفات المخفية" })).toBeInTheDocument();
  });

  // -- the "changed by the assistant" marker ---------------------------
  it("marks the row the assistant changed and counts them in the header", async () => {
    mockApi(respond());
    renderPanel({ changed: [{ path: "notes.md", action: "changed", at: 1 }] });

    const row = await screen.findByRole("button", { name: /notes\.md/ });
    expect(row).toHaveTextContent("AI");
    expect(
      screen.getByLabelText("1 file changed by the assistant"),
    ).toBeInTheDocument();

    // The row nothing touched says nothing.
    expect(screen.getByRole("button", { name: /^src/ })).not.toHaveTextContent("AI");
  });

  it("matches an absolute backslashed spelling to the relative row", async () => {
    // The event carries whatever the tool was called with; the tree carries
    // the workspace-relative form. Both must land on the same row.
    mockApi(respond());
    renderPanel({
      changed: [
        { path: "C:\\ws\\ZaytoonWorkspace\\notes.md", action: "changed", at: 1 },
      ],
    });

    const row = await screen.findByRole("button", { name: /notes\.md/ });
    expect(row).toHaveTextContent("AI");
    expect(
      screen.getByLabelText("1 file changed by the assistant"),
    ).toBeInTheDocument();
  });

  it("leaves an untouched row unmarked while still counting the change", async () => {
    mockApi(respond());
    renderPanel({ changed: [{ path: "other.txt", action: "changed", at: 1 }] });

    const row = await screen.findByRole("button", { name: /notes\.md/ });
    expect(row).not.toHaveTextContent("AI");
    // The count is about what changed, not about what happens to be on screen.
    expect(
      screen.getByLabelText("1 file changed by the assistant"),
    ).toBeInTheDocument();
  });

  it("says nothing at all when nothing changed", async () => {
    mockApi(respond());
    renderPanel();

    await screen.findByRole("button", { name: /notes\.md/ });
    expect(screen.queryByText(/AI/)).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText(/changed by the assistant/),
    ).not.toBeInTheDocument();
  });
});
