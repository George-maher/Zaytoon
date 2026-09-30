/**
 * The canonical state vocabulary of the HUD.
 *
 * Split out from the component so that `StatusIndicator.jsx` exports only a
 * component: mixing constants into a component module breaks React Fast
 * Refresh (`react-refresh/only-export-components`), and it is the right shape
 * anyway -- this is data, not a view.
 *
 * `resolveState` is the single reducer from live hook state to one state
 * name. Both clients use the same precedence, and the desktop mirrors this
 * table in desktop/app/theme.py, so "what does the UI think is happening"
 * has exactly one answer.
 */

export const STATES = {
  offline: { word: "OFFLINE", en: "Offline", ar: "غير متصل", hue: "red", glyph: "○", rate: 0, pulse: true },
  idle: { word: "READY", en: "Ready", ar: "جاهز", hue: "cyan", glyph: "●", rate: 12, pulse: false },
  listening: { word: "LISTENING", en: "Listening", ar: "بستمع", hue: "green", glyph: "◉", rate: 4, pulse: true },
  transcribing: { word: "TRANSCRIBING", en: "Transcribing", ar: "بفهم", hue: "cyan", glyph: "◌", rate: 2, pulse: true },
  thinking: { word: "PROCESSING", en: "Processing", ar: "بفكر", hue: "cyan", glyph: "◐", rate: 1.5, pulse: true },
  planning: { word: "PLANNING", en: "Planning", ar: "يخطط", hue: "blue", glyph: "◇", rate: 2, pulse: true },
  executing: { word: "RUNNING TOOL", en: "Running tool", ar: "بينفّذ", hue: "amber", glyph: "▲", rate: 2, pulse: true },
  speaking: { word: "SPEAKING", en: "Speaking", ar: "بتكلم", hue: "cyan", glyph: "▶", rate: 3, pulse: true },
  confirming: { word: "AWAIT CONFIRM", en: "Awaiting approval", ar: "في انتظار موافقتك", hue: "amber", glyph: "!", rate: 2, pulse: true },
  awaiting: { word: "AWAIT COMMAND", en: "Say your command", ar: "قول الأمر", hue: "amber", glyph: "?", rate: 6, pulse: false },
  // A distinct glyph from `confirming`, which is also "!"-shaped: the two are
  // both amber and the word is the only other channel, so sharing a glyph
  // would have left a red "!" and an amber "!" indistinguishable at a glance.
  error: { word: "ERROR", en: "Error", ar: "خطأ", hue: "red", glyph: "✕", rate: 1, pulse: true },
};

export const HUES = {
  cyan: { color: "var(--hud-cyan)", glow: "var(--hud-glow-soft)" },
  blue: { color: "var(--hud-blue)", glow: "rgba(22, 141, 255, 0.22)" },
  amber: { color: "var(--hud-amber)", glow: "var(--hud-amber-glow)" },
  green: { color: "var(--hud-green)", glow: "var(--hud-green-glow)" },
  red: { color: "var(--hud-red)", glow: "var(--hud-red-glow)" },
};

/** The CSS custom properties a hue contributes to a subtree. */
export function hueVars(hue) {
  const h = HUES[hue] || HUES.cyan;
  return { "--state-color": h.color, "--state-glow": h.glow };
}

/**
 * Reduce live hook state to one canonical state name.
 *
 * Precedence matters: a pending human approval outranks "thinking", or the UI
 * claims to be working while it is actually blocked waiting for a person.
 */
export function resolveState({
  isConnected,
  pendingConfirmation,
  awaitingCommand,
  isListening,
  isTyping,
  isSpeaking,
  status,
  lastError,
} = {}) {
  if (!isConnected) return "offline";
  if (pendingConfirmation) return "confirming";
  if (awaitingCommand) return "awaiting";
  if (isListening) return "listening";
  if (status === "executing") return "executing";
  if (status === "planning") return "planning";
  if (status === "transcribing") return "transcribing";
  if (isTyping || status === "thinking") return "thinking";
  if (isSpeaking) return "speaking";
  if (lastError) return "error";
  return "idle";
}
