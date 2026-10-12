import { memo, useCallback, useEffect, useRef, useState } from "react";
import ChatMessage from "./ChatMessage";
import InputBar from "./InputBar";
import ActivityFeed from "./Chat/ActivityFeed";
import StatusLine from "./StatusLine";
import AIPresence from "./HUD/AIPresence";
import { resolveState } from "./HUD/hudState";

/**
 * ChatView — the dedicated conversation screen.
 *
 * Extracted from the Workspace shell so the conversation is a first-class
 * destination, not a panel you reach by opening a project. The view carries:
 *
 * - **Full-height transcript** — every message, streaming in real time.
 * - **Multiline composer** — Enter sends, Shift+Enter inserts a newline.
 * - **Code blocks with copy** — fenced code blocks are detected, rendered in
 *   monospace with a copy button.
 * - **Tool activity feed** — the plan and per-tool rows, folded compactly.
 * - **Status line** — the derived "what is happening now" sentence.
 *
 * The view reads the same WebSocket state as the Workspace console, so a
 * conversation started here is visible there and vice versa.
 */

function CodeBlock({ code, language }) {
  const [copied, setCopied] = useState(false);

  const copy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard unavailable (non-secure context); the code is still readable.
    }
  }, [code]);

  return (
    <div
      style={{
        margin: "var(--space-2) 0",
        border: "var(--border-w) solid var(--hud-edge-faint)",
        background: "color-mix(in srgb, var(--hud-void) 60%, transparent)",
      }}
    >
      <div
        className="flex items-center"
        style={{
          padding: "var(--space-1) var(--space-2)",
          borderBottom: "var(--border-w) solid var(--hud-edge-faint)",
          gap: "var(--space-2)",
        }}
      >
        <span
          className="hud-micro"
          style={{ fontSize: "var(--font-xs)", color: "var(--hud-text-mute)", letterSpacing: "0.12em" }}
        >
          {language || "CODE"}
        </span>
        <button
          type="button"
          onClick={copy}
          className="hud-micro ml-auto"
          style={{
            padding: "var(--space-1) var(--space-2)",
            fontSize: "var(--font-xs)",
            color: copied ? "var(--hud-green)" : "var(--hud-text-muted)",
            background: "transparent",
            border: "var(--border-w) solid var(--hud-edge-faint)",
            cursor: "pointer",
          }}
        >
          {copied ? "COPIED" : "COPY"}
        </button>
      </div>
      <pre
        className="hud-mono"
        style={{
          margin: 0,
          padding: "var(--space-2)",
          fontSize: "var(--font-xs)",
          lineHeight: 1.5,
          color: "var(--hud-text)",
          overflowX: "auto",
          whiteSpace: "pre",
          direction: "ltr",
        }}
      >
        {code}
      </pre>
    </div>
  );
}

/**
 * Render message content, detecting fenced code blocks and rendering them
 * as styled CodeBlock components. Non-code content renders as prose.
 */
function MessageContent({ content }) {
  if (!content) return null;

  // Split on fenced code blocks (```lang\n...```)
  const parts = content.split(/```(\w*)\n?([\s\S]*?)```/g);

  if (parts.length === 1) {
    // No code blocks — render as prose.
    return (
      <p
        style={{
          color: "var(--hud-text)",
          fontFamily: "var(--font-body)",
          fontSize: "var(--font-lg)",
          lineHeight: 1.55,
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
          margin: 0,
        }}
      >
        {content}
      </p>
    );
  }

  const rendered = [];
  for (let i = 0; i < parts.length; i++) {
    if (i % 3 === 0) {
      // Prose segment
      if (parts[i]) {
        rendered.push(
          <p
            key={`p-${i}`}
            style={{
              color: "var(--hud-text)",
              fontFamily: "var(--font-body)",
              fontSize: "var(--font-lg)",
              lineHeight: 1.55,
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
              margin: "var(--space-2) 0",
            }}
          >
            {parts[i]}
          </p>
        );
      }
    } else if (i % 3 === 1) {
      // Language label
      const lang = parts[i];
      const code = parts[i + 1] || "";
      rendered.push(<CodeBlock key={`code-${i}`} code={code} language={lang} />);
      i++; // Skip the code part
    }
  }

  return <>{rendered}</>;
}

export default memo(function ChatView({
  language = "en",
  messages = [],
  isTyping = false,
  isSpeaking = false,
  isConnected = false,
  statusText = "",
  statusTone = "accent",
  subtitle = "",
  plan = [],
  activeTool = null,
  activity = [],
  onSend,
  onVoiceStart,
  onVoiceStop,
  voiceMode = false,
  onStop,
  pendingConfirmation = null,
}) {
  const ar = language === "ar";
  const endRef = useRef(null);

  // The orb reports the same state the rest of the product does, derived by
  // the shared reducer rather than re-guessed here — so it cannot disagree
  // with the status line three rows below it.
  const presenceState = resolveState({ isConnected, isTyping, isSpeaking, pendingConfirmation });

  // Auto-scroll to bottom on new messages or typing changes.
  useEffect(() => {
    const reduced =
      typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    endRef.current?.scrollIntoView({ behavior: reduced ? "auto" : "smooth" });
  }, [messages, isTyping, subtitle]);

  return (
    <div
      data-enter="center"
      className="flex-1 min-h-0 flex flex-col"
      style={{ padding: "var(--space-4)" }}
    >
      {/* Header */}
      <div
        className="flex items-baseline flex-wrap"
        style={{ gap: "var(--space-3)", marginBottom: "var(--space-3)" }}
      >
        <AIPresence
          state={presenceState}
          size={40}
          active={isTyping || isSpeaking}
          language={language}
        />
        <h2
          className="hud-identity"
          style={{
            fontSize: "var(--font-lg)",
            letterSpacing: ar ? "0" : "0.16em",
            margin: 0,
          }}
        >
          {ar ? "المحادثة" : "CHAT"}
        </h2>
        <p className="hud-micro" style={{ fontSize: "var(--font-xs)", margin: 0 }}>
          {ar ? "محادثة مع زيتون" : "CONVERSATION WITH ZAYTOON"}
        </p>
      </div>

      {/* Transcript */}
      <div
        className="hud-panel flex-1 flex flex-col min-h-0"
        style={{ maxHeight: "60vh" }}
      >
        <header className="hud-panel-head">
          <span>{ar ? "الرسائل" : "MESSAGES"}</span>
          <span className="hud-micro ml-auto">
            {messages.length} {ar ? "رسالة" : "MSG"}
          </span>
        </header>
        <div
          className="flex-1 overflow-y-auto"
          style={{ padding: "var(--space-3) var(--space-4)", display: "grid", gap: "var(--space-3)", alignContent: "start" }}
        >
          {messages.length === 0 && !isTyping && (
            <div
              className="flex flex-col items-center justify-center text-center"
              style={{
                gap: "var(--sp-2)",
                padding: "var(--sp-5) var(--sp-3)",
                color: "var(--hud-text-mute)",
              }}
            >
              <span className="hud-pill hud-pill--accent">{ar ? "جاهز" : "READY"}</span>
              <p style={{ fontSize: "var(--font-lg)", color: "var(--hud-text-dim)", lineHeight: 1.5 }}>
                {ar
                  ? "ابدأ المحادثة — اكتب سؤالاً أو أمراً بالأسفل."
                  : "Start the conversation — type a question or command below."}
              </p>
            </div>
          )}

          {messages.map((msg) => (
            <ChatMessage key={msg.id} msg={msg} language={language} contentRenderer={MessageContent} />
          ))}

          {subtitle && (
            <div className="flex justify-center">
              <div
                className="font-mono text-center max-w-lg"
                style={{
                  padding: "var(--space-1) var(--space-3)",
                  fontSize: "var(--font-sm)",
                  background: "color-mix(in srgb, var(--hud-cyan) 5%, transparent)",
                  border: "var(--border-w) solid color-mix(in srgb, var(--hud-cyan) 12%, transparent)",
                  borderRadius: "var(--radius-sm)",
                  color: "var(--hud-cyan)",
                }}
                dir={/[\u0600-\u06FF]/.test(subtitle) ? "rtl" : "ltr"}
              >
                {subtitle}
              </div>
            </div>
          )}

          {isTyping && (
            <div className="flex items-center" style={{ gap: "var(--space-2)", padding: "var(--space-1)" }}>
              <div className="flex items-center" style={{ gap: "var(--space-1)" }}>
                <span className="w-[3px] h-[3px] rounded-full typing-dot" style={{ background: "var(--hud-cyan-core)" }} />
                <span className="w-[3px] h-[3px] rounded-full typing-dot" style={{ background: "var(--hud-cyan-core)" }} />
                <span className="w-[3px] h-[3px] rounded-full typing-dot" style={{ background: "var(--hud-cyan-core)" }} />
              </div>
              <span className="font-mono tracking-wider" style={{ fontSize: "var(--font-sm)", color: "var(--hud-text-mute)" }}>
                {statusText || (ar ? "يعالج…" : "PROCESSING…")}
              </span>
            </div>
          )}
          <div ref={endRef} />
        </div>
      </div>

      {/* Activity feed */}
      {(plan.length > 0 || activeTool || activity.length > 0) && (
        <div
          className="flex-none w-full"
          style={{ padding: "var(--space-3) 0" }}
        >
          <div className="hud-panel" style={{ maxHeight: "200px", overflowY: "auto" }}>
            <header className="hud-panel-head">
              <span>{ar ? "نشاط التنفيذ" : "EXECUTION LOG"}</span>
            </header>
            <div style={{ padding: "var(--space-2) var(--space-3)" }}>
              <ActivityFeed
                plan={plan}
                activeTool={activeTool}
                activity={activity}
                language={language}
              />
            </div>
          </div>
        </div>
      )}

      {/* Status line */}
      {statusText && (
        <div style={{ padding: "var(--space-2) 0" }}>
          <StatusLine text={statusText} language={language} tone={statusTone} />
        </div>
      )}

      {/* Composer */}
      <div className="flex-none" style={{ paddingTop: "var(--space-2)" }}>
        <InputBar
          onSend={onSend}
          isConnected={isConnected}
          language={language}
          onVoiceStart={onVoiceStart}
          onVoiceStop={onVoiceStop}
          voiceMode={voiceMode}
          onStop={onStop}
          busy={isTyping || isSpeaking || !!pendingConfirmation}
          multiline
        />
      </div>
    </div>
  );
});
