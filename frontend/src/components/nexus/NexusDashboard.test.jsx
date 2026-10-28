/**
 * The Nexus dashboard replaces Home as the product's identity screen, so the
 * old HomeScreen shape contract carries over *by behavior*, not by look:
 *
 *  - one prompt over one field: the console input is the field;
 *  - exactly three onboarding actions — open folder, open file, commands —
 *    now living in the launchpad, with the browser path fallback beside them;
 *  - recent workspaces listed from the real REST client and handed to the
 *    open handler;
 *  - no second top-level heading: App's header owns the document's only h1
 *    (and on HOME the dashboard owns the deck instead of the header, so this
 *    screen renders zero h1 either — the top bar's brand is not a heading);
 *  - everything telemetry-shaped renders "—" when the backend has not
 *    answered, and never a fabricated zero.
 *
 * Network is mocked per test; the WebSocket layer is passed in as props, so
 * none of these tests need a real socket.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import NexusDashboard from "./NexusDashboard";

vi.mock("../../lib/api", () => ({
  getRecentProjects: vi.fn(() =>
    Promise.resolve({
      projects: [
        { id: "1", name: "Alpha", path: "C:/Alpha" },
        { id: "2", name: "Beta", path: "C:/Beta" },
      ],
    }),
  ),
}));

vi.mock("./useNexusTelemetry", async (importOriginal) => {
  const mod = await importOriginal();
  return {
    ...mod,
    default: vi.fn(() => ({
      system: null,
      systemError: null,
      health: null,
      history: { cpu: [], memory: [], disk: [], load: [] },
    })),
  };
});

vi.mock("../../lib/desktop", () => ({
  desktop: { available: false },
}));

vi.mock("../InputBar", () => ({
  default: function InputBar({ onSend, isConnected }) {
    return (
      <div>
        <input aria-label="Your message" disabled={!isConnected} data-testid="nexus-input" />
        <button
          onClick={() => onSend("ping")}
          aria-label="Send message"
          disabled={!isConnected}
        />
      </div>
    );
  },
}));

const baseProps = {
  language: "en",
  isConnected: true,
  state: "idle",
  isListening: false,
  isSpeaking: false,
  isTyping: false,
  messages: [],
  sendMessage: vi.fn(),
  startVoiceMode: vi.fn(),
  stopVoiceMode: vi.fn(),
  voiceMode: false,
  requestStop: vi.fn(),
  clearChat: vi.fn(),
  onOpenFolder: vi.fn(),
  onOpenFile: vi.fn(),
  onOpenProject: vi.fn(),
  onOpenCommands: vi.fn(),
  onSettingsToggle: vi.fn(),
  onNavigate: vi.fn(),
};

async function renderDash(overrides = {}) {
  const utils = render(<NexusDashboard {...baseProps} {...overrides} />);
  // The launchpad mounts closed; open it via either entry (top bar or dock).
  const entries = screen.getAllByRole("button", { name: "All commands" });
  fireEvent.click(entries[0]);
  return utils;
}

beforeEach(() => {
  for (const key of Object.keys(baseProps)) {
    if (typeof baseProps[key] === "function") baseProps[key].mockClear();
  }
});

describe("NexusDashboard", () => {
  it("carries the console: an input over the field that answers it", async () => {
    await renderDash();
    expect(screen.getByLabelText(/your message/i)).toBeInTheDocument();
  });

  it("sends through the same handler a typed message takes", async () => {
    await renderDash();
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    expect(baseProps.sendMessage).toHaveBeenCalledWith("ping");
  });

  it("offers the three onboarding actions in the launchpad, and only three", async () => {
    await renderDash();
    expect(screen.getByRole("button", { name: "OPEN FOLDER" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "OPEN FILE" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "COMMANDS" })).toBeInTheDocument();
    // The reference shows many more tiles; the build keeps the promise only
    // an action can keep.
    expect(screen.queryByRole("button", { name: "DEPLOY" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "SCAN" })).not.toBeInTheDocument();
  });

  it("opens the command palette from the launchpad's third action", async () => {
    await renderDash();
    fireEvent.click(screen.getByRole("button", { name: "COMMANDS" }));
    expect(baseProps.onOpenCommands).toHaveBeenCalledTimes(1);
  });

  it("keeps the browser path fallback and its OPEN submit", async () => {
    await renderDash();
    expect(screen.getByLabelText(/path/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "OPEN" })).toBeInTheDocument();
  });

  it("runs the open handler with the typed path and stays", async () => {
    await renderDash();
    fireEvent.change(screen.getByLabelText(/path/i), {
      target: { value: "C:/somewhere" },
    });
    fireEvent.click(screen.getByRole("button", { name: "OPEN FILE" }));
    expect(baseProps.onOpenFile).toHaveBeenCalledWith("C:/somewhere");
    // This screen never navigates itself — it hands the path over.
    expect(baseProps.onOpenFolder).not.toHaveBeenCalled();
  });

  it("hands recent workspaces to the open handler", async () => {
    await renderDash();
    fireEvent.click(
      await screen.findByRole("button", { name: /alpha/i }),
    );
    expect(baseProps.onOpenProject).toHaveBeenCalledWith("C:/Alpha");
  });

  it("carries no top-level heading", async () => {
    await renderDash();
    // App renders the document's h1 in its header, which is absent on HOME —
    // so the document as a whole has exactly one, and this card has none.
    expect(screen.queryAllByRole("heading", { level: 1 })).toHaveLength(0);
  });

  it("maps the product's real states to the reference's words", async () => {
    const { rerender } = render(<NexusDashboard {...baseProps} state="listening" />);
    // The word appears in the pill, the core SVG label and the core word —
    // any one of them proves the state rode through.
    const markers = await screen.findAllByText("LISTENING");
    expect(markers.length).toBeGreaterThan(0);

    rerender(<NexusDashboard {...baseProps} state="speaking" />);
    expect(screen.getAllByText("SPEAKING").length).toBeGreaterThan(0);
  });

  it("shows OFFLINE, not a state word, when the socket is down", async () => {
    render(<NexusDashboard {...baseProps} isConnected={false} state="offline" />);
    // The word rides the state pill, the core's aria label and the core word.
    const markers = await screen.findAllByText("OFFLINE");
    expect(markers.length).toBeGreaterThan(0);
  });

  it("toggles voice from the core", async () => {
    render(<NexusDashboard {...baseProps} />);
    fireEvent.click(screen.getByRole("button", { name: /start voice input/i }));
    expect(baseProps.startVoiceMode).toHaveBeenCalledTimes(1);
    expect(baseProps.stopVoiceMode).not.toHaveBeenCalled();

    render(<NexusDashboard {...baseProps} voiceMode />);
    fireEvent.click(screen.getByRole("button", { name: /stop voice input/i }));
    expect(baseProps.stopVoiceMode).toHaveBeenCalledTimes(1);
  });

  it("renders the dash for telemetry the backend has not answered", async () => {
    render(<NexusDashboard {...baseProps} />);
    // The headline load metric comes straight from /system, which is stubbed
    // to an error here: the tile must show the dash, never a zero.
    const dash = await screen.findAllByText("—");
    expect(dash.length).toBeGreaterThan(0);
  });

  it("quick chips send real commands", async () => {
    await renderDash();
    fireEvent.click(screen.getByRole("button", { name: "status" }));
    expect(baseProps.sendMessage).toHaveBeenCalledWith("system information");
  });

  it("clears the conversation from the console header", async () => {
    await renderDash();
    fireEvent.click(screen.getByRole("button", { name: /clear conversation/i }));
    expect(baseProps.clearChat).toHaveBeenCalledTimes(1);
  });

  it("docks navigate to real destinations", async () => {
    await renderDash();
    fireEvent.click(screen.getByRole("button", { name: "Computer" }));
    expect(baseProps.onNavigate).toHaveBeenCalledWith("computer");
    fireEvent.click(screen.getByRole("button", { name: "Tasks" }));
    expect(baseProps.onNavigate).toHaveBeenCalledWith("tasks");
  });

  it("asks the same things in Arabic", async () => {
    render(<NexusDashboard {...baseProps} language="ar" />);
    // The top bar and the dock both carry the palette button — open either.
    fireEvent.click(screen.getAllByRole("button", { name: "كل الأوامر" })[0]);
    expect(screen.getByRole("button", { name: "الأوامر" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /alpha/i })).toBeInTheDocument();
  });
});
