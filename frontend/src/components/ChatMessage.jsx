import { memo } from "react";

function isArabic(text) {
  return /[\u0600-\u06FF]/.test(text);
}

/**
 * A transcript entry, framed as a system transmission rather than a chat
 * bubble.
 *
 * Each message carries a 2px accent spine on the side it is anchored to, a
 * monospace source label, and — once a real timestamp exists — a time field.
 * There is no per-message timestamp fabrication: `msg.ts` is only rendered
 * when the backend actually supplied one, because inventing "14:32" on a
 * message that has no clock reading is exactly the sort of invented
 * telemetry this design brief forbids.
 */
export default memo(function ChatMessage({ msg, language }) {
  const isUser = msg.role === "user";
  const isSystem = msg.role === "system";
  const isError = msg.type === "error";

  if (isSystem) {
    return (
      <div className="animate-fade-in flex justify-center">
        <div
          className="max-w-[90%] px-3 py-1"
          style={{
            background: "rgba(6, 17, 26, 0.6)",
            border: "1px solid var(--hud-edge-faint)",
            borderInlineStart: `2px solid ${isError ? "var(--hud-red)" : "var(--hud-text-mute)"}`,
          }}
          role={isError ? "alert" : undefined}
        >
          <p
            className="font-mono"
            style={{ color: isError ? "var(--hud-red)" : "var(--hud-text-dim)", fontSize: 10, lineHeight: 1.6 }}
          >
            {msg.content}
          </p>
        </div>
      </div>
    );
  }

  return (
    <div
      className="animate-fade-in flex"
      style={{ justifyContent: isUser ? "flex-end" : "flex-start" }}
    >
      <div
        className="max-w-[85%] px-3 py-2"
        dir={isArabic(msg.content) ? "rtl" : "ltr"}
        style={{
          background: isUser ? "rgba(0, 229, 255, 0.045)" : "rgba(3, 7, 13, 0.62)",
          border: "1px solid var(--hud-edge-faint)",
          // Spine on the anchored side; in RTL that flips with the document.
          borderInlineStart: `2px solid ${isUser ? "var(--hud-cyan)" : "var(--hud-blue)"}`,
          borderInlineEnd: isUser ? "none" : undefined,
          borderInlineStartWidth: 2,
          boxShadow: msg.streaming ? "0 0 18px rgba(0, 229, 255, 0.06)" : "none",
        }}
      >
        <p
          className="whitespace-pre-wrap leading-relaxed"
          style={{
            color: isUser ? "var(--hud-text)" : "var(--hud-text)",
            fontFamily: "var(--font-body)",
            fontSize: 13,
          }}
        >
          {msg.content}
          {msg.streaming && (
            <span className="inline-flex gap-[2px] ml-1.5 align-middle" aria-hidden="true">
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
        <div className="flex items-center gap-2 mt-1.5">
          <span
            className="font-mono"
            style={{
              color: isUser ? "var(--hud-cyan)" : "var(--hud-blue)",
              fontSize: 8,
              letterSpacing: "0.18em",
            }}
          >
            {isUser ? (language === "ar" ? "أنت" : "YOU") : "ZAYTOON"}
          </span>
          {msg.streaming && (
            <span
              className="font-mono"
              style={{ color: "var(--hud-text-mute)", fontSize: 7, letterSpacing: "0.18em" }}
            >
              ▍STREAM
            </span>
          )}
          {msg.ts ? (
            <span className="font-mono ml-auto" style={{ color: "var(--hud-text-mute)", fontSize: 8 }}>
              {msg.ts}
            </span>
          ) : null}
        </div>
      </div>
    </div>
  );
});
