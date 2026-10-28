import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import useWebSocket from "./hooks/useWebSocket";
import useWorkspace from "./hooks/useWorkspace";
import { statusLabel } from "./lib/statusLabels";
import { desktop } from "./lib/desktop";
import AICore from "./components/HUD/AICore";
import MissionStatus from "./components/HUD/MissionStatus";
import StatusIndicator from "./components/HUD/StatusIndicator";
import { resolveState } from "./components/HUD/hudState";
import CommandConsole from "./components/HUD/CommandConsole";
import Panel from "./components/HUD/HUDPanel";
import BottomDock from "./components/BottomDock";
import InputBar from "./components/InputBar";
import ActivityFeed from "./components/Chat/ActivityFeed";
import ChatTranscript from "./components/Chat/ChatTranscript";
import ConfirmationDialog from "./components/ConfirmationDialog";
import ReminderPanel from "./components/ReminderPanel";
import SettingsPanel from "./components/SettingsPanel";
import SetupWizard from "./components/providers/SetupWizard";
import WorkspaceShell from "./components/workspace/WorkspaceShell";
import NexusDashboard from "./components/nexus/NexusDashboard";
import ComputerView from "./components/ComputerView";
import TaskFlowView from "./components/TaskFlowView";
import AIFloatingBar from "./components/AIFloatingBar";
import CommandPalette from "./components/CommandPalette";
import ViewTransition from "./components/ViewTransition";
import Sidebar from "./components/Sidebar";
import ChatView from "./components/ChatView";
import SkillsView from "./components/SkillsView";
import SecurityView from "./components/SecurityView";
import VirtualMachinesView from "./components/VirtualMachinesView";
import ModelsView from "./components/ModelsView";
import { VIEWS, resolveScreen } from "./lib/viewState";

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

/** Below this, the header's four destinations stop fitting next to the logo. */
const NARROW_QUERY = "(max-width: 767px)";

/**
 * Narrow enough that the header has to shed weight.
 *
 * The same mechanism as `useWide()`, deliberately: both are *structural*
 * decisions — what is rendered and in what shape — and a media query read in
 * JS is the one thing that can change markup on a resize without mounting two
 * copies of the app and asking assistive technology to tell them apart.
 *
 * Presentation that needs no structural change (a panel stacking, a grid
 * collapsing) belongs in CSS; this exists for the three places below where the
 * DOM itself differs:
 *
 * 1. the tagline, which is the widest element in the header's left block —
 *    "TACTICAL COMMAND INTERFACE" is ~200px, more than three times the logo
 *    and the wordmark beside it — so it is what a phone actually runs out of;
 * 2. the nav labels, which collapse to their glyphs with the label kept as
 *    `sr-only` text rather than dropped: the button's accessible name is
 *    identical at every width, so a screen-reader user on a phone and a
 *    sighted user on a phone can reach the same four destinations;
 * 3. the status word, which hands off to `StatusIndicator`'s own `compact`
 *    mode — glyph visible, word announced — rather than inventing a second
 *    way to do what that prop already does.
 *
 * Defaults to *not* narrow when `matchMedia` is unavailable, so an
 * unrecognised host gets the complete header instead of a stripped one.
 */
function useNarrow() {
  const [narrow, setNarrow] = useState(() =>
    typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia(NARROW_QUERY).matches
      : false,
  );
  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return undefined;
    const mq = window.matchMedia(NARROW_QUERY);
    const onChange = (e) => setNarrow(e.matches);
    mq.addEventListener?.("change", onChange);
    return () => mq.removeEventListener?.("change", onChange);
  }, []);
  return narrow;
}

export default memo(function App() {
  const {
    messages, isConnected, everConnected, isTyping, status, language, setLanguage,
    subtitle, voiceMode, awaitingCommand, isListening, isSpeaking, reminders,
    voiceState, stopRecording, cancelVoiceCommand,
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
  const narrow = useNarrow();

  // View state. `setView` is deliberately private: every navigation goes
  // through `navigate()`, which resolves the request against the vocabulary
  // in lib/viewState, so an unknown or not-yet-built destination lands on
  // Home rather than mounting a shell that has nothing to show.
  const [view, setView] = useState(VIEWS.HOME);
  const navigate = useCallback((next) => setView(resolveScreen(next)), []);
  const screen = resolveScreen(view);
  // Command palette (Ctrl+K)
  const [paletteOpen, setPaletteOpen] = useState(false);

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

  /**
   * The mic's off-click means two different things, and conflating them is how
   * both halves broke: while speech is being captured it must *finish* the
   * utterance (send it, keep listening), and while the listener is idle it must
   * turn the listener off. Discarding a finished sentence loses the command;
   * sending a half-spoken one turns "stop" into a ghost turn.
   */
  const handleVoiceStop = useCallback(() => {
    if (isListening) stopRecording();
    else stopVoiceMode();
  }, [isListening, stopRecording, stopVoiceMode]);

  /**
   * STOP. A voice turn is cancelled through the voice channel, which also
   * returns the microphone to standby; a typed turn uses the plain interrupt.
   * Using the plain interrupt during a voice turn left the client's capture
   * gate latched in PROCESSING until the server's own frame happened to land.
   */
  const handleStop = useCallback(() => {
    if (voiceMode) cancelVoiceCommand();
    else requestStop();
  }, [voiceMode, cancelVoiceCommand, requestStop]);

  // Which strip button reads as active: the inspector when wide, the shared
  // drawer tab when not. Centre jumps have no persistent panel and so never
  // claim one; a left-rail id (files, …) matches no strip button.
  const dockPanel = wide ? shellInspectorTab : shellTab;

  // -- Open Folder / Open File workflows -----------------------------------

  const handleOpenFolder = useCallback(
    async (path) => {
      // Set as workspace root and open the project
      const result = await workspace.openProject(path);
      if (result?.error) {
        workspace.report(result.error);
        return;
      }
      navigate(VIEWS.WORKSPACE);
    },
    [workspace, navigate],
  );

  const handleOpenFile = useCallback(
    async (path) => {
      // Open the file in the editor
      const result = await workspace.open(path);
      if (result?.error) {
        workspace.report(result.error);
        return;
      }
      // Also try to detect the project context from the file's directory
      const lastSlash = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
      if (lastSlash > 0) {
        const dir = path.substring(0, lastSlash);
        await workspace.openProject(dir);
      }
      navigate(VIEWS.WORKSPACE);
    },
    [workspace, navigate],
  );

  const handleOpenProject = useCallback(
    async (path) => {
      await handleOpenFolder(path);
    },
    [handleOpenFolder],
  );

  // -- Command palette: pickers and the composer ---------------------------

  /**
   * Focus a field by id, on the frame *after* the palette closes.
   *
   * Doing it synchronously would focus a control behind a modal that is about
   * to unmount, and the browser would hand focus to `<body>` instead.
   */
  const focusById = useCallback((id) => {
    requestAnimationFrame(() => document.getElementById(id)?.focus());
  }, []);

  /**
   * Palette → Open Folder / Open File.
   *
   * These must never be called with no path: `handleOpenFolder(undefined)`
   * sends a request that cannot succeed and reports the resulting "path is
   * required" back as though something had gone wrong. So the command either
   * uses a native picker, or hands off to the one field built for typing a
   * path — Home's input, which is the documented browser fallback. In an
   * environment with neither, CommandPalette drops the command entirely
   * rather than offering a row that does nothing.
   */
  const paletteOpenFolder = useCallback(async () => {
    if (desktop.available) {
      const path = await desktop.openFolderDialog();
      if (path) await handleOpenFolder(path);
      return;
    }
    navigate(VIEWS.HOME);
    focusById("home-path");
  }, [handleOpenFolder, navigate, focusById]);

  const paletteOpenFile = useCallback(async () => {
    if (desktop.available) {
      const path = await desktop.openFileDialog();
      if (path) await handleOpenFile(path);
      return;
    }
    navigate(VIEWS.HOME);
    focusById("home-path");
  }, [handleOpenFile, navigate, focusById]);

  /** Palette → Ask Zaytoon: put the caret in the composer, wherever it is. */
  const paletteAsk = useCallback(() => focusById("zaytoon-composer"), [focusById]);

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
      voiceState,
      language,
    });
  }, [messages, isConnected, everConnected, status, intent, plan, activeTool, isSpeaking, isTyping, pendingConfirmation, voiceState, language]);

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

  // Ctrl+K and Ctrl+Space both open the command palette — the two chords a
  // command surface is expected to answer to, and the ones the product
  // documents. Guarded on no text input having focus for Ctrl+Space, because
  // that chord is insert-a-space in some editors and stealing it from an
  // in-progress message would be a bug, not a shortcut.
  useEffect(() => {
    const onKey = (e) => {
      if (!(e.ctrlKey || e.metaKey)) return;
      if (e.key === "k" || e.key === " ") {
        if (e.key === " " && e.target instanceof HTMLElement && e.target.closest("input, textarea, [contenteditable=true]")) {
          return;
        }
        e.preventDefault();
        setPaletteOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

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
      className="h-screen w-screen flex overflow-hidden hud-void"
    >
      {/* Environment layers. Static, cheap, and behind everything. */}
      <div className="hud-grid" aria-hidden="true" />
      <div className="hud-traces" aria-hidden="true" />
      {/* The reference frames its artwork with a ruler of short ticks; here
          it marks the top of the deck, masked so it fades rather than ends. */}
      <div className="hud-ticks" aria-hidden="true" />
      <div className="hud-scan" aria-hidden="true" />

      {/* Sidebar — the product's only navigation. On HOME the Nexus dashboard
          is its own navigator (its dock), so the rail stands aside there. */}
      {screen !== VIEWS.HOME && (
        <Sidebar
          view={screen}
          language={language}
          onNavigate={navigate}
          onOpenProject={handleOpenProject}
          isConnected={isConnected}
        />
      )}

      {/* ---------------------------------------------------------------- */}
      <div className="flex-1 flex flex-col min-w-0 min-h-0">
      {/* On HOME the Nexus dashboard owns the whole deck — its top bar and
          dock replace the shell chrome, by design. Any other screen keeps the
          chrome below unchanged. */}
      {screen !== VIEWS.HOME && (
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
            {!narrow && (
              <p className="hud-micro truncate" style={{ fontSize: "var(--font-xs)" }}>
                {ar ? "واجهة الأوامر" : "TACTICAL COMMAND INTERFACE"}
              </p>
            )}
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
          <StatusIndicator state={state} language={language} compact={narrow} />
        </div>
      </header>
      )}

      {/* ---------------------------------------------------------------- */}
      <ViewTransition viewKey={screen} className="flex-1 flex flex-col min-h-0">
      {screen === VIEWS.WORKSPACE ? (
      <WorkspaceShell
        workspace={workspace}
        socket={{ tasks, activeTask, cancelTask, terminalLines, watchTerminal, fileChanges }}
        extras={extras}
        language={language}
        wide={wide}
        narrow={narrow}
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
            onVoiceStop={handleVoiceStop}
            voiceMode={voiceMode}
            voiceState={voiceState}
            isListening={isListening}
            subtitle={subtitle}
            statusText={statusText}
            statusTone={statusTone}
            onStop={handleStop}
            onSettingsToggle={() => setSettingsOpen((v) => !v)}
            settingsOpen={settingsOpen}
            onLanguageToggle={() => setLanguage((l) => (l === "en" ? "ar" : "en"))}
            onPanelSelect={handlePanelSelect}
            activePanel={dockPanel}
          />
        </div>
      </WorkspaceShell>
      ) : screen === VIEWS.CHAT ? (
        <ChatView
          language={language}
          messages={messages}
          isTyping={isTyping}
          isConnected={isConnected}
          statusText={statusText}
          statusTone={statusTone}
          subtitle={subtitle}
          plan={plan}
          activeTool={activeTool}
          activity={activity}
          onSend={sendMessage}
          onVoiceStart={startVoiceMode}
          onVoiceStop={handleVoiceStop}
          voiceMode={voiceMode}
          voiceState={voiceState}
          onStop={handleStop}
          isSpeaking={isSpeaking}
          pendingConfirmation={pendingConfirmation}
        />
      ) : screen === VIEWS.COMPUTER ? (
        <ComputerView language={language} onOpenFile={handleOpenFile} />
      ) : screen === VIEWS.TASKS ? (
        <TaskFlowView
          language={language}
          plan={plan}
          activeTool={activeTool}
          activity={activity}
          tasks={tasks}
          activeTask={activeTask}
          onCancel={cancelTask}
        />
      ) : screen === VIEWS.SKILLS ? (
        <SkillsView language={language} />
      ) : screen === VIEWS.SECURITY ? (
        <SecurityView language={language} />
      ) : screen === VIEWS.MACHINES ? (
        <VirtualMachinesView language={language} />
      ) : screen === VIEWS.MODELS ? (
        <ModelsView language={language} />
      ) : (
        <NexusDashboard
          language={language}
          isConnected={isConnected}
          state={state}
          isListening={isListening}
          isSpeaking={isSpeaking}
          isTyping={isTyping}
          messages={messages}
          sendMessage={sendMessage}
          startVoiceMode={startVoiceMode}
          stopVoiceMode={stopVoiceMode}
          voiceMode={voiceMode}
          voiceState={voiceState}
          requestStop={requestStop}
          clearChat={clearChat}
          onOpenFolder={handleOpenFolder}
          onOpenFile={handleOpenFile}
          onOpenProject={handleOpenProject}
          onOpenCommands={() => setPaletteOpen(true)}
          onSettingsToggle={() => setSettingsOpen((v) => !v)}
          onNavigate={navigate}
          remindersProps={{
            reminders,
            onRefresh: refreshReminders,
            onDelete: (id) => sendReminderCmd("delete", id),
            onDone: (id) => sendReminderCmd("done", id),
            language,
          }}
        />
      )}
      </ViewTransition>

      {/* Every screen can be talked to, and the guarantee is structural: App
          mounts this rather than each screen doing so, so a destination added
          later gets a composer by default instead of by remembering. Home has
          its prompt, the workspace has the console, and Chat carries its own
          multiline composer — there the strip would be a second field for the
          same job, which is why it is absent. */}
      {screen !== VIEWS.HOME && screen !== VIEWS.CHAT && screen !== VIEWS.WORKSPACE && (
        <AIFloatingBar
          language={language}
          isConnected={isConnected}
          statusText={statusText}
          statusTone={statusTone}
          onSend={sendMessage}
          onVoiceStart={startVoiceMode}
          onVoiceStop={handleVoiceStop}
          voiceMode={voiceMode}
          voiceState={voiceState}
          onStop={handleStop}
          busy={isTyping || isSpeaking || !!pendingConfirmation}
        />
      )}

      {/* High-risk tool approval gate. Unchanged: the security boundary does
          not move for a redesign. */}
      <ConfirmationDialog
        request={pendingConfirmation}
        onRespond={respondToConfirmation}
        language={language}
      />

      {/* First-run setup. Rendered unconditionally — the wizard itself reads
          GET /setup/status and returns null unless setup is genuinely
          required and not yet skipped, so nothing here has to know the
          backend's verdict. */}
      <SetupWizard language={language} />

      <SettingsPanel
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        language={language}
      />

      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        language={language}
        onOpenFolder={paletteOpenFolder}
        onOpenFile={paletteOpenFile}
        onSearch={() => {
          navigate(VIEWS.WORKSPACE);
          setShellTab("files");
        }}
        onOpenTerminal={() => {
          navigate(VIEWS.WORKSPACE);
          setShellInspectorTab("terminal");
        }}
        onToggleExplorer={() => {
          navigate(VIEWS.WORKSPACE);
          setShellTab((t) => (t === "files" ? "projects" : "files"));
        }}
        onAsk={paletteAsk}
        onOpenSettings={() => setSettingsOpen(true)}
        onNewTask={() => {
          navigate(VIEWS.WORKSPACE);
          setShellInspectorTab("tasks");
        }}
        onNavigate={navigate}
      />
      </div>
    </div>
  );
});
