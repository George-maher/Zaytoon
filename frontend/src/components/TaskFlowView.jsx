import { memo } from "react";
import ActivityFeed from "./Chat/ActivityFeed";
import TaskPanel from "./workspace/TaskPanel";

/**
 * The Tasks destination — "what was I asked to do, and what came of it".
 *
 * Two panels, each answering half of that, and both are already-written
 * components rather than a new interpretation of the same data:
 *
 * - **EXECUTION LOG** — the plan the model published and every tool it ran,
 *   folded to one row per invocation with its outcome. This is the *how*.
 *   It is capped in height on purpose: a long tool log must never be able to
 *   push the records below it off the screen, which is the failure mode that
 *   makes a history panel useless exactly when there is most to see.
 * - **TASKS** — the persisted records with their detail (steps, files changed,
 *   tools used, logs, result, model), which is the *what*.
 *
 * Neither panel claims to be live. The log carries no "IN PROGRESS" chip of its
 * own: the rows it shows already carry RUNNING / COMPLETED / FAILED, and a
 * second, coarser status computed from `activity.length` would still read as
 * running after the turn had ended, because that array is only emptied when the
 * transcript is cleared. A component that cannot be sure has no business
 * asserting it.
 *
 * App's header owns the document's single `h1`; this screen's heading is an
 * `h2`, matching Computer.
 */
function TaskFlowView({
  language = "en",
  plan = [],
  activeTool = null,
  activity = [],
  tasks = [],
  activeTask = null,
  onCancel,
}) {
  const ar = language === "ar";
  const hasFeed = Boolean(plan.length || activeTool || activity.length);

  return (
    <div
      data-enter="center"
      className="flex-1 min-h-0 flex flex-col"
      style={{ padding: "var(--space-4)" }}
    >
      <div
        className="flex items-baseline flex-wrap"
        style={{ gap: "var(--space-3)", marginBottom: "var(--space-3)" }}
      >
        <h2
          className="hud-identity"
          style={{
            fontSize: "var(--font-lg)",
            letterSpacing: ar ? "0" : "0.16em",
            margin: 0,
          }}
        >
          {ar ? "المهام" : "TASKS"}
        </h2>
        <p className="hud-micro" style={{ fontSize: "var(--font-xs)", margin: 0 }}>
          {ar
            ? "ما طُلب من المساعد — وما نفّذه"
            : "WHAT THE ASSISTANT WAS ASKED — AND WHAT IT DID"}
        </p>
      </div>

      <div
        className="flex-1 min-h-0 flex flex-col"
        style={{ gap: "var(--space-3)" }}
      >
        <section
          aria-labelledby="taskflow-log-title"
          className="hud-panel flex-none flex flex-col min-h-0"
          style={{ maxHeight: "42%" }}
        >
          <header className="hud-panel-head">
            <span id="taskflow-log-title">
              {ar ? "سجل التنفيذ" : "EXECUTION LOG"}
            </span>
            <span className="hud-micro ml-auto">LOG</span>
          </header>
          <div
            className="min-h-0 overflow-y-auto"
            style={{ padding: "var(--space-3)", flex: "1 1 auto" }}
          >
            {hasFeed ? (
              <ActivityFeed
                plan={plan}
                activeTool={activeTool}
                activity={activity}
                language={language}
              />
            ) : (
              /* The feed renders nothing at all when it is empty, which would
                 leave a bare framed box. One honest sentence instead — and it
                 is about tools, because a prose-only turn never produces an
                 event for this log to hold. */
              <p
                role="status"
                className="hud-micro"
                style={{ color: "var(--hud-text-mute)", margin: 0 }}
              >
                {ar ? "لم يُنفَّذ شيء بعد" : "Nothing has run yet"}
              </p>
            )}
          </div>
        </section>

        {/* `flex-1` is TaskPanel's own root, so it takes whatever the log did
            not — the two share the screen without either being scrollable as a
            whole. */}
        <TaskPanel
          tasks={tasks}
          activeTask={activeTask}
          onCancel={onCancel}
          language={language}
        />
      </div>
    </div>
  );
}

export default memo(TaskFlowView);
