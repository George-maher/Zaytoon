import { memo } from "react";
import StatusLine from "../StatusLine";

/**
 * CommandConsole — the bottom command region.
 *
 * Composes the existing InputBar (which owns all keyboard and voice
 * behaviour, and carries the accessible labels the tests assert) with the
 * system dock. It adds framing only: a command prompt glyph, the live status
 * line, a transcription strip, and the system control group.
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
  statusText,
  statusTone,
  InputBar,
  BottomDock,
  onStop,
  onSettingsToggle,
  settingsOpen,
  onLanguageToggle,
  onPanelSelect,
  activePanel,
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

      <div className="flex flex-col" style={{ gap: "var(--space-3)", padding: "var(--space-3)" }}>
        {/* What the engine is doing right now, announced politely. */}
        <StatusLine text={statusText} language={language} tone={statusTone} />

        {/* Live transcription strip. Only while the user is actually being
            heard, so it cannot be mistaken for a response. */}
        {(isListening || (voiceMode && subtitle)) && (
          <div
            className="flex items-center"
            style={{
              gap: "var(--space-2)",
              padding: "var(--space-1) var(--space-3)",
              border: "var(--border-w) solid var(--hud-edge-faint)",
              background: "color-mix(in srgb, var(--hud-cyan) 4%, transparent)",
            }}
            aria-live="polite"
          >
            <span className="hud-led hud-led--pulse" style={{ "--state-color": "var(--hud-green)" }} aria-hidden="true" />
            <span className="hud-micro" style={{ color: "var(--hud-green)" }}>
              {ar ? "جارٍ الاستماع" : "LISTENING"}
            </span>
            <span
              className="truncate text-right flex-1"
              style={{ fontFamily: "var(--font-mono)", fontSize: "var(--font-sm)", color: "var(--hud-text-dim)" }}
            >
              {subtitle || (ar ? "تحدث الآن…" : "speak now…")}
            </span>
          </div>
        )}

        <div className="flex items-stretch" style={{ gap: "var(--space-3)" }}>
          {/* Prompt glyph. Decorative: the field itself carries the label. */}
          <span
            className="self-center select-none"
            style={{ paddingInlineStart: "var(--space-1)", fontFamily: "var(--font-mono)", fontSize: "var(--font-md)", color: "var(--hud-cyan)" }}
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
              onPanelSelect={onPanelSelect}
              activePanel={activePanel}
              hideMic
            />
          </div>
        </div>
      </div>
    </div>
  );
}

export default memo(CommandConsole);
