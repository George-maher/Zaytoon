import { memo, useRef, useCallback, useEffect, useState } from "react";
import { voiceStateBadge } from "../lib/voiceState";

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
 * components/HUD/CommandConsole.jsx for why.
 *
 * When `multiline` is true the field becomes a `<textarea>` that grows with
 * its content (up to a cap), so a long command or a multi-line prompt is
 * writable without a horizontal scroll. Enter still sends; Shift+Enter still
 * inserts a newline.
 */
export default memo(function InputBar({
  onSend,
  isConnected,
  language,
  onVoiceStart,
  onVoiceStop,
  voiceMode,
  /** The server's voice pipeline state (one of the seven in `lib/voiceState`).
   *  `voiceMode` is the toggle; this is what the pipeline is actually doing, and
   *  it is what the badge says. */
  voiceState = "disabled",
  onStop,
  /** True while a turn is generating or speaking — the window in which STOP
   *  means something. Without it the button would either always show (and
   *  interrupt nothing) or never show. */
  busy = false,
  multiline = false,
}) {
  const inputRef = useRef(null);
  const formRef = useRef(null);
  const canRecord = !!onVoiceStart || !!onVoiceStop;
  const [_composing, setComposing] = useState(false);
  // The badge names the *pipeline* state, not just "recording or not". With a
  // continuous wake-word listener there are moments (working, waiting for an
  // approval, speaking) where the button is on but the microphone is shut, and
  // a bare "REC" claimed the opposite.
  const badge = voiceStateBadge(voiceState, language);
  // The toggle reflects the user's *intent* (`voiceMode`, which also follows
  // the server's `enabled` frames); the badge reflects what the pipeline is
  // doing. Keeping them separate is what lets the button read as "on" while
  // the badge says WORK or TTS instead of a dishonest "REC".
  const recorderOn = !!voiceMode;
  const badgeColor =
    badge.tone === "error"
      ? "var(--hud-red)"
      : badge.tone === "wait"
        ? "var(--hud-amber)"
        : badge.tone === "busy"
          ? "var(--hud-cyan)"
          : "var(--hud-green)";

  const handleSubmit = useCallback(
    (e) => {
      e.preventDefault();
      const text = inputRef.current?.value?.trim();
      if (text) {
        onSend(text);
        inputRef.current.value = "";
        // Reset the textarea height after clearing.
        if (multiline && inputRef.current) {
          inputRef.current.style.height = "auto";
        }
        inputRef.current.focus();
      }
    },
    [onSend, multiline],
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

  // Auto-grow the textarea with its content, up to a cap.
  const autoGrow = useCallback(() => {
    const el = inputRef.current;
    if (!el || !multiline) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [multiline]);

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
    form.style.borderColor = on ? "var(--input-border-focus)" : "var(--hud-edge)";
    form.style.boxShadow = on ? "0 0 12px color-mix(in srgb, var(--zt-accent) 18%, transparent)" : "inset 0 1px 0 color-mix(in srgb, var(--zt-accent) 8%, transparent)";
  };

  const sharedProps = {
    ref: inputRef,
    id: "zaytoon-composer",
    maxLength: 8000,
    "aria-label": language === "ar" ? "رسالتك" : "Your message",
    placeholder: language === "ar" ? "اكتب أمراً…" : "ENTER COMMAND…",
    className: "w-full",
    style: {
      padding: "var(--space-3) var(--space-4)",
      background: "var(--zt-bg-input)",
      border: "1px solid var(--zt-border-default)",
      borderRadius: "var(--zt-radius-sm)",
      color: "var(--zt-text-primary)",
      caretColor: "var(--zt-accent)",
      fontFamily: "var(--font-ui)",
      fontSize: "var(--font-md)",
      letterSpacing: "0.02em",
      ...(multiline
        ? { resize: "none", overflowY: "auto", minHeight: "44px", lineHeight: 1.5 }
        : {}),
    },
    dir: language === "ar" ? "rtl" : "ltr",
    onKeyDown: handleKeyDown,
    onFocus: () => setFocused(true),
    onBlur: () => setFocused(false),
    onInput: multiline ? autoGrow : undefined,
    onCompositionStart: () => setComposing(true),
    onCompositionEnd: () => setComposing(false),
  };

  return (
    <form
      ref={formRef}
      onSubmit={handleSubmit}
      className="flex items-center"
      style={{
        background: "color-mix(in srgb, var(--bg0) 72%, transparent)",
        border: "var(--border-w) solid var(--hud-edge)",
        boxShadow: "inset 0 1px 0 color-mix(in srgb, var(--hud-cyan) 4%, transparent)",
        transition: "border-color 120ms ease-out, box-shadow 120ms ease-out",
      }}
    >
      <div className="flex-1 relative min-w-0 flex items-center">
        {multiline ? (
          <textarea {...sharedProps} rows={1} />
        ) : (
          <input {...sharedProps} type="text" />
        )}
        {canRecord && (
          <button
            type="button"
            /* Always rendered, and always a toggle. It used to be unmounted
               while recording (`onVoiceStart={voiceMode ? undefined : ...}`),
               which left no way to stop a recording from the UI once the
               dock's duplicate mic was removed. The control must persist
               across the state change it controls. */
            onClick={() => (recorderOn ? onVoiceStop?.() : onVoiceStart?.())}
            className="absolute top-1/2 -translate-y-1/2 flex items-center justify-center transition-all"
            style={{
              width: "var(--ctl-size)",
              height: "var(--ctl-size)",
              color: recorderOn ? badgeColor : "var(--hud-text-mute)",
              insetInlineEnd: "var(--space-2)",
              background: recorderOn ? `color-mix(in srgb, ${badgeColor} 10%, transparent)` : "transparent",
              border: `var(--border-w) solid ${recorderOn ? `color-mix(in srgb, ${badgeColor} 35%, transparent)` : "var(--hud-edge-faint)"}`,
            }}
            title={
              recorderOn
                ? language === "ar" ? "إيقاف وضع الصوت" : "Disable voice mode"
                : language === "ar" ? "تفعيل وضع الصوت" : "Enable voice mode"
            }
            aria-label={
              recorderOn
                ? language === "ar" ? "إيقاف وضع الصوت" : "Disable voice mode"
                : language === "ar" ? "تفعيل وضع الصوت" : "Enable voice mode"
            }
            aria-pressed={recorderOn}
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

      {onStop && busy && (
        <button
          type="button"
          onClick={onStop}
          className="font-mono font-bold transition-all flex-none"
          style={{
            padding: "var(--space-3) var(--space-4)",
            background: "color-mix(in srgb, var(--hud-red) 10%, transparent)",
            color: "var(--hud-red)",
            border: "var(--border-w) solid var(--hud-edge)",
            borderInlineStartColor: "var(--hud-edge-faint)",
            fontSize: "var(--font-md)",
            letterSpacing: "0.18em",
          }}
          title={language === "ar" ? "إيقاف التوليد الحالي" : "Stop the current response"}
          aria-label={language === "ar" ? "إيقاف" : "Stop"}
        >
          {language === "ar" ? "إيقاف" : "STOP"}
        </button>
      )}

      <button
        type="submit"
        disabled={!isConnected}
        className="font-mono font-bold transition-all disabled:opacity-25 flex-none"
        style={{
          padding: "var(--space-3) var(--space-5)",
          background: "color-mix(in srgb, var(--hud-cyan) 8%, transparent)",
          color: "var(--hud-cyan)",
          border: "var(--border-w) solid var(--hud-edge)",
          borderInlineStartColor: "var(--hud-edge-faint)",
          fontSize: "var(--font-md)",
          letterSpacing: "0.18em",
          textShadow: "0 0 8px var(--hud-glow-soft)",
        }}
        onMouseEnter={(e) => {
          if (!isConnected) return;
          e.currentTarget.style.background = "color-mix(in srgb, var(--hud-cyan) 16%, transparent)";
        }}
        onMouseLeave={(e) => { e.currentTarget.style.background = "color-mix(in srgb, var(--hud-cyan) 8%, transparent)"; }}
      >
        {language === "ar" ? "إرسال" : "SEND"}
      </button>

      {badge.text && (
        <span
          className="font-mono flex-none"
          style={{
            padding: "0 var(--space-3)",
            color: badgeColor,
            fontSize: "var(--font-sm)",
            letterSpacing: "0.18em",
          }}
          role="status"
          aria-live="polite"
          data-voice-state={badge.key}
        >
          <span aria-hidden="true">●</span> {badge.text}
        </span>
      )}
    </form>
  );
});
