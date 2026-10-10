/**
 * Input visibility is a promise, not a hope.
 *
 * The reported failure was white-on-white: text, placeholders or dropdown
 * options that could not be read. Three different mechanisms cause that class
 * of bug and only one of them lives in a component:
 *
 *   1. the browser paints native widgets (<select> popups, autofill,
 *      checkboxes) with a light scheme unless told otherwise -- no amount of
 *      page CSS reaches them;
 *   2. a placeholder or a field styles itself and nobody checked its contrast;
 *   3. a focus ring is suppressed by source order rather than by decision.
 *
 * So this asserts the *stylesheet* (which is where the fix lives), and it
 * computes the actual WCAG ratios from the token values rather than trusting
 * a comment that says "readable". Comments lie; the ratio does not.
 *
 * Comments are stripped before matching: the rule "never write outline: none
 * on an input" must not be defeated by the sentence that explains it.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { describe, it, expect } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const raw = readFileSync(resolve(here, "styles/theme.css"), "utf8");
const css = raw.replace(/\/\*[\s\S]*?\*\//g, "");
const read = (p) => readFileSync(resolve(here, p), "utf8");

/** Pull the declaration block for a selector out of the stylesheet. */
function blockFor(selector) {
  const i = css.indexOf(selector);
  expect(i, `${selector} missing from theme.css`).toBeGreaterThan(-1);
  const open = css.indexOf("{", i);
  const close = css.indexOf("}", open);
  return css.slice(open, close);
}

/** Every token declaration, name -> raw value. */
function token(name) {
  const m = css.match(new RegExp(`${name}:\\s*([^;]+);`));
  expect(m, `${name} not defined`).toBeTruthy();
  return m[1].trim();
}

/** WCAG 2.1 relative luminance + contrast ratio, over sRGB hex pairs. */
function contrast(hexA, hexB) {
  const lin = (c) => {
    const v = c / 255;
    return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  };
  const lum = (hex) => {
    const n = parseInt(hex.slice(1), 16);
    return (
      0.2126 * lin((n >> 16) & 255) +
      0.7152 * lin((n >> 8) & 255) +
      0.0722 * lin(n & 255)
    );
  };
  const [a, b] = [lum(hexA), lum(hexB)].sort((x, y) => y - x);
  return (a + 0.05) / (b + 0.05);
}

describe("native widgets are told the page is dark", () => {
  it("sets color-scheme: dark so the browser's own popups are dark", () => {
    // Without this, Chromium draws <select> option lists, autofill
    // suggestions, checkboxes and scrollbar tracks with a LIGHT scheme while
    // the option text inherits the control's light colour: light on light.
    // This is the one white-on-white path no component style can fix.
    const root = blockFor(":root");
    expect(root).toContain("color-scheme: dark");
  });

  it("gives <select> options an explicit dark fill as well", () => {
    // Gecko and older engines paint options from CSS, not from the scheme.
    const opt = blockFor("select option");
    expect(opt).toContain("background: var(--input-bg)");
    expect(opt).toContain("color: var(--input-text)");
  });

  it("keeps autofill on the dark surface instead of the browser's white box", () => {
    const fill = blockFor("input:-webkit-autofill");
    expect(fill).toContain("-webkit-text-fill-color: var(--input-text)");
    expect(fill).toContain("var(--input-bg)");
  });

  it("tints native checkboxes with the accent rather than the OS default", () => {
    expect(blockFor('input[type="checkbox"]')).toContain("accent-color: var(--hud-cyan)");
  });
});

describe("semantic input tokens", () => {
  it("defines the input token set in one place", () => {
    for (const t of [
      "--input-bg",
      "--input-text",
      "--input-placeholder",
      "--input-border",
      "--input-border-hover",
      "--input-border-focus",
      "--input-caret",
    ]) {
      expect(css).toContain(`${t}:`);
    }
  });

  it("has no ad-hoc hex left in the command input", () => {
    // InputBar hardcoded #00ff9c for text and caret. The accent migrated
    // once already (cyan -> green); a literal cannot follow it.
    const bar = read("components/InputBar.jsx");
    expect(bar).not.toMatch(/#[0-9a-fA-F]{3,6}\b/);
    expect(bar).toContain("var(--hud-cyan)");
  });
});

describe("contrast, computed from the tokens", () => {
  const text = token("--input-text");
  const bg = token("--input-bg");
  const placeholder = token("--input-placeholder");

  it("field text meets AA against the field fill", () => {
    const ratio = contrast(text, bg);
    expect(ratio, `${text} on ${bg} = ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(4.5);
  });

  it("placeholder text meets AA -- it is the 'what do I type' hint", () => {
    const ratio = contrast(placeholder, bg);
    expect(ratio, `${placeholder} on ${bg} = ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(4.5);
  });

  it("placeholder is visibly dimmer than typed text", () => {
    // Above AA but below the text itself, so it still reads as a hint rather
    // than as content the user has already entered.
    expect(contrast(placeholder, bg)).toBeLessThan(contrast(text, bg));
  });

  it("field fill is opaque, so no light parent can bleed through", () => {
    // A translucent fill over an unknown parent is how a white-on-white field
    // happens without any component doing anything wrong.
    expect(bg).toMatch(/^#[0-9a-f]{6}$/i);
    expect(bg.toLowerCase()).not.toBe("#ffffff");
  });
});

describe("focus is visible by decision", () => {
  it("does not suppress the focus ring on the shared input class", () => {
    // `.hud-input { outline: none }` out-specified the global :focus-visible
    // ring purely by source order, so keyboard users got no indicator at all.
    const input = blockFor(".hud-input");
    expect(input).not.toContain("outline: none");
    // ...and the class opts back in explicitly, close to the control.
    expect(blockFor(".hud-input:focus-visible")).toContain("outline: 2px solid var(--hud-cyan)");
  });

  it("keeps the global ring available to inputs that style themselves", () => {
    expect(blockFor(":focus-visible")).toContain("outline: 2px solid var(--hud-cyan)");
  });
});

describe("placeholders are styled everywhere, not per component", () => {
  it("has a global placeholder rule covering self-styled fields", () => {
    // The command bar, the terminal inputs, the settings boxes and the file
    // explorer's rename rows never take .hud-input; without this rule each
    // inherits Chromium's light-scheme default and goes near-invisible.
    // The trailing comma distinguishes this multi-selector rule from the
    // `.hud-input::placeholder` block above, whose text contains this one.
    const at = css.indexOf("input::placeholder,");
    expect(at, "global placeholder rule missing").toBeGreaterThan(-1);
    const rule = css.slice(at);
    const body = rule.slice(0, rule.indexOf("}"));
    expect(body).toContain("color: var(--input-placeholder)");
    expect(body).toContain("opacity: 1");
  });

  it("does not dim the placeholder with opacity", () => {
    // opacity: 0.7 on the old rule took a passing colour below AA.
    const input = blockFor(".hud-input::placeholder");
    expect(input).toContain("opacity: 1");
  });
});

describe("the monospace class seven components rely on", () => {
  it("is actually defined", () => {
    // Editor, terminal, log lines and skill sources all carry `hud-mono`.
    // It was never declared, so all of them silently rendered proportional.
    expect(blockFor(".hud-mono")).toContain("font-family: var(--font-mono)");
  });

  it("is referenced by the components that need it", () => {
    for (const p of [
      "components/workspace/CodeWorkspace.jsx",
      "components/workspace/TerminalPanel.jsx",
    ]) {
      expect(read(p), `${p} lost its monospace class`).toContain("hud-mono");
    }
  });
});

describe("tokens components referenced before they existed", () => {
  it("defines --hud-surface and --font-xl", () => {
    // GitPanel's <select> read var(--hud-surface, transparent) with no token
    // behind it, so the control had no fill of its own.
    expect(css).toContain("--hud-surface:");
    expect(css).toContain("--font-xl:");
  });
});
