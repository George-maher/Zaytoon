import { memo, useCallback, useEffect, useMemo, useState } from "react";
import InputBar from "../InputBar";
import { getRecentProjects } from "../../lib/api";
import { desktop } from "../../lib/desktop";
import { translate } from "../../i18n/strings";

import NexusTopBar from "./NexusTopBar";
import NexusMonitor from "./NexusMonitor";
import NexusOrb from "./NexusOrb";
import NexusConsole from "./NexusConsole";
import NexusDock from "./NexusDock";
import useNexusTelemetry from "./useNexusTelemetry";
import ".././../styles/nexus.css";

/**
 * NexusDashboard — the home screen, rebuilt on the "NEXUS AI" reference.
 *
 * Section map:
 *   top bar    — brand, live state pill, headline telemetry, clock, actions
 *   left rail  — system monitor (load gauge, the four counters, session rows)
 *   center     — the core: the real resolved state; a click toggles voice
 *   right rail — the command console: transcript, real quick commands, InputBar
 *   bottom     — the app dock (every cell a real destination), voice meter
 *
 * Everything the old HomeScreen owned still exists here: the input and its
 * three actions moved into the console and the palette entry, and the path
 * fallback lives in the launchpad. No screen of the reference that Zaytoon has
 * no backend for is faked — the gesture-control module of the mock depends on
 * a camera + hand-tracking stack the product does not ship, so it is not here.
 */

function NexusDashboard({
  language = "en",
  isConnected,
  state,
  isListening,
  isSpeaking,
  isTyping,
  messages,
  sendMessage,
  startVoiceMode,
  stopVoiceMode,
  voiceMode,
  voiceState = "disabled",
  requestStop,
  clearChat,
  onOpenFolder,
  onOpenFile,
  onOpenProject,
  onOpenCommands,
  onSettingsToggle,
  onNavigate,
  remindersProps,
}) {
  const t = (key, vars) => translate(language, key, vars);
  // The state is resolved once in App (pending approvals, awaiting command,
  // offline all carry it) and handed down — this screen never re-derives it.

  const { system, systemError, health, history } = useNexusTelemetry();

  const [launchOpen, setLaunchOpen] = useState(false);
  const [pathInput, setPathInput] = useState("");
  const [pathError, setPathError] = useState(null);

  // Recent projects — carried over from HomeScreen: fetched from the real
  // REST client, rendered nothing until they arrive, and handed to
  // onOpenProject by path.
  const [recent, setRecent] = useState([]);
  useEffect(() => {
    let live = true;
    getRecentProjects(8)
      .then((result) => {
        if (!live) return;
        if (!result?.error && Array.isArray(result?.projects)) setRecent(result.projects);
      })
      .catch(() => {}); // absent recents are an answer of "none", not an error
    return () => {
      live = false;
    };
  }, []);

  const handlePathSubmit = useCallback(
    (e) => {
      e.preventDefault();
      const value = pathInput.trim();
      if (!value) return;
      // Same contract as before: this screen never navigates itself — it
      // hands the typed path to the open handler and stays.
      onOpenFile(value);
    },
    [pathInput, onOpenFile],
  );

  // The launchpad's OPEN FILE: a typed path wins, an empty field reaches the
  // native picker — the same one-input contract HomeScreen's action had.
  const openFileFromLaunch = useCallback(() => {
    const value = pathInput.trim();
    if (value) onOpenFile(value);
    else onOpenFile();
  }, [pathInput, onOpenFile]);

  // Voice meter bars: an animated presence indicator for listening/speaking —
  // the shapes carry the *state*, never an unmeasured audio level. The tick
  // only runs while the meter is active, idling to a static baseline.
  const meterActive = isSpeaking || isListening || voiceMode;
  const [ticker, setTicker] = useState(0);
  useEffect(() => {
    if (!meterActive) return;
    const id = setInterval(() => setTicker((v) => v + 1), 140);
    return () => clearInterval(id);
  }, [meterActive]);
  const meterBars = useMemo(() => Array.from({ length: 28 }, (_, i) => i), []);
  const meterHue = voiceMode ? "var(--nx-green)" : "var(--nx-cyan)";

  const onCoreToggle = useCallback(() => {
    if (voiceMode) stopVoiceMode();
    else startVoiceMode();
  }, [voiceMode, startVoiceMode, stopVoiceMode]);

  const segments = [
    { key: "nexus.cpu_tile", value: system?.cpu, hue: "var(--nx-cyan)" },
    { key: "nexus.memory_tile", value: system?.memory, hue: "var(--nx-purple)" },
    { key: "nexus.disk_tile", value: system?.disk, hue: "var(--nx-amber)" },
  ];

  return (
    <div className="nexus" dir="ltr">
      <NexusTopBar
        language={language}
        state={state}
        isConnected={isConnected}
        health={health}
        system={system}
        onVoiceToggle={onCoreToggle}
        voiceMode={voiceMode}
        onOpenCommands={() => {
          setLaunchOpen(true);
        }}
        onSettingsToggle={onSettingsToggle}
        remindersProps={remindersProps}
      />

      <div className="nx-deck">
        <NexusMonitor language={language} system={system} systemError={systemError} history={history} />

        <div className="nx-center">
          <NexusOrb
            state={state}
            size={300}
            onToggle={onCoreToggle}
            voiceMode={voiceMode}
            language={language}
            system={system}
          />

          <div className="nx-segments" aria-hidden="true">
            {segments.map((seg) => (
              <div className="nx-segment" key={seg.key} title={`${t(seg.key)}: ${seg.value == null ? "—" : `${Math.round(seg.value)}%`}`}>
                <div className="nx-segment-bar">
                  <span
                    style={{
                      width: `${seg.value == null ? 0 : Math.min(seg.value, 100)}%`,
                      background: seg.hue,
                      boxShadow: `0 0 6px ${seg.hue}`,
                    }}
                  />
                </div>
                <span className="nx-segment-label">{t(seg.key)}</span>
              </div>
            ))}
          </div>
        </div>

        <NexusConsole
          language={language}
          messages={messages}
          isTyping={isTyping}
          isConnected={isConnected}
          onSend={sendMessage}
          onStop={requestStop}
          isSpeaking={isSpeaking}
          InputBar={InputBar}
          onClear={clearChat}
          voiceMode={voiceMode}
          voiceState={voiceState}
          onVoiceStart={startVoiceMode}
          onVoiceStop={stopVoiceMode}
        />
      </div>

      <div className="nx-bottom">
        <div className="nx-meter">
          <div className="nx-meter-bars">
            {meterBars.map((i) => {
              const h = meterActive
                ? 4 + Math.abs(Math.sin(i * 1.7 + ticker * 0.9)) * 18
                : 3 + ((i * 3) % 4);
              return (
                <span key={i} style={{ height: `${h}px`, background: meterHue }} />
              );
            })}
          </div>
          <span className="nx-meter-caption">
            {isListening
              ? t("nexus.word_listening")
              : isSpeaking
                ? t("nexus.word_speaking")
                : t("nexus.ready")}
          </span>
        </div>

        <NexusDock
          language={language}
          screen="home"
          onNavigate={onNavigate}
          onOpenCommands={() => setLaunchOpen(true)}
          onSettingsToggle={onSettingsToggle}
        />

        <div className="nx-dock-side">
          <span className="nx-live">{t("nexus.live")}</span>
          <span>ZAYTOON · APP DOCK</span>
        </div>
      </div>

      {launchOpen && (
        <div
          className="nx-launch"
          role="dialog"
          aria-modal="true"
          aria-label={t("nexus.launch_label")}
          onKeyDown={(e) => {
            if (e.key === "Escape") setLaunchOpen(false);
          }}
        >
          <div className="nx-launch-card">
            <header className="nx-panel-head" style={{ marginBottom: 0 }}>
              <span className="nx-panel-title">{t("nexus.launch_title")}</span>
            </header>
            <p style={{ fontSize: 10, color: "var(--nx-mute)", marginTop: 6 }}>
              {t("nexus.launch_note")}
            </p>
            <div className="nx-launch-row">
              <button type="button" className="nx-chip" onClick={onOpenFolder}>
                {t("nexus.open_folder")}
              </button>
              <button type="button" className="nx-chip" onClick={openFileFromLaunch}>
                {t("nexus.open_file")}
              </button>
              <button type="button" className="nx-chip" onClick={onOpenCommands}>
                {t("nexus.commands")}
              </button>
            </div>

            {!desktop.available && (
              <form
                onSubmit={handlePathSubmit}
                className="nx-console-input"
                style={{ marginTop: 12 }}
              >
                <input
                  id="nexus-path"
                  type="text"
                  className="hud-input flex-1"
                  value={pathInput}
                  onChange={(e) => {
                    setPathInput(e.target.value);
                    setPathError(null);
                  }}
                  placeholder={t("nexus.path_placeholder")}
                  aria-label={t("nexus.path_label")}
                  dir="ltr"
                  style={{ padding: "6px 10px", fontSize: 11, color: "var(--nx-text)" }}
                />
                <button
                  type="submit"
                  className="nx-chip"
                  style={{ padding: "7px 14px" }}
                >
                  {t("nexus.open_submit")}
                </button>
              </form>
            )}
            {pathError && (
              <p role="alert" style={{ color: "var(--nx-red)", fontSize: 10, marginTop: 6 }}>
                {pathError}
              </p>
            )}

            {recent.length > 0 && (
              <div style={{ marginTop: 12 }}>
                <p
                  style={{
                    fontSize: 9,
                    letterSpacing: "0.2em",
                    color: "var(--nx-mute)",
                    textTransform: "uppercase",
                  }}
                >
                  {t("nexus.recent", { count: recent.length })}
                </p>
                <div className="nx-rows" style={{ marginTop: 6 }}>
                  {recent.map((project) => (
                    <button
                      key={project.id || project.path}
                      type="button"
                      className="nx-row"
                      style={{ cursor: "pointer", background: "rgba(9,20,34,0.4)", border: "1px solid var(--nx-line)", borderRadius: 8, padding: "6px 10px", width: "100%" }}
                      onClick={() => onOpenProject(project.path)}
                    >
                      <span>{project.name || project.path}</span>
                      <span className="nx-row-aux" style={{ direction: "ltr" }}>
                        {project.path}
                      </span>
                    </button>
                  ))}
                </div>
              </div>
            )}
            <div className="nx-launch-close">
              <button
                type="button"
                className="nx-chip"
                onClick={() => setLaunchOpen(false)}
                aria-label={t("nexus.close")}
              >
                {t("nexus.close")}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default memo(NexusDashboard);
