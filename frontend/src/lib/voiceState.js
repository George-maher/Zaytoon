/**
 * The browser half of the voice state machine.
 *
 * The server owns the truth (`services/voice_session.py` emits one `voice_state`
 * frame per real transition) and this module mirrors the same seven states so
 * the client can answer one question correctly: *may the microphone be
 * capturing right now?* That question used to be answered by three unrelated
 * booleans spread through the hook, which is how the client came to send
 * `isManualVoice: true` on every VAD segment (bypassing the server's wake-word
 * gate) and to keep a recorder running while the assistant was speaking (so it
 * transcribed its own TTS output).
 *
 * Two rules are load-bearing and both are tested:
 *
 * 1. **Capture is allowed only in a capturing state.** `STANDBY` and
 *    `LISTENING` are the only ones; `PROCESSING`, `AWAITING_CONFIRMATION`,
 *    `SPEAKING`, `RECOVERING` and `DISABLED` all suppress the microphone.
 * 2. **An unknown state suppresses capture.** A newer server may add a state
 *    this bundle has never heard of; the safe reading of "I do not know what
 *    the pipeline is doing" is "do not listen", never "listen anyway".
 *
 * The transitions are declared here as well as on the server so the client can
 * tell a *real* transition from an out-of-order frame: an illegal jump is
 * logged once and followed (the server is authoritative), but it can never
 * enable capture.
 */

export const VOICE_STATES = Object.freeze({
  DISABLED: "disabled",
  STANDBY: "standby",
  LISTENING: "listening",
  PROCESSING: "processing",
  AWAITING_CONFIRMATION: "awaiting_confirmation",
  SPEAKING: "speaking",
  RECOVERING: "recovering",
});

const S = VOICE_STATES;

/** Legal destinations per state — a mirror of `LEGAL_TRANSITIONS` on the server. */
export const LEGAL_TRANSITIONS = Object.freeze({
  [S.DISABLED]: [S.STANDBY, S.LISTENING, S.RECOVERING],
  [S.STANDBY]: [S.LISTENING, S.PROCESSING, S.SPEAKING, S.DISABLED, S.RECOVERING],
  [S.LISTENING]: [S.PROCESSING, S.SPEAKING, S.STANDBY, S.DISABLED, S.RECOVERING],
  [S.PROCESSING]: [
    S.AWAITING_CONFIRMATION,
    S.SPEAKING,
    S.STANDBY,
    S.DISABLED,
    S.RECOVERING,
  ],
  [S.AWAITING_CONFIRMATION]: [S.PROCESSING, S.SPEAKING, S.STANDBY, S.DISABLED, S.RECOVERING],
  [S.SPEAKING]: [S.STANDBY, S.PROCESSING, S.DISABLED, S.RECOVERING],
  [S.RECOVERING]: [S.STANDBY, S.PROCESSING, S.LISTENING, S.DISABLED],
});

/** States in which the microphone may collect audio. */
export const CAPTURING_STATES = Object.freeze([S.STANDBY, S.LISTENING]);

/** States in which a command has been accepted and must not be cancelled. */
export const ACTIVE_COMMAND_STATES = Object.freeze([
  S.PROCESSING,
  S.AWAITING_CONFIRMATION,
  S.SPEAKING,
]);

const KNOWN = new Set(Object.values(S));

/**
 * Coerce an incoming state into a known one.
 *
 * Unknown → `RECOVERING`: a state the client cannot reason about is treated as
 * "the pipeline is not ready for audio", which is the only safe reading. It is
 * deliberately not `STANDBY`, because that would arm the microphone on a frame
 * the client did not understand.
 */
export function normalizeVoiceState(state) {
  return KNOWN.has(state) ? state : S.RECOVERING;
}

export function canTransition(from, to) {
  const targets = LEGAL_TRANSITIONS[normalizeVoiceState(from)];
  return Array.isArray(targets) && targets.includes(normalizeVoiceState(to));
}

/**
 * Follow the server's state, noting an impossible jump.
 *
 * The server is authoritative — a client that refused a frame would drift from
 * the pipeline — so the state is always adopted. What matters is that the
 * caller cannot be *talked into* capturing by a malformed frame: capture is
 * derived from the adopted state, and an unknown state maps to `RECOVERING`.
 *
 * @param {string} previous
 * @param {string} incoming
 * @param {(message: string) => void} [warn]  called on an illegal transition
 * @returns {string}
 */
export function applyVoiceState(previous, incoming, warn) {
  const next = normalizeVoiceState(incoming);
  const prev = normalizeVoiceState(previous);
  if (prev !== next && !canTransition(prev, next)) {
    if (typeof warn === "function") {
      warn(`unexpected voice transition ${prev} -> ${next} (${String(incoming)})`);
    }
  }
  return next;
}

export function isCapturing(state) {
  return CAPTURING_STATES.includes(normalizeVoiceState(state));
}

export function isActiveCommand(state) {
  return ACTIVE_COMMAND_STATES.includes(normalizeVoiceState(state));
}

/**
 * May the microphone capture right now?
 *
 * `speaking` is the client-side playback fact (the server's SPEAKING frame
 * precedes the audio and its STANDBY arrives after the drain, but the client's
 * own player is the only thing that knows a chunk is still audible). Either
 * being true suppresses capture: the assistant must never transcribe itself.
 */
export function canCapture(state, { speaking = false } = {}) {
  if (speaking) return false;
  return isCapturing(state);
}

/**
 * The badge beside the composer. Short by design: the sentence lives in
 * `statusLabels.js`, this is the one- or two-word indicator.
 *
 * @returns {{ key: string, text: string, tone: "idle"|"active"|"busy"|"wait"|"error" }}
 */
export function voiceStateBadge(state, language = "en") {
  const ar = language === "ar";
  switch (normalizeVoiceState(state)) {
    case S.STANDBY:
      return { key: "standby", text: ar ? "جاهز" : "READY", tone: "active" };
    case S.LISTENING:
      return { key: "listening", text: ar ? "أسجل" : "REC", tone: "active" };
    case S.PROCESSING:
      return { key: "processing", text: ar ? "أعمل" : "WORK", tone: "busy" };
    case S.AWAITING_CONFIRMATION:
      return { key: "awaiting_confirmation", text: ar ? "موافقة" : "WAIT", tone: "wait" };
    case S.SPEAKING:
      return { key: "speaking", text: ar ? "أتكلّم" : "TTS", tone: "busy" };
    case S.RECOVERING:
      return { key: "recovering", text: ar ? "خطأ" : "ERR", tone: "error" };
    case S.DISABLED:
    default:
      return { key: "disabled", text: "", tone: "idle" };
  }
}

export default VOICE_STATES;
