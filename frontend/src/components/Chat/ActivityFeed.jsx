import { memo } from "react";
import { deriveToolRows, formatDuration } from "../../lib/toolRows";

/**
 * Plan + per-tool progress for the current turn.
 *
 * Tool events are folded by `deriveToolRows` into one row per invocation,
 * each with a status chip — ● RUNNING (carrying the `tool_progress`
 * heartbeat), ✓ COMPLETED (with a duration), ✕ FAILED (with the short
 * reason). Two tools in flight therefore render as two independent rows; one
 * finishing never blanks the other, which is what a line-per-event log could
 * not express.
 *
 * Chips carry a glyph as well as a colour (docs/DESIGN_SYSTEM.md §4), and
 * the whole feed is one polite live region so a screen reader hears the
 * progress without being spammed per frame.
 */
function ActivityFeed({ plan = [], activeTool = null, activity = [], language }) {
  const rows = deriveToolRows(activity);
  if (!plan.length && !activeTool && !activity.length) return null;

  const ar = language === "ar";

  // Density: the feed sits inside a rail, so its insets come from the same
  // scale as every other panel rather than a one-off padding.
  const block = {
    padding: "var(--space-3)",
    marginBottom: "var(--space-3)",
    borderRadius: "var(--radius-sm)",
  };
  const cap = {
    fontSize: "var(--font-xs)",
    letterSpacing: "0.18em",
  };

  // The dedicated "active tool" callout is only needed when nothing else is
  // showing it: in the app the started event is already in `activity`, so
  // rendering both would print the same tool twice.
  const activeCovered =
    !!activeTool && rows.some((r) => r.name === activeTool.name && r.status === "running");

  return (
    <div
      className="w-full max-w-2xl animate-fade-in"
      style={{ marginBottom: "var(--space-3)" }}
      role="status"
      aria-live="polite"
      aria-label="Assistant activity"
    >
      {/* Plan */}
      {plan.length > 0 && (
        <div className="glass-light animate-fade-in" style={block}>
          <div className="flex items-center" style={{ gap: "var(--space-2)", marginBottom: "var(--space-1)" }}>
            <span className="font-display" style={{ ...cap, color: "var(--hud-cyan)" }}>
              {ar ? "الخطة" : "PLAN"}
            </span>
            <div className="w-px h-3 bg-gradient-to-b from-transparent via-[var(--border-glow)] to-transparent" />
          </div>
          <ol className="flex flex-col" style={{ gap: "var(--space-1)" }}>
            {plan.map((step, i) => (
              <li
                key={i}
                className="flex items-center font-mono"
                style={{ gap: "var(--space-2)", fontSize: "var(--font-sm)", color: "var(--hud-text-dim)" }}
              >
                <span className="text-center" style={{ width: "var(--space-5)", color: "var(--hud-cyan)" }}>{i + 1}.</span>
                <span style={{ lineHeight: 1.45 }}>{step}</span>
              </li>
            ))}
          </ol>
        </div>
      )}

      {/* Active tool, only when no row already shows it running. */}
      {activeTool && !activeCovered && (
        <div className="glass-light animate-fade-in" style={block}>
          <div className="flex items-center" style={{ gap: "var(--space-2)", marginBottom: "var(--space-1)" }}>
            <span className="font-display" style={{ ...cap, color: "var(--hud-amber)" }}>
              {ar ? "تنفيذ" : "TOOL"}
            </span>
            <div className="w-px h-3 bg-gradient-to-b from-transparent via-[var(--hud-amber)] to-transparent" />
          </div>
          <div
            className="flex items-center font-mono"
            style={{ gap: "var(--space-2)", fontSize: "var(--font-sm)", color: "var(--hud-text-dim)" }}
          >
            <span className="text-center" style={{ width: "var(--space-5)", color: "var(--hud-amber)" }}>▶</span>
            <span style={{ color: "var(--hud-amber)" }}>
              {activeTool.label || activeTool.name}
            </span>
            <span className="flex-1 border-b border-dotted" style={{ borderColor: "color-mix(in srgb, var(--hud-amber) 25%, transparent)" }} />
          </div>
        </div>
      )}

      {/* Per-tool rows */}
      {rows.length > 0 && (
        <div className="glass-light animate-fade-in" style={block}>
          <div className="font-display" style={{ ...cap, color: "var(--hud-text-mute)", marginBottom: "var(--space-2)" }}>
            {ar ? "التنفيذ" : "ACTIVITY"}
          </div>
          <div
            className="flex flex-col overflow-y-auto"
            style={{ gap: "var(--space-2)", maxHeight: "calc(var(--space-6) * 6)" }}
          >
            {rows.slice(-6).map((row) => {
              const duration = formatDuration(row.durationMs);
              const detail =
                row.status === "running"
                  ? row.message || row.name
                  : `${row.name}${row.summary ? ` — ${row.summary}` : ""}`;
              return (
                <div key={row.key} className="flex flex-col" style={{ gap: 1 }}>
                  <div className="flex items-center" style={{ gap: "var(--space-2)" }}>
                    <ToolChip status={row.status} language={language} />
                    <span
                      className="truncate font-mono"
                      style={{ fontSize: "var(--font-sm)", color: "var(--hud-text-dim)", lineHeight: 1.4 }}
                    >
                      {row.label}
                    </span>
                    {duration && row.status !== "running" && (
                      <span
                        className="ml-auto flex-none font-mono"
                        style={{ fontSize: "var(--font-xs)", color: "var(--hud-text-mute)" }}
                        title={ar ? "مدة التنفيذ" : "duration"}
                      >
                        {duration}
                      </span>
                    )}
                  </div>
                  <div className="flex items-center font-mono" style={{ gap: "var(--space-2)" }}>
                    <span
                      className="truncate"
                      style={{
                        fontSize: "var(--font-xs)",
                        lineHeight: 1.5,
                        color:
                          row.status === "failed"
                            ? "var(--hud-red)"
                            : row.status === "ok"
                              ? "var(--hud-text-mute)"
                              : "var(--hud-amber)",
                      }}
                    >
                      {detail}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * The state chip. Glyph + word + colour, so "running/ok/failed" survives
 * greyscale — and the running dot pulses, which is how a heartbeat reads
 * before its message text arrives.
 */
function ToolChip({ status, language }) {
  const ar = language === "ar";
  if (status === "ok") {
    return (
      <span className="hud-pill hud-pill--ok flex-none">
        <span aria-hidden="true">✓</span>
        {ar ? "تم" : "DONE"}
      </span>
    );
  }
  if (status === "failed") {
    return (
      <span className="hud-pill hud-pill--danger flex-none">
        <span aria-hidden="true">✕</span>
        {ar ? "فشل" : "FAILED"}
      </span>
    );
  }
  return (
    <span className="hud-pill hud-pill--warn flex-none">
      <span className="hud-led hud-led--pulse" style={{ "--state-color": "var(--hud-amber)" }} aria-hidden="true" />
      {ar ? "يعمل" : "RUNNING"}
    </span>
  );
}

export default memo(ActivityFeed);
