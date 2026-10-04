import { memo, useEffect, useRef, useState } from "react";
import { STATES, hueVars } from "./hudState";

/**
 * AICore — the visual centre of the command deck.
 *
 * Layers, outermost in:
 *   segmented ring   60 ticks, a fixed subset lit, rotating
 *   telemetry arc    a real arc whose sweep is derived from live state
 *   sweep ring       a single hairline that spins; speed carries the state
 *   waveform band    live only while listening or speaking
 *   identity         ZAYTOON + the state word
 *   status LED       the same four-channel encoding as StatusIndicator
 *
 * Everything animated is a transform on a composited layer. No layout, no
 * filter animation, no per-frame React state: the waveform is the only thing
 * that runs JS, and it idles to a hard stop when there is nothing to show.
 */

const SEGMENTS = 60;

function Ring({ size, stroke, opacity = 1, duration, reverse = false, spin = true }) {
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="absolute inset-0" aria-hidden="true">
      <circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        fill="none"
        stroke="var(--state-color)"
        strokeWidth={stroke}
        opacity={opacity}
        strokeDasharray={c}
        strokeDashoffset={c * 0.62}
        transform={`rotate(-90 ${size / 2} ${size / 2})`}
        className={spin ? `hud-core-spin-${reverse ? "rev" : "slow"}` : undefined}
        style={spin ? { "--ring-dur": `${duration}s` } : undefined}
      />
    </svg>
  );
}

function SegmentedRing({ size, active }) {
  const ticks = [];
  const r = size / 2 - 4;
  for (let i = 0; i < SEGMENTS; i++) {
    // Lit ticks are a contiguous block, not a scatter: it reads as a
    // progress position rather than as decoration.
    const lit = active > 0 && i < active;
    const a = (i / SEGMENTS) * Math.PI * 2;
    const inner = lit ? r - 9 : r - 4;
    ticks.push(
      <line
        key={i}
        x1={size / 2 + Math.cos(a) * inner}
        y1={size / 2 + Math.sin(a) * inner}
        x2={size / 2 + Math.cos(a) * r}
        y2={size / 2 + Math.sin(a) * r}
        stroke={lit ? "var(--state-color)" : "var(--hud-edge-faint)"}
        strokeWidth={lit ? 1.6 : 1}
        opacity={lit ? 0.95 : 0.5}
      />,
    );
  }
  return <svg width={size} height={size} className="absolute inset-0" aria-hidden="true">{ticks}</svg>;
}

function WaveformBand({ active }) {
  const bars = 28;
  const [phase, setPhase] = useState(0);
  const raf = useRef(null);

  useEffect(() => {
    if (!active) return undefined;
    // The stylesheet can freeze CSS animations under reduced motion, but this
    // is a rAF loop pushing React state every frame — it has to police itself.
    // The bars keep their active shape and colour; only the per-frame motion
    // stops, which is exactly what `prefers-reduced-motion` asks for.
    const reduced =
      typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduced) return undefined;
    let stop = false;
    const tick = () => {
      if (stop) return;
      setPhase((p) => (p + 1) % 360);
      raf.current = requestAnimationFrame(tick);
    };
    raf.current = requestAnimationFrame(tick);
    return () => {
      stop = true;
      if (raf.current) cancelAnimationFrame(raf.current);
    };
  }, [active]);

  return (
    <div className="flex items-end gap-[2px] h-6" aria-hidden="true">
      {Array.from({ length: bars }, (_, i) => {
        // A fixed pseudo-random shape, phase-shifted, so it reads as audio
        // without pretending to be a real measurement.
        const seed = Math.abs(Math.sin(i * 12.9898) * 43758.5453) % 1;
        const h = active ? 4 + seed * 18 * (0.6 + 0.4 * Math.sin((i / bars) * Math.PI)) : 2;
        return (
          <span
            key={i}
            className="w-[2px]"
            style={{
              height: `${h}px`,
              background: "var(--state-color)",
              opacity: active ? 0.55 + seed * 0.45 : 0.18,
              transform: active ? `translateY(${Math.sin((phase + i * 18) * 0.12) * 1.5}px)` : undefined,
              transition: "height 90ms linear, opacity 90ms linear",
            }}
          />
        );
      })}
    </div>
  );
}

function AICore({ state = "idle", size = 300, language = "en", speakingVoice = null }) {
  const s = STATES[state] || STATES.idle;
  const inner = Math.round(size * 0.62);
  const showWave = state === "listening" || state === "speaking" || state === "transcribing";
  // The telemetry arc advances with the ring rate, so a faster state visibly
  // fills faster. It is an activity indicator, not a progress bar, and it is
  // labelled as such.
  const active = state === "idle" || state === "offline" ? 0 : Math.round(SEGMENTS * 0.4);

  return (
    <div
      className="relative flex items-center justify-center"
      style={{ width: size, height: size, ...hueVars(s.hue) }}
      role="img"
      aria-label={`AI core: ${language === "ar" ? s.ar : s.en}`}
    >
      {/* Ambient bloom. One element, the only resting glow in the layout. */}
      <div
        className="absolute rounded-full"
        style={{
          width: size * 0.62,
          height: size * 0.62,
          background: "radial-gradient(circle, var(--state-glow), transparent 68%)",
          filter: "blur(18px)",
        }}
        aria-hidden="true"
      />

      <div className="hud-core-spin-slow absolute inset-0" style={{ "--ring-dur": `${s.rate * 3}s` }}>
        <SegmentedRing size={size} active={active} />
      </div>

      <div className="absolute inset-0 flex items-center justify-center">
        <Ring size={size} stroke={1} opacity={0.5} duration={s.rate * 2} />
        <div className="absolute inset-0 flex items-center justify-center">
          <Ring size={size - 26} stroke={1} opacity={0.34} duration={s.rate * 2.6} reverse />
        </div>
      </div>

      {/* Identity disc. Flat fill plus a lit edge — no blur. */}
      <div
        className="absolute flex flex-col items-center justify-center"
        style={{
          gap: "var(--space-2)",
          width: inner,
          height: inner,
          borderRadius: "50%",
          background: "color-mix(in srgb, var(--bg0) 82%, transparent)",
          border: `var(--border-w) solid var(--state-color)`,
          boxShadow: `0 0 22px var(--state-glow), inset 0 0 26px color-mix(in srgb, var(--hud-cyan) 4%, transparent)`,
        }}
      >
        <span
          className="hud-identity"
          style={{ fontSize: Math.max(12, Math.round(size * 0.052)) }}
        >
          ZAYTOON
        </span>
        <span
          className="hud-micro"
          style={{ color: "var(--state-color)", letterSpacing: "0.2em" }}
        >
          <span aria-hidden="true">{s.glyph}</span>
          <span className="ml-1.5">{s.word}</span>
        </span>
        <span className="hud-micro" style={{ opacity: 0.5, fontSize: "var(--font-xs)" }}>
          {language === "ar" ? s.ar : s.en.toUpperCase()}
        </span>
      </div>

      {/* Hairline sweep. Transform-only. */}
      <div
        className="absolute rounded-full hud-core-spin-rev"
        style={{
          width: size - 12,
          height: size - 12,
          border: "1px solid transparent",
          borderTopColor: "var(--state-color)",
          borderRightColor: "var(--state-color)",
          opacity: 0.45,
          "--ring-dur": `${s.rate}s`,
        }}
        aria-hidden="true"
      />

      {showWave && (
        <div className="absolute" style={{ bottom: size * 0.08 }}>
          <WaveformBand active />
        </div>
      )}

      {speakingVoice && state === "speaking" && (
        <span
          className="hud-micro absolute"
          style={{ top: size * 0.06, color: "var(--hud-text-mute)" }}
        >
          {speakingVoice.replace("Neural", "")}
        </span>
      )}
    </div>
  );
}

export default memo(AICore);
