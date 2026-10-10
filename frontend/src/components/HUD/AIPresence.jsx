import { memo } from "react";
import { STATES, hueVars } from "./hudState";
import { prefersReducedMotion } from "../../lib/motion";

/**
 * AIPresence — the single animated radial element in the product.
 *
 * This is the reference's central motif: a glowing circular dial with
 * concentric rings, built from 1px strokes rather than a bitmap so it stays
 * crisp at any size and inherits the palette from tokens.
 *
 * The rule that shapes the whole component: **it is still unless the AI is
 * actually working.** `active` is passed in, and when it is false every ring
 * renders as a static hairline at reduced opacity — no rotation, no pulse, no
 * bloom. The reference image is a still of an active system; a product that
 * animated it permanently would be advertising a state it is not in, and would
 * spend the one animated-element budget on something that carries no meaning.
 *
 * Three states, deliberately distinguishable without colour:
 *   idle       static concentric rings, dim
 *   active     counter-rotating rings + a sweeping arc + a bloom
 *   offline    static, desaturated, glyph carries the state
 *
 * `size` is the outer diameter. Rings are drawn as SVG circles with
 * `strokeDasharray` so a sweep is one attribute change, not a re-layout.
 */

const RING_COUNT = 3;

function PresenceRing({ size, inset, stroke, opacity, dashFraction, speed, reverse }) {
  const r = (size / 2 - inset - stroke / 2);
  const c = 2 * Math.PI * r;
  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      className="absolute inset-0"
      aria-hidden="true"
      style={{ animation: speed ? `hud-orbit ${speed}s linear infinite ${reverse ? "reverse" : ""}` : undefined }}
    >
      <circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        fill="none"
        stroke="var(--state-color)"
        strokeWidth={stroke}
        opacity={opacity}
        strokeLinecap="butt"
        strokeDasharray={`${c * dashFraction} ${c}`}
        transform={`rotate(-90 ${size / 2} ${size / 2})`}
      />
    </svg>
  );
}

/**
 * Node marks on the outer ring. The reference sets small square nodes at
 * points around its dial; these are the same idea at 1px stroke weight, and
 * they only appear once the system is live.
 */
function NodeRing({ size, active }) {
  const marks = 12;
  const r = size / 2 - 4;
  const nodes = [];
  for (let i = 0; i < marks; i++) {
    const a = (i / marks) * Math.PI * 2;
    const x = size / 2 + Math.cos(a) * r;
    const y = size / 2 + Math.sin(a) * r;
    nodes.push(
      <circle
        key={i}
        cx={x}
        cy={y}
        r={1.4}
        fill="var(--state-color)"
        opacity={active ? 0.85 : 0.28}
      />,
    );
  }
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="absolute inset-0" aria-hidden="true">
      {nodes}
    </svg>
  );
}

function AIPresence({
  state = "idle",
  size = 44,
  active = false,
  language = "en",
  showLabel = false,
}) {
  const s = STATES[state] || STATES.idle;
  const hue = hueVars(s.hue);
  const reduced = prefersReducedMotion();
  // Motion is requested *and* granted: `active` says the AI is working, and
  // the user's OS preference gets the final word. Neither alone is enough.
  const motion = active && !reduced;

  const label = language === "ar" ? s.ar : s.en;

  return (
    <div
      className="relative flex items-center justify-center flex-none"
      style={{ width: size, height: size, ...hue }}
      role="img"
      aria-label={`${language === "ar" ? "حالة المساعد" : "AI status"}: ${label}`}
    >
      {/* Bloom. Only while active — a resting glow is decoration, and the
          brief reserves glow for active states. */}
      {motion && (
        <div
          className="absolute rounded-full"
          style={{
            width: size * 0.7,
            height: size * 0.7,
            background: "radial-gradient(circle, var(--state-glow), transparent 70%)",
            filter: "blur(10px)",
            animation: "hud-breathe 2.6s ease-in-out infinite",
          }}
          aria-hidden="true"
        />
      )}

      <NodeRing size={size} active={motion} />

      {Array.from({ length: RING_COUNT }, (_, i) => (
        <PresenceRing
          key={i}
          size={size}
          inset={7 + i * 6}
          stroke={i === 0 ? 1.6 : 1}
          opacity={motion ? 0.9 - i * 0.18 : 0.26}
          // A ring's dash fraction is its "load". Idle rings are near-closed so
          // they read as complete rims rather than as partial progress.
          dashFraction={motion ? [0.72, 0.34, 0.18][i] : 1}
          speed={motion ? [3.2, 4.6, 6.4][i] : 0}
          reverse={i % 2 === 1}
        />
      ))}

      {/* The core: a filled disc carrying the state glyph. The glyph is what
          survives greyscale and colour-blindness. */}
      <div
        className="absolute flex items-center justify-center"
        style={{
          width: size * 0.42,
          height: size * 0.42,
          borderRadius: "50%",
          background: "color-mix(in srgb, var(--bg0) 88%, transparent)",
          border: "1px solid color-mix(in srgb, var(--state-color) 55%, transparent)",
        }}
      >
        <span
          aria-hidden="true"
          style={{
            fontSize: Math.max(9, Math.round(size * 0.2)),
            color: "var(--state-color)",
            lineHeight: 1,
          }}
        >
          {s.glyph}
        </span>
      </div>

      {showLabel && (
        <span
          className="hud-micro absolute"
          style={{
            top: "100%",
            left: 0,
            marginTop: "var(--space-2)",
            whiteSpace: "nowrap",
            color: "var(--state-color)",
          }}
        >
          {s.word}
        </span>
      )}
    </div>
  );
}

export default memo(AIPresence);