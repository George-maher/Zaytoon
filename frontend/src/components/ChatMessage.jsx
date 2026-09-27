import { memo } from "react";

function isArabic(text) {
  return /[\u0600-\u06FF]/.test(text);
}

export default memo(function ChatMessage({ msg, language }) {
  const isUser = msg.role === "user";
  const isSystem = msg.role === "system";

  return (
    <div className="animate-fade-in flex"
      style={{ justifyContent: isUser ? "flex-end" : isSystem ? "center" : "flex-start" }}
    >
      {isSystem ? (
        <div className="max-w-[90%] px-3 py-1.5 rounded-sm" style={{
          background: "rgba(0, 212, 255, 0.03)",
          border: "1px solid rgba(0, 212, 255, 0.08)",
        }}>
          <p className="text-[10px] font-mono" style={{ color: "var(--text-muted)" }}>{msg.content}</p>
        </div>
      ) : (
        <div className={`max-w-[85%] rounded-sm px-3 py-2 ${msg.streaming ? "border-l-[2px]" : "border"}`}
          dir={isArabic(msg.content) ? "rtl" : "ltr"}
          style={{
            background: isUser
              ? "rgba(0, 212, 255, 0.04)"
              : "rgba(5, 5, 15, 0.6)",
            borderColor: isUser
              ? "rgba(0, 212, 255, 0.12)"
              : msg.streaming
                ? "rgba(0, 240, 255, 0.3)"
                : "rgba(0, 212, 255, 0.08)",
            borderLeftColor: msg.streaming ? "#00f0ff" : undefined,
            boxShadow: msg.streaming
              ? "0 0 20px rgba(0, 240, 255, 0.05)"
              : "none",
          }}
        >
          <p className="text-xs font-mono leading-relaxed whitespace-pre-wrap"
            style={{ color: isUser ? "#b0c4ff" : "var(--text-primary)" }}
          >
            {msg.content}
            {msg.streaming && (
              <span className="inline-flex gap-[2px] ml-1.5 align-middle">
                <span className="w-[3px] h-[3px] rounded-full typing-dot" style={{ background: "#00f0ff" }} />
                <span className="w-[3px] h-[3px] rounded-full typing-dot" style={{ background: "#00f0ff" }} />
                <span className="w-[3px] h-[3px] rounded-full typing-dot" style={{ background: "#00f0ff" }} />
              </span>
            )}
          </p>
          <div className="flex items-center gap-2 mt-1">
            <span className="text-[8px] font-mono tracking-wider"
              style={{ color: isUser ? "rgba(0, 212, 255, 0.5)" : "rgba(0, 240, 255, 0.5)" }}
            >
              {isUser ? (language === "ar" ? "أنت" : "YOU") : "ZAYTOON"}
            </span>
            {msg.streaming && (
              <span className="text-[7px] font-mono" style={{ color: "rgba(0, 240, 255, 0.4)" }}>▍STREAM</span>
            )}
          </div>
        </div>
      )}
    </div>
  );
});
