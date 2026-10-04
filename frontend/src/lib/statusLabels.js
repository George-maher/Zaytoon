/**
 * The status line: one derived label for "what is the system doing right now".
 *
 * Pure on purpose. The mapping from live hook state to a human sentence used
 * to be spread across the generic "PROCESSING..." text in the transcript and
 * the state word in the header, neither of which said *what* was happening —
 * a plan, a tool call and a streamed answer all read the same. Keeping it as
 * a pure function makes the precedence testable without mounting the app.
 *
 * Precedence, most specific first. Two rules matter:
 *
 * - A pending human approval outranks everything below it: the engine is
 *   blocked on a person, so claiming to be working would be a lie.
 * - "Planning…" keys off `status === "planning"` — the frame that sets the
 *   plan also sets that status — and never off `plan.length`, because a plan
 *   is only cleared with the transcript and would otherwise leave the line
 *   saying "Planning…" for the rest of the session.
 */

const EN = {
  ready: "Ready",
  connecting: "Connecting…",
  reconnecting: "Reconnecting…",
  understanding: "Understanding…",
  thinking: "Thinking…",
  planning: "Planning…",
  calling: (label) => `Calling ${label}…`,
  generating: "Generating response…",
  speaking: "Speaking…",
  approval: "Waiting for your approval…",
  listening: "Listening…",
  transcribing: "Transcribing…",
  awaiting: "Say your command…",
  offline: "Offline",
};

const AR = {
  ready: "جاهز",
  connecting: "أتّصل…",
  reconnecting: "أعيد الاتصال…",
  understanding: "أفهم…",
  thinking: "أفكّر…",
  planning: "أخطط…",
  calling: (label) => `أستدعي ${label}…`,
  generating: "أكتب الرد…",
  speaking: "أتكلّم…",
  approval: "في انتظار موافقتك…",
  listening: "أستمع…",
  transcribing: "أحوّل الصوت…",
  awaiting: "قل أمرك…",
  offline: "غير متصل",
};

/**
 * @param {object} s
 * @param {boolean} s.connected         socket open
 * @param {boolean} [s.everConnected]   has this session ever been open
 * @param {string}  [s.status]          backend status frame (normalised)
 * @param {string|null} [s.intent]      router intent
 * @param {string[]|number} [s.plan]    announced plan steps
 * @param {{name?:string,label?:string}|null} [s.activeTool]
 * @param {boolean} [s.streaming]       tokens are arriving right now
 * @param {boolean} [s.isSpeaking]
 * @param {boolean} [s.isTyping]
 * @param {object|null} [s.pendingConfirmation]
 * @param {"en"|"ar"} [s.language]
 * @returns {string}
 */
export function statusLabel({
  connected = false,
  everConnected = true,
  status = "",
  intent = null,
  plan = [],
  activeTool = null,
  streaming = false,
  isSpeaking = false,
  isTyping = false,
  pendingConfirmation = null,
  language = "en",
} = {}) {
  const t = language === "ar" ? AR : EN;
  const hasPlan = Array.isArray(plan) ? plan.length > 0 : Number(plan) > 0;

  // Nothing can happen without a socket, and the reconnect loop is already
  // running, so this is what the user is actually waiting on.
  if (!connected) return everConnected ? t.reconnecting : t.connecting;

  // Blocked on a person: above every "working" state by design.
  if (pendingConfirmation || status === "awaiting_confirmation") return t.approval;

  if (isSpeaking || status === "speaking") return t.speaking;

  // Tokens are already flowing — the most recent thing that is happening.
  if (streaming) return t.generating;

  if (status === "planning") return t.planning;

  if (
    activeTool &&
    (status === "executing" || intent === "tools" || intent === "action")
  ) {
    return t.calling(activeTool.label || activeTool.name || "…");
  }

  // Executing with no tool in hand: the engine is between steps of an
  // announced plan (say so), or simply working (the honest generic word).
  if (status === "executing") return hasPlan ? t.planning : t.thinking;

  if (status === "transcribing") return t.transcribing;

  if (status === "thinking" || status === "analyzing" || isTyping) {
    // The router's intent is what distinguishes "understanding a request"
    // from "the model is working" — when there is one, say so.
    return intent ? t.understanding : t.thinking;
  }

  if (status === "awaiting_command") return t.awaiting;

  if (status === "listening" || status === "connected" || status === "done" || !status) {
    // A finished turn keeps its plan on screen (it is only cleared with the
    // transcript), but the line must go back to Ready.
    return t.ready;
  }

  // `status === "disconnected"` *with an open socket* is the stale initial
  // value: the hook deliberately invents no status on open — the server
  // sends one — so link truth comes from `connected` (handled at the top) and
  // an unanswered first frame reads as Ready. It must never fall through to
  // "Reconnecting…" while the socket is in fact live.
  return t.ready;
}

export default statusLabel;
