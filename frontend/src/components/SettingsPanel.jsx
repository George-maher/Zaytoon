import { memo, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  getAiSettings,
  getWorkspace,
  healthCheck,
  setAiSettings,
  setWorkspace,
  validateAiSettings,
} from "../lib/api";
import { enterOverlay } from "../lib/motion";
import ExecutionHistory from "./ExecutionHistory";
// The provider registry (W6) lives in its own directory: it is the only card
// here that talks to /providers rather than /settings/ai.
import ProvidersSection from "./providers/ProvidersSection";

/**
 * Configuration panel.
 *
 * Four things, in priority order:
 *
 * 1. **The AI provider.** Which backend answers (Ollama locally, OpenRouter
 *    in the cloud), whether it is reachable at all, which models are actually
 *    installed, and the three configured models for the active provider. A
 *    missing local model is reported by the backend as
 *    "MODEL NOT INSTALLED ... `ollama pull <model>`" -- that string is shown
 *    verbatim, because the pull command *is* the instruction. The panel never
 *    pulls a model itself: status and validation only, never a download
 *    button, because a model download is minutes of unbounded network I/O
 *    that the user did not ask to start from a status read-out.
 * 2. **The workspace root.** Every file tool is confined to it by a
 *    `commonpath` containment check, so it decides what the assistant can
 *    touch at all. It used to be settable only by editing `backend/.env` and
 *    restarting, which made the containment boundary effectively invisible.
 * 3. **Whether the OpenRouter key is present.** If it is missing the assistant
 *    cannot answer, and until now the UI gave no hint of that.
 * 4. **The model actually in use.** The side panel used to display a
 *    hard-coded "QWEN 2.5 7B" that matched no configured model.
 *
 * Properties this must keep, all covered in `ui.test.jsx`:
 *
 * - **A failure is reported, never papered over.** An unreachable backend or a
 *   rejected path shows what happened; it does not fall back to rendering the
 *   last known-good values, which would look like a healthy system.
 * - **The workspace is edited as a draft and committed explicitly.** The path
 *   field used to POST on every keystroke, so typing "D:/" fired a
 *   `POST /settings/workspace` per character and a partially-typed path was
 *   rejected (and displayed) three times before the first valid one.
 * - **A draft in progress is never clobbered by the poll.** The 15s refresh
 *   rewrites both drafts only while they are untouched; once a field has been
 *   edited the refresh updates the *status* around it, not the text under the
 *   user's cursor.
 *
 * Added in W6: `ProvidersSection` (`./providers/ProvidersSection`) renders as
 * the first card on SYSTEM — the full `/providers` registry, above the
 * `/settings/ai` view this panel was originally built around.
 */

const AI_POLL_MS = 15000;

/** Server payload -> editable draft. `api_key` is write-only and never round-trips. */
function aiDraftFrom(payload) {
  const cfg = payload?.configured_models || {};
  return {
    base_url: payload?.provider === "ollama" ? payload?.endpoint || "" : "",
    general_model: cfg.general || "",
    coding_model: cfg.coding || "",
    fast_model: cfg.fast || "",
    primary_model: cfg.primary || "",
    reasoning_model: cfg.reasoning || "",
    fallback_model: cfg.fallback || "",
    api_key: "",
  };
}

/**
 * Merge a validation result into the displayed status.
 *
 * `/settings/ai/validate` answers with a subset of the GET payload (and with
 * models as bare names rather than objects), so only the fields it actually
 * carries are taken. An error the backend does not re-send is *not* cleared
 * by silence -- clearing it here would turn "still broken" into "fixed".
 */
function applyValidation(prev, res) {
  if (!prev) return prev;
  return {
    ...prev,
    connected: typeof res.connected === "boolean" ? res.connected : prev.connected,
    endpoint: res.endpoint || prev.endpoint,
    version: res.version || prev.version,
    error: Object.prototype.hasOwnProperty.call(res, "error") ? res.error || "" : prev.error,
    models: Array.isArray(res.models) && res.models.length ? res.models : prev.models,
  };
}

function SettingsPanel({ open, onClose, language = "en" }) {
  const ar = language === "ar";

  // SYSTEM is the default section: everything the panel existed for — provider,
  // workspace, health — stays one click away, and the history view gets a
  // home instead of being another card wedged in after SAFETY.
  const [section, setSection] = useState("system");

  const [health, setHealth] = useState(null);
  const [healthError, setHealthError] = useState(null);
  const [workspace, setWorkspaceState] = useState(null);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState(null);
  const inputRef = useRef(null);
  const scrimRef = useRef(null);
  const panelRef = useRef(null);

  // AI provider: status (from the poll) plus a draft of the editable fields.
  const [ai, setAi] = useState(null);
  const [aiError, setAiError] = useState(null);
  const [aiDraft, setAiDraft] = useState(null);
  const [aiBusy, setAiBusy] = useState(null); // "validate" | "save"
  const [aiNotice, setAiNotice] = useState(null);
  const aiTouchedRef = useRef(false);

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

  /**
   * Loaded apart from `load()` on purpose: `/settings/ai` did not exist on
   * older backends, and a 404 here must not be reported as "backend
   * unreachable" for the two endpoints that *did* answer.
   */
  const loadAi = useCallback(async () => {
    try {
      const data = await getAiSettings();
      setAi(data);
      setAiError(null);
      if (!aiTouchedRef.current) setAiDraft(aiDraftFrom(data));
    } catch (e) {
      setAi(null);
      setAiError(e?.message || String(e));
    }
  }, []);

  useEffect(() => {
    if (!open) return undefined;
    load();
    loadAi();
    const poll = setInterval(() => {
      load();
      loadAi();
    }, AI_POLL_MS);
    return () => clearInterval(poll);
  }, [open, load, loadAi]);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  // Enter motion, keyed on `open`. Focus runs in its own effect above so a
  // settings panel is typeable on the first frame — the animation is the last
  // thing that may be unfinished, never the first thing that has to happen.
  useLayoutEffect(() => {
    if (!open) return undefined;
    return enterOverlay(scrimRef.current, panelRef.current, { duration: 0.24 });
  }, [open]);

  // Clear a transient notice so it cannot linger and mislead.
  useEffect(() => {
    if (!notice) return undefined;
    const t = setTimeout(() => setNotice(null), 5000);
    return () => clearTimeout(t);
  }, [notice]);

  useEffect(() => {
    if (!aiNotice) return undefined;
    const t = setTimeout(() => setAiNotice(null), 6000);
    return () => clearTimeout(t);
  }, [aiNotice]);

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

  const onAiField = useCallback((key, value) => {
    aiTouchedRef.current = true;
    setAiDraft((prev) => ({ ...(prev || aiDraftFrom(null)), [key]: value }));
  }, []);

  const validateAi = useCallback(async () => {
    if (aiBusy) return;
    setAiBusy("validate");
    setAiNotice(null);
    const provider = ai?.provider || "ollama";
    const base_url = aiDraft?.base_url || ai?.endpoint || "";
    try {
      const res = await validateAiSettings(
        provider === "ollama" ? { provider, base_url } : { provider },
      );
      setAi((prev) => applyValidation(prev, res));
      if (res.connected) {
        const n = Array.isArray(res.models) ? res.models.length : 0;
        setAiNotice({
          ok: true,
          text: ar
            ? `متصل${res.version ? ` · ${res.version}` : ""} · ${n} نموذج`
            : `CONNECTED${res.version ? ` · ${res.version}` : ""} · ${n} models`,
        });
      } else {
        // Verbatim: the backend's reason may be the actionable instruction.
        setAiNotice({ ok: false, text: res.error || (ar ? "غير متصل" : "DISCONNECTED") });
      }
    } catch (e) {
      setAiNotice({ ok: false, text: e.message || String(e) });
    } finally {
      setAiBusy(null);
    }
  }, [aiBusy, ai, aiDraft, ar]);

  const saveAi = useCallback(async () => {
    if (aiBusy) return;
    const provider = ai?.provider || "ollama";
    const d = aiDraft || aiDraftFrom(ai);
    const payload = { provider };
    if (provider === "ollama") {
      payload.base_url = (d.base_url || "").trim();
      payload.general_model = (d.general_model || "").trim();
      payload.coding_model = (d.coding_model || "").trim();
      payload.fast_model = (d.fast_model || "").trim();
    } else {
      payload.primary_model = (d.primary_model || "").trim();
      payload.reasoning_model = (d.reasoning_model || "").trim();
      payload.fallback_model = (d.fallback_model || "").trim();
      const key = (d.api_key || "").trim();
      if (key) payload.api_key = key;
    }

    setAiBusy("save");
    setAiNotice(null);
    try {
      const result = await setAiSettings(payload);
      if (result.error) {
        // Includes the MODEL NOT INSTALLED / `ollama pull` text: shown as-is.
        setAiNotice({ ok: false, text: result.error });
      } else {
        setAiNotice({
          ok: true,
          text: ar ? "تم حفظ إعدادات المزود" : "Provider settings saved",
        });
        // Untouch the draft first: `loadAi` re-seeds it from the server only
        // while it is untouched, and the saved values are now server truth.
        aiTouchedRef.current = false;
        await loadAi();
      }
    } catch (e) {
      setAiNotice({ ok: false, text: e.message || String(e) });
    } finally {
      setAiBusy(null);
    }
  }, [aiBusy, ai, aiDraft, ar, loadAi]);

  if (!open) return null;

  const llm = health?.llm || {};
  const chain = Array.isArray(llm.chain) ? llm.chain : [];
  const metrics = llm.metrics || {};
  // Tool-call telemetry lives at the health payload's **top** level
  // (`health.metrics.tools`); `llm.metrics` has never carried it, so this
  // row read nothing no matter what the backend reported. Absent on older
  // builds — nothing renders, rather than a row of zeroes claiming a
  // measured zero.
  const toolMetrics =
    health?.metrics && typeof health.metrics.tools === "object"
      ? health.metrics.tools
      : null;
  const rejected = Array.isArray(llm.rejected_models) ? llm.rejected_models : [];
  // The probe is a *measured* answer, `configured` is a paperwork answer, and
  // this panel used to show only the paperwork: API key "present", tone green,
  // while `/health` next door reported `unreachable` — so the number the user
  // came here to read (is my model up?) was the one row that could not say.
  // Missing entirely on an older backend, which is `unknown`, never `ok`.
  const probe = llm.probe && typeof llm.probe === "object" ? llm.probe : null;
  const probeState = probe ? probe.state || "unknown" : "unknown";
  const missingModels = probe && Array.isArray(probe.missing_models) ? probe.missing_models : [];
  const voice = health?.voice || {};
  const safety = health?.safety || {};
  const dirty = draft.trim() && draft.trim() !== workspace?.root;

  const provider = ai?.provider || "ollama";
  const isOllama = provider === "ollama";
  // Three states, three words: a fetch failure is not the same as a
  // reachable-but-refusing endpoint, and neither is the same as "OK". The
  // fourth word is the honest one for "the poll has not answered yet" --
  // claiming ERROR before anything has failed would be noise.
  const aiStatus = aiError
    ? "ERROR"
    : !ai
      ? (ar ? "جارٍ الفحص…" : "CHECKING…")
      : ai.connected === true
        ? "CONNECTED"
        : ai.connected === false
          ? "DISCONNECTED"
          : "ERROR";
  const aiModels = (Array.isArray(ai?.models) ? ai.models : [])
    .map((m) => (typeof m === "string" ? { id: m, name: m } : m))
    .filter((m) => m && (m.id || m.name));
  const installedIds = aiModels.map((m) => m.id || m.name);

  /**
   * "Not installed" is a *derived* status, not a guess: it is only claimed
   * once the backend has actually reported a model list it says is complete
   * (connected, non-empty). It mirrors `OllamaClient.model_exists`, where
   * `x:latest` also satisfies a configured `x`.
   */
  const missingHint = (value) => {
    const v = (value || "").trim();
    if (!v || !isOllama || ai?.connected !== true || installedIds.length === 0) return null;
    const present = installedIds.some((id) => id === v || id.startsWith(`${v}:`));
    if (present) return null;
    return `MODEL NOT INSTALLED — ollama pull ${v}`;
  };

  const modelFields = isOllama
    ? [
        { key: "general_model", label: ar ? "عام" : "GENERAL MODEL" },
        { key: "coding_model", label: ar ? "برمجة" : "CODING MODEL" },
        { key: "fast_model", label: ar ? "سريع" : "FAST MODEL" },
      ]
    : [
        { key: "primary_model", label: ar ? "أساسي" : "PRIMARY MODEL" },
        { key: "reasoning_model", label: ar ? "الاستدلال" : "REASONING MODEL" },
        { key: "fallback_model", label: ar ? "احتياطي" : "FALLBACK MODEL" },
      ];

  return (
    <div
      ref={scrimRef}
      className="fixed inset-0 z-50 flex items-center justify-center"
      style={{ padding: "var(--space-5)", background: "var(--scrim)", backdropFilter: "blur(6px)" }}
      role="dialog"
      aria-modal="true"
      aria-label={ar ? "الإعدادات" : "Settings"}
    >
      <div
        ref={panelRef}
        className="w-full max-w-lg max-h-[85vh] overflow-y-auto font-mono"
        style={{
          background: "var(--surface-2)",
          border: "var(--border-w) solid color-mix(in srgb, var(--hud-cyan) 20%, transparent)",
          borderRadius: "var(--radius-sm)",
          boxShadow: "0 0 30px color-mix(in srgb, var(--hud-cyan) 5%, transparent)",
          padding: "var(--space-5)",
        }}
      >
        <div className="flex items-center" style={{ gap: "var(--space-2)", marginBottom: "var(--space-3)" }}>
          {/* Shared LED rather than a bespoke span: `pulse-ring` was an
              animation name nothing defined, so the dot never pulsed — and
              `.hud-led--pulse` is already frozen under reduced motion. */}
          <span
            className="hud-led hud-led--pulse"
            style={{ "--state-color": healthError ? "var(--hud-amber)" : "var(--hud-cyan)" }}
            aria-hidden="true"
          />
          <h2 className="font-bold tracking-[0.25em]" style={{ fontSize: "var(--font-md)", color: "var(--hud-cyan)" }}>
            {ar ? "الإعدادات" : "SETTINGS"}
          </h2>
          <p className="hud-micro" style={{ fontSize: "var(--font-sm)", color: "var(--hud-text-mute)", marginTop: "var(--space-1)" }}>
            {ar ? "ضبط التفضيلات وتكوين المزودين" : "Configure AI provider and workspace"}
          </p>
        </div>

        <div
          role="tablist"
          aria-label={ar ? "أقسام الإعدادات" : "Settings sections"}
          className="flex"
          style={{ gap: "var(--space-2)", marginBottom: "var(--space-3)" }}
        >
          {[
            { id: "system", label: ar ? "النظام" : "SYSTEM" },
            { id: "history", label: ar ? "سجل التنفيذ" : "EXECUTION HISTORY" },
          ].map((t) => (
            <button
              key={t.id}
              type="button"
              role="tab"
              aria-selected={section === t.id}
              onClick={() => setSection(t.id)}
              className="font-bold tracking-[0.15em] transition-colors"
              style={{
                padding: "var(--space-2) var(--space-3)",
                fontSize: "var(--font-xs)",
                fontFamily: "var(--font-mono)",
                color: section === t.id ? "var(--hud-cyan)" : "var(--text-muted)",
                border: "var(--border-w) solid",
                borderColor: section === t.id ? "var(--border-strong)" : "var(--border-color)",
                background: section === t.id
                  ? "color-mix(in srgb, var(--hud-cyan) 8%, transparent)"
                  : "transparent",
                borderRadius: "var(--radius-sm)",
              }}
            >
              {t.label}
            </button>
          ))}
        </div>

        <div style={{ display: "grid", gap: "var(--space-4)" }}>
          {healthError && (
            <Banner
              tone="error"
              text={`${ar ? "تعذر الاتصال بالخدمة" : "Backend unreachable"}: ${healthError}`}
            />
          )}

          {section === "system" && (
          <>
          {/* The provider registry sits above everything else on SYSTEM: it
              is the answer to "what can this thing talk to at all", and the
              card below it only reports the one provider the legacy client
              happens to be using. */}
          <ProvidersSection language={language} />

          {/* AI provider */}
          <Card title={ar ? "مزود الذكاء الاصطناعي" : "AI PROVIDER"}>
            {aiError && (
              <div style={{ marginBottom: "var(--space-2)" }}>
                <Banner tone="error" text={`${ar ? "تعذر جلب إعدادات المزود" : "AI settings unreachable"}: ${aiError}`} />
              </div>
            )}

            <Row label={ar ? "المزود" : "PROVIDER"} value={provider.toUpperCase()} tone="ok" />
            <Row
              label={ar ? "الاتصال" : "STATUS"}
              value={aiStatus}
              tone={aiStatus === "CONNECTED" ? "ok" : "error"}
              title={ai?.error || aiError || undefined}
            />
            <Row label={ar ? "الإصدار" : "VERSION"} value={ai?.version || "—"} />
            <Row
              label={ar ? "النقطة" : "ENDPOINT"}
              value={ai?.endpoint || "—"}
              title={ai?.endpoint || undefined}
            />

            {/*
              The backend's own message, unedited. When a configured model is
              missing it arrives here as an `ollama pull <model>` instruction;
              rewriting it would throw away the one line that fixes things.
            */}
            {ai?.error && (
              <div style={{ marginTop: "var(--space-2)" }}>
                <Banner tone="error" text={ai.error} />
              </div>
            )}

            {/* Installed models, as reported. Never a download button. */}
            <div style={{ marginTop: "var(--space-3)" }}>
              <span
                className="font-mono"
                style={{ fontSize: "var(--font-xs)", letterSpacing: "0.14em", color: "var(--text-muted)" }}
              >
                {ar ? "النماذج المتاحة" : "INSTALLED MODELS"}
              </span>
              <div className="flex flex-wrap" style={{ gap: "var(--space-1)", marginTop: "var(--space-1)" }}>
                {aiModels.length > 0 ? (
                  aiModels.slice(0, 20).map((m) => (
                    <span
                      key={m.id || m.name}
                      title={Array.isArray(m.capabilities) && m.capabilities.length
                        ? m.capabilities.join(", ")
                        : undefined}
                      className="font-mono"
                      style={{
                        fontSize: "var(--font-xs)",
                        padding: "var(--space-1) var(--space-2)",
                        background: "color-mix(in srgb, var(--hud-cyan) 5%, transparent)",
                        border: "var(--border-w) solid color-mix(in srgb, var(--hud-cyan) 12%, transparent)",
                        borderRadius: "var(--radius-sm)",
                        color: "var(--text-secondary)",
                      }}
                    >
                      {m.id || m.name}
                    </span>
                  ))
                ) : (
                  <span className="font-mono" style={{ fontSize: "var(--font-xs)", color: "var(--text-muted)" }}>
                    —
                  </span>
                )}
              </div>
            </div>

            {/* Configured models for the active provider, as a draft. */}
            <div style={{ marginTop: "var(--space-3)", paddingTop: "var(--space-3)", borderTop: "var(--border-w) solid var(--border-color)" }}>
              <span
                className="font-mono"
                style={{ fontSize: "var(--font-xs)", letterSpacing: "0.14em", color: "var(--text-muted)" }}
              >
                {ar ? "النماذج المحددة" : "CONFIGURED"}
              </span>

              {modelFields.map((f) => (
                <Field
                  key={f.key}
                  id={`ai-${f.key}`}
                  label={f.label}
                  value={aiDraft?.[f.key] ?? ""}
                  onChange={(v) => onAiField(f.key, v)}
                  hint={missingHint(aiDraft?.[f.key])}
                  disabled={aiBusy === "save"}
                />
              ))}

              {/* The endpoint is editable for Ollama (it *is* OLLAMA_BASE_URL).
                  For OpenRouter the GET payload returns the full
                  `/chat/completions` URL, so writing it back as a base URL
                  would corrupt the config -- there it is display-only. */}
              {isOllama ? (
                <Field
                  id="ai-base-url"
                  label={ar ? "عنوان الخادم" : "BASE URL"}
                  value={aiDraft?.base_url ?? ""}
                  onChange={(v) => onAiField("base_url", v)}
                  disabled={aiBusy === "save"}
                />
              ) : (
                <Field
                  id="ai-api-key"
                  label={ar ? "المفتاح" : "API KEY (OPTIONAL)"}
                  type="password"
                  autoComplete="off"
                  placeholder={ar ? "اتركه فارغاً لعدم التغيير" : "leave blank to keep"}
                  value={aiDraft?.api_key ?? ""}
                  onChange={(v) => onAiField("api_key", v)}
                  disabled={aiBusy === "save"}
                />
              )}
            </div>

            <div className="flex items-center" style={{ gap: "var(--space-2)", marginTop: "var(--space-3)" }}>
              <button
                type="button"
                onClick={validateAi}
                disabled={!!aiBusy}
                className="font-bold tracking-[0.15em] transition-all disabled:opacity-30"
                style={{
                  padding: "var(--space-2) var(--space-4)",
                  fontSize: "var(--font-xs)",
                  background: "color-mix(in srgb, var(--hud-cyan) 6%, transparent)",
                  color: "var(--hud-cyan)",
                  border: "var(--border-w) solid color-mix(in srgb, var(--hud-cyan) 25%, transparent)",
                  borderRadius: "var(--radius-sm)",
                }}
              >
                {aiBusy === "validate" ? "…" : ar ? "تحقق" : "VALIDATE"}
              </button>
              <button
                type="button"
                onClick={saveAi}
                disabled={!!aiBusy || !ai}
                className="font-bold tracking-[0.15em] transition-all disabled:opacity-30"
                style={{
                  padding: "var(--space-2) var(--space-4)",
                  fontSize: "var(--font-xs)",
                  background: "color-mix(in srgb, var(--hud-green) 8%, transparent)",
                  color: "var(--hud-green)",
                  border: "var(--border-w) solid color-mix(in srgb, var(--hud-green) 30%, transparent)",
                  borderRadius: "var(--radius-sm)",
                }}
              >
                {aiBusy === "save" ? "…" : ar ? "حفظ الإعدادات" : "SAVE"}
              </button>
              {aiBusy && (
                <span className="font-mono" style={{ fontSize: "var(--font-xs)", color: "var(--text-muted)" }}>
                  {ar ? "جارٍ التنفيذ…" : "WORKING…"}
                </span>
              )}
            </div>

            {aiNotice && (
              <div style={{ marginTop: "var(--space-2)" }}>
                <Banner tone={aiNotice.ok ? "ok" : "error"} text={aiNotice.text} />
              </div>
            )}
          </Card>

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
              label={ar ? "متاح الآن" : "Reachable now"}
              value={
                probeState === "reachable"
                  ? `YES${typeof probe?.latency_ms === "number" ? ` · ${probe.latency_ms} ms` : ""}${
                      probe?.cached ? (ar ? " · مخزّن" : " · cached") : ""
                    }`
                  : probeState === "never_probed"
                  ? ar
                    ? "لم يُقَس بعد"
                    : "not measured yet"
                  : probeState === "unknown"
                  ? ar
                    ? "غير معروف"
                    : "unknown"
                  : (probe?.error || "unreachable").toString().slice(0, 90)
              }
              tone={
                probeState === "reachable"
                  ? "ok"
                  : probeState === "never_probed" || probeState === "unknown"
                  ? "default"
                  : "error"
              }
              title={
                probe?.checked_at
                  ? `${ar ? "آخر قياس" : "last measured"}: ${probe.checked_at}`
                  : undefined
              }
            />
            {missingModels.length > 0 && (
              <Row
                label={ar ? "نماذج ناقصة" : "Models missing"}
                value={missingModels.join(", ")}
                title={
                  ar
                    ? "المزود يعمل لكن هذه النماذج غير مسحوبة — اسحبها من لوحة النماذج"
                    : "the endpoint answers, these models are not pulled"
                }
                tone="error"
              />
            )}
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
              <div style={{ marginTop: "var(--space-2)" }}>
                <Banner
                  tone="error"
                  text={`${ar ? "نماذج مرفوضة" : "Rejected models"}: ${rejected.join(", ")}`}
                />
              </div>
            )}

            {typeof metrics.requests === "number" && (
              <>
                <div
                  style={{
                    marginTop: "var(--space-2)",
                    paddingTop: "var(--space-2)",
                    borderTop: "var(--border-w) solid var(--border-color)",
                  }}
                >
                  <span className="font-mono" style={{ fontSize: "var(--font-xs)", letterSpacing: "0.14em", color: "var(--text-muted)" }}>
                    METRICS
                  </span>
                </div>
                <div className="grid grid-cols-2" style={{ gap: "var(--space-2)", marginTop: "var(--space-1)" }}>
                  <Stat label="REQUESTS" value={metrics.requests} color="var(--hud-cyan)" />
                  <Stat label="SUCCESS" value={metrics.success ?? 0} color="var(--hud-green)" />
                  <Stat label="RATE LIMITED" value={metrics.rate_limited ?? 0} color="var(--hud-red)" />
                  <Stat label="INVALID" value={metrics.invalid_completion_total ?? 0} color="var(--hud-red)" />
                  <Stat
                    label="AVG LATENCY"
                    value={`${metrics.avg_latency_ms ?? 0} ms`}
                    color="var(--hud-amber)"
                    wide
                  />
                </div>
              </>
            )}

            {/*
              Task-level telemetry, separate from the request metrics above:
              how often the bounded tool loop actually ran, and how often it
              failed. Rendered only when the backend sends it.
            */}
            {toolMetrics && (
              <div style={{ marginTop: "var(--space-2)" }}>
                <Row
                  label={ar ? "استدعاءات الأدوات" : "TOOL CALLS"}
                  value={
                    ar
                      ? `${toolMetrics.calls ?? 0} استدعاء، ${toolMetrics.failed ?? 0} فشل، متوسط ${toolMetrics.avg_ms ?? toolMetrics.avg_latency_ms ?? 0}ms`
                      : `${toolMetrics.calls ?? 0} calls, ${toolMetrics.failed ?? 0} failed, avg ${toolMetrics.avg_ms ?? toolMetrics.avg_latency_ms ?? 0}ms`
                  }
                />
              </div>
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
            <p className="mb-2 leading-relaxed" style={{ fontSize: "var(--font-xs)", color: "var(--text-muted)" }}>
              {ar
                ? "كل أدوات الملفات مقيّدة بهذا المجلد. أي مسار خارجه يُرفض."
                : "Every file tool is confined to this directory. Any path outside it is refused."}
            </p>
            <div className="flex" style={{ gap: "var(--space-2)" }}>
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
                /* Everything except the unsaved-change signal comes from the
                   shared class; the amber border is this field's own state
                   and is the only thing left inline. */
                className="flex-1 min-w-0 hud-input"
                style={
                  dirty
                    ? { borderColor: "color-mix(in srgb, var(--hud-amber) 30%, transparent)" }
                    : undefined
                }
              />
              <button
                onClick={save}
                disabled={saving || !dirty}
                className="font-bold tracking-[0.15em] transition-all disabled:opacity-30"
                style={{
                  padding: "var(--space-2) var(--space-4)",
                  fontSize: "var(--font-xs)",
                  fontFamily: "var(--font-mono)",
                  background: "color-mix(in srgb, var(--hud-cyan) 10%, transparent)",
                  color: "var(--hud-cyan)",
                  border: "var(--border-w) solid color-mix(in srgb, var(--hud-cyan) 25%, transparent)",
                  borderRadius: "var(--radius-sm)",
                }}
              >
                {saving ? "…" : ar ? "حفظ" : "SET"}
              </button>
            </div>
            {workspace?.enforced && (
              <div style={{ marginTop: "var(--space-2)" }}>
                <Row
                  label={ar ? "التقيد" : "Enforced"}
                  value={workspace.available ? (ar ? "مفعّل" : "yes") : (ar ? "غير متاح" : "unavailable")}
                  tone={workspace.available ? "ok" : "error"}
                />
              </div>
            )}
            {Array.isArray(workspace?.entries) && workspace.entries.length > 0 && (
              <div className="flex flex-wrap" style={{ gap: "var(--space-1)", marginTop: "var(--space-2)" }}>
                {workspace.entries.slice(0, 12).map((entry) => (
                  <span
                    key={entry}
                    className="font-mono"
                    style={{
                      fontSize: "var(--font-xs)",
                      padding: "var(--space-1) var(--space-2)",
                      background: "color-mix(in srgb, var(--hud-cyan) 5%, transparent)",
                      color: "var(--text-secondary)",
                      borderRadius: "var(--radius-sm)",
                    }}
                  >
                    {entry}
                  </span>
                ))}
              </div>
            )}
          </Card>

          {notice && <Banner tone={notice.ok ? "ok" : "error"} text={notice.text} />}
          </>
          )}

          {section === "history" && <ExecutionHistory language={language} />}
        </div>

        <div className="flex items-center justify-end" style={{ gap: "var(--space-3)", paddingTop: "var(--space-4)" }}>
          <button
            onClick={onClose}
            className="font-bold tracking-[0.15em] transition-all"
            style={{
              fontSize: "var(--font-xs)",
              fontFamily: "var(--font-mono)",
              padding: "var(--space-2) var(--space-5)",
              background: "color-mix(in srgb, var(--hud-red) 8%, transparent)",
              color: "var(--hud-red)",
              border: "var(--border-w) solid color-mix(in srgb, var(--hud-red) 35%, transparent)",
              borderRadius: "var(--radius-sm)",
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
    <div
      className="glass-light"
      style={{
        padding: "var(--space-3)",
        borderRadius: "var(--radius-sm)",
        border: "var(--border-w) solid var(--hud-edge-faint)",
      }}
    >
      <div
        className="font-display tracking-[0.2em]"
        style={{ fontSize: "var(--font-xs)", color: "var(--text-muted)", marginBottom: "var(--space-3)" }}
      >
        {title}
      </div>
      <div style={{ display: "grid", gap: "var(--space-1)" }}>{children}</div>
    </div>
  );
}

function Row({ label, value, tone = "default", title }) {
  const color =
    tone === "ok" ? "var(--hud-green)" : tone === "warn" ? "var(--hud-amber)" : tone === "error" ? "var(--hud-red)" : "var(--hud-cyan)";
  return (
    <div className="flex items-start justify-between" style={{ gap: "var(--space-2)" }} title={title}>
      <span className="font-mono flex-none" style={{ fontSize: "var(--font-xs)", color: "var(--text-muted)" }}>{label}</span>
      <span className="font-mono text-right break-all min-w-0" style={{ fontSize: "var(--font-sm)", color }}>{value}</span>
    </div>
  );
}

/** An editable configuration value, with an optional status hint beneath it. */
function Field({ id, label, value, onChange, hint, type = "text", placeholder, disabled, autoComplete }) {
  return (
    <div style={{ marginTop: "var(--space-2)" }}>
      <label
        htmlFor={id}
        className="font-mono block"
        style={{
          fontSize: "var(--font-xs)",
          letterSpacing: "0.12em",
          textTransform: "uppercase",
          color: "var(--text-muted)",
          marginBottom: "var(--space-1)",
        }}
      >
        {label}
      </label>
      <input
        id={id}
        type={type}
        dir="ltr"
        spellCheck={false}
        autoComplete={autoComplete}
        placeholder={placeholder}
        disabled={disabled}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        /* One class instead of nine inline declarations re-stating it. The
           inline version also filled the field with a *translucent* cyan wash,
           so whatever sat behind it decided the contrast; --input-bg is opaque. */
        className="w-full hud-input"
      />
      {hint && (
        <p
          className="font-mono"
          style={{
            fontSize: "var(--font-xs)",
            color: "var(--hud-amber)",
            marginTop: "var(--space-1)",
            wordBreak: "break-word",
          }}
        >
          {hint}
        </p>
      )}
    </div>
  );
}

function Stat({ label, value, color, wide }) {
  return (
    <div className={wide ? "col-span-2" : ""}>
      <span className="font-mono tracking-wider" style={{ fontSize: "var(--font-xs)", color: "var(--text-muted)" }}>{label}</span>
      <div className="font-mono font-bold" style={{ fontSize: "var(--font-md)", color }}>{value}</div>
    </div>
  );
}

function Banner({ tone, text }) {
  const color = tone === "ok" ? "var(--hud-green)" : "var(--hud-red)";
  return (
    <div
      className="font-mono break-words"
      style={{
        fontSize: "var(--font-sm)",
        padding: "var(--space-2) var(--space-3)",
        background: tone === "ok" ? "color-mix(in srgb, var(--hud-green) 6%, transparent)" : "color-mix(in srgb, var(--hud-red) 8%, transparent)",
        border: `var(--border-w) solid ${tone === "ok" ? "color-mix(in srgb, var(--hud-green) 20%, transparent)" : "color-mix(in srgb, var(--hud-red) 25%, transparent)"}`,
        borderRadius: "var(--radius-sm)",
        color,
      }}
    >
      {text}
    </div>
  );
}

export default memo(SettingsPanel);
