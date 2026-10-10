import { memo, useCallback, useEffect, useState } from "react";
import { desktop } from "../lib/desktop";
import * as api from "../lib/api";
import InputBar from "./InputBar";
import StatusIndicator from "./hud/StatusIndicator";
import AIPresence from "./hud/AIPresence";
import { resolveState } from "./hud/hudState";

/**
 * HomeScreen — the first screen.
 *
 * The screen answers one question and then gets out of the way:
 *
 *   ZAYTOON / AI COMPUTER ENVIRONMENT      the mark (App's header owns the
 *                                           page's single <h1>, so this is a
 *                                           visual identity, not a second
 *                                           top-level heading)
 *   What do you want to do?                <h2>, the section's real heading
 *   [ command input ]                      the answer to that question
 *   OPEN FOLDER | OPEN FILE | COMMANDS     the three things that are not it
 *   [ path ]                               browser fallback for the pickers
 *   ▸ RECENT WORKSPACES (n)                collapsed until asked for
 *   ● status
 *
 * No cards, no telemetry, no diagnostics — those belong to Computer → System.
 * Recent workspaces are collapsed by default because a list of eight absolute
 * paths is the tallest block of text on the page and the question above it is
 * the actual purpose of the screen; the count keeps the section discoverable.
 *
 * The transition to the workspace happens when a folder is opened (native
 * picker or path input), a file is opened, or a recent project is clicked.
 */
function HomeScreen({
  language = "en",
  isConnected,
  onOpenFolder,
  onOpenFile,
  onOpenProject,
  onSendCommand,
  onOpenCommands,
  onVoiceStart,
  onVoiceStop,
  voiceMode,
  isBusy = false,
}) {
  const ar = language === "ar";
  const [recent, setRecent] = useState([]);
  const [recentOpen, setRecentOpen] = useState(false);
  const [pathInput, setPathInput] = useState("");
  const [pathError, setPathError] = useState(null);

  // Load recent projects for the home screen. The section renders nothing
  // until they arrive, so there is no loading state to show and no
  // `loadingRecent` flag to strand as dead state.
  useEffect(() => {
    let live = true;
    api
      .getRecentProjects(8)
      .then((result) => {
        if (!live) return;
        if (result?.error) {
          // Silently fail — the home screen works without recents
          setRecent([]);
        } else {
          setRecent(Array.isArray(result?.projects) ? result.projects : []);
        }
      })
      .catch(() => {
        if (live) setRecent([]);
      });
    return () => {
      live = false;
    };
  }, []);

  // Open folder via native picker (desktop) or path input (browser)
  const handleOpenFolder = useCallback(async () => {
    if (desktop.available) {
      const result = await desktop.openFolderDialog();
      if (result) {
        onOpenFolder(result);
        return;
      }
    }
    // Browser fallback: use the path input
    const path = pathInput.trim();
    if (path) {
      setPathError(null);
      onOpenFolder(path);
    } else {
      setPathError(ar ? "أدخل مسار مجلد" : "Enter a folder path");
    }
  }, [ar, pathInput, onOpenFolder]);

  // Open file via native picker (desktop) or path input (browser)
  const handleOpenFile = useCallback(async () => {
    if (desktop.available) {
      const result = await desktop.openFileDialog();
      if (result) {
        onOpenFile(result);
        return;
      }
    }
    // Browser fallback: use the path input
    const path = pathInput.trim();
    if (path) {
      setPathError(null);
      onOpenFile(path);
    } else {
      setPathError(ar ? "أدخل مسار ملف" : "Enter a file path");
    }
  }, [ar, pathInput, onOpenFile]);

  const handlePathSubmit = useCallback(
    (e) => {
      e.preventDefault();
      // Default to opening as folder if no extension
      const path = pathInput.trim();
      if (!path) return;
      const isFile = /\.[a-z0-9]+$/i.test(path);
      if (isFile) {
        setPathError(null);
        onOpenFile(path);
      } else {
        setPathError(null);
        onOpenFolder(path);
      }
    },
    [pathInput, onOpenFile, onOpenFolder],
  );

  const state = resolveState({ isConnected });

  // Letter-spacing tears Arabic apart — it is a connected script, so the
  // tracked-out HUD treatment applies to the Latin label only.
  const tracked = ar ? "0" : "0.2em";

  const actionStyle = {
    padding: "var(--sp-3) var(--sp-5)",
    fontSize: "var(--font-md)",
    fontFamily: "var(--font-mono)",
    letterSpacing: ar ? "0" : "0.15em",
    cursor: "pointer",
  };

  return (
    <div
      className="flex-1 flex flex-col items-center justify-center min-h-0"
      style={{ padding: "var(--sp-5)", gap: "var(--sp-5)" }}
    >
      {/* The presence orb: the reference's central motif, given the product's
          one job of reporting "is the AI working right now". Static while
          idle, animated only while a turn is actually in flight. It is not
          decorative — it is the first thing on the screen that changes. */}
      <div
        className="flex items-center justify-center"
        style={{ height: 96 }}
      >
        <AIPresence
          state={state}
          size={92}
          active={isBusy}
          language={language}
        />
      </div>

      {/* Identity. App's header already renders the document's single h1, so
          a second one here gave assistive technology two top-level headings
          for one product. This is the visual mark that belongs to Home. */}
      <div
        className="flex flex-col items-center"
        style={{ gap: "var(--sp-2)" }}
        aria-hidden="true"
      >
        <p
          className="hud-identity"
          style={{
            fontSize: "var(--font-xl)",
            lineHeight: 1,
            letterSpacing: "0.35em",
            textShadow: "0 0 20px var(--hud-glow-soft)",
          }}
        >
          ZAYTOON
        </p>
        <p
          className="hud-micro"
          style={{
            fontSize: "var(--font-sm)",
            color: "var(--hud-text-mute)",
            letterSpacing: ar ? "0" : "0.25em",
          }}
        >
          {ar ? "بيئة حاسوب ذكية" : "AI COMPUTER ENVIRONMENT"}
        </p>
      </div>

      {/* The question and the field that answers it. Kept as one block: the
          prompt is a heading for the input below it, not a banner above it. */}
      <div className="flex flex-col w-full max-w-2xl" style={{ gap: "var(--sp-2)" }}>
        <h2
          className="text-center"
          style={{
            fontSize: "var(--font-lg)",
            fontFamily: "var(--font-mono)",
            fontWeight: 700,
            textTransform: "uppercase",
            letterSpacing: tracked,
            color: "var(--hud-cyan)",
            textShadow: "0 0 8px var(--hud-glow-soft)",
          }}
        >
          {ar ? "ماذا تريد أن تفعل؟" : "What do you want to do?"}
        </h2>

        <InputBar
          onSend={onSendCommand}
          isConnected={isConnected}
          language={language}
          onVoiceStart={onVoiceStart}
          onVoiceStop={onVoiceStop}
          voiceMode={voiceMode}
        />
      </div>

      {/* Primary actions. Three, and only three, because only three exist. */}
      <div className="flex flex-wrap items-center justify-center" style={{ gap: "var(--sp-3)" }}>
        <button
          onClick={handleOpenFolder}
          className="hud-input"
          style={{
            ...actionStyle,
            color: "var(--hud-cyan)",
            background: "color-mix(in srgb, var(--hud-cyan) 6%, transparent)",
            border: "var(--border-w) solid color-mix(in srgb, var(--hud-cyan) 25%, transparent)",
          }}
        >
          {ar ? "فتح مجلد" : "OPEN FOLDER"}
        </button>
        <button
          onClick={handleOpenFile}
          className="hud-input"
          style={{ ...actionStyle, color: "var(--hud-text-dim)" }}
        >
          {ar ? "فتح ملف" : "OPEN FILE"}
        </button>
        <button
          onClick={onOpenCommands}
          className="hud-input"
          style={{ ...actionStyle, color: "var(--hud-text-dim)" }}
        >
          {ar ? "الأوامر" : "COMMANDS"}
        </button>
      </div>

      {/* Path input (browser fallback) */}
      {!desktop.available && (
        <form
          onSubmit={handlePathSubmit}
          className="flex items-center w-full max-w-md"
          style={{ gap: "var(--sp-2)" }}
        >
          <input
            id="home-path"
            type="text"
            className="hud-input flex-1"
            value={pathInput}
            onChange={(e) => {
              setPathInput(e.target.value);
              setPathError(null);
            }}
            placeholder={ar ? "مسار مجلد أو ملف…" : "Folder or file path…"}
            aria-label={ar ? "مسار" : "Path"}
            dir="ltr"
          />
          <button
            type="submit"
            className="hud-input flex-none"
            style={{
              padding: "var(--sp-2) var(--sp-4)",
              color: "var(--hud-cyan)",
              cursor: "pointer",
            }}
          >
            {ar ? "فتح" : "OPEN"}
          </button>
        </form>
      )}
      {pathError && (
        <p role="alert" style={{ color: "var(--hud-red)", fontSize: "var(--font-sm)" }}>
          {pathError}
        </p>
      )}

      {/* Recent workspaces — collapsed until asked for. */}
      {recent.length > 0 && (
        <section className="w-full max-w-2xl">
          <button
            type="button"
            className="hud-input flex items-center w-full"
            aria-expanded={recentOpen}
            aria-controls="home-recent"
            onClick={() => setRecentOpen((open) => !open)}
            style={{ padding: "var(--sp-2) var(--sp-3)", cursor: "pointer", gap: "var(--sp-2)" }}
          >
            <span
              className="flex-none"
              style={{ color: "var(--hud-cyan)", fontSize: "var(--font-sm)" }}
              aria-hidden="true"
            >
              {recentOpen ? "▾" : "▸"}
            </span>
            <span
              className="flex-1 truncate"
              style={{
                textAlign: "start",
                color: "var(--hud-text-mute)",
                fontSize: "var(--font-xs)",
                letterSpacing: tracked,
                textTransform: "uppercase",
              }}
            >
              {ar ? "مشاريع حديثة" : "RECENT WORKSPACES"}
            </span>
            <span
              className="flex-none"
              style={{
                color: "var(--hud-text-dim)",
                fontSize: "var(--font-xs)",
                fontFamily: "var(--font-mono)",
              }}
            >
              {recent.length}
            </span>
          </button>

          {recentOpen && (
            <div
              id="home-recent"
              className="flex flex-col"
              style={{ gap: "var(--sp-1)", marginTop: "var(--sp-1)" }}
            >
              {recent.map((project) => (
                <button
                  key={project.id || project.path}
                  onClick={() => onOpenProject(project.path)}
                  className="hud-input flex items-center w-full"
                  style={{
                    padding: "var(--sp-2) var(--sp-3)",
                    cursor: "pointer",
                    gap: "var(--sp-2)",
                    textAlign: "start",
                  }}
                >
                  <span
                    className="flex-none"
                    style={{
                      color: "var(--hud-cyan)",
                      fontSize: "var(--font-sm)",
                    }}
                    aria-hidden="true"
                  >
                    ▸
                  </span>
                  <span
                    className="flex-1 min-w-0 truncate"
                    style={{
                      color: "var(--hud-text)",
                      fontSize: "var(--font-sm)",
                      fontFamily: "var(--font-mono)",
                    }}
                  >
                    {project.name || project.path}
                  </span>
                  <span
                    className="flex-none"
                    style={{
                      color: "var(--hud-text-mute)",
                      fontSize: "var(--font-xs)",
                      fontFamily: "var(--font-mono)",
                      direction: "ltr",
                    }}
                  >
                    {project.path}
                  </span>
                </button>
              ))}
            </div>
          )}
        </section>
      )}

      {/* Compact status */}
      <div className="flex items-center" style={{ gap: "var(--sp-2)" }}>
        <StatusIndicator state={state} language={language} />
      </div>
    </div>
  );
}

export default memo(HomeScreen);
