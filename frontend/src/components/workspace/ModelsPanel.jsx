import { memo, useCallback, useEffect, useMemo, useState } from "react";
import * as api from "../../lib/api";

/** Human names for the tier dropdowns; unknown tiers fall back to their id. */
const TIER_LABELS = {
  fast: { en: "Fast chat", ar: "سريع" },
  coding: { en: "Coding", ar: "برمجة" },
  reasoning: { en: "Reasoning", ar: "استدلال" },
  default: { en: "Default", ar: "افتراضي" },
};

/**
 * Models: providers, their models, capabilities and routing.
 *
 * The panel exists to answer one question that has to be answerable: **why did
 * it use that model?** So the routing preview is shown, with every rejected
 * candidate and the reason — not just the winner. A model selector that only
 * shows the choice is a black box the user can only take on trust.
 *
 * Tier pins (`TIER ASSIGNMENTS`) are the same answer one level down: the
 * dropdown shows what the router *must* use per tier, and "auto" — the
 * default — keeps saying "the router decides". Pull and probe are explicit
 * actions with their own states: nothing here downloads on its own, and a
 * probe reports what it measured rather than what a catalogue claims.
 *
 * Nothing here can display a credential. The backend has no route that returns
 * one; each provider carries `has_api_key` and nothing else, and the form never
 * reads a stored key back — it can only set one.
 */
function ModelsPanel({ language = "en" }) {
  const [providers, setProviders] = useState([]);
  const [models, setModels] = useState([]);
  const [preview, setPreview] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  // Tier pins: `null` = not loaded yet, `false` = this backend has no such
  // route. An unpinned router is not an error — the section simply hides.
  const [tierInfo, setTierInfo] = useState(null);
  // The one pull being watched; `null` when nothing is being pulled.
  const [pull, setPull] = useState(null);
  const [pullInput, setPullInput] = useState("");
  const [form, setForm] = useState({
    name: "",
    kind: "openai_compatible",
    base_url: "http://localhost:11434",
    requires_key: false,
    api_key: "",
  });

  const ar = language === "ar";

  const load = useCallback(async () => {
    const [p, m, t] = await Promise.all([
      api.getProviders(),
      api.getModels({ includeDisabled: true }),
      api.getTierAssignments(),
    ]);
    if (p.error) setError(p.error);
    else setError(null);
    setProviders(p.providers || []);
    setModels(m.models || []);
    setTierInfo(
      t.available && t.available.length
        ? { available: t.available, tiers: t.tiers || {} }
        : false,
    );
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Poll the running pull one second apart until it reaches a terminal
  // state. The effect re-arms on each new record, stops itself the moment
  // the record says `done` or `failed`, and cancels its timer on unmount —
  // so no timer outlives the panel.
  useEffect(() => {
    if (!pull || pull.state !== "running") return undefined;
    let cancelled = false;
    const timer = setTimeout(async () => {
      const status = await api.getPullStatus(pull.model);
      if (cancelled) return;
      if (status.error) {
        setError(status.error);
        return;
      }
      setPull(status);
    }, 1000);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [pull]);

  const refreshPreview = useCallback(async () => {
    const result = await api.getRoutingPreview({ tier: "fast" });
    setPreview(result);
  }, []);

  const addProvider = useCallback(
    async (event) => {
      event.preventDefault();
      setBusy(true);
      // The key is sent once and never read back. `api_key: ""` when blank is
      // deliberately omitted rather than sent as empty, because the backend
      // treats an empty string as "leave the stored key alone".
      const payload = { ...form };
      if (!payload.api_key) delete payload.api_key;
      const result = await api.saveProvider(payload);
      setBusy(false);
      if (result.error) {
        setError(result.error);
        return;
      }
      setError(null);
      setForm({ name: "", kind: "openai_compatible", base_url: "http://localhost:11434", requires_key: false, api_key: "" });
      await load();
    },
    [form, load],
  );

  const test = useCallback(
    async (id) => {
      const result = await api.testProvider(id);
      // A failed probe is a result, not an error: the endpoint being down is
      // the answer, and the reason text is what the user needs.
      setError(result.connected ? null : result.error || "Could not reach the endpoint.");
    },
    [],
  );

  const discover = useCallback(
    async (id) => {
      setBusy(true);
      const result = await api.refreshModels(id);
      setBusy(false);
      if (result.error) setError(result.error);
      await load();
      await refreshPreview();
    },
    [load, refreshPreview],
  );

  const remove = useCallback(
    async (id) => {
      const result = await api.deleteProvider(id);
      if (result.error) setError(result.error);
      await load();
    },
    [load],
  );

  const setEnabled = useCallback(
    async (providerId, modelId, enabled) => {
      const result = await api.saveModel({
        provider_id: providerId,
        id: modelId,
        enabled,
        capabilities: (models.find((m) => m.id === modelId)?.capabilities) || [],
      });
      if (result.error) setError(result.error);
      await load();
    },
    [models, load],
  );

  const assignTier = useCallback(
    async (tier, value) => {
      setBusy(true);
      // `""` clears the pin back to "auto"; `"provider|model"` sets it.
      const [providerId, modelId] = value ? value.split("|") : [null, null];
      const result = await api.setTierAssignment(tier, providerId, modelId);
      setBusy(false);
      if (result.error) {
        setError(result.error);
        return;
      }
      setError(null);
      await load();
      await refreshPreview();
    },
    [load, refreshPreview],
  );

  const probe = useCallback(
    async (providerId, modelId) => {
      setBusy(true);
      const result = await api.probeModel(modelId, providerId);
      setBusy(false);
      if (result.error) {
        setError(result.error);
        return;
      }
      setError(null);
      await load();
      await refreshPreview();
    },
    [load, refreshPreview],
  );

  const startPull = useCallback(async () => {
    const model = pullInput.trim();
    if (!model) return;
    setBusy(true);
    const result = await api.pullModel(model);
    setBusy(false);
    if (result.error) {
      setError(result.error);
      return;
    }
    setError(null);
    // Seed the record; the polling effect above takes it from here.
    setPull({
      model: result.model,
      state: result.state || "running",
      status: "starting",
      total: 0,
      completed: 0,
    });
  }, [pullInput]);

  const byProvider = useMemo(() => {
    const grouped = new Map();
    for (const model of models) {
      if (!grouped.has(model.provider_id)) grouped.set(model.provider_id, []);
      grouped.get(model.provider_id).push(model);
    }
    return grouped;
  }, [models]);

  return (
    <div className="hud-panel flex-1 flex flex-col min-h-0 overflow-y-auto">
      <header className="hud-panel-head">
        <span>{ar ? "النماذج" : "MODELS"}</span>
        <button onClick={refreshPreview} className="hud-micro ml-auto">
          {ar ? "التوجيه" : "ROUTING"}
        </button>
      </header>

      {error && (
        <p role="alert" style={{ padding: "var(--space-2) var(--space-3)", color: "var(--hud-red)" }}>
          {error}
        </p>
      )}

      {/* Routing, with every rejected candidate and why. */}
      {preview && (
        <div style={{ padding: "0 var(--space-3) var(--space-3)" }}>
          <p className="hud-micro" style={{ color: "var(--hud-cyan)" }}>
            {preview.model
              ? `${ar ? "سيُختار" : "would choose"}: ${preview.model}${
                  preview.source === "tier"
                    ? ar
                      ? " — مثبّت يدويًا"
                      : " — pinned by you"
                    : ""
                }`
              : preview.error}
          </p>
          {preview.forced && (
            <p className="hud-micro" style={{ color: "var(--hud-amber)" }}>
              {ar ? "مفروض يدويًا" : "forced by you — not chosen by the router"}
            </p>
          )}
          <ul style={{ listStyle: "none", margin: "var(--space-2) 0 0", padding: 0 }}>
            {(preview.candidates || []).map((c) => (
              <li
                key={`${c.provider_id}/${c.model}`}
                className="hud-micro"
                style={{ color: c.usable ? "var(--hud-green)" : "var(--hud-text-mute)" }}
              >
                <span aria-hidden="true">{c.usable ? "✓" : "·"}</span> {c.model} — {c.reason}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Tier pins: four dropdowns, "auto" first and always available. */}
      {tierInfo && (
        <section
          style={{
            borderTop: "var(--border-w) solid var(--hud-edge-faint)",
            padding: "var(--space-3)",
          }}
        >
          <h3 style={{ fontSize: "var(--font-sm)", marginBottom: "var(--space-2)" }}>
            {ar ? "تثبيت الفئات" : "TIER ASSIGNMENTS"}
          </h3>
          <p
            className="hud-micro"
            style={{ color: "var(--hud-text-mute)", marginBottom: "var(--space-2)" }}
          >
            {ar
              ? "الفئة المثبّتة تسبق ترتيب الأولوية. «تلقائي» يعني أن التوجيه يقرر."
              : "A pinned model beats priority order. “Auto” means the router decides."}
          </p>
          {tierInfo.available.map((tier) => {
            const pin = tierInfo.tiers[tier];
            const value = pin ? `${pin.provider_id}|${pin.model_id}` : "";
            return (
              <div
                key={tier}
                className="flex items-center"
                style={{ gap: "var(--space-2)", marginBottom: "var(--space-1)" }}
              >
                <label
                  className="hud-micro"
                  style={{ minWidth: "7rem", color: "var(--hud-text-dim)" }}
                  htmlFor={`tier-${tier}`}
                >
                  {(TIER_LABELS[tier] && TIER_LABELS[tier][ar ? "ar" : "en"]) || tier}
                </label>
                <select
                  id={`tier-${tier}`}
                  className="hud-input flex-1"
                  value={value}
                  disabled={busy}
                  onChange={(e) => assignTier(tier, e.target.value)}
                >
                  <option value="">{ar ? "تلقائي" : "auto"}</option>
                  {models.map((m) => (
                    <option key={`${m.provider_id}|${m.id}`} value={`${m.provider_id}|${m.id}`}>
                      {m.id} @ {m.provider_id}
                    </option>
                  ))}
                </select>
                {pin && (!pin.exists || !pin.enabled) && (
                  <span className="hud-micro" style={{ color: "var(--hud-amber)" }}>
                    {!pin.exists
                      ? ar
                        ? "غير موجود"
                        : "missing"
                      : ar
                        ? "موقوف"
                        : "disabled"}
                  </span>
                )}
              </div>
            );
          })}
        </section>
      )}

      {providers.map((provider) => (
        <section key={provider.id} style={{ borderTop: "var(--border-w) solid var(--hud-edge-faint)" }}>
          <header
            className="flex items-center"
            style={{ padding: "var(--space-3)", gap: "var(--space-2)" }}
          >
            <div className="flex-1 min-w-0">
              <p style={{ fontSize: "var(--font-sm)" }}>{provider.name}</p>
              <p className="hud-micro truncate" style={{ color: "var(--hud-text-mute)" }}>
                {provider.kind} · {provider.base_url}
                {provider.is_default && ` · ${ar ? "افتراضي" : "default"}`}
              </p>
              {provider.has_api_key && (
                <p className="hud-micro" style={{ color: "var(--hud-green)" }}>
                  {ar ? "مفتاح محفوظ" : "API key stored"}
                </p>
              )}
            </div>
            <button onClick={() => test(provider.id)} className="hud-micro" disabled={busy}>
              {ar ? "اختبار" : "TEST"}
            </button>
            <button onClick={() => discover(provider.id)} className="hud-micro" disabled={busy}>
              {ar ? "اكتشاف" : "SCAN"}
            </button>
            <button onClick={() => remove(provider.id)} className="hud-micro" style={{ color: "var(--hud-red)" }}>
              {ar ? "حذف" : "DEL"}
            </button>
          </header>

          <table className="w-full" style={{ borderCollapse: "collapse" }}>
            <caption className="sr-only">
              {ar ? `نماذج ${provider.name}` : `Models for ${provider.name}`}
            </caption>
            <thead>
              <tr>
                <th scope="col" className="hud-micro" style={{ textAlign: "start", padding: "var(--space-1) var(--space-3)" }}>
                  {ar ? "النموذج" : "MODEL"}
                </th>
                <th scope="col" className="hud-micro" style={{ textAlign: "start", padding: "var(--space-1)" }}>
                  {ar ? "القدرات" : "CAPABILITIES"}
                </th>
                <th scope="col" className="hud-micro" style={{ textAlign: "end", padding: "var(--space-1)" }}>
                  {ar ? "قياس" : "PROBE"}
                </th>
                <th scope="col" className="hud-micro" style={{ textAlign: "end", padding: "var(--space-1) var(--space-3)" }}>
                  {ar ? "مفعّل" : "ON"}
                </th>
              </tr>
            </thead>
            <tbody>
              {(byProvider.get(provider.id) || []).map((model) => (
                <tr key={model.id} style={{ borderTop: "var(--border-w) solid var(--hud-edge-faint)" }}>
                  <td className="hud-micro truncate" style={{ padding: "var(--space-2) var(--space-3)", color: "var(--hud-text-dim)" }}>
                    {model.id}
                  </td>
                  <td className="hud-micro" style={{ padding: "var(--space-2)", color: "var(--hud-text-mute)" }}>
                    {(model.capabilities || []).join(" ")}
                  </td>
                  <td style={{ padding: "var(--space-2)", textAlign: "end" }}>
                    {/* Only where probing can work: the backend refuses any
                        other kind, and a button that always 400s teaches
                        people to ignore buttons. */}
                    {provider.kind === "ollama" && (
                      <button
                        onClick={() => probe(provider.id, model.id)}
                        className="hud-micro"
                        disabled={busy}
                        aria-label={`${ar ? "قياس القدرات لـ" : "Probe capabilities of"} ${model.id}`}
                      >
                        {ar ? "قياس" : "PROBE"}
                      </button>
                    )}
                  </td>
                  <td style={{ padding: "var(--space-2) var(--space-3)", textAlign: "end" }}>
                    <input
                      type="checkbox"
                      checked={model.enabled}
                      onChange={(e) => setEnabled(provider.id, model.id, e.target.checked)}
                      aria-label={`${ar ? "مفعّل" : "Enable"} ${model.id}`}
                    />
                  </td>
                </tr>
              ))}
              {(byProvider.get(provider.id) || []).length === 0 && (
                <tr>
                  <td colSpan={4} className="hud-micro" style={{ padding: "var(--space-3)", color: "var(--hud-text-mute)" }}>
                    {ar ? "لم يُسجَّل أي نموذج — استخدم SCAN" : "No models recorded — use SCAN"}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </section>
      ))}

      {/* Pull: only where it can work, only ever on request. */}
      {providers.some((p) => p.kind === "ollama") && (
        <section
          style={{
            borderTop: "var(--border-w) solid var(--hud-edge-faint)",
            padding: "var(--space-3)",
          }}
        >
          <h3 style={{ fontSize: "var(--font-sm)", marginBottom: "var(--space-2)" }}>
            {ar ? "تنزيل نموذج" : "PULL A MODEL"}
          </h3>
          <p
            className="hud-micro"
            style={{ color: "var(--hud-text-mute)", marginBottom: "var(--space-2)" }}
          >
            {ar
              ? "التنزيل يحدث فقط عند طلبك — لا يبدأ تلقائيًا أبدًا."
              : "Downloads only when you ask — never on its own."}
          </p>
          <form
            className="flex items-center"
            style={{ gap: "var(--space-2)" }}
            onSubmit={(event) => {
              event.preventDefault();
              startPull();
            }}
          >
            <label className="sr-only" htmlFor="pull-model">
              {ar ? "اسم النموذج" : "Model name"}
            </label>
            <input
              id="pull-model"
              className="hud-input flex-1"
              placeholder="qwen3:9b"
              value={pullInput}
              onChange={(e) => setPullInput(e.target.value)}
            />
            <button
              type="submit"
              className="hud-micro"
              disabled={busy || pull?.state === "running"}
            >
              {ar ? "تنزيل" : "PULL"}
            </button>
          </form>
          {pull && (
            <p
              role="status"
              className="hud-micro"
              style={{
                marginTop: "var(--space-2)",
                color:
                  pull.state === "failed"
                    ? "var(--hud-red)"
                    : pull.state === "done"
                      ? "var(--hud-green)"
                      : "var(--hud-cyan)",
              }}
            >
              {pull.model}:{" "}
              {pull.state === "running"
                ? `${pull.status || (ar ? "جارٍ" : "running")}${
                    pull.total > 0
                      ? ` — ${Math.round((pull.completed / pull.total) * 100)}%`
                      : ""
                  }`
                : pull.state === "done"
                  ? ar
                    ? "تم"
                    : "done"
                  : `${ar ? "فشل" : "failed"}${pull.error ? ` — ${pull.error}` : ""}`}
            </p>
          )}
        </section>
      )}

      <section style={{ borderTop: "var(--border-w) solid var(--hud-edge-faint)", padding: "var(--space-3)" }}>
        <h3 style={{ fontSize: "var(--font-sm)", marginBottom: "var(--space-2)" }}>
          {ar ? "إضافة مزوّد" : "Add a provider"}
        </h3>
        <form onSubmit={addProvider} className="flex flex-col" style={{ gap: "var(--space-2)" }}>
          <label className="hud-micro" htmlFor="provider-name">
            {ar ? "الاسم" : "Name"}
          </label>
          <input
            id="provider-name"
            className="hud-input w-full"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            required
          />
          <label className="hud-micro" htmlFor="provider-kind">
            {ar ? "النوع" : "Kind"}
          </label>
          <select
            id="provider-kind"
            className="hud-input w-full"
            value={form.kind}
            onChange={(e) => setForm({ ...form, kind: e.target.value })}
          >
            <option value="ollama">ollama</option>
            <option value="openai_compatible">openai_compatible</option>
            <option value="openrouter">openrouter</option>
          </select>
          <label className="hud-micro" htmlFor="provider-url">
            {ar ? "عنوان النهاية" : "Base URL"}
          </label>
          <input
            id="provider-url"
            className="hud-input w-full"
            value={form.base_url}
            onChange={(e) => setForm({ ...form, base_url: e.target.value })}
            required
          />
          <label className="hud-micro" htmlFor="provider-key" style={{ display: "flex", gap: "var(--space-2)", alignItems: "center" }}>
            <input
              id="provider-key"
              type="checkbox"
              checked={form.requires_key}
              onChange={(e) => setForm({ ...form, requires_key: e.target.checked })}
            />
            {ar ? "يحتاج مفتاح" : "Needs an API key"}
          </label>
          {form.requires_key && (
            <>
              <label className="hud-micro" htmlFor="provider-key-value">
                {ar ? "المفتاح (لن يُعرض مرة أخرى)" : "Key (never shown again)"}
              </label>
              <input
                id="provider-key-value"
                type="password"
                autoComplete="off"
                className="hud-input w-full"
                value={form.api_key}
                onChange={(e) => setForm({ ...form, api_key: e.target.value })}
              />
            </>
          )}
          <button
            type="submit"
            className="hud-micro"
            disabled={busy}
            style={{
              marginTop: "var(--space-2)",
              padding: "var(--space-2)",
              border: "var(--border-w) solid var(--hud-edge-strong)",
              color: "var(--hud-cyan)",
            }}
          >
            {ar ? "حفظ" : "SAVE"}
          </button>
        </form>
      </section>
    </div>
  );
}

export default memo(ModelsPanel);