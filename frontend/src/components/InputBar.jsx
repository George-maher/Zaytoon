import { memo, useRef, useCallback, useEffect } from "react";

/**
 * Command input.
 *
 * Behaviour and accessible names are unchanged: the field is uncontrolled and
 * cleared imperatively, Enter submits, Shift+Enter inserts a newline, and the
 * maximum length matches the backend's. Only the surface is restyled — square,
 * hairline-framed, monospace, with the focus state moving to the *form* border
 * so the whole console lights up rather than a rounded pill.
 *
 * The microphone lives here and nowhere else in the product; see
 * components/hud/CommandConsole.jsx for why.
 */
export default memo(function InputBar({
  onSend,
  isConnected,
  language,
  onVoiceStart,
  onVoiceStop,
  voiceMode,
}) {
  const inputRef = useRef(null);
  const formRef = useRef(null);
  const canRecord = !!onVoiceStart || !!onVoiceStop;

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

  const setFocused = (on) => {
    const form = formRef.current;
    if (!form) return;
    form.style.borderColor = on ? "var(--hud-edge-strong)" : "var(--hud-edge)";
    form.style.boxShadow = on ? "0 0 16px rgba(0, 229, 255, 0.10)" : "inset 0 1px 0 rgba(0, 229, 255, 0.05)";
  };

  return (
    <form
      ref={formRef}
      onSubmit={handleSubmit}
      className="flex items-center"
      style={{
        background: "rgba(3, 7, 13, 0.72)",
        border: "1px solid var(--hud-edge)",
        boxShadow: "inset 0 1px 0 rgba(0, 229, 255, 0.05)",
        transition: "border-color 120ms ease-out, box-shadow 120ms ease-out",
      }}
    >
      <div className="flex-1 relative min-w-0 flex items-center">
        <input
          ref={inputRef}
          type="text"
          maxLength={8000}
          aria-label={language === "ar" ? "رسالتك" : "Your message"}
          placeholder={language === "ar" ? "اكتب أمراً…" : "ENTER COMMAND…"}
          className="w-full px-3 py-2.5 outline-none"
          style={{
            background: "transparent",
            border: "none",
            color: "var(--hud-text)",
            caretColor: "var(--hud-cyan)",
            fontFamily: "var(--font-mono)",
            fontSize: 12,
            letterSpacing: "0.02em",
          }}
          dir={language === "ar" ? "rtl" : "ltr"}
          onKeyDown={handleKeyDown}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
        />
        {canRecord && (
          <button
            type="button"
            /* Always rendered, and always a toggle. It used to be unmounted
               while recording (`onVoiceStart={voiceMode ? undefined : ...}`),
               which left no way to stop a recording from the UI once the
               dock's duplicate mic was removed. The control must persist
               across the state change it controls. */
            onClick={() => (voiceMode ? onVoiceStop?.() : onVoiceStart?.())}
            className="absolute top-1/2 -translate-y-1/2 w-7 h-7 flex items-center justify-center transition-all"
            style={{
              color: voiceMode ? "var(--hud-green)" : "var(--hud-text-mute)",
              insetInlineEnd: "0.3rem",
              background: voiceMode ? "rgba(0, 255, 156, 0.1)" : "transparent",
              border: `1px solid ${voiceMode ? "rgba(0,255,156,0.35)" : "var(--hud-edge-faint)"}`,
            }}
            title={language === "ar" ? "إدخال صوتي مباشر" : "Direct voice input"}
            aria-label={language === "ar" ? "تفعيل الصوت" : "Activate voice mode"}
            aria-pressed={!!voiceMode}
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

      <button
        type="submit"
        disabled={!isConnected}
        className="px-4 py-2.5 font-mono font-bold transition-all disabled:opacity-25 flex-none"
        style={{
          background: "rgba(0, 229, 255, 0.08)",
          color: "var(--hud-cyan)",
          border: "1px solid var(--hud-edge)",
          borderInlineStartColor: "var(--hud-edge-faint)",
          fontSize: 10,
          letterSpacing: "0.18em",
          textShadow: "0 0 10px var(--hud-glow-soft)",
        }}
        onMouseEnter={(e) => {
          if (!isConnected) return;
          e.currentTarget.style.background = "rgba(0, 229, 255, 0.16)";
        }}
        onMouseLeave={(e) => { e.currentTarget.style.background = "rgba(0, 229, 255, 0.08)"; }}
      >
        {language === "ar" ? "إرسال" : "SEND"}
      </button>

      {voiceMode && (
        <span
          className="font-mono flex-none px-2"
          style={{ color: "var(--hud-green)", fontSize: 8, letterSpacing: "0.18em" }}
          role="status"
        >
          <span aria-hidden="true">●</span> REC
        </span>
      )}
    </form>
  );
});
