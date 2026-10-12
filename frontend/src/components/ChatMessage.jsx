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
            ? "color-mix(in srgb, var(--zt-accent) 8%, var(--surface-1))"
            : "var(--surface-1)",
          border: "1px solid var(--zt-border-subtle)",
          clipPath: "polygon(0 var(--cut, 10px), var(--cut, 10px) 0, 100% 0, 100% calc(100% - var(--cut, 10px)), calc(100% - var(--cut, 10px)) 100%, 0 100%)",
          // Spine on the anchored side; in RTL that flips with the document.
          borderInlineStart: `2px solid ${isUser ? "var(--zt-accent)" : "var(--zt-accent-dim)"}`,
          borderInlineEnd: isUser ? "none" : undefined,
          borderInlineStartWidth: 2,
          boxShadow: msg.streaming
            ? "0 0 18px color-mix(in srgb, var(--zt-accent) 12%, transparent)"
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
                  style={{ background: "var(--zt-accent)" }}
                />
              ))}
            </span>
          )}
        </p>
        {!isUser && Array.isArray(msg.sources) && msg.sources.length > 0 && (
          <div
            style={{ marginTop: "var(--space-2)", borderTop: "1px dashed var(--zt-border-subtle)", paddingTop: "var(--space-2)" }}
          >
            <span
              className="font-mono"
              style={{
                color: "var(--hud-text-mute)",
                fontSize: "var(--font-xs)",
                letterSpacing: "0.16em",
                display: "block",
                marginBottom: "var(--space-1)",
              }}
            >
              {language === "ar" ? "المصادر" : "SOURCES"}
            </span>
            <ol
              dir="ltr"
              className="font-mono"
              style={{
                display: "grid",
                gap: "2px",
                fontSize: "var(--font-xs)",
                listStyle: "none",
                padding: 0,
                margin: 0,
              }}
            >
              {msg.sources.map((src, i) => {
                const href =
                  typeof src?.url === "string" && /^https?:\/\//i.test(src.url) ? src.url : null;
                const label = src?.title || src?.host || href || "";
                return (
                  <li key={`${src?.n ?? i}-${href || label}`} style={{ display: "flex", gap: "6px" }}>
                    <span style={{ color: "var(--zt-accent-dim)" }}>{src?.n ?? i + 1}</span>
                    {href ? (
                      <a
                        href={href}
                        target="_blank"
                        rel="noopener noreferrer"
                        title={href}
                        style={{
                          color: "var(--hud-cyan)",
                          textDecoration: "none",
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                          maxWidth: "100%",
                        }}
                      >
                        {label}
                      </a>
                    ) : (
                      <span style={{ color: "var(--hud-text-dim)" }}>{label}</span>
                    )}
                    {src?.kind === "snippet" && (
                      <span style={{ color: "var(--hud-text-faint, var(--hud-text-mute))" }}>
                        {language === "ar" ? "(مقتطف)" : "(snippet)"}
                      </span>
                    )}
                  </li>
                );
              })}
            </ol>
          </div>
        )}
        <div className="flex items-center" style={{ gap: "var(--space-3)", marginTop: "var(--space-2)" }}>
          <span
            className="font-mono"
            style={{
              color: isUser ? "var(--zt-accent)" : "var(--zt-accent-dim)",
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
