/**
 * Shared helpers for the Nexus home screen.
 *
 * Two jobs: format (finite numbers or a dash, never a fabricated zero) and
 * the state word map. The words differ from `hudState.js` on purpose — the
 * Nexus reference labels states STANDBY / LISTENING / PROCESSING / SPEAKING —
 * but the *states* here are the same four the product already resolves from
 * the live WebSocket session. Visual mapping only; no new state machine.
 */

/** "—" for null/undefined; integers for finite numbers. */
export function fmt(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return String(Math.round(value));
}

/** A percentage with its sign and a dash when absent. */
export function pct(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `${Math.round(value)}%`;
}

/** Latency band label: the number when known, otherwise the dash. */
export function ms(value) {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return "—";
  return `${Math.round(value)}ms`;
}

/**
 * `hhh mm` uptime from real seconds; null renders "—".
 */
export function uptime(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return "—";
  const totalMinutes = Math.floor(seconds / 60);
  const h = Math.floor(totalMinutes / 60);
  const m = totalMinutes % 60;
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

import { translate } from "../../i18n/strings";

/**
 * The reference word for each resolved HUD state, as an i18n key. Every output
 * of `hudState.resolveState()` has an entry; unknown falls to standby.
 */
const STATE_KEYS = {
  idle: { key: "nexus.word_standby", hue: "cyan" },
  listening: { key: "nexus.word_listening", hue: "green" },
  transcribing: { key: "nexus.word_transcribing", hue: "green" },
  thinking: { key: "nexus.word_processing", hue: "blue" },
  planning: { key: "nexus.word_processing", hue: "blue" },
  executing: { key: "nexus.word_processing", hue: "blue" },
  speaking: { key: "nexus.word_speaking", hue: "amber" },
  confirming: { key: "nexus.word_awaiting", hue: "purple" },
  awaiting: { key: "nexus.word_awaiting", hue: "purple" },
  error: { key: "nexus.word_error", hue: "red" },
  offline: { key: "nexus.word_offline", hue: "mute" },
};

export function coreWord(state) {
  return STATE_KEYS[state] || STATE_KEYS.idle;
}

export function coreLabel(language, state) {
  return translate(language, coreWord(state).key);
}

/** Load-index band → status key + hue. Bands are stated, not hidden. */
export function loadBand(load) {
  if (typeof load !== "number" || !Number.isFinite(load)) {
    return { key: "nexus.band_nodata", hue: "mute" };
  }
  if (load < 40) return { key: "nexus.band_optimal", hue: "green" };
  if (load < 70) return { key: "nexus.band_nominal", hue: "cyan" };
  return { key: "nexus.band_strained", hue: "amber" };
}

/** Clock text in the reference's HH:MM narrow form + full date line. */
export function clockText(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return {
    time: `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`,
    date: date.toLocaleDateString(undefined, {
      weekday: "short",
      month: "short",
      day: "numeric",
      year: "numeric",
    }),
  };
}

/** The quick actions the console offers. Every one is a real command. */
export const CHIPS = [
  { key: "status", textKey: "nexus.chip_status", command: "system information" },
  { key: "time", textKey: "nexus.chip_time", command: "what time is it" },
  { key: "screenshot", textKey: "nexus.chip_screenshot", command: "take a screenshot" },
  { key: "tests", textKey: "nexus.chip_tests", command: "run the tests" },
  { key: "project", textKey: "nexus.chip_project", command: "create a project" },
  { key: "help", textKey: "nexus.chip_help", command: "what can you do" },
];
