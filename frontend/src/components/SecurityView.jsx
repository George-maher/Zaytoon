import { memo, useCallback, useEffect, useMemo, useState } from "react";
import * as api from "../lib/api";
import { translate } from "../i18n/strings";

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
 * Three halves:
 *
 *   **Authorisations** — the recorded targets, what each one covers, and a
 *   revoke button. Revoking takes effect on the next probe, not on restart.
 *
 *   **Capabilities** — the permission grants. This is not a display of the
 *   vocabulary: `deny` is checked at the tool gate *before* the approval
 *   prompt, so a switch here changes what the product will do. `allow` is the
 *   opposite case and the UI says so rather than implying it — it is recorded
 *   and shown, and a confirmation-tier action still asks, because a stored
 *   preference must never become a silent bypass of the prompt.
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

//: The three decisions, in the order a user reads them: the default first,
//: then the two that mean something. Deliberately not a two-state toggle —
//: "allow" and "ask" are different answers and collapsing them would hide the
//: distinction the backend makes.
const DECISIONS = ["ask", "allow", "deny"];

const DECISION_LABELS = {
  ask: { en: "ASK", ar: "اسأل" },
  allow: { en: "ALLOW", ar: "سماح" },
  deny: { en: "DENY", ar: "منع" },
};

const DENIED_COUNT = (grants) =>
  Object.values(grants.grants || {}).filter((decision) => decision === "deny").length;

function SecurityView({ language = "en" }) {
  const ar = language === "ar";
  // Every string on this page comes from `src/i18n/strings.js`; the two
  // remaining `ar ? "ar" : "en"` expressions below are locale *selectors*
  // (they choose a key in a lookup table), not copy.
  const t = useMemo(() => (key, vars) => translate(language, key, vars), [language]);
  const [state, setState] = useState({ authorizations: [], operations: [], active_count: 0 });
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [grants, setGrants] = useState({ vocabulary: [], grants: {}, default_decision: "ask" });
  const [grantsError, setGrantsError] = useState(null);
  const [savingGrant, setSavingGrant] = useState(null);

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
    // Fetched separately and reported separately: a permissions endpoint that
    // is down does not make the authorisation list unknown, and blanking both
    // panels would hide which one actually failed.
    const permissions = await api.getPermissions();
    if (permissions.error) {
      setGrantsError(permissions.error);
    } else {
      setGrantsError(null);
      setGrants(permissions);
    }
    setLoading(false);
  }, []);

  const decide = useCallback(async (permission, decision) => {
    setSavingGrant(permission);
    setGrantsError(null);
    const result = await api.setPermission(permission, decision);
    if (result.error) {
      setGrantsError(result.error);
    } else {
      // The backend is the record, so the row shows what it stored rather than
      // what was clicked — those differ for `allow`, which is stored but does
      // not stop the tier gate from prompting.
      setGrants((prev) => ({
        ...prev,
        grants: { ...prev.grants, [permission]: result.decision },
      }));
    }
    setSavingGrant(null);
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
          {t("security.security_workspace")}
        </h1>
        <p className="hud-micro" style={{ color: "var(--hud-text-mute)" }}>
          {t("security.active_testing_requires_an_authorised_scope_enfo")}
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
            <span>{t("security.authorise_a_target")}</span>
            <span className="hud-micro ml-auto">{t("security.active")}: {state.active_count ?? 0}</span>
          </header>
          <form onSubmit={authorize} style={{ padding: "var(--space-3)", display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
            <label className="hud-micro" htmlFor="sec-target">
              {t("security.target")}
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
              {t("security.a_wildcard_for_every_host_is_refused_an_authoris")}
            </p>

            <fieldset style={{ border: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: "var(--space-1)" }}>
              <legend className="hud-micro" style={{ padding: 0 }}>
                {t("security.operations")}
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
                <label className="hud-micro" htmlFor="sec-ttl">{t("security.expires_after_days")}</label>
                <input
                  id="sec-ttl"
                  className="hud-input w-full"
                  type="number"
                  min="0"
                  value={form.ttl}
                  onChange={(e) => setForm({ ...form, ttl: e.target.value })}
                  placeholder={t("security.no_expiry")}
                />
              </div>
            </div>

            <label className="hud-micro" htmlFor="sec-note">{t("security.note")}</label>
            <input
              id="sec-note"
              className="hud-input w-full"
              value={form.note}
              onChange={(e) => setForm({ ...form, note: e.target.value })}
              placeholder={t("security.why_or_which_engagement")}
            />

            <button
              type="submit"
              disabled={busy || !form.target.trim() || form.operations.length === 0}
              className="hud-micro"
              style={{ padding: "var(--space-2) var(--space-3)", cursor: "pointer", background: "color-mix(in srgb, var(--hud-cyan) 8%, transparent)", border: "var(--border-w) solid color-mix(in srgb, var(--hud-cyan) 25%, transparent)", color: "var(--hud-cyan)" }}
            >
              {t("security.authorise")}
            </button>
          </form>
        </section>

        {/* ---- Run a probe ---- */}
        <section className="hud-panel flex-1" style={{ minWidth: "300px" }}>
          <header className="hud-panel-head">
            <span>{t("security.authorised_probe")}</span>
          </header>
          <div style={{ padding: "var(--space-3)", display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
            <p className="hud-micro" style={{ color: "var(--hud-text-mute)", fontSize: "var(--font-xs)" }}>
              {t("security.one_non_destructive_request_no_payloads_no_explo")}
            </p>
            <label className="hud-micro" htmlFor="sec-url">{t("security.url")}</label>
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
              {t("security.run_probe")}
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
                      {t("security.authorisation")}: <span className="hud-mono">{probeResult.authorization_id}</span>
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

      {/* ---- Capability grants ---- */}
      <section className="hud-panel">
        <header className="hud-panel-head">
          <span>{t("security.capabilities")}</span>
          <span className="hud-micro ml-auto">
            {t("security.denied")}: {DENIED_COUNT(grants)}
          </span>
        </header>

        {grantsError && (
          <p role="alert" className="hud-micro" style={{ padding: "var(--space-3)", color: "var(--hud-red)" }}>
            {grantsError}
          </p>
        )}

        <p className="hud-micro" style={{ padding: "var(--space-3) var(--space-3) 0", color: "var(--hud-text-mute)", fontSize: "var(--font-xs)" }}>
          {t("security.deny_is_refused_at_the_tool_gate_before_any_appr")}
        </p>

        {(grants.vocabulary || []).length === 0 ? (
          <p className="hud-micro" style={{ padding: "var(--space-3)", color: "var(--hud-text-mute)" }}>
            {t("security.no_permissions_listed")}
          </p>
        ) : (
          <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
            {(grants.vocabulary || []).map((row) => {
              // Partial payloads are tolerated on purpose: a response missing a
              // key must render as "unknown", never as a crash that takes the
              // whole shell down with it (the App runtime test walks every
              // destination with a stubbed API).
              const decision = (grants.grants || {})[row.permission] || grants.default_decision || "ask";
              return (
                <li
                  key={row.permission}
                  className="flex items-center flex-wrap"
                  style={{ gap: "var(--space-3)", padding: "var(--space-2) var(--space-3)", borderTop: "var(--border-w) solid var(--hud-edge-faint)" }}
                >
                  <span
                    aria-hidden="true"
                    // Three states, three colours: the indicator has to agree
                    // with the pressed button or a user reads the node, not
                    // the control, and has to be able to tell "no answer yet"
                    // from "this is refused".
                    className={`hud-hex ${
                      decision === "deny"
                        ? "hud-hex--error"
                        : decision === "allow"
                          ? "hud-hex--ok"
                          : "hud-hex--hollow hud-hex--idle"
                    }`}
                  />
                  <span style={{ fontSize: "var(--font-sm)", color: "var(--hud-text)" }}>
                    {row.label}
                  </span>
                  <span className="hud-mono" dir="ltr" style={{ fontSize: "var(--font-xs)", color: "var(--hud-text-mute)" }}>
                    {row.permission}
                  </span>
                  <div className="ml-auto flex" style={{ gap: "var(--space-1)" }}>
                    {DECISIONS.map((option) => (
                      <button
                        key={option}
                        type="button"
                        // "DENY" appears once per row, so the accessible name
                        // has to carry the permission: three identical buttons
                        // are three identical buttons to a screen reader.
                        aria-label={`${row.permission} ${option}`}
                        aria-pressed={decision === option}
                        disabled={savingGrant === row.permission}
                        onClick={() => decide(row.permission, option)}
                        className="hud-micro"
                        style={{
                          padding: "var(--space-1) var(--space-2)",
                          cursor: "pointer",
                          background: decision === option ? "color-mix(in srgb, var(--hud-cyan) 12%, transparent)" : "transparent",
                          border: `var(--border-w) solid ${decision === option ? "color-mix(in srgb, var(--hud-cyan) 35%, transparent)" : "var(--hud-edge-faint)"}`,
                          color:
                            decision !== option
                              ? "var(--hud-text-mute)"
                              : option === "deny"
                                ? "var(--hud-red)"
                                : "var(--hud-cyan)",
                        }}
                      >
                        {DECISION_LABELS[option][ar ? "ar" : "en"]}
                      </button>
                    ))}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      {/* ---- Recorded authorisations ---- */}
      <section className="hud-panel">
        <header className="hud-panel-head">
          <span>{t("security.recorded_authorisations")}</span>
        </header>
        {loading ? (
          <p className="hud-micro" style={{ padding: "var(--space-3)", color: "var(--hud-text-mute)" }}>
            {t("security.loading")}
          </p>
        ) : (state.authorizations || []).length === 0 ? (
          <p className="hud-micro" style={{ padding: "var(--space-3)", color: "var(--hud-text-mute)" }}>
            {t("security.none_active_testing_is_refused_until_one_exists")}
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
                    ? (t("security.revoked"))
                    : auth.expired
                      ? (t("security.expired"))
                      : (t("security.active_2"))}
                </span>
                {auth.active && (
                  <button
                    type="button"
                    onClick={() => revoke(auth.id)}
                    className="hud-micro"
                    style={{ padding: "var(--space-1) var(--space-2)", cursor: "pointer", background: "transparent", border: "var(--border-w) solid var(--hud-edge-faint)", color: "var(--hud-red)" }}
                  >
                    {t("security.revoke")}
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
