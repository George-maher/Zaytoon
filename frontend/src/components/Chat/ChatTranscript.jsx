import { memo, useRef, useEffect } from "react";
import ChatMessage from "../ChatMessage";

/**
 * The transcript.
 *
 * Carries three derived things on top of the message list:
 *
 * - **An empty state.** Before the first turn the panel used to be a blank
 *   rectangle, which reads as "broken" rather than "waiting for a command".
 * - **A status line in place of generic loading text.** The typing indicator
 *   now shows the caller's derived label ("Generating response…",
 *   "Understanding…") instead of one word for every possible state.
 * - **A subtitle strip** for the live transcription.
 *
 * The scroll is `auto` rather than `smooth` under reduced motion: an
 * animated scroll is still an animation.
 */
export default memo(function ChatTranscript({ messages, isTyping, subtitle, language, statusText }) {
  const endRef = useRef(null);

  useEffect(() => {
    const reduced =
      typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    endRef.current?.scrollIntoView({ behavior: reduced ? "auto" : "smooth" });
  }, [messages, isTyping, subtitle]);

  const ar = language === "ar";

  return (
    <div
      className="flex-1 overflow-y-auto relative z-10 max-h-[300px]"
      style={{ padding: "var(--space-3) var(--space-4)", display: "grid", gap: "var(--space-3)", alignContent: "start" }}
    >
      {messages.length === 0 && !isTyping && (
        <div
          className="flex flex-col items-center justify-center text-center animate-fade-in"
          style={{
            gap: "var(--sp-2)",
            padding: "var(--sp-5) var(--sp-3)",
            color: "var(--hud-text-mute)",
          }}
        >
          <span className="hud-pill hud-pill--accent">{ar ? "جاهز" : "READY"}</span>
          <p style={{ fontSize: "var(--font-lg)", color: "var(--hud-text-dim)", lineHeight: 1.5 }}>
            {ar
              ? "لا رسائل بعد — جرّب: «كم الساعة؟» أو «افتح المفكرة»"
              : "No messages yet — try “what time is it?” or “open notepad”."}
          </p>
          <p className="font-mono" style={{ fontSize: "var(--font-xs)", letterSpacing: "0.12em", color: "var(--text-faint)" }}>
            {ar ? "اكتب أمرًا بالأسفل أو فعّل الميكروفون" : "TYPE A COMMAND BELOW, OR USE THE MICROPHONE"}
          </p>
        </div>
      )}

      {messages.map((msg) => (
        <ChatMessage key={msg.id} msg={msg} language={language} />
      ))}
      {subtitle && (
        <div className="flex justify-center animate-fade-in">
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
          >{subtitle}</div>
        </div>
      )}
      {isTyping && (
        <div className="flex items-center animate-fade-in" style={{ gap: "var(--space-2)", padding: "var(--space-1)" }}>
          <div className="flex items-center" style={{ gap: "var(--space-1)" }}>
            <span className="w-[3px] h-[3px] rounded-full typing-dot" style={{ background: "var(--hud-cyan-core)" }} />
            <span className="w-[3px] h-[3px] rounded-full typing-dot" style={{ background: "var(--hud-cyan-core)" }} />
            <span className="w-[3px] h-[3px] rounded-full typing-dot" style={{ background: "var(--hud-cyan-core)" }} />
          </div>
          <span className="font-mono tracking-wider" style={{ fontSize: "var(--font-sm)", color: "var(--hud-text-mute)" }}>
            {/* The caller's derived label: what is happening, not "busy". */}
            {statusText || (ar ? "يعالج…" : "PROCESSING…")}
          </span>
        </div>
      )}
      <div ref={endRef} />
    </div>
  );
});
