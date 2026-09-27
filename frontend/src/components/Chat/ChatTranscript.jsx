import { memo, useRef, useEffect } from "react";
import ChatMessage from "../ChatMessage";

export default memo(function ChatTranscript({ messages, isTyping, subtitle, language }) {
  const endRef = useRef(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, isTyping, subtitle]);

  return (
    <div className="flex-1 overflow-y-auto px-4 py-2 space-y-2 relative z-10 max-h-[280px]">
      {messages.map((msg) => (
        <ChatMessage key={msg.id} msg={msg} language={language} />
      ))}
      {subtitle && (
        <div className="flex justify-center animate-fade-in">
          <div className="px-3 py-1 rounded-sm text-[10px] font-mono text-center max-w-lg"
            style={{
              background: "rgba(0, 212, 255, 0.05)",
              border: "1px solid rgba(0, 212, 255, 0.12)",
              color: "#00d4ff",
            }}
            dir={/[\u0600-\u06FF]/.test(subtitle) ? "rtl" : "ltr"}
          >{subtitle}</div>
        </div>
      )}
      {isTyping && (
        <div className="flex items-center gap-2 px-2 py-1 animate-fade-in">
          <div className="flex items-center gap-1">
            <span className="w-[3px] h-[3px] rounded-full typing-dot" style={{ background: "#00f0ff" }} />
            <span className="w-[3px] h-[3px] rounded-full typing-dot" style={{ background: "#00f0ff" }} />
            <span className="w-[3px] h-[3px] rounded-full typing-dot" style={{ background: "#00f0ff" }} />
          </div>
          <span className="text-[10px] font-mono tracking-wider" style={{ color: "var(--text-muted)" }}>
            {language === "ar" ? "يعالج..." : "PROCESSING..."}
          </span>
        </div>
      )}
      <div ref={endRef} />
    </div>
  );
});
