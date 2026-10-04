/**
 * Density is a system, not a per-component opinion.
 *
 * The compact pass is only durable if "how tight the UI reads" is decided in
 * one file. These tests fail if the scale is removed from `theme.css`, if it
 * drifts (a border thicker than a hairline, a scale that is not ascending),
 * or if the components that actually carry the layout quietly go back to
 * hard-coded paddings and type sizes.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { describe, it, expect } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const css = readFileSync(resolve(here, "styles/theme.css"), "utf8");
const read = (p) => readFileSync(resolve(here, p), "utf8");

const TOKENS = [
  "--space-1", "--space-2", "--space-3", "--space-4", "--space-5", "--space-6",
  "--font-xs", "--font-sm", "--font-md",
  "--radius-sm", "--border-w",
];

describe("density tokens", () => {
  it("defines the whole scale in theme.css", () => {
    for (const token of TOKENS) {
      expect(css, `${token} is missing from theme.css`).toContain(`${token}:`);
    }
  });

  it("keeps spacing ascending and borders at a hairline", () => {
    const spaces = [...css.matchAll(/--space-\d:\s*(\d+)px/g)].map((m) => Number(m[1]));
    expect(spaces).toHaveLength(6);
    for (let i = 1; i < spaces.length; i++) {
      expect(spaces[i], "the spacing scale must ascend").toBeGreaterThan(spaces[i - 1]);
    }
    // An instrument panel is hairlines only; a 2px border reads as a card.
    expect(css).toMatch(/--border-w:\s*1px/);
  });

  it("is consumed by the components that carry the layout", () => {
    for (const file of [
      "App.jsx",
      "components/SettingsPanel.jsx",
      "components/ChatMessage.jsx",
      "components/Chat/ChatTranscript.jsx",
      "components/InputBar.jsx",
      "components/HUD/HUDPanel.jsx",
      "components/HUD/CommandConsole.jsx",
    ]) {
      expect(read(file), `${file} no longer reads the density scale`).toMatch(
        /var\(--(space-\d|font-(xs|sm|md|lg)|border-w|radius-sm|ctl-size|header-h)/,
      );
    }
  });

  it("sizes the shared HUD primitives from the same scale", () => {
    // The telemetry strip and the panel header are the two elements every
    // rail is built from; if they hard-code a size, the rails drift apart.
    for (const [selector, token] of [
      [".hud-panel-head", "--font-sm"],
      [".hud-tele", "--font-sm"],
      [".hud-micro", "--font-sm"],
    ]) {
      const block = css.slice(css.indexOf(`${selector} {`));
      const body = block.slice(0, block.indexOf("}"));
      expect(body, `${selector} ignores ${token}`).toContain(`var(${token})`);
    }
  });
});
