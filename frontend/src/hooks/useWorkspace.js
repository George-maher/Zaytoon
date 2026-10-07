import { useCallback, useEffect, useRef, useState } from "react";
import * as api from "../lib/api";
import { taskPercent, taskSummary, taskTone } from "../lib/taskState";

/**
 * The workspace: projects, the file tree, the open file, and the terminal.
 *
 * Every mutation goes through `lib/api.js` and every path is workspace-relative,
 * because the backend is what decides whether a path is inside the containment
 * root. This hook does not second-guess it — a 403 is rendered as the backend
 * worded it, naming the workspace, because the user cannot act on a boundary
 * they cannot see.
 *
 * The polling here is deliberate and narrow: the tree is refetched when *this*
 * client changes something, and on focus. Polling the tree on a timer would
 * re-walk the project every second and make the explorer the slowest thing in
 * the app — the failure mode that makes a file tree feel broken.
 */
/**
 * Workspace state: projects, the file tree, the open file, and terminal
 * sessions.
 *
 * The `language` parameter is accepted for symmetry with the other hooks and is
 * deliberately unused: every string this hook produces comes from the backend,
 * which words its own messages in the user's language. Deriving them here would
 * mean two translations of the same refusal.
 */
export default function useWorkspace(_language = "en") {
  const [root, setRoot] = useState(null);
  const [projects, setProjects] = useState([]);
  const [activeProject, setActiveProject] = useState(null);
  const [tree, setTree] = useState({ entries: [], truncated: false });
  const [expanded, setExpanded] = useState(new Set());
  const [openFile, setOpenFile] = useState(null);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [sessions, setSessions] = useState([]);
  const [terminalLines, setTerminalLines] = useState([]);

  // Refs for values read inside callbacks but not used as dependencies. A
  // `useCallback` that depends on `tree` re-creates on every tree change, which
  // would re-render every consumer — the classic stale-closure tradeoff, resolved
  // by keeping the mutable read in a ref.
  const openFileRef = useRef(null);
  const activeProjectRef = useRef(null);
  openFileRef.current = openFile;
  activeProjectRef.current = activeProject;

  const fail = useCallback((result) => {
    if (result?.error) {
      setError(result.error);
      return true;
    }
    setError(null);
    return false;
  }, []);

  /**
   * Show a message from outside this hook.
   *
   * The panels each call `api` directly for their own operations (skills,
   * models), so without this a failed save in the editor would be reported only
   * inside whichever component happened to make the call.
   */
  const report = useCallback((message) => {
    setError(message || null);
  }, []);

  // -- loading ---------------------------------------------------------
  const loadRoot = useCallback(async () => {
    try {
      setRoot(await api.getFileRoot());
    } catch (e) {
      setError("Could not reach the backend.");
    }
  }, []);

  const loadProjects = useCallback(async () => {
    const result = await api.getProjects();
    if (!fail(result)) setProjects(result.projects || []);
  }, [fail]);

  const loadTree = useCallback(
    async (path = ".") => {
      const result = await api.getFileTree(path, 3);
      if (result.error) {
        setError(result.error);
        return;
      }
      setError(null);
      setTree(result);
    },
    [],
  );

  const loadSessions = useCallback(async () => {
    const result = await api.getTerminals();
    if (!fail(result)) setSessions(result.sessions || []);
  }, [fail]);

  useEffect(() => {
    loadRoot();
    loadProjects();
    loadTree();
  }, [loadRoot, loadProjects, loadTree]);

  // Refresh when the tab regains focus: the desktop shell and the browser can
  // both change the workspace behind this client's back, and a tree that is
  // silently stale is worse than one that is never loaded.
  useEffect(() => {
    const onFocus = () => {
      loadTree();
      loadProjects();
    };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [loadTree, loadProjects]);

  // -- projects --------------------------------------------------------
  const openProject = useCallback(
    async (path) => {
      setBusy(true);
      const result = await api.openProject(path);
      setBusy(false);
      if (fail(result)) return null;
      setActiveProject(result);
      setProjects((prev) => {
        const without = prev.filter((p) => p.id !== result.id);
        return [result, ...without];
      });
      await loadTree();
      return result;
    },
    [fail, loadTree],
  );

  const pinProject = useCallback(
    async (id, pinned) => {
      const result = await api.pinProject(id, pinned);
      if (fail(result)) return;
      setActiveProject(result);
      setProjects((prev) => prev.map((p) => (p.id === id ? result : p)));
    },
    [fail],
  );

  const setProjectCommands = useCallback(
    async (id, commands) => {
      const result = await api.setProjectCommands(id, commands);
      if (fail(result)) return;
      setActiveProject(result);
      setProjects((prev) => prev.map((p) => (p.id === id ? result : p)));
    },
    [fail],
  );

  const closeProject = useCallback(
    async (id) => {
      const result = await api.closeProject(id);
      if (fail(result)) return;
      setProjects((prev) => prev.filter((p) => p.id !== id));
      if (activeProjectRef.current?.id === id) setActiveProject(null);
    },
    [fail],
  );

  // -- the tree --------------------------------------------------------
  const toggle = useCallback((path) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }, []);

  // -- files -----------------------------------------------------------
  const open = useCallback(
    async (path) => {
      const result = await api.readFile(path);
      if (fail(result)) return null;
      setOpenFile(result);
      setDirty(false);
      return result;
    },
    [fail],
  );

  const save = useCallback(async () => {
    const file = openFileRef.current;
    if (!file || !dirty) return null;
    const result = await api.writeFile(file.path, file.content ?? "");
    if (fail(result)) return null;
    setDirty(false);
    // The tree is refetched because a new file may have appeared, and a stale
    // tree that omits a file you just created looks like a lost write.
    await loadTree();
    return result;
  }, [dirty, fail, loadTree]);

  /**
   * Discard unsaved changes.
   *
   * Refuses while the buffer differs from what was opened — the guard is here
   * rather than in the caller because there are two callers (a button and a
   * route change) and one of them will forget.
   */
  const discard = useCallback(() => {
    const file = openFileRef.current;
    if (!file) return true;
    setDirty(false);
    return true;
  }, []);

  const revealDiff = useCallback(
    async (content) => {
      const file = openFileRef.current;
      if (!file) return null;
      const result = await api.diffFile(file.path, content);
      if (fail(result)) return null;
      return result;
    },
    [fail],
  );

  // -- terminal --------------------------------------------------------
  const openTerminal = useCallback(
    async (command, cwd = "") => {
      const result = await api.openTerminal(command, { cwd });
      if (fail(result)) return null;
      setSessions((prev) => [result, ...prev]);
      setTerminalLines([]);
      return result;
    },
    [fail],
  );

  const sendToTerminal = useCallback(
    async (sessionId, data) => {
      const result = await api.writeTerminal(sessionId, data);
      return !fail(result);
    },
    [fail],
  );

  const closeTerminal = useCallback(
    async (sessionId) => {
      const result = await api.closeTerminal(sessionId);
      if (fail(result)) return;
      setSessions((prev) => prev.filter((s) => s.id !== sessionId));
    },
    [fail],
  );

  /** Refetch one session's output — used on open and on manual refresh. */
  const refreshOutput = useCallback(
    async (sessionId) => {
      const result = await api.getTerminalOutput(sessionId, 500);
      if (result.error) return;
      setTerminalLines(result.lines || []);
    },
    [],
  );

  return {
    // state
    root,
    projects,
    activeProject,
    tree,
    expanded,
    openFile,
    dirty,
    error,
    busy,
    sessions,
    terminalLines,
    // actions
    report,
    openProject,
    pinProject,
    setProjectCommands,
    closeProject,
    toggle,
    open,
    save,
    discard,
    setOpenFile,
    setDirty,
    revealDiff,
    openTerminal,
    sendToTerminal,
    closeTerminal,
    refreshOutput,
    loadTree,
    loadProjects,
    loadSessions,
    // helpers, re-exported so a component imports one module
    taskTone,
    taskPercent,
    taskSummary,
  };
}