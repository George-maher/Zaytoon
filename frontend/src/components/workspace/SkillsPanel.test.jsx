/**
 * The skills panel: what a skill declares, what it ships, and what failed.
 *
 * The inspection lines are the point — `asks for` (the declared permissions)
 * and the detail view's `ASKS FOR` / `TOOL FILES` sit *above* the instructions
 * the user is about to trust, so a skill that wants `filesystem.write` says so
 * before its prose gets read. Load errors must stay visible: a skill that
 * silently does not load is the subsystem's worst failure.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import SkillsPanel from "./SkillsPanel";
import * as api from "../../lib/api";

function skill(overrides = {}) {
  return {
    name: "laravel",
    description: "Laravel security review",
    version: "1.0.0",
    permissions: [],
    tools: [],
    enabled: true,
    has_instructions: true,
    ...overrides,
  };
}

function respond(overrides = {}) {
  return {
    getSkills: vi.fn(async () => ({ skills: [skill()], errors: [], root: "/skills" })),
    setSkillEnabled: vi.fn(async () => ({ status: "saved" })),
    createSkill: vi.fn(async () => ({ status: "created" })),
    getSkill: vi.fn(async () => ({
      name: "laravel",
      permissions: ["filesystem.write"],
      tools: ["tools/review.py"],
      content: "# Review\n\nCheck for SQL injection.",
    })),
    removeSkill: vi.fn(async () => ({ status: "removed" })),
    ...overrides,
  };
}

function mockApi(stubs) {
  vi.spyOn(api, "getSkills").mockImplementation(stubs.getSkills);
  vi.spyOn(api, "setSkillEnabled").mockImplementation(stubs.setSkillEnabled);
  vi.spyOn(api, "createSkill").mockImplementation(stubs.createSkill);
  vi.spyOn(api, "getSkill").mockImplementation(stubs.getSkill);
  vi.spyOn(api, "removeSkill").mockImplementation(stubs.removeSkill);
}

describe("SkillsPanel inspection", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("shows what a skill asks for next to it in the list", async () => {
    mockApi(respond({
      getSkills: vi.fn(async () => ({
        skills: [skill({ permissions: ["filesystem.write", "process.execute"] })],
        errors: [],
        root: "/skills",
      })),
    }));
    render(<SkillsPanel />);

    expect(
      await screen.findByText(/asks for: filesystem.write · process.execute/),
    ).toBeInTheDocument();
  });

  it("shows no permission line when the skill declares nothing", async () => {
    mockApi(respond());
    render(<SkillsPanel />);

    expect(await screen.findByText("laravel")).toBeInTheDocument();
    expect(screen.queryByText(/asks for/)).not.toBeInTheDocument();
  });

  it("puts declarations above the instructions in the detail view", async () => {
    mockApi(respond());
    render(<SkillsPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "VIEW" }));

    // Both declarations, then the content they precede. `findByText` because
    // inspect() resolves asynchronously — the detail lands a tick after the
    // click.
    const asks = await screen.findByText(/ASKS FOR/);
    expect(asks).toHaveTextContent("filesystem.write");
    expect(screen.getByText(/TOOL FILES.*tools\/review.py/)).toBeInTheDocument();
    expect(screen.getByText(/Check for SQL injection\./)).toBeInTheDocument();
    const detail = asks.parentElement;
    expect(detail.textContent.indexOf("ASKS FOR")).toBeLessThan(
      detail.textContent.indexOf("Check for SQL injection."),
    );
  });

  it("keeps manifest load failures visible", async () => {
    mockApi(respond({
      getSkills: vi.fn(async () => ({
        skills: [],
        errors: [{ path: "/skills/broken/skill.json", error: "'name' is required" }],
        root: "/skills",
      })),
    }));
    render(<SkillsPanel />);

    expect(await screen.findByText(/1 failed to load/)).toBeInTheDocument();
    expect(screen.getByText(/'name' is required/)).toBeInTheDocument();
  });

  it("surfaces a fetch failure as an alert", async () => {
    mockApi(respond({
      getSkills: vi.fn(async () => ({ error: "HTTP 500" })),
    }));
    render(<SkillsPanel />);

    expect(await screen.findByRole("alert")).toHaveTextContent("HTTP 500");
  });
});
