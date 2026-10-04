import { memo, useEffect, useMemo, useRef, useState } from "react";
import useWebSocket from "./hooks/useWebSocket";
import { statusLabel } from "./lib/statusLabels";
import AICore from "./components/hud/AICore";
import SystemStatus from "./components/hud/SystemStatus";
import MissionStatus from "./components/hud/MissionStatus";
import StatusIndicator from "./components/hud/StatusIndicator";
import { resolveState } from "./components/hud/hudState";
import CommandConsole from "./components/hud/CommandConsole";
import Panel from "./components/hud/HUDPanel";
import BottomDock from "./components/BottomDock";
import InputBar from "./components/InputBar";
import ActivityFeed from "./components/Chat/ActivityFeed";
import ChatTranscript from "./components/Chat/ChatTranscript";
import ConfirmationDialog from "./components/ConfirmationDialog";
import ReminderPanel from "./components/ReminderPanel";
import SettingsPanel from "./components/SettingsPanel";

/**
 * Zaytoon command deck.
 *
 * Composition, per docs/DESIGN_SYSTEM.md §5:
 *
 *   header   identity · reminders · language · state LED
 *   left     live backend telemetry
 *   centre   AI core, with the conversation beneath it
 *   right    mission state and the engine's activity feed
 *   console  command input + system controls
 *
 * The rails collapse into a tabbed drawer below 1024px rather than merely
 * shrinking: the core and the console are the two things that must survive on
 * a phone, and a 12px-wide squeezed rail helps nobody.
 */

/**
 * Render the rails once, not twice.
 *
 * An earlier pass emitted the left/right rails in both a desktop <aside> and a
 * mobile drawer, so every panel existed twice in the DOM. Besides doubling
 * the panels' DOM cost, that gave assistive technology two copies of every
 * landmark and two live status regions to announce. This picks one placement
 * from a media query instead.
 */
function useWide() {
  const [wide, setWide] = useState(() =>
    typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia("(min-width: 1280px)").matches
      : true,
  );
  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return undefined;
    const mq = window.matchMedia("(min-width: 1280px)");
    const onChange = (e) => setWide(e.matches);
    mq.addEventListener?.("change", onChange);
    return () => mq.removeEventListener?.("change", onChange);
  }, []);
  return wide;
}

function RailTabs({ tabs, active, onSelect, language }) {
  const ar = language === "ar";
  return (
    <div
      className="flex"
      style={{ gap: "var(--space-1)", padding: "var(--space-1) var(--space-2) 0" }}
      role="tablist"
      aria-label={ar ? "اللوحات" : "Panels"}
    >
      {tabs.map((t) => (
        <button
          key={t.id}
          role="tab"
          aria-selected={active === t.id}
          onClick={() => onSelect(t.id)}
          className="hud-micro transition-colors"
          style={{
            padding: "var(--space-1) var(--space-3)",
            color: active === t.id ? "var(--hud-cyan)" : "var(--hud-text-mute)",
            border: "var(--border-w) solid",
            borderColor: active === t.id ? "var(--hud-edge-strong)" : "var(--hud-edge-faint)",
            background: active === t.id ? "color-mix(in srgb, var(--hud-cyan) 6%, transparent)" : "transparent",
          }}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

export default memo(function App() {
  const {
    messages, isConnected, everConnected, isTyping, status, language, setLanguage,
    subtitle, voiceMode, awaitingCommand, isListening, isSpeaking, reminders,
    sendMessage, sendReminderCmd, refreshReminders, clearChat,
    startVoiceMode, stopVoiceMode, pendingConfirmation, respondToConfirmation,
    speakingVoice, plan, activeTool, activity, intent, requestStop,
  } = useWebSocket();

  const [settingsOpen, setSettingsOpen] = useState(false);
  const [tab, setTab] = useState("sys");
  const containerRef = useRef(null);
  const wide = useWide();

  const state = useMemo(
    () => resolveState({ isConnected, pendingConfirmation, awaitingCommand, isListening, isTyping, isSpeaking, status }),
    [isConnected, pendingConfirmation, awaitingCommand, isListening, isTyping, isSpeaking, status],
  );

  // One derived sentence for "what is happening now" — shown under the
  // console (aria-live) and reused by the transcript's typing indicator, so
  // the two can never disagree. Pure and precedence-tested in
  // lib/statusLabels.test.js.
  const statusText = useMemo(() => {
    const last = messages[messages.length - 1];
    const streaming = Boolean(last && last.role === "assistant" && last.streaming);
    return statusLabel({
      connected: isConnected,
      everConnected,
      status,
      intent,
      plan,
      activeTool,
      streaming,
      isSpeaking,
      isTyping,
      pendingConfirmation,
      language,
    });
  }, [messages, isConnected, everConnected, status, intent, plan, activeTool, isSpeaking, isTyping, pendingConfirmation, language]);

  // Hue for the line: amber while blocked on a person, red without a link,
  // cyan while the system is simply working.
  const statusTone = pendingConfirmation
    ? "warn"
    : !isConnected
      ? "danger"
      : "accent";

  // Global text direction. Message content is still detected per-message,
  // because one session can contain both scripts.
  useEffect(() => {
    const root = document.documentElement;
    root.setAttribute("lang", language);
    root.setAttribute("dir", language === "ar" ? "rtl" : "ltr");
  }, [language]);

  // Entrance is a transform on three layers, once per language change. It is
  // deliberately not re-run on every state change -- a HUD that re-animates on
  // each frame of a response is unreadable.
  useEffect(() => {
    const root = containerRef.current;
    if (!root) return undefined;
    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    if (reduced) return undefined;
    const ctx = document.createAnimations?.();
    void ctx;
    // rAF rather than a timer so it paints after layout and cannot cause a
    // flash of unstyled rail.
    const raf = requestAnimationFrame(() => {
      root.querySelectorAll("[data-enter]").forEach((el, i) => {
        const from = el.dataset.enter;
        el.style.opacity = "0";
        el.style.transform = from === "left" ? "translateX(-14px)" : from === "right" ? "translateX(14px)" : "translateY(10px)";
        el.style.transition = `opacity 320ms ease-out ${i * 60}ms, transform 320ms ease-out ${i * 60}ms`;
        requestAnimationFrame(() => {
          el.style.opacity = "1";
          el.style.transform = "none";
        });
      });
    });
    return () => cancelAnimationFrame(raf);
  }, [language]);

  const ar = language === "ar";

  const leftRail = (
    <div
      data-enter="left"
      className="flex flex-col overflow-y-auto min-h-0"
      style={{ gap: "var(--space-3)", padding: "var(--space-3)" }}
    >
      <SystemStatus language={language} />
    </div>
  );

  const rightRail = (
    <div
      data-enter="right"
      className="flex flex-col overflow-y-auto min-h-0"
      style={{ gap: "var(--space-3)", padding: "var(--space-3)" }}
    >
      <MissionStatus
        intent={intent}
        state={state}
        activeTool={activeTool}
        awaitingCommand={awaitingCommand}
        pendingConfirmation={pendingConfirmation}
        reminders={reminders}
        language={language}
      />
      <Panel title={ar ? "نشاط التنفيذ" : "EXECUTION LOG"} code="LOG">
        <ActivityFeed
          plan={plan}
          activeTool={activeTool}
          activity={activity}
          language={language}
        />
      </Panel>
    </div>
  );

  return (
    <div
      ref={containerRef}
      className="h-screen w-screen flex flex-col overflow-hidden hud-void"
    >
      {/* Environment layers. Static, cheap, and behind everything. */}
      <div className="hud-grid" aria-hidden="true" />
      <div className="hud-traces" aria-hidden="true" />
      <div className="hud-scan" aria-hidden="true" />

      {/* ---------------------------------------------------------------- */}
      <header
        className="flex-none flex items-center justify-between"
        style={{
          padding: "var(--space-2) var(--space-4)",
          borderBottom: "var(--border-w) solid var(--hud-edge-faint)",
          background: "color-mix(in srgb, var(--bg0) 70%, transparent)",
        }}
      >
        <div className="flex items-center min-w-0" style={{ gap: "var(--space-3)" }}>
          <span
            className="flex items-center justify-center flex-none"
            style={{
              width: 24,
              height: 24,
              border: "var(--border-w) solid var(--hud-edge-strong)",
              background: "color-mix(in srgb, var(--hud-cyan) 6%, transparent)",
            }}
            aria-hidden="true"
          >
            <span className="hud-identity" style={{ fontSize: "var(--font-md)", letterSpacing: 0 }}>Z</span>
          </span>
          <div className="min-w-0">
            <h1 className="hud-identity">ZAYTOON</h1>
            <p className="hud-micro truncate" style={{ fontSize: "var(--font-xs)" }}>
              {ar ? "واجهة الأوامر" : "TACTICAL COMMAND INTERFACE"}
            </p>
          </div>
        </div>

        <div className="flex items-center flex-none" style={{ gap: "var(--space-2)" }}>
          <ReminderPanel
            reminders={reminders}
            onRefresh={refreshReminders}
            onDelete={(id) => sendReminderCmd("delete", id)}
            onDone={(id) => sendReminderCmd("done", id)}
            language={language}
          />
          <StatusIndicator state={state} language={language} />
        </div>
      </header>

      {/* ---------------------------------------------------------------- */}
      <main className="flex-1 flex overflow-hidden min-h-0">
        {/* Rails, from 1280px up. */}
        {wide && (
          <>
            <aside
              className="flex-none w-[268px] flex flex-col overflow-hidden"
              style={{ borderInlineEnd: "var(--border-w) solid var(--hud-edge-faint)" }}
              aria-label={ar ? "معلومات النظام" : "System information"}
            >
              {leftRail}
            </aside>

            <aside
              className="flex-none w-[300px] flex flex-col overflow-hidden"
              style={{ borderInlineStart: "var(--border-w) solid var(--hud-edge-faint)" }}
              aria-label={ar ? "حالة المهمة" : "Mission state"}
            >
              {rightRail}
            </aside>
          </>
        )}

        {/* Below 1280px the rails become one tabbed drawer. */}
        {!wide && (
          <div
            className="flex-none w-full max-h-[34vh] flex flex-col overflow-hidden"
            style={{ borderBottom: "var(--border-w) solid var(--hud-edge-faint)" }}
          >
            <RailTabs
              language={language}
              active={tab}
              onSelect={setTab}
              tabs={[
                { id: "sys", label: ar ? "النظام" : "SYSTEM" },
                { id: "mission", label: ar ? "المهمة" : "MISSION" },
              ]}
            />
            <div className="flex-1 overflow-y-auto min-h-0">
              {tab === "sys" ? leftRail : rightRail}
            </div>
          </div>
        )}

        {/* Core column. Never removed at any width. */}
        <section className="flex-1 flex flex-col min-w-0 min-h-0">
          <div
            data-enter="center"
            className="flex-1 flex items-center justify-center min-h-0"
            style={{ padding: "var(--space-3) 0" }}
          >
            <AICore
              state={state}
              /* The core must never be the reason a 360px viewport scrolls:
                 at phone widths it steps down from 260 to 180. */
              size={wide ? 260 : 180}
              language={language}
              speakingVoice={speakingVoice}
            />
          </div>

          <div
            data-enter="center"
            className="flex-none flex flex-col min-h-0 mx-auto w-full max-w-3xl"
            style={{ maxHeight: "42vh", padding: "0 var(--space-4)" }}
          >
            <div className="hud-panel hud-bracket flex-1 flex flex-col min-h-0">
              <header className="hud-panel-head">
                <span>{ar ? "السجل" : "TRANSCRIPT"}</span>
                <span className="hud-micro ml-auto">
                  {messages.length} {ar ? "رسالة" : "MSG"}
                </span>
                {messages.length > 0 && (
                  <button
                    onClick={clearChat}
                    className="hud-micro"
                    style={{
                      padding: "var(--space-1) var(--space-2)",
                      border: "var(--border-w) solid var(--hud-edge-faint)",
                      color: "var(--hud-text-dim)",
                    }}
                    title={ar ? "مسح" : "Clear"}
                  >
                    {ar ? "مسح" : "CLR"}
                  </button>
                )}
              </header>
              <ChatTranscript
                messages={messages}
                isTyping={isTyping}
                subtitle={subtitle}
                language={language}
                statusText={statusText}
              />
            </div>
          </div>

          <div
            data-enter="center"
            className="flex-none w-full max-w-3xl mx-auto"
            style={{ padding: "var(--space-3) var(--space-4) var(--space-4)" }}
          >
            <CommandConsole
              InputBar={InputBar}
              BottomDock={BottomDock}
              onSend={sendMessage}
              isConnected={isConnected}
              language={language}
              onVoiceStart={startVoiceMode}
              onVoiceStop={stopVoiceMode}
              voiceMode={voiceMode}
              isListening={isListening}
              subtitle={subtitle}
              statusText={statusText}
              statusTone={statusTone}
              onStop={requestStop}
              onSettingsToggle={() => setSettingsOpen((v) => !v)}
              settingsOpen={settingsOpen}
              onLanguageToggle={() => setLanguage((l) => (l === "en" ? "ar" : "en"))}
            />
          </div>
        </section>
      </main>

      {/* High-risk tool approval gate. Unchanged: the security boundary does
          not move for a redesign. */}
      <ConfirmationDialog
        request={pendingConfirmation}
        onRespond={respondToConfirmation}
        language={language}
      />

      <SettingsPanel
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        language={language}
      />
    </div>
  );
});
