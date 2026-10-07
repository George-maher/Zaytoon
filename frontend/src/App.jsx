import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import useWebSocket from "./hooks/useWebSocket";
import useWorkspace from "./hooks/useWorkspace";
import { statusLabel } from "./lib/statusLabels";
import { desktop } from "./lib/desktop";
import AICore from "./components/hud/AICore";
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
import WorkspaceShell from "./components/workspace/WorkspaceShell";

/**
 * Zaytoon: a workspace with a conversation in it, not a chat with panels
 * attached.
 *
 * Composition, per docs/DESIGN_SYSTEM.md §5 and docs/ARCHITECTURE.md §6:
 *
 *   shell     header, then three columns
 *   left      projects · files · skills · models   — *what is this project*
 *   centre    transcript + activity + console + editor — *what are we doing*
 *   right     tasks · terminal                    — *what happened*
 *
 * The rails collapse into a tabbed drawer below 1280px rather than merely
 * shrinking: the centre column is the two things that must survive on a phone,
 * and a 12px-wide squeezed rail helps nobody.
 *
 * The confirmation dialog is mounted **here, above everything**, and is not part
 * of the shell. That is deliberate and load-bearing: the approval gate is a
 * security boundary, and a boundary that a layout decision could move or hide is
 * not one.
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

export default memo(function App() {
  const {
    messages, isConnected, everConnected, isTyping, status, language, setLanguage,
    subtitle, voiceMode, awaitingCommand, isListening, isSpeaking, reminders,
    sendMessage, sendReminderCmd, refreshReminders, clearChat,
    startVoiceMode, stopVoiceMode, pendingConfirmation, respondToConfirmation,
    speakingVoice, plan, activeTool, activity, intent, requestStop,
    tasks, activeTask, cancelTask, terminalLines, watchTerminal, fileChanges,
  } = useWebSocket();

  // The workspace is a separate hook rather than more state here, so the file
  // tree, the editor buffer and the terminal survive a transcript clear and can
  // be tested without mounting the socket.
  const workspace = useWorkspace(language);

  const [settingsOpen, setSettingsOpen] = useState(false);
  const containerRef = useRef(null);
  const wide = useWide();

  // The reminder list lives in hook state and is populated only by a
  // `reminder_list` frame, so ask for one as soon as the socket is up.
  // Without this the panel reads "No pending reminders" until someone finds
  // the refresh button — a *false* statement whenever a reminder exists.
  useEffect(() => {
    if (isConnected) refreshReminders();
  }, [isConnected, refreshReminders]);

  // The shell's panel focus lives here because the bottom dock — rendered
  // into the command console, *outside* WorkspaceShell — drives it too. The
  // header nav inside the shell writes through the same two setters, so
  // there is exactly one source of truth for "what is open".
  const [shellTab, setShellTab] = useState("projects");
  const [shellInspectorTab, setShellInspectorTab] = useState("tasks");

  /**
   * The dock's panel strip (TERM / LOG / DIFF / TASK).
   *
   * TERM and TASK move the inspector — and, below the breakpoint, the shared
   * drawer tab, which is what `shellTab` is down there. LOG and DIFF are
   * centre-column jumps (the execution log, the editor): there is no rail
   * panel for them to switch *to*, so they scroll rather than pretend.
   * `scrollIntoView?.()` is called optionally because jsdom does not
   * implement it — a control that throws in a test browser would be a
   * control that lies.
   */
  const handlePanelSelect = useCallback(
    (id) => {
      if (id === "log" || id === "diff") {
        document
          .getElementById(id === "log" ? "zaytoon-activity" : "zaytoon-editor")
          ?.scrollIntoView?.({ behavior: "smooth", block: "nearest" });
        return;
      }
      setShellInspectorTab(id);
      if (!wide) setShellTab(id);
    },
    [wide],
  );

  // Which strip button reads as active: the inspector when wide, the shared
  // drawer tab when not. Centre jumps have no persistent panel and so never
  // claim one; a left-rail id (files, …) matches no strip button.
  const dockPanel = wide ? shellInspectorTab : shellTab;

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

  // Native bridge integration: use OS notifications for reminders when running
  // inside the desktop host. In a regular browser this is a no-op.
  useEffect(() => {
    if (!desktop.available) return;
    if (reminders.length > 0) {
      const latest = reminders[reminders.length - 1];
      if (latest && latest.task) {
        desktop.notify("Zaytoon Reminder", latest.task);
      }
    }
  }, [reminders]);

  const ar = language === "ar";

  /**
   * What the rails carry on top of their tabs.
   *
   * The mission summary stays above the inspector. Telemetry *moved*: `—
   * SystemStatus` is now the inspector's **System** tab (a right-rail panel,
   * where the command-centre map puts it), so the left rail starts at its
   * tab list — which is what a workspace rail should open with — and the
   * system figures are one click away instead of permanently occupying the
   * top of the project list.
   */
  const extras = {
    right: (
      <MissionStatus
        intent={intent}
        state={state}
        activeTool={activeTool}
        awaitingCommand={awaitingCommand}
        pendingConfirmation={pendingConfirmation}
        reminders={reminders}
        language={language}
      />
    ),
  };

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
      <WorkspaceShell
        workspace={workspace}
        socket={{ tasks, activeTask, cancelTask, terminalLines, watchTerminal, fileChanges }}
        extras={extras}
        language={language}
        wide={wide}
        tab={shellTab}
        onTabChange={setShellTab}
        inspectorTab={shellInspectorTab}
        onInspectorTabChange={setShellInspectorTab}
        onOpenSettings={() => setSettingsOpen((v) => !v)}
      >
        {/* Core column. Never removed at any width. */}
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
          style={{ maxHeight: "38vh", padding: "0 var(--space-4)" }}
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

        {/* The activity feed lives under the transcript rather than in a rail:
            it describes the turn that is happening now, and the rail it used to
            occupy now holds the tasks that record it. */}
        <div
          data-enter="center"
          id="zaytoon-activity"
          className="flex-none w-full max-w-3xl mx-auto"
          style={{ padding: "var(--space-3) var(--space-4) 0" }}
        >
          <Panel title={ar ? "نشاط التنفيذ" : "EXECUTION LOG"} code="LOG">
            <ActivityFeed
              plan={plan}
              activeTool={activeTool}
              activity={activity}
              language={language}
            />
          </Panel>
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
            onPanelSelect={handlePanelSelect}
            activePanel={dockPanel}
          />
        </div>
      </WorkspaceShell>

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
