import { useState, useEffect, useCallback, useRef } from "react";
import { healthCheck, getWorkspace, setWorkspace } from "../lib/api";

/**
 * Configuration panel.
 *
 * This is what the `CONF` button in the bottom dock was wired to visually but
 * not functionally — `BottomDock.handleClick` had no `case "settings"`, so the
 * button did nothing at all.
 *
 * It exists for three reasons, in priority order:
 *
 * 1. **The workspace root.** Every file tool is confined to it via a
 *    `commonpath` containment check, so it decides what the assistant can
 *    touch at all. It used to be settable only by editing `backend/.env` and
 *    restarting, which made the containment boundary effectively invisible.
 * 2. **Whether the OpenRouter key is present.** If it is missing the assistant
 *    cannot answer, and until now the UI gave no hint of that.
 * 3. **The model actually in use.** The side panel used to display a
 *    hard-coded "QWEN 2.5 7B" that matched no configured model.
 */
export default function SettingsPanel({ open, onClose, language = "en" }) {
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
      setHealthError(e.message || String(e));
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

  return (
    <>
      <div className="fixed inset-0 z-40" onClick={onClose} />
      <div
        role="dialog"
        aria-modal="true"
        aria-label={ar ? "الإعدادات" : "Settings"}
        className="fixed right-3 top-12 z-50 w-96 max-h-[75vh] rounded-sm overflow-hidden flex flex-col animate-fade-in font-mono"
        style={{
          background: "rgba(5, 5, 12, 0.97)",
          border: "1px solid rgba(0, 212, 255, 0.18)",
          backdropFilter: "blur(20px)",
          boxShadow: "0 8px 40px rgba(0,0,0,0.6), 0 0 30px rgba(0,212,255,0.05)",
        }}
      >
        <Header ar={ar} onClose={onClose} />
        <div className="overflow-y-auto p-3 space-y-4" style={{ maxHeight: "calc(75vh - 40px)" }}>
          {healthError && (
            <Banner
              tone="error"
              text={`${ar ? "تعذر الاتصال بالخدمة" : "Backend unreachable"}: ${healthError}`}
            />
          )}

          <Workspace
            ar={ar}
            workspace={workspace}
            draft={draft}
            setDraft={setDraft}
            inputRef={inputRef}
            saving={saving}
            onSave={save}
          />

          {notice && (
            <Banner tone={notice.ok ? "ok" : "error"} text={notice.text} />
          )}

          <Llm ar={ar} llm={llm} chain={chain} metrics={metrics} rejected={rejected} />
          <Voice ar={ar} voice={voice} />
          <Safety ar={ar} safety={safety} health={health} />
        </div>
      </div>
    </>
  );
}

function Header({ ar, onClose }) {
  return (
    <div
      className="flex items-center justify-between px-3 py-2 border-b flex-none"
      style={{ borderColor: "rgba(0, 212, 255, 0.1)" }}
    >
      <span className="text-[10px] font-display font-bold tracking-wider" style={{ color: "#00d4ff" }}>
        {ar ? "الإعدادات" : "SETTINGS"}
      </span>
      <button
        onClick={onClose}
        className="text-[8px] px-1.5 py-0.5 rounded-sm font-mono"
        style={{ color: "var(--text-muted)" }}
      >
        {ar ? "إغلاق" : "CLOSE"}
      </button>
    </div>
  );
}

function Section({ title, children }) {
  return (
    <section>
      <h3 className="text-[8px] font-display tracking-[0.2em] mb-1.5" style={{ color: "var(--text-muted)" }}>
        {title}
      </h3>
      {children}
    </section>
  );
}

function Row({ label, value, tone = "default", title }) {
  const color =
    tone === "ok" ? "#00ff88" : tone === "warn" ? "#ffb400" : tone === "error" ? "#ff3355" : "var(--text-primary)";
  return (
    <div className="flex items-start justify-between gap-2 py-0.5" title={title}>
      <span className="text-[9px] flex-none" style={{ color: "var(--text-muted)" }}>{label}</span>
      <span className="text-[9px] text-right break-all min-w-0" style={{ color }}>{value}</span>
    </div>
  );
}

function Banner({ tone, text }) {
  const color = tone === "ok" ? "#00ff88" : "#ff5a75";
  return (
    <div
      className="text-[9px] p-2 rounded-sm break-words"
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

function Workspace({ ar, workspace, draft, setDraft, inputRef, saving, onSave }) {
  return (
    <Section title={ar ? "مجلد العمل" : "WORKSPACE"}>
      <p className="text-[8px] mb-1.5 leading-relaxed" style={{ color: "var(--text-muted)" }}>
        {ar
          ? "كل أدوات الملفات مقيّدة بهذا المجلد. أي مسار خارجه يُرفض."
          : "Every file tool is confined to this directory. Any path outside it is refused."}
      </p>
      <div className="flex gap-1">
        {/* dir="ltr": a Windows path must never be reordered by RTL. */}
        <input
          ref={inputRef}
          dir="ltr"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && onSave()}
          spellCheck={false}
          placeholder="D:/Projects"
          className="flex-1 min-w-0 px-2 py-1.5 rounded-sm text-[10px] outline-none"
          style={{
            background: "rgba(0, 212, 255, 0.04)",
            border: "1px solid rgba(0, 212, 255, 0.15)",
            color: "var(--text-primary)",
          }}
        />
        <button
          onClick={onSave}
          disabled={saving || !draft.trim() || draft.trim() === workspace?.root}
          className="px-2 py-1.5 rounded-sm text-[9px] font-bold tracking-wider disabled:opacity-30"
          style={{
            background: "rgba(0, 212, 255, 0.1)",
            color: "#00d4ff",
            border: "1px solid rgba(0, 212, 255, 0.25)",
          }}
        >
          {saving ? "…" : ar ? "حفظ" : "SET"}
        </button>
      </div>
      {workspace?.enforced && (
        <div className="mt-1">
          <Row
            label={ar ? "التقيد" : "Enforced"}
            value={ar ? "مفعّل" : "yes"}
            tone={workspace.available ? "ok" : "error"}
          />
        </div>
      )}
      {Array.isArray(workspace?.entries) && workspace.entries.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1">
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
    </Section>
  );
}

function Llm({ ar, llm, chain, metrics, rejected }) {
  return (
    <Section title="LLM">
      <Row
        label={ar ? "المزوّد" : "Provider"}
        value={llm.provider || "—"}
      />
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
        <div className="mt-1">
          <Row
            label={ar ? "سلسلة احتياطية" : "Fallback chain"}
            value={chain.map((c) => c.model).join(" → ")}
            title={chain.map((c) => c.model).join(" → ")}
          />
        </div>
      )}
      {rejected.length > 0 && (
        <div className="mt-1">
          <Banner
            tone="error"
            text={`${ar ? "نماذج مرفوضة" : "Rejected models"}: ${rejected.join(", ")}`}
          />
        </div>
      )}
      {typeof metrics.requests === "number" && (
        <>
          <div className="mt-1.5" />
          <Row label={ar ? "الطلبات" : "Requests"} value={metrics.requests} />
          <Row
            label={ar ? "معدّل النجاح" : "Success"}
            value={`${metrics.success ?? 0} (${metrics.rate_limited ?? 0} rl, ${metrics.timeouts ?? 0} to)`}
          />
          <Row
            label={ar ? "زمن الاستجابة" : "Avg latency"}
            value={`${metrics.avg_latency_ms ?? 0} ms`}
          />
        </>
      )}
    </Section>
  );
}

function Voice({ ar, voice }) {
  return (
    <Section title={ar ? "الصوت" : "VOICE"}>
      <Row
        label="STT"
        value={voice.stt ? (ar ? "متاح" : "ready") : ar ? "غير متاح" : "unavailable"}
        tone={voice.stt ? "ok" : "warn"}
      />
      <Row
        label="TTS"
        value={voice.tts ? (ar ? "متاح" : "ready") : ar ? "غير متاح" : "unavailable"}
        tone={voice.tts ? "ok" : "warn"}
      />
    </Section>
  );
}

function Safety({ ar, safety, health }) {
  return (
    <Section title={ar ? "الأمان" : "SAFETY"}>
      <Row
        label={ar ? "بوابة التأكيد" : "Gate"}
        value={safety.enabled ? (ar ? "مفعّلة" : "on") : ar ? "معطّلة" : "OFF"}
        tone={safety.enabled ? "ok" : "error"}
      />
      <Row
        label={ar ? "مهلة التأكيد" : "Timeout"}
        value={`${safety.confirm_timeout ?? 0}s`}
      />
      <Row
        label={ar ? "أدوات محمّلة" : "Tools"}
        value={health?.tools_loaded ?? 0}
      />
    </Section>
  );
}
