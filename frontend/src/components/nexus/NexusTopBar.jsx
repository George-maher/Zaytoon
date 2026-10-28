import { memo, useEffect, useState } from "react";
import { coreWord, coreLabel, clockText, fmt, ms, pct } from "./nexusFormat";
import { translate } from "../../i18n/strings";

/**
 * NexusTopBar — brand, live state pill, the four headline metrics, the clock,
 * and the action cluster.
 *
 * Every figure comes down as a prop from the one telemetry hook; anything the
 * backend did not answer renders "—" (never a zero, never a placeholder
 * number from the mock). The pill shows the same resolved state the rest of
 * the product uses, in the reference's vocabulary, both languages.
 */

const ICONS = {
  mic: (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />
      <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
      <line x1="12" y1="19" x2="12" y2="23" />
      <line x1="8" y1="23" x2="16" y2="23" />
    </svg>
  ),
  grid: (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3" y="3" width="7" height="7" rx="1" />
      <rect x="14" y="3" width="7" height="7" rx="1" />
      <rect x="14" y="14" width="7" height="7" rx="1" />
      <rect x="3" y="14" width="7" height="7" rx="1" />
    </svg>
  ),
  gear: (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </svg>
  ),
};

function NexusTopBar({
  language,
  state,
  isConnected,
  health,
  system,
  ReminderPanel,
  onVoiceToggle,
  voiceMode,
  onOpenCommands,
  onSettingsToggle,
  remindersProps,
}) {
  const t = (key, vars) => translate(language, key, vars);
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);
  const clock = clockText(now);
  const { hue } = coreWord(state);

  // State word: OFFLINE when the socket is down, the resolved state otherwise —
  // in either language the product speaks.
  const pillWord = isConnected ? coreLabel(language, state) : t("nexus.word_offline");
  const pillHue = isConnected ? hue : "mute";

  const netUnavailable = system?.netStatus === "unavailable";
  const netReason = netUnavailable
    ? String(system?.netReason || t("nexus.unavailable"))
    : undefined;

  return (
    <div className="nx-topbar" dir="ltr">
      <div className="nx-brand">
        <span className="nx-brand-orb" aria-hidden="true" />
        <div style={{ minWidth: 0 }}>
          <div className="nx-brand-name">ZAYTOON</div>
          <div className="nx-brand-sub">
            {health?.version ? `v${health.version}` : "AI OS"}
          </div>
        </div>
      </div>

      <span className="nx-pill" data-hue={pillHue} role="status" aria-live="polite">
        <span className="nx-pill-dot" aria-hidden="true" />
        {pillWord}
      </span>

      <div className="nx-metrics">
        <span className="nx-metric" title={t("nexus.top_load")}>
          {t("nexus.top_load")} <b>{pct(system?.load)}</b>
        </span>
        <span className="nx-metric" title={t("nexus.top_tools")}>
          {t("nexus.top_tools")} <b>{fmt(health?.toolCalls)}</b>
        </span>
        <span className="nx-metric" title={t("nexus.top_valid")}>
          {t("nexus.top_valid")}{" "}
          <b>{health?.successRate == null ? "—" : `${Math.round(health.successRate)}%`}</b>
        </span>
        <span className="nx-metric" title={t("nexus.top_latency")}>
          {t("nexus.top_latency")} <b>{ms(health?.latencyMs)}</b>
        </span>
        <span className="nx-metric" title={netReason}>
          {t("nexus.top_net")} <b>—</b>
        </span>
      </div>

      <div className="nx-clock">
        <div className="nx-clock-time">{clock.time}</div>
        <div className="nx-clock-date">{clock.date}</div>
      </div>

      <div className="nx-actions">
        {ReminderPanel ? <ReminderPanel {...remindersProps} /> : null}
        <button
          type="button"
          className="nx-iconbtn"
          onClick={onVoiceToggle}
          aria-pressed={voiceMode}
          aria-label={t("nexus.mic")}
          title={t("nexus.voice_input")}
        >
          {ICONS.mic}
        </button>
        <button
          type="button"
          className="nx-iconbtn"
          onClick={onOpenCommands}
          aria-label={t("nexus.all_commands")}
          title={t("nexus.palette_title")}
        >
          {ICONS.grid}
        </button>
        <button
          type="button"
          className="nx-iconbtn"
          onClick={onSettingsToggle}
          aria-label={t("nexus.settings")}
          title={t("nexus.settings")}
        >
          {ICONS.gear}
        </button>
      </div>
    </div>
  );
}

export default memo(NexusTopBar);
