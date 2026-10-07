/**
 * The task panel's state vocabulary.
 *
 * Pure, and pure for the same reason `statusLabels.js` is: the task list, the
 * inspector and the header all need to say the *same* thing about a task, and
 * three components each deriving their own wording is how they drift.
 *
 * Two things are worth knowing about the ordering below.
 *
 * `TASK_TONE` grades up, never down. An unrecognised status is `high`, because
 * a task in a state this build has never heard of is exactly the one a user
 * needs to look at — showing it as routine would be the dangerous direction.
 *
 * The lifecycle list is the spec's order, not an alphabetical one, because the
 * panel draws a progress rail through them and an alphabetical list draws a
 * nonsensical rail.
 */

/**
 * The tones a task status maps to.
 *
 * `high` is in this list as the **fallback** for an unrecognised status, so an
 * unknown state grades up to "look at this" rather than down to "fine". It is
 * not a tone any real status produces.
 */
export const TASK_TONES = ["pending", "active", "blocked", "done", "failed", "cancelled", "high"];

const TONE_BY_STATUS = {
  received: "pending",
  analyzing: "active",
  planning: "active",
  executing: "active",
  verifying: "active",
  waiting_for_permission: "blocked",
  waiting_for_user: "blocked",
  completed: "done",
  failed: "failed",
  cancelled: "cancelled",
};

const LABELS_EN = {
  received: "Received",
  analyzing: "Understanding",
  planning: "Planning",
  executing: "Running",
  verifying: "Verifying",
  waiting_for_permission: "Needs approval",
  waiting_for_user: "Needs you",
  completed: "Done",
  failed: "Failed",
  cancelled: "Cancelled",
};

const LABELS_AR = {
  received: "اتسلمت",
  analyzing: "بفهم",
  planning: "بخطط",
  executing: "بشغّل",
  verifying: "بتحقق",
  waiting_for_permission: "محتاج موافقة",
  waiting_for_user: "محتاج ردك",
  completed: "خلص",
  failed: "فشل",
  cancelled: "اتلغى",
};

/**
 * @param {string} status
 * @returns {"pending"|"active"|"blocked"|"done"|"failed"|"cancelled"}
 */
export function taskTone(status) {
  return TONE_BY_STATUS[status] || "high";
}

/**
 * @param {string} status
 * @param {"en"|"ar"} [language]
 * @returns {string} never throws; an unknown status is shown verbatim.
 */
export function taskLabel(status, language = "en") {
  const table = language === "ar" ? LABELS_AR : LABELS_EN;
  if (table[status]) return table[status];
  // Verbatim rather than a generic word: "some_new_state" is more useful to
  // whoever is debugging than "Active", and it is not a lie.
  return String(status || "—");
}

/** The lifecycle order the progress rail is drawn through. */
export const TASK_LIFECYCLE = [
  "received",
  "analyzing",
  "planning",
  "executing",
  "verifying",
  "completed",
];

/**
 * A task's progress as a whole percentage.
 *
 * Prefers the server's `progress` when present and falls back to deriving it
 * from the lifecycle position — because a task with no steps still has to show
 * *something* moving, and a bar stuck at 0% while work happens reads as a hang.
 *
 * @param {object|null} task
 * @returns {number} 0..100
 */
export function taskPercent(task) {
  if (!task) return 0;
  if (typeof task.progress === "number") {
    return Math.max(0, Math.min(100, Math.round(task.progress * 100)));
  }
  const index = TASK_LIFECYCLE.indexOf(task.status);
  if (index >= 0) return Math.round((index / (TASK_LIFECYCLE.length - 1)) * 100);
  return task.status === "completed" || task.status === "failed" ? 100 : 0;
}

/**
 * Whether a task is doing something, and so deserves a Stop control.
 *
 * "Waiting for permission" counts: the work is in flight and a person is the
 * only thing that can move it on.
 */
export function isTaskActive(task) {
  if (!task) return false;
  return taskTone(task.status) === "active" || taskTone(task.status) === "blocked";
}

/**
 * One-line summary for a task row.
 *
 * Says what actually happened rather than what is happening: a finished task's
 * file list is the interesting fact, and "Done" alone tells the user nothing
 * they cannot see by expanding it.
 */
export function taskSummary(task, language = "en") {
  if (!task) return "";
  const ar = language === "ar";
  const files = (task.files_changed || []).length;
  const tools = (task.tools_used || []).length;

  if (task.status === "failed" && task.error) return task.error;
  if (task.status === "cancelled") return ar ? "اتلغت قبل ما تخلص" : "Cancelled before it finished";
  if (task.status === "completed") {
    if (files) return ar ? `غيّر ${files} ملف` : `Changed ${files} file${files === 1 ? "" : "s"}`;
    if (tools) return ar ? `استعمل ${tools} أداة` : `Used ${tools} tool${tools === 1 ? "" : "s"}`;
    return ar ? "خلص" : "Done";
  }
  const active = (task.steps || []).filter((s) => s.status === "running");
  if (active.length) return active[0].title;
  return taskLabel(task.status, language);
}

export default taskTone;