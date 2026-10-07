/**
 * Limits the UI enforces, kept in one module so they cannot drift from the
 * backend's.
 *
 * `MAX_ENABLED_SKILLS` mirrors `core.skills.MAX_ENABLED`. It is duplicated rather
 * than fetched because it decides whether the *button* is worth offering — and
 * the backend still refuses regardless, so the worst a stale copy can do is
 * show a button that then explains the limit. That is the better failure than
 * hiding a control that would have worked.
 */

export const MAX_ENABLED_SKILLS = 12;

/** Terminal sessions the backend will hold open at once. */
export const MAX_TERMINALS = 12;

/** Bytes past which the backend refuses to open a file in an editor. */
export const MAX_EDIT_BYTES = 2 * 1024 * 1024;

/** Lines of terminal output kept per session in the browser. */
export const MAX_TERMINAL_LINES = 2000;

export default MAX_ENABLED_SKILLS;