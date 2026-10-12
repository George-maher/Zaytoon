import { memo, useCallback, useEffect, useState } from "react";
import * as api from "../lib/api";

/**
 * SecurityView — the Security Workspace.
 *
 * This page is the *only* place an active assessment becomes permitted, and
 * that is the design rather than a side effect. The backend enforces scope in
 * the execution layer (`core/security/scope.py`, called from inside the tool
 * before any socket exists), and there is deliberately no tool that can reach
 * `scope.create` — a model must not be able to widen its own scope by asserting
 * something. So authorisation has to be a user action, and this is where the
 * user performs it.
 *
 * Two halves:
 *
 *   **Authorisations** — the recorded targets, what each one covers, and a
 *   revoke button. Revoking takes effect on the next probe, not on restart.
 *
 *   **Probe** — runs `security_probe` through `/computer`-style tool dispatch
 *   for a target the user names. A target that is not authorised comes back
 *   refused with the reason, which is the honest answer and is rendered as one.
 *
 * What the page does not claim
 * ----------------------------
 * The findings shown are *configuration* findings from a single non-destructive
 * request. The page says so in the panel rather than in a footnote, because a
 * scan result presented without that boundary reads as a penetration test.
 */

const OP_LABELS = {
  "http.probe": { en: "HTTP probe", ar: "فحص HTTP" },
  "http.fetch": { en: "Fetch page", ar: "جلب صفحة" },
  "port.probe": { en: "Port probe", ar: "فحص منفذ" },
  "local.scan": { en: "Local scan", ar: "فحص محلي" },
};

function SecurityView({ language = "en" }) {
  const ar = language === "ar";
  const [state, setState] = useState({ authorizations: [], operations: [], active_count: 0 });
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  const [form, setForm] = useState({ target: "", operations: ["http.probe"], ttl: "", note: "" });
  const [busy, setBusy] = useState(false);

  const [probeUrl, setProbeUrl] = useState("");
  const [probeResult, setProbeResult] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    const result = await api.getSecurityAuthorizations();
    if (result.error) {
      setError(result.error);
    } else {
      setError(null);
      setState(result);
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const authorize = useCallback(
    async (event) => {
      event.preventDefault();
      setBusy(true);
      setError(null);
      const result = await api.authorizeSecurityTarget({
        targets: [form.target],
        operations: form.operations,
        ttl_days: form.ttl === "" ? null : Number(form.ttl),
        note: form.note,
      });
      if (result.error) {
        setError(result.error);
      } else {
        setForm({ target: "", operations: ["http.probe"], ttl: "", note: "" });
        await load();
      }
      setBusy(false);
    },
    [form, load],
  );

  const revoke = useCallback(
    async (id) => {
      const result = await api.revokeSecurityAuthorization(id);
      if (result.error) setError(result.error);
      await load();
    },
    [load],
  );

  const runProbe = useCallback(async () => {
    setBusy(true);
    setError(null);
    const result = await api.runSecurityProbe(probeUrl);
    setProbeResult(result);
    setBusy(false);
  }, [probeUrl]);

  const toggleOp = useCallback((op) => {
    setForm((prev) => ({
      ...prev,
      operations: prev.operations.includes(op)
        ? prev.operations.filter((o) => o !== op)
        : [...prev.operations, op],
    }));
  }, []);

  return (
    <div
      className="flex-1 min-h-0 overflow-y-auto"
      style={{ padding: "var(--space-4)", display: "flex", flexDirection: "column", gap: "var(--space-4)" }}
    >
      <header>
        <h1 className="hud-label" style={{ fontSize: "var(--font-lg)" }}>
          {ar ? "مساحة الأمان" : "SECURITY WORKSPACE"}
        </h1>
        <p className="hud-micro" style={{ color: "var(--hud-text-mute)" }}>
          {ar
            ? "الفحص النشط يتطلب نطاقًا مُصرَّحًا به. الفرض يتم في طبقة التنفيذ، لا في تعليمات النموذج."
            : "Active testing requires an authorised scope. Enforced in the execution layer, not in model instructions."}
        </p>
      </header>

      {error && (
        <p role="alert" className="hud-panel" style={{ padding: "var(--space-3)", color: "var(--hud-red)", fontSize: "var(--font-sm)" }}>
          {error}
        </p>
      )}

      <div className="flex flex-wrap" style={{ gap: "var(--space-4)" }}>
        {/* ---- Authorise a target ---- */}
        <section className="hud-panel flex-1" style={{ minWidth: "300px" }}>
          <header className="hud-panel-head">
            <span>{ar ? "تفويض هدف" : "AUTHORISE A TARGET"}</span>
            <span className="hud-micro ml-auto">{ar ? "نشط" : "ACTIVE"}: {state.active_count ?? 0}</span>
          </header>
          <form onSubmit={authorize} style={{ padding: "var(--space-3)", display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
            <label className="hud-micro" htmlFor="sec-target">
              {ar ? "الهدف" : "Target"}
            </label>
            <input
              id="sec-target"
              className="hud-input w-full"
              dir="ltr"
              value={form.target}
              onChange={(e) => setForm({ ...form, target: e.target.value })}
              placeholder="127.0.0.1 or *.example.com or https://host/path"
              required
            />
            <p className="hud-micro" style={{ color: "var(--hud-text-mute)", fontSize: "var(--font-xs)" }}>
              {ar
                ? "نطاق كامل للمضيف مرفوض: التفويض لكل شيء ليس تفويضًا."
                : "A wildcard for every host is refused — an authorisation for everything is not one."}
            </p>

            <fieldset style={{ border: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: "var(--space-1)" }}>
              <legend className="hud-micro" style={{ padding: 0 }}>
                {ar ? "العمليات" : "OPERATIONS"}
              </legend>
              {(state.operations || []).map((op) => (
                <label key={op} className="flex items-center" style={{ gap: "var(--space-2)", fontSize: "var(--font-xs)", color: "var(--hud-text-dim)" }}>
                  <input type="checkbox" checked={form.operations.includes(op)} onChange={() => toggleOp(op)} />
                  <span className="hud-mono" dir="ltr">{op}</span>
                  <span style={{ color: "var(--hud-text-mute)" }}>
                    — {(OP_LABELS[op] || {})[ar ? "ar" : "en"] || ""}
                  </span>
                </label>
              ))}
            </fieldset>

            <div className="flex" style={{ gap: "var(--space-2)" }}>
              <div className="flex-1">
                <label className="hud-micro" htmlFor="sec-ttl">{ar ? "ينتهي بعد (أيام)" : "Expires after (days)"}</label>
                <input
                  id="sec-ttl"
                  className="hud-input w-full"
                  type="number"
                  min="0"
                  value={form.ttl}
                  onChange={(e) => setForm({ ...form, ttl: e.target.value })}
                  placeholder={ar ? "بدون انتهاء" : "no expiry"}
                />
              </div>
            </div>

            <label className="hud-micro" htmlFor="sec-note">{ar ? "ملاحظة" : "Note"}</label>
            <input
              id="sec-note"
              className="hud-input w-full"
              value={form.note}
              onChange={(e) => setForm({ ...form, note: e.target.value })}
              placeholder={ar ? "سبب التفويض / رقم المهمة" : "why, or which engagement"}
            />

            <button
              type="submit"
              disabled={busy || !form.target.trim() || form.operations.length === 0}
              className="hud-micro"
              style={{ padding: "var(--space-2) var(--space-3)", cursor: "pointer", background: "color-mix(in srgb, var(--hud-cyan) 8%, transparent)", border: "var(--border-w) solid color-mix(in srgb, var(--hud-cyan) 25%, transparent)", color: "var(--hud-cyan)" }}
            >
              {ar ? "تفويض" : "AUTHORISE"}
            </button>
          </form>
        </section>

        {/* ---- Run a probe ---- */}
        <section className="hud-panel flex-1" style={{ minWidth: "300px" }}>
          <header className="hud-panel-head">
            <span>{ar ? "فحص مُصرَّح به" : "AUTHORISED PROBE"}</span>
          </header>
          <div style={{ padding: "var(--space-3)", display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
            <p className="hud-micro" style={{ color: "var(--hud-text-mute)", fontSize: "var(--font-xs)" }}>
              {ar
                ? "طلب واحد غير مُدمِّر. لا حمولات، ولا تعطيل، ولا تخمين — كل نتيجة مصحوبة بدليلها."
                : "One non-destructive request. No payloads, no exploitation, no guessing — every finding carries its evidence."}
            </p>
            <label className="hud-micro" htmlFor="sec-url">{ar ? "الرابط" : "URL"}</label>
            <input
              id="sec-url"
              className="hud-input w-full"
              dir="ltr"
              value={probeUrl}
              onChange={(e) => setProbeUrl(e.target.value)}
              placeholder="http://127.0.0.1:8099/admin"
            />
            <button
              type="button"
              onClick={runProbe}
              disabled={busy || !probeUrl.trim()}
              className="hud-micro"
              style={{ padding: "var(--space-2) var(--space-3)", cursor: "pointer", background: "transparent", border: "var(--border-w) solid var(--hud-edge-faint)", color: "var(--hud-text-dim)" }}
            >
              {ar ? "تشغيل الفحص" : "RUN PROBE"}
            </button>

            {probeResult && (
              <div style={{ marginTop: "var(--space-2)" }}>
                {probeResult.refused ? (
                  <p role="alert" className="hud-mono" style={{ fontSize: "var(--font-xs)", color: "var(--hud-amber)", whiteSpace: "pre-wrap" }}>
                    {probeResult.message || probeResult.error}
                  </p>
                ) : probeResult.error ? (
                  <p role="alert" style={{ fontSize: "var(--font-xs)", color: "var(--hud-red)" }}>{probeResult.error}</p>
                ) : (
                  <>
                    <p className="hud-micro" style={{ color: "var(--hud-text-mute)" }}>
                      {ar ? "المعرّف" : "authorisation"}: <span className="hud-mono">{probeResult.authorization_id}</span>
                      {" · "}HTTP {probeResult.status}
                    </p>
                    {(probeResult.findings || []).map((finding) => (
                      <div
                        key={finding.id}
                        style={{ borderTop: "var(--border-w) solid var(--hud-edge-faint)", padding: "var(--space-2) 0" }}
                      >
                        <p style={{ fontSize: "var(--font-xs)" }}>
                          <span
                            className="hud-micro"
                            style={{
                              color:
                                finding.severity === "high"
                                  ? "var(--hud-red)"
                                  : finding.severity === "medium"
                                    ? "var(--hud-amber)"
                                    : "var(--hud-text-mute)",
                            }}
                          >
                            {finding.severity.toUpperCase()}
                          </span>{" "}
                          {finding.title}
                        </p>
                        <p className="hud-micro hud-mono" dir="ltr" style={{ color: "var(--hud-text-mute)", fontSize: "var(--font-xs)" }}>
                          {finding.evidence}
                        </p>
                        <p className="hud-micro" style={{ color: "var(--hud-text-dim)", fontSize: "var(--font-xs)" }}>
                          {finding.remediation}
                        </p>
                      </div>
                    ))}
                  </>
                )}
              </div>
            )}
          </div>
        </section>
      </div>

      {/* ---- Recorded authorisations ---- */}
      <section className="hud-panel">
        <header className="hud-panel-head">
          <span>{ar ? "التفويضات المسجّلة" : "RECORDED AUTHORISATIONS"}</span>
        </header>
        {loading ? (
          <p className="hud-micro" style={{ padding: "var(--space-3)", color: "var(--hud-text-mute)" }}>
            {ar ? "جارٍ التحميل…" : "LOADING…"}
          </p>
        ) : (state.authorizations || []).length === 0 ? (
          <p className="hud-micro" style={{ padding: "var(--space-3)", color: "var(--hud-text-mute)" }}>
            {ar ? "لا تفويضات. الفحص النشط مرفوض حتى تُضاف واحدة." : "None. Active testing is refused until one exists."}
          </p>
        ) : (
          <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
            {(state.authorizations || []).map((auth) => (
              <li
                key={auth.id}
                className="flex items-center flex-wrap"
                style={{ gap: "var(--space-3)", padding: "var(--space-2) var(--space-3)", borderTop: "var(--border-w) solid var(--hud-edge-faint)" }}
              >
                <span
                  aria-hidden="true"
                  className={auth.active ? "hud-hex hud-hex--ok" : "hud-hex hud-hex--hollow hud-hex--idle"}
                />
                <span className="hud-mono" dir="ltr" style={{ fontSize: "var(--font-sm)", color: "var(--hud-text)" }}>
                  {auth.target}
                </span>
                <span className="hud-micro" style={{ color: "var(--hud-text-mute)" }}>
                  {(auth.operations || []).join(" · ")}
                </span>
                {auth.note && (
                  <span className="hud-micro" style={{ color: "var(--hud-text-dim)" }}>{auth.note}</span>
                )}
                <span className="hud-micro ml-auto" style={{ color: auth.active ? "var(--hud-green)" : "var(--hud-amber)" }}>
                  {auth.revoked_at
                    ? (ar ? "ملغى" : "REVOKED")
                    : auth.expired
                      ? (ar ? "منتهٍ" : "EXPIRED")
                      : (ar ? "نشط" : "ACTIVE")}
                </span>
                {auth.active && (
                  <button
                    type="button"
                    onClick={() => revoke(auth.id)}
                    className="hud-micro"
                    style={{ padding: "var(--space-1) var(--space-2)", cursor: "pointer", background: "transparent", border: "var(--border-w) solid var(--hud-edge-faint)", color: "var(--hud-red)" }}
                  >
                    {ar ? "إلغاء" : "REVOKE"}
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

export default memo(SecurityView);
