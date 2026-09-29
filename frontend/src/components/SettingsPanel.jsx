import { memo, useCallback, useEffect, useRef, useState } from "react";
import { getWorkspace, healthCheck, setWorkspace } from "../lib/api";

/**
 * Configuration panel.
 *
 * Three things, in priority order:
 *
 * 1. **The workspace root.** Every file tool is confined to it by a
 *    `commonpath` containment check, so it decides what the assistant can
 *    touch at all. It used to be settable only by editing `backend/.env` and
 *    restarting, which made the containment boundary effectively invisible.
 * 2. **Whether the OpenRouter key is present.** If it is missing the assistant
 *    cannot answer, and until now the UI gave no hint of that.
 * 3. **The model actually in use.** The side panel used to display a
 *    hard-coded "QWEN 2.5 7B" that matched no configured model.
 *
 * Two properties this must keep, both covered in `ui.test.jsx`:
 *
 * - **A failure is reported, never papered over.** An unreachable backend or a
 *   rejected path shows what happened; it does not fall back to rendering the
 *   last known-good values, which would look like a healthy system.
 * - **The workspace is edited as a draft and committed explicitly.** The path
 *   field used to POST on every keystroke, so typing "D:/" fired a
 *   `POST /settings/workspace` per character and a partially-typed path was
 *   rejected (and displayed) three times before the first valid one.
 */
function SettingsPanel({ open, onClose, language = "en" }) {
  const ar = language === "ar";

  const [health, setHealth] = useState(null);
  const [healthError, setHealthError] = useState(null);
  const [workspace, setWorkspaceState] = useState(null);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState(null);
  const inputRef = useRef(null);

  const load = useCallback(async () => {
    try {
      const [h, w] = await Promise.all([healthCheck(), getWorkspace()]);
      setHealth(h);
      setHealthError(null);
      setWorkspaceState(w);
      setDraft(w.root || "");
    } catch (e) {
      // Stale data is worse than none: an unreachable backend rendered with
      // the last successful payload reads as a working system.
      setHealthError(e.message || String(e));
      setHealth(null);
      setWorkspaceState(null);
    }
  }, []);

  useEffect(() => {
    if (!open) return undefined;
    load();
    const poll = setInterval(load, 15000);
    return () => clearInterval(poll);
  }, [open, load]);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  // Clear a transient notice so it cannot linger and mislead.
  useEffect(() => {
    if (!notice) return undefined;
    const t = setTimeout(() => setNotice(null), 5000);
    return () => clearTimeout(t);
  }, [notice]);

  const save = useCallback(async () => {
    const root = draft.trim();
    if (!root || root === workspace?.root) return;
    setSaving(true);
    setNotice(null);
    try {
      const result = await setWorkspace(root);
      if (result.error) {
        setNotice({ ok: false, text: result.error });
      } else {
        setWorkspaceState((prev) => ({ ...prev, ...result }));
        setDraft(result.root || root);
        setNotice({ ok: true, text: ar ? "تم تحديث مجلد العمل" : "Workspace updated" });
      }
    } catch (e) {
      setNotice({ ok: false, text: e.message || String(e) });
    } finally {
      setSaving(false);
    }
  }, [draft, workspace, ar]);

  if (!open) return null;

  const llm = health?.llm || {};
  const chain = Array.isArray(llm.chain) ? llm.chain : [];
  const metrics = llm.metrics || {};
  const rejected = Array.isArray(llm.rejected_models) ? llm.rejected_models : [];
  const voice = health?.voice || {};
  const safety = health?.safety || {};
  const dirty = draft.trim() && draft.trim() !== workspace?.root;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ background: "rgba(3, 3, 8, 0.78)", backdropFilter: "blur(6px)" }}
      role="dialog"
      aria-modal="true"
      aria-label={ar ? "الإعدادات" : "Settings"}
    >
      <div
        className="w-full max-w-lg max-h-[85vh] overflow-y-auto rounded-lg p-5 font-mono"
        style={{
          background: "rgba(8, 8, 18, 0.97)",
          border: "1px solid rgba(0, 212, 255, 0.2)",
          boxShadow: "0 0 50px rgba(0, 212, 255, 0.06)",
        }}
      >
        <div className="flex items-center gap-2 mb-3">
          <span
            className="inline-block w-2.5 h-2.5 rounded-full"
            style={{
              background: healthError ? "#F0B000" : "#00D4FF",
              boxShadow: `0 0 10px ${healthError ? "#F0B000" : "#00D4FF"}`,
              animation: "pulse-ring 1.2s infinite",
            }}
          />
          <h2 className="text-xs font-bold tracking-[0.25em]" style={{ color: "#00D4FF" }}>
            {ar ? "الإعدادات" : "SETTINGS"}
          </h2>
        </div>

        <div className="space-y-4">
          {healthError && (
            <Banner
              tone="error"
              text={`${ar ? "تعذر الاتصال بالخدمة" : "Backend unreachable"}: ${healthError}`}
            />
          )}

          {/* LLM */}
          <Card title="LLM CONFIGURATION">
            <Row
              label={ar ? "المفتاح" : "API key"}
              value={
                llm.configured === false
                  ? ar ? "مفقود" : "MISSING"
                  : ar ? "موجود" : "present"
              }
              tone={llm.configured === false ? "error" : "ok"}
            />
            <Row
              label={ar ? "النموذج" : "Model"}
              value={llm.primary_model || "—"}
              title={llm.primary_model}
            />
            <Row
              label={ar ? "الاستدلال" : "Reasoning"}
              value={llm.reasoning_model || "—"}
              title={llm.reasoning_model}
            />
            {chain.length > 0 && (
              <Row
                label={ar ? "سلسلة احتياطية" : "Fallback chain"}
                value={chain.map((c) => c.model).join(" → ")}
                title={chain.map((c) => c.model).join(" → ")}
              />
            )}

            {/*
              A rejected model is a policy event, not a detail: the free-only
              rule is enforced on the request path, so a model appearing here
              means it was requested and refused.
            */}
            {rejected.length > 0 && (
              <div className="mt-2">
                <Banner
                  tone="error"
                  text={`${ar ? "نماذج مرفوضة" : "Rejected models"}: ${rejected.join(", ")}`}
                />
              </div>
            )}

            {typeof metrics.requests === "number" && (
              <>
                <div className="mt-2 pt-2 border-t" style={{ borderColor: "var(--border-color)" }}>
                  <span className="text-[7px] font-mono tracking-wider" style={{ color: "var(--text-muted)" }}>
                    METRICS
                  </span>
                </div>
                <div className="grid grid-cols-2 gap-2 mt-1">
                  <Stat label="REQUESTS" value={metrics.requests} color="#00D4FF" />
                  <Stat label="SUCCESS" value={metrics.success ?? 0} color="#00FF88" />
                  <Stat label="RATE LIMITED" value={metrics.rate_limited ?? 0} color="#FF3355" />
                  <Stat label="INVALID" value={metrics.invalid_completion_total ?? 0} color="#FF3355" />
                  <Stat
                    label="AVG LATENCY"
                    value={`${metrics.avg_latency_ms ?? 0} ms`}
                    color="#F0B000"
                    wide
                  />
                </div>
              </>
            )}
          </Card>

          {/* Voice */}
          <Card title="VOICE">
            <Row label="STT" value={voice.stt ? "AVAILABLE" : "UNAVAILABLE"} tone={voice.stt ? "ok" : "error"} />
            <Row label="TTS" value={voice.tts ? "AVAILABLE" : "UNAVAILABLE"} tone={voice.tts ? "ok" : "error"} />
          </Card>

          {/* Safety */}
          <Card title="SAFETY">
            <Row
              label="TOOL SANDBOX"
              value={safety.enabled ? "ENABLED" : "DISABLED"}
              tone={safety.enabled ? "ok" : "error"}
            />
            <Row label="CONFIRM TIMEOUT" value={`${safety.confirm_timeout ?? 120}s`} />
            <Row label="ALWAYS CONFIRM" value={safety.always_confirm || "—"} />
            <Row label={ar ? "أدوات محمّلة" : "TOOLS LOADED"} value={health?.tools_loaded ?? 0} />
          </Card>

          {/* Workspace */}
          <Card title={ar ? "مجلد العمل" : "WORKSPACE"}>
            <p className="text-[8px] mb-2 leading-relaxed" style={{ color: "var(--text-muted)" }}>
              {ar
                ? "كل أدوات الملفات مقيّدة بهذا المجلد. أي مسار خارجه يُرفض."
                : "Every file tool is confined to this directory. Any path outside it is refused."}
            </p>
            <div className="flex gap-2">
              {/* dir="ltr": a Windows path must never be reordered by RTL. */}
              <input
                ref={inputRef}
                type="text"
                dir="ltr"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && save()}
                disabled={saving}
                spellCheck={false}
                placeholder="D:/Projects"
                className="flex-1 min-w-0 px-3 py-2 rounded-sm text-[9px] outline-none transition-all"
                style={{
                  background: "rgba(0, 212, 255, 0.04)",
                  border: `1px solid ${dirty ? "rgba(240,176,0,0.3)" : "rgba(0, 212, 255, 0.12)"}`,
                  color: "var(--text-primary)",
                  caretColor: "#00D4FF",
                }}
              />
              <button
                onClick={save}
                disabled={saving || !dirty}
                className="px-3 py-2 rounded-sm text-[9px] font-bold tracking-[0.15em] transition-all disabled:opacity-30"
                style={{
                  background: "rgba(0, 212, 255, 0.1)",
                  color: "#00D4FF",
                  border: "1px solid rgba(0, 212, 255, 0.25)",
                }}
              >
                {saving ? "…" : ar ? "حفظ" : "SET"}
              </button>
            </div>
            {workspace?.enforced && (
              <div className="mt-2">
                <Row
                  label={ar ? "التقيد" : "Enforced"}
                  value={workspace.available ? (ar ? "مفعّل" : "yes") : (ar ? "غير متاح" : "unavailable")}
                  tone={workspace.available ? "ok" : "error"}
                />
              </div>
            )}
            {Array.isArray(workspace?.entries) && workspace.entries.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1">
                {workspace.entries.slice(0, 12).map((entry) => (
                  <span
                    key={entry}
                    className="text-[8px] px-1.5 py-0.5 rounded-sm"
                    style={{ background: "rgba(0,212,255,0.05)", color: "var(--text-secondary)" }}
                  >
                    {entry}
                  </span>
                ))}
              </div>
            )}
          </Card>

          {notice && <Banner tone={notice.ok ? "ok" : "error"} text={notice.text} />}
        </div>

        <div className="flex items-center justify-end gap-3 pt-4">
          <button
            onClick={onClose}
            className="text-[9px] px-4 py-2 rounded-sm font-bold tracking-[0.15em] transition-all"
            style={{
              background: "rgba(255, 51, 85, 0.08)",
              color: "#FF5A75",
              border: "1px solid rgba(255, 51, 85, 0.35)",
            }}
          >
            {ar ? "إغلاق" : "CLOSE"}
          </button>
        </div>
      </div>
    </div>
  );
}

function Card({ title, children }) {
  return (
    <div className="glass-light rounded-md p-3">
      <div className="text-[7px] font-display tracking-[0.2em] mb-3" style={{ color: "var(--text-muted)" }}>
        {title}
      </div>
      <div className="space-y-1">{children}</div>
    </div>
  );
}

function Row({ label, value, tone = "default", title }) {
  const color =
    tone === "ok" ? "#00FF88" : tone === "warn" ? "#F0B000" : tone === "error" ? "#FF3355" : "#00D4FF";
  return (
    <div className="flex items-start justify-between gap-2" title={title}>
      <span className="text-[8px] font-mono flex-none" style={{ color: "var(--text-muted)" }}>{label}</span>
      <span className="text-[9px] font-mono text-right break-all min-w-0" style={{ color }}>{value}</span>
    </div>
  );
}

function Stat({ label, value, color, wide }) {
  return (
    <div className={wide ? "col-span-2" : ""}>
      <span className="text-[7px] font-mono tracking-wider" style={{ color: "var(--text-muted)" }}>{label}</span>
      <div className="text-[11px] font-mono font-bold" style={{ color }}>{value}</div>
    </div>
  );
}

function Banner({ tone, text }) {
  const color = tone === "ok" ? "#00FF88" : "#FF5A75";
  return (
    <div
      className="text-[9px] p-2 rounded-sm font-mono break-words"
      style={{
        background: tone === "ok" ? "rgba(0,255,136,0.06)" : "rgba(255,51,85,0.08)",
        border: `1px solid ${tone === "ok" ? "rgba(0,255,136,0.2)" : "rgba(255,51,85,0.25)"}`,
        color,
      }}
    >
      {text}
    </div>
  );
}

export default memo(SettingsPanel);
