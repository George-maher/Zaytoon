import { memo } from "react";

/**
 * StatusRing — a compact circular gauge, used sparingly.
 *
 * The reference image carries several segmented rings with percentage
 * readouts. That motif is worth having, but a gauge is the most tempting
 * place in any UI to invent a number, so the component is built to make
 * fabrication impossible rather than merely discouraged:
 *
 * - `value` must be a finite number. `null`, `undefined`, `NaN` and a
 *   non-number all render the **unknown** state — an empty ring with a dash,
 *   never a fabricated 0%.
 * - The percentage is clamped to 0–100.
 * - The number is always printed as text, so the ring's arc is decoration
 *   *and* the read-out; a screen reader and a greyscale print get the same
 *   fact.
 *
 * `label` carries the state as a word. The arc colour is a second channel
 * only — never the sole one.
 */
function StatusRing({
  value = null,
  max = 100,
  size = 34,
  tone = "accent",
  label = "",
  language = "en",
}) {
  const ar = language === "ar";
  const numeric = typeof value === "number" && Number.isFinite(value);
  const pct = numeric ? Math.max(0, Math.min(100, (value / max) * 100)) : null;

  const r = (size - 4) / 2;
  const c = 2 * Math.PI * r;
  const colour =
    tone === "ok" ? "var(--hud-green)"
      : tone === "warn" ? "var(--hud-amber)"
        : tone === "error" ? "var(--hud-red)"
          : "var(--hud-cyan)";

  const readout = pct === null
    ? "—"
    : `${Math.round(pct)}%`;

  // The accessible name states the fact in words. An unlabelled gauge is a
  // picture of a number nobody can hear.
  const spoken = label || (pct === null
    ? (ar ? "غير متاح" : "unavailable")
    : (ar ? `${readout} — ${label}` : `${readout} ${label}`));

  return (
    <span
      className="relative flex-none inline-flex items-center justify-center"
      style={{ width: size, height: size }}
      role="img"
      aria-label={spoken}
      title={spoken}
    >
      <svg
        width={size}
        height={size}
        viewBox={`0 0 ${size} ${size}`}
        className="absolute inset-0"
        aria-hidden="true"
      >
        {/* Track */}
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke="var(--hud-edge-faint)"
          strokeWidth={1.5}
        />
        {/* Value. Absent entirely when unknown — an empty track *is* the
            honest rendering of "we do not know", and drawing a zero-length
            arc would look identical to a measured zero. */}
        {pct !== null && (
          <circle
            cx={size / 2}
            cy={size / 2}
            r={r}
            fill="none"
            stroke={colour}
            strokeWidth={1.5}
            strokeLinecap="butt"
            strokeDasharray={`${(c * pct) / 100} ${c}`}
            transform={`rotate(-90 ${size / 2} ${size / 2})`}
          />
        )}
      </svg>
      <span
        className="hud-micro"
        style={{
          fontSize: "var(--font-xs)",
          color: pct === null ? "var(--hud-text-mute)" : colour,
        }}
      >
        {readout}
      </span>
    </span>
  );
}

export default memo(StatusRing);