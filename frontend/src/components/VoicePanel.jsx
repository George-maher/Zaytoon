export default function VoicePanel({ isListening, isTyping, isSpeaking, awaitingCommand, isConnected, language, onStop }) {
  const getStatusText = () => {
    if (awaitingCommand) return language === "ar" ? "قول الأمر..." : "Say your command...";
    if (isListening) return "Listening... speak naturally";
    if (isTyping) return "Processing your request...";
    if (isSpeaking) return "Speaking...";
    return language === "ar" ? "بانتظار كلمة زيتون..." : "Awaiting 'Zaytoon'...";
  };

  const getStatusColor = () => {
    if (awaitingCommand) return "#f0b000";
    if (isListening) return "#00ff88";
    if (isSpeaking) return "#00f0ff";
    return "var(--text-secondary)";
  };

  return (
    <div className="flex-none px-4 py-3 flex items-center justify-center gap-4"
      style={{
        background: "rgba(5, 5, 15, 0.8)",
        borderTop: "1px solid rgba(0, 212, 255, 0.12)",
      }}
    >
      <div className="flex items-center gap-3">
        <div className="relative flex items-center gap-0.5">
          {awaitingCommand && [1,2,3,4,5].map((i) => (
            <span key={i} className="w-[2px] rounded-sm equalizer-bar"
              style={{
                background: "#f0b000",
                boxShadow: "0 0 6px rgba(240,176,0,0.3)",
                height: `${3 + Math.random() * 12}px`,
                animationDelay: `${-i * 0.15}s`,
              }}
            />
          ))}
          {isListening && [1,2,3,4,5].map((i) => (
            <span key={i} className="w-[2px] rounded-sm equalizer-bar"
              style={{
                background: "#00ff88",
                boxShadow: "0 0 6px rgba(0,255,136,0.3)",
                height: `${3 + Math.random() * 12}px`,
                animationDelay: `${-i * 0.1}s`,
              }}
            />
          ))}
          {isSpeaking && [1,2,3,4,5].map((i) => (
            <span key={i} className="w-[2px] rounded-sm equalizer-bar"
              style={{
                background: "#00f0ff",
                boxShadow: "0 0 6px rgba(0,240,255,0.3)",
                height: `${3 + Math.random() * 12}px`,
                animationDelay: `${-i * 0.12}s`,
              }}
            />
          ))}
          {!awaitingCommand && !isListening && !isSpeaking && (
            <span className="w-2 h-2 rounded-full animate-pulse" style={{ background: "var(--text-muted)" }} />
          )}
        </div>

        <span className="text-[10px] font-mono tracking-wider" style={{ color: getStatusColor() }}>
          {getStatusText()}
        </span>
      </div>

      <button type="button" onClick={onStop}
        className="px-3 py-1.5 rounded-sm text-[9px] font-mono tracking-wider transition-all"
        style={{
          background: "rgba(255, 51, 85, 0.08)",
          color: "#ff3355",
          border: "1px solid rgba(255, 51, 85, 0.2)",
        }}
        onMouseEnter={(e) => {
          e.target.style.background = "rgba(255, 51, 85, 0.15)";
          e.target.style.boxShadow = "0 0 15px rgba(255,51,85,0.1)";
        }}
        onMouseLeave={(e) => {
          e.target.style.background = "rgba(255, 51, 85, 0.08)";
          e.target.style.boxShadow = "none";
        }}
      >
        {language === "ar" ? "إيقاف الصوت" : "STOP VOICE"}
      </button>
    </div>
  );
}
