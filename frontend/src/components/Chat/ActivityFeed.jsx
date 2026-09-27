import { memo } from "react";

/**
 * Agent activity feed.
 *
 * The engine emits `plan`, `tool_started`, `tool_progress` and `tool_finished`
 * as it works through a multi-step request. This renders that as a live feed,
 * so "Create a React project called AI Dashboard" shows the actual steps rather
 * than one undifferentiated spinner.
 *
 * Everything shown here is a fixed, human-written label from the backend's
 * `agents/engine.py` — never the model's own reasoning, which is deliberate:
 * the UI shows *what is happening*, not *what the model is thinking*.
 */
function ActivityFeed({ plan = [], activeTool = null, activity = [], language = "en" }) {
  const ar = language === "ar";
  if (!plan.length && !activeTool && !activity.length) return null;

  return (
    <div
      className="mb-2 rounded-sm overflow-hidden animate-fade-in"
      style={{ border: "1px solid rgba(0, 212, 255, 0.15)" }}
      aria-live="polite"
      aria-label={ar ? "نشاط المساعد" : "Assistant activity"}
    >
      {plan.length > 0 && (
        <div className="px-2.5 py-1.5" style={{ background: "rgba(0,212,255,0.04)" }}>
          <div className="text-[8px] font-display tracking-[0.2em] mb-1" style={{ color: "var(--text-muted)" }}>
            {ar ? "الخطة" : "PLAN"}
          </div>
          <ol className="space-y-0.5">
            {plan.map((step, i) => (
              <li key={`${step}-${i}`} className="text-[10px] flex items-start gap-1.5" style={{ color: "var(--text-secondary)" }}>
                <span style={{ color: "#00d4ff" }}>{i + 1}.</span>
                <span>{step}</span>
              </li>
            ))}
          </ol>
        </div>
      )}

      {activity.length > 0 && (
        <ul
          className="px-2.5 py-1.5 space-y-0.5 font-mono"
          style={{ background: "rgba(0,0,0,0.25)", maxHeight: 140, overflowY: "auto" }}
        >
          {activity.map((entry) => (
            <li key={entry.id} className="text-[9px] flex items-start gap-1.5">
              <span
                className="flex-none"
                style={{
                  color:
                    entry.kind === "finished"
                      ? entry.success
                        ? "#00ff88"
                        : "#ff3355"
                      : "#00d4ff",
                }}
              >
                {entry.kind === "started" ? "▸" : entry.kind === "progress" ? "·" : entry.success ? "✓" : "✕"}
              </span>
              <span className="min-w-0 break-words" style={{ color: "var(--text-secondary)" }}>
                {entry.kind === "finished" ? entry.name : entry.label || entry.name}
                {entry.kind === "finished" && entry.summary
                  ? ` — ${entry.summary}`
                  : entry.kind === "progress" && entry.message
                    ? ` — ${entry.message}`
                    : ""}
              </span>
            </li>
          ))}
        </ul>
      )}

      {activeTool && (
        <div
          className="px-2.5 py-1 flex items-center gap-1.5 text-[9px] font-mono"
          style={{ background: "rgba(0,212,255,0.06)", color: "#00d4ff" }}
        >
          <span
            className="inline-block w-1.5 h-1.5 rounded-full"
            style={{ background: "#00d4ff", animation: "pulse-ring 1.2s infinite" }}
          />
          {activeTool.label || activeTool.name}
        </div>
      )}
    </div>
  );
}

export default memo(ActivityFeed);
