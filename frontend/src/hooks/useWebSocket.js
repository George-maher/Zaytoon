import { useState, useRef, useEffect, useCallback } from "react";
import { clientMsgId } from "../lib/ids";

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
  const voiceModeRef = useRef(false);
  const isSpeakingRef = useRef(false);
  const isProcessingRef = useRef(false);
  const renderedResponseIds = useRef(new Set());
  //: The turn the transcript is currently showing. Every frame that carries a
  //: `response_id` is checked against it, so a superseded turn's tokens or
  //: audio cannot be appended to the reply the user is actually reading. The
  //: backstop for the server-side guard: the two agree because the server
  //: stamps the id on the first frame of each turn.
  const activeResponseIdRef = useRef(null);
  const renderedContentHashes = useRef(new Set());
  // Sources for the turn in flight: set by the `sources` frame (which the
  // backend sends *before* the final `message`) and consumed when that
  // message lands. Held in a ref rather than dropped because the client used
  // to have no `sources` case at all — the frame fell through the switch and
  // citations never reached the transcript.
  const pendingSourcesRef = useRef([]);
  const unsentSpeechRef = useRef(false);
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
              break;
            }
          }
          setIsTyping(true);
          setMessages((prev) => {
            const last = prev[prev.length - 1];
            if (last && last.role === "assistant" && last.streaming) {
              return [...prev.slice(0, -1), { ...last, content: last.content + data.content, trace_id: data.trace_id }];
            }
            return [
              ...prev,
              {
                id: ++_msgId,
                role: "assistant",
                content: data.content,
                streaming: true,
                trace_id: data.trace_id,
                // Stamp on the first token of the turn: the receipt time of
                // the reply, not an invented one (see clockTime).
                ts: clockTime(),
              },
            ];
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
        case "audio_start":
          audioChunksRef.current = [];
          audioChunksRef.format = data.format || "mpeg";
          // Server-side voice selection, surfaced so the HUD can show which
          // locale is being spoken.
          activeVoiceRef.current = data.voice || null;
          setSpeakingVoice(data.voice || null);
          setIsSpeaking(true);
          isSpeakingRef.current = true;
          break;
        case "audio_chunk":
          audioChunksRef.current.push(data.data);
          break;
        case "audio_end": {
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
        case "wake_word":
          if (data.detected) {
            setAwaitingCommand(true);
            setStatus("awaiting_command");
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
          const rlist = data.reminders || [];
          setReminders(rlist);
          if (rlist.length === 0) {
            addMessage("system", "\uD83D\uDCCB No pending reminders.", { type: "reminder" });
          } else {
            const lines = rlist.map((r, i) => {
              const task = r.task || r.title || "?";
              const t = r.scheduled_time || r.remind_time || "";
              const rec = r.recurring && r.recurring !== "none" ? ` (${r.recurring})` : "";
              return `#${r.reminder_id || r.id || i + 1}: ${task} @ ${t}${rec}`;
            });
            addMessage("system", `\uD83D\uDCCB Reminders:\n${lines.join("\n")}`, { type: "reminder" });
          }
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
      reconnectTimeoutRef.current = setTimeout(connectWebSocket, 3000);
    };
    socket.onerror = () => socket.close();
    wsRef.current = socket;
  }, [addMessage, isSocketOpen, nextRunningTool]);

  useEffect(() => {
    connectWebSocket();
    return () => {
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

  const transmitAudio = useCallback(
    (chunks, mimeType, minSize = 5000, isManualVoice = false) => {
      if (!chunks || chunks.length === 0) return;
      const blob = new Blob(chunks, { type: mimeType || "audio/webm" });
      if (blob.size < minSize || blob.size > 5 * 1024 * 1024) {
        console.warn(`Blob out of range: ${blob.size} bytes, type=${blob.type}`);
        isProcessingRef.current = false;
        return;
      }
      const reader = new FileReader();
      reader.onloadend = () => {
        const base64 = reader.result.split(",")[1];
        if (isSocketOpen()) {
          addMessage("user", "\uD83C\uDFA4");
          wsRef.current.send(JSON.stringify({ type: "audio", audio: base64, language, isManualVoice }));
          setIsTyping(true);
          setStatus("transcribing");
        } else {
          isProcessingRef.current = false;
        }
      };
      reader.readAsDataURL(blob);
    },
    [addMessage, language, isSocketOpen],
  );

  // Prefers opus/webm because that is what faster-whisper decodes most
  // reliably. The selected MIME type is reported by the out-of-range warning
  // in transmitAudio when something is actually wrong, so it is not logged on
  // every recording.
  const _createRecorder = (stream) => {
    const types = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus"];
    for (const mime of types) {
      if (MediaRecorder.isTypeSupported(mime)) {
        return new MediaRecorder(stream, { mimeType: mime });
      }
    }
    console.warn("No preferred audio MIME type supported, using browser default");
    return new MediaRecorder(stream);
  };

  const startVoiceMode = useCallback(async () => {
    voiceModeRef.current = true;
    setVoiceMode(true);
    setStatus("listening");
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      addMessage("system", "Microphone permission denied.");
      voiceModeRef.current = false;
      setVoiceMode(false);
      return;
    }
    mediaStreamRef.current = stream;
    mediaChunksRef.current = [];
    const mr = _createRecorder(stream);
    mr.ondataavailable = (e) => {
      if (e.data.size > 0) mediaChunksRef.current.push(e.data);
    };
    mr.start(100);
    mediaRecorderRef.current = mr;

    const _restartRecorder = () => {
      if (!mediaStreamRef.current) return;
      const newMr = _createRecorder(mediaStreamRef.current);
      newMr.ondataavailable = (e) => {
        if (e.data.size > 0) mediaChunksRef.current.push(e.data);
      };
      newMr.start(100);
      mediaRecorderRef.current = newMr;
    };

    const audioContext = new (window.AudioContext || window.webkitAudioContext)();
    if (audioContext.state === "suspended") {
      try {
        await audioContext.resume();
      } catch (e) {}
    }
    const analyser = audioContext.createAnalyser();
    analyser.fftSize = 512;
    const bufferLength = analyser.frequencyBinCount;
    const dataArray = new Uint8Array(bufferLength);
    const source = audioContext.createMediaStreamSource(stream);
    source.connect(analyser);
    let energyHistory = [],
      silenceFrames = 0,
      isSpeechDetected = false,
      speechStartTime = 0,
      lastProcessTime = 0,
      hasSpokenSinceLastSend = false;

    const vadProcess = () => {
      const now = Date.now();
      if (!voiceModeRef.current || now - lastProcessTime < 50) {
        if (voiceModeRef.current) requestAnimationFrame(vadProcess);
        return;
      }
      lastProcessTime = now;
      if (!voiceModeRef.current) {
        analyser.disconnect();
        source.disconnect();
        audioContext.close();
        return;
      }

      analyser.getByteTimeDomainData(dataArray);
      let sum = 0;
      for (let i = 0; i < bufferLength; i++) {
        const val = (dataArray[i] - 128) / 128;
        sum += val * val;
      }
      const rms = Math.sqrt(sum / bufferLength);
      energyHistory.push(rms);
      if (energyHistory.length > 50) energyHistory.shift();
      let noiseFloor = 0.01;
      if (energyHistory.length >= 10) {
        const sorted = [...energyHistory].sort((a, b) => a - b);
        noiseFloor = Math.max(0.005, sorted[Math.floor(sorted.length * 0.2)]);
      }
      const threshold = Math.max(0.008, noiseFloor + 0.015);

if (rms > threshold) {
          silenceFrames = 0;
          if (!isSpeechDetected) {
            isSpeechDetected = true;
            hasSpokenSinceLastSend = true;
            unsentSpeechRef.current = true;
            speechStartTime = Date.now();
            setIsListening(true);
            setStatus("listening");
          }
      } else {
        silenceFrames++;
        if (silenceFrames > 25 && isSpeechDetected) {
          isSpeechDetected = false;
          setIsListening(false);
          // Must be a status the backend actually emits. It used to be
          // "processing...", which is not in the backend's vocabulary, so the
          // HUD rendered it literally as "MODE PROCESSING..." with a
          // default-coloured dot.
          setStatus("transcribing");
          if (
            mediaChunksRef.current.length > 0 &&
            !isProcessingRef.current &&
            speechStartTime > 0 &&
            Date.now() - speechStartTime > 400
          ) {
            isProcessingRef.current = true;
            if (hasSpokenSinceLastSend) {
              sendInterrupt();
              hasSpokenSinceLastSend = false;
            }
            unsentSpeechRef.current = false;
            const chunks = [...mediaChunksRef.current];
            const mime = mediaRecorderRef.current?.mimeType || "audio/webm";
            if (mediaRecorderRef.current?.state !== "inactive") {
              mediaRecorderRef.current.ondataavailable = null;
              mediaRecorderRef.current.stop();
            }
            _restartRecorder();
            mediaChunksRef.current = [];
            transmitAudio(chunks, mime, 5000, true);
          }
        }
      }
      if (voiceModeRef.current) requestAnimationFrame(vadProcess);
    };
    requestAnimationFrame(vadProcess);
    // `language` is intentionally absent: the VAD loop never reads it, and
    // including it would tear down and rebuild the audio graph on every
    // language change. `transmitAudio` already closes over it.
  }, [addMessage, sendInterrupt, transmitAudio]);

  const stopVoiceMode = useCallback(() => {
    voiceModeRef.current = false;
    setVoiceMode(false);
    setIsListening(false);
    setSubtitle("");
    const chunks = [...mediaChunksRef.current];
    const mime = mediaRecorderRef.current?.mimeType || "audio/webm";
    if (mediaRecorderRef.current?.state !== "inactive") {
      try {
        mediaRecorderRef.current.ondataavailable = null;
        mediaRecorderRef.current.stop();
      } catch (e) {}
      mediaRecorderRef.current = null;
    }
    if (mediaStreamRef.current) {
      mediaStreamRef.current.getTracks().forEach((t) => t.stop());
      mediaStreamRef.current = null;
    }
    mediaChunksRef.current = [];
    const aiBusy = isSpeakingRef.current || isProcessingRef.current;
    if (chunks.length > 0 && (unsentSpeechRef.current || !aiBusy)) {
      const shouldInterrupt = unsentSpeechRef.current;
      unsentSpeechRef.current = false;
      isProcessingRef.current = true;
      if (shouldInterrupt) sendInterrupt();
      transmitAudio(chunks, mime, 2000, true);
    }
    setStatus(isSocketOpen() ? "connected" : "disconnected");
  }, [sendInterrupt, transmitAudio, isSocketOpen]);

  // The microphone is NOT acquired automatically on connect.
  //
  // This used to call `startVoiceMode()` as soon as the socket opened, which
  // requests `getUserMedia` with no user gesture. Browsers generally prompt for
  // this, but the app was still grabbing a live microphone stream the moment it
  // loaded, and the recording indicator stayed on for users who only wanted to
  // type. Voice is now opt-in via the existing dock toggle.
  useEffect(() => {
    if (!isConnected && voiceModeRef.current) {
      stopVoiceMode();
    }
  }, [isConnected, stopVoiceMode]);

  useEffect(() => {
    return () => {
      voiceModeRef.current = false;
      if (mediaRecorderRef.current?.state !== "inactive") {
        try {
          mediaRecorderRef.current.stop();
        } catch (e) {}
        mediaRecorderRef.current = null;
      }
      if (mediaStreamRef.current) {
        mediaStreamRef.current.getTracks().forEach((t) => t.stop());
        mediaStreamRef.current = null;
      }
      mediaChunksRef.current = [];
      if (isSpeakingRef.current) stopAudio();
    };
  }, [stopAudio]);

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
