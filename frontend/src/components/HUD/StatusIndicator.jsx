import { memo } from "react";
import { STATES, hueVars } from "./hudState";

/**
 * The compact state read-out: an LED and a word.
 *
 * State is carried by four redundant channels — hue, the LED glyph, the state
 * word, and the LED's fill style — so it survives colour-blindness and
 * greyscale. The vocabulary itself lives in ./hudState.js.
 */
function StatusIndicator({ state, language = "en", word, compact = false, className = "" }) {
  const s = STATES[state] || STATES.idle;
  const label = word || (language === "ar" ? s.ar : s.en);
  return (
    <span
      className={`inline-flex items-center ${className}`}
      style={{ gap: "var(--space-2)", ...hueVars(s.hue) }}
      role="status"
      /* One announcer per screen, and it is not this one: the console's
         status line (components/StatusLine) already announces the derived
         sentence for every state this word mirrors. Two polite regions
         updating together would read the same moment out loud twice.
         The word stays as readable static text for anyone navigating to it. */
      aria-live="off"
    >
      <span
        className={`hud-led${s.pulse ? " hud-led--pulse" : ""}${state === "idle" ? " hud-led--hollow" : ""}`}
        aria-hidden="true"
      />
      <span
        className="hud-micro"
        style={{ color: "var(--state-color)", letterSpacing: compact ? "0.14em" : "0.22em" }}
      >
        <span aria-hidden="true">{s.glyph}</span>
        {!compact && <span className="ml-1">{label}</span>}
        {compact && <span className="sr-only">{label}</span>}
      </span>
    </span>
  );
}

export default memo(StatusIndicator);
