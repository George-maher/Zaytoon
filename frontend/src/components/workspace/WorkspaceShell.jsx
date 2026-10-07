import { memo, useCallback, useMemo, useState } from "react";
import CodeWorkspace from "./CodeWorkspace";
import FileExplorer from "./FileExplorer";
import GitPanel from "./GitPanel";
import ModelsPanel from "./ModelsPanel";
import ProjectsPanel from "./ProjectsPanel";
import SkillsPanel from "./SkillsPanel";
import TaskPanel from "./TaskPanel";
import TerminalPanel from "./TerminalPanel";
import SystemStatus from "../HUD/SystemStatus";

/**
 * The application shell.
 *
 * ```
 * ┌──────────────────────────────────────────────────────────┐
 * │ ZAYTOON        [ Project | Tasks | Files | Models | … ]  │
 * ├────────────┬──────────────────────────────┬──────────────┤
 * │ Workspace  │        AI Workspace          │  Inspector   │
 * │            │                              │              │
 * │ Projects   │  Transcript / Tool activity  │ Tasks ·      │
 * │ Files      │                              │ Source       │
 * │ Skills     │  Command console            │ Terminal     │
 * │ Models     │                              │              │
 * ├────────────┴──────────────────────────────┴──────────────┤
 * └──────────────────────────────────────────────────────────┘
 * ```
 *
 * Three panels, and each answers a different question: *what is this project*
 * (left), *what are we doing* (centre), *what happened* (right). The centre is
 * the one that must survive every width, so it is never removed below the
 * breakpoint — the rails collapse into a tabbed drawer instead.
 *
 * The confirmation dialog is deliberately **not** here. It is mounted by `App`
 * above everything, because the security boundary must not be something a panel
 * layout decision can move.
 */

const TABS = [
  { id: "projects", en: "Projects", ar: "المشاريع" },
  { id: "files", en: "Files", ar: "الملفات" },
  { id: "skills", en: "Skills", ar: "المهارات" },
  { id: "models", en: "Models", ar: "النماذج" },
];

const INSPECTOR_TABS = [
  { id: "tasks", en: "Tasks", ar: "المهام" },
  { id: "git", en: "Source", ar: "المصدر" },
  { id: "terminal", en: "Terminal", ar: "الطرفية" },
  { id: "system", en: "System", ar: "النظام" },
];

//: The header's navigation. Each entry drives the shell's focus through
//: `selectPanel` — this header exists *as* the navigation; it used to repeat
//: the `ZAYTOON` h1 that App already renders, and two h1s are two identities.
const NAV = [
  { id: "projects", en: "Project", ar: "مشروع" },
  { id: "models", en: "Model", ar: "نموذج" },
  { id: "system", en: "System", ar: "النظام" },
];

//: Below the breakpoint there is one drawer, so it needs one tab list — the
//: union. Splitting it into two lists would mean the second is unreachable.
const NARROW_TABS = [...TABS, ...INSPECTOR_TABS];

export default memo(function WorkspaceShell({
  workspace,
  socket,
  extras,
  children,
  language = "en",
  wide: wideProp,
  // Controlled when something *outside* this component drives the focus —
  // the header nav lives here, but the bottom dock does not (App renders it
  // into the command console). Uncontrolled when rendered on its own, so a
  // test or a future embed does not have to invent state plumbing. Both
  // setters always run, so the two views cannot disagree mid-transition.
  tab: tabProp,
  onTabChange,
  inspectorTab: inspectorTabProp,
  onInspectorTabChange,
  onOpenSettings,
}) {
  const [ownTab, setOwnTab] = useState("projects");
  const [ownInspector, setOwnInspector] = useState("tasks");
  const tab = tabProp ?? ownTab;
  const inspectorTab = inspectorTabProp ?? ownInspector;
  const setTab = useCallback(
    (id) => {
      setOwnTab(id);
      onTabChange?.(id);
    },
    [onTabChange],
  );
  const setInspectorTab = useCallback(
    (id) => {
      setOwnInspector(id);
      onInspectorTabChange?.(id);
    },
    [onInspectorTabChange],
  );
  const ar = language === "ar";

  const wide =
    wideProp ??
    (typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia("(min-width: 1280px)").matches
      : true);

  /**
   * Navigate to a panel by id — the header nav's one action.
   *
   * An inspector id sets *both* sides below the breakpoint (the drawer reads
   * `tab`) but only the inspector when wide: yanking the left rail back to
   * its default panel because someone opened the System view would lose the
   * user's place for no reason.
   */
  const selectPanel = useCallback(
    (id) => {
      if (INSPECTOR_TABS.some((item) => item.id === id)) {
        setInspectorTab(id);
        if (!wide) setTab(id);
      } else {
        setTab(id);
      }
    },
    [wide, setTab, setInspectorTab],
  );

  const {
    projects,
    activeProject,
    tree,
    expanded,
    openFile,
    dirty,
    error,
    sessions,
    terminalLines,
  } = workspace;

  // The terminal's lines come from the socket while it is watched, and from a
  // single fetch when a panel is opened cold. Merging here means the panel has
  // one source whichever it is.
  const lines = useMemo(() => {
    const streamed = socket?.terminalLines?.[sessions[0]?.id];
    return streamed?.length ? streamed : terminalLines;
  }, [socket, terminalLines, sessions]);

  const toggle = useCallback(
    (path) => workspace.toggle(path),
    [workspace],
  );

  const openFileByPath = useCallback((path) => workspace.open(path), [workspace]);
  const refresh = useCallback(() => workspace.loadTree(), [workspace]);

  /**
   * The one place a failed operation becomes visible.
   *
   * Every mutation returns `{ error }` rather than throwing (see `lib/api.js`),
   * so without a single reporter a failed save would look exactly like a
   * successful one. The backend's message is shown verbatim — it names what to
   * fix, and paraphrasing it would lose the only actionable part.
   */
  const reportError = useCallback(
    (message) => workspace.report(message),
    [workspace],
  );

  const setContent = useCallback(
    (content) => {
      workspace.setOpenFile({ ...openFile, content });
      workspace.setDirty(true);
    },
    [workspace, openFile],
  );

  const left = useMemo(() => {
    switch (tab) {
      case "files":
        return (
          <FileExplorer
            tree={tree}
            expanded={expanded}
            onToggle={toggle}
            onOpen={openFileByPath}
            openPath={openFile?.path}
            language={language}
            onRefresh={refresh}
            changed={socket?.fileChanges}
          />
        );
      case "skills":
        return <SkillsPanel language={language} />;
      case "models":
        return <ModelsPanel language={language} />;
      default:
        return (
          <ProjectsPanel
            projects={projects}
            activeProject={activeProject}
            onOpen={workspace.openProject}
            onPin={workspace.pinProject}
            onSetCommands={workspace.setProjectCommands}
            onClose={workspace.closeProject}
            language={language}
          />
        );
    }
  }, [
    tab, tree, expanded, toggle, openFileByPath, openFile, refresh, language,
    projects, activeProject, workspace, socket?.fileChanges,
  ]);

  /**
   * The inspector's content, by tab id.
   *
   * One function rather than a `right` node, because the narrow drawer shows
   * the *same* panels and a second switch would be a second thing to keep in
   * step. That duplication is how the phone ended up losing the task list
   * while the desktop rail kept it.
   */
  const inspector = useMemo(() => {
    // Narrow: the drawer's one tab id drives the inspector too. Without this
    // the drawer would render the panel for `inspectorTab` (default "tasks")
    // whatever the user actually clicked — so "Source" on a phone would open
    // the task list.
    const active = wide ? inspectorTab : tab;
    switch (active) {
      case "terminal":
        return (
          <TerminalPanel
            sessions={sessions}
            lines={lines}
            onOpen={workspace.openTerminal}
            onSend={workspace.sendToTerminal}
            onClose={workspace.closeTerminal}
            onRefreshOutput={workspace.refreshOutput}
            onWatch={socket?.watchTerminal}
            language={language}
          />
        );
      case "git":
        return (
          <GitPanel
            path={activeProject?.path || "."}
            language={language}
            onError={reportError}
          />
        );
      case "system":
        // The right-rail system panel: machine state plus the model/health
        // telemetry, polled on its own cadence like every other panel here.
        return <SystemStatus language={language} />;
      default:
        return (
          <TaskPanel
            tasks={socket?.tasks || []}
            activeTask={socket?.activeTask}
            onCancel={socket?.cancelTask}
            language={language}
          />
        );
    }
    }, [
    inspectorTab, tab, wide, sessions, lines, workspace, socket, language,
    activeProject, reportError,
  ]);

  const right = inspector;

  const tabBar = (items, active, onSelect, label) => (
    <div
      className="flex"
      style={{ gap: "var(--space-1)", padding: "var(--space-1) var(--space-2) 0" }}
      role="tablist"
      aria-label={label}
    >
      {items.map((item) => (
        <button
          key={item.id}
          role="tab"
          aria-selected={active === item.id}
          onClick={() => onSelect(item.id)}
          className="hud-micro"
          style={{
            padding: "var(--space-1) var(--space-3)",
            color: active === item.id ? "var(--hud-cyan)" : "var(--hud-text-mute)",
            border: "var(--border-w) solid",
            borderColor: active === item.id ? "var(--hud-edge-strong)" : "var(--hud-edge-faint)",
          }}
        >
          {ar ? item.ar : item.en}
        </button>
      ))}
    </div>
  );

  return (
    <div className="flex-1 flex flex-col min-h-0">
      <header
        className="flex flex-none items-center justify-between gap-3"
        style={{
          padding: "var(--space-2) var(--space-4)",
          borderBottom: "var(--border-w) solid var(--hud-edge-faint)",
        }}
      >
        {/* One h1 in the product — App's. This header used to repeat
            `ZAYTOON` above the project line, so the page had two identities
            and assistive technology two top-level headings. It earns its
            place now as the navigation that drives the shell's focus. */}
        <nav
          className="flex flex-none items-center"
          style={{ gap: "var(--space-1)" }}
          aria-label={ar ? "التنقل الرئيسي" : "Main navigation"}
        >
          {NAV.map((item) => {
            const active = INSPECTOR_TABS.some((t) => t.id === item.id)
              ? (wide ? inspectorTab : tab) === item.id
              : tab === item.id;
            return (
              <button
                key={item.id}
                type="button"
                className="hud-micro"
                aria-pressed={active}
                onClick={() => selectPanel(item.id)}
                style={{
                  padding: "var(--space-1) var(--space-3)",
                  color: active ? "var(--hud-cyan)" : "var(--hud-text-mute)",
                  border: "var(--border-w) solid",
                  borderColor: active
                    ? "var(--hud-edge-strong)"
                    : "var(--hud-edge-faint)",
                }}
              >
                {ar ? item.ar : item.en}
              </button>
            );
          })}
          <button
            type="button"
            className="hud-micro"
            onClick={() => onOpenSettings?.()}
            style={{
              padding: "var(--space-1) var(--space-3)",
              color: "var(--hud-text-mute)",
              border: "var(--border-w) solid var(--hud-edge-faint)",
            }}
          >
            {ar ? "إعدادات" : "Settings"}
          </button>
        </nav>
        <div className="min-w-0 flex-1">
          <p className="hud-micro truncate" style={{ fontSize: "var(--font-xs)", color: "var(--hud-text-mute)" }}>
            {activeProject
              ? `${activeProject.name} · ${activeProject.path}`
              : ar
                ? "افتح مشروعًا للبدء"
                : "Open a project to begin"}
          </p>
        </div>
        <div className="flex-none" style={{ minWidth: "12rem" }}>
          <span className="hud-micro" style={{ color: "var(--hud-text-mute)" }}>
            {workspace.root?.root || ""}
          </span>
        </div>
      </header>

      {error && (
        <p role="alert" style={{ padding: "var(--space-2) var(--space-4)", color: "var(--hud-red)" }}>
          {error}
        </p>
      )}

      <main className="flex-1 flex overflow-hidden min-h-0">
        {wide ? (
          <>
            <aside
              className="flex-none w-[280px] flex flex-col overflow-hidden"
              style={{ borderInlineEnd: "var(--border-w) solid var(--hud-edge-faint)" }}
              aria-label={ar ? "مساحة العمل" : "Workspace"}
            >
              {extras?.left && <div style={{ padding: "var(--space-3)" }}>{extras.left}</div>}
              {tabBar(TABS, tab, setTab, ar ? "مساحة العمل" : "Workspace")}
              {left}
            </aside>
            <aside
              className="flex-none w-[320px] flex flex-col overflow-hidden"
              style={{ borderInlineStart: "var(--border-w) solid var(--hud-edge-faint)" }}
              aria-label={ar ? "المفتش" : "Inspector"}
            >
              {extras?.right && <div style={{ padding: "var(--space-3)" }}>{extras.right}</div>}
              {tabBar(INSPECTOR_TABS, inspectorTab, setInspectorTab, ar ? "المفتش" : "Inspector")}
              {right}
            </aside>
          </>
        ) : (
          // Narrow: one drawer, and it carries **both** rails' tabs. Showing only
          // the workspace tabs would hide the task list and the terminal
          // entirely on a phone — the two things a user most needs while work
          // is running.
          <div
            className="flex-none w-full max-h-[36vh] flex flex-col overflow-hidden"
            style={{ borderBottom: "var(--border-w) solid var(--hud-edge-faint)" }}
          >
            {extras?.left && <div style={{ padding: "var(--space-3)" }}>{extras.left}</div>}
            {/* Both rails' extras below the breakpoint, same rule as the tab
                lists: the right-hand block used to vanish on a phone while
                the desktop rail kept it. */}
            {extras?.right && <div style={{ padding: "var(--space-3)" }}>{extras.right}</div>}
            {tabBar(NARROW_TABS, tab, setTab, ar ? "اللوحات" : "Panels")}
            {/* `tab` here is the *drawer's* tab id, and below the breakpoint it
                is shared with the left rail's — which is why the inspector
                panels are reached through the same ids (`tasks`, `git`,
                `terminal`) that the wide layout uses. */}
            {INSPECTOR_TABS.some((item) => item.id === tab) ? inspector : left}
          </div>
        )}

        {/* The centre column. Never removed at any width.

            The conversation and the editor split it, rather than stacking: both
            are things you read *while* working, and a stacked layout forces a
            choice between them every time the window is short. `min-h-0` on
            both halves is load-bearing — without it a flex child refuses to
            shrink below its content and the editor pushes the transcript off
            screen. */}
        <section
          className="flex-1 flex flex-col min-w-0 min-h-0"
          aria-label={ar ? "مساحة العمل" : "AI workspace"}
        >
          <div className="flex-1 flex flex-col min-h-0">{children}</div>
          <div
            className="flex-none flex flex-col min-h-0"
            id="zaytoon-editor"
            style={{
              padding: "var(--space-3)",
              borderTop: "var(--border-w) solid var(--hud-edge-faint)",
              maxHeight: "45vh",
            }}
          >
            <CodeWorkspace
              openFile={openFile}
              dirty={dirty}
              onSave={workspace.save}
              onDiscard={workspace.discard}
              onSetContent={setContent}
              onError={reportError}
              language={language}
            />
          </div>
        </section>
      </main>
    </div>
  );
});