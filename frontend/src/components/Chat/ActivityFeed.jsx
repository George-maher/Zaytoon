import { memo } from "react";

function ActivityFeed({ plan = [], activeTool = null, activity = [], language }) {
  if (!plan.length && !activeTool && !activity.length) return null;

  return (
    <div
      className="w-full max-w-2xl mb-2 animate-fade-in"
      role="status"
      aria-live="polite"
      aria-label="Assistant activity"
    >
      {/* Plan */}
      {plan.length > 0 && (
        <div className="glass-light rounded-sm p-2 mb-2 animate-slide-down">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-[7px] font-display tracking-[0.2em]" style={{ color: "#00D4FF" }}>
              {language === "ar" ? "الخطة" : "PLAN"}
            </span>
            <div className="w-px h-4 bg-gradient-to-b from-transparent via-[var(--border-glow)] to-transparent" />
          </div>
          <ol className="space-y-1">
            {plan.map((step, i) => (
              <li key={i} className="flex items-center gap-2 text-[9px] font-mono" style={{ color: "var(--text-secondary)" }}>
                <span className="w-4 text-center" style={{ color: "#00D4FF" }}>{i + 1}.</span>
                <span className="leading-relaxed">{step}</span>
              </li>
            ))}
          </ol>
        </div>
      )}

      {/* Active Tool */}
      {activeTool && (
        <div className="glass-light rounded-sm p-2 mb-2 animate-fade-in">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-[7px] font-display tracking-[0.2em]" style={{ color: "#F0B000" }}>
              {activeTool.type === "progress" ? "EXECUTING" : "TOOL"}
            </span>
            <div className="w-px h-4 bg-gradient-to-b from-transparent via-[#F0B000] to-transparent" />
          </div>
          <div className="flex items-center gap-2 text-[9px] font-mono" style={{ color: "var(--text-secondary)" }}>
            <span className="w-4 text-center" style={{ color: "#F0B000" }}>{activeTool.type === "progress" ? "▶" : "▸"}</span>
            <span className="font-medium" style={{ color: "#F0B000" }}>
              {activeTool.label || activeTool.name}
            </span>
            <span className="flex-1 border-b border-dotted" style={{ borderColor: "rgba(240,176,0,0.2)" }} />
            {activeTool.progress !== undefined && (
              <span style={{ color: "#F0B000" }}>{Math.round(activeTool.progress)}%</span>
            )}
          </div>
        </div>
      )}

      {/* Activity Log */}
      {activity.length > 0 && (
        <div className="glass-light rounded-sm p-2 animate-fade-in">
          <div className="text-[7px] font-display tracking-[0.2em] mb-1" style={{ color: "var(--text-muted)" }}>
            ACTIVITY
          </div>
          <div className="space-y-1 max-h-24 overflow-y-auto">
            {activity.slice(-6).map((a, i) => (
              <div key={a.id ?? i} className="flex items-center gap-2 text-[8px] font-mono" style={{ color: "var(--text-secondary)" }}>
                <span
                  style={{
                    color:
                      a.kind === "finished"
                        ? a.success
                          ? "#00FF88"
                          : "#FF3355"
                        : "#F0B000",
                  }}
                >
                  {a.kind === "started"
                    ? "▸"
                    : a.kind === "progress"
                      ? "·"
                      : a.success
                        ? "✓"
                        : "✕"}
                </span>
                {/* A finished entry is named by its tool plus the outcome, so
                    "git_commit — ok" is distinguishable from a still-running
                    one; anything else shows the backend's human-written label. */}
                <span className="truncate">
                  {a.kind === "finished" ? a.name : a.label || a.name || "action"}
                  {a.kind === "finished" && a.summary
                    ? ` — ${a.summary}`
                    : a.kind === "progress" && a.message
                      ? ` — ${a.message}`
                      : ""}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export default memo(ActivityFeed);