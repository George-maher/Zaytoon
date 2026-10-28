import { useState, useRef, useEffect, useCallback } from "react";
import { clientMsgId } from "../lib/ids";
import {
  VOICE_STATES,
  applyVoiceState,
  canCapture,
} from "../lib/voiceState";
import { reconnectDelay } from "../lib/backoff";

const WS_URL = (() => {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const hostname = window.location.hostname;
  const port = window.location.port;
  // If served by nginx (Docker, port 3000 or 80), proxy through same origin
  if (port === "3000" || port === "80" || port === "443") {
    return `${protocol}//${hostname}${port ? ":" + port : ""}/ws`;
  }
  // Dev mode: connect directly to backend
  return `${protocol}//${hostname}:8000/ws`;
})();

let _msgId = 0;

/**
 * Simple content hash for deduplication fallback when response_id is missing
 * or when the same content arrives with a different response_id.
 */
function contentHash(text) {
  let hash = 0;
  const str = text.trim().toLowerCase();
  for (let i = 0; i < str.length; i++) {
    hash = ((hash << 5) - hash) + str.charCodeAt(i);
    hash |= 0; // Convert to 32bit integer
  }
  return hash.toString(36);
}

/**
 * Statuses the backend emits. Anything else is coerced to a real one so a
 * server-side typo (or a new status added without updating this client) can
 * never render as a literal label like "MODE PROCESSING..." in the HUD.
 *
 * `protocol.py::ServerEvent.status` does the same normalisation for the
 * desktop shell, so both clients now agree.
 */
const KNOWN_STATUSES = new Set([
  "connected",
  "disconnected",
  "listening",
  "transcribing",
  "thinking",
  "planning",
  "executing",
  "speaking",
  "done",
  "awaiting_command",
  "awaiting_confirmation",
]);

/** Fallback for a status we do not recognise. */
const FALLBACK_STATUS = "thinking";

/**
 * The client's own clock reading for a message.
 *
 * This is a *receipt* stamp taken when the message is created here — the
 * user hits Enter, or the first token of a reply lands — not a time invented
 * for content that never carried one. A server-supplied `msg.ts` still takes
 * precedence where one exists (see ChatMessage).
 */
function clockTime() {
  try {
    return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  } catch (e) {
    return "";
  }
}

export function normalizeStatus(status) {
  return KNOWN_STATUSES.has(status) ? status : FALLBACK_STATUS;
}

// ---------------------------------------------------------------------------
// Microphone capture
// ---------------------------------------------------------------------------
/** How often the VAD samples the analyser. */
const VAD_FRAME_MS = 50;
/**
 * Silence frames that end an utterance. 10 × 50 ms = 500 ms: long enough that
 * a natural pause between words does not split one command into two, short
 * enough that the assistant answers before the user wonders whether it heard.
 */
const VAD_SILENCE_FRAMES = 10;
/** Anything shorter than this is a cough or a door, not a word. */
const VAD_MIN_SPEECH_MS = 400;
/** A hard ceiling on one utterance, so a monologue still produces an answer. */
const VAD_MAX_SEGMENT_MS = 20000;

/**
 * A recorder with the codec faster-whisper decodes most reliably.
 *
 * Module scope, not a component local: it is called from a `useCallback`, and a
 * local would either be a missing dependency or rebuild the recorder whenever
 * an unrelated render happened.
 */
function createRecorder(stream) {
  const types = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus"];
  if (typeof MediaRecorder === "undefined") {
    throw new Error("MediaRecorder is unavailable in this browser");
  }
  for (const mime of types) {
    if (MediaRecorder.isTypeSupported(mime)) {
      return new MediaRecorder(stream, { mimeType: mime });
    }
  }
  console.warn("No preferred audio MIME type supported, using browser default");
  return new MediaRecorder(stream);
}

/**
 * Frames that establish which turn is the current one.
 *
 * `intent` opens a turn, `message` closes it, and `status`/`subtitle` are
 * emitted inside it. Payload frames (`token`, `audio_start`, `audio_chunk`,
 * `audio_end`) are deliberately absent: they can carry a superseded turn's
 * data, and treating one as an anchor would make the staleness guards compare a
 * turn against itself.
 */
const ANCHOR_FRAMES = new Set(["intent", "message", "status", "subtitle"]);

/**
 * One assistant bubble, in the shape the token and message branches both build.
 *
 * Factored out because a turn's tokens and its final `message` have to agree on
 * every field: when they were built separately a field added to one was missed
 * in the other, and the mismatch showed up as a bubble that changed shape (and
 * lost its timestamp) the moment the authoritative text arrived.
 *
 * `ts` is the receipt time of the first token — a real clock reading, not an
 * invented one — so a reply is never stamped with a time nobody observed (see
 * `clockTime`).
 */
function newAssistantBubble(data) {
  return {
    id: ++_msgId,
    role: "assistant",
    content: data.content,
    streaming: true,
    trace_id: data.trace_id,
    ts: clockTime(),
  };
}

/**
 * Stamp the turn a `model.completed` frame belongs to.
 *
 * Joined on `trace_id` rather than "whatever is streaming right now": the
 * backend tags each attempt with the turn's trace, so a late attempt from a
 * turn the user has already replaced cannot relabel the answer on screen. A
 * badge that names the wrong model is worse than no badge — it is a claim
 * about routing that happens to be false, and it is exactly the claim a user
 * checks when a reply looks wrong.
 *
 * Returns `prev` unchanged when nothing matches or nothing changed, so a
 * fallback chain that produced four attempts does not re-render four times.
 */
function modelBadgeFor(prev, data) {
  const trace = data.trace_id;
  if (!trace) return prev;
  for (let i = prev.length - 1; i >= 0; i -= 1) {
    const msg = prev[i];
    if (msg.role !== "assistant" || msg.trace_id !== trace) continue;
    const next = {
      ...msg,
      model: data.model || msg.model || null,
      provider: data.provider || msg.provider || null,
      latencyMs: typeof data.latency_ms === "number" ? data.latency_ms : msg.latencyMs ?? null,
      attempt: data.attempt || msg.attempt || null,
      toolCalled: typeof data.tool_called === "boolean" ? data.tool_called : msg.toolCalled ?? null,
    };
    if (
      next.model === msg.model &&
      next.provider === msg.provider &&
      next.latencyMs === msg.latencyMs &&
      next.attempt === msg.attempt &&
      next.toolCalled === msg.toolCalled
    ) {
      return prev;
    }
    const out = [...prev];
    out[i] = next;
    return out;
  }
  return prev;
}

export default function useWebSocket() {
  const [messages, setMessages] = useState([]);
  const [isListening, setIsListening] = useState(false);
  const [isSpeaking, setIsSpeaking] = useState(false);
  const [isConnected, setIsConnected] = useState(false);
  // Once this session has had a live socket, a drop is a *re*-connection.
  // The status line needs the difference: "Connecting…" on first attempt,
  // "Reconnecting…" when a working link went away.
  const [everConnected, setEverConnected] = useState(false);
  const [isTyping, setIsTyping] = useState(false);
  const [status, setStatus] = useState("disconnected");
  const [intent, setIntent] = useState(null);
  const [subtitle, setSubtitle] = useState("");
  const [voiceMode, setVoiceMode] = useState(false);
  //: What the *pipeline* is doing, as the server reports it. `voiceMode` is the
  //: user's choice; this is the truth. Keeping them apart is what lets the HUD
  //: say "working on your command" without the microphone capturing a second
  //: one, and it is the state the capture gate reads.
  const [voiceState, setVoiceState] = useState(VOICE_STATES.DISABLED);
  const [awaitingCommand, setAwaitingCommand] = useState(false);
  const [language, setLanguage] = useState(() => {
    // Restored from the device so an Arabic session survives a reload. The
    // value used to reset to "en" every time, which threw away the user's
    // choice (and the RTL layout) on every restart.
    try {
      const saved = localStorage.getItem("zaytoon-language");
      return saved === "ar" ? "ar" : "en";
    } catch (e) {
      return "en";
    }
  });
  const [reminders, setReminders] = useState([]);
  const [pendingConfirmation, setPendingConfirmation] = useState(null);
  const [speakingVoice, setSpeakingVoice] = useState(null);
  // Agent progress. `plan` is the steps the engine announced, `activeTool` is
  // what is running right now, and `activity` is the running log.
  const [plan, setPlan] = useState([]);
  const [activeTool, setActiveTool] = useState(null);
  const [activity, setActivity] = useState([]);
  // Track current trace for correlation
  const [currentTraceId, setCurrentTraceId] = useState(null);
  // -- The core runtime, over the same socket --------------------------
  // Tasks, terminal output and file changes arrive as core events relayed by
  // `api/websocket.py`. They live here rather than in a second hook because
  // they arrive on the *same* frames: a socket per concern would double the
  // connections and let the two disagree about ordering.
  // What the backend actually used for the reply in flight (the `model.*`
  // core events). Held per turn and attached to the assistant bubble, because
  // "which model answered this" is a property of the answer, not of the
  // session: a fallback mid-conversation moves it, and a settings page that
  // showed one global value would be describing a different request.
  const [modelInfo, setModelInfo] = useState(null);
  const [tasks, setTasks] = useState([]);
  const [activeTask, setActiveTask] = useState(null);
  const [terminalLines, setTerminalLines] = useState({});
  const [fileChanges, setFileChanges] = useState([]);

  const wsRef = useRef(null);
  const activeVoiceRef = useRef(null);
  const mediaRecorderRef = useRef(null);
  const mediaChunksRef = useRef([]);
  const mediaStreamRef = useRef(null);
  const reconnectTimeoutRef = useRef(null);
  const audioChunksRef = useRef([]);
  // The utterance currently being played. It used to be a local inside the
  // audio_end branch, so nothing could stop a voice that had already started —
  // the STOP control only flipped the flags while the audio kept talking.
  const audioPlayerRef = useRef(null);
  //: Voice-state refs. The capture loop runs outside React's render cycle, so
  //: it reads the *refs*, never the state values, or a segment boundary would
  //: see whatever state happened to be captured when the callback was created.
  const voiceStateRef = useRef(VOICE_STATES.DISABLED);
  //: The user's intent, which deliberately survives a reconnect: enabling voice
  //: starts a persistent wake-word listener, so a dropped socket re-arms it
  //: once the link is back. No audio is ever replayed.
  const micDesiredRef = useRef(false);
  const captureActiveRef = useRef(false);
  const startingCaptureRef = useRef(false);
  const audioGraphRef = useRef(null);
  const vadTimerRef = useRef(null);
  const speechRef = useRef({ speaking: false, start: 0 });
  const energyHistoryRef = useRef([]);
  const silenceFramesRef = useRef(0);
  const segmentStartRef = useRef(0);
  const reconnectAttemptRef = useRef(0);
  //: Set by `teardownCapture` below. Callbacks declared before it (device loss,
  //: recorder error) call through the ref so they do not have to make a
  //: function that tears down its own dependencies part of their dependency
  //: list.
  const teardownCaptureRef = useRef(null);
  //: True from "we asked the server to enable voice" until its `enabled: true`
  //: frame arrives. A reconnect races the initial `voice_state: disabled`
  //: frame against our own `voice enable`, and without this the stale frame
  //: would cancel the intent the effect just restored.
  const pendingEnableRef = useRef(false);
  const isSpeakingRef = useRef(false);
  const isProcessingRef = useRef(false);
  const renderedResponseIds = useRef(new Set());
  //: The turn the transcript is currently showing. Every frame that carries a
  //: `response_id` is checked against it, so a superseded turn's tokens or
  //: audio cannot be appended to the reply the user is actually reading. The
  //: backstop for the server-side guard: the two agree because the server
  //: stamps the id on the first frame of each turn.
  const activeResponseIdRef = useRef(null);
  //: The newest turn this client has seen *any* frame for. Audio is checked
  //: against it, not against `activeResponseIdRef`: a reply's audio arrives
  //: after its `message` frame (the transport streams speech while it writes
  //: the text, and the drain finishes last), so by the time `audio_start`
  //: lands the transcript has already finished the turn and the "active" ref
  //: is deliberately cleared.
  const latestResponseIdRef = useRef(null);
  //: Which turn's audio is currently being collected. Three states: a
  //: response id (collecting), `IGNORED` (a superseded turn's audio — dropped
  //: whole), or null (nothing in flight).
  const audioTurnRef = useRef(null);
  const renderedContentHashes = useRef(new Set());
  //: Set by the effect's cleanup. `onclose` fires *during* teardown when the
  //: socket is closed by the cleanup itself, and scheduling a reconnect there
  //: creates a socket nobody owns — after unmount, and (in tests) after the
  //: fake globals were swapped. The timer also outlived the `clearTimeout`
  //: above it, because `close()` runs after the clear.
  const disposedRef = useRef(false);
  // Sources for the turn in flight: set by the `sources` frame (which the
  // backend sends *before* the final `message`) and consumed when that
  // message lands. Held in a ref rather than dropped because the client used
  // to have no `sources` case at all — the frame fell through the switch and
  // citations never reached the transcript.
  const pendingSourcesRef = useRef([]);
  const confirmationRef = useRef(null);
  // Tools currently in flight, name → display label. See `tool_started`.
  const runningToolsRef = useRef(new Map());

  const addMessage = useCallback((role, content, meta = {}) => {
    setMessages((prev) => [
      ...prev,
      { id: ++_msgId, role, content, ts: meta.ts ?? clockTime(), ...meta },
    ]);
  }, []);

  /**
   * True when the socket is connected and safe to write to.
   *
   * This exists because the guard was previously written out eight times as
   * `wsRef.current?.readyState === WebSocket.OPEN` followed by a *second*
   * unguarded `wsRef.current.send(...)`. Reading a ref twice like that is a
   * time-of-check/time-of-use hazard, and it makes the hook impossible to test:
   * a WebSocket double that omits the static `OPEN` constant turns every one of
   * those checks into `undefined === undefined`, i.e. true, and the "safe" path
   * dereferences null.
   */
  const isSocketOpen = useCallback(() => {
    const ws = wsRef.current;
    return !!ws && ws.readyState === 1; // 1 === WebSocket.OPEN
  }, []);

  const stopAudio = useCallback(() => {
    // A stop must also discard whatever utterance is mid-collection, otherwise
    // its `audio_end` would start playing a voice the user just silenced.
    audioTurnRef.current = null;
    audioChunksRef.current = [];
    // Silence the playing utterance itself, not just the flags. Releasing the
    // blob URL here matters: once the handlers are detached the browser will
    // never call onended, so nothing else would revoke it.
    const player = audioPlayerRef.current;
    if (player) {
      audioPlayerRef.current = null;
      try {
        player.audio.pause();
      } catch (e) {}
      player.audio.onended = null;
      player.audio.onerror = null;
      try {
        URL.revokeObjectURL(player.url);
      } catch (e) {}
    }
    // Discard chunks of the in-flight utterance: a late `audio_end` must not
    // assemble the response the user just stopped.
    audioChunksRef.current = [];
    setIsSpeaking(false);
    isSpeakingRef.current = false;
    setIsTyping(false);
    isProcessingRef.current = false;
  }, []);

  /**
   * The tool the HUD should call "active": the first one still running.
   *
   * Deliberately not "the last one that finished clears everything" — with
   * more than one tool in flight, a finish for A must not blank the display
   * of B. Returns null only when nothing is running.
   */
  const nextRunningTool = useCallback(() => {
    const first = runningToolsRef.current.entries().next();
    return first.done ? null : { name: first.value[0], label: first.value[1] };
  }, []);

  const connectWebSocket = useCallback(() => {
    if (isSocketOpen()) return;
    const socket = new WebSocket(WS_URL);

    socket.onopen = () => {
      setIsConnected(true);
      setEverConnected(true);
      // A working link resets the backoff. Without this, a long session that
      // survived three drops would still hold a 4s delay for the fourth,
      // because the attempt counter only ever grew.
      reconnectAttemptRef.current = 0;
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
        reconnectTimeoutRef.current = null;
      }
    };

    socket.onmessage = (event) => {
      // A malformed frame must not throw inside the WebSocket handler. The
      // desktop shell already tolerates this (`ServerEvent.parse` returns
      // None); the browser client did not, so a single bad frame produced an
      // uncaught SyntaxError and the rest of the turn was lost.
      let data;
      try {
        data = JSON.parse(event.data);
      } catch (e) {
        console.warn("Ignoring malformed WebSocket frame", e);
        return;
      }
      if (!data || typeof data !== "object" || !data.type) return;
      
      // Track trace_id for correlation
      if (data.trace_id) {
        setCurrentTraceId(data.trace_id);
      }
      
      // Anchor frames update what "the newest turn" is. Everything else that
      // carries a `response_id` — a token, an audio frame, `audio_start` — can
      // belong to a turn that has already been superseded, so using one as the
      // anchor would drag the reference *back* to the turn the client is trying
      // to ignore and quietly defeat the two guards below.
      //
      // A list of anchors rather than a list of exceptions, because the next
      // frame type somebody adds is far more likely to be a payload than an
      // anchor, and a missed exclusion fails silently.
      //
      // The server makes this reliable: every turn emits `intent`, and every
      // reply ends with `message`/`status`, all carrying that turn's own
      // `response_id`, and `intent` precedes any token of the turn.
      if (data.response_id && ANCHOR_FRAMES.has(data.type)) {
        latestResponseIdRef.current = data.response_id;
      }

      switch (data.type) {
        case "status":
          setStatus(normalizeStatus(data.content));
          if (
            data.content === "thinking" ||
            data.content === "transcribing"
          ) {
            setAwaitingCommand(false);
          }
          setIsTyping(
            data.content === "thinking" ||
              data.content === "analyzing" ||
              data.content === "running_tools",
          );
          setIsSpeaking(data.content === "speaking");
          isSpeakingRef.current = data.content === "speaking";
          if (data.content === "done" || data.content === "listening") {
            isProcessingRef.current = false;
          }
          break;
        case "intent":
          setIntent(data.intent);
          break;
        // -- agent progress (Increment 3) -------------------------------
        // The engine emits semantic events; the backend relays them as these
        // frames. They drive the activity feed and the status ring, so a
        // multi-step tool turn shows "Creating the project" rather than an
        // undifferentiated spinner.
        case "plan":
          setPlan(Array.isArray(data.steps) ? data.steps : []);
          setStatus("planning");
          break;
        case "tool_started":
          // Several tools can be in flight at once (the engine runs them
          // sequentially, but a progress/finish for one must not clear the
          // display of another), so "what is running" is a map, and
          // `activeTool` is derived from it rather than nulled blindly.
          runningToolsRef.current.set(data.name, data.label || data.name);
          setActiveTool({ name: data.name, label: data.label || data.name });
          setStatus("executing");
          setActivity((prev) => [
            ...prev,
            {
              id: `${data.name}-${prev.length}`,
              kind: "started",
              name: data.name,
              label: data.label || data.name,
              at: Date.now(),
            },
          ]);
          break;
        case "tool_progress":
          setActivity((prev) => [
            ...prev,
            {
              id: `${data.name}-${prev.length}`,
              kind: "progress",
              name: data.name,
              message: data.message || "",
              at: Date.now(),
            },
          ]);
          break;
        case "tool_finished": {
          runningToolsRef.current.delete(data.name);
          const rest = nextRunningTool();
          setActiveTool(rest);
          setActivity((prev) => [
            ...prev,
            {
              id: `${data.name}-${prev.length}`,
              kind: "finished",
              name: data.name,
              success: Boolean(data.success),
              summary: (data.summary || "").slice(0, 200),
              at: Date.now(),
              // The backend's own measurement when it sends one; otherwise
              // deriveToolRows falls back to the client-side start stamp.
              durationMs: typeof data.duration_ms === "number" ? data.duration_ms : undefined,
            },
          ]);
          break;
        }
        case "stt_result":
          // Whisper's verdict on the input language. Adopt it so the agent
          // prompt and the TTS voice match what was actually spoken.
          if (data.language) {
            setLanguage(data.language);
          }
          setStatus("thinking");
          break;
        case "token":
          // A token from a turn this client is no longer showing is dropped.
          // It happens when a second message supersedes the first: the server
          // stops the old turn, but frames already in flight on the socket
          // arrive after the new turn's first token, and appending them
          // interleaves two answers into one bubble.
          if (data.response_id) {
            if (!activeResponseIdRef.current) {
              activeResponseIdRef.current = data.response_id;
            } else if (activeResponseIdRef.current !== data.response_id) {
              if (data.response_id !== latestResponseIdRef.current) {
                // A superseded turn still draining its buffer.
                break;
              }
              // A new turn: its `intent` frame already named it, so this is the
              // turn the user is waiting for. The previous bubble has to stop
              // being the streaming one, or this turn's tokens append to it and
              // two answers merge into one bubble — which is the very thing
              // this guard exists to prevent.
              activeResponseIdRef.current = data.response_id;
              // A new turn: the streaming bubble belongs to the turn that just
              // ended, so it must stop being the one this turn appends to.
              setMessages((prev) => {
                const last = prev[prev.length - 1];
                if (!last || last.role !== "assistant" || !last.streaming) return prev;
                return [...prev.slice(0, -1), { ...last, streaming: false }];
              });
            }
          }
          setIsTyping(true);
          setMessages((prev) => {
            const last = prev[prev.length - 1];
            if (last && last.role === "assistant" && last.streaming) {
              return [...prev.slice(0, -1), { ...last, content: last.content + data.content, trace_id: data.trace_id }];
            }
            return [...prev, newAssistantBubble(data)];
          });
          break;
        case "message": {
          // A *final* reply ends its turn. Two cases must be told apart, and
          // the turn's own state is what distinguishes them:
          //
          //   * a different id arriving while this client is still streaming
          //     another turn is that other turn's late reply — the server
          //     stops a superseded turn, but frames already on the socket
          //     arrive afterwards, and rendering one would print an answer to
          //     a question the user has moved past;
          //   * a different id arriving while nothing is streaming is simply
          //     the next turn, which must be rendered. (Comparing ids alone
          //     cannot tell these apart: ids carry no order, and a client that
          //     guessed would either drop every second answer or accept every
          //     stale one.)
          if (data.response_id && activeResponseIdRef.current
              && activeResponseIdRef.current !== data.response_id
              && isProcessingRef.current) {
            break;
          }
          setIsTyping(false);
          isProcessingRef.current = false;
          // The turn is over either way, so nothing is "active" any more; the
          // per-turn dedupe set below is what remembers the id.
          activeResponseIdRef.current = null;
          
          // Dual-layer deduplication: response_id (primary) + content hash (fallback)
          const responseId = data.response_id;
          const contentHashValue = data.content ? contentHash(data.content) : null;
          
          // Citations the `sources` frame held for this turn (it precedes
          // this frame on the wire). Taken and cleared here so a cancelled
          // turn's leftovers can never attach to the next reply.
          const turnSources = pendingSourcesRef.current;
          pendingSourcesRef.current = [];
          
          const isDuplicate = (responseId && renderedResponseIds.current.has(responseId)) ||
                             (contentHashValue && renderedContentHashes.current.has(contentHashValue));
          
          if (!isDuplicate) {
            if (responseId) renderedResponseIds.current.add(responseId);
            if (contentHashValue) renderedContentHashes.current.add(contentHashValue);
            
            setMessages((prev) => {
              // Only replace a bubble that is still *streaming*. The final
              // `message` frame is the authoritative text for the tokens that
              // preceded it, so it supersedes an in-progress bubble and nothing
              // else.
              //
              // It previously searched for the last assistant message of any
              // kind, so two consecutive complete replies collapsed into one and
              // the first was silently lost.
              const last = prev[prev.length - 1];
              if (last && last.role === "assistant" && last.streaming) {
                return [
                  ...prev.slice(0, -1),
                  {
                    ...last,
                    content: data.content,
                    streaming: false,
                    trace_id: data.trace_id,
                    // Already attached mid-stream, or held for this frame.
                    sources: last.sources || turnSources,
                  },
                ];
              }
              return [
                ...prev,
                {
                  id: ++_msgId,
                  role: "assistant",
                  content: data.content,
                  streaming: false,
                  trace_id: data.trace_id,
                  sources: turnSources,
                  ts: clockTime(),
                },
              ];
            });
          }
          break;
        }
        case "subtitle":
          setSubtitle((prev) => (prev !== data.content ? data.content : prev));
          break;
        case "sources": {
          // Citations for the current turn (≤12, sent after the prose and
          // before the final `message`). The frame carries no response_id on
          // the wire, so association is by order: it lands while this turn's
          // bubble is still streaming, and the `message` frame that follows
          // picks up anything held here when no bubble was streaming yet.
          const sourceList = Array.isArray(data.sources)
            ? data.sources.filter((s) => s && (s.url || s.title))
            : [];
          pendingSourcesRef.current = sourceList;
          setMessages((prev) => {
            const last = prev[prev.length - 1];
            // Only a *streaming* assistant bubble is known to be this turn's.
            // Attaching to the last completed message would stamp a previous
            // reply with citations from a later tool-only turn.
            if (!last || last.role !== "assistant" || !last.streaming) return prev;
            return [...prev.slice(0, -1), { ...last, sources: sourceList }];
          });
          break;
        }
        case "audio_start": {
          // Speech for a superseded turn is dropped entire: the server stops
          // synthesising at the next chunk boundary, but sentences already
          // queued (or frames already on the socket) would otherwise play
          // over the answer the user is actually waiting for. Comparing to the
          // newest id is what makes this a *turn* check rather than a guess.
          const turn = data.response_id || null;
          if (turn && latestResponseIdRef.current && turn !== latestResponseIdRef.current) {
            audioTurnRef.current = null;
            audioChunksRef.current = [];
            break;
          }
          audioTurnRef.current = turn || latestResponseIdRef.current || "current";
          audioChunksRef.current = [];
          audioChunksRef.current.format = data.format || "mpeg";
          // Server-side voice selection, surfaced so the HUD can show which
          // locale is being spoken.
          activeVoiceRef.current = data.voice || null;
          setSpeakingVoice(data.voice || null);
          setIsSpeaking(true);
          isSpeakingRef.current = true;
          break;
        }
        case "audio_chunk":
          if (audioTurnRef.current === null) break;  // dropped utterance
          audioChunksRef.current.push(data.data);
          break;
        case "audio_end": {
          if (audioTurnRef.current === null) {
            audioChunksRef.current = [];
            break;
          }
          audioTurnRef.current = null;
          const chunks = audioChunksRef.current;
          if (chunks.length > 0) {
            const decoded = [];
            for (const b64 of chunks) {
              try {
                const bin = atob(b64);
                const arr = new Uint8Array(bin.length);
                for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
                decoded.push(arr);
              } catch (e) {
                console.error("Audio chunk decode error:", e);
              }
            }
            if (decoded.length > 0) {
              const totalLen = decoded.reduce((s, a) => s + a.length, 0);
              const allBytes = new Uint8Array(totalLen);
              let offset = 0;
              for (const arr of decoded) {
                allBytes.set(arr, offset);
                offset += arr.length;
              }
              const blob = new Blob([allBytes], { type: "audio/" + (audioChunksRef.format || "mpeg") });
              const url = URL.createObjectURL(blob);
              const audio = new Audio(url);
              // Publish the player so the STOP control can reach it.
              audioPlayerRef.current = { audio, url };
              const release = () => {
                if (audioPlayerRef.current?.audio === audio) {
                  audioPlayerRef.current = null;
                }
              };
              audio.onended = () => {
                URL.revokeObjectURL(url);
                release();
                setIsSpeaking(false);
                isSpeakingRef.current = false;
              };
              audio.onerror = () => {
                URL.revokeObjectURL(url);
                release();
                setIsSpeaking(false);
                isSpeakingRef.current = false;
                console.error("Audio playback error");
              };
              audio.play().catch((e) => {
                console.error("Audio play error:", e);
                release();
                setIsSpeaking(false);
                isSpeakingRef.current = false;
              });
            } else {
              setIsSpeaking(false);
              isSpeakingRef.current = false;
            }
            audioChunksRef.current = [];
          } else {
            setIsSpeaking(false);
            isSpeakingRef.current = false;
          }
          break;
        }
        case "voice_state": {
          // The server's view of the voice pipeline, one frame per real
          // transition. It is the only thing allowed to say whether the
          // microphone may capture: the client can observe its own recorder,
          // but not whether the utterance it just sent was accepted, is being
          // transcribed, or is blocked on an approval.
          const next = applyVoiceState(
            voiceStateRef.current,
            data.state,
            (message) => console.warn(`[voice] ${message}`),
          );
          voiceStateRef.current = next;
          setVoiceState(next);
          const enabled = Boolean(data.enabled);
          if (enabled) {
            pendingEnableRef.current = false;
            setVoiceMode(true);
          } else if (pendingEnableRef.current) {
            // Our own `voice enable` is still in flight; this is the frame the
            // server emitted at connect, before it saw the request. Do not
            // cancel the listener the user just asked for.
          } else {
            // The server turned voice off (another client, a device failure, or
            // a disable this window had not sent yet). Release the microphone:
            // an enabled-looking UI with a live recorder is the exact bug that
            // made the assistant transcribe the room.
            setVoiceMode(false);
            micDesiredRef.current = false;
          }
          setAwaitingCommand(Boolean(data.awaiting_command));
          break;
        }
        case "wake_word":
          if (data.detected) {
            setAwaitingCommand(true);
            // A wake word *with* a command is followed immediately by
            // `voice_state: processing`; only the wake-only case becomes a
            // listening state, so do not claim the user must speak again.
            if (voiceStateRef.current !== VOICE_STATES.PROCESSING) {
              setStatus("awaiting_command");
            }
            isProcessingRef.current = false;
          } else {
            // The backend dropped the utterance: it did not start with the
            // wake word. Without this the app just goes quiet, which reads as
            // "broken" rather than "say my name first".
            setAwaitingCommand(false);
            isProcessingRef.current = false;
            setSubtitle("");
            addMessage(
              "system",
              "I only act on commands that start with “Zaytoon” — say “Zaytoon, what time is it?”.",
              { type: "wake_hint" },
            );
          }
          break;
        case "interrupted":
          setIsTyping(false);
          setIsSpeaking(false);
          isSpeakingRef.current = false;
          setStatus("listening");
          setSubtitle("");
          audioChunksRef.current = [];
          isProcessingRef.current = false;
          // The turn was cancelled, so no `tool_finished` is coming for what
          // was running. Close those rows out here rather than leaving a
          // chip that says "Running" forever.
          if (runningToolsRef.current.size > 0) {
            const cancelled = [...runningToolsRef.current.keys()];
            runningToolsRef.current.clear();
            setActiveTool(null);
            setActivity((prev) => [
              ...prev,
              ...cancelled.map((name, i) => ({
                id: `${name}-${prev.length + i}-cancelled`,
                kind: "finished",
                name,
                success: false,
                summary: "stopped",
                at: Date.now(),
              })),
            ]);
          }
          break;
        case "require_confirmation": {
          // High-risk tool call: the backend is blocked and waiting on us.
          setPendingConfirmation(data);
          confirmationRef.current = data;
          setStatus("awaiting_confirmation");
          isProcessingRef.current = true;
          setIsTyping(false);
          addMessage(
            "system",
            `\u26A0\uFE0F Confirmation required: ${data.text || data.summary || data.action}`,
            { type: "confirmation" },
          );
          break;
        }
        case "confirmation_result":
          confirmationRef.current = null;
          setPendingConfirmation(null);
          isProcessingRef.current = false;
          if (!data.resolved) {
            addMessage(
              "system",
              "\u26A0\uFE0F That approval request is no longer valid.",
              { type: "error" },
            );
          }
          break;
        case "reminder":
          addMessage("system", `\uD83D\uDD14 ${data.message}`, { type: "reminder" });
          break;
        case "reminder_list": {
          // The list's home is the ReminderPanel, not the transcript. This
          // used to echo into the chat as well, so the panel's own
          // connect-time refresh dropped a "No pending reminders" chip into
          // every fresh conversation — a system message about panel state
          // nobody asked about in chat. The action echoes below stay: those
          // answer a request the user actually made.
          setReminders(data.reminders || []);
          break;
        }
        case "reminder_deleted":
          if (data.success) {
            setReminders((prev) => prev.filter((r) => (r.reminder_id || r.id) !== data.id));
            addMessage("system", `\u2705 Reminder #${data.id} deleted.`, { type: "reminder" });
          } else {
            addMessage("system", `\u274C Could not delete reminder #${data.id}.`, { type: "error" });
          }
          break;
        case "reminder_done":
          if (data.success) {
            setReminders((prev) => prev.filter((r) => (r.reminder_id || r.id) !== data.id));
            addMessage("system", `\u2705 Reminder #${data.id} marked done.`, { type: "reminder" });
          } else {
            addMessage("system", `\u274C Could not find reminder #${data.id}.`, { type: "error" });
          }
          break;
        case "error":
          addMessage("system", `Error: ${data.content}`, { type: "error" });
          setIsTyping(false);
          isProcessingRef.current = false;
          break;

        // -- Core runtime events --------------------------------------
        // `CoreEvent.to_dict()` flattens the dotted type, so the frame type is
        // the literal `task.created` / `terminal.output` and the payload sits
        // alongside it. Unrecognised core events fall through the switch
        // silently, which is the point: a newer backend adding an event must
        // not break an older client.
        // -- model routing (real, from the core bus) ---------------------
        // `services/llm_client.py` publishes one `model.started` per call and
        // one outcome event per *attempt*, so a call that fell back produces
        // several. The last one to arrive is the attempt that actually served
        // the reply; a failure is recorded as a failure rather than being
        // dropped, because "which model answered" is exactly the question a
        // user asks when the reply looks wrong. This is observation of the
        // backend's real routing — no provider list is invented here.
        case "model.started":
          setModelInfo((prev) => ({
            ...(prev || {}),
            provider: data.provider || (prev && prev.provider) || null,
            tier: data.tier || (prev && prev.tier) || null,
            requestId: data.request_id || (prev && prev.requestId) || null,
            pending: true,
          }));
          break;
        case "model.completed":
          setModelInfo((prev) => ({
            ...(prev || {}),
            model: data.model || null,
            provider: data.provider || null,
            requestId: data.request_id || (prev && prev.requestId) || null,
            latencyMs: typeof data.latency_ms === "number" ? data.latency_ms : null,
            attempt: data.attempt || null,
            toolCalled: data.tool_called || null,
            ok: true,
            pending: false,
          }));
          // …and on the bubble itself, when this attempt names a turn we are
          // showing. The banner says "the last thing that ran"; the badge says
          // "this answer".
          if (data.trace_id) setMessages((prev) => modelBadgeFor(prev, data));
          break;
        case "model.failed":
          setModelInfo((prev) => ({
            ...(prev || {}),
            model: data.model || (prev && prev.model) || null,
            provider: data.provider || (prev && prev.provider) || null,
            requestId: data.request_id || (prev && prev.requestId) || null,
            reason: data.reason || data.outcome || null,
            ok: false,
            pending: false,
          }));
          break;
        case "task.created":
          setTasks((prev) => [data.task, ...prev.filter((t) => t.id !== data.task?.id)]);
          setActiveTask(data.task);
          break;
        case "task.updated":
          setTasks((prev) => {
            const known = prev.some((t) => t.id === data.task?.id);
            return known
              ? prev.map((t) => (t.id === data.task.id ? data.task : t))
              : [data.task, ...prev];
          });
          // Cleared only on a terminal state, so a task stays on screen while
          // it works and leaves when it is done.
          if (data.task?.is_terminal) {
            setActiveTask((current) => (current?.id === data.task.id ? null : current));
          } else {
            setActiveTask(data.task);
          }
          break;
        case "task.step":
          // A step event with no `step` is the plan being announced; the task's
          // own `plan` field already carries it, so there is nothing to merge.
          if (!data.step) break;
          setTasks((prev) =>
            prev.map((t) => {
              if (t.id !== data.task_id) return t;
              const steps = (t.steps || []).map((s) =>
                s.seq === data.step.seq ? data.step : s,
              );
              return { ...t, steps };
            }),
          );
          break;
        case "task.log":
          // Not added to the transcript: the transcript is user-facing prose,
          // and a log line is diagnostic. The task panel shows it.
          break;
        case "file.changed":
          // One row per path, newest first, capped: a long session touches
          // many files and this list is held in memory and rendered as a
          // count — unbounded growth for a badge would be its own bug.
          setFileChanges((prev) =>
            [
              { path: data.path, action: data.action, at: Date.now() },
              ...prev.filter((f) => f.path !== data.path),
            ].slice(0, 50),
          );
          break;
        case "terminal_output": {
          const sessionId = data.session_id;
          const entry = { text: data.text, stream: data.stream, replay: data.replay };
          // Bounded per session so a flooding dev server cannot grow the
          // React state without limit.
          setTerminalLines((prev) => {
            const existing = prev[sessionId] || [];
            const merged = data.replay ? [...existing, entry] : [...existing, entry];
            return { ...prev, [sessionId]: merged.slice(-2000) };
          });
          break;
        }
        case "task_result":
          if (data.error) {
            addMessage("system", data.error, { type: "error" });
          }
          break;
      }
    };

    socket.onclose = () => {
      setIsConnected(false);
      setStatus("disconnected");
      // A dropped socket must never leave the input latched: otherwise the
      // user can type but nothing is ever sent again.
      isProcessingRef.current = false;
      isSpeakingRef.current = false;
      setIsTyping(false);
      if (confirmationRef.current) {
        confirmationRef.current = null;
        setPendingConfirmation(null);
        addMessage("system", "\u26A0\uFE0F Connection lost, pending approval cancelled.", {
          type: "error",
        });
      }
      // The component is gone (unmount / effect teardown). Reconnecting now
      // would resurrect a socket with no owner and, worse, keep retrying
      // forever against a page that no longer exists.
      if (disposedRef.current) return;
      // Exponential, bounded, jittered. A flat 3s retry either hammered a
      // restarting backend or added three seconds to a one-second blip; the
      // counter is reset by a successful open.
      const delay = reconnectDelay(reconnectAttemptRef.current++);
      reconnectTimeoutRef.current = setTimeout(connectWebSocket, delay);
    };
    socket.onerror = () => socket.close();
    wsRef.current = socket;
  }, [addMessage, isSocketOpen, nextRunningTool]);

  useEffect(() => {
    // A fresh subscription: a previous teardown may have marked this hook
    // disposed (StrictMode double-invoke, or a dependency change).
    disposedRef.current = false;
    connectWebSocket();
    return () => {
      // Order matters: mark disposed *before* closing, because `close()` calls
      // `onclose` synchronously and that is what would schedule a reconnect.
      disposedRef.current = true;
      if (reconnectTimeoutRef.current) clearTimeout(reconnectTimeoutRef.current);
      const ws = wsRef.current;
      if (ws && isSocketOpen()) ws.close();
    };
  }, [connectWebSocket, isSocketOpen]);

  // Persist the language whenever it changes — whether the user toggled it or
  // Whisper inferred it from speech — so the next session (and the RTL layout
  // that follows it) starts where the last one left off.
  useEffect(() => {
    try {
      localStorage.setItem("zaytoon-language", language);
    } catch (e) {
      // Private mode / disabled storage: the session still works, it just
      // will not remember.
    }
  }, [language]);

  const sendMessage = useCallback(
    (text) => {
      if (!text.trim() || isProcessingRef.current) return;
      if (text.length > 8000) {
        addMessage("system", "Message too long.");
        return;
      }
      // Only latch if we can actually send. Previously the flag was set before
      // the readiness check, so a send attempted while the socket was
      // reconnecting was dropped silently AND left the input permanently
      // latched - the app looked alive but ignored everything afterwards.
      if (!isSocketOpen()) {
        addMessage("system", "Not connected - reconnecting. Try again in a moment.", {
          type: "error",
        });
        return;
      }
      isProcessingRef.current = true;
      // A cancelled previous turn may have left citations unclaimed; the new
      // turn must start with a clean slate.
      pendingSourcesRef.current = [];
      addMessage("user", text);
      // client_msg_id: a client-generated id the backend can use to drop a
      // duplicate send (double Enter, a retry after a flaky reconnect).
      // Optional server-side — its absence means "nothing to dedupe" — so it
      // must never block a send.
      wsRef.current.send(
        JSON.stringify({ type: "message", message: text, client_msg_id: clientMsgId() }),
      );
      setIsTyping(true);
    },
    [addMessage, isSocketOpen],
  );

  /** Low-level frame only. Voice mode uses this to cut off a turn that is
   *  still speaking; the STOP button wants that *plus* local silencing, so it
   *  goes through `requestStop` below. */
  const sendInterrupt = useCallback(() => {
    if (isSocketOpen()) {
      wsRef.current.send(JSON.stringify({ type: "interrupt" }));
    }
  }, [isSocketOpen]);

  /**
   * The STOP control, in one place.
   *
   * Both halves are required: silence whatever is playing *here*, and ask the
   * backend to cancel the in-flight turn (`interrupt` -> `state.cancel()` kills
   * the LLM task and the TTS task, then the server acks with `interrupted`).
   * The button previously called `stopAudio` alone, so it only flipped flags —
   * the audio kept talking and the backend kept generating.
   */
  const requestStop = useCallback(() => {
    stopAudio();
    sendInterrupt();
  }, [stopAudio, sendInterrupt]);

  // Human-in-the-loop: approve or deny a blocked high-risk tool call.
  const respondToConfirmation = useCallback(
    (approved) => {
      const pending = confirmationRef.current;
      if (!pending) return;
      if (isSocketOpen()) {
        wsRef.current.send(
          JSON.stringify({
            type: "tool_confirm",
            request_id: pending.request_id,
            approved: Boolean(approved),
          }),
        );
      } else {
        addMessage("system", "Error: not connected, cannot confirm.", { type: "error" });
      }
      addMessage(
        "system",
        approved
          ? `\u2705 Approved: ${pending.summary || pending.action}`
          : `\u274C Denied: ${pending.summary || pending.action}`,
        { type: "confirmation" },
      );
      confirmationRef.current = null;
      setPendingConfirmation(null);
      setStatus("thinking");
      isProcessingRef.current = false;
    },
    [addMessage, isSocketOpen],
  );

  // If the socket drops while a tool is blocked, never leave the UI stuck.
  const cancelPendingConfirmation = useCallback(() => {
    if (!confirmationRef.current) return;
    respondToConfirmation(false);
  }, [respondToConfirmation]);

  const sendReminderCmd = useCallback(
    (action, id) => {
      if (isSocketOpen()) {
        wsRef.current.send(
          JSON.stringify({ type: "reminder_cmd", action, id, language }),
        );
      }
    },
    [language, isSocketOpen],
  );

  /**
   * Cancel the session's active task.
   *
   * Separate from `requestStop` because the two stop different things: an
   * interrupt kills the current turn, while a task can outlive its turn (a test
   * suite keeps running after the answer was delivered). Firing an interrupt
   * and hoping the task stopped is how a test suite ends up orphaned.
   */
  const cancelTask = useCallback(() => {
    if (isSocketOpen()) {
      wsRef.current.send(JSON.stringify({ type: "task" }));
      return true;
    }
    return false;
  }, [isSocketOpen]);

  /**
   * Subscribe to one terminal session's output.
   *
   * Subscribing rather than filtering a global stream means a client is only
   * sent lines from a terminal it asked about. The server replays the recent
   * output on subscribe, so a panel opened late is not blank until the next
   * line arrives.
   */
  const watchTerminal = useCallback(
    (sessionId) => {
      if (!sessionId || !isSocketOpen()) return false;
      wsRef.current.send(JSON.stringify({ type: "terminal", session_id: sessionId }));
      return true;
    },
    [isSocketOpen],
  );

  const refreshReminders = useCallback(() => {
    sendReminderCmd("list");
  }, [sendReminderCmd]);

  /**
   * Adopt a voice state the client decided on its own (optimistically).
   *
   * Used at exactly two moments: "I have just handed an utterance to the
   * server" (PROCESSING) and "I disabled voice" (DISABLED). Both are followed
   * by the server's own frame within milliseconds; this exists so the capture
   * loop stops *immediately* rather than continuing to record its own tail
   * while the round trip completes.
   */
  const setLocalVoiceState = useCallback((next) => {
    const adopted = applyVoiceState(
      voiceStateRef.current,
      next,
      (message) => console.warn(`[voice] ${message}`),
    );
    voiceStateRef.current = adopted;
    setVoiceState(adopted);
  }, []);

  const transmitAudio = useCallback(
    (chunks, mimeType, minSize = 5000) => {
      if (!chunks || chunks.length === 0) return;
      const blob = new Blob(chunks, { type: mimeType || "audio/webm" });
      if (blob.size < minSize || blob.size > 5 * 1024 * 1024) {
        // Too short to contain a word, or larger than the server accepts.
        // Neither is an error worth a banner: the next utterance is unaffected,
        // and the capture loop has already moved on.
        console.warn(`Blob out of range: ${blob.size} bytes, type=${blob.type}`);
        isProcessingRef.current = false;
        return;
      }
      const utteranceId = clientMsgId();
      const reader = new FileReader();
      reader.onloadend = () => {
        const base64 = reader.result.split(",")[1];
        if (!isSocketOpen()) {
          // A recording belongs to the session that was live when it was made.
          // It is never queued and replayed: after a reconnect the command may
          // be stale, and the backend explicitly refuses a resent utterance id.
          isProcessingRef.current = false;
          addMessage("system", "Not connected — that recording was not sent.", {
            type: "error",
          });
          return;
        }
        addMessage("user", "\uD83C\uDFA4");
        wsRef.current.send(
          JSON.stringify({
            type: "audio",
            audio: base64,
            language,
            // The id makes a resend idempotent server-side. `isManualVoice` is
            // deliberately absent: it used to be sent as `true` on every VAD
            // segment, which told the backend to skip its wake-word gate.
            utterance_id: utteranceId,
          }),
        );
        isProcessingRef.current = true;
        setIsTyping(true);
        setStatus("transcribing");
        setLocalVoiceState(VOICE_STATES.PROCESSING);
      };
      reader.readAsDataURL(blob);
    },
    [addMessage, language, isSocketOpen, setLocalVoiceState],
  );

  /**
   * End the current recording segment.
   *
   * `transmit` says what the audio is: a finished utterance the user wants
   * handled, or a partial capture being dropped because the pipeline is busy
   * or the microphone is being turned off. A partial capture is never sent —
   * that is how a "stop" turned into a ghost command.
   *
   * The final slice arrives *after* `stop()`: `MediaRecorder` flushes the last
   * buffer through `dataavailable` as it stops. The previous code nulled that
   * handler first, which threw the tail of the utterance away and is why short
   * commands could arrive truncated.
   */
  const finalizeSegment = useCallback(
    ({ transmit = true } = {}) => {
      const mr = mediaRecorderRef.current;
      const chunks = mediaChunksRef.current;
      mediaChunksRef.current = [];
      mediaRecorderRef.current = null;

      const finish = (collected) => {
        if (transmit && collected.length > 0) {
          const mime = mr?.mimeType || "audio/webm";
          transmitAudio(collected, mime);
        }
      };

      if (!mr || mr.state === "inactive") {
        finish(chunks);
        return;
      }
      mr.onstop = () => {
        try {
          mr.ondataavailable = null;
        } catch (e) {
          /* the recorder is already gone */
        }
        finish(chunks);
      };
      mr.ondataavailable = (e) => {
        if (e.data && e.data.size > 0) chunks.push(e.data);
      };
      try {
        mr.stop();
      } catch (e) {
        finish(chunks);
      }
    },
    [transmitAudio],
  );

  /**
   * Start (or keep) a recorder for the current capture window.
   *
   * One recorder, running only while the microphone is allowed to capture.
   * `mediaChunksRef` is cleared when speech begins, so the blob that is sent
   * contains the utterance and not the silence that preceded it.
   */
  const beginSegment = useCallback(() => {
    const stream = mediaStreamRef.current;
    if (!stream) return;
    if (mediaRecorderRef.current && mediaRecorderRef.current.state !== "inactive") {
      return;
    }
    let mr;
    try {
      mr = createRecorder(stream);
    } catch (e) {
      console.error("Could not start the recorder", e);
      addMessage("system", "Could not start recording. Check your microphone.", {
        type: "error",
      });
      return;
    }
    mr.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) mediaChunksRef.current.push(e.data);
    };
    mr.onerror = (event) => {
      console.error("Recorder error", event);
      addMessage("system", "The microphone stopped working. Voice is off.", {
        type: "error",
      });
      micDesiredRef.current = false;
      setVoiceMode(false);
      setLocalVoiceState(VOICE_STATES.DISABLED);
      teardownCaptureRef.current?.();
      if (isSocketOpen()) {
        wsRef.current.send(JSON.stringify({ type: "voice", action: "disable" }));
      }
    };
    mediaChunksRef.current = [];
    try {
      mr.start(100);
    } catch (e) {
      console.error("Recorder failed to start", e);
      return;
    }
    mediaRecorderRef.current = mr;
    segmentStartRef.current = Date.now();
  }, [addMessage, isSocketOpen, setLocalVoiceState]);

  /**
   * The VAD tick.
   *
   * Three rules, each of which fixes something the browser used to get wrong:
   *
   * 1. **Capture only in a capturing state.** While the server says PROCESSING,
   *    AWAITING_CONFIRMATION, SPEAKING, RECOVERING or DISABLED — or while the
   *    client's own player is audible — the recorder is stopped and its buffer
   *    dropped. Otherwise the assistant transcribes its own speech.
   * 2. **A segment ends the recording; it does not interrupt the turn.** The old
   *    loop sent `interrupt` before every utterance, which is why speaking a
   *    second sentence killed the command the first one had produced.
   * 3. **A new segment starts on demand.** After sending, the recorder stays
   *    stopped until the state returns to STANDBY, so no audio accumulates
   *    across a turn.
   */
  const runVad = useCallback(() => {
    vadTimerRef.current = null;
    const graph = audioGraphRef.current;
    if (!captureActiveRef.current || !graph) return;

    const { analyser, dataArray, bufferLength } = graph;
    const allowed =
      micDesiredRef.current &&
      canCapture(voiceStateRef.current, { speaking: isSpeakingRef.current });

    if (!allowed) {
      if (speechRef.current.speaking) {
        speechRef.current.speaking = false;
        silenceFramesRef.current = 0;
        setIsListening(false);
      }
      finalizeSegment({ transmit: false });
      vadTimerRef.current = setTimeout(runVad, VAD_FRAME_MS);
      return;
    }

    // The recorder is created lazily and kept running for the whole capture
    // window; buffers are cleared at speech onset, not before transcription.
    beginSegment();

    analyser.getByteTimeDomainData(dataArray);
    let sum = 0;
    for (let i = 0; i < bufferLength; i++) {
      const val = (dataArray[i] - 128) / 128;
      sum += val * val;
    }
    const rms = Math.sqrt(sum / bufferLength);
    const history = energyHistoryRef.current;
    history.push(rms);
    if (history.length > 50) history.shift();
    let noiseFloor = 0.01;
    if (history.length >= 10) {
      const sorted = [...history].sort((a, b) => a - b);
      noiseFloor = Math.max(0.005, sorted[Math.floor(sorted.length * 0.2)]);
    }
    const threshold = Math.max(0.008, noiseFloor + 0.015);

    if (rms > threshold) {
      silenceFramesRef.current = 0;
      if (!speechRef.current.speaking) {
        speechRef.current.speaking = true;
        speechRef.current.start = Date.now();
        // Drop the silence that preceded the first word, so the blob the
        // server receives is the utterance and not a minute of room tone.
        mediaChunksRef.current = [];
        setIsListening(true);
        setStatus("listening");
      }
    } else if (speechRef.current.speaking) {
      silenceFramesRef.current += 1;
      const spokeFor = Date.now() - speechRef.current.start;
      const tooLong = Date.now() - segmentStartRef.current > VAD_MAX_SEGMENT_MS;
      if (silenceFramesRef.current >= VAD_SILENCE_FRAMES || tooLong) {
        speechRef.current.speaking = false;
        silenceFramesRef.current = 0;
        setIsListening(false);
        setStatus("transcribing");
        // Shorter than a word: a cough, a door, the tail of the TTS the echo
        // cancellation missed. Never sent.
        finalizeSegment({ transmit: spokeFor >= VAD_MIN_SPEECH_MS });
      }
    }

    vadTimerRef.current = setTimeout(runVad, VAD_FRAME_MS);
  }, [beginSegment, finalizeSegment]);

  /**
   * Turn voice mode on: acquire the microphone and start the wake-word listener.
   *
   * The listener is continuous for as long as voice mode is on, but the
   * recorder is not: see `runVad`. Ending a recording is never a cancellation —
   * the command it produced finishes on its own.
   */
  const startVoiceMode = useCallback(async () => {
    micDesiredRef.current = true;
    if (startingCaptureRef.current || captureActiveRef.current) {
      // A second click while `getUserMedia` is still resolving must not open a
      // second stream (two recorders, two copies of every utterance).
      setVoiceMode(true);
      return;
    }
    startingCaptureRef.current = true;

    if (isSocketOpen()) {
      pendingEnableRef.current = true;
      wsRef.current.send(JSON.stringify({ type: "voice", action: "enable" }));
    }

    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      startingCaptureRef.current = false;
      micDesiredRef.current = false;
      setVoiceMode(false);
      setLocalVoiceState(VOICE_STATES.DISABLED);
      // Actionable, per failure: "permission denied" and "no microphone" need
      // different things from the user, and a single generic sentence teaches
      // them nothing.
      const name = e && e.name ? e.name : "";
      let message = "Could not start the microphone. Voice mode is off.";
      if (name === "NotAllowedError" || name === "SecurityError") {
        message =
          "Microphone permission is blocked. Allow it in your browser's site settings, then press the mic again.";
      } else if (name === "NotFoundError" || name === "DevicesNotFoundError") {
        message = "No microphone was found. Connect one, then press the mic again.";
      } else if (name === "NotReadableError" || name === "TrackStartError") {
        message = "The microphone is in use by another application. Close it and try again.";
      }
      addMessage("system", message, { type: "error" });
      if (isSocketOpen()) {
        wsRef.current.send(JSON.stringify({ type: "voice", action: "disable" }));
      }
      return;
    }

    if (!micDesiredRef.current) {
      // Disabled while the permission prompt was open.
      stream.getTracks().forEach((t) => t.stop());
      startingCaptureRef.current = false;
      return;
    }

    mediaStreamRef.current = stream;
    // A device can disappear (headset unplugged, USB microphone removed). The
    // track ends, the recorder silently produces nothing, and the UI keeps
    // claiming to listen — so react to it explicitly.
    const [track] = stream.getAudioTracks ? stream.getAudioTracks() : [];
    if (track) {
      track.onended = () => {
        if (!micDesiredRef.current) return;
        addMessage("system", "The microphone disconnected. Voice mode is off.", {
          type: "error",
        });
        micDesiredRef.current = false;
        setVoiceMode(false);
        setLocalVoiceState(VOICE_STATES.DISABLED);
        teardownCaptureRef.current?.();
        if (isSocketOpen()) {
          wsRef.current.send(JSON.stringify({ type: "voice", action: "disable" }));
        }
      };
    }

    const AudioCtor = window.AudioContext || window.webkitAudioContext;
    let audioContext = null;
    let analyser = null;
    let source = null;
    try {
      audioContext = new AudioCtor();
      if (audioContext.state === "suspended") {
        try {
          await audioContext.resume();
        } catch (e) {
          /* autoplay policy: the analyser still works once resumed */
        }
      }
      analyser = audioContext.createAnalyser();
      analyser.fftSize = 512;
      source = audioContext.createMediaStreamSource(stream);
      source.connect(analyser);
    } catch (e) {
      console.error("Could not open the audio graph", e);
      addMessage("system", "Audio capture is unavailable in this browser.", {
        type: "error",
      });
      try {
        stream.getTracks().forEach((t) => t.stop());
      } catch (_) {}
      startingCaptureRef.current = false;
      micDesiredRef.current = false;
      setVoiceMode(false);
      setLocalVoiceState(VOICE_STATES.DISABLED);
      return;
    }

    audioGraphRef.current = {
      ctx: audioContext,
      analyser,
      source,
      stream,
      dataArray: new Uint8Array(analyser.frequencyBinCount),
      bufferLength: analyser.frequencyBinCount,
    };
    energyHistoryRef.current = [];
    silenceFramesRef.current = 0;
    speechRef.current = { speaking: false, start: 0 };
    captureActiveRef.current = true;
    startingCaptureRef.current = false;
    setVoiceMode(true);
    // Optimistic STANDBY: the server answers `voice enable` with the same
    // state, but the mic is already open and the UI should not wait a round
    // trip to say so.
    setLocalVoiceState(VOICE_STATES.STANDBY);
    beginSegment();
    vadTimerRef.current = setTimeout(runVad, VAD_FRAME_MS);
    // `language` is read through `transmitAudio`, deliberately not a dep here:
    // rebuilding the audio graph on a language change would interrupt capture.
  }, [addMessage, beginSegment, isSocketOpen, runVad, setLocalVoiceState]);

  /**
   * Stop the microphone without changing what the user asked for.
   *
   * Releases the device and drops any partial capture. Never sends the partial
   * buffer: a half-spoken sentence is not a command, and sending it is how
   * "stop recording" used to produce a ghost turn.
   */
  const teardownCapture = useCallback(() => {
    captureActiveRef.current = false;
    if (vadTimerRef.current) {
      clearTimeout(vadTimerRef.current);
      vadTimerRef.current = null;
    }
    const graph = audioGraphRef.current;
    audioGraphRef.current = null;
    if (graph) {
      try {
        graph.analyser?.disconnect();
      } catch (e) {}
      try {
        graph.source?.disconnect();
      } catch (e) {}
      try {
        graph.ctx?.close?.();
      } catch (e) {}
      try {
        graph.stream?.getTracks().forEach((t) => t.stop());
      } catch (e) {}
    }
    if (mediaRecorderRef.current) {
      const mr = mediaRecorderRef.current;
      mediaRecorderRef.current = null;
      try {
        mr.ondataavailable = null;
        mr.onstop = null;
        if (mr.state !== "inactive") mr.stop();
      } catch (e) {}
    }
    if (mediaStreamRef.current) {
      try {
        mediaStreamRef.current.getTracks().forEach((t) => t.stop());
      } catch (e) {}
      mediaStreamRef.current = null;
    }
    mediaChunksRef.current = [];
    speechRef.current = { speaking: false, start: 0 };
    silenceFramesRef.current = 0;
    energyHistoryRef.current = [];
    setIsListening(false);
  }, []);

  // The teardown is needed by callbacks declared before it (device loss, the
  // recorder error handler); a ref keeps the ordering honest without making
  // every one of them a dependency of a function that tears down its own deps.
  teardownCaptureRef.current = teardownCapture;

  /**
   * Finish the utterance in progress. *Not* a cancel and *not* a disable.
   *
   * Kept separate because the three used to be one operation: stopping the
   * recorder also interrupted the turn (killing the command just spoken) and,
   * before that, closing the socket. This sends what the user said, tells the
   * server the recording ended, and keeps voice mode on.
   */
  const stopRecording = useCallback(() => {
    if (!micDesiredRef.current) return;
    finalizeSegment({ transmit: true });
    if (isSocketOpen()) {
      wsRef.current.send(JSON.stringify({ type: "voice", action: "stop_recording" }));
    }
  }, [finalizeSegment, isSocketOpen]);

  /**
   * Turn voice mode off.
   *
   * Distinct from `stopRecording`: nothing is sent, because the user asked for
   * the microphone to stop, not for the half-sentence to become a command. The
   * WebSocket stays open — voice is a mode, not a connection — and a turn that
   * was already accepted keeps running (an explicit STOP is what cancels).
   */
  const stopVoiceMode = useCallback(() => {
    micDesiredRef.current = false;
    pendingEnableRef.current = false;
    setVoiceMode(false);
    setAwaitingCommand(false);
    setSubtitle("");
    teardownCapture();
    setLocalVoiceState(VOICE_STATES.DISABLED);
    if (isSocketOpen()) {
      wsRef.current.send(JSON.stringify({ type: "voice", action: "disable" }));
      setStatus("connected");
    } else {
      setStatus("disconnected");
    }
  }, [isSocketOpen, setLocalVoiceState, teardownCapture]);

  /**
   * Cancel the command in flight — the explicit third operation.
   *
   * Silence the speaker here, and ask the server to stop the turn and the
   * recorder work behind it. Unlike `stopRecording`, this *is* a cancellation,
   * and it is the only microphone-side action that is allowed to be one.
   */
  const cancelVoiceCommand = useCallback(() => {
    stopAudio();
    setAwaitingCommand(false);
    isProcessingRef.current = false;
    setIsTyping(false);
    setLocalVoiceState(
      micDesiredRef.current ? VOICE_STATES.STANDBY : VOICE_STATES.DISABLED,
    );
    if (isSocketOpen()) {
      wsRef.current.send(JSON.stringify({ type: "voice", action: "cancel" }));
    }
  }, [isSocketOpen, setLocalVoiceState, stopAudio]);

  // The microphone is NOT acquired automatically on connect.
  //
  // This used to call `startVoiceMode()` as soon as the socket opened, which
  // requests `getUserMedia` with no user gesture. Browsers generally prompt for
  // this, but the app was still grabbing a live microphone stream the moment it
  // loaded. Voice is opt-in via the composer's mic toggle.
  //
  // A *drop* is different: if the user had voice on, the intent survives
  // (`micDesiredRef`) and the listener re-arms once the link is back. That is
  // what makes it a persistent wake-word listener rather than a recording that
  // dies with the socket. No audio is replayed — a recording belongs to the
  // session that was live when it was made.
  useEffect(() => {
    if (!isConnected) {
      if (captureActiveRef.current) {
        // Release the device while there is nowhere to send audio, but keep
        // the intent so the reconnect can resume.
        teardownCapture();
      }
      setVoiceMode(false);
      setLocalVoiceState(VOICE_STATES.DISABLED);
      return;
    }
    if (micDesiredRef.current && !captureActiveRef.current) {
      startVoiceMode();
    }
  }, [isConnected, setLocalVoiceState, startVoiceMode, teardownCapture]);

  useEffect(() => {
    return () => {
      micDesiredRef.current = false;
      teardownCapture();
      if (isSpeakingRef.current) stopAudio();
    };
  }, [stopAudio, teardownCapture]);

  const clearChat = useCallback(() => {
    setMessages([]);
    renderedResponseIds.current.clear();
    renderedContentHashes.current.clear();
    pendingSourcesRef.current = [];
    // Progress is per-turn, so clearing the transcript clears it too.
    setPlan([]);
    setActiveTool(null);
    setActivity([]);
    runningToolsRef.current.clear();
  }, []);

  return {
    messages,
    setMessages,
    isConnected,
    everConnected,
    isTyping,
    status,
    intent,
    language,
    setLanguage,
    subtitle,
    voiceMode,
    voiceState,
    awaitingCommand,
    isListening,
    isSpeaking,
    reminders,
    pendingConfirmation,
    speakingVoice,
    plan,
    activeTool,
    activity,
    sendMessage,
    sendReminderCmd,
    refreshReminders,
    stopAudio,
    requestStop,
    clearChat,
    startVoiceMode,
    stopVoiceMode,
    stopRecording,
    cancelVoiceCommand,
    addMessage,
    respondToConfirmation,
    cancelPendingConfirmation,
    currentTraceId,
    modelInfo,
    // Core runtime
    tasks,
    activeTask,
    cancelTask,
    terminalLines,
    fileChanges,
    watchTerminal,
  };
}
