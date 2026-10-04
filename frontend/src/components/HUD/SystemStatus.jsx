import { memo, useCallback, useEffect, useState } from "react";
import Panel, { TelemetryRow } from "./HUDPanel";
import { healthCheck } from "../../lib/api";

/**
 * SystemStatus — live backend telemetry.
 *
 * Every row maps to a real field on `GET /health`. Nothing here is invented
 * for the sake of a fuller-looking panel: when a value is absent the row
 * shows `—`, which is an honest answer rather than a decorative zero.
 *
 * A failed refresh clears the previous values and reports the failure, rather
 * than leaving stale numbers on screen looking current — the same honesty
 * rule SettingsPanel follows.
 */

function fmtLatency(ms) {
  if (typeof ms !== "number") return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
}

function SystemStatus({ language = "en", pollMs = 20000 }) {
  // Derived, not passed. Taking both `language` and a precomputed `ar` flag
  // let the two disagree: App passed `language` only, so the Arabic labels in
  // this panel were unreachable while the English ones rendered.
  const ar = language === "ar";
  const [health, setHealth] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      const data = await healthCheck();
      setHealth(data);
      setError(null);
    } catch (e) {
      setHealth(null);
      setError(e?.message || String(e));
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, pollMs);
    return () => clearInterval(t);
  }, [load, pollMs]);

  const llm = health?.llm || {};
  const metrics = llm.metrics || {};
  const voice = health?.voice || {};
  const safety = health?.safety || {};
  const tiers = health?.tools_by_tier || {};
  const totalTools = health?.tools_loaded;

  return (
    <Panel title={ar ? "حالة النظام" : "SYSTEM STATUS"} code="SYS" led accent={health ? "var(--hud-cyan)" : "var(--hud-red)"}>
      {error && (
        <div
          className="hud-tele__v text-left"
          style={{ color: "var(--hud-red)", fontSize: "var(--font-sm)", marginBottom: "var(--space-1)" }}
          role="alert"
        >
          {ar ? "تعذر الاتصال بالخدمة" : "BACKEND UNREACHABLE"}
        </div>
      )}

      <TelemetryRow
        label={ar ? "الاتصال" : "LINK"}
        value={health ? (ar ? "متصل" : "ONLINE") : (ar ? "مقطوع" : "DOWN")}
        tone={health ? "ok" : "error"}
      />
      <div className="hud-rule" style={{ margin: "var(--space-2) 0" }} />
      <TelemetryRow
        label={ar ? "النموذج" : "MODEL"}
        value={llm.primary_model || "—"}
        tone="accent"
        title={llm.primary_model || undefined}
      />
      <TelemetryRow
        label={ar ? "الاستدلال" : "REASONING"}
        value={llm.reasoning_model || "—"}
      />
      <TelemetryRow
        label={ar ? "السلسلة" : "CHAIN"}
        value={Array.isArray(llm.chain) ? `${llm.chain.length} ${ar ? "نموذج" : "models"}` : "—"}
      />
      <div className="hud-rule" style={{ margin: "var(--space-2) 0" }} />
      <TelemetryRow label="STT" value={voice.stt ? (ar ? "جاهز" : "READY") : (ar ? "غير متاح" : "N/A")} tone={voice.stt ? "ok" : "warn"} />
      <TelemetryRow label="TTS" value={voice.tts ? (ar ? "جاهز" : "READY") : (ar ? "غير متاح" : "N/A")} tone={voice.tts ? "ok" : "warn"} />
      <div className="hud-rule" style={{ margin: "var(--space-2) 0" }} />
      <TelemetryRow
        label={ar ? "الصندوق" : "SANDBOX"}
        value={safety.enabled ? (ar ? "مفعّل" : "ARMED") : (ar ? "معطّل" : "OFF")}
        tone={safety.enabled ? "ok" : "error"}
      />
      <TelemetryRow
        label={ar ? "مهلة التأكيد" : "GATE T/O"}
        value={typeof safety.confirm_timeout === "number" ? `${safety.confirm_timeout}s` : "—"}
      />
      <TelemetryRow
        label={ar ? "دائمة التأكيد" : "ALWAYS ASK"}
        value={Array.isArray(safety.always_confirm) && safety.always_confirm.length
          ? safety.always_confirm.join(" ")
          : "—"}
        title={Array.isArray(safety.always_confirm) ? safety.always_confirm.join(", ") : undefined}
      />
      <div className="hud-rule" style={{ margin: "var(--space-2) 0" }} />
      <TelemetryRow
        label={ar ? "الأدوات" : "TOOLS"}
        value={typeof totalTools === "number" ? totalTools : "—"}
        tone="accent"
      />
      {["safe", "confirm", "dangerous"].map((tier) =>
        tiers[tier] ? (
          <TelemetryRow
            key={tier}
            label={tier.toUpperCase()}
            value={tiers[tier].length}
            tone={tier === "dangerous" ? "warn" : "default"}
          />
        ) : null,
      )}
      <div className="hud-rule" style={{ margin: "var(--space-2) 0" }} />
      <TelemetryRow label={ar ? "الطلبات" : "REQS"} value={metrics.requests ?? "—"} />
      <TelemetryRow label={ar ? "زمن" : "LATENCY"} value={fmtLatency(metrics.avg_latency_ms)} />
      <TelemetryRow
        label={ar ? "مرفوضة" : "REJECTED"}
        value={Array.isArray(llm.rejected_models) && llm.rejected_models.length ? llm.rejected_models.join(" ") : "0"}
        tone={Array.isArray(llm.rejected_models) && llm.rejected_models.length ? "error" : "default"}
      />
    </Panel>
  );
}

export default memo(SystemStatus);
