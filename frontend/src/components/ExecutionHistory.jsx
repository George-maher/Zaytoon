import { memo, useCallback, useEffect, useState } from "react";
import { getAudit, clearAudit, getProcesses, stopProcess } from "../lib/api";
import { riskTone, riskPillClass } from "../lib/risk";
import { translate } from "../i18n/strings";
import EmptyState from "./EmptyState";

/**
 * Execution history: what the assistant has actually been allowed to do.
 *
 * Three rules this component exists to keep:
 *
 * 1. **Every endpoint here is optional.** `/audit` and `/processes` answer
 *    404 on a backend that has not shipped them; that renders as "Audit log
 *    unavailable" and a hidden processes card — never as a broken panel and
 *    never as an empty log that reads like "nothing ever ran".
 * 2. **Clearing is deliberate.** `window.confirm` is unavailable in jsdom and
 *    hostile to keyboard users anyway, so CLEAR is a two-step inline confirm.
 * 3. **Rows are parsed defensively.** The audit schema is the backend's to
 *    change; an unknown shape degrades to showing whatever text the entry
 *    carries rather than throwing inside a list render.
 */

const AUDIT_UNAVAILABLE_EN = "Audit log unavailable";

function formatWhen(value) {
  if (value === null || value === undefined || value === "") return "";
  if (typeof value === "number") {
    const ms = value > 1e12 ? value : value * 1000;
    const d = new Date(ms);
    return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleTimeString();
  }
  const s = String(value);
  const d = new Date(s);
  if (!Number.isNaN(d.getTime()) && s.includes("-")) return d.toLocaleString();
  return s;
}

function normaliseEntry(entry, index) {
  if (typeof entry === "string") {
    return { key: `a${index}`, when: "", action: entry, detail: "", tone: "confirm", ok: null };
  }
  const e = entry && typeof entry === "object" ? entry : {};
  const rawStatus = e.success ?? e.ok ?? e.status;
  const ok =
    typeof rawStatus === "boolean"
      ? rawStatus
      : typeof rawStatus === "string"
        ? ["ok", "success", "done", "completed", "passed"].includes(rawStatus.toLowerCase())
        : null;
  const action = e.action || e.tool || e.command || e.event || e.type || "";
  const detail = e.detail || e.summary || e.message || "";
  const tone = ok === false ? "high" : ok === true ? "safe" : riskTone(e.permission, e.level);
  return {
    key: e.id ?? `a${index}`,
    when: formatWhen(e.time ?? e.timestamp ?? e.ts ?? e.created_at),
    action: typeof action === "string" ? action : JSON.stringify(action),
    detail: typeof detail === "string" ? detail : JSON.stringify(detail),
    tone,
    ok,
  };
}

function HistoryCard({ title, children, actions }) {
  return (
    <div
      className="glass-light"
      style={{
        padding: "var(--space-3)",
        borderRadius: "var(--radius-sm)",
        border: "var(--border-w) solid var(--hud-edge-faint)",
      }}
    >
      <div className="flex items-center" style={{ gap: "var(--space-2)", marginBottom: "var(--space-2)" }}>
        <span
          className="font-display tracking-[0.2em]"
          style={{ fontSize: "var(--font-xs)", color: "var(--text-muted)" }}
        >
          {title}
        </span>
        <span className="flex items-center" style={{ gap: "var(--space-1)", marginInlineStart: "auto" }}>
          {actions}
        </span>
      </div>
      <div style={{ display: "grid", gap: "var(--space-1)" }}>{children}</div>
    </div>
  );
}

function MiniButton({ onClick, disabled, children, tone = "accent" }) {
  const color =
    tone === "danger" ? "var(--hud-red)" : tone === "ok" ? "var(--hud-green)" : "var(--hud-cyan)";
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="font-bold tracking-[0.15em] transition-all disabled:opacity-30"
      style={{
        padding: "var(--space-1) var(--space-3)",
        fontSize: "var(--font-xs)",
        fontFamily: "var(--font-mono)",
        background: `color-mix(in srgb, ${color} 8%, transparent)`,
        color,
        border: `var(--border-w) solid color-mix(in srgb, ${color} 30%, transparent)`,
        borderRadius: "var(--radius-sm)",
      }}
    >
      {children}
    </button>
  );
}

function Note({ tone, children }) {
  const color = tone === "error" ? "var(--hud-red)" : "var(--text-muted)";
  return (
    <p className="font-mono" style={{ fontSize: "var(--font-xs)", color }}>
      {children}
    </p>
  );
}

export default memo(function ExecutionHistory({ language = "en" }) {
  const ar = language === "ar";
  const [audit, setAudit] = useState(null); // null = still loading
  const [auditError, setAuditError] = useState(null);
  const [proc, setProc] = useState(null);
  const [busy, setBusy] = useState(null); // "clear" | "refresh" | stop-id
  const [confirmClear, setConfirmClear] = useState(false);
  const [notice, setNotice] = useState(null);

  const loadAudit = useCallback(async () => {
    try {
      const data = await getAudit(100);
      setAudit(data);
      setAuditError(null);
    } catch (e) {
      setAudit(null);
      setAuditError(e?.message || String(e));
    }
  }, []);

  const loadProcesses = useCallback(async () => {
    try {
      setProc(await getProcesses());
    } catch {
      // A failing processes poll must never take the history down with it;
      // the card simply does not render.
      setProc({ unavailable: true, processes: [] });
    }
  }, []);

  useEffect(() => {
    loadAudit();
    loadProcesses();
  }, [loadAudit, loadProcesses]);

  const refresh = useCallback(async () => {
    setBusy("refresh");
    try {
      await Promise.all([loadAudit(), loadProcesses()]);
    } finally {
      setBusy(null);
    }
  }, [loadAudit, loadProcesses]);

  const confirmClearNow = useCallback(async () => {
    setBusy("clear");
    try {
      const res = await clearAudit();
      if (res.unavailable) {
        setNotice({ ok: false, text: ar ? "سجل العمليات غير متاح" : AUDIT_UNAVAILABLE_EN });
      } else {
        setNotice({
          ok: true,
          text: ar ? `تم مسح ${res.cleared} سجلًا` : `Cleared ${res.cleared} entries`,
        });
        await loadAudit();
      }
    } catch (e) {
      setNotice({ ok: false, text: e?.message || String(e) });
    } finally {
      setBusy(null);
      setConfirmClear(false);
    }
  }, [ar, loadAudit]);

  const onStop = useCallback(
    async (id) => {
      setBusy(`stop:${id}`);
      try {
        const res = await stopProcess(id);
        if (!res.unavailable) await loadProcesses();
      } catch (e) {
        setNotice({ ok: false, text: e?.message || String(e) });
      } finally {
        setBusy(null);
      }
    },
    [loadProcesses],
  );

  const unavailable = audit?.unavailable === true;
  const entries = Array.isArray(audit?.entries) ? audit.entries.map(normaliseEntry) : [];
  const processes = Array.isArray(proc?.processes) ? proc.processes : [];

  return (
    <div style={{ display: "grid", gap: "var(--space-4)" }}>
      <HistoryCard
        title={ar ? "سجل التنفيذ" : "EXECUTION HISTORY"}
        actions={
          <>
            <MiniButton onClick={refresh} disabled={!!busy}>
              {busy === "refresh" ? "…" : ar ? "تحديث" : "REFRESH"}
            </MiniButton>
            {!unavailable && !auditError && (
              <MiniButton tone="danger" onClick={() => setConfirmClear((v) => !v)} disabled={!!busy}>
                {ar ? "مسح" : "CLEAR"}
              </MiniButton>
            )}
          </>
        }
      >
        {notice && <Note tone={notice.ok ? "ok" : "error"}>{notice.text}</Note>}

        {confirmClear && (
          <div
            className="flex items-center flex-wrap"
            style={{
              gap: "var(--space-2)",
              padding: "var(--space-2)",
              border: "var(--border-w) solid color-mix(in srgb, var(--hud-red) 35%, transparent)",
              background: "color-mix(in srgb, var(--hud-red) 6%, transparent)",
              borderRadius: "var(--radius-sm)",
            }}
          >
            <span className="font-mono" style={{ fontSize: "var(--font-xs)", color: "var(--text-secondary)" }}>
              {ar
                ? `مسح كل السجلات (${entries.length})؟ لا يمكن التراجع.`
                : `Clear all ${entries.length} entries? This cannot be undone.`}
            </span>
            <span style={{ marginInlineStart: "auto", display: "flex", gap: "var(--space-1)" }}>
              <MiniButton onClick={() => setConfirmClear(false)} disabled={!!busy}>
                {ar ? "إلغاء" : "CANCEL"}
              </MiniButton>
              <MiniButton tone="danger" onClick={confirmClearNow} disabled={!!busy}>
                {busy === "clear" ? "…" : ar ? "تأكيد" : "CONFIRM"}
              </MiniButton>
            </span>
          </div>
        )}

        {auditError && <Note tone="error">{`${ar ? "تعذر جلب السجل" : "Audit fetch failed"}: ${auditError}`}</Note>}

        {!auditError && audit === null && !unavailable && (
          <Note>{ar ? "جارٍ التحميل…" : "LOADING…"}</Note>
        )}

        {/* Optional endpoint: say so plainly instead of rendering an empty
            list that reads as "nothing has ever run". */}
        {unavailable && (
          <Note tone="error">{ar ? "سجل العمليات غير متاح" : AUDIT_UNAVAILABLE_EN}</Note>
        )}

        {!auditError && !unavailable && audit !== null && entries.length === 0 && (
          <EmptyState
            title={translate(language, "execution_history.no_records")}
            description={translate(language, "execution_history.no_records_description")}
            glyph="○"
          />
        )}

        {entries.map((e) => (
          <div
            key={e.key}
            className="flex items-baseline"
            style={{
              gap: "var(--space-2)",
              padding: "var(--space-1) 0",
              borderBottom: "var(--border-w) solid var(--border-color)",
              fontFamily: "var(--font-mono)",
              fontSize: "var(--font-xs)",
            }}
          >
            <span className="flex-none" style={{ color: "var(--text-faint)", minWidth: 64 }}>
              {e.when || "—"}
            </span>
            <span className="flex-none" style={{ color: "var(--text-secondary)" }}>
              {e.action || "—"}
            </span>
            <span className={`${riskPillClass(e.tone)} flex-none`}>
              {e.ok === false ? (ar ? "فشل" : "FAILED") : e.ok === true ? (ar ? "نجح" : "OK") : (ar ? "؟" : "?")}
            </span>
            {e.detail && (
              <span className="truncate" style={{ color: "var(--text-faint)" }} title={e.detail}>
                {e.detail}
              </span>
            )}
          </div>
        ))}

        {!unavailable && !auditError && audit !== null && (
          <div className="font-mono" style={{ fontSize: "var(--font-xs)", color: "var(--text-faint)" }}>
            {ar ? `الإجمالي: ${audit.total}` : `TOTAL ${audit.total}`}
          </div>
        )}
      </HistoryCard>

      {/* Optional endpoint: the card does not exist when the backend has no
          process registry, rather than existing and saying "none". */}
      {!proc?.unavailable && (
        <HistoryCard title={ar ? "العمليات" : "PROCESSES"}>
          {processes.length === 0 ? (
            <EmptyState
              title={translate(language, "execution_history.no_processes")}
              glyph="○"
            />
          ) : (
            processes.map((p) => {
              const id = p.id ?? p.pid ?? p.name;
              return (
                <div
                  key={String(id)}
                  className="flex items-center"
                  style={{
                    gap: "var(--space-2)",
                    padding: "var(--space-1) 0",
                    borderBottom: "var(--border-w) solid var(--border-color)",
                    fontFamily: "var(--font-mono)",
                    fontSize: "var(--font-xs)",
                  }}
                >
                  <span className="truncate" style={{ color: "var(--text-secondary)" }}>
                    {p.name || p.command || id}
                  </span>
                  <span className="flex-none" style={{ color: "var(--text-faint)" }}>
                    {p.status || p.state || "—"}
                  </span>
                  <span style={{ marginInlineStart: "auto" }}>
                    <MiniButton
                      tone="danger"
                      onClick={() => onStop(id)}
                      disabled={!!busy}
                    >
                      {busy === `stop:${id}` ? "…" : ar ? "إيقاف" : "STOP"}
                    </MiniButton>
                  </span>
                </div>
              );
            })
          )}
        </HistoryCard>
      )}
    </div>
  );
});
