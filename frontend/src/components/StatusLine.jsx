import { memo } from "react";

/**
 * The live status line: one sentence for "what is the system doing right now".
 *
 * Presentation only — the sentence itself is produced by `lib/statusLabels`,
 * a pure function whose precedence (approval > speaking > streaming > …) is
 * unit-tested without mounting anything. The chrome comes from the shared
 * `.hud-status-line` / `.hud-led` definitions in theme.css, so this line and
 * the rest of the instrument speak in the same visual language.
 *
 * `role="status"` + `aria-live="polite"`: a screen-reader user hears the turn
 * progress (thinking → calling a tool → speaking) without polling the HUD.
 * The colour is never the only channel — the LED is decorative and the
 * sentence carries the meaning.
 */

const TONES = {
  accent: "var(--hud-cyan)",
  ok: "var(--hud-green)",
  warn: "var(--hud-amber)",
  danger: "var(--hud-red)",
};

function StatusLine({ text, language = "en", tone = "accent" }) {
  if (!text) return null;
  const ar = language === "ar";
  const color = TONES[tone] || TONES.accent;

  return (
    <div
      role="status"
      aria-live="polite"
      className="hud-status-line"
      style={{ "--state-color": color }}
    >
      <span className="hud-led" aria-hidden="true" />
      <span className="flex-none">{ar ? "الحالة" : "STATUS"}</span>
      <span className="truncate" style={{ textTransform: "none" }}>
        {text}
      </span>
    </div>
  );
}

export default memo(StatusLine);
