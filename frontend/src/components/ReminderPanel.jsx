import { memo, useState, useEffect, useRef } from "react";

function isArabic(text) { return /[\u0600-\u06FF]/.test(text); }

function formatTime(isoStr) {
  if (!isoStr) return "";
  try {
    const d = new Date(isoStr);
    if (isNaN(d.getTime())) return isoStr.replace("T", " ").slice(0, 16);
    return d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  } catch {
    return isoStr.slice(0, 16);
  }
}

function ReminderPanel({ reminders, onRefresh, onDelete, onDone, language }) {
  const [open, setOpen] = useState(false);
  const panelRef = useRef(null);
  const hasFetched = useRef(false);

  useEffect(() => {
    if (open && !hasFetched.current) { hasFetched.current = true; onRefresh(); }
    if (!open) { hasFetched.current = false; }
  }, [open, onRefresh]);

  const count = reminders.length;
  const ar = language === "ar";

  const trigger = (
    <button onClick={() => setOpen((v) => !v)}
      className="relative font-display font-bold tracking-wider transition-all"
      style={{
        fontSize: "var(--font-sm)",
        padding: "var(--space-1) var(--space-3)",
        background: open ? "color-mix(in srgb, var(--hud-cyan) 8%, transparent)" : count > 0 ? "color-mix(in srgb, var(--hud-cyan) 5%, transparent)" : "transparent",
        color: count > 0 ? "var(--hud-cyan)" : "var(--text-muted)",
        border: `var(--border-w) solid ${open ? "color-mix(in srgb, var(--hud-cyan) 25%, transparent)" : count > 0 ? "color-mix(in srgb, var(--hud-cyan) 12%, transparent)" : "var(--border-color)"}`,
        borderRadius: "var(--radius-sm)",
      }}
    >
      {ar ? "مهام" : "TASKS"}
      {count > 0 && (
        <span className="absolute font-bold flex items-center justify-center"
          style={{
            top: -4,
            insetInlineEnd: -4,
            width: 14,
            height: 14,
            fontSize: "var(--font-xs)",
            borderRadius: "50%",
            background: "var(--hud-cyan)",
            color: "var(--bg0)", // dark glyph on the bright cyan dot: contrast, not decoration
          }}
        >{count > 9 ? "9+" : count}</span>
      )}
    </button>
  );

  return (
    <>
      {trigger}
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div ref={panelRef} className="fixed z-50 overflow-hidden animate-fade-in"
            style={{
              insetInlineEnd: "var(--space-4)",
              top: "var(--header-h)",
              width: "20rem",
              maxHeight: "55vh",
              background: "var(--surface-2)",
              border: "var(--border-w) solid color-mix(in srgb, var(--hud-cyan) 15%, transparent)",
              borderRadius: "var(--radius-sm)",
              backdropFilter: "blur(20px)",
              boxShadow: "0 8px 40px rgba(0,0,0,0.6), 0 0 24px color-mix(in srgb, var(--hud-cyan) 5%, transparent)",
            }}
          >
            <div className="flex items-center justify-between" style={{ padding: "var(--space-2) var(--space-3)", borderBottom: "var(--border-w) solid color-mix(in srgb, var(--hud-cyan) 10%, transparent)" }}>
              <span className="font-display font-bold tracking-wider" style={{ fontSize: "var(--font-sm)", color: "var(--hud-cyan)" }}>
                {ar ? "المهام" : "REMINDERS"}
                {count > 0 && <span className="font-mono" style={{ marginLeft: "var(--space-1)", color: "var(--text-muted)" }}>({count})</span>}
              </span>
              <div className="flex" style={{ gap: "var(--space-1)" }}>
                <button onClick={onRefresh}
                  className="font-mono transition-all"
                  style={{
                    fontSize: "var(--font-xs)",
                    padding: "var(--space-1) var(--space-2)",
                    background: "color-mix(in srgb, var(--hud-cyan) 6%, transparent)",
                    color: "var(--hud-cyan)",
                    border: "var(--border-w) solid color-mix(in srgb, var(--hud-cyan) 12%, transparent)",
                    borderRadius: "var(--radius-sm)",
                  }}
                >{ar ? "تحديث" : "REFRESH"}</button>
                <button onClick={() => setOpen(false)}
                  className="font-mono transition-all"
                  style={{ fontSize: "var(--font-xs)", padding: "var(--space-1) var(--space-2)", color: "var(--text-muted)" }}
                >{ar ? "إغلاق" : "CLOSE"}</button>
              </div>
            </div>

            <div className="overflow-y-auto" style={{ maxHeight: "calc(55vh - 34px)" }}>
              {count === 0 ? (
                <div className="flex flex-col items-center justify-center text-center" style={{ padding: "var(--space-6) var(--space-5)" }}>
                  <span className="mb-1 font-mono" style={{ fontSize: "var(--font-md)", color: "var(--text-muted)" }}>📋</span>
                  <p className="font-mono" style={{ fontSize: "var(--font-sm)", color: "var(--text-muted)" }}>
                    {ar ? "لا توجد مهام" : "No pending reminders."}
                  </p>
                </div>
              ) : (
                <div className="flex flex-col" style={{ gap: "var(--space-1)", padding: "var(--space-1)" }}>
                  {reminders.map((r) => {
                    const rid = r.reminder_id || r.id;
                    const task = r.task || r.title || "?";
                    const time = formatTime(r.scheduled_time || r.remind_time);
                    const rec = r.recurring && r.recurring !== "none"
                      ? (ar ? { daily: "يومي", weekly: "أسبوعي", monthly: "شهري" }[r.recurring] || r.recurring : r.recurring)
                      : null;
                    return (
                      <div key={rid} className="flex items-start transition-all"
                        style={{ gap: "var(--space-2)", padding: "var(--space-2) var(--space-3)", background: "color-mix(in srgb, var(--hud-cyan) 2%, transparent)", border: "1px solid color-mix(in srgb, var(--hud-cyan) 6%, transparent)", borderRadius: "var(--radius-sm)" }}
                        dir={isArabic(task) ? "rtl" : "ltr"}
                      >
                        <div className="flex-1 min-w-0">
                          <p className="font-mono" style={{ fontSize: "var(--font-sm)", lineHeight: 1.45, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{task}</p>
                          <div className="flex items-center" style={{ gap: "var(--space-2)", marginTop: "var(--space-1)" }}>
                            <span className="font-mono" style={{ fontSize: "var(--font-xs)", color: "color-mix(in srgb, var(--hud-cyan) 60%, transparent)" }}>{time}</span>
                            {rec && (
                              <span className="font-mono"
                                style={{ fontSize: "var(--font-xs)", padding: "0 var(--space-1)", background: "color-mix(in srgb, var(--hud-cyan) 6%, transparent)", color: "var(--hud-cyan)", borderRadius: "var(--radius-sm)" }}
                              >{rec}</span>
                            )}
                          </div>
                        </div>
                        <div className="flex flex-shrink-0" style={{ gap: "var(--space-1)" }}>
                          <button onClick={() => onDone(rid)}
                            className="font-mono transition-all"
                            style={{ fontSize: "var(--font-xs)", padding: "var(--space-1)", color: "var(--hud-green)", border: "1px solid color-mix(in srgb, var(--hud-green) 15%, transparent)" }}
                          >✓</button>
                          <button onClick={() => onDelete(rid)}
                            className="font-mono transition-all"
                            style={{ fontSize: "var(--font-xs)", padding: "var(--space-1)", color: "var(--hud-red)", border: "1px solid color-mix(in srgb, var(--hud-red) 15%, transparent)" }}
                          >✕</button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </>
  );
}

export default memo(ReminderPanel);