import { memo, useCallback, useEffect, useMemo, useState } from "react";
import * as api from "../../lib/api";
import { TASK_LIFECYCLE, isTaskActive, taskLabel, taskPercent, taskSummary } from "../../lib/taskState";

/**
 * The task panel.
 *
 * Everything the spec asks a task to carry, in one list: status, progress, when
 * it started, which files it changed, which tools it used, and the error if it
 * failed. A task row is **not** a spinner — a task that has finished and changed
 * four files should say so, because that is the answer to "what did you just
 * do" without the user opening anything.
 *
 * `task.*` frames arrive over the socket, so the list is live without polling.
 * Polling is only used for the initial fill and after a cancel, because the
 * socket only tells you about changes it made.
 */
function TaskPanel({ tasks, activeTask, onCancel, language = "en" }) {
  const [expanded, setExpanded] = useState(() => new Set());
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState(null);
  const ar = language === "ar";

  const load = useCallback(async () => {
    const result = await api.getTasks({ limit: 50 });
    if (result.error) setError(result.error);
    else setError(null);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  /**
   * Cancel through the socket, not REST.
   *
   * `api.cancelTask` addresses one task by id; the socket's `task` message
   * cancels the session's *active* task, which is what the Stop button means —
   * and it works for a task started from the browser panel as well as one the
   * desktop shell started, because both share the core.
   */
  const cancel = useCallback(
    async (id) => {
      if (onCancel) {
        onCancel();
        await load();
        return;
      }
      const result = await api.cancelTask(id);
      if (result.error) setError(result.error);
      await load();
    },
    [onCancel, load],
  );

  const toggle = useCallback((id) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const openDetail = useCallback(async (id) => {
    const result = await api.getTask(id);
    if (result.error) setError(result.error);
    else setDetail(result);
  }, []);

  const rows = useMemo(() => tasks || [], [tasks]);

  return (
    <div className="hud-panel flex-1 flex flex-col min-h-0">
      <header className="hud-panel-head">
        <span>{ar ? "المهام" : "TASKS"}</span>
        <span className="hud-micro ml-auto">{rows.length}</span>
        <button
          onClick={load}
          className="hud-micro"
          style={{
            padding: "var(--space-1) var(--space-2)",
            border: "var(--border-w) solid var(--hud-edge-faint)",
            color: "var(--hud-text-dim)",
          }}
          title={ar ? "تحديث" : "Refresh"}
        >
          {ar ? "تحديث" : "RFS"}
        </button>
      </header>

      {/* The one task in flight, if any. Promoted above the list because it is
          the only row that changes right now, and it needs a Stop control. */}
      {activeTask && isTaskActive(activeTask) && (
        <div
          role="status"
          style={{
            padding: "var(--space-3)",
            borderBottom: "var(--border-w) solid var(--hud-edge-faint)",
          }}
        >
          <div className="flex items-baseline justify-between">
            <span className="hud-micro" style={{ color: "var(--hud-cyan)" }}>
              {taskLabel(activeTask.status, language)}
            </span>
            <span className="hud-micro" style={{ color: "var(--hud-text-mute)" }}>
              {taskPercent(activeTask)}%
            </span>
          </div>
          <p style={{ fontSize: "var(--font-sm)", marginTop: "var(--space-2)" }}>
            {activeTask.title}
          </p>
          <Rail status={activeTask.status} language={language} />
          <button
            onClick={() => cancel(activeTask.id)}
            className="hud-micro"
            style={{
              marginTop: "var(--space-3)",
              padding: "var(--space-1) var(--space-3)",
              border: "var(--border-w) solid var(--hud-red)",
              color: "var(--hud-red)",
            }}
          >
            {ar ? "إيقاف" : "STOP"}
          </button>
        </div>
      )}

      {error && (
        <p role="alert" style={{ padding: "var(--space-2) var(--space-3)", color: "var(--hud-red)" }}>
          {error}
        </p>
      )}

      <ul
        className="flex-1 overflow-y-auto min-h-0"
        style={{ listStyle: "none", margin: 0, padding: 0 }}
        aria-label={ar ? "قائمة المهام" : "Task list"}
      >
        {rows.map((task) => {
          const open = expanded.has(task.id);
          const tone = toneColor(task);
          return (
            <li
              key={task.id}
              style={{ borderBottom: "var(--border-w) solid var(--hud-edge-faint)" }}
            >
              <div className="flex items-center" style={{ padding: "var(--space-2) var(--space-3)" }}>
                <button
                  onClick={() => toggle(task.id)}
                  aria-expanded={open}
                  className="hud-micro"
                  style={{ color: "var(--hud-text-mute)", padding: "var(--space-1)" }}
                >
                  <span aria-hidden="true">{open ? "▾" : "▸"}</span>
                  <span className="sr-only">{ar ? "تفاصيل" : "Details"}</span>
                </button>
                <button
                  onClick={() => openDetail(task.id)}
                  className="flex-1 text-left min-w-0"
                  style={{ padding: "var(--space-1)" }}
                >
                  <span className="truncate" style={{ fontSize: "var(--font-sm)", display: "block" }}>
                    {task.title}
                  </span>
                  <span className="hud-micro" style={{ color: tone }}>
                    {taskLabel(task.status, language)} · {taskSummary(task, language)}
                  </span>
                </button>
                {task.is_terminal ? (
                  <span className="hud-micro" style={{ color: "var(--hud-text-mute)" }}>
                    {task.duration_ms != null ? `${task.duration_ms}ms` : ""}
                  </span>
                ) : (
                  <button
                    onClick={() => cancel(task.id)}
                    className="hud-micro"
                    style={{ padding: "var(--space-1) var(--space-2)", color: "var(--hud-red)" }}
                  >
                    {ar ? "إيقاف" : "STOP"}
                  </button>
                )}
              </div>

              <Rail status={task.status} language={language} />

              {open && (
                <div style={{ padding: "0 var(--space-3) var(--space-3)" }}>
                  {(task.steps || []).length > 0 && <StepList steps={task.steps} />}
                  {detail?.id === task.id && <TaskDetail task={detail} language={language} />}
                  {(task.files_changed || []).length > 0 && (
                    <p className="hud-micro" style={{ color: "var(--hud-green)" }}>
                      {ar ? "معدّل:" : "Modified:"} {task.files_changed.join(", ")}
                    </p>
                  )}
                  {(task.tools_used || []).length > 0 && (
                    <p className="hud-micro" style={{ color: "var(--hud-text-mute)" }}>
                      {ar ? "أدوات:" : "Tools:"} {task.tools_used.join(", ")}
                    </p>
                  )}
                </div>
              )}
            </li>
          );
        })}
        {rows.length === 0 && (
          <li className="hud-micro" style={{ padding: "var(--space-4)", color: "var(--hud-text-mute)" }}>
            {ar ? "لا مهام بعد" : "No tasks yet"}
          </li>
        )}
      </ul>
    </div>
  );
}

/** The lifecycle rail: which phase the task reached, and how far. */
function Rail({ status, language }) {
  if (status === "failed" || status === "cancelled") {
    return (
      <p className="hud-micro" style={{ color: "var(--hud-red)", padding: "0 var(--space-3) var(--space-2)" }}>
        {taskLabel(status, language)}
      </p>
    );
  }
  const index = TASK_LIFECYCLE.indexOf(status);
  if (index < 0) return null;
  return (
    <div
      className="flex"
      aria-hidden="true"
      style={{ gap: 2, padding: "0 var(--space-3) var(--space-2)" }}
    >
      {TASK_LIFECYCLE.slice(0, index + 1).map((phase) => (
        <span
          key={phase}
          style={{
            flex: 1,
            height: 2,
            background:
              phase === "completed" ? "var(--hud-green)" : "var(--hud-cyan)",
          }}
        />
      ))}
      {TASK_LIFECYCLE.slice(index + 1).map((phase) => (
        <span key={phase} style={{ flex: 1, height: 2, background: "var(--hud-line)" }} />
      ))}
    </div>
  );
}

function StepList({ steps }) {
  return (
    <ol style={{ listStyle: "none", margin: "0 0 var(--space-2)", padding: 0 }}>
      {steps.map((step) => {
        const color =
          step.status === "done"
            ? "var(--hud-green)"
            : step.status === "failed"
              ? "var(--hud-red)"
              : step.status === "running"
                ? "var(--hud-cyan)"
                : "var(--hud-text-mute)";
        const mark =
          step.status === "done"
            ? "✓"
            : step.status === "failed"
              ? "✕"
              : step.status === "running"
                ? "▸"
                : step.status === "skipped"
                  ? "–"
                  : "○";
        return (
          <li
            key={step.seq}
            className="hud-micro"
            style={{ color, display: "flex", gap: "var(--space-2)", padding: "2px 0" }}
          >
            <span aria-hidden="true">{mark}</span>
            <span>{step.title}</span>
            {step.tool && (
              <span style={{ color: "var(--hud-text-mute)" }}>({step.tool})</span>
            )}
            {step.status === "skipped" && step.detail && (
              <span style={{ color: "var(--hud-amber)" }}>{step.detail}</span>
            )}
          </li>
        );
      })}
    </ol>
  );
}

function TaskDetail({ task, language }) {
  const ar = language === "ar";
  return (
    <div style={{ marginBottom: "var(--space-2)" }}>
      {(task.logs || []).slice(-8).map((line, i) => (
        <p
          key={`${line.ts}-${i}`}
          className="hud-mono"
          style={{
            fontSize: "var(--font-xs)",
            color:
              line.level === "error"
                ? "var(--hud-red)"
                : line.level === "warning"
                  ? "var(--hud-amber)"
                  : "var(--hud-text-mute)",
          }}
        >
          {line.message}
        </p>
      ))}
      {task.result && (
        <p style={{ fontSize: "var(--font-sm)", marginTop: "var(--space-2)" }}>
          {task.result}
        </p>
      )}
      {task.model && (
        <p className="hud-micro" style={{ color: "var(--hud-text-mute)" }}>
          {ar ? "النموذج:" : "Model:"} {task.provider ? `${task.provider} / ` : ""}
          {task.model}
        </p>
      )}
    </div>
  );
}

/** Colour by tone. Never the only channel — every row also names its status. */
function toneColor(task) {
  if (task.status === "failed" || task.status === "cancelled") return "var(--hud-red)";
  if (task.status === "completed") return "var(--hud-green)";
  if (task.status === "waiting_for_permission" || task.status === "waiting_for_user") {
    return "var(--hud-amber)";
  }
  return "var(--hud-cyan)";
}

export default memo(TaskPanel);