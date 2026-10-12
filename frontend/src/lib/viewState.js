/**
 * The view vocabulary — the product's destinations, in one place.
 *
 * Zaytoon is an AI Computer System, not an IDE with two panes. Its top-level
 * destinations are named here rather than scattered through the components so
 * that "what screen is the app on" has exactly one answer, and that answer
 * cannot be a typo: `setView("wrkspace")` used to be a blank screen, because
 * App's render was a bare ternary whose `else` branch mounted the workspace
 * for *any* string that was not literally `"home"`.
 *
 * Three rules the module exists to enforce:
 *
 * 1. **A destination can be declared without being offered.** `ready` is the
 *    honesty gate: the header nav renders from `navItems()`, which only
 *    returns destinations whose screen actually exists today. `tasks` is
 *    declared because it is part of the product's shape, but it is not
 *    clickable, because there is nothing behind it yet. Flip `ready` the same
 *    day the screen lands — never before, or the nav starts promising rooms
 *    this build does not have.
 *
 * 2. **Settings is an overlay, not a screen.** It is a modal over whatever
 *    you were doing (`SettingsPanel`'s `open`/`onClose`), which is both what
 *    settings should be and what ~30 tests pin it to. So `resolveScreen()`
 *    maps it to Home rather than pretending it is somewhere you can stand.
 *
 * 3. **Everything unresolvable falls back to Home.** Not to the workspace:
 *    an unknown view must land on a screen that is always safe, never on a
 *    shell that expects a project root.
 */

/** The destinations the product is made of. */
export const VIEWS = Object.freeze({
  HOME: "home",
  CHAT: "chat",
  COMPUTER: "computer",
  WORKSPACE: "workspace",
  TASKS: "tasks",
  SKILLS: "skills",
  SECURITY: "security",
  MODELS: "models",
  SETTINGS: "settings",
});

/** Product order — top to bottom in the sidebar nav, conceptually. */
export const VIEW_LIST = Object.freeze([
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

/**
 * Per-destination metadata.
 *
 * `kind`  — `screen` swaps the centre of the app; `overlay` sits on top of it.
 * `ready` — a real screen is mounted for this destination *in this build*.
 * `glyph` — one monospace character, for the nav. Decoration only: every
 *           entry also carries a text label, so the glyph is `aria-hidden`.
 */
const META = Object.freeze({
  [VIEWS.HOME]: Object.freeze({
    kind: "screen",
    ready: true,
    en: "Home",
    ar: "الرئيسية",
    glyph: "⌂",
  }),
  [VIEWS.CHAT]: Object.freeze({
    kind: "screen",
    ready: true, // ChatView: the conversation, streaming, tool activity.
    en: "Chat",
    ar: "المحادثة",
    glyph: "◈",
  }),
  [VIEWS.COMPUTER]: Object.freeze({
    kind: "screen",
    ready: true, // ComputerView; `system / apps / processes / files`.
    en: "Computer",
    ar: "الحاسوب",
    glyph: "▦",
  }),
  [VIEWS.WORKSPACE]: Object.freeze({
    kind: "screen",
    ready: true,
    en: "Workspace",
    ar: "مساحة العمل",
    glyph: "▣",
  }),
  [VIEWS.TASKS]: Object.freeze({
    kind: "screen",
    // `TaskFlowView`: the execution log (plan + per-tool rows) over the task
    // records. Flipped the day it mounted — the nav and `resolveScreen()`
    // both read this one flag, so there is no way to advertise the screen
    // without also being able to mount it.
    ready: true,
    en: "Tasks",
    ar: "المهام",
    glyph: "≡",
  }),
  [VIEWS.SKILLS]: Object.freeze({
    kind: "screen",
    ready: true, // SkillsView: create / edit / enable / disable / delete.
    en: "Skills",
    ar: "المهارات",
    glyph: "✦",
  }),
  [VIEWS.SECURITY]: Object.freeze({
    kind: "screen",
    // SecurityView: target authorisations + the non-destructive probe. It is
    // `ready` because it mounts — the page manages authorisations and runs
    // probes; it is not a placeholder for a scanner that does not exist.
    ready: true,
    en: "Security",
    ar: "الأمان",
    glyph: "⛨",
  }),
  [VIEWS.MODELS]: Object.freeze({
    kind: "screen",
    ready: true, // ModelsView: providers, models, routing, tier pins.
    en: "Models",
    ar: "النماذج",
    glyph: "◉",
  }),
  [VIEWS.SETTINGS]: Object.freeze({
    kind: "overlay",
    ready: true,
    en: "Settings",
    ar: "الإعدادات",
    glyph: "⚙",
  }),
});

/** Is `v` one of the five? Unknown input is never an error here. */
export function isView(v) {
  return Object.prototype.hasOwnProperty.call(META, v);
}

/** Metadata for a destination; Home's for anything unrecognised. */
export function viewMeta(v) {
  return isView(v) ? META[v] : META[VIEWS.HOME];
}

/** Bilingual label. `language` is `"en"` or `"ar"`. */
export function viewTitle(v, language) {
  const meta = viewMeta(v);
  return language === "ar" ? meta.ar : meta.en;
}

/** A destination this build can actually serve. */
export function isReady(v) {
  return isView(v) && viewMeta(v).ready;
}

/**
 * The screen to mount for a requested destination.
 *
 * Overlays and destinations without a screen both resolve to Home, so the
 * caller can never render a stub or an empty shell.
 */
export function resolveScreen(v) {
  // `viewMeta` falls back to *Home's* metadata for unknown input, which would
  // make this return `v` itself for a typo — a destination with no screen
  // behind it. So the question is asked about `v`, not about the fallback.
  if (!isView(v)) return VIEWS.HOME;
  const meta = META[v];
  return meta.kind === "screen" && meta.ready ? v : VIEWS.HOME;
}

/**
 * Header navigation, in product order, containing only destinations that
 * exist. This is the one function that decides what the app advertises —
 * adding a nav item here without a screen behind it is exactly the fake UI
 * this system must not have.
 */
export function navItems(language) {
  return VIEW_LIST.filter((v) => viewMeta(v).kind === "screen" && isReady(v)).map((v) => ({
    id: v,
    label: viewTitle(v, language),
    glyph: viewMeta(v).glyph,
  }));
}
