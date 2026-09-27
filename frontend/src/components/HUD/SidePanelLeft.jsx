import { useState, useEffect } from "react";
import { healthCheck } from "../../lib/api";

/**
 * Statuses the backend actually emits. Anything else falls through to the
 * default colour rather than rendering as an unknown label.
 */
const STATUS_COLORS = {
  idle: "#00d4ff",
  connected: "#00d4ff",
  disconnected: "#ff3355",
  listening: "#00ff88",
  transcribing: "#00ff88",
  thinking: "#00d4ff",
  planning: "#00d4ff",
  executing: "#f0b000",
  awaiting_confirmation: "#ff3355",
  awaiting_command: "#ffb400",
  speaking: "#00f0ff",
  done: "#00d4ff",
  error: "#ff3355",
};

export default function SidePanelLeft({ status = "idle", isConnected = false, language = "en" }) {
  const [time, setTime] = useState(new Date());
  const [uptime, setUptime] = useState(0);
  const [health, setHealth] = useState(null);

  useEffect(() => {
    const timer = setInterval(() => {
      setTime(new Date());
      setUptime((p) => p + 1);
    }, 1000);
    return () => clearInterval(timer);
  }, []);

  // The model label used to be a hard-coded "QWEN 2.5 7B" that did not match
  // any configured model and never changed. Read it from the backend instead,
  // which also surfaces whether the API key is actually present.
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const data = await healthCheck();
        if (!cancelled) setHealth(data);
      } catch {
        if (!cancelled) setHealth(null);
      }
    };
    load();
    const poll = setInterval(load, 30000);
    return () => {
      cancelled = true;
      clearInterval(poll);
    };
  }, []);

  const formatUptime = (s) => {
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
  };

  const statusColor = STATUS_COLORS[status] || "#00d4ff";

  const llm = health?.llm || {};
  const modelLabel = llm.primary_model || (llm.configured === false ? "NOT CONFIGURED" : "—");
  const workspace = health?.workspace?.root || null;

  return (
    <div className="w-48 flex flex-col gap-3 p-3 animate-fade-in" style={{ animationDelay: "0.1s" }}>
      {/* Date / Time */}
      <div className="hud-glass-light rounded-sm p-3">
        <div className="text-[8px] font-display tracking-[0.2em] mb-1" style={{ color: "var(--text-muted)" }}>
          {language === "ar" ? "التاريخ والوقت" : "DATE & TIME"}
        </div>
        <div className="text-lg font-display font-bold glow-text" style={{ color: "#00d4ff" }}>
          {time.toLocaleTimeString(language === "ar" ? "ar-EG" : "en-US", { hour12: false })}
        </div>
        <div className="text-[10px] font-mono" style={{ color: "var(--text-secondary)" }}>
          {time.toLocaleDateString(language === "ar" ? "ar-EG" : "en-US", {
            weekday: "long",
            year: "numeric",
            month: "short",
            day: "numeric",
          })}
        </div>
      </div>

      {/* System Status */}
      <div className="hud-glass-light rounded-sm p-3">
        <div className="text-[8px] font-display tracking-[0.2em] mb-2" style={{ color: "var(--text-muted)" }}>
          {language === "ar" ? "حالة النظام" : "SYSTEM STATUS"}
        </div>

        <div className="space-y-2">
          <StatRow label="CONN" value={isConnected ? "ONLINE" : "OFFLINE"}
            color={isConnected ? "#00ff88" : "#ff3355"} />
          <StatRow label="MODE" value={status.toUpperCase()} color={statusColor} />
          <StatRow label="UPTIME" value={formatUptime(uptime)} color="#00d4ff" />
          <StatRow label="AGENT" value="J.A.R.V.I.S" color="#00d4ff" />
        </div>
      </div>

      {/* Active Model — read from GET /health, not hard-coded */}
      <div className="hud-glass-light rounded-sm p-3">
        <div className="text-[8px] font-display tracking-[0.2em] mb-1" style={{ color: "var(--text-muted)" }}>
          {language === "ar" ? "النموذج النشط" : "ACTIVE MODEL"}
        </div>
        <div
          className="text-[10px] font-mono break-all"
          style={{ color: llm.configured === false ? "#ff3355" : "#00d4ff" }}
          title={modelLabel}
        >
          {modelLabel}
        </div>
        <div className="text-[9px] font-mono mt-0.5" style={{ color: "var(--text-muted)" }}>
          {llm.configured === false
            ? language === "ar" ? "مفتاح API مفقود" : "API KEY MISSING"
            : language === "ar" ? "عبر OpenRouter" : "via OpenRouter"}
        </div>
        {llm.rejected_models?.length > 0 && (
          <div className="text-[9px] font-mono mt-0.5" style={{ color: "#ff3355" }}>
            {language === "ar" ? "انتهاك سياسة النماذج المجانية" : "free-model policy violated"}
          </div>
        )}
      </div>

      {/* Workspace — the directory every file tool is confined to */}
      {workspace && (
        <div className="hud-glass-light rounded-sm p-3">
          <div className="text-[8px] font-display tracking-[0.2em] mb-1" style={{ color: "var(--text-muted)" }}>
            {language === "ar" ? "مجلد العمل" : "WORKSPACE"}
          </div>
          <div className="text-[9px] font-mono break-all" style={{ color: "var(--text-secondary)" }} title={workspace}>
            {workspace}
          </div>
        </div>
      )}

      {/* Microphone Status */}
      <div className="hud-glass-light rounded-sm p-3">
        <div className="text-[8px] font-display tracking-[0.2em] mb-2" style={{ color: "var(--text-muted)" }}>
          {language === "ar" ? "المايكروفون" : "MICROPHONE"}
        </div>
        <div className="flex items-center gap-2">
          <div className="w-2 h-2 rounded-full" style={{
            background: status === "listening" ? "#00ff88" : "var(--text-muted)",
            boxShadow: status === "listening" ? "0 0 8px #00ff88" : "none",
          }} />
          <span className="text-[10px] font-mono" style={{
            color: status === "listening" ? "#00ff88" : "var(--text-secondary)",
          }}>
            {status === "listening" ? "ACTIVE" : "STANDBY"}
          </span>
        </div>
      </div>
    </div>
  );
}

function StatRow({ label, value, color }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-[8px] font-mono tracking-wider" style={{ color: "var(--text-muted)" }}>{label}</span>
      <span className="text-[10px] font-mono font-bold" style={{ color }}>{value}</span>
    </div>
  );
}
