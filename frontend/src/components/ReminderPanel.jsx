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
      className="relative text-[9px] px-2 py-1 rounded-sm font-display font-bold tracking-wider transition-all"
      style={{
        background: open ? "rgba(0, 212, 255, 0.08)" : count > 0 ? "rgba(0, 212, 255, 0.05)" : "transparent",
        color: count > 0 ? "#00D4FF" : "var(--text-muted)",
        border: `1px solid ${open ? "rgba(0, 212, 255, 0.25)" : count > 0 ? "rgba(0, 212, 255, 0.12)" : "var(--border-color)"}`,
      }}
    >
      {ar ? "مهام" : "TASKS"}
      {count > 0 && (
        <span className="absolute -top-1.5 -right-1.5 w-3.5 h-3.5 rounded-full text-[7px] flex items-center justify-center font-bold"
          style={{ background: "#00D4FF", color: "#050508" }}
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
          <div ref={panelRef} className="fixed right-3 top-12 z-50 w-80 max-h-[60vh] rounded-sm overflow-hidden animate-fade-in"
            style={{
              background: "rgba(5, 5, 12, 0.95)",
              border: "1px solid rgba(0, 212, 255, 0.15)",
              backdropFilter: "blur(20px)",
              boxShadow: "0 8px 40px rgba(0,0,0,0.6), 0 0 30px rgba(0,212,255,0.05)",
            }}
          >
            <div className="flex items-center justify-between px-3 py-2 border-b" style={{ borderColor: "rgba(0, 212, 255, 0.1)" }}>
              <span className="text-[10px] font-display font-bold tracking-wider" style={{ color: "#00D4FF" }}>
                {ar ? "المهام" : "REMINDERS"}
                {count > 0 && <span className="ml-1 font-mono" style={{ color: "var(--text-muted)" }}>({count})</span>}
              </span>
              <div className="flex gap-1">
                <button onClick={onRefresh}
                  className="text-[8px] px-1.5 py-0.5 rounded-sm font-mono transition-all"
                  style={{
                    background: "rgba(0, 212, 255, 0.06)",
                    color: "#00D4FF",
                    border: "1px solid rgba(0, 212, 255, 0.12)",
                  }}
                >{ar ? "تحديث" : "REFRESH"}</button>
                <button onClick={() => setOpen(false)}
                  className="text-[8px] px-1.5 py-0.5 rounded-sm font-mono transition-all"
                  style={{ color: "var(--text-muted)" }}
                >{ar ? "إغلاق" : "CLOSE"}</button>
              </div>
            </div>

            <div className="overflow-y-auto max-h-[calc(60vh-36px)]">
              {count === 0 ? (
                <div className="flex flex-col items-center justify-center py-8 px-4 text-center">
                  <span className="text-lg mb-1 font-mono" style={{ color: "var(--text-muted)" }}>📋</span>
                  <p className="text-[10px] font-mono" style={{ color: "var(--text-muted)" }}>
                    {ar ? "لا توجد مهام" : "No pending reminders."}
                  </p>
                </div>
              ) : (
                <div className="space-y-0.5 p-1">
                  {reminders.map((r) => {
                    const rid = r.reminder_id || r.id;
                    const task = r.task || r.title || "?";
                    const time = formatTime(r.scheduled_time || r.remind_time);
                    const rec = r.recurring && r.recurring !== "none"
                      ? (ar ? { daily: "يومي", weekly: "أسبوعي", monthly: "شهري" }[r.recurring] || r.recurring : r.recurring)
                      : null;
                    return (
                      <div key={rid} className="flex items-start gap-1.5 px-2 py-1.5 rounded-sm transition-all"
                        style={{ background: "rgba(0, 212, 255, 0.02)", border: "1px solid rgba(0, 212, 255, 0.06)" }}
                        dir={isArabic(task) ? "rtl" : "ltr"}
                      >
                        <div className="flex-1 min-w-0">
                          <p className="text-[9px] font-mono leading-relaxed truncate">{task}</p>
                          <div className="flex items-center gap-1.5 mt-0.5">
                            <span className="text-[7px] font-mono" style={{ color: "rgba(0, 212, 255, 0.6)" }}>{time}</span>
                            {rec && (
                              <span className="text-[6px] font-mono px-1 rounded-sm"
                                style={{ background: "rgba(0, 212, 255, 0.06)", color: "#00D4FF" }}
                              >{rec}</span>
                            )}
                          </div>
                        </div>
                        <div className="flex gap-0.5 flex-shrink-0">
                          <button onClick={() => onDone(rid)}
                            className="text-[7px] px-1 py-0.5 rounded-sm font-mono transition-all"
                            style={{ color: "#00FF88", border: "1px solid rgba(0, 255, 136, 0.15)" }}
                          >✓</button>
                          <button onClick={() => onDelete(rid)}
                            className="text-[7px] px-1 py-0.5 rounded-sm font-mono transition-all"
                            style={{ color: "#FF3355", border: "1px solid rgba(255, 51, 85, 0.15)" }}
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