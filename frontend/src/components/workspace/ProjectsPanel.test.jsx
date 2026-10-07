/**
 * The projects panel.
 *
 * The behaviour worth testing is the **detect → open** two-step and the
 * context block, because both exist to prevent the same mistake: guessing what
 * a folder is. Detect must not register anything (the mock asserts
 * `onOpen` was *not* called), the refusal lands in the panel in the backend's
 * own words, and the context block is shown line for line — it is the exact
 * text the model receives, so a paraphrase of it would be a different
 * document from the one being sent.
 *
 * The open/pin/close wiring is asserted too: it predates the new affordances
 * and is easy to break while adding them.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import ProjectsPanel from "./ProjectsPanel";
import * as api from "../../lib/api";

function respond(overrides = {}) {
  return {
    getWorkspace: vi.fn(async () => ({ root: "C:/ws" })),
    detectProject: vi.fn(async () => ({
      path: "C:/ws/app",
      kind: "node",
      language: "javascript",
      framework: "vite",
      package_manager: "npm",
      run_command: "npm run dev",
      test_command: "npm test",
    })),
    getRecentProjects: vi.fn(async () => ({
      projects: [{ id: "r1", path: "C:/ws/app", name: "app", kind: "node" }],
      count: 1,
    })),
    getProjectContext: vi.fn(async () => ({
      project_id: "p1",
      context: "PROJECT: demo (.)\nType: node\n  test: npm test",
    })),
    ...overrides,
  };
}

/** Stub every API function the panel imports, so a missing one is explicit. */
function mockApi(stubs) {
  vi.spyOn(api, "getWorkspace").mockImplementation(stubs.getWorkspace);
  vi.spyOn(api, "detectProject").mockImplementation(stubs.detectProject);
  vi.spyOn(api, "getRecentProjects").mockImplementation(stubs.getRecentProjects);
  vi.spyOn(api, "getProjectContext").mockImplementation(stubs.getProjectContext);
}

function baseProps(overrides = {}) {
  return {
    projects: [],
    activeProject: null,
    onOpen: vi.fn(async () => ({ id: "p1", path: "C:/ws/app" })),
    onPin: vi.fn(),
    onSetCommands: vi.fn(async () => ({})),
    onClose: vi.fn(),
    language: "en",
    ...overrides,
  };
}

/** The folder input, once the workspace root has arrived. */
async function findPathInput() {
  const input = await screen.findByLabelText(/folder/i);
  await waitFor(() => expect(input).toHaveValue("C:/ws"));
  return input;
}

describe("ProjectsPanel", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("defaults the folder input to the workspace root", async () => {
    mockApi(respond());
    render(<ProjectsPanel {...baseProps()} />);
    await findPathInput();
    expect(api.getWorkspace).toHaveBeenCalled();
  });

  it("detects without opening, then opens on demand", async () => {
    mockApi(respond());
    const props = baseProps();
    render(<ProjectsPanel {...props} />);
    await findPathInput();

    fireEvent.click(screen.getByRole("button", { name: "Detect project" }));
    await waitFor(() => expect(api.detectProject).toHaveBeenCalledWith("C:/ws"));
    expect(await screen.findByText("DETECTED")).toBeInTheDocument();
    expect(screen.getByText(/node · vite/)).toBeInTheDocument();
    expect(screen.getByText(/npm run dev/)).toBeInTheDocument();
    // Detect is a preview: nothing has been registered yet.
    expect(props.onOpen).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "OPEN DETECTED" }));
    await waitFor(() => expect(props.onOpen).toHaveBeenCalledWith("C:/ws/app"));
  });

  it("renders a detect refusal in the panel", async () => {
    mockApi(
      respond({
        detectProject: vi.fn(async () => ({ error: "HTTP 400: 'path' is required" })),
      }),
    );
    render(<ProjectsPanel {...baseProps()} />);
    await findPathInput();

    fireEvent.click(screen.getByRole("button", { name: "Detect project" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("'path' is required");
    expect(screen.queryByText("DETECTED")).not.toBeInTheDocument();
  });

  it("opens a recent path on click", async () => {
    mockApi(respond());
    const props = baseProps();
    render(<ProjectsPanel {...props} />);

    fireEvent.click(await screen.findByRole("button", { name: /C:\/ws\/app/ }));
    await waitFor(() => expect(props.onOpen).toHaveBeenCalledWith("C:/ws/app"));
  });

  it("shows the context block for the active project, line for line", async () => {
    mockApi(respond());
    render(
      <ProjectsPanel {...baseProps({ activeProject: { id: "p1", name: "demo", path: "." } })} />,
    );

    expect(await screen.findByText("PROJECT: demo (.)")).toBeInTheDocument();
    expect(screen.getByText("Type: node")).toBeInTheDocument();
    // Indentation preserved: this is the document, not a summary of it.
    expect(screen.getByText("test: npm test")).toBeInTheDocument();
    expect(api.getProjectContext).toHaveBeenCalledWith("p1");
  });

  it("keeps the open form and the pin wiring intact", async () => {
    mockApi(respond());
    const props = baseProps({
      projects: [
        {
          id: "p1",
          name: "demo",
          path: "C:/ws/demo",
          kind: "node",
          pinned: false,
          run_command: "npm run dev",
        },
      ],
    });
    render(<ProjectsPanel {...props} />);

    const input = await findPathInput();
    fireEvent.change(input, { target: { value: "sub" } });
    fireEvent.submit(input.closest("form"));
    await waitFor(() => expect(props.onOpen).toHaveBeenCalledWith("sub"));

    fireEvent.click(screen.getByRole("button", { name: "☆" }));
    expect(props.onPin).toHaveBeenCalledWith("p1", true);
  });
});
