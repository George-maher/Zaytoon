import { memo } from "react";

/**
 * CommandConsole — the bottom command region.
 *
 * Composes the existing InputBar (which owns all keyboard and voice
 * behaviour, and carries the accessible labels the tests assert) with the
 * system dock. It adds framing only: a command prompt glyph, a live
 * transcription strip, and the system control group.
 *
 * There is exactly one microphone in the product. It lives in the InputBar,
 * because that is where the recording state and the permission request are;
 * the dock therefore carries settings / stop / language only. Two competing
 * mic buttons is the kind of redundancy a HUD cannot afford.
 */
function CommandConsole({
  onSend,
  isConnected,
  language,
  onVoiceStart,
  onVoiceStop,
  voiceMode,
  isListening,
  subtitle,
  InputBar,
  BottomDock,
  onStop,
  onSettingsToggle,
  settingsOpen,
  onLanguageToggle,
}) {
  const ar = language === "ar";

  return (
    <div className="hud-panel hud-bracket" style={{ "--state-color": "var(--hud-cyan)" }}>
      <header className="hud-panel-head">
        <span>{ar ? "وحدة الأوامر" : "COMMAND CONSOLE"}</span>
        <span className="hud-micro ml-auto">
          {isConnected ? (ar ? "متصل" : "LINK OK") : (ar ? "مقطوع" : "LINK DOWN")}
        </span>
      </header>

      <div className="p-2 flex flex-col gap-2">
        {/* Live transcription strip. Only while the user is actually being
            heard, so it cannot be mistaken for a response. */}
        {(isListening || (voiceMode && subtitle)) && (
          <div
            className="flex items-center gap-2 px-2 py-1"
            style={{
              border: "1px solid var(--hud-edge-faint)",
              background: "rgba(0, 229, 255, 0.04)",
            }}
            aria-live="polite"
          >
            <span className="hud-led hud-led--pulse" style={{ "--state-color": "var(--hud-green)" }} aria-hidden="true" />
            <span className="hud-micro" style={{ color: "var(--hud-green)" }}>
              {ar ? "جارٍ الاستماع" : "LISTENING"}
            </span>
            <span
              className="truncate text-right flex-1"
              style={{ fontFamily: "var(--font-mono)", fontSize: 10, color: "var(--hud-text-dim)" }}
            >
              {subtitle || (ar ? "تحدث الآن…" : "speak now…")}
            </span>
          </div>
        )}

        <div className="flex items-stretch gap-2">
          {/* Prompt glyph. Decorative: the field itself carries the label. */}
          <span
            className="self-center select-none pl-1"
            style={{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--hud-cyan)" }}
            aria-hidden="true"
          >
            &gt;
          </span>

          <div className="flex-1 min-w-0">
            <InputBar
              onSend={onSend}
              isConnected={isConnected}
              language={language}
              onVoiceStart={onVoiceStart}
              onVoiceStop={onVoiceStop}
              voiceMode={voiceMode}
            />
          </div>

          <div className="flex items-center flex-none">
            <BottomDock
              voiceMode={voiceMode}
              language={language}
              onLanguageToggle={onLanguageToggle}
              onSettingsToggle={onSettingsToggle}
              settingsOpen={settingsOpen}
              onStop={onStop}
              hideMic
            />
          </div>
        </div>
      </div>
    </div>
  );
}

export default memo(CommandConsole);
