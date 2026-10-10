import { memo } from "react";
import InputBar from "./InputBar";
import StatusLine from "./StatusLine";

/**
 * AIFloatingBar — the composer for screens that do not carry their own.
 *
 * Home has its prompt under "What do you want to do?" and the workspace has
 * the command console; this is the strip that makes every *other* screen its
 * equal. Without it, "an AI you can talk to anywhere" would be a property of
 * two screens out of five: you could open Computer, look at the whole
 * machine, and have no way to ask about any of it.
 *
 * It is rendered by App rather than by each screen, which is the point — the
 * guarantee is structural. A screen added later gets a composer by default
 * instead of by remembering.
 *
 * It reuses `InputBar` rather than growing a second field: one implementation
 * of the send path, one microphone (there is exactly one in the product), one
 * accessible name for the composer. The status line is the shared
 * `StatusLine`, so the turn's progress reads the same here as it does in the
 * workspace console.
 */
function AIFloatingBar({
  language = "en",
  isConnected,
  statusText,
  statusTone,
  onSend,
  onVoiceStart,
  onVoiceStop,
  voiceMode,
}) {
  return (
    <div
      className="flex-none"
      data-testid="ai-floating-bar"
      style={{
        padding: "var(--space-2) var(--space-4) var(--space-3)",
        borderTop: "var(--border-w) solid var(--hud-edge-faint)",
        background: "color-mix(in srgb, var(--hud-void) 88%, transparent)",
      }}
    >
      {statusText ? (
        <div style={{ marginBottom: "var(--space-1)" }}>
          <StatusLine text={statusText} language={language} tone={statusTone} />
        </div>
      ) : null}
      <InputBar
        onSend={onSend}
        isConnected={isConnected}
        language={language}
        onVoiceStart={onVoiceStart}
        onVoiceStop={onVoiceStop}
        voiceMode={voiceMode}
      />
    </div>
  );
}

export default memo(AIFloatingBar);
