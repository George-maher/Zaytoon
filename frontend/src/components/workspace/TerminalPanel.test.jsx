/**
 * The integrated terminal.
 *
 * The panel is a thin, honest window onto a process the backend owns, so the
 * behaviour worth pinning is at the seams:
 *
 * - opening a session from the empty state: the callback's result decides
 *   everything — on success the panel watches and catches up on output; on a
 *   refusal (403, shell rules) the backend's own words land in `role="alert"`
 *   and the typed command is *kept*, because the user needs to correct it;
 * - output arrives in a `role="log"` with `aria-live`, so it is announced;
 * - the input is disabled for a process that is no longer running — a text
 *   field that accepts input nobody will ever read is a lie;
 * - sending appends the newline the process needs to see a complete line.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import TerminalPanel from "./TerminalPanel";

const RUNNING_SESSION = {
  id: "s1",
  name: "dev",
  command: "npm run dev",
  pid: 4242,
  status: "running",
};

function renderPanel(props = {}) {
  const handlers = {
    onOpen: vi.fn(async () => ({ id: "s1" })),
    onSend: vi.fn(),
    onClose: vi.fn(),
    onRefreshOutput: vi.fn(),
    onWatch: vi.fn(),
  };
  render(
    <TerminalPanel
      sessions={[]}
      lines={[]}
      language="en"
      {...handlers}
      {...props}
    />,
  );
  return handlers;
}

describe("TerminalPanel", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("opens a session from the empty state and immediately watches it", async () => {
    const handlers = renderPanel();
    const input = screen.getByLabelText("New command");
    fireEvent.change(input, { target: { value: "npm run dev" } });
    fireEvent.submit(input.closest("form"));

    await waitFor(() => expect(handlers.onOpen).toHaveBeenCalledWith("npm run dev"));
    // Watch + catch-up, in that order: the session exists but nothing has been
    // said about it on this socket yet, so the panel would sit blank.
    await waitFor(() => expect(handlers.onWatch).toHaveBeenCalledWith("s1"));
    expect(handlers.onRefreshOutput).toHaveBeenCalledWith("s1");
    expect(input).toHaveValue("");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows a refusal in the backend's own words and keeps the input", async () => {
    const handlers = renderPanel({
      onOpen: vi.fn(async () => ({ error: "Command chaining is not allowed" })),
    });
    const input = screen.getByLabelText("New command");
    fireEvent.change(input, { target: { value: "ls && ls" } });
    fireEvent.submit(input.closest("form"));

    // The exact message, not a paraphrase: "no pipes" and "outside the
    // workspace" need different corrections from the user.
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Command chaining is not allowed",
    );
    expect(handlers.onWatch).not.toHaveBeenCalled();
    expect(input).toHaveValue("ls && ls");
  });

  it("streams output into a live log and sends input to a running process", async () => {
    const handlers = renderPanel({
      sessions: [RUNNING_SESSION],
      lines: [
        { text: "ready on http://localhost:5173", stream: "stdout" },
        { text: "warn: port in use", stream: "stderr" },
      ],
    });

    expect(screen.getByText(/npm run dev · pid 4242 · running/)).toBeInTheDocument();
    const log = screen.getByRole("log");
    expect(log).toHaveAttribute("aria-live", "polite");
    expect(log).toHaveTextContent("ready on http://localhost:5173");
    expect(log).toHaveTextContent("warn: port in use");

    const input = screen.getByLabelText("Send to the process");
    fireEvent.change(input, { target: { value: "ls" } });
    fireEvent.submit(input.closest("form"));
    // The trailing newline is what makes a line a line for the process.
    expect(handlers.onSend).toHaveBeenCalledWith("s1", "ls\n");
  });

  it("disables the input once the process has stopped", () => {
    renderPanel({
      sessions: [{ ...RUNNING_SESSION, status: "exited", exit_code: 0 }],
      lines: [],
    });

    expect(screen.getByText(/npm run dev · pid 4242 · exited → 0/)).toBeInTheDocument();
    expect(screen.getByLabelText("Send to the process")).toBeDisabled();
    expect(screen.getByText("(no output yet)")).toBeInTheDocument();
  });

  it("selects among sessions and closes the active one", async () => {
    const handlers = renderPanel({
      sessions: [RUNNING_SESSION, { ...RUNNING_SESSION, id: "s2", name: "test", status: "running" }],
    });

    // The selector only appears with more than one session — a dropdown of
    // one is chrome that hides the command line it sits above.
    fireEvent.change(screen.getByLabelText("Select a session"), { target: { value: "s2" } });
    await waitFor(() => expect(handlers.onWatch).toHaveBeenCalledWith("s2"));
    expect(handlers.onRefreshOutput).toHaveBeenCalledWith("s2");

    fireEvent.click(screen.getByRole("button", { name: "CLOSE" }));
    expect(handlers.onClose).toHaveBeenCalledWith("s2");
  });

  it("labels the panel in Arabic when asked", () => {
    renderPanel({ language: "ar" });

    expect(screen.getByText("الطرفية")).toBeInTheDocument();
    expect(screen.getByLabelText("أمر جديد")).toBeInTheDocument();
  });
});
