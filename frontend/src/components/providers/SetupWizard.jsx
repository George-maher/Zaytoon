import { memo, useCallback, useEffect, useState } from "react";
import * as api from "../../lib/api";

/**
 * First-run setup: choose a provider, connect it, and prove it works.
 *
 * The wizard appears only when `GET /setup/status` says `setup_required:
 * true` — which the backend answers *only* from a passed chat test, never
 * from "a provider row exists". A wrong key scope, a missing model and an
 * exhausted balance each look exactly like success until a completion is
 * attempted, so the gate here is a real completion too: step 3 runs
 * `refreshModels` and then `testChatProvider`, and the wizard is not done
 * until that last call answers `ok`.
 *
 * Three decisions shape the whole thing:
 *
 * - **It renders `null` unless setup is genuinely required.** An unreadable
 *   status (`null`, the value `getSetupStatus` returns on any failure — a
 *   404 from a backend that predates the route, a down server) is not
 *   evidence that setup is missing, so it is treated as "not required". A
 *   wizard that pops up on every boot of an old backend would be a bug the
 *   user cannot fix.
 * - **Skipping is remembered per browser, not per account.** "Skip for now"
 *   writes `zaytoon.setup.dismissed` to sessionStorage — this session and
 *   tab, not forever: a setup the user skipped once is still missing, and a
 *   permanent dismiss flag would turn a real error into an invisible one.
 *   The settings page keeps the full registry either way.
 * - **Nothing here can display or echo a credential.** The API key field is
 *   write-only, is cleared from the form the moment it is sent, and the
 *   provider rows the wizard reads carry `has_api_key` and no key material.
 */

/**
 * Where "Skip for now" is remembered — sessionStorage, this tab only. Not
 * exported: a constant beside a component export trips react-refresh's
 * only-export-components rule, and the test pins the literal instead.
 */
const SETUP_DISMISSED_KEY = "zaytoon.setup.dismissed";

/** The three paths a first-run user can actually be on. */
const CHOICES = [
  {
    kind: "ollama",
    title: { en: "Local Ollama", ar: "أولاما محلي" },
    blurb: {
      en: "Free and local. No account, no key — models run on this machine.",
      ar: "مجاني ومحلي. بدون حساب أو مفتاح — النماذج تعمل على جهازك.",
    },
    base_url: "http://localhost:11434",
    requires_key: false,
  },
  {
    kind: "openrouter",
    title: { en: "OpenRouter", ar: "أوبن راوتر" },
    blurb: {
      en: "Cloud models through one key. Free-tier models only, by policy.",
      ar: "نماذج سحابية بمفتاح واحد. نماذج مجانية فقط، حسب السياسة.",
    },
    base_url: "https://openrouter.ai/api/v1",
    requires_key: true,
  },
  {
    kind: "openai_compatible",
    title: { en: "Custom endpoint", ar: "نقطة مخصّصة" },
    blurb: {
      en: "Any OpenAI-compatible server you already run.",
      ar: "أي خادم متوافق مع OpenAI تديره بنفسك.",
    },
    base_url: "",
    requires_key: false,
  },
];

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

function WizardButton({ onClick, disabled, tone = "default", type = "button", children }) {
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
        padding: "var(--space-2) var(--space-3)",
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

function WizardNote({ tone = "mute", children }) {
  const color =
    tone === "ok"
      ? "var(--hud-green)"
      : tone === "warn"
        ? "var(--hud-amber)"
        : tone === "error"
          ? "var(--hud-red)"
          : "var(--text-muted)";
  return (
    <p
      className="font-mono break-words"
      style={{ fontSize: "var(--font-xs)", color, margin: "var(--space-1) 0" }}
    >
      {children}
    </p>
  );
}

function SetupWizard({ language = "en" }) {
  const ar = language === "ar";
  // `(arabic, english)` — the argument order every call site in this file
  // uses, so a call reads in the order the string was written.
  const t = (arText, en) => (ar ? arText : en);

  // Read the dismiss flag once, up front: rendering the wizard for a frame
  // and then removing it is a flash the user experiences as a glitch.
  const [dismissed, setDismissed] = useState(() => {
    try {
      return window.sessionStorage.getItem(SETUP_DISMISSED_KEY) === "1";
    } catch {
      // A browser with storage disabled still gets the wizard; it just cannot
      // remember a skip, which fails in the user's favour.
      return false;
    }
  });
  const [status, setStatus] = useState(undefined); // undefined = still reading
  const [providers, setProviders] = useState([]);
  const [step, setStep] = useState("choose"); // choose -> connect -> verify -> done
  const [form, setForm] = useState(null); // { id?, name, kind, base_url, requires_key, api_key }
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [verify, setVerify] = useState(null); // test-chat result

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const [s, p] = await Promise.all([api.getSetupStatus(), api.getProviders()]);
        if (!alive) return;
        setStatus(s);
        setProviders(p.providers || []);
      } catch (e) {
        if (!alive) return;
        // Unreadable status is NOT "setup required": same rule as the
        // component contract — a failure reads as null, never true.
        setStatus({ setup_required: null, error: e.message || String(e) });
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  const skip = useCallback(() => {
    try {
      window.sessionStorage.setItem(SETUP_DISMISSED_KEY, "1");
    } catch {
      // Storage refused: still hide it for this mount.
    }
    setDismissed(true);
  }, []);

  /** Enter the connect step, prefilled from any provider of the same kind. */
  const choose = useCallback(
    (choice) => {
      // Kinds are exact ids in both directions: a provider row's `kind` is
      // one of the three the registry validates, and the cards below use the
      // same strings, so a looser match would only risk adopting a row that
      // is not actually the endpoint the user picked.
      const existing = providers.find((p) => p.kind === choice.kind);
      setForm({
        // An existing row of this kind is *updated*, not duplicated: the id
        // rides along so `POST /providers` upserts instead of minting a
        // second provider that half-shares its configuration.
        id: existing?.id,
        name: existing?.name || "",
        kind: choice.kind,
        base_url: existing?.base_url || choice.base_url,
        requires_key: existing ? existing.requires_key : choice.requires_key,
        api_key: "",
      });
      setError(null);
      setStep("connect");
    },
    [providers],
  );

  const save = useCallback(
    async (event) => {
      event.preventDefault();
      setBusy(true);
      setError(null);
      try {
        // The key is sent once and never read back; omitted when blank
        // because an empty string means "keep the stored key".
        const payload = { ...form };
        if (!payload.api_key) delete payload.api_key;
        const result = await api.saveProvider(payload);
        if (result.error) {
          setError(result.error);
          return;
        }
        setForm((prev) => ({ ...prev, id: result.id || form.id }));
        setStep("verify");
      } catch (e) {
        setError(e.message || String(e));
      } finally {
        setBusy(false);
      }
    },
    [form],
  );

  /**
   * The completion gate: refresh the model list, then generate for real.
   *
   * Refresh first because the chat test needs a recorded model to call, and
   * because "no models recorded" is a 409 whose fix is exactly this step —
   * showing that error after a refresh would mean the endpoint has no usable
   * model at all, which is the honest answer the user needs.
   */
  const runVerify = useCallback(async () => {
    if (!form?.id) return;
    setBusy(true);
    setError(null);
    setVerify(null);
    try {
      const refreshed = await api.refreshModels(form.id);
      if (refreshed.error) {
        setError(refreshed.error);
        return;
      }
      const result = await api.testChatProvider(form.id);
      setVerify(result);
      if (result.ok) setStep("done");
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setBusy(false);
    }
  }, [form]);

  const retest = useCallback(async () => {
    if (!form?.id) return;
    setBusy(true);
    setError(null);
    try {
      const result = await api.testChatProvider(form.id);
      setVerify(result);
      if (result.ok) setStep("done");
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setBusy(false);
    }
  }, [form]);

  // Nothing to show unless setup is *provably* required and not skipped.
  if (dismissed || status?.setup_required !== true) return null;

  const stepIndex = { choose: 1, connect: 2, verify: 3, done: 3 }[step];

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center"
      style={{ padding: "var(--space-5)", background: "var(--scrim)", backdropFilter: "blur(6px)" }}
      role="dialog"
      aria-modal="true"
      aria-label={t("الإعداد الأول", "First-run setup")}
    >
      <div
        className="w-full max-w-md font-mono"
        style={{
          background: "var(--surface-2)",
          border: "var(--border-w) solid color-mix(in srgb, var(--hud-cyan) 20%, transparent)",
          borderRadius: "var(--radius-sm)",
          boxShadow: "0 0 30px color-mix(in srgb, var(--hud-cyan) 5%, transparent)",
          padding: "var(--space-5)",
          maxHeight: "85vh",
          overflowY: "auto",
        }}
      >
        <div className="flex items-center" style={{ gap: "var(--space-2)", marginBottom: "var(--space-1)" }}>
          <span className="hud-led hud-led--pulse" style={{ "--state-color": "var(--hud-cyan)" }} aria-hidden="true" />
          <h2
            className="font-bold tracking-[0.25em]"
            style={{ fontSize: "var(--font-md)", color: "var(--hud-cyan)" }}
          >
            {t("الإعداد الأول", "FIRST-RUN SETUP")}
          </h2>
        </div>
        <WizardNote>
          {t(`الخطوة ${stepIndex} من 3`, `Step ${stepIndex} of 3`)} ·{" "}
          {t("لا يمكن للمساعد أن يجيب حتى يجتز مزود اختبار محادثة حقيقيًا", "The assistant cannot answer until one provider passes a real chat test")}
        </WizardNote>

        {error && (
          <div role="alert" style={{ margin: "var(--space-2) 0" }}>
            <WizardNote tone="error">{error}</WizardNote>
          </div>
        )}

        {step === "choose" && (
          <div style={{ display: "grid", gap: "var(--space-2)", marginTop: "var(--space-3)" }}>
            {CHOICES.map((choice) => (
              <button
                key={choice.kind}
                type="button"
                onClick={() => choose(choice)}
                className="text-start"
                style={{
                  padding: "var(--space-3)",
                  border: "var(--border-w) solid var(--hud-edge-faint)",
                  borderRadius: "var(--radius-sm)",
                  background: "transparent",
                  cursor: "pointer",
                }}
              >
                <span style={{ color: "var(--hud-cyan)", fontSize: "var(--font-sm)" }}>
                  {t(choice.title.ar, choice.title.en)}
                </span>
                <WizardNote>{t(choice.blurb.ar, choice.blurb.en)}</WizardNote>
              </button>
            ))}
          </div>
        )}

        {step === "connect" && form && (
          <form onSubmit={save} style={{ display: "grid", gap: "var(--space-2)", marginTop: "var(--space-3)" }}>
            {form.id && (
              <WizardNote tone="warn">
                {t(
                  `سيتم تحديث المزود الموجود «${form.name || form.id}»، ولن يُنشئ مزودًا ثانيًا.`,
                  `This updates the existing “${form.name || form.id}” provider rather than creating a second one.`,
                )}
              </WizardNote>
            )}

            <label htmlFor="wiz-name" style={{ fontSize: "var(--font-xs)", color: "var(--text-muted)" }}>
              {t("الاسم", "NAME")}
            </label>
            <input
              id="wiz-name"
              className="w-full hud-input"
              dir="ltr"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              required
            />

            <label htmlFor="wiz-url" style={{ fontSize: "var(--font-xs)", color: "var(--text-muted)" }}>
              {t("عنوان النهاية", "BASE URL")}
            </label>
            <input
              id="wiz-url"
              className="w-full hud-input"
              dir="ltr"
              spellCheck={false}
              value={form.base_url}
              onChange={(e) => setForm({ ...form, base_url: e.target.value })}
              required
            />
            <WizardNote>{t(KIND_HELP[form.kind].ar, KIND_HELP[form.kind].en)}</WizardNote>

            {form.requires_key && (
              <>
                <label htmlFor="wiz-key" style={{ fontSize: "var(--font-xs)", color: "var(--text-muted)" }}>
                  {t("مفتاح API (لا يُعرض مرة أخرى)", "API KEY (never shown again)")}
                </label>
                <input
                  id="wiz-key"
                  type="password"
                  autoComplete="off"
                  className="w-full hud-input"
                  dir="ltr"
                  value={form.api_key}
                  onChange={(e) => setForm({ ...form, api_key: e.target.value })}
                />
              </>
            )}

            <div className="flex" style={{ gap: "var(--space-2)", marginTop: "var(--space-2)" }}>
              <WizardButton type="submit" disabled={busy}>
                {busy ? "…" : t("حفظ ومتابعة", "SAVE & CONTINUE")}
              </WizardButton>
              <WizardButton onClick={() => setStep("choose")}>
                {t("رجوع", "BACK")}
              </WizardButton>
            </div>
          </form>
        )}

        {step === "verify" && (
          <div style={{ marginTop: "var(--space-3)" }}>
            <WizardNote>
              {t(
                "سيُحدَّث قائمة النماذج ثم يُطلب من النموذج ردًّا حقيقيًا. الإتمام لا يكون إلا بعد رد فعلي.",
                "The model list is refreshed, then the model is asked for a real reply. Setup completes only on an actual response.",
              )}
            </WizardNote>
            {verify && !verify.ok && (
              <div role="alert">
                {/* Verbatim, including the "refresh first" 409 text when the
                    endpoint records nothing usable — it is the instruction. */}
                <WizardNote tone="error">{verify.error}</WizardNote>
              </div>
            )}
            {verify?.ok && (
              <WizardNote tone="ok">
                ✓ {verify.latency_ms}ms — {t("رد النموذج:", "model replied:")} «{verify.reply}»
              </WizardNote>
            )}
            <div className="flex flex-wrap" style={{ gap: "var(--space-2)", marginTop: "var(--space-2)" }}>
              {/* First press verifies (refresh + chat test). A retry after a
                  failure re-asks the model only: the catalogue is already
                  fresh, and re-running the refresh would let a refresh error
                  mask the chat answer the user is actually retrying. */}
              <WizardButton onClick={verify ? retest : runVerify} disabled={busy} tone="ok">
                {busy ? "…" : verify ? t("إعادة الاختبار", "RETEST") : t("اختبار الاتصال", "VERIFY")}
              </WizardButton>
              <WizardButton onClick={() => setStep("connect")}>
                {t("رجوع", "BACK")}
              </WizardButton>
            </div>
          </div>
        )}

        {step === "done" && (
          <div style={{ marginTop: "var(--space-3)" }}>
            <WizardNote tone="ok">
              {t("تم — تم التحقق من المزود", "Done — provider verified")}
            </WizardNote>
            {verify?.ok && (
              <WizardNote>
                {verify.provider} · {verify.model} · {verify.latency_ms}ms — «{verify.reply}»
              </WizardNote>
            )}
            <div style={{ marginTop: "var(--space-2)" }}>
              <WizardButton tone="ok" onClick={() => setDismissed(true)}>
                {t("ابدأ", "GET STARTED")}
              </WizardButton>
            </div>
          </div>
        )}

        {/* Skip is offered at every step, and it is remembered only for this
            session — the setup is still missing, and saying so on the next
            launch is the point. */}
        {step !== "done" && (
          <div className="flex items-center" style={{ gap: "var(--space-2)", marginTop: "var(--space-4)" }}>
            <WizardButton tone="danger" onClick={skip}>
              {t("تخطَّ الآن", "Skip for now")}
            </WizardButton>
            <WizardButton onClick={() => setDismissed(true)}>
              {t("إغلاق", "Close")}
            </WizardButton>
          </div>
        )}
      </div>
    </div>
  );
}

export default memo(SetupWizard);
