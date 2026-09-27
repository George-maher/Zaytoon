import { useState, useRef, useEffect, useCallback } from "react";

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

export function normalizeStatus(status) {
  return KNOWN_STATUSES.has(status) ? status : FALLBACK_STATUS;
}

export default function useWebSocket() {
  const [messages, setMessages] = useState([]);
  const [isListening, setIsListening] = useState(false);
  const [isSpeaking, setIsSpeaking] = useState(false);
  const [isConnected, setIsConnected] = useState(false);
  const [isTyping, setIsTyping] = useState(false);
  const [status, setStatus] = useState("disconnected");
  const [intent, setIntent] = useState(null);
  const [subtitle, setSubtitle] = useState("");
  const [voiceMode, setVoiceMode] = useState(false);
  const [awaitingCommand, setAwaitingCommand] = useState(false);
  const [language, setLanguage] = useState("en");
  const [reminders, setReminders] = useState([]);
  const [pendingConfirmation, setPendingConfirmation] = useState(null);
  const [speakingVoice, setSpeakingVoice] = useState(null);

  const wsRef = useRef(null);
  const activeVoiceRef = useRef(null);
  const mediaRecorderRef = useRef(null);
  const mediaChunksRef = useRef([]);
  const mediaStreamRef = useRef(null);
  const reconnectTimeoutRef = useRef(null);
  const audioChunksRef = useRef([]);
  const voiceModeRef = useRef(false);
  const isSpeakingRef = useRef(false);
  const isProcessingRef = useRef(false);
  const renderedResponseIds = useRef(new Set());
  const unsentSpeechRef = useRef(false);
  const confirmationRef = useRef(null);

  const addMessage = useCallback((role, content, meta = {}) => {
    setMessages((prev) => [...prev, { id: ++_msgId, role, content, ...meta }]);
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
    setIsSpeaking(false);
    isSpeakingRef.current = false;
    isProcessingRef.current = false;
  }, []);

  const connectWebSocket = useCallback(() => {
    if (isSocketOpen()) return;
    const socket = new WebSocket(WS_URL);

    socket.onopen = () => {
      setIsConnected(true);
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
        case "stt_result":
          // Whisper's verdict on the input language. Adopt it so the agent
          // prompt and the TTS voice match what was actually spoken.
          if (data.language) {
            setLanguage(data.language);
          }
          setStatus("thinking");
          break;
        case "token":
          setIsTyping(true);
          setMessages((prev) => {
            const last = prev[prev.length - 1];
            if (last && last.role === "assistant" && last.streaming) {
              return [...prev.slice(0, -1), { ...last, content: last.content + data.content }];
            }
            return [
              ...prev,
              { id: ++_msgId, role: "assistant", content: data.content, streaming: true },
            ];
          });
          break;
        case "message":
          setIsTyping(false);
          isProcessingRef.current = false;
          if (data.response_id && renderedResponseIds.current.has(data.response_id)) break;
          if (data.response_id) renderedResponseIds.current.add(data.response_id);
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
                { ...last, content: data.content, streaming: false },
              ];
            }
            return [
              ...prev,
              { id: ++_msgId, role: "assistant", content: data.content, streaming: false },
            ];
          });
          break;
        case "subtitle":
          setSubtitle((prev) => (prev !== data.content ? data.content : prev));
          break;
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
              audio.onended = () => {
                URL.revokeObjectURL(url);
                setIsSpeaking(false);
                isSpeakingRef.current = false;
              };
              audio.onerror = () => {
                URL.revokeObjectURL(url);
                setIsSpeaking(false);
                isSpeakingRef.current = false;
                console.error("Audio playback error");
              };
              audio.play().catch((e) => {
                console.error("Audio play error:", e);
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
  }, [addMessage, isSocketOpen]);

  useEffect(() => {
    connectWebSocket();
    return () => {
      if (reconnectTimeoutRef.current) clearTimeout(reconnectTimeoutRef.current);
      const ws = wsRef.current;
      if (ws && isSocketOpen()) ws.close();
    };
  }, [connectWebSocket, isSocketOpen]);

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
      addMessage("user", text);
      wsRef.current.send(JSON.stringify({ type: "message", message: text }));
      setIsTyping(true);
    },
    [addMessage, isSocketOpen],
  );

  const sendInterrupt = useCallback(() => {
    if (isSocketOpen()) {
      wsRef.current.send(JSON.stringify({ type: "interrupt" }));
    }
  }, [isSocketOpen]);

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

  const refreshReminders = useCallback(() => {
    sendReminderCmd("list");
  }, [sendReminderCmd]);

  const transmitAudio = useCallback(
    (chunks, mimeType, minSize = 5000) => {
      if (!chunks || chunks.length === 0) return;
      const blob = new Blob(chunks, { type: mimeType || "audio/webm" });
      if (blob.size < minSize || blob.size > 5 * 1024 * 1024) {
        console.warn(`Blob out of range: ${blob.size} bytes, type=${blob.type}`);
        isProcessingRef.current = false;
        return;
      }
      console.log(`Sending audio blob: ${blob.size} bytes, type=${blob.type}`);
      const reader = new FileReader();
      reader.onloadend = () => {
        const base64 = reader.result.split(",")[1];
        if (isSocketOpen()) {
          console.log(`Base64 prefix: ${base64.slice(0, 8)}`);
          addMessage("user", "\uD83C\uDFA4");
          wsRef.current.send(JSON.stringify({ type: "audio", audio: base64, language }));
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

  const _createRecorder = (stream) => {
    const types = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus"];
    for (const mime of types) {
      if (MediaRecorder.isTypeSupported(mime)) {
        console.log(`Using MediaRecorder mimetype: ${mime}`);
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
            transmitAudio(chunks, mime);
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
      transmitAudio(chunks, mime, 2000);
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
  }, []);

  return {
    messages,
    setMessages,
    isConnected,
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
    sendMessage,
    sendReminderCmd,
    refreshReminders,
    stopAudio,
    clearChat,
    startVoiceMode,
    stopVoiceMode,
    addMessage,
    respondToConfirmation,
    cancelPendingConfirmation,
  };
}
