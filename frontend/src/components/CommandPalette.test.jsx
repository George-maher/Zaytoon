import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import CommandPalette from "./CommandPalette";

/**
 * The palette is a promise machine: every row is a claim that this build can
 * do that thing. Two invariants are worth pinning, because neither shows up
 * in a screenshot — a row that closes the palette and does nothing, and a
 * "Go to" entry for a screen that does not exist.
 *
 * jsdom implements neither scrollIntoView nor canvas; the App suite stubs the
 * former in `stubBrowserApis`, and this file needs the same because the
 * palette scrolls its selection into view on every keystroke.
 */
beforeEach(() => {
  window.Element.prototype.scrollIntoView = function scrollIntoView() {};
});
const ALL = () => ({
  onClose: vi.fn(),
  onOpenFolder: vi.fn(),
  onOpenFile: vi.fn(),
  onSearch: vi.fn(),
  onOpenTerminal: vi.fn(),
  onToggleExplorer: vi.fn(),
  onAsk: vi.fn(),
  onOpenSettings: vi.fn(),
  onNewTask: vi.fn(),
  onNavigate: vi.fn(),
});

const labels = () => screen.getAllByRole("option").map((o) => o.textContent);

describe("CommandPalette", () => {
  it("renders nothing while closed", () => {
    render(<CommandPalette open={false} onClose={vi.fn()} />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("offers only destinations this build actually mounts", () => {
    render(<CommandPalette open language="en" {...ALL()} />);
    const text = labels().join(" | ");
    expect(text).toMatch(/Go Home/);
    expect(text).toMatch(/Go to Computer/);
    expect(text).toMatch(/Go to Workspace/);
    // Built, so it is offered — same source as the header nav, same gate.
    expect(text).toMatch(/Go to Tasks/);
    // Settings is an overlay — reached by its own command, not by standing in it.
    expect(text).not.toMatch(/Go to Settings/);
    expect(text).toMatch(/Open Settings/);
  });

  it("never offers a command it cannot perform", () => {
    // A missing handler used to yield a row that closed the palette and did
    // nothing — indistinguishable from a broken feature.
    render(
      <CommandPalette
        open
        language="en"
        onClose={vi.fn()}
        onAsk={vi.fn()}
        onNavigate={vi.fn()}
        onOpenSettings={undefined}
        onOpenFolder={undefined}
        onOpenFile={undefined}
        onSearch={undefined}
        onOpenTerminal={undefined}
        onToggleExplorer={undefined}
        onNewTask={undefined}
      />,
    );
    const text = labels().join(" | ");
    expect(text).toMatch(/Ask Zaytoon/);
    expect(text).not.toMatch(/Open Settings/);
    expect(text).not.toMatch(/Search Files/);
    expect(text).not.toMatch(/Open Folder/);
  });

  it("runs Ask Zaytoon and closes", () => {
    const p = ALL();
    render(<CommandPalette open language="en" {...p} />);
    fireEvent.click(screen.getByRole("option", { name: /Ask Zaytoon/ }));
    expect(p.onAsk).toHaveBeenCalledTimes(1);
    expect(p.onClose).toHaveBeenCalledTimes(1);
  });

  it("filters on a command's label or its keywords", () => {
    render(<CommandPalette open language="en" {...ALL()} />);
    const input = screen.getByRole("textbox", { name: /search commands/i });
    fireEvent.change(input, { target: { value: "terminal" } });
    expect(screen.getAllByRole("option")).toHaveLength(1);
    expect(screen.getByRole("option", { name: /Open Terminal/ })).toBeInTheDocument();
  });

  it("moves the selection with the arrow keys and runs it on Enter", () => {
    const p = ALL();
    render(<CommandPalette open language="en" {...p} />);
    const input = screen.getByRole("textbox", { name: /search commands/i });
    const options = () => screen.getAllByRole("option");

    // Command order is the design: Open Folder, Open File, Search Files, …
    expect(options()[0]).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(options()[2]).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(input, { key: "Enter" });
    expect(p.onSearch).toHaveBeenCalledTimes(1);
    expect(p.onClose).toHaveBeenCalledTimes(1);
  });

  it("closes on Escape without running anything", () => {
    const p = ALL();
    render(<CommandPalette open language="en" {...p} />);
    fireEvent.keyDown(screen.getByRole("textbox", { name: /search commands/i }), {
      key: "Escape",
    });
    expect(p.onClose).toHaveBeenCalledTimes(1);
    expect(p.onAsk).not.toHaveBeenCalled();
    expect(p.onOpenFolder).not.toHaveBeenCalled();
  });

  it("stops at the ends of the list instead of wrapping into nothing", () => {
    const p = ALL();
    render(<CommandPalette open language="en" {...p} />);
    const input = screen.getByRole("textbox", { name: /search commands/i });
    const last = screen.getAllByRole("option").length - 1;
    for (let i = 0; i < last + 3; i += 1) fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(screen.getAllByRole("option")[last]).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(input, { key: "Enter" });
    // The final entry is Open Settings, and only it may have run.
    expect(p.onOpenSettings).toHaveBeenCalledTimes(1);
    expect(p.onAsk).not.toHaveBeenCalled();
  });

  it("speaks Arabic, including the navigation it generates", () => {
    render(<CommandPalette open language="ar" {...ALL()} />);
    expect(screen.getByRole("dialog", { name: "لوحة الأوامر" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /اسأل زيتون/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /الرئيسية/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /اذهب إلى الحاسوب/ })).toBeInTheDocument();
    expect(screen.getByPlaceholderText("اكتب أمراً…")).toBeInTheDocument();
  });
});
