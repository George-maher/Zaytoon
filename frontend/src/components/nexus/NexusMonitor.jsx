import { memo } from "react";
import { fmt, pct, uptime, loadBand } from "./nexusFormat";
import { translate } from "../../i18n/strings";

/**
 * NexusMonitor — the left rail: load gauge, the four counters, session rows.
 * All figures come from the real GET /system poll; "—" means the backend did
 * not report that counter.
 *
 * Tiles follow the reference's two-by-two rhythm: CPU, MEMORY, NETWORK, DISK.
 * The machine exposes no GPU counter, and the network counters are reported
 * `unavailable` with a reason — those facts are rendered, not smoothed over:
 * the network tile carries its reason as a title, the fourth slot is the real
 * disk reading instead of a fabricated GPU percentage.
 */

function Sparkline({ data, hue = "#00f5ff" }) {
  // A series shorter than two points cannot show a trend, so it renders an
  // empty axis rather than a straight line that implies calm.
  const pts = (data || []).filter((v) => typeof v === "number");
  if (pts.length < 2) {
    return <svg className="nx-spark" viewBox="0 0 100 30" preserveAspectRatio="none" aria-hidden="true" />;
  }
  const width = 100;
  const height = 30;
  const max = Math.max(...pts, 100);
  const min = Math.min(...pts, 0);
  const span = max - min || 1;
  const step = width / (pts.length - 1);
  const path = pts
    .map((v, i) => {
      const x = i * step;
      const y = height - ((v - min) / span) * (height - 4) - 2;
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg className="nx-spark" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-hidden="true">
      <path d={path} fill="none" stroke={hue} strokeWidth="1.5" opacity="0.9" />
    </svg>
  );
}

function Tile({ label, value, hue, history, flat, title }) {
  return (
    <div
      className="nx-tile"
      data-hue={hue}
      title={title}
      aria-label={`${label} ${value}`}
    >
      <div className="nx-tile-head">{label}</div>
      <div className="nx-tile-value" data-flat={flat ? "true" : undefined}>
        {value}
      </div>
      <Sparkline data={history} hue={`var(--nx-${hue === "mute" ? "mute" : hue})`} />
    </div>
  );
}

function NexusMonitor({ language, system, history }) {
  const t = (key, vars) => translate(language, key, vars);
  const load = system?.load == null ? null : system.load;
  const { hue: bandHue, key: bandKey } = loadBand(load);
  const gaugeColor =
    bandHue === "mute"
      ? "var(--nx-mute)"
      : bandHue === "cyan"
        ? "var(--nx-cyan)"
        : bandHue === "amber"
          ? "var(--nx-amber)"
          : "var(--nx-green)";

  return (
    <div className="nx-rail" aria-label={t("nexus.monitor_panel")}>
      <section className="nx-panel">
        <header className="nx-panel-head">
          <span className="nx-panel-title">{t("nexus.monitor_panel")}</span>
          <span className="nx-live">{t("nexus.live")}</span>
        </header>

        <div className="nx-gauge-card">
          <svg width="64" height="64" viewBox="0 0 64 64" aria-hidden="true">
            <circle cx="32" cy="32" r="26" fill="none" stroke="rgba(0,245,255,0.12)" strokeWidth="6" />
            {typeof load === "number" && (
              <circle
                cx="32"
                cy="32"
                r="26"
                fill="none"
                stroke={gaugeColor}
                strokeWidth="6"
                strokeLinecap="round"
                strokeDasharray={`${(Math.min(load, 100) / 100) * 163.4} 163.4`}
                transform="rotate(-90 32 32)"
              />
            )}
            <text
              x="32"
              y="37"
              textAnchor="middle"
              fontSize="15"
              fontFamily="var(--nx-mono)"
              fontWeight="700"
              fill="var(--nx-text)"
            >
              {fmt(load)}
            </text>
          </svg>
          <div className="nx-gauge-copy">
            <div className="nx-gauge-title">{t("nexus.load_index")}</div>
            <div className="nx-gauge-status">{t(bandKey)}</div>
            <div className="nx-gauge-sub">
              {t("nexus.uptime")}: {uptime(system?.uptimeSeconds)}
            </div>
          </div>
        </div>

        <div className="nx-tiles" style={{ marginTop: 10 }}>
          <Tile
            label={t("nexus.cpu_tile")}
            value={pct(system?.cpu)}
            hue="cyan"
            history={history.cpu}
            flat={system?.cpu == null}
            title={
              system?.cpuStatus && system.cpuStatus !== "ok"
                ? String(system.cpuStatus)
                : undefined
            }
          />
          <Tile
            label={t("nexus.memory_tile")}
            value={pct(system?.memory)}
            hue="purple"
            history={history.memory}
            flat={system?.memory == null}
          />
          <Tile
            label={t("nexus.network_tile")}
            value="—"
            hue="blue"
            history={[]}
            flat
            title={
              system?.netStatus === "unavailable"
                ? String(system?.netReason || t("nexus.unavailable"))
                : undefined
            }
          />
          <Tile
            label={t("nexus.disk_tile")}
            value={pct(system?.disk)}
            hue="amber"
            history={history.disk}
            flat={system?.disk == null}
          />
        </div>
      </section>

      <section className="nx-panel">
        <header className="nx-panel-head">
          <span className="nx-panel-title">{t("nexus.session_panel")}</span>
        </header>
        <div className="nx-rows">
          <div className="nx-row">
            <span>{t("nexus.active_tasks")}</span>
            <b>{fmt(system?.tasksActive)}</b>
          </div>
          <div className="nx-row">
            <span>{t("nexus.tasks_total")}</span>
            <b>{fmt(system?.tasksTotal)}</b>
          </div>
          <div className="nx-row">
            <span>{t("nexus.tracked")}</span>
            <b>{fmt(system?.processesTracked)}</b>
          </div>
          <div className="nx-row">
            <span>{t("nexus.terminals")}</span>
            <b>{fmt(system?.terminalsOpen)}</b>
          </div>
        </div>
      </section>
    </div>
  );
}

export default memo(NexusMonitor);
