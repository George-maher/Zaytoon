import { memo, useCallback, useEffect, useState } from "react";
import * as api from "../../lib/api";

/**
 * The provider registry: add, verify, enable, delete — and the models each
 * provider offers.
 *
 * This is the settings surface for the *new* registry (`/providers`), and it is
 * deliberately not a second copy of the legacy "AI PROVIDER" card below it,
 * which reads `/settings/ai` — a different endpoint with a different job. That
 * one answers "which provider is the legacy LLM client using right now"; this
 * one is the registry itself: every configured endpoint, whether it has ever
 * answered, and what it may be asked.
 *
 * Four properties this must keep:
 *
 * - **No credential can appear here.** The backend has no route that returns
 *   key material; each provider carries `has_api_key` and nothing else. The
 *   add-form field is write-only, is never read back, and is dropped from the
 *   DOM the moment it is submitted — `save_provider` treats an absent or empty
 *   key as "keep the stored one", so a re-save of an existing row never needs
 *   to send it. A test asserts a stray `api_key` in a payload is not rendered.
 * - **Every failure arrives verbatim.** A test-chat refusal, a policy reason, a
 *   422 from `save_provider` — all shown as the backend wrote them. The reason
 *   a model is refused *is* the instruction, and a paraphrase of it is a guess.
 * - **A refused model has no toggle, not a disabled one.** A row carrying
 *   `policy_reason` is written disabled by the store whatever `enabled` says,
 *   so offering a switch that cannot stay on would be a control that lies.
 * - **The chat test is the only completion proof.** `TEST` answers "is it
 *   reachable"; `TEST CHAT` answers "can it generate", which is the only
 *   question first-run setup is allowed to ask (see `SetupWizard`).
 *
 * Deletes are two-click inline confirms rather than `window.confirm`: the
 * native dialog does not exist in jsdom and is hostile to keyboard users —
 * the same reasoning as `ExecutionHistory`'s CLEAR.
 */

const KIND_HELP = {
  ollama: {
    en: "Local daemon — no API key. Usually http://localhost:11434.",
    ar: "عميل محلي — بدون مفتاح. عادةً http://localhost:11434.",
  },
  openrouter: {
    en: "Cloud router — an API key is required.",
    ar: "موجّه سحابي — مطلوب مفتاح API.",
  },
  openai_compatible: {
    en: "Any OpenAI-style endpoint — set the base URL, key optional.",
    ar: "أي نقطة توافق OpenAI — حدّد العنوان، والمفتاح اختياري.",
  },
};

function Badge({ children, tone = "default" }) {
  const color =
    tone === "ok"
      ? "var(--hud-green)"
      : tone === "warn"
        ? "var(--hud-amber)"
        : tone === "mute"
          ? "var(--text-muted)"
          : "var(--hud-cyan)";
  return (
    <span
      className="font-mono"
      style={{
        fontSize: "var(--font-xs)",
        letterSpacing: "0.12em",
        padding: "0 var(--space-1)",
        border: "var(--border-w) solid",
        borderColor: `color-mix(in srgb, ${color} 35%, transparent)`,
        background: `color-mix(in srgb, ${color} 6%, transparent)`,
        borderRadius: "var(--radius-sm)",
        color,
        whiteSpace: "nowrap",
      }}
    >
      {children}
    </span>
  );
}

function MiniButton({ onClick, disabled, tone = "default", type = "button", children }) {
  const color =
    tone === "danger" ? "var(--hud-red)" : tone === "ok" ? "var(--hud-green)" : "var(--hud-cyan)";
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className="font-bold tracking-[0.12em] transition-colors"
      style={{
        fontSize: "var(--font-xs)",
        fontFamily: "var(--font-mono)",
        padding: "var(--space-1) var(--space-2)",
        border: "var(--border-w) solid",
        borderColor: `color-mix(in srgb, ${color} 35%, transparent)`,
        background: `color-mix(in srgb, ${color} 6%, transparent)`,
        borderRadius: "var(--radius-sm)",
        color,
      }}
    >
      {children}
    </button>
  );
}

function Note({ tone = "mute", children }) {
  const color =
    tone === "ok" ? "var(--hud-green)" : tone === "warn" ? "var(--hud-amber)" : tone === "error" ? "var(--hud-red)" : "var(--text-muted)";
  return (
    <p
      className="font-mono break-words"
      style={{ fontSize: "var(--font-xs)", color, margin: "var(--space-1) 0" }}
    >
      {children}
    </p>
  );
}

/**
 * The verification badge, from the two stamps the backend keeps.
 *
 * `chat_verified_at` means a model actually generated a completion — the only
 * proof that completes setup. `verified_at` alone means the endpoint answered
 * a models-list probe, which says nothing about whether the key may generate.
 * No stamp at all is "unverified", never "connected".
 */
function verifyBadge(p) {
  if (p.chat_verified_at) return { text: "VERIFIED", tone: "ok" };
  if (p.verified_at) return { text: "REACHABLE", tone: "warn" };
  return { text: "UNVERIFIED", tone: "mute" };
}

/** `$prompt/$completion per token`, as the catalog reported it. */
function pricingHint(model) {
  const pricing = model?.metadata?.pricing;
  if (!pricing || typeof pricing !== "object") return null;
  const prompt = pricing.prompt ?? pricing.input;
  const completion = pricing.completion ?? pricing.output;
  if (prompt === undefined || completion === undefined) return null;
  return `$${prompt}/$${completion} per token`;
}

function ProvidersSection({ language = "en" }) {
  const ar = language === "ar";

  const [providers, setProviders] = useState([]);
  // `null` until read. A backend that predates `/setup/status` answers 404 ->
  // `{ setup_required: null }`, and `null` must render as *nothing*, because
  // "I could not read the status" is not "setup is missing".
  const [setup, setSetup] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(null); // `${action}:${providerId}` | "add"
  const [chat, setChat] = useState({}); // providerId -> test-chat result
  const [probe, setProbe] = useState({}); // providerId -> reachability result
  const [summaries, setSummaries] = useState({}); // providerId -> refresh summary
  const [models, setModels] = useState({}); // providerId -> rows | undefined
  const [expanded, setExpanded] = useState(null); // providerId | null
  const [confirmDelete, setConfirmDelete] = useState(null); // providerId | null
  const [formOpen, setFormOpen] = useState(false);
  const [form, setForm] = useState({
    name: "",
    kind: "ollama",
    base_url: "http://localhost:11434",
    requires_key: false,
    api_key: "",
  });

  const load = useCallback(async () => {
    try {
      const [p, s] = await Promise.all([api.getProviders(), api.getSetupStatus()]);
      if (p.error) {
        setError(p.error);
      } else {
        setProviders(p.providers || []);
        setError(null);
      }
      setSetup(s);
    } catch (e) {
      setError(e.message || String(e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  /**
   * Re-save an existing row with one field changed.
   *
   * The whole row goes back because `save_provider`'s defaults are not
   * "keep": an omitted `enabled` becomes `True` and an omitted `is_default`
   * becomes `False`, so a partial update would silently re-enable a disabled
   * provider and quietly strip the default flag off whichever row held it.
   */
  const resave = useCallback(
    async (p, patch) => {
      const key = `${patch.is_default ? "default" : "toggle"}:${p.id}`;
      setBusy(key);
      try {
        const result = await api.saveProvider({
          id: p.id,
          name: p.name,
          kind: p.kind,
          base_url: p.base_url,
          requires_key: p.requires_key,
          timeout_s: p.timeout_s,
          org_id: p.org_id,
          enabled: p.enabled,
          is_default: p.is_default,
          ...patch,
        });
        if (result.error) setError(result.error);
        await load();
      } catch (e) {
        setError(e.message || String(e));
      } finally {
        setBusy(null);
      }
    },
    [load],
  );

  const test = useCallback(async (p) => {
    setBusy(`test:${p.id}`);
    try {
      const result = await api.testProvider(p.id);
      setProbe((prev) => ({ ...prev, [p.id]: result }));
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setBusy(null);
    }
  }, []);

  const testChat = useCallback(
    async (p) => {
      setBusy(`chat:${p.id}`);
      try {
        const result = await api.testChatProvider(p.id);
        setChat((prev) => ({ ...prev, [p.id]: result }));
        // A pass stamps `chat_verified_at`, so the badge has to follow it.
        if (result.ok) await load();
      } catch (e) {
        setError(e.message || String(e));
      } finally {
        setBusy(null);
      }
    },
    [load],
  );

  const loadModels = useCallback(async (providerId) => {
    try {
      const result = await api.getModels({ providerId, includeDisabled: true });
      if (result.error) {
        setError(result.error);
        return;
      }
      setModels((prev) => ({ ...prev, [providerId]: result.models || [] }));
    } catch (e) {
      setError(e.message || String(e));
    }
  }, []);

  const refresh = useCallback(
    async (p) => {
      setBusy(`refresh:${p.id}`);
      try {
        const result = await api.refreshModels(p.id);
        if (result.error) {
          setError(result.error);
        } else {
          setError(null);
          setSummaries((prev) => ({ ...prev, [p.id]: result }));
          await loadModels(p.id);
        }
      } catch (e) {
        setError(e.message || String(e));
      } finally {
        setBusy(null);
      }
    },
    [loadModels],
  );

  const openModels = useCallback(
    async (p) => {
      if (expanded === p.id) {
        setExpanded(null);
        return;
      }
      setExpanded(p.id);
      if (!models[p.id]) await loadModels(p.id);
    },
    [expanded, models, loadModels],
  );

  const toggleModel = useCallback(
    async (p, model, enabled) => {
      setBusy(`model:${p.id}`);
      try {
        const result = await api.saveModel({
          provider_id: p.id,
          id: model.id,
          capabilities: model.capabilities || [],
          enabled,
        });
        if (result.error) setError(result.error);
        await loadModels(p.id);
      } catch (e) {
        setError(e.message || String(e));
      } finally {
        setBusy(null);
      }
    },
    [loadModels],
  );

  const remove = useCallback(
    async (p) => {
      if (confirmDelete !== p.id) {
        setConfirmDelete(p.id);
        return;
      }
      setConfirmDelete(null);
      setBusy(`delete:${p.id}`);
      try {
        const result = await api.deleteProvider(p.id);
        if (result.error) setError(result.error);
        await load();
      } catch (e) {
        setError(e.message || String(e));
      } finally {
        setBusy(null);
      }
    },
    [confirmDelete, load],
  );

  const addProvider = useCallback(
    async (event) => {
      event.preventDefault();
      setBusy("add");
      try {
        // The key is sent once and never read back. Omitted entirely when
        // blank, because an empty string means "leave the stored key alone"
        // and a field that could clear a key by being emptied would be a
        // footgun dressed as a convenience.
        const payload = { ...form };
        if (!payload.api_key) delete payload.api_key;
        const result = await api.saveProvider(payload);
        if (result.error) {
          setError(result.error);
        } else {
          setError(null);
          setForm({
            name: "",
            kind: "ollama",
            base_url: "http://localhost:11434",
            requires_key: false,
            api_key: "",
          });
          setFormOpen(false);
        }
        await load();
      } catch (e) {
        setError(e.message || String(e));
      } finally {
        setBusy(null);
      }
    },
    [form, load],
  );

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
        className="flex items-center"
        style={{ gap: "var(--space-2)", marginBottom: "var(--space-2)" }}
      >
        <span
          className="font-display tracking-[0.2em]"
          style={{ fontSize: "var(--font-xs)", color: "var(--text-muted)" }}
        >
          {ar ? "مزودو الذكاء الاصطناعي" : "AI PROVIDERS"}
        </span>
        <MiniButton onClick={() => setFormOpen((v) => !v)} disabled={!!busy}>
          {formOpen ? (ar ? "إلغاء" : "CANCEL") : ar ? "إضافة" : "ADD"}
        </MiniButton>
      </div>

      {/* Only `true` counts. `null` (unreadable) and `false` both render
          nothing: an unknown status is not evidence that setup is missing. */}
      {setup?.setup_required === true && (
        <Note tone="warn">
          {ar
            ? "الإعداد الأول لم يكتمل بعد — لم يجتز أي مزود اختبار محادثة حقيقي."
            : "First-run setup is not complete — no provider has passed a real chat test yet."}
        </Note>
      )}

      {error && (
        <div role="alert" style={{ margin: "var(--space-1) 0" }}>
          <Note tone="error">{error}</Note>
        </div>
      )}

      {formOpen && (
        <form
          onSubmit={addProvider}
          className="flex flex-col"
          style={{ gap: "var(--space-2)", marginBottom: "var(--space-3)" }}
        >
          <label className="font-mono" htmlFor="reg-provider-name" style={{ fontSize: "var(--font-xs)", color: "var(--text-muted)" }}>
            {ar ? "الاسم" : "NAME"}
          </label>
          <input
            id="reg-provider-name"
            className="w-full hud-input"
            dir="ltr"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            required
          />

          <label className="font-mono" htmlFor="reg-provider-kind" style={{ fontSize: "var(--font-xs)", color: "var(--text-muted)" }}>
            {ar ? "النوع" : "KIND"}
          </label>
          <select
            id="reg-provider-kind"
            className="w-full hud-input"
            dir="ltr"
            value={form.kind}
            onChange={(e) => {
              const kind = e.target.value;
              // Each kind knows its own default endpoint and whether it needs
              // a key — retyping both after every kind change is exactly the
              // sort of friction that ends with a wrong URL left in the field.
              const base =
                kind === "ollama"
                  ? "http://localhost:11434"
                  : kind === "openrouter"
                    ? "https://openrouter.ai/api/v1"
                    : form.base_url;
              setForm({
                ...form,
                kind,
                base_url: base,
                requires_key: kind === "openrouter",
              });
            }}
          >
            <option value="ollama">ollama</option>
            <option value="openrouter">openrouter</option>
            <option value="openai_compatible">openai_compatible</option>
          </select>
          <Note>{KIND_HELP[form.kind][ar ? "ar" : "en"]}</Note>

          <label className="font-mono" htmlFor="reg-provider-url" style={{ fontSize: "var(--font-xs)", color: "var(--text-muted)" }}>
            {ar ? "عنوان النهاية" : "BASE URL"}
          </label>
          <input
            id="reg-provider-url"
            className="w-full hud-input"
            dir="ltr"
            spellCheck={false}
            value={form.base_url}
            onChange={(e) => setForm({ ...form, base_url: e.target.value })}
            required
          />

          <label
            className="font-mono flex items-center"
            htmlFor="reg-provider-requires-key"
            style={{ gap: "var(--space-2)", fontSize: "var(--font-xs)", color: "var(--text-muted)" }}
          >
            <input
              id="reg-provider-requires-key"
              type="checkbox"
              checked={form.requires_key}
              onChange={(e) => setForm({ ...form, requires_key: e.target.checked })}
            />
            {ar ? "يحتاج مفتاح" : "REQUIRES A KEY"}
          </label>

          {form.requires_key && (
            <>
              <label className="font-mono" htmlFor="reg-provider-key" style={{ fontSize: "var(--font-xs)", color: "var(--text-muted)" }}>
                {ar ? "المفتاح (لا يُعرض مرة أخرى)" : "API KEY (never shown again)"}
              </label>
              <input
                id="reg-provider-key"
                type="password"
                autoComplete="off"
                className="w-full hud-input"
                dir="ltr"
                value={form.api_key}
                onChange={(e) => setForm({ ...form, api_key: e.target.value })}
              />
            </>
          )}

          <div className="flex" style={{ gap: "var(--space-2)", marginTop: "var(--space-1)" }}>
            <MiniButton type="submit" disabled={!!busy}>
              {ar ? "حفظ" : "SAVE"}
            </MiniButton>
          </div>
        </form>
      )}

      {providers.length === 0 && !error && (
        <Note>{ar ? "لا يوجد مزودون بعد" : "No providers configured yet"}</Note>
      )}

      {providers.map((p) => {
        const verify = verifyBadge(p);
        const chatResult = chat[p.id];
        const probeResult = probe[p.id];
        const summary = summaries[p.id];
        const rows = models[p.id];
        const isBusy = !!busy && busy.endsWith(`:${p.id}`);

        return (
          <section
            key={p.id}
            style={{
              borderTop: "var(--border-w) solid var(--hud-edge-faint)",
              padding: "var(--space-2) 0",
            }}
          >
            <div className="flex items-center flex-wrap" style={{ gap: "var(--space-2)" }}>
              <span style={{ fontSize: "var(--font-sm)", color: "var(--text-primary)" }}>{p.name}</span>
              {p.is_default && <Badge>{ar ? "افتراضي" : "DEFAULT"}</Badge>}
              <Badge tone={p.enabled ? "ok" : "warn"}>
                {p.enabled ? (ar ? "مفعّل" : "ENABLED") : ar ? "موقوف" : "DISABLED"}
              </Badge>
              {/* Proof of a key, never the key: the row carries `has_api_key`
                  and the API has no way to return the value itself. */}
              {p.has_api_key && <Badge tone="mute">{ar ? "مفتاح محفوظ" : "KEY SET"}</Badge>}
              <Badge tone={verify.tone}>{verify.text}</Badge>
            </div>

            <p
              className="font-mono break-all"
              dir="ltr"
              style={{ fontSize: "var(--font-xs)", color: "var(--text-muted)", margin: "var(--space-1) 0" }}
            >
              {p.kind} · {p.base_url}
            </p>

            <div className="flex flex-wrap" style={{ gap: "var(--space-1)" }}>
              {!p.is_default && (
                <MiniButton onClick={() => resave(p, { is_default: true })} disabled={isBusy}>
                  {ar ? "تعيين افتراضي" : "SET DEFAULT"}
                </MiniButton>
              )}
              <MiniButton onClick={() => resave(p, { enabled: !p.enabled })} disabled={isBusy}>
                {p.enabled ? (ar ? "إيقاف" : "DISABLE") : ar ? "تفعيل" : "ENABLE"}
              </MiniButton>
              <MiniButton onClick={() => test(p)} disabled={isBusy}>
                {ar ? "اختبار" : "TEST"}
              </MiniButton>
              <MiniButton onClick={() => testChat(p)} disabled={isBusy}>
                {ar ? "اختبار محادثة" : "TEST CHAT"}
              </MiniButton>
              <MiniButton onClick={() => refresh(p)} disabled={isBusy}>
                {ar ? "تحديث النماذج" : "REFRESH MODELS"}
              </MiniButton>
              <MiniButton onClick={() => openModels(p)} disabled={isBusy}>
                {expanded === p.id ? (ar ? "إخفاء" : "HIDE MODELS") : ar ? "النماذج" : "MODELS"}
              </MiniButton>
              {confirmDelete === p.id ? (
                <>
                  <MiniButton
                    tone="danger"
                    onClick={() => remove(p)}
                    disabled={isBusy}
                  >
                    {ar ? "تأكيد الحذف" : "CONFIRM DELETE"}
                  </MiniButton>
                  <MiniButton onClick={() => setConfirmDelete(null)} disabled={isBusy}>
                    {ar ? "إلغاء" : "CANCEL"}
                  </MiniButton>
                </>
              ) : (
                <MiniButton tone="danger" onClick={() => remove(p)} disabled={isBusy}>
                  {ar ? "حذف" : "DELETE"}
                </MiniButton>
              )}
            </div>

            {/* Reachability: a measured answer, not a claim. */}
            {probeResult && (
              <div role={probeResult.connected ? "status" : "alert"}>
                {probeResult.connected ? (
                  <Note tone="ok">
                    {ar ? "✓ متصل" : "✓ reachable"}
                    {typeof probeResult.latency_ms === "number" ? ` · ${probeResult.latency_ms}ms` : ""}
                  </Note>
                ) : (
                  <Note tone="error">{probeResult.error || (ar ? "تعذر الوصول للنهاية" : "Could not reach the endpoint")}</Note>
                )}
              </div>
            )}

            {/* Chat test: the completion proof. A 404 means this backend
                predates the route — that is "unavailable", not "broken", and
                the difference decides whether the user is told to fix
                something they cannot. */}
            {chatResult && (
              <div role={chatResult.ok ? "status" : "alert"}>
                {chatResult.ok ? (
                  <Note tone="ok">
                    {ar ? "✓" : "✓"} {chatResult.latency_ms}ms —{" "}
                    {ar ? "رد النموذج:" : "model replied:"} «{chatResult.reply}»
                  </Note>
                ) : chatResult.error === "HTTP 404" ? (
                  <Note tone="mute">
                    {ar
                      ? "اختبار المحادثة غير متاح — هذه النسخة من الخادم لا تملك هذا المسار."
                      : "Chat test unavailable — this backend has no test-chat route."}
                  </Note>
                ) : (
                  <Note tone="error">{chatResult.error}</Note>
                )}
              </div>
            )}

            {/* What the last refresh actually found, in the backend's own
                numbers — including the refusals, which are the interesting
                half of the answer. */}
            {summary && (
              <Note tone="mute">
                {ar
                  ? `اكتُشف ${summary.discovered}، سُجّل ${summary.added}، ورُفض ${summary.refused_by_policy} بسبب السياسة${summary.skipped ? `، وتخطّى ${summary.skipped}` : ""}`
                  : `discovered ${summary.discovered}, recorded ${summary.added}, ${summary.refused_by_policy} refused by policy${summary.skipped ? `, ${summary.skipped} skipped` : ""}`}
              </Note>
            )}

            {expanded === p.id && (
              <div style={{ marginTop: "var(--space-2)" }}>
                {rows === undefined && <Note>{ar ? "جارٍ التحميل…" : "LOADING…"}</Note>}
                {rows !== undefined && rows.length === 0 && (
                  <Note>
                    {ar
                      ? "لا توجد نماذج مسجّلة — استخدم «تحديث النماذج»."
                      : "No models recorded — use REFRESH MODELS."}
                  </Note>
                )}
                {(rows || []).map((m) => {
                  const hint = pricingHint(m);
                  return (
                    <div
                      key={m.id}
                      className="flex items-center flex-wrap"
                      style={{
                        gap: "var(--space-2)",
                        padding: "var(--space-1) 0",
                        borderTop: "var(--border-w) solid var(--border-color)",
                      }}
                    >
                      <span
                        className="font-mono break-all"
                        dir="ltr"
                        style={{ fontSize: "var(--font-xs)", color: "var(--text-secondary)" }}
                      >
                        {m.id}
                      </span>
                      {(m.capabilities || []).map((c) => (
                        <Badge key={c} tone="mute">
                          {c}
                        </Badge>
                      ))}
                      {m.context_length != null && (
                        <span className="font-mono" style={{ fontSize: "var(--font-xs)", color: "var(--text-muted)" }}>
                          {Number(m.context_length).toLocaleString()} ctx
                        </span>
                      )}
                      {hint && (
                        <span className="font-mono" dir="ltr" style={{ fontSize: "var(--font-xs)", color: "var(--text-muted)" }}>
                          {hint}
                        </span>
                      )}
                      {m.policy_reason ? (
                        // No toggle. The store writes a refused row disabled
                        // whatever `enabled` says, so a switch here could
                        // never stay on — the reason text is the whole answer.
                        <span
                          className="font-mono break-words"
                          style={{ fontSize: "var(--font-xs)", color: "var(--hud-amber)" }}
                        >
                          {ar ? "مرفوض بالسياسة: " : "refused by policy: "}
                          {m.policy_reason}
                        </span>
                      ) : (
                        <label
                          className="font-mono flex items-center"
                          style={{ gap: "var(--space-1)", fontSize: "var(--font-xs)", color: "var(--text-muted)", marginInlineStart: "auto" }}
                        >
                          <input
                            type="checkbox"
                            checked={!!m.enabled}
                            disabled={isBusy}
                            onChange={(e) => toggleModel(p, m, e.target.checked)}
                            aria-label={`${ar ? "مفعّل" : "Enable"} ${m.id}`}
                          />
                          {ar ? "مفعّل" : "ENABLED"}
                        </label>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </section>
        );
      })}
    </div>
  );
}

export default memo(ProvidersSection);
