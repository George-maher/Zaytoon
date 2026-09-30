import { memo } from "react";

/**
 * HUDPanel — the frame every module sits in.
 *
 * Replaces the rounded translucent card with a flat, angular instrument
 * frame: a 1px lit edge, L-shaped corner brackets, a header strip with an
 * accent bar, and an optional state colour that propagates to its children
 * through `--state-color`. The notched variant clips the *frame* only, never
 * interactive content, so hit areas stay rectangular and focus rings are
 * never cut off.
 */

function Panel({
  title,
  code,
  state,
  accent,
  notch = false,
  flush = false,
  led = false,
  trailing,
  children,
  className = "",
  bodyClassName = "",
  ...rest
}) {
  const colour =
    accent || (state ? `var(--hud-${state === "offline" || state === "error" ? "red" : state === "confirming" || state === "executing" || state === "awaiting" ? "amber" : state === "listening" ? "green" : "cyan"})` : "var(--hud-cyan)");

  return (
    <section
      className={`hud-panel hud-bracket ${notch ? "hud-notch" : ""} ${flush ? "hud-panel--flush" : ""} ${className}`}
      style={{ "--state-color": colour, "--state-glow": "var(--hud-glow-soft)" }}
      {...rest}
    >
      {title && (
        <header className="hud-panel-head">
          {led && <span className="hud-led" aria-hidden="true" />}
          <span className="truncate">{title}</span>
          {code && <span className="hud-micro ml-auto shrink-0">{code}</span>}
          {trailing}
        </header>
      )}
      <div className={bodyClassName || "p-2"}>{children}</div>
    </section>
  );
}

/** A label/value telemetry row. `—` is a real answer, not a placeholder. */
export function TelemetryRow({ label, value, tone = "default", title, mono = true }) {
  const colour =
    tone === "ok" ? "var(--hud-green)"
      : tone === "warn" ? "var(--hud-amber)"
        : tone === "error" ? "var(--hud-red)"
          : tone === "accent" ? "var(--hud-cyan)"
            : "var(--hud-text)";
  return (
    <div className="hud-tele" title={title}>
      <span className="hud-tele__k">{label}</span>
      <span
        className="hud-tele__v"
        style={{ color: colour, fontFamily: mono ? "var(--font-mono)" : "var(--font-body)" }}
      >
        {value === null || value === undefined || value === "" ? "—" : value}
      </span>
    </div>
  );
}

export default memo(Panel);
