import { useEffect, useRef } from "react";
import ChatMessage from "./ChatMessage";

export default function MessageList({ messages, isTyping, language, subtitle }) {
  const endRef = useRef(null);
  const listRef = useRef(null);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, isTyping, subtitle]);

  const empty = messages.length === 0;

  if (empty) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="text-center max-w-md px-4">
          <div className="relative w-24 h-24 mx-auto mb-6 flex items-center justify-center">
            <svg className="absolute inset-0 w-full h-full animate-spin-slow" viewBox="0 0 100 100" style={{ opacity: 0.15 }}>
              <circle cx="50" cy="50" r="45" fill="none" stroke="#00d4ff" strokeWidth="1" strokeDasharray="70 140" />
              <circle cx="50" cy="50" r="30" fill="none" stroke="#00d4ff" strokeWidth="0.5" strokeDasharray="40 120" />
            </svg>
            <svg className="absolute inset-0 w-full h-full animate-spin-slow" viewBox="0 0 100 100" style={{ animationDirection: "reverse", opacity: 0.1 }}>
              <circle cx="50" cy="50" r="38" fill="none" stroke="#00f0ff" strokeWidth="0.5" strokeDasharray="30 100" />
            </svg>
            <div className="relative z-10 w-14 h-14 flex items-center justify-center rounded-sm border" style={{
              borderColor: "rgba(0, 212, 255, 0.2)",
              boxShadow: "0 0 30px rgba(0,212,255,0.1), inset 0 0 30px rgba(0,212,255,0.03)",
              background: "rgba(0, 212, 255, 0.03)",
            }}>
              <span className="text-2xl font-display font-bold glow-text" style={{ color: "#00d4ff" }}>Z</span>
            </div>
            <div className="absolute -bottom-1 left-[10%] right-[10%] h-[1px]" style={{
              background: "linear-gradient(90deg, transparent, rgba(0,212,255,0.4), transparent)",
            }} />
          </div>

          <h2 className="text-sm font-display font-bold tracking-[0.2em] mb-2 glow-text"
            style={{ color: "#00d4ff" }}
            dir={language === "ar" ? "rtl" : "ltr"}
          >
            {language === "ar" ? "إزاي أقدر أساعدك؟" : "AT YOUR SERVICE, SIR."}
          </h2>

          <p className="text-xs font-mono leading-relaxed" style={{ color: "var(--text-secondary)" }}
            dir={language === "ar" ? "rtl" : "ltr"}
          >
            {language === "ar" ? "تكلم بطلاقة. أفهم العربية والإنجليزية." : "Speak naturally. I understand Arabic and English."}
          </p>

          <p className="text-[10px] font-mono mt-1" style={{ color: "rgba(0, 212, 255, 0.4)" }}>
            {language === "ar" ? "النظام على الخط · جميع الوكلاء مستعدون" : "SYSTEM ONLINE · ALL AGENTS READY"}
          </p>

          <div className="mt-5 flex flex-wrap gap-1.5 justify-center">
            {(language === "ar"
              ? ["ذكرني", "ابحث", "اكتب كود", "معلومات النظام"]
              : ["Remind me", "Search", "Write code", "System info", "Analyze"]
            ).map((hint) => (
              <span key={hint} className="px-2.5 py-1 text-[8px] rounded-sm font-mono tracking-wider uppercase"
                style={{
                  background: "rgba(0, 212, 255, 0.04)",
                  border: "1px solid rgba(0, 212, 255, 0.08)",
                  color: "var(--text-muted)",
                }}
              >{hint}</span>
            ))}
          </div>

          <div className="mt-6 flex items-center justify-center gap-2">
            <div className="flex items-center gap-1">
              <span className="w-[2px] h-3 rounded-sm equalizer-bar" style={{ background: "#00d4ff" }} />
              <span className="w-[2px] h-3 rounded-sm equalizer-bar" style={{ background: "#00d4ff" }} />
              <span className="w-[2px] h-3 rounded-sm equalizer-bar" style={{ background: "#00d4ff" }} />
              <span className="w-[2px] h-3 rounded-sm equalizer-bar" style={{ background: "#00d4ff" }} />
              <span className="w-[2px] h-3 rounded-sm equalizer-bar" style={{ background: "#00d4ff" }} />
            </div>
            <span className="text-[8px] font-mono" style={{ color: "var(--text-muted)" }}>HOLOGRAPHIC INTERFACE v3.0</span>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div ref={listRef} className="flex-1 overflow-y-auto px-4 py-3 space-y-2">
      {messages.map((msg) => <ChatMessage key={msg.id} msg={msg} language={language} />)}
      {subtitle && (
        <div className="flex justify-center animate-fade-in">
          <div className="px-4 py-1.5 rounded-sm text-[10px] font-mono text-center max-w-lg"
            style={{
              background: "rgba(0, 212, 255, 0.05)",
              border: "1px solid rgba(0, 212, 255, 0.12)",
              color: "#00d4ff",
              backdropFilter: "blur(8px)",
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
}
