import { memo, useCallback, useEffect, useState } from "react";
import Panel, { TelemetryRow } from "./HUDPanel";
import { getSystem, healthCheck } from "../../lib/api";

/**
 * SystemStatus — live backend telemetry.
 *
 * Two endpoints, one tick: `GET /health` answers *what is configured* (chain,
 * metrics, voice, safety) and `GET /system` answers *what the machine is doing*
 * (CPU, memory, disk, uptime, totals). Either may fail alone, so the panel
 * keeps whichever answered and reports one failure — a half-populated panel
 * with honest dashes beats an empty one that says nothing.
 *
 * Nothing here is invented for the sake of a fuller-looking panel: an absent
 * value renders `—`, and a resource block that knows *why* it is missing puts
 * the reason in the row's `title`. `0%` is never drawn from a `sampling` CPU —
 * the first counter reading has nothing to divide yet, and "0%" would read as
 * an idle machine at exactly the wrong moment.
 *
 * A failed refresh clears the previous values and reports the failure, rather
 * than leaving stale numbers on screen looking current — the same honesty
 * rule SettingsPanel follows.
 */

function fmtLatency(ms) {
  if (typeof ms !== "number") return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
}

function fmtBytes(n) {
  if (typeof n !== "number") return "—";
  const gb = n / 1024 ** 3;
  return gb >= 1 ? `${gb.toFixed(1)}GB` : `${Math.round(n / 1024 ** 2)}MB`;
}

function fmtDuration(seconds) {
  if (typeof seconds !== "number" || seconds < 0) return "—";
  const s = Math.floor(seconds % 60);
  const m = Math.floor(seconds / 60) % 60;
  const h = Math.floor(seconds / 3600) % 24;
  const d = Math.floor(seconds / 86400);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}

/** The word for a block that is not `ok` — never a fabricated number. */
function blockWord(status, ar) {
  if (status === "sampling") return ar ? "…" : "SAMPLING";
  return "—";
}

function SystemStatus({ language = "en", pollMs = 20000 }) {
  // Derived, not passed. Taking both `language` and a precomputed `ar` flag
  // let the two disagree: App passed `language` only, so the Arabic labels in
  // this panel were unreachable while the English ones rendered.
  const ar = language === "ar";
  const [health, setHealth] = useState(null);
  const [sys, setSys] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      // `/system` failing on its own (an older backend) must not take the
      // health rows down with it — hence the caught side of the pair.
      const [healthData, sysData] = await Promise.all([
        healthCheck(),
        getSystem().catch(() => null),
      ]);
      setHealth(healthData);
      setSys(sysData);
      setError(null);
    } catch (e) {
      setHealth(null);
      setSys(null);
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

  const resources = sys?.resources || {};
  const cpu = resources.cpu || {};
  const mem = resources.memory || {};
  const disk = resources.disk || {};
  const net = resources.network || {};
  const core = health?.core || {};

  const modelHealth = llm.model_health && typeof llm.model_health === "object"
    ? llm.model_health
    : {};
  const healthNames = Object.keys(modelHealth);
  const blockedNames = healthNames.filter((name) => modelHealth[name]?.blocked);
  const byLatency = metrics.by_model_latency_ms && typeof metrics.by_model_latency_ms === "object"
    ? metrics.by_model_latency_ms
    : {};

  return (
    <Panel title={ar ? "حالة النظام" : "SYSTEM STATUS"} code="SYS" led accent={health ? "var(--hud-cyan)" : "var(--hud-red)"}>
      {/* The rail itself is `overflow-hidden`, so this list owns its own
          scroll — without it the rows below the fold would be clipped with
          no way to reach them. */}
      <div style={{ maxHeight: "60vh", overflowY: "auto" }}>
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
      {/* Circuit health, then each model's own mean latency: "which model is
          being benched, and how slow was it when it ran" — the two numbers
          behind every fallback decision, in one place. */}
      <TelemetryRow
        label={ar ? "صحة النماذج" : "M. HEALTH"}
        value={
          healthNames.length
            ? `${healthNames.length - blockedNames.length}/${healthNames.length}`
            : "—"
        }
        tone={blockedNames.length ? "warn" : healthNames.length ? "ok" : "default"}
        title={blockedNames.join(", ") || undefined}
      />
      {Object.entries(byLatency).map(([model, ms]) => (
        <TelemetryRow
          key={model}
          label={(model.split("/").pop() || model).slice(0, 22)}
          value={fmtLatency(ms)}
          title={`${model} · ${fmtLatency(ms)}`}
        />
      ))}
      <div className="hud-rule" style={{ margin: "var(--space-2) 0" }} />
      {/* Machine state. Each block is `ok`, `sampling` (CPU's first reading)
          or `unavailable` with a reason in `title` — `—` is the value, the
          reason is on hover, and `0%` is never drawn from a guess. */}
      <TelemetryRow
        label={ar ? "المعالج" : "CPU"}
        value={cpu.status === "ok" ? `${cpu.percent}%` : blockWord(cpu.status, ar)}
        tone={cpu.status === "ok" && cpu.percent >= 90 ? "warn" : "default"}
        title={cpu.reason}
      />
      <TelemetryRow
        label={ar ? "الذاكرة" : "MEM"}
        value={
          mem.status === "ok"
            ? `${mem.percent}% · ${fmtBytes(mem.used_bytes)}/${fmtBytes(mem.total_bytes)}`
            : blockWord(mem.status, ar)
        }
        title={mem.reason}
      />
      <TelemetryRow
        label={ar ? "القرص" : "DISK"}
        value={
          disk.status === "ok"
            ? `${disk.percent}% · ${fmtBytes(disk.free_bytes)} ${ar ? "حر" : "free"}`
            : blockWord(disk.status, ar)
        }
        title={disk.reason || disk.path}
      />
      <TelemetryRow
        label={ar ? "الشبكة" : "NET"}
        // The declared state: counters are not collected, and the row says
        // so with the reason attached — not an omission, not a zero.
        value={net.status === "ok" ? "—" : (ar ? "غير متاح" : "N/A")}
        tone="warn"
        title={net.reason}
      />
      <div className="hud-rule" style={{ margin: "var(--space-2) 0" }} />
      <TelemetryRow
        label={ar ? "التشغيل" : "UPTIME"}
        value={fmtDuration(sys?.uptime_seconds)}
      />
      {/* What the runtime can draw on, plus skill load failures — a manifest
          that no longer loads is a real problem and gets the warn tone. The
          fuller breakdown (providers, models, default) rides in `title`. */}
      <TelemetryRow
        label={ar ? "النواة" : "CORE"}
        value={
          core.skills != null
            ? `${core.skills} ${ar ? "مهارة" : "skills"} · ${core.projects ?? 0} ${ar ? "مشروع" : "projects"}${
                core.skill_errors ? ` · ${core.skill_errors} ${ar ? "خطأ" : "err"}` : ""
              }`
            : "—"
        }
        tone={core.skill_errors ? "warn" : "default"}
        title={
          core.providers != null
            ? `${core.providers} providers · ${core.models} models · default ${core.default_provider || "—"}`
            : undefined
        }
      />
      <TelemetryRow
        label={ar ? "المهام" : "TASKS"}
        value={sys?.tasks ? `${sys.tasks.active}/${sys.tasks.total}` : "—"}
      />
      <TelemetryRow
        label={ar ? "عمليات" : "PROCS"}
        value={
          sys?.processes
            ? `${sys.processes.running?.length ?? 0}/${sys.processes.tracked}`
            : "—"
        }
      />
      <TelemetryRow
        label={ar ? "طرفية" : "TERMS"}
        value={typeof sys?.terminals?.open === "number" ? sys.terminals.open : "—"}
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
      </div>
    </Panel>
  );
}

export default memo(SystemStatus);
