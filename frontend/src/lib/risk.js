/**
 * Risk → colour and label for the approval dialog and audit rows.
 *
 * Two fields describe risk on a `require_confirmation` frame and they do not
 * share a vocabulary: `permission` is the tool's static tier (safe /
 * confirm / dangerous) and `level` is the regex-derived hazard grade (LOW /
 * MODERATE / HIGH…). The UI must not care which one the backend sent, so
 * both are normalised into three tones here — one mapping, one test.
 *
 * Colour is never the only channel: each tone also carries a word and a
 * glyph (docs/DESIGN_SYSTEM.md §4).
 */

export const RISK_TONES = ["safe", "confirm", "high"];

const TONE_BY_WORD = {
  // static permission tiers
  safe: "safe",
  low: "safe",
  confirm: "confirm",
  moderate: "confirm",
  medium: "confirm",
  high: "high",
  dangerous: "high",
  critical: "high",
};

/**
 * @param {...any} candidates first non-empty candidate wins (pass
 *        `request.permission, request.level` so the tier leads).
 * @returns {"safe"|"confirm"|"high"} never throws; unknown input is "confirm",
 *          because an unrecognised risk must not be shown as safe.
 */
export function riskTone(...candidates) {
  for (const c of candidates) {
    if (!c) continue;
    const tone = TONE_BY_WORD[String(c).toLowerCase()];
    if (tone) return tone;
  }
  return "confirm";
}

/** CSS colour for a tone, from the palette tokens only. */
export function riskColor(tone) {
  if (tone === "safe") return "var(--hud-green)";
  if (tone === "high") return "var(--hud-red)";
  return "var(--hud-amber)";
}

/** The `.hud-pill--*` modifier class for a tone. */
export function riskPillClass(tone) {
  if (tone === "safe") return "hud-pill hud-pill--ok";
  if (tone === "high") return "hud-pill hud-pill--danger";
  return "hud-pill hud-pill--warn";
}

export default riskTone;
