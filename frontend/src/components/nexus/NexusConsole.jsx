import { memo } from "react";
import { CHIPS } from "./nexusFormat";
import { translate } from "../../i18n/strings";

/**
 * NexusConsole — the right rail: the transcript, the quick actions, and the
 * one input the product owns (InputBar, unchanged — it carries every
 * accessibility label the tests assert).
 *
 * The reference's chips redeployed as REAL commands: each one sends its text
 * through the same WebSocket path a typed message takes. The header's
 * download button exports the transcript to a .txt file; the trash clears the
 * conversation — both existing capabilities, not mock decoration.
 */

const isArabicText = (text) => /[\u0600-\u06FF]/.test(text || "");

function hhmmss(ts) {
  if (typeof ts !== "number" || !Number.isFinite(ts)) return null;
  const d = new Date(ts);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function NexusConsole({
  language,
  messages,
  isTyping,
  isConnected,
  onSend,
  onStop,
  isSpeaking,
  InputBar,
  onClear,
  voiceMode,
  voiceState = "disabled",
  onVoiceStart,
  onVoiceStop,
}) {
  const t = (key) => translate(language, key);
  const userCount = messages.filter((m) => m.role === "user").length;

  const download = () => {
    const text = messages
      .map((m) => {
        const role = m.role === "user" ? "YOU" : m.role === "system" ? "SYS" : "ZAYTOON";
        const stamp = hhmmss(m.ts) || "";
        return `[${stamp}] ${role}: ${m.content}`;
      })
      .join("\n");
    const blob = new Blob([text || ""], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "nexus-transcript.txt";
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <section className="nx-panel nx-console" aria-label={t("nexus.console_panel")}>
      <header className="nx-panel-head">
        <span className="nx-panel-title">{t("nexus.console_panel")}</span>
        <span className="nx-panel-sub">
          {userCount} {t("nexus.msg")}
        </span>
        <button
          type="button"
          className="nx-iconbtn"
          style={{ width: 26, height: 26 }}
          onClick={download}
          aria-label={t("nexus.export_transcript")}
          title={t("nexus.export_title")}
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
            <polyline points="7 10 12 15 17 10" />
            <line x1="12" y1="15" x2="12" y2="3" />
          </svg>
        </button>
        <button
          type="button"
          className="nx-iconbtn"
          style={{ width: 26, height: 26 }}
          onClick={onClear}
          aria-label={t("nexus.clear_transcript")}
          title={t("nexus.clear_title")}
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <polyline points="3 6 5 6 21 6" />
            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
          </svg>
        </button>
      </header>

      <div
        className="nx-transcript"
        role="log"
        aria-live="polite"
      >
        {/* Empty state: an onboarding prompt, not an invented transcript. */}
        {messages.length === 0 && !isTyping && (
          <p className="nx-msg nx-msg-system">{t("nexus.empty_transcript")}</p>
        )}

        {messages.map((msg, i) => (
          <div
            key={msg.id || i}
            className={`nx-msg ${
              msg.role === "user" ? "nx-msg-user" : msg.role === "system" ? "nx-msg-system" : "nx-msg-assistant"
            }`}
            data-dir={isArabicText(msg.content) ? "rtl" : "ltr"}
            style={
              isArabicText(msg.content)
                ? { alignSelf: msg.role === "user" ? "flex-end" : "flex-start" }
                : undefined
            }
          >
            {msg.content}
            {msg.ts && <span className="nx-msg-ts">{hhmmss(msg.ts)}</span>}
          </div>
        ))}

        {isTyping && (
          <p className="nx-msg nx-msg-system">…</p>
        )}
      </div>

      <div className="nx-chips">
        {CHIPS.map((chip) => (
          <button
            key={chip.key}
            type="button"
            className="nx-chip"
            onClick={() => onSend(chip.command)}
          >
            {t(chip.textKey)}
          </button>
        ))}
      </div>

      <div className="nx-console-input">
        <InputBar
          onSend={onSend}
          isConnected={isConnected}
          language={language}
          onStop={onStop}
          isSpeaking={isSpeaking}
          voiceMode={voiceMode}
          voiceState={voiceState}
          onVoiceStart={onVoiceStart}
          onVoiceStop={onVoiceStop}
        />
      </div>
    </section>
  );
}

export default memo(NexusConsole);
