import { memo, useEffect, useMemo } from "react";
import { riskTone, riskColor, riskPillClass } from "../lib/risk";

/**
 * ConfirmationDialog - Human-in-the-loop approval prompt.
 *
 * Rendered when the backend reports a high-risk tool call. The contract with
 * the user (docs/DESIGN_SYSTEM.md §4):
 *
 * - a boxed "ZAYTOON LOCAL EXECUTION" header, so a system prompt can never be
 *   mistaken for page content;
 * - the exact command / parameters, left-to-right and never hidden — in any
 *   language — because approving something you cannot read is not consent;
 * - the working directory the command runs in, when the backend sends one;
 * - a risk badge that carries a word as well as a colour (src/lib/risk.js);
 * - [ALLOW] / [CANCEL], with Enter/Esc to match.
 */

// Parameter keys with their own row — a path is easier to verify when it is
// not buried in a `key: value` dump.
const DIR_KEYS = ["working_directory", "cwd", "directory", "path_root"];

function serialiseParam(value) {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function ConfirmationDialog({ request, onRespond, language = "en" }) {
  const isArabic = language === "ar";

  // Enter allows, Escape cancels. Keeps the hotkey workflow fast.
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

  const derived = useMemo(() => {
    if (!request) return null;
    const params = request.params && typeof request.params === "object" ? request.params : {};
    const entries = Object.entries(params);
    const directoryEntry = entries.find(([k]) => DIR_KEYS.includes(k));
    const otherEntries = entries.filter(([k]) => !DIR_KEYS.includes(k));

    const reasons = Array.isArray(request.reasons) ? request.reasons : [];

    // The backend sends a `permission` tier (safe / confirm / dangerous) and a
    // regex-derived `level`. `riskTone` prefers the tier and grades by the
    // level only when the tier is missing — an unrecognised risk is never
    // shown as safe.
    const tone = riskTone(request.permission, request.level);

    const summary = request.text || request.summary || request.action || "";

    return { params, directoryEntry, otherEntries, reasons, tone, summary };
  }, [request]);

  if (!request || !derived) return null;

  const { directoryEntry, otherEntries, reasons, tone, summary } = derived;

  const title = isArabic ? "تأكيد مطلوب" : "Confirmation Required";
  const allowLabel = isArabic ? "موافق" : "ALLOW";
  const cancelLabel = isArabic ? "إلغاء" : "CANCEL";
  const hint = isArabic
    ? "Enter للسماح • Esc للإلغاء"
    : "Enter to allow • Esc to cancel";
  const directoryLabel = isArabic ? "المجلد" : "Directory";

  const TONE_LABEL = {
    safe: isArabic ? "آمن" : "SAFE",
    confirm: isArabic ? "يحتاج تأكيد" : "CONFIRMATION REQUIRED",
    high: isArabic ? "خطر" : "DANGEROUS",
  };
  const toneLabel = TONE_LABEL[tone];
  const color = riskColor(tone);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center"
      style={{
        padding: "var(--sp-4)",
        background: "var(--scrim, rgba(3, 6, 12, 0.8))",
        backdropFilter: "blur(6px)",
      }}
      role="alertdialog"
      aria-modal="true"
      aria-label={title}
    >
      <div
        className="w-full max-w-lg font-mono"
        style={{
          background: "var(--surface-2)",
          border: `1px solid ${color}`,
          borderRadius: "var(--radius-panel)",
          boxShadow: `0 0 0 1px color-mix(in srgb, ${color} 30%, transparent), 0 24px 60px rgba(0, 0, 0, 0.55)`,
          overflow: "hidden",
        }}
      >
        {/* Boxed system header: this prompt is executed locally, by Zaytoon. */}
        <div
          className="flex items-center justify-between"
          style={{
            gap: "var(--sp-2)",
            padding: "var(--sp-2) var(--sp-3)",
            borderBottom: "1px solid var(--border)",
            background: "color-mix(in srgb, var(--accent) 8%, var(--surface-1))",
          }}
        >
          <span
            className="text-[10px] font-bold tracking-[0.28em] uppercase"
            style={{
              padding: "2px var(--sp-2)",
              border: "1px solid var(--border-strong)",
              borderRadius: "var(--radius-sm)",
              color: "var(--accent)",
              background: "var(--surface-0)",
            }}
          >
            ZAYTOON LOCAL EXECUTION
          </span>
          <span className={`${riskPillClass(tone)} text-[10px]`}>{toneLabel}</span>
        </div>

        <div style={{ padding: "var(--sp-4)" }}>
          <div
            className="flex items-center flex-wrap"
            style={{ gap: "var(--sp-2)", marginBottom: "var(--sp-3)" }}
          >
            <span
              className="inline-block rounded-full"
              style={{
                width: 8,
                height: 8,
                background: color,
                boxShadow: `0 0 8px ${color}`,
              }}
            />
            <h2 className="font-bold tracking-[0.2em]" style={{ fontSize: "var(--font-md)", color }}>
              {title}
            </h2>
            {request.action && (
              <span className="text-[11px]" style={{ color: "var(--text-mute)" }}>
                {request.action}
              </span>
            )}
          </div>

          {/* What will happen, in the user's language. */}
          {summary && (
            <div
              className="mb-3 break-words"
              style={{
                fontSize: "var(--font-lg)",
                padding: "var(--sp-3)",
                background: `color-mix(in srgb, ${color} 8%, transparent)`,
                border: `1px solid color-mix(in srgb, ${color} 35%, transparent)`,
                borderRadius: "var(--radius-card)",
                color: "var(--hud-text)",
              }}
            >
              {summary}
            </div>
          )}

          {/* The exact command / parameters, always shown, always LTR. */}
          {otherEntries.length > 0 && (
            <pre
              dir="ltr"
              className="mb-3 overflow-x-auto whitespace-pre-wrap break-words"
              style={{
                fontSize: "var(--font-lg)",
                padding: "var(--sp-3)",
                background: "var(--surface-0)",
                border: "1px solid var(--border)",
                borderRadius: "var(--radius-card)",
                color: "var(--hud-text)",
              }}
            >
              {otherEntries.map(([k, v]) => `${k}: ${serialiseParam(v)}`).join("\n")}
            </pre>
          )}

          {/* Where it runs — the containment root, verifiable at a glance. */}
          {directoryEntry && (
            <div
              className="flex items-baseline flex-wrap mb-3 break-all"
              style={{ gap: "var(--sp-2)", fontSize: "var(--font-lg)" }}
            >
              <span style={{ color: "var(--text-mute)" }}>{directoryLabel}:</span>
              <span dir="ltr" style={{ color: "var(--hud-text)" }}>
                {serialiseParam(directoryEntry[1])}
              </span>
            </div>
          )}

          {reasons.length > 0 && (
            <ul className="flex flex-col" style={{ gap: "var(--space-1)", marginBottom: "var(--sp-4)" }}>
              {reasons.map((reason, i) => (
                <li
                  key={i}
                  className="flex items-start"
                  style={{ gap: "var(--sp-2)", fontSize: "var(--font-sm)", color: "var(--text-mute)" }}
                >
                  <span style={{ color }}>▸</span>
                  <span>{reason}</span>
                </li>
              ))}
            </ul>
          )}

          <div className="flex items-center justify-end flex-wrap" style={{ gap: "var(--sp-3)" }}>
            <span
              className="text-[10px] tracking-wider"
              style={{ color: "var(--text-faint)", marginInlineEnd: "auto" }}
            >
              {hint}
            </span>
            <button
              onClick={() => onRespond(false)}
              className="font-bold tracking-[0.2em]"
              style={{
                fontSize: "var(--font-sm)",
                padding: "var(--sp-2) var(--sp-4)",
                background: "transparent",
                color: "var(--text-mute)",
                border: "1px solid var(--border-strong)",
                borderRadius: "var(--radius-card)",
                transition: "var(--t-fast)",
              }}
            >
              {cancelLabel}
            </button>
            <button
              onClick={() => onRespond(true)}
              autoFocus
              className="font-bold tracking-[0.2em]"
              style={{
                fontSize: "var(--font-sm)",
                padding: "var(--sp-2) var(--sp-4)",
                background: `color-mix(in srgb, ${color} 14%, transparent)`,
                color,
                border: `1px solid color-mix(in srgb, ${color} 55%, transparent)`,
                borderRadius: "var(--radius-card)",
                transition: "var(--t-fast)",
              }}
            >
              {allowLabel}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default memo(ConfirmationDialog);
