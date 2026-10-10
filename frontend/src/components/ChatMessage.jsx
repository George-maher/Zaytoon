import { memo } from "react";

function isArabic(text) {
  return /[\u0600-\u06FF]/.test(text);
}

/**
 * A transcript entry: a framed transmission, not a rounded chat bubble.
 *
 * - **Side** carries the speaker: the user's message is anchored to the
 *   composer side, the assistant's to the rail side, each with a 2px accent
 *   spine that flips with the document in RTL.
 * - **Timestamp** is `msg.ts` — a receipt stamp taken by the client when the
 *   message was created or received (see `clockTime` in useWebSocket), or a
 *   server-supplied one if the backend ever sends it. It is never invented
 *   for content that has none: the field renders only when present.
 * - **Streaming** shows a live cursor and a STREAM marker, and gets one
 *   entrance animation on mount (`hud-msg-enter`) — no per-token animation,
 *   so a long reply does not flicker as it grows.
 * - **System notices** (errors, reminders, confirmations) are centred,
 *   monospace and non-destructive: an error is a strip with a red spine and
 *   `role="alert"`, not a modal or a red panel that hides the transcript.
 */
export default memo(function ChatMessage({ msg, language }) {
  const isUser = msg.role === "user";
  const isSystem = msg.role === "system";
  const isError = msg.type === "error";

  if (isSystem) {
    return (
      <div className="hud-msg-enter flex justify-center">
        <div
          style={{
            maxWidth: "90%",
            padding: "var(--space-2) var(--space-4)",
            background: isError
              ? "color-mix(in srgb, var(--hud-red) 6%, var(--surface-1))"
              : "var(--surface-1)",
            border: "var(--border-w) solid var(--hud-edge-faint)",
            borderInlineStart: `2px solid ${isError ? "var(--hud-red)" : "var(--hud-text-mute)"}`,
          }}
          role={isError ? "alert" : "note"}
        >
          <p
            className="font-mono"
            style={{
              color: isError ? "var(--hud-red)" : "var(--hud-text-dim)",
              fontSize: "var(--font-sm)",
              lineHeight: 1.55,
            }}
          >
            {msg.content}
          </p>
        </div>
      </div>
    );
  }

  return (
    <div
      className="hud-msg-enter flex"
      style={{ justifyContent: isUser ? "flex-end" : "flex-start" }}
    >
      <div
        style={{
          maxWidth: "85%",
          padding: "var(--space-3) var(--space-4)",
          background: isUser
            ? "color-mix(in srgb, var(--hud-cyan) 6%, var(--surface-1))"
            : "var(--surface-1)",
          border: "var(--border-w) solid var(--hud-edge-faint)",
          // Spine on the anchored side; in RTL that flips with the document.
          borderInlineStart: `2px solid ${isUser ? "var(--hud-cyan)" : "var(--hud-blue)"}`,
          borderInlineEnd: isUser ? "none" : undefined,
          borderInlineStartWidth: 2,
          boxShadow: msg.streaming
            ? "0 0 18px color-mix(in srgb, var(--hud-cyan) 12%, transparent)"
            : "none",
        }}
        dir={isArabic(msg.content) ? "rtl" : "ltr"}
      >
        <p
          className="whitespace-pre-wrap"
          style={{
            color: "var(--hud-text)",
            fontFamily: "var(--font-body)",
            fontSize: "var(--font-lg)",
            lineHeight: 1.55,
          }}
        >
          {msg.content}
          {msg.streaming && (
            <span className="inline-flex ml-1 align-middle" style={{ gap: "2px" }} aria-hidden="true">
              {[0, 1, 2].map((i) => (
                <span
                  key={i}
                  className="w-[3px] h-[3px] typing-dot"
                  style={{ background: "var(--hud-cyan)" }}
                />
              ))}
            </span>
          )}
        </p>
        <div className="flex items-center" style={{ gap: "var(--space-3)", marginTop: "var(--space-2)" }}>
          <span
            className="font-mono"
            style={{
              color: isUser ? "var(--hud-cyan)" : "var(--hud-blue)",
              fontSize: "var(--font-xs)",
              letterSpacing: "0.16em",
            }}
          >
            {isUser ? (language === "ar" ? "أنت" : "YOU") : "ZAYTOON"}
          </span>
          {msg.streaming && (
            <span
              className="font-mono"
              style={{ color: "var(--hud-text-mute)", fontSize: "var(--font-xs)", letterSpacing: "0.16em" }}
            >
              ▍STREAM
            </span>
          )}
          {msg.ts ? (
            <span
              className="font-mono ml-auto"
              style={{ color: "var(--hud-text-faint, var(--hud-text-mute))", fontSize: "var(--font-xs)" }}
            >
              {msg.ts}
            </span>
          ) : null}
        </div>
      </div>
    </div>
  );
});
