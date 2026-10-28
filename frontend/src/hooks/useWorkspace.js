import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [sessions, setSessions] = useState([]);
  const [terminalLines, setTerminalLines] = useState([]);
  /**
   * The open files — **the** buffer store, not a copy of one.
   *
   * Each entry is `{ ...fileMetadata, baseline, content, dirty }`, where
   * `baseline` is the content as it came off disk. Two properties fall out of
   * that shape and both used to be broken:
   *
   * - **One dirty flag per buffer, not one for the editor.** A single shared
   *   boolean meant a background tab's `●` was a lie the moment you switched
   *   away — and, worse, switching *cleared* it, so the work was still there
   *   but nothing said so.
   * - **Dirty is derived (`content !== baseline`), never stored.** A flag that
   *   has to be set and cleared by hand in five places is a flag that is
   *   wrong in one of them; this cannot drift, and typing the original text
   *   back correctly reads as "no changes".
   *
   * `openFile` and `dirty` are now *views* over this list, so the editor and
   * the tab bar can no longer disagree about what is open.
   */
  const [openFiles, setOpenFiles] = useState([]);
  const [activeFileId, setActiveFileId] = useState(null);

  const openFile = useMemo(
    () => openFiles.find((f) => f.path === activeFileId) ?? null,
    [openFiles, activeFileId],
  );
  const dirty = Boolean(openFile?.dirty);

  // Refs for values read inside callbacks but not used as dependencies. A
  // `useCallback` that depends on `tree` re-creates on every tree change, which
  // would re-render every consumer — the classic stale-closure tradeoff, resolved
  // by keeping the mutable read in a ref.
  const openFileRef = useRef(null);
  const openFilesRef = useRef(openFiles);
  const activeFileIdRef = useRef(activeFileId);
  const activeProjectRef = useRef(null);
  openFileRef.current = openFile;
  openFilesRef.current = openFiles;
  activeFileIdRef.current = activeFileId;
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

  /** Turn a `/files/read` payload into a buffer with a baseline to diff against. */
  const toBuffer = useCallback(
    (result) => ({
      ...result,
      baseline: result.content ?? "",
      content: result.content ?? "",
      dirty: false,
    }),
    [],
  );

  const open = useCallback(
    async (path) => {
      const existing = openFilesRef.current.find((f) => f.path === path);

      // An unsaved buffer is *switched to*, never re-read.
      //
      // The old behaviour re-read on every click, which replaced whatever was
      // in the buffer with what is on disk — so an afternoon of typing
      // disappeared on a double-click that looks exactly like navigation, with
      // nothing on screen to say work had been thrown away. A clean buffer, on
      // the other hand, should refresh: the file may well have changed under
      // it, and keeping a stale copy of something you have not touched is its
      // own quiet lie.
      if (existing?.dirty) {
        setActiveFileId(path);
        return existing;
      }

      const result = await api.readFile(path);
      if (fail(result)) return null;
      setActiveFileId(path);
      setOpenFiles((prev) => {
        const buffer = toBuffer(result);
        const i = prev.findIndex((f) => f.path === path);
        if (i === -1) return [...prev, buffer];
        const next = [...prev];
        next[i] = buffer;
        return next;
      });
      return result;
    },
    [fail, toBuffer],
  );

  /**
   * Close a tab.
   *
   * Returns `true` when it closed, `false` when it refused because the buffer
   * still holds unsaved work — the refusal lives here rather than in the
   * button because there is more than one way to reach a close, and the one
   * that forgets is the one that loses the file. The caller arms a second
   * click, which is how every destructive action in this UI asks twice.
   */
  const closeFile = useCallback((path) => {
    const target = openFilesRef.current.find((f) => f.path === path);
    if (target?.dirty) return false;

    const next = openFilesRef.current.filter((f) => f.path !== path);
    setOpenFiles(next);
    if (path === activeFileIdRef.current) {
      setActiveFileId(next.length ? next[next.length - 1].path : null);
    }
    return true;
  }, []);

  /**
   * Switch the active buffer.
   *
   * Deliberately does *nothing else*: the content, the baseline and the dirty
   * mark all belong to the buffer being left, and touching them on the way out
   * is how a tab came back with someone else's state.
   */
  const switchFile = useCallback((path) => {
    setActiveFileId(path);
  }, []);

  const setContent = useCallback((content) => {
    setOpenFiles((prev) =>
      prev.map((f) =>
        f.path === activeFileIdRef.current
          ? { ...f, content, dirty: content !== f.baseline }
          : f,
      ),
    );
  }, []);

  /**
   * Write the active buffer.
   *
   * Returns the API result in **both** directions. Returning `null` on failure
   * left the editor's `if (result?.error)` unreachable, so a refused save
   * cleared the review pane as though the write had landed — the one moment a
   * user most needs to be told the truth. A success still refreshes the tree:
   * a new file that never appears there looks like a lost write.
   */
  const save = useCallback(async () => {
    const file = openFileRef.current;
    if (!file || !file.dirty || saving) return null;
    setSaving(true);
    const result = await api.writeFile(file.path, file.content ?? "");
    setSaving(false);
    if (result?.error) {
      setError(result.error);
      return result;
    }
    setError(null);
    setOpenFiles((prev) =>
      prev.map((f) =>
        f.path === file.path
          ? { ...f, baseline: f.content, dirty: false }
          : f,
      ),
    );
    await loadTree();
    return result;
  }, [saving, loadTree]);

  /**
   * Revert the active buffer to what is on disk — for real.
   *
   * The old version only cleared the dirty flag, so the edited text stayed on
   * screen while SAVE went grey: the user could neither save the change nor
   * see the file, and the only way out was to re-click it — the very action
   * that threw the work away. Re-reading means a revert also picks up whatever
   * changed the file underneath (the assistant, git), which is usually the
   * reason to press it.
   *
   * Async, and it reports failure: a revert that could not read the file must
   * not leave the buffer looking untouched.
   */
  const discard = useCallback(async () => {
    const file = openFileRef.current;
    if (!file) return true;
    const result = await api.readFile(file.path);
    if (fail(result)) return false;
    setOpenFiles((prev) =>
      prev.map((f) => (f.path === file.path ? toBuffer(result) : f)),
    );
    return true;
  }, [fail, toBuffer]);

  // -- terminal --------------------------------------------------------
  const openTerminal = useCallback(
    async (command, cwd = "", confirm = null) => {
      const result = await api.openTerminal(command, { cwd, confirm });
      if (fail(result)) return null;
      // The server's preview frame: `{requires_confirmation, confirm, …}` —
      // nothing has run yet, and no session exists to add. The caller renders
      // the preview and re-calls with the token.
      if (result.requires_confirmation) return result;
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
    openFiles,
    activeFileId,
    saving,
    // actions
    report,
    openProject,
    pinProject,
    setProjectCommands,
    closeProject,
    toggle,
    open,
    closeFile,
    switchFile,
    setContent,
    save,
    discard,
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