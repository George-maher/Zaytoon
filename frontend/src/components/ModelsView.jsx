import { memo, useCallback, useEffect, useState } from "react";
import * as api from "../lib/api";

/**
 * ModelsView — providers, models, routing, and tier pins as a top-level page.
 *
 * Promoted from a Workspace tab to a first-class destination. The page
 * answers the question "why did it use that model?" with the routing
 * preview (every candidate and the reason each was skipped) and the tier
 * pins (what the router *must* use per tier).
 *
 * Credentials are never displayed. Each provider carries `has_api_key`
 * (a boolean) and nothing else — the backend has no route that returns a
 * key, so there is nothing here to leak.
 *
 * The page is a two-pane layout: providers + models on the left, routing
 * preview + tier pins on the right.
 */

const TIER_LABELS = {
  fast: { en: "Fast chat", ar: "سريع" },
  coding: { en: "Coding", ar: "برمجة" },
  reasoning: { en: "Reasoning", ar: "استدلال" },
  default: { en: "Default", ar: "افتراضي" },
};

function ModelsView({ language = "en" }) {
  const ar = language === "ar";
  const [providers, setProviders] = useState([]);
  const [models, setModels] = useState([]);
  const [selectedProvider, setSelectedProvider] = useState(null);
  const [preview, setPreview] = useState(null);
  const [tierInfo, setTierInfo] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [pullInput, setPullInput] = useState("");
  const [pull, setPull] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
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
    setLoading(false);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Poll a running pull.
  useEffect(() => {
    if (!pull || pull.state !== "running") return undefined;
    const timer = setTimeout(async () => {
      try {
        const status = await api.getPullStatus(pull.model);
        if (status) setPull(status);
      } catch {
        // A failed poll does not stop the pull.
      }
    }, 1000);
    return () => clearTimeout(timer);
  }, [pull]);

  const selectProvider = useCallback(async (provider) => {
    setSelectedProvider(provider);
    setBusy(true);
    try {
      const result = await api.getModels({ providerId: provider.id, includeDisabled: true });
      if (!result.error) setModels(result.models || []);
      // Load routing preview for this provider.
      const routing = await api.getRoutingPreview({ providerId: provider.id });
      setPreview(routing.error ? null : routing);
    } catch {
      setPreview(null);
    } finally {
      setBusy(false);
    }
  }, []);

  const assignTier = useCallback(
    async (tier, value) => {
      if (!value) return;
      const [providerId, modelId] = value.split("|");
      const result = await api.setTierAssignment(tier, providerId, modelId);
      if (result.error) {
        setError(result.error);
      } else {
        setError(null);
        await load();
      }
    },
    [load],
  );

  const clearTier = useCallback(
    async (tier) => {
      const result = await api.setTierAssignment(tier, null, null);
      if (result.error) {
        setError(result.error);
      } else {
        setError(null);
        await load();
      }
    },
    [load],
  );

  const startPull = useCallback(async () => {
    if (!pullInput.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const result = await api.pullModel(pullInput.trim());
      if (result.error) {
        setError(result.error);
      } else {
        setPull(result);
        setPullInput("");
      }
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setBusy(false);
    }
  }, [pullInput]);

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center" style={{ padding: "var(--space-5)" }}>
        <p className="hud-micro" style={{ color: "var(--hud-text-mute)" }}>
          {ar ? "جارٍ التحميل…" : "LOADING…"}
        </p>
      </div>
    );
  }

  const providerModels = selectedProvider
    ? models.filter((m) => m.provider_id === selectedProvider.id)
    : models;

  return (
    <div className="flex-1 min-h-0 flex" style={{ padding: "var(--space-4)", gap: "var(--space-4)" }}>
      {/* Left: providers + models */}
      <div className="flex-1 flex flex-col min-h-0" style={{ gap: "var(--space-3)" }}>
        {error && (
          <p role="alert" style={{ padding: "var(--space-2) var(--space-3)", color: "var(--hud-red)", fontSize: "var(--font-sm)" }}>
            {error}
          </p>
        )}

        {/* Providers */}
        <div className="hud-panel flex-none">
          <header className="hud-panel-head">
            <span>{ar ? "المزودون" : "PROVIDERS"}</span>
          </header>
          <div style={{ padding: "var(--space-2) var(--space-3)", display: "flex", flexDirection: "column", gap: "var(--space-1)" }}>
            {providers.map((p) => (
              <button
                key={p.id}
                type="button"
                onClick={() => selectProvider(p)}
                className="flex items-center w-full"
                style={{
                  padding: "var(--space-2)",
                  gap: "var(--space-2)",
                  background: selectedProvider?.id === p.id
                    ? "color-mix(in srgb, var(--hud-cyan) 6%, transparent)"
                    : "transparent",
                  border: "var(--border-w) solid",
                  borderColor: selectedProvider?.id === p.id
                    ? "var(--hud-edge-strong)"
                    : "var(--hud-edge-faint)",
                  cursor: "pointer",
                  textAlign: "start",
                }}
              >
                <span
                  aria-hidden="true"
                  className={
                    selectedProvider?.id === p.id
                      ? "hud-hex hud-hex--ok"
                      : "hud-hex hud-hex--hollow hud-hex--idle"
                  }
                />
                <span style={{ fontSize: "var(--font-sm)", color: "var(--hud-text)", fontFamily: "var(--font-mono)" }}>
                  {p.name || p.id}
                </span>
                <span className="hud-micro ml-auto" style={{ fontSize: "var(--font-xs)", color: "var(--hud-text-mute)" }}>
                  {p.kind}
                </span>
                {/* Credential presence — a boolean, never a key */}
                <span
                  className="hud-micro"
                  style={{
                    fontSize: "var(--font-xs)",
                    color: p.has_api_key ? "var(--hud-green)" : "var(--hud-text-mute)",
                  }}
                >
                  {p.has_api_key ? (ar ? "مفتاح ✓" : "KEY ✓") : (ar ? "بدون مفتاح" : "NO KEY")}
                </span>
              </button>
            ))}
            {providers.length === 0 && (
              <p className="hud-micro" style={{ color: "var(--hud-text-mute)", fontSize: "var(--font-xs)" }}>
                {ar ? "لا مزودون مسجلون." : "No providers registered."}
              </p>
            )}
          </div>
        </div>

        {/* Models table */}
        <div className="hud-panel flex-1 flex flex-col min-h-0">
          <header className="hud-panel-head">
            <span>{ar ? "النماذج" : "MODELS"}</span>
            {selectedProvider && (
              <button
                onClick={() => setSelectedProvider(null)}
                className="hud-micro ml-auto"
                style={{ padding: "var(--space-1) var(--space-2)", fontSize: "var(--font-xs)", cursor: "pointer", background: "transparent", border: "var(--border-w) solid var(--hud-edge-faint)", color: "var(--hud-text-dim)" }}
              >
                {ar ? "عرض الكل" : "ALL"}
              </button>
            )}
          </header>
          <div className="flex-1 overflow-y-auto" style={{ padding: "var(--space-2) var(--space-3)" }}>
            {providerModels.length > 0 ? (
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "var(--font-sm)" }}>
                <thead>
                  <tr style={{ borderBottom: "var(--border-w) solid var(--hud-edge)" }}>
                    <th style={{ textAlign: "start", padding: "var(--space-1) var(--space-2)", color: "var(--hud-text-mute)", fontSize: "var(--font-xs)", fontFamily: "var(--font-mono)", fontWeight: 400 }}>
                      {ar ? "النموذج" : "MODEL"}
                    </th>
                    <th style={{ textAlign: "start", padding: "var(--space-1) var(--space-2)", color: "var(--hud-text-mute)", fontSize: "var(--font-xs)", fontFamily: "var(--font-mono)", fontWeight: 400 }}>
                      {ar ? "القدرات" : "CAPS"}
                    </th>
                    <th style={{ textAlign: "end", padding: "var(--space-1) var(--space-2)", color: "var(--hud-text-mute)", fontSize: "var(--font-xs)", fontFamily: "var(--font-mono)", fontWeight: 400 }}>
                      {ar ? "مفعّل" : "ON"}
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {providerModels.map((m) => (
                    <tr
                      key={`${m.provider_id}|${m.id}`}
                      style={{ borderBottom: "var(--border-w) solid var(--hud-edge-faint)" }}
                    >
                      <td style={{ padding: "var(--space-2)", fontFamily: "var(--font-mono)", color: "var(--hud-text)", fontSize: "var(--font-xs)" }}>
                        {m.id}
                      </td>
                      <td style={{ padding: "var(--space-2)", fontSize: "var(--font-xs)" }}>
                        {m.capabilities?.length > 0 ? (
                          <div className="flex flex-wrap" style={{ gap: "var(--space-1)" }}>
                            {m.capabilities.map((c) => (
                              <span
                                key={c}
                                className="hud-pill"
                                style={{ fontSize: "var(--font-xs)" }}
                              >
                                {c}
                              </span>
                            ))}
                          </div>
                        ) : (
                          <span style={{ color: "var(--hud-text-mute)" }}>—</span>
                        )}
                      </td>
                      <td style={{ padding: "var(--space-2)", textAlign: "end" }}>
                        <input
                          type="checkbox"
                          checked={m.enabled}
                          onChange={async (e) => {
                            await api.saveModel({ ...m, enabled: e.target.checked });
                            load();
                          }}
                          aria-label={`${ar ? "مفعّل" : "Enable"} ${m.id}`}
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="hud-micro" style={{ color: "var(--hud-text-mute)", fontSize: "var(--font-xs)" }}>
                {ar ? "لا نماذج مسجلة." : "No models recorded."}
              </p>
            )}
          </div>
        </div>

        {/* Pull a model */}
        {providers.some((p) => p.kind === "ollama") && (
          <div className="hud-panel flex-none">
            <header className="hud-panel-head">
              <span>{ar ? "تنزيل نموذج" : "PULL A MODEL"}</span>
            </header>
            <div style={{ padding: "var(--space-3)" }}>
              <p className="hud-micro" style={{ color: "var(--hud-text-mute)", marginBottom: "var(--space-2)", fontSize: "var(--font-xs)" }}>
                {ar ? "التنزيل يحدث فقط عند طلبك — لا يبدأ تلقائيًا أبدًا." : "Downloads only when you ask — never on its own."}
              </p>
              <form
                className="flex items-center"
                style={{ gap: "var(--space-2)" }}
                onSubmit={(e) => {
                  e.preventDefault();
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
                  style={{ padding: "var(--space-2) var(--space-3)", cursor: "pointer", background: "color-mix(in srgb, var(--hud-cyan) 8%, transparent)", border: "var(--border-w) solid color-mix(in srgb, var(--hud-cyan) 25%, transparent)", color: "var(--hud-cyan)" }}
                >
                  {busy ? "…" : (ar ? "تنزيل" : "PULL")}
                </button>
              </form>
              {pull && (
                <p role="status" className="hud-micro" style={{ marginTop: "var(--space-2)", fontSize: "var(--font-xs)", color: pull.state === "done" ? "var(--hud-green)" : pull.state === "failed" ? "var(--hud-red)" : "var(--hud-amber)" }}>
                  {pull.model}: {pull.state}
                  {pull.progress ? ` — ${pull.progress}%` : ""}
                  {pull.error ? ` — ${pull.error}` : ""}
                </p>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Right: routing preview + tier pins */}
      <div className="flex-none flex flex-col min-h-0" style={{ width: "340px", gap: "var(--space-3)" }}>
        {/* Routing preview */}
        <div className="hud-panel flex-none">
          <header className="hud-panel-head">
            <span>{ar ? "معاينة التوجيه" : "ROUTING PREVIEW"}</span>
          </header>
          <div style={{ padding: "var(--space-2) var(--space-3)" }}>
            {preview ? (
              <>
                {preview.candidates?.length > 0 ? (
                  <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
                    {preview.candidates.map((c, i) => (
                      <div
                        key={c.model || i}
                        style={{
                          padding: "var(--space-2)",
                          border: "var(--border-w) solid",
                          borderColor: i === 0 ? "var(--hud-edge-strong)" : "var(--hud-edge-faint)",
                          background: i === 0 ? "color-mix(in srgb, var(--hud-cyan) 4%, transparent)" : "transparent",
                        }}
                      >
                        <div className="flex items-center" style={{ gap: "var(--space-2)" }}>
                          <span style={{ fontSize: "var(--font-sm)", fontFamily: "var(--font-mono)", color: i === 0 ? "var(--hud-cyan)" : "var(--hud-text-dim)" }}>
                            {c.model}
                          </span>
                          {i === 0 && (
                            <span className="hud-pill hud-pill--accent" style={{ fontSize: "var(--font-xs)" }}>
                              {ar ? "مختار" : "SELECTED"}
                            </span>
                          )}
                        </div>
                        {c.reason && (
                          <p className="hud-micro" style={{ fontSize: "var(--font-xs)", color: "var(--hud-text-mute)", marginTop: "var(--space-1)" }}>
                            {c.reason}
                          </p>
                        )}
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="hud-micro" style={{ color: "var(--hud-text-mute)", fontSize: "var(--font-xs)" }}>
                    {ar ? "لا مرشحين." : "No candidates."}
                  </p>
                )}
              </>
            ) : (
              <p className="hud-micro" style={{ color: "var(--hud-text-mute)", fontSize: "var(--font-xs)" }}>
                {ar ? "اختر مزودًا لرؤية التوجيه." : "Select a provider to see routing."}
              </p>
            )}
          </div>
        </div>

        {/* Tier pins */}
        {tierInfo && tierInfo !== false && (
          <div className="hud-panel flex-none">
            <header className="hud-panel-head">
              <span>{ar ? "تثبيت الفئات" : "TIER PINS"}</span>
            </header>
            <div style={{ padding: "var(--space-2) var(--space-3)", display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
              {tierInfo.available.map((tier) => {
                const pin = tierInfo.tiers[tier];
                const label = (TIER_LABELS[tier] && TIER_LABELS[tier][ar ? "ar" : "en"]) || tier;
                return (
                  <div key={tier} className="flex items-center" style={{ gap: "var(--space-2)" }}>
                    <span className="hud-micro" style={{ minWidth: "7rem", fontSize: "var(--font-xs)", color: "var(--hud-text-dim)" }}>
                      {label}
                    </span>
                    <select
                      className="hud-input flex-1"
                      value={pin ? `${pin.provider_id}|${pin.model_id}` : ""}
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
                    {pin && (
                      <button
                        onClick={() => clearTier(tier)}
                        className="hud-micro"
                        style={{ padding: "var(--space-1) var(--space-2)", fontSize: "var(--font-xs)", cursor: "pointer", background: "transparent", border: "var(--border-w) solid var(--hud-edge-faint)", color: "var(--hud-text-dim)" }}
                      >
                        {ar ? "مسح" : "CLEAR"}
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default memo(ModelsView);
