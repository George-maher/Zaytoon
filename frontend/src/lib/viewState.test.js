/**
 * The view vocabulary is a contract, not a convenience.
 *
 * Three things are load-bearing here and none of them are visible in a
 * screenshot:
 *
 *  - an unresolvable view lands on **Home**, never on the workspace shell
 *    (which expects a project root and would render an empty IDE);
 *  - the header nav only advertises destinations whose screen exists, so a
 *    declared-but-unbuilt view cannot become a click that does nothing;
 *  - Settings is an overlay, so it is never a place you can stand.
 *
 * The third rule is the one that silently rots: it costs nothing to add a
 * nav entry for a view nobody has built, and that entry is a promise the
 * build cannot keep.
 */
import { describe, it, expect } from "vitest";
import {
  VIEWS,
  VIEW_LIST,
  isView,
  isReady,
  viewMeta,
  viewTitle,
  resolveScreen,
  navItems,
} from "./viewState";

describe("view vocabulary", () => {
  it("declares the destinations, in product order", () => {
    expect(Object.values(VIEWS).sort()).toEqual(
      [
        "home",
        "chat",
        "computer",
        "workspace",
        "tasks",
        "skills",
        "security",
        "models",
        "settings",
      ].sort(),
    );
    expect(VIEW_LIST).toEqual([
      VIEWS.HOME,
      VIEWS.CHAT,
      VIEWS.COMPUTER,
      VIEWS.WORKSPACE,
      VIEWS.TASKS,
      VIEWS.SKILLS,
      VIEWS.SECURITY,
      VIEWS.MODELS,
      VIEWS.SETTINGS,
    ]);
  });

  it("recognises a real destination and rejects anything else", () => {
    for (const id of VIEW_LIST) expect(isView(id)).toBe(true);
    // The typo case is the one that used to be a blank screen.
    expect(isView("wrkspace")).toBe(false);
    expect(isView(undefined)).toBe(false);
    expect(isView(null)).toBe(false);
  });

  it("never throws on unknown input", () => {
    expect(viewMeta("nope").kind).toBe("screen");
    expect(viewTitle("nope", "en")).toBe("Home");
    expect(viewTitle(VIEWS.HOME, "ar")).toBe("الرئيسية");
    expect(viewTitle(VIEWS.HOME, "en")).toBe("Home");
  });

  it("marks only destinations with a real screen as ready", () => {
    expect(isReady(VIEWS.HOME)).toBe(true);
    expect(isReady(VIEWS.COMPUTER)).toBe(true);
    expect(isReady(VIEWS.WORKSPACE)).toBe(true);
    // TaskFlow landed, so this flipped with it — the assertion exists to make
    // that flip an explicit, failing-until-it-is-true act rather than a
    // comment nobody reads.
    expect(isReady(VIEWS.TASKS)).toBe(true);
    // Settings is an overlay: ready to be *opened*, never a place you stand.
    expect(isReady(VIEWS.SETTINGS)).toBe(true);
  });
});

describe("resolveScreen", () => {
  it("returns a ready screen unchanged", () => {
    expect(resolveScreen(VIEWS.HOME)).toBe(VIEWS.HOME);
    expect(resolveScreen(VIEWS.COMPUTER)).toBe(VIEWS.COMPUTER);
    expect(resolveScreen(VIEWS.WORKSPACE)).toBe(VIEWS.WORKSPACE);
  });

  it("falls back to Home for anything it cannot mount", () => {
    // An overlay is not somewhere you can stand.
    expect(resolveScreen(VIEWS.SETTINGS)).toBe(VIEWS.HOME);
    // Unknown, including the typo a stringly-typed call site would send.
    expect(resolveScreen("wrkspace")).toBe(VIEWS.HOME);
    expect(resolveScreen(undefined)).toBe(VIEWS.HOME);
    expect(resolveScreen(null)).toBe(VIEWS.HOME);
  });

  it("serves Tasks now that TaskFlow exists", () => {
    expect(resolveScreen(VIEWS.TASKS)).toBe(VIEWS.TASKS);
  });

  it("never resolves to a screen that is not ready", () => {
    for (const candidate of [...VIEW_LIST, "wrkspace", undefined, null]) {
      expect(isReady(resolveScreen(candidate))).toBe(true);
    }
  });
});

describe("navItems", () => {
  it("only advertises destinations this build mounts", () => {
    const ids = navItems("en").map((item) => item.id);
    // A nav entry for a screen that does not exist is a promise the build
    // cannot keep; an overlay in a screen nav is a button that lies.
    expect(ids).toContain(VIEWS.COMPUTER);
    expect(ids).toContain(VIEWS.TASKS);
    // A screen nav that offered an overlay would be a button that lands you
    // back where you started.
    expect(ids).not.toContain(VIEWS.SETTINGS);
    for (const id of ids) expect(resolveScreen(id)).toBe(id);
  });

  it("is ordered by product order", () => {
    expect(navItems("en").map((item) => item.id)).toEqual(
      VIEW_LIST.filter((id) => isReady(id) && viewMeta(id).kind === "screen"),
    );
  });

  it("labels every entry in both languages", () => {
    const en = navItems("en");
    const ar = navItems("ar");
    expect(en.length).toBeGreaterThan(0);
    expect(en.map((item) => item.id)).toEqual(ar.map((item) => item.id));
    en.forEach((item, i) => {
      expect(item.label).toBe(viewTitle(item.id, "en"));
      expect(ar[i].label).toBe(viewTitle(item.id, "ar"));
      expect(ar[i].label).not.toBe(item.label);
      expect(item.label.length).toBeGreaterThan(0);
    });
  });

  it("carries a glyph for decoration only", () => {
    for (const item of navItems("en")) {
      expect(typeof item.glyph).toBe("string");
      expect(item.glyph.length).toBeGreaterThan(0);
      expect(item.label).not.toBe(item.glyph);
    }
  });
});
