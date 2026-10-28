import { memo, useMemo } from "react";
import { coreWord, pct } from "./nexusFormat";
import { translate } from "../../i18n/strings";

/**
 * NexusOrb — the visual core.
 *
 * A click on it toggles voice input (the reference's "click the core" maps to
 * the one real state change a user can cause here), so the word under it is
 * the real resolved state, not a demo cycle. The labels orbiting the rings are
 * the live counters — load, CPU, memory, disk, active tasks, uptime — the same
 * real samples the left rail reads. Where a counter is missing, its marker
 * shows "—" rather than inventing a percentage.
 */

// The reference's series colours: cyan = system, sky = network, amber = gpu,
// purple = memory. Kept as literals because SVG stroke/fill cannot take a
// scoped custom property from an ancestor reliably in every engine; the values
// are the same as --nx-* in styles/nexus.css.
const HUE = {
  cyan: "#00f5ff",
  green: "#22c55e",
  blue: "#0ea5e9",
  amber: "#f59e0b",
};

function NexusOrb({ state, size = 320, onToggle, voiceMode, language, system }) {
  const t = (key) => translate(language, key);
  const { key: stateKey, hue } = coreWord(state);
  const color = HUE[hue] || HUE.cyan;
  const label = translate(language, stateKey);
  const active = state !== "idle";

  // Six evenly-spaced readout slots around the ring — real counters.
  const markers = useMemo(
    () => [
      pct(system?.load),
      pct(system?.cpu),
      pct(system?.memory),
      pct(system?.disk),
      system?.tasksActive === 0 || system?.tasksActive
        ? String(system.tasksActive)
        : "—",
      system?.uptimeSeconds != null
        ? `${Math.floor(system.uptimeSeconds / 60)}m`
        : "—",
    ],
    [system],
  );

  const r1 = size / 2;

  return (
    <div>
      <button
        type="button"
        className="nx-core-btn"
        onClick={onToggle}
        aria-pressed={voiceMode}
        aria-label={
          voiceMode
            ? t("nexus.voice_stop")
            : t("nexus.voice_start")
        }
        style={{ width: size, height: size }}
      >
        <svg
          width={size}
          height={size}
          viewBox={`0 0 ${size} ${size}`}
          role="img"
          aria-label={label}
        >
          <defs>
            <radialGradient id="nx-core-fill" cx="50%" cy="50%" r="50%">
              <stop offset="0%" stopColor={color} stopOpacity="0.16" />
              <stop offset="70%" stopColor={color} stopOpacity="0.05" />
              <stop offset="100%" stopColor={color} stopOpacity="0" />
            </radialGradient>
          </defs>

          {/* outer tick ring: 48 ticks, emphasized every 4th */}
          <g>
            {Array.from({ length: 48 }).map((_, i) => {
              const ang = (i / 48) * Math.PI * 2;
              const long = i % 4 === 0;
              const rIn = r1 - (long ? 14 : 10);
              const rOut = r1 - 4;
              const cos = Math.cos(ang);
              const sin = Math.sin(ang);
              return (
                <line
                  key={`t${i}`}
                  x1={r1 + cos * rIn}
                  y1={r1 + sin * rIn}
                  x2={r1 + cos * rOut}
                  y2={r1 + sin * rOut}
                  stroke={color}
                  strokeOpacity={long ? 0.55 : 0.22}
                  strokeWidth={long ? 1.6 : 1}
                />
              );
            })}
          </g>

          {/* rotating dashed ring */}
          <g style={{ transformOrigin: "50% 50%", animation: "nx-rot 26s linear infinite" }}>
            <circle
              cx={r1}
              cy={r1}
              r={r1 * 0.82}
              fill="none"
              stroke={color}
              strokeOpacity="0.4"
              strokeWidth="1"
              strokeDasharray="3 9"
            />
          </g>

          {/* solid inner arcs (two, offset) */}
          <circle
            cx={r1}
            cy={r1}
            r={r1 * 0.66}
            fill="url(#nx-core-fill)"
            stroke={color}
            strokeOpacity="0.75"
            strokeWidth="2"
          />
          <circle
            cx={r1}
            cy={r1}
            r={r1 * 0.55}
            fill="none"
            stroke="#0f8457"
            strokeOpacity={state === "thinking" ? 0.25 : 0.5}
            strokeWidth="1.2"
          />

          {/* crosshair */}
          <g stroke={color} strokeOpacity="0.8" strokeWidth="1.4">
            <line x1={r1 - 14} y1={r1} x2={r1 + 14} y2={r1} />
            <line x1={r1} y1={r1 - 14} x2={r1} y2={r1 + 14} />
          </g>
          <circle cx={r1} cy={r1} r={active ? 7 : 5.5} fill={color}>
            {active && (
              <>
                <animate attributeName="r" values="6;8.5;6" dur="1.6s" repeatCount="indefinite" />
                <animate attributeName="opacity" values="1;0.6;1" dur="1.6s" repeatCount="indefinite" />
              </>
            )}
          </circle>

          {/* identifier */}
          <text
            x={r1}
            y={r1 - r1 * 0.32}
            textAnchor="middle"
            fontSize={Math.round(size / 34)}
            fontFamily="var(--nx-mono)"
            letterSpacing="3"
            fill="var(--nx-dim)"
          >
            ZAYTOON
          </text>
        </svg>

        {/* orbiting live readouts */}
        <div style={{ position: "absolute", inset: 0, pointerEvents: "none" }} aria-hidden="true">
          {markers.map((value, i) => {
            const ang = -Math.PI / 2 + (i / 6) * Math.PI * 2;
            const rr = size * 0.47;
            const cx = size / 2 + Math.cos(ang) * rr;
            const cy = size / 2 + Math.sin(ang) * rr;
            return (
              <span
                key={`m${i}`}
                style={{
                  position: "absolute",
                  left: cx,
                  top: cy,
                  transform: "translate(-50%, -50%)",
                  fontSize: 8,
                  fontFamily: "var(--nx-mono)",
                  color: "var(--nx-dim)",
                  background: "rgba(4,9,16,0.7)",
                  border: "1px solid rgba(0,245,255,0.25)",
                  borderRadius: 999,
                  padding: "1px 6px",
                  whiteSpace: "nowrap",
                }}
              >
                {value}
              </span>
            );
          })}
        </div>
      </button>

      <div style={{ marginTop: 8 }}>
        <div className="nx-core-state" style={{ "--nx-hue": color }}>
          {label}
        </div>
        <p className="nx-core-hint" style={{ marginTop: 6 }}>
          {t("nexus.click_core")}
        </p>
      </div>
    </div>
  );
}

export default memo(NexusOrb);
