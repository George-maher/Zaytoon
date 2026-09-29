import { memo, useRef, useCallback, useEffect } from "react";

/**
 * InputBar - Text composer with voice activation
 */
export default memo(function InputBar({
  onSend,
  isConnected,
  language,
  onVoiceStart,
  voiceMode,
}) {
  const inputRef = useRef(null);

  const handleSubmit = useCallback(
    (e) => {
      e.preventDefault();
      const text = inputRef.current?.value?.trim();
      if (text) {
        onSend(text);
        inputRef.current.value = "";
        inputRef.current.focus();
      }
    },
    [onSend],
  );

  const handleKeyDown = useCallback(
    (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        handleSubmit(e);
      }
    },
    [handleSubmit],
  );

  // Keep focus on the composer so a reply can be followed by typing.
  // Guarded on `document.activeElement === document.body` so focus is never stolen
  // from a dialog or another control the user is using.
  useEffect(() => {
    if (isConnected && document.activeElement === document.body) {
      inputRef.current?.focus();
    }
  }, [isConnected]);

  return (
    <form onSubmit={handleSubmit}
      className="flex items-center gap-2 py-2"
      style={{
        background: "rgba(5, 5, 15, 0.8)",
        border: "1px solid var(--border-color)",
        borderRadius: "var(--radius-sm)",
      }}
    >
      <div className="flex-1 relative min-w-0">
        <input ref={inputRef} type="text" maxLength={8000}
          aria-label={language === "ar" ? "رسالتك" : "Your message"}
          placeholder={language === "ar" ? "اكتب رسالة أو تكلم..." : "Type a message or speak..."}
          className="w-full px-3 py-2 rounded-sm text-xs font-mono outline-none transition-all"
          style={{
            background: "rgba(0, 212, 255, 0.04)",
            border: "1px solid rgba(0, 212, 255, 0.12)",
            color: "var(--text-primary)",
            caretColor: "#00D4FF",
          }}
          dir={language === "ar" ? "rtl" : "ltr"}
          onKeyDown={handleKeyDown}
          onFocus={(e) => e.target.style.borderColor = "rgba(0, 212, 255, 0.3)"}
          onBlur={(e) => e.target.style.borderColor = "rgba(0, 212, 255, 0.12)"}
        />
        {onVoiceStart && (
          <button type="button" onClick={onVoiceStart}
            className="absolute top-1/2 -translate-y-1/2 w-7 h-7 flex items-center justify-center rounded-sm transition-all"
            style={{
              color: "#00FF88",
              insetInlineEnd: "0.375rem",
              background: "rgba(0, 255, 136, 0.08)",
              border: "1px solid rgba(0, 255, 136, 0.2)",
            }}
            title={language === "ar" ? "تفعيل الصوت" : "Activate voice mode"}
            aria-label={language === "ar" ? "تفعيل الصوت" : "Activate voice mode"}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />
              <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
              <line x1="12" y1="19" x2="12" y2="23" />
              <line x1="8" y1="23" x2="16" y2="23" />
            </svg>
          </button>
        )}
      </div>
      <button type="submit" disabled={!isConnected}
        className="px-4 py-2 rounded-sm text-[9px] font-display font-bold tracking-[0.15em] transition-all disabled:opacity-20 flex-none"
        style={{
          background: "rgba(0, 212, 255, 0.1)",
          color: "#00D4FF",
          border: "1px solid rgba(0, 212, 255, 0.25)",
          textShadow: "0 0 10px rgba(0,212,255,0.3)",
        }}
        onMouseEnter={(e) => {
          if (!isConnected) return;
          e.target.style.background = "rgba(0, 212, 255, 0.2)";
          e.target.style.boxShadow = "0 0 20px rgba(0,212,255,0.15)";
        }}
        onMouseLeave={(e) => {
          e.target.style.background = "rgba(0, 212, 255, 0.1)";
          e.target.style.boxShadow = "none";
        }}
      >{language === "ar" ? "إرسال" : "SEND"}</button>
      {voiceMode && (
        <span className="text-[7px] font-mono tracking-wider flex-none" style={{ color: "#00FF88" }}>
          {language === "ar" ? "الصوت مفعّل" : "VOICE ON"}
        </span>
      )}
    </form>
  );
});