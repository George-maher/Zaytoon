import { useCallback } from "react";

export default function VoiceButton({ isListening, isSpeaking, voiceMode, language, onStart, onStop }) {
  const handleClick = useCallback(() => {
    if (voiceMode) {
      onStop?.();
    } else {
      onStart?.();
    }
  }, [voiceMode, onStart, onStop]);

  const getColor = () => {
    if (isListening) return "#00ff88";
    if (isSpeaking) return "#00f0ff";
    if (voiceMode) return "#00d4ff";
    return "var(--text-muted)";
  };

  const getLabel = () => {
    if (voiceMode) return language === "ar" ? "إيقاف" : "STOP";
    return language === "ar" ? "ابدأ التحدث" : "TALK";
  };

  return (
    <button
      onClick={handleClick}
      className="relative flex items-center gap-2 px-4 py-2 rounded-sm transition-all duration-300"
      style={{
        background: voiceMode ? `${getColor()}10` : "rgba(0,212,255,0.06)",
        border: `1px solid ${voiceMode ? `${getColor()}44` : "rgba(0,212,255,0.15)"}`,
        color: getColor(),
        boxShadow: voiceMode ? `0 0 20px ${getColor()}15` : "none",
      }}
    >
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />
        <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
        <line x1="12" y1="19" x2="12" y2="23" />
        <line x1="8" y1="23" x2="16" y2="23" />
      </svg>
      <span className="text-[10px] font-display font-bold tracking-[0.15em]">{getLabel()}</span>
      {voiceMode && (
        <span className="absolute -top-1 -right-1 w-2 h-2 rounded-full animate-pulse" style={{
          background: getColor(),
          boxShadow: `0 0 6px ${getColor()}`,
        }} />
      )}
    </button>
  );
}
