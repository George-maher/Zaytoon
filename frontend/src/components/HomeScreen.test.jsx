/**
 * Home is the screen that sets the product's identity, so its shape is
 * pinned here rather than left to taste:
 *
 *  - one question, one field, three actions — and only three, because only
 *    three exist;
 *  - recent workspaces collapsed by default (they are the tallest block of
 *    text on the page and the question above is the point of the screen),
 *    but discoverable behind a count;
 *  - no second top-level heading: App's header owns the document's only h1,
 *    which is exactly what WorkspaceShell's comment says and what Home had
 *    quietly been violating;
 *  - the browser path fallback, which App's runtime suite drives to reach
 *    the workspace, is still there.
 *
 * Every test awaits the recents before asserting: the fetch resolves after
 * render, so an early assertion would leave a state update outside `act()`
 * and pass for the wrong reason.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import HomeScreen from "./HomeScreen";

vi.mock("../lib/api", () => ({
  getRecentProjects: vi.fn(() =>
    Promise.resolve({
      projects: [
        { id: "1", name: "Alpha", path: "C:/Alpha" },
        { id: "2", name: "Beta", path: "C:/Beta" },
      ],
    }),
  ),
}));

const baseProps = {
  language: "en",
  isConnected: true,
  onOpenFolder: vi.fn(),
  onOpenFile: vi.fn(),
  onOpenProject: vi.fn(),
  onSendCommand: vi.fn(),
  onOpenCommands: vi.fn(),
  onVoiceStart: vi.fn(),
  onVoiceStop: vi.fn(),
  voiceMode: false,
};

/** Render, then wait for the recents so the fetch settles inside `act()`. */
async function renderHome(overrides = {}) {
  const utils = render(<HomeScreen {...baseProps} {...overrides} />);
  await waitFor(() => expect(document.querySelector('[aria-controls="home-recent"]')).toBeTruthy());
  return utils;
}

beforeEach(() => {
  for (const key of Object.keys(baseProps)) {
    if (typeof baseProps[key] === "function") baseProps[key].mockClear();
  }
});

describe("HomeScreen", () => {
  it("asks one question, over the field that answers it", async () => {
    await renderHome();
    const prompt = screen.getByRole("heading", { level: 2, name: /what do you want to do/i });
    const input = screen.getByLabelText(/your message/i);
    // The prompt labels the input's block, so it must precede it in the DOM —
    // not merely appear earlier in some string.
    expect(prompt.compareDocumentPosition(input) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("offers exactly three actions, because only three exist", async () => {
    await renderHome();
    expect(screen.getByRole("button", { name: "OPEN FOLDER" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "OPEN FILE" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "COMMANDS" })).toBeInTheDocument();
    // A fourth entry would be a promise the build cannot keep.
    expect(screen.queryByRole("button", { name: "COMPUTER" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "TASKS" })).not.toBeInTheDocument();
  });

  it("opens the command palette from the third action", async () => {
    await renderHome();
    fireEvent.click(screen.getByRole("button", { name: "COMMANDS" }));
    expect(baseProps.onOpenCommands).toHaveBeenCalledTimes(1);
  });

  it("keeps the browser path fallback and its OPEN submit", async () => {
    await renderHome();
    expect(screen.getByLabelText(/path/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "OPEN" })).toBeInTheDocument();
  });

  it("runs the open handler with the typed path", async () => {
    await renderHome();
    fireEvent.change(screen.getByLabelText(/path/i), {
      target: { value: "C:/somewhere" },
    });
    fireEvent.click(screen.getByRole("button", { name: "OPEN FILE" }));
    expect(baseProps.onOpenFile).toHaveBeenCalledWith("C:/somewhere");
    // This screen never navigates itself — it hands the path over.
    expect(baseProps.onOpenFolder).not.toHaveBeenCalled();
    expect(
      screen.getByRole("heading", { level: 2, name: /what do you want to do/i }),
    ).toBeInTheDocument();
  });

  it("collapses recent workspaces behind a count until asked for", async () => {
    await renderHome();
    const toggle = screen.getByRole("button", { name: /recent workspaces/i });
    // Collapsed: eight absolute paths are the tallest block on a screen
    // whose whole job is the question above.
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Alpha")).not.toBeInTheDocument();

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(await screen.findByText("Alpha")).toBeInTheDocument();
    expect(screen.getByText("Beta")).toBeInTheDocument();

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await waitFor(() => expect(screen.queryByText("Alpha")).not.toBeInTheDocument());
  });

  it("opens a recent project through its handler", async () => {
    await renderHome();
    fireEvent.click(screen.getByRole("button", { name: /recent workspaces/i }));
    fireEvent.click(await screen.findByRole("button", { name: /alpha/i }));
    expect(baseProps.onOpenProject).toHaveBeenCalledWith("C:/Alpha");
  });

  it("carries no second top-level heading", async () => {
    // App's header renders the document's h1; a second one here gave
    // assistive technology two top-level headings for one product.
    await renderHome();
    expect(screen.queryAllByRole("heading", { level: 1 })).toHaveLength(0);
  });

  it("asks the same question in Arabic, untracked", async () => {
    await renderHome({ language: "ar" });
    const prompt = screen.getByRole("heading", { level: 2, name: "ماذا تريد أن تفعل؟" });
    expect(prompt).toBeInTheDocument();
    // Letter-spacing tears a connected script apart. jsdom reports the
    // zero as written rather than normalising it to a length.
    expect(prompt.style.letterSpacing).toBe("0");
    expect(screen.getByRole("button", { name: "فتح مجلد" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "الأوامر" })).toBeInTheDocument();
  });
});
