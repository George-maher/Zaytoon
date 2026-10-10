import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as api from "../../lib/api";
import { filterTree, flatten, glyphFor } from "../../lib/fileTree";

/**
 * The file explorer: browse, search, and edit the workspace.
 *
 * Four things here are worth explaining.
 *
 * **The tree is rendered through `flatten`.** A recursive component is the
 * obvious way to write this and the wrong one: every node becomes a component
 * instance, and a project with a few thousand files becomes a few thousand
 * components. Flattening to rows means the DOM only holds what is visible, and
 * a collapsed folder costs nothing.
 *
 * **Search has two tiers, and the line is the character count.** A
 * one-character query is answered locally by `filterTree` — instant, and it
 * reveals matches inside collapsed folders. Two or more characters go to the
 * server after a pause in typing, because `searchFiles` searches *content*,
 * which no loaded tree can do, and one request per keystroke is how a file
 * panel becomes the slowest thing in the app. Clearing the box returns to the
 * tree.
 *
 * **The hidden-file view is a listing, not a tree.** `getFileTree` has no
 * `show_hidden` parameter, so `listDirectory(path, true)` is the only way to
 * see dotfiles. Toggling it on swaps to that flat one-level listing (with its
 * own directory navigation, which is what "the current directory" means for
 * refresh); toggling it off returns to the tree, which never contains hidden
 * entries — so there is nothing to re-list, and expansion is restored.
 *
 * **Every mutation reports the server's own words.** 403 ("outside the
 * workspace"), 404 and 409 arrive as `{ error }` from `lib/api.js` and are
 * rendered verbatim in the panel: the containment boundary is enforced by the
 * backend, and the user needs to see where it is. There is no `window.confirm`
 * here — confirmation is an in-panel row that names the exact entry, and for a
 * directory it says the delete is recursive, because that is what the request
 * sends.
 *
 * **A file the assistant changed is marked.** `file.changed` events (the tool
 * layer's declaration that a mutating tool ran) arrive as the `changed` prop;
 * matching rows get an amber `AI` badge and the header counts them. The match
 * is by suffix as well as equality because the event carries whatever spelling
 * the tool was called with — absolute, relative, backslashes — while the tree
 * carries the workspace-relative form. It is an observation, not a lock: git
 * stays the authority on *what* differs, this only says *who* touched it.
 */
const SEARCH_DEBOUNCE_MS = 250;
const MIN_SEARCH_CHARS = 2;

const HEAD_BTN = {
  padding: "var(--space-1) var(--space-2)",
  border: "var(--border-w) solid var(--hud-edge-faint)",
  color: "var(--hud-text-dim)",
};

const INLINE_INPUT = {
  background: "var(--bg0)",
  border: "var(--border-w) solid var(--hud-edge-faint)",
  color: "var(--hud-text)",
  padding: "var(--space-1) var(--space-2)",
  fontSize: "var(--font-sm)",
};

/** A dropped connection becomes a `{ error }` result like any other. */
async function call(request) {
  try {
    return await request();
  } catch {
    return { error: "Could not reach the backend." };
  }
}

/** Bytes as a person reads them. The server reports directories as 0. */
function humanSize(bytes) {
  const size = Number(bytes);
  if (!Number.isFinite(size) || size < 0) return "—";
  if (size < 1024) return `${size} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = size;
  let unit = -1;
  do {
    value /= 1024;
    unit += 1;
  } while (value >= 1024 && unit < units.length - 1);
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`;
}

/** `2026-10-01T12:34:00Z` → `2026-10-01 12:34` — no locale surprises. */
function shortTime(iso) {
  const text = String(iso || "");
  const at = text.indexOf("T");
  return at === -1 ? text || "—" : `${text.slice(0, at)} ${text.slice(at + 1, at + 6)}`;
}

/** A new entry's path: the current directory plus the typed name. */
function joinPath(dir, name) {
  const base = String(dir || ".").replace(/[\\/]+$/, "");
  const clean = String(name || "").trim().replace(/^[\\/]+/, "");
  if (!clean) return "";
  return !base || base === "." ? clean : `${base}/${clean}`;
}

/** `.\src\a.py`, `./src/a.py`, `C:\ws\src\a.py` → one comparable form. */
function normPath(path) {
  return String(path || "").replace(/\\/g, "/").replace(/^\.\//, "");
}

function FileExplorer({
  tree,
  expanded,
  onToggle,
  onOpen,
  openPath,
  language = "en",
  onRefresh,
  changed = [],
}) {
  const [query, setQuery] = useState("");
  /** Server results for the current query, or `null` — the tree is showing. */
  const [search, setSearch] = useState(null);
  const [showHidden, setShowHidden] = useState(false);
  /** The flat listing, non-null only while the hidden view is on. */
  const [listing, setListing] = useState(null);
  const [dirPath, setDirPath] = useState(".");
  /** `{ path, name, is_dir }` — the entry the action row applies to. */
  const [selected, setSelected] = useState(null);
  const [stat, setStat] = useState(null);
  const [statError, setStatError] = useState(null);
  /** `"folder" | "file" | null` — the inline create row. */
  const [creating, setCreating] = useState(null);
  const [createName, setCreateName] = useState("");
  /** `{ kind: "rename" | "move" | "copy", value }` — the inline edit row. */
  const [editing, setEditing] = useState(null);
  /** The delete is armed: the confirm row is showing, nothing has been sent. */
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  // A stat that returns after the user has selected something else must not
  // paint the details line with the previous entry's numbers.
  const statSeq = useRef(0);

  const ar = language === "ar";
  const q = query.trim();

  // -- search ----------------------------------------------------------
  useEffect(() => {
    if (q.length < MIN_SEARCH_CHARS) {
      setSearch(null);
      return undefined;
    }
    let live = true;
    const timer = setTimeout(async () => {
      const result = await call(() => api.searchFiles(q, { path: dirPath }));
      if (!live) return;
      if (result.error) {
        // The server's words, in the panel's one error slot.
        setError(result.error);
        setSearch({ q, matches: [], truncated: false });
        return;
      }
      setSearch({
        q,
        matches: result.matches || [],
        truncated: Boolean(result.truncated),
      });
    }, SEARCH_DEBOUNCE_MS);
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [q, dirPath]);

  const filtered = useMemo(() => filterTree(tree?.entries, q), [tree, q]);

  // What the assistant changed while you were looking elsewhere. The hook
  // already dedupes per path and caps the list; the suffix comparison is what
  // makes a tool's absolute or backslashed spelling land on the
  // workspace-relative row.
  const changedPaths = useMemo(
    () => (changed || []).map((c) => normPath(c?.path)).filter(Boolean),
    [changed],
  );

  const isAiChanged = useCallback(
    (nodePath) => {
      const p = normPath(nodePath);
      return Boolean(p) && changedPaths.some((c) => c === p || c.endsWith(`/${p}`));
    },
    [changedPaths],
  );

  // With a query active, everything is shown — that is the whole point of
  // filtering. Without one, `expanded` decides. `collectAllDirs` returns a
  // list of paths, so it becomes a Set here: `flatten` asks `has()`, and
  // handing it an array crashed the panel on the first keystroke.
  const openSet = useMemo(
    () => (q ? new Set(collectAllDirs(filtered.entries)) : expanded),
    [q, filtered, expanded],
  );

  const searchActive = Boolean(search);

  const rows = useMemo(() => {
    if (searchActive) return [];
    if (listing) return listing.entries.map((node) => ({ node, depth: 0 }));
    return flatten(filtered.entries, openSet);
  }, [searchActive, listing, filtered, openSet]);

  // -- selection and metadata ----------------------------------------
  const select = useCallback(async (node) => {
    const seq = ++statSeq.current;
    setSelected(node);
    setStat(null);
    setStatError(null);
    setArmed(false);
    setEditing(null);
    const result = await call(() => api.statFile(node.path));
    if (seq !== statSeq.current) return;
    if (result.error) setStatError(result.error);
    else setStat(result);
  }, []);

  // -- mutations -------------------------------------------------------
  /**
   * Re-read everything the panel can be showing: the listing when the hidden
   * view is up, and always the tree behind it (`onRefresh`), because a create
   * that succeeds here must appear there.
   */
  const refresh = useCallback(async () => {
    setError(null);
    if (showHidden) {
      const result = await call(() => api.listDirectory(dirPath, true));
      if (result.error) setError(result.error);
      else setListing(result);
    }
    onRefresh?.();
  }, [showHidden, dirPath, onRefresh]);

  /**
   * One mutation: the server's refusal becomes the panel's error and the form
   * stays open for correction; success refreshes and resets the action state.
   * `clearSelection: false` is for creates — a new sibling does not invalidate
   * the entry you have selected.
   */
  const mutate = useCallback(
    async (action, { clearSelection = true } = {}) => {
      setBusy(true);
      setError(null);
      const result = await call(action);
      setBusy(false);
      if (result?.error) {
        setError(result.error);
        return false;
      }
      if (clearSelection) {
        setSelected(null);
        setStat(null);
        setStatError(null);
      }
      setArmed(false);
      setEditing(null);
      await refresh();
      return true;
    },
    [refresh],
  );

  const openCreate = useCallback((kind) => {
    setCreating(kind);
    setCreateName("");
    setEditing(null);
    setArmed(false);
    setError(null);
  }, []);

  const cancelCreate = useCallback(() => {
    setCreating(null);
    setCreateName("");
  }, []);

  const submitCreate = useCallback(
    async (event) => {
      event.preventDefault();
      const name = createName.trim();
      if (!name || busy) return;
      const target = joinPath(dirPath, name);

      const action =
        creating === "folder"
          ? async () => {
              const result = await api.createFolder(target);
              if (result.error) return result;
              // `mkdir` on an existing directory answers `created: false`
              // rather than a 409 — the intent was not fulfilled, so it is
              // reported the same way a refusal would be.
              if (result.created === false) return { error: `${target} already exists` };
              return result;
            }
          : async () => {
              // There is no create-file endpoint, and `writeFile` overwrites
              // (keeping a `.bak`). So existence is checked first: creating a
              // file must never turn into clobbering one.
              const existing = await api.statFile(target);
              if (!existing.error) return { error: `${target} already exists` };
              return api.writeFile(target, "");
            };

      const done = await mutate(action, { clearSelection: false });
      if (done) cancelCreate();
    },
    [createName, creating, dirPath, busy, mutate, cancelCreate],
  );

  const startEdit = useCallback(
    (kind) => {
      if (!selected) return;
      setCreating(null);
      setArmed(false);
      setEditing({ kind, value: kind === "rename" ? selected.name : "" });
      setError(null);
    },
    [selected],
  );

  const cancelEdit = useCallback(() => setEditing(null), []);

  const submitEdit = useCallback(
    async (event) => {
      event.preventDefault();
      if (!selected || !editing || busy) return;
      const value = editing.value.trim();
      if (!value) return;
      const entry = selected;
      const action =
        editing.kind === "rename"
          ? () => api.renameEntry(entry.path, value)
          : editing.kind === "move"
            ? () => api.moveEntry(entry.path, value)
            : () => api.copyEntry(entry.path, value);
      await mutate(action);
    },
    [selected, editing, busy, mutate],
  );

  const armDelete = useCallback(() => {
    setCreating(null);
    setEditing(null);
    setArmed(true);
    setError(null);
  }, []);

  const confirmDelete = useCallback(async () => {
    if (!selected || busy) return;
    const entry = selected;
    // A directory delete is recursive on the server, so the confirm row says
    // so before the second click can send it.
    await mutate(() => api.deleteEntry(entry.path, { recursive: Boolean(entry.is_dir) }));
  }, [selected, busy, mutate]);

  // -- hidden view and navigation --------------------------------------
  const toggleHidden = useCallback(async () => {
    const next = !showHidden;
    setShowHidden(next);
    setError(null);
    if (!next) {
      // The tree never holds hidden entries, so switching back needs no
      // request — and it restores expansion, which the flat listing has none
      // of. Back to the root: the tree is rooted at ".".
      setListing(null);
      setDirPath(".");
      return;
    }
    const result = await call(() => api.listDirectory(dirPath, true));
    if (result.error) setError(result.error);
    else setListing(result);
  }, [showHidden, dirPath]);

  const enterDir = useCallback(
    async (path) => {
      setDirPath(path);
      setError(null);
      const result = await call(() => api.listDirectory(path, showHidden));
      if (result.error) setError(result.error);
      else setListing(result);
    },
    [showHidden],
  );

  const goUp = useCallback(() => {
    const parts = String(dirPath || ".").split("/").filter(Boolean);
    parts.pop();
    return enterDir(parts.join("/") || ".");
  }, [dirPath, enterDir]);

  // -- row actions -------------------------------------------------------
  const onRow = useCallback(
    (node) => {
      select({ path: node.path, name: node.name, is_dir: Boolean(node.is_dir) });
      if (node.is_dir) {
        // In the listing a directory has no loaded children — clicking it
        // navigates into it. In the tree it expands.
        if (listing) enterDir(node.path);
        else onToggle?.(node.path);
      } else {
        onOpen?.(node.path);
      }
    },
    [select, listing, enterDir, onToggle, onOpen],
  );

  const openResult = useCallback(
    (match) => {
      select({ path: match.path, name: match.name || match.path, is_dir: false });
      onOpen?.(match.path);
    },
    [select, onOpen],
  );

  const onSubmit = useCallback(
    (event) => {
      event.preventDefault();
      if (searchActive) {
        const first = search.matches[0];
        if (first) openResult(first);
        return;
      }
      const first = rows.find((r) => !r.node.is_dir);
      if (first) onRow(first.node);
    },
    [searchActive, search, rows, openResult, onRow],
  );

  if (!tree) {
    return (
      <div className="hud-panel flex-1 flex items-center justify-center">
        <span className="hud-micro">{ar ? "جارٍ التحميل…" : "Loading…"}</span>
      </div>
    );
  }

  const detailsText = statError
    ? null
    : stat
      ? [
          humanSize(stat.size),
          stat.is_dir ? (ar ? "مجلد" : "folder") : stat.language || (ar ? "ملف" : "file"),
          shortTime(stat.modified),
        ]
      : null;

  return (
    <div className="hud-panel flex-1 flex flex-col min-h-0">
      <header className="hud-panel-head">
        <span>{ar ? "الملفات" : "FILES"}</span>
        {changedPaths.length > 0 && (
          <span
            className="hud-micro"
            style={{ color: "var(--hud-amber)" }}
            title={changedPaths.join(", ")}
            aria-label={
              ar
                ? `${changedPaths.length} ملف عدّله المساعد`
                : `${changedPaths.length} ${
                    changedPaths.length === 1 ? "file" : "files"
                  } changed by the assistant`
            }
          >
            {changedPaths.length} AI
          </span>
        )}
        <button
          type="button"
          onClick={toggleHidden}
          className="hud-micro"
          aria-label={ar ? "إظهار الملفات المخفية" : "Show hidden files"}
          aria-pressed={showHidden}
          title={ar ? "إظهار الملفات المخفية" : "Show hidden files"}
          style={{ ...HEAD_BTN, color: showHidden ? "var(--hud-cyan)" : "var(--hud-text-dim)" }}
        >
          HID
        </button>
        <button
          type="button"
          onClick={refresh}
          className="hud-micro"
          aria-label={ar ? "تحديث" : "Refresh"}
          title={ar ? "تحديث" : "Refresh"}
          style={HEAD_BTN}
        >
          {ar ? "تحديث" : "RFS"}
        </button>
      </header>

      {error && (
        <p
          role="alert"
          className="hud-micro"
          style={{ padding: "var(--space-2) var(--space-3)", color: "var(--hud-red)" }}
        >
          {error}
        </p>
      )}

      <form onSubmit={onSubmit} style={{ padding: "var(--space-2) var(--space-3)" }}>
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={ar ? "بحث في الملفات…" : "Search files…"}
          aria-label={ar ? "بحث في الملفات" : "Search files"}
          /* Was `.hud-input` plus five inline declarations re-stating it;
             they also overrode the class's placeholder and focus styles. */
          className="w-full hud-input"
        />
      </form>

      {creating ? (
        <form
          onSubmit={submitCreate}
          className="flex items-center"
          style={{ gap: "var(--space-2)", padding: "0 var(--space-3) var(--space-2)" }}
        >
          <input
            value={createName}
            onChange={(e) => setCreateName(e.target.value)}
            aria-label={
              creating === "folder"
                ? ar
                  ? "اسم المجلد الجديد"
                  : "New folder name"
                : ar
                  ? "اسم الملف الجديد"
                  : "New file name"
            }
            placeholder={creating === "folder" ? (ar ? "مجلد" : "folder name") : ar ? "ملف" : "file name"}
            dir="ltr"
            className="hud-micro flex-1 min-w-0"
            style={INLINE_INPUT}
          />
          <button type="submit" className="hud-micro" disabled={busy || !createName.trim()}>
            {ar ? "إنشاء" : "ADD"}
          </button>
          <button
            type="button"
            className="hud-micro"
            onClick={cancelCreate}
            aria-label={ar ? "إلغاء" : "Cancel"}
          >
            {ar ? "إلغاء" : "CANCEL"}
          </button>
        </form>
      ) : (
        <div
          className="flex items-center"
          style={{ gap: "var(--space-2)", padding: "0 var(--space-3) var(--space-2)" }}
        >
          <button
            type="button"
            onClick={() => openCreate("folder")}
            className="hud-micro"
            aria-label={ar ? "مجلد جديد" : "New folder"}
            title={ar ? "مجلد جديد" : "New folder"}
            style={HEAD_BTN}
          >
            + DIR
          </button>
          <button
            type="button"
            onClick={() => openCreate("file")}
            className="hud-micro"
            aria-label={ar ? "ملف جديد" : "New file"}
            title={ar ? "ملف جديد" : "New file"}
            style={HEAD_BTN}
          >
            + FILE
          </button>
          {listing && (
            <>
              <button
                type="button"
                onClick={goUp}
                className="hud-micro"
                aria-label={ar ? "المجلد الأعلى" : "Up one level"}
                title={ar ? "المجلد الأعلى" : "Up one level"}
                disabled={dirPath === "."}
                style={HEAD_BTN}
              >
                ↑
              </button>
              <span
                className="hud-micro flex-1 truncate"
                style={{ color: "var(--hud-text-mute)", direction: "ltr", textAlign: "left" }}
                title={dirPath}
              >
                {dirPath}
              </span>
            </>
          )}
        </div>
      )}

      {selected && (
        <div
          className="flex-none"
          style={{
            padding: "var(--space-2) var(--space-3)",
            borderTop: "var(--border-w) solid var(--hud-edge-faint)",
            background: "var(--hud-raised)",
          }}
        >
          <p
            className="hud-micro truncate"
            style={{ color: "var(--hud-cyan)" }}
            title={selected.path}
          >
            {selected.name}
          </p>

          <p
            className="hud-micro"
            style={{
              marginTop: "var(--space-1)",
              color: statError ? "var(--hud-amber)" : "var(--hud-text-mute)",
              wordBreak: "break-word",
            }}
          >
            {statError ? (
              statError
            ) : detailsText ? (
              <>
                <span>{detailsText[0]}</span>
                {" · "}
                <span>{detailsText[1]}</span>
                {" · "}
                <span>{detailsText[2]}</span>
              </>
            ) : (
              (ar ? "جارٍ القراءة…" : "Reading…")
            )}
          </p>

          {armed ? (
            <>
              <p
                className="hud-micro"
                style={{ marginTop: "var(--space-2)", color: "var(--hud-amber)" }}
              >
                <span>{ar ? `حذف «${selected.name}»؟` : `Delete "${selected.name}"?`}</span>
                {selected.is_dir && (
                  <span>
                    {ar
                      ? " كل ما بداخله سيُحذف (تكراري)."
                      : " Everything inside is deleted (recursive)."}
                  </span>
                )}
              </p>
              <div className="flex items-center" style={{ gap: "var(--space-2)", marginTop: "var(--space-2)" }}>
                <button
                  type="button"
                  className="hud-micro"
                  disabled={busy}
                  onClick={confirmDelete}
                  aria-label={ar ? `تأكيد حذف ${selected.name}` : `Confirm delete ${selected.name}`}
                  style={{ color: "var(--hud-red)" }}
                >
                  {ar ? "تأكيد" : "DELETE"}
                </button>
                <button
                  type="button"
                  className="hud-micro"
                  onClick={() => setArmed(false)}
                  aria-label={ar ? "إلغاء" : "Cancel"}
                >
                  {ar ? "إلغاء" : "CANCEL"}
                </button>
              </div>
            </>
          ) : editing ? (
            <form
              onSubmit={submitEdit}
              className="flex items-center"
              style={{ gap: "var(--space-2)", marginTop: "var(--space-2)" }}
            >
              <input
                value={editing.value}
                onChange={(e) => setEditing({ ...editing, value: e.target.value })}
                aria-label={
                  editing.kind === "rename"
                    ? ar
                      ? "الاسم الجديد"
                      : "New name"
                    : ar
                      ? "وجهة المسار"
                      : "Destination path"
                }
                placeholder={
                  editing.kind === "rename"
                    ? undefined
                    : ar
                      ? "مسار الوجهة"
                      : "destination path"
                }
                dir="ltr"
                className="hud-micro flex-1 min-w-0"
                style={INLINE_INPUT}
              />
              <button type="submit" className="hud-micro" disabled={busy || !editing.value.trim()}>
                {ar ? "حفظ" : "APPLY"}
              </button>
              <button
                type="button"
                className="hud-micro"
                onClick={cancelEdit}
                aria-label={ar ? "إلغاء" : "Cancel"}
              >
                {ar ? "إلغاء" : "CANCEL"}
              </button>
            </form>
          ) : (
            <div
              className="flex items-center"
              style={{ gap: "var(--space-2)", marginTop: "var(--space-2)" }}
            >
              <button
                type="button"
                className="hud-micro"
                disabled={busy}
                onClick={() => startEdit("rename")}
                style={{ color: "var(--hud-cyan)" }}
              >
                {ar ? "تسمية" : "Rename"}
              </button>
              <button
                type="button"
                className="hud-micro"
                disabled={busy}
                onClick={() => startEdit("move")}
                style={{ color: "var(--hud-cyan)" }}
              >
                {ar ? "نقل" : "Move"}
              </button>
              <button
                type="button"
                className="hud-micro"
                disabled={busy}
                onClick={() => startEdit("copy")}
                style={{ color: "var(--hud-cyan)" }}
              >
                {ar ? "نسخ" : "Copy"}
              </button>
              <button
                type="button"
                className="hud-micro"
                disabled={busy}
                onClick={armDelete}
                aria-label={ar ? `حذف ${selected.name}` : `Delete ${selected.name}`}
                style={{ color: "var(--hud-red)" }}
              >
                {ar ? "حذف" : "Delete"}
              </button>
            </div>
          )}
        </div>
      )}

      {searchActive ? (
        <p className="hud-micro" style={{ padding: "0 var(--space-3) var(--space-2)" }}>
          {search.matches.length}{" "}
          {ar ? "نتيجة" : search.matches.length === 1 ? "result" : "results"}
          {search.truncated && (
            <span style={{ color: "var(--hud-amber)" }}>
              {" · "}
              {ar ? "مقتطعة" : "truncated"}
            </span>
          )}
        </p>
      ) : (
        q && (
          <p className="hud-micro" style={{ padding: "0 var(--space-3) var(--space-2)" }}>
            {filtered.matches.length}{" "}
            {ar ? "نتيجة" : filtered.matches.length === 1 ? "match" : "matches"}
            {tree.truncated && (
              <span style={{ color: "var(--hud-amber)" }}>
                {" · "}
                {ar ? "الشجرة مقتطعة" : "tree truncated"}
              </span>
            )}
          </p>
        )
      )}

      {searchActive ? (
        <ul
          className="flex-1 overflow-y-auto min-h-0"
          style={{ listStyle: "none", margin: 0, padding: "0 var(--space-2) 0 var(--space-3)" }}
          role="list"
          aria-label={ar ? "نتائج البحث" : "Search results"}
        >
          {search.matches.map((match, index) => (
            <li key={`${match.path}:${match.line || 0}:${index}`}>
              <button
                type="button"
                onClick={() => openResult(match)}
                className="hud-micro flex items-center w-full text-left"
                style={{ padding: "var(--space-1) var(--space-2)", color: "var(--hud-text-dim)" }}
                title={match.path}
              >
                <span
                  className="flex-1 min-w-0 truncate"
                  style={{ direction: "ltr", textAlign: "left" }}
                >
                  {match.path}
                </span>
                <span
                  className="flex-none"
                  style={{ marginInlineStart: "var(--space-2)", color: "var(--hud-text-mute)" }}
                >
                  {/* Which field matched: a line number is content, `line: 0`
                      is the file name (see `core/workspace/files.py`). */}
                  {match.line > 0
                    ? ar
                      ? `محتوى سطر ${match.line}`
                      : `content line ${match.line}`
                    : ar
                      ? "اسم"
                      : "name"}
                </span>
              </button>
            </li>
          ))}
          {search.matches.length === 0 && (
            <li
              className="hud-micro"
              style={{ padding: "var(--space-3)", color: "var(--hud-text-mute)" }}
            >
              {ar ? "لا نتائج" : "No matches"}
            </li>
          )}
        </ul>
      ) : (
        <ul
          className="flex-1 overflow-y-auto min-h-0"
          style={{ listStyle: "none", margin: 0, padding: "0 var(--space-2) var(--space-3)" }}
          role="tree"
          aria-label={ar ? "شجرة الملفات" : "File tree"}
        >
          {rows.map(({ node, depth }) => {
            const isOpen = openSet.has(node.path);
            const isCurrent = openPath === node.path;
            const isSelected = selected?.path === node.path;
            return (
              <li
                key={node.path}
                role="treeitem"
                // The listing has no loaded children, so it cannot claim an
                // expansion state it does not have.
                aria-expanded={listing ? undefined : node.is_dir ? isOpen : undefined}
                aria-selected={isSelected}
              >
                <button
                  type="button"
                  onClick={() => onRow(node)}
                  className="hud-micro flex items-center w-full text-left"
                  aria-current={isCurrent ? "true" : undefined}
                  style={{
                    padding: "var(--space-1) var(--space-2)",
                    paddingInlineStart: `calc(${depth} * var(--space-5) + var(--space-2))`,
                    color: isSelected
                      ? "var(--hud-cyan)"
                      : isCurrent
                        ? "var(--hud-cyan)"
                        : node.is_dir
                          ? "var(--hud-text)"
                          : "var(--hud-text-dim)",
                    background: isSelected ? "var(--hud-glow-soft)" : "transparent",
                    borderInlineStart: isSelected
                      ? "2px solid var(--hud-cyan)"
                      : "2px solid transparent",
                  }}
                  title={node.path}
                >
                  <span
                    aria-hidden="true"
                    style={{ width: "1.4em", color: "var(--hud-text-mute)" }}
                  >
                    {glyphFor({ ...node, expanded: isOpen })}
                  </span>
                  <span className="truncate">{node.name}</span>
                  {isAiChanged(node.path) && (
                    <span
                      className="flex-none"
                      style={{
                        marginInlineStart: "var(--space-2)",
                        color: "var(--hud-amber)",
                      }}
                      title={ar ? "عدّله المساعد" : "Changed by the assistant"}
                    >
                      ● AI
                    </span>
                  )}
                </button>
              </li>
            );
          })}
          {rows.length === 0 && (
            <li
              className="hud-micro"
              style={{ padding: "var(--space-3)", color: "var(--hud-text-mute)" }}
            >
              {q
                ? ar
                  ? "لا نتائج"
                  : "No matches"
                : ar
                  ? "مجلد فارغ"
                  : "Empty directory"}
            </li>
          )}
        </ul>
      )}
    </div>
  );
}

/** Every directory in a subtree, so a search can show everything at once. */
function collectAllDirs(entries, acc = []) {
  for (const node of entries || []) {
    if (node.is_dir) {
      acc.push(node.path);
      if (!node.skipped) collectAllDirs(node.children || [], acc);
    }
  }
  return acc;
}

export default memo(FileExplorer);
