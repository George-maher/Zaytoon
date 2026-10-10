/**
 * The HUD must stay usable, not just futuristic.
 *
 * These assert the accessibility and motion promises in docs/DESIGN_SYSTEM.md
 * against the real stylesheet and the real components, because a design system
 * that documents "respects prefers-reduced-motion" and does not is worse than
 * one that never claimed it.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import AICore from "./components/HUD/AICore";
import StatusIndicator from "./components/HUD/StatusIndicator";
import { STATES, resolveState } from "./components/HUD/hudState";

const here = dirname(fileURLToPath(import.meta.url));
const css = readFileSync(resolve(here, "styles/theme.css"), "utf8");

describe("design token system", () => {
  it("defines the reference palette as tokens, not ad-hoc hex in components", () => {
    for (const token of [
      "--hud-void", "--hud-deep", "--hud-raised", "--hud-line",
      "--hud-cyan", "--hud-cyan-core", "--hud-blue", "--hud-indigo",
      "--hud-amber", "--hud-green", "--hud-red",
      "--hud-text", "--hud-text-dim", "--hud-text-mute",
      "--hud-edge", "--hud-edge-strong", "--hud-glow-soft",
    ]) {
      expect(css).toContain(`${token}:`);
    }
  });

  it("does not reintroduce backdrop blur, which was the SaaS look", () => {
    // The reference is flat black with lit edges. `backdrop-filter: blur` is
    // the glassmorphism the brief rules out.
    expect(css).not.toMatch(/backdrop-filter:\s*blur/);
  });

  it("keeps panel corners square", () => {
    // A rounded panel is the single clearest "this is a card, not an
    // instrument" signal.
    expect(css).not.toMatch(/\.hud-panel\s*\{[^}]*border-radius/);
  });

  it("animates transforms and opacity, not layout properties", () => {
    // Filtering this to the animation blocks; animating width/height/filter
    // forces layout or is compositor-expensive.
    const blocks = [...css.matchAll(/@keyframes\s+([\w-]+)\s*\{([\s\S]*?)\n\}/g)];
    expect(blocks.length).toBeGreaterThan(0);
    for (const [, name, body] of blocks) {
      expect(body, `${name} animates a layout property`).not.toMatch(
        /^\s*(width|height|top|left|right|bottom|margin|padding)\s*:/m,
      );
    }
  });
});

describe("reduced motion", () => {
  it("freezes every loop in the stylesheet", () => {
    const block = css.slice(css.indexOf("@media (prefers-reduced-motion: reduce)"));
    expect(block).toContain("animation: none !important");
    // Each looping class must be named explicitly, otherwise it keeps running.
    for (const cls of ["hud-scan::after", "hud-core-spin-slow", "hud-core-spin-rev", "hud-core-breathe", "hud-led--pulse"]) {
      expect(block, `${cls} is not disabled under reduced motion`).toContain(cls);
    }
  });
});

describe("state is not colour-only", () => {
  it("gives every state a distinct glyph", () => {
    const glyphs = Object.values(STATES).map((s) => s.glyph);
    expect(new Set(glyphs).size).toBe(glyphs.length);
  });

  it("gives every state a written word, not just a hue", () => {
    for (const [name, s] of Object.entries(STATES)) {
      expect(s.word, `${name} has no word`).toBeTruthy();
      expect(s.en, `${name} has no English label`).toBeTruthy();
      expect(s.ar, `${name} has no Arabic label`).toBeTruthy();
    }
  });

  it("renders the state as text for assistive technology", () => {
    render(<StatusIndicator state="thinking" language="en" />);
    // role=status + the visible word, so it is announced, not just coloured.
    const el = screen.getByRole("status");
    expect(el).toHaveTextContent(/Processing/i);
  });

  it("exposes the core's state in its accessible name", () => {
    render(<AICore state="speaking" language="en" />);
    expect(screen.getByRole("img")).toHaveAccessibleName(/speaking/i);
  });
});

describe("resolveState precedence", () => {
  it("puts a pending human approval above everything else", () => {
    // Otherwise the UI claims to be working while it is blocked on a person.
    expect(resolveState({ isConnected: true, pendingConfirmation: { id: 1 }, isTyping: true, status: "thinking" }))
      .toBe("confirming");
  });

  it("reports offline above all other signals", () => {
    expect(resolveState({ isConnected: false, isTyping: true, isSpeaking: true })).toBe("offline");
  });

  it("does not report speaking while a tool is running", () => {
    expect(resolveState({ isConnected: true, status: "executing", isSpeaking: true })).toBe("executing");
  });

  it("falls back to idle for an unknown status", () => {
    expect(resolveState({ isConnected: true, status: "something-new" })).toBe("idle");
  });
});
