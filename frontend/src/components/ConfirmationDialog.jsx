import { memo, useEffect } from "react";

/**
 * ConfirmationDialog - Human-in-the-loop approval prompt
 * Rendered when the backend reports a high-risk tool call
 */
function ConfirmationDialog({ request, onRespond, language = "en" }) {
  const isArabic = language === "ar";

  // Enter approves, Escape denies. Keeps the hotkey workflow fast.
  useEffect(() => {
    if (!request) return undefined;
    const onKey = (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        onRespond(true);
      } else if (e.key === "Escape") {
        e.preventDefault();
        onRespond(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [request, onRespond]);

  if (!request) return null;

  const reasons = Array.isArray(request.reasons) ? request.reasons : [];
  const title = isArabic ? "تأكيد مطلوب" : "Confirmation Required";
  const approve = isArabic ? "موافق" : "APPROVE";
  const deny = isArabic ? "رفض" : "DENY";
  const hint = isArabic
    ? "Enter للموافقة • Esc للرفض"
    : "Enter to approve • Esc to deny";

  // The backend sends a `permission` tier (safe / confirm / dangerous) in
  // addition to the regex-derived `level`. It is the tier that describes the
  // *class* of action, so it is what the user should weigh.
  const PERMISSION_LABEL = {
    safe: isArabic ? "آمن" : "SAFE",
    confirm: isArabic ? "يحتاج تأكيد" : "CONFIRMATION REQUIRED",
    dangerous: isArabic ? "خطر" : "DANGEROUS",
  };
  const tier = request.permission || "confirm";
  const tierLabel = PERMISSION_LABEL[tier] || tier.toUpperCase();
  const isDangerous = tier === "dangerous";

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ background: "rgba(3, 3, 8, 0.78)", backdropFilter: "blur(6px)" }}
      role="alertdialog"
      aria-modal="true"
      aria-label={title}
    >
      <div
        className="w-full max-w-lg rounded-lg p-5 font-mono"
        style={{
          background: "rgba(20, 8, 8, 0.96)",
          border: `1px solid ${isDangerous ? "rgba(255, 40, 40, 0.85)" : "rgba(255, 60, 80, 0.5)"}`,
          boxShadow: isDangerous
            ? "0 0 0 1px rgba(255,40,40,0.3), 0 0 50px rgba(255, 40, 40, 0.35)"
            : "0 0 0 1px rgba(255,60,80,0.15), 0 0 40px rgba(255, 51, 85, 0.25)",
        }}
      >
        <div className="flex items-center gap-2 mb-3 flex-wrap">
          <span
            className="inline-block w-2.5 h-2.5 rounded-full"
            style={{
              background: "#ff3355",
              boxShadow: "0 0 10px #ff3355",
              animation: "pulse-ring-red 1.2s infinite",
            }}
          />
          <h2
            className="text-xs font-bold tracking-[0.25em]"
            style={{ color: "#ff5a75" }}
          >
            {title}
          </h2>
          <span
            className="text-[9px] px-2 py-0.5 rounded-sm tracking-[0.15em]"
            style={{
              color: isDangerous ? "#ff8080" : "#ffb400",
              border: `1px solid ${isDangerous ? "rgba(255,40,40,0.5)" : "rgba(255,180,0,0.45)"}`,
              background: isDangerous ? "rgba(255,40,40,0.1)" : "rgba(255,180,0,0.08)",
            }}
          >
            {tierLabel}
          </span>
          {request.action && (
            <span className="text-[9px]" style={{ color: "var(--text-muted)" }}>
              {request.action}
            </span>
          )}
        </div>

        {/* Show exactly what will run, so the user can check it before approving. */}
        {request.params && Object.keys(request.params).length > 0 && (
          <pre
            dir="ltr"
            className="text-[11px] mb-3 p-3 rounded-sm overflow-x-auto whitespace-pre-wrap break-words"
            style={{
              background: "rgba(0, 0, 0, 0.45)",
              border: "1px solid rgba(255, 255, 255, 0.1)",
              color: "#CFE8FF",
            }}
          >
            {Object.entries(request.params)
              .map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`)
              .join("\n")}
          </pre>
        )}

        <div
          className="text-sm mb-3 p-3 rounded-sm break-words"
          style={{
            background: "rgba(255, 51, 85, 0.06)",
            border: "1px solid rgba(255, 51, 85, 0.2)",
            color: "#FFD7DD",
          }}
        >
          {request.text || request.summary || request.action}
        </div>

        {reasons.length > 0 && (
          <ul className="mb-4 space-y-1">
            {reasons.map((reason, i) => (
              <li
                key={i}
                className="text-[11px] flex items-start gap-2"
                style={{ color: "#FF9AA8" }}
              >
                <span style={{ color: "#FF3355" }}>▸</span>
                <span>{reason}</span>
              </li>
            ))}
          </ul>
        )}

        <div className="flex items-center justify-end gap-3">
          <span className="text-[10px] tracking-wider" style={{ color: "#8888AA" }}>
            {hint}
          </span>
          <button
            onClick={() => onRespond(false)}
            className="text-[11px] px-4 py-2 rounded-sm font-bold tracking-[0.2em] transition-all"
            style={{
              background: "rgba(255, 51, 85, 0.08)",
              color: "#FF5A75",
              border: "1px solid rgba(255, 51, 85, 0.35)",
            }}
          >
            {deny}
          </button>
          <button
            onClick={() => onRespond(true)}
            autoFocus
            className="text-[11px] px-4 py-2 rounded-sm font-bold tracking-[0.2em] transition-all"
            style={{
              background: "rgba(0, 255, 136, 0.1)",
              color: "#00FF88",
              border: "1px solid rgba(0, 255, 136, 0.4)",
              boxShadow: "0 0 12px rgba(0,255,136,0.2)",
            }}
          >
            {approve}
          </button>
        </div>
      </div>
    </div>
  );
}

export default memo(ConfirmationDialog);