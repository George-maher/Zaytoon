import { memo, useCallback, useEffect, useMemo, useState } from "react";
import * as api from "../../lib/api";
import { MAX_EDIT_BYTES } from "../../lib/constants";

/**
 * The code workspace: open files, edit, save, and preview a diff first.
 *
 * **The editor is a plain `<textarea>` with a language label.** Not a
 * contenteditable, and not Monaco. That is a deliberate trade and worth stating
 * plainly:
 *
 * - Monaco is ~5 MB and needs five worker bundles configured; for a *file
 *   preview* pane that is the wrong cost.
 * - A textarea is keyboard-accessible, RTL-aware (the shell is bilingual),
 *   selectable, and works identically in a WebView and a browser — which a
 *   canvas-rendered editor does not.
 *
 * What it lacks is per-token highlighting, which `language` is at least
 * *labelled* with. `docs/ARCHITECTURE.md` §5 records the upgrade path: when the
 * pane becomes a real editing surface rather than a preview, swap this for
 * Monaco behind the same props (`path`, `content`, `onChange`) and nothing
 * upstream changes.
 *
 * The save flow is diff-first on purpose. An AI-modified file should be
 * reviewable before it lands — `docs/ARCHITECTURE.md` §30 — so the primary
 * action is "review the change", and the write happens only after.
 *
 * Two rules this component keeps, both invisible in a screenshot:
 *
 * 1. **A destructive action takes two clicks.** REVERT and closing a tab with
 *    unsaved work in it both arm first and act second — the same ask-twice
 *    shape FileExplorer uses for a recursive delete. There is no dialog
 *    infrastructure here (and `window.confirm` is not dependable inside a
 *    WebView), so the button itself says what the next click will do.
 * 2. **A refused save stays visible.** `onSave` returns the API result in both
 *    directions; treating a failure as "nothing happened" cleared the review
 *    pane as though the write had landed.
 */
function CodeWorkspace({
  openFile,
  dirty,
  saving = false,
  onSave,
  onDiscard,
  onSetContent,
  language = "en",
  onError,
  openFiles,
  activeFileId,
  onCloseFile,
  onSwitchFile,
}) {
  const [diff, setDiff] = useState(null);
  /**
   * The armed destructive action: `"revert"` or the path of a tab pending
   * close. `null` means nothing is armed. One slot, because only one of these
   * can be "the next thing I am about to do".
   */
  const [armed, setArmed] = useState(null);
  const ar = language === "ar";

  const content = openFile?.content ?? "";
  const tooBig = typeof openFile?.size === "number" && openFile.size > MAX_EDIT_BYTES;

  const review = useCallback(async () => {
    const result = await api.diffFile(openFile.path, content);
    if (result.error) {
      onError?.(result.error);
      return;
    }
    setDiff(result);
  }, [openFile, content, onError]);

  const save = useCallback(async () => {
    if (saving) return;
    const result = await onSave?.();
    // A refusal reaches the user *here* as well as in the shell banner. The
    // `else` branch used to run unconditionally, so a failed write looked like
    // a successful one in the one place that shows what the file contains.
    if (result?.error) {
      onError?.(result.error);
      return;
    }
    setDiff(null);
    setArmed(null);
  }, [onSave, onError, saving]);

  /**
   * Ctrl/Cmd+S — the chord every editor answers to, and the one this one did
   * not.
   *
   * The listener is scoped to "a file is open" rather than to `dirty`, so the
   * editor owns the key consistently: with nothing open the chord is left
   * alone entirely, and with a clean buffer it is swallowed rather than
   * falling through to a browser's save-page.
   */
  useEffect(() => {
    const onKey = (e) => {
      if (!(e.ctrlKey || e.metaKey)) return;
      if (String(e.key).toLowerCase() !== "s") return;
      if (!openFile) return;
      e.preventDefault();
      if (dirty && !saving) save();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openFile, dirty, saving, save]);

  const revert = useCallback(() => {
    if (armed !== "revert") {
      setArmed("revert");
      return;
    }
    setArmed(null);
    onDiscard?.();
  }, [armed, onDiscard]);

  const requestClose = useCallback(
    (file) => {
      // Unsaved work means the first click only arms. `onCloseFile` refuses a
      // dirty buffer outright, so `false` here is belt-and-braces — and a
      // component that cannot close must not claim it just did.
      if (file.dirty && armed !== file.path) {
        setArmed(file.path);
        return;
      }
      const closed = onCloseFile?.(file.path);
      if (closed !== false) setArmed(null);
    },
    [armed, onCloseFile],
  );

  const header = useMemo(() => {
    if (!openFile) return null;
    return {
      path: openFile.path,
      language: openFile.language,
      lines: openFile.lines,
    };
  }, [openFile]);

  if (!openFile) {
    return (
      <div className="hud-panel flex-1 flex items-center justify-center">
        <p className="hud-micro" style={{ color: "var(--hud-text-mute)" }}>
          {ar ? "اختر ملفًا من الشجرة" : "Select a file from the tree"}
        </p>
      </div>
    );
  }

  if (openFile.binary) {
    return (
      <div className="hud-panel flex-1 flex flex-col min-h-0">
        <header className="hud-panel-head">
          <span className="truncate">{header.path}</span>
        </header>
        <p className="hud-micro" style={{ padding: "var(--space-3)", color: "var(--hud-text-mute)" }}>
          {ar
            ? "ملف ثنائي — غير معروض كنص."
            : openFile.note || "Binary file. Not shown as text."}
        </p>
      </div>
    );
  }

  // The bar shows from one file upward: with a single file there is nothing to
  // *switch* to, but there is still something to close, and hiding the only
  // close affordance in the product because the count is one meant an open
  // file could not be put down at all.
  const tabs = Array.isArray(openFiles) ? openFiles : [];

  return (
    <div className="hud-panel flex-1 flex flex-col min-h-0">
      <div
        className="flex flex-none overflow-x-auto"
        style={{
          borderBottom: "var(--border-w) solid var(--hud-edge-faint)",
          background: "var(--bg0)",
        }}
        role="tablist"
        aria-label={ar ? "الملفات المفتوحة" : "Open files"}
      >
        {tabs.map((file) => {
          const isActive = file.path === activeFileId;
          const fileName = file.path.split(/[\\/]/).pop() || file.path;
          const closing = armed === file.path;
          return (
            // `role="tab"` on a div, not a button: the close control has to be
            // a real `<button>` inside it, and a `<button>` nested in a
            // `<button>` is invalid HTML that browsers repair by moving the
            // inner one out of the click handler entirely. A roving `tabIndex`
            // keeps one stop per tablist.
            <div
              key={file.path}
              role="tab"
              aria-selected={isActive}
              tabIndex={isActive ? 0 : -1}
              onClick={() => {
                setArmed(null);
                onSwitchFile?.(file.path);
              }}
              onKeyDown={(e) => {
                if (e.key !== "Enter" && e.key !== " ") return;
                e.preventDefault();
                setArmed(null);
                onSwitchFile?.(file.path);
              }}
              className="flex items-center flex-none"
              style={{
                padding: "var(--space-2) var(--space-3)",
                gap: "var(--space-2)",
                background: isActive ? "color-mix(in srgb, var(--hud-cyan) 6%, transparent)" : "transparent",
                borderBottom: isActive ? "2px solid var(--hud-cyan)" : "2px solid transparent",
                color: isActive ? "var(--hud-cyan)" : "var(--hud-text-mute)",
                cursor: "pointer",
                fontSize: "var(--font-xs)",
                fontFamily: "var(--font-mono)",
                maxWidth: "12rem",
              }}
            >
              <span className="truncate">{fileName}</span>
              {file.dirty && (
                <>
                  <span
                    aria-hidden="true"
                    className="flex-none"
                    style={{ color: "var(--hud-amber)", fontSize: "var(--font-xs)" }}
                  >
                    ●
                  </span>
                  {/* Colour alone cannot carry "this will be lost" — the dot
                      is the affordance, the word is the information. */}
                  <span className="sr-only">{ar ? "غير محفوظ" : "unsaved"}</span>
                </>
              )}
              <button
                type="button"
                aria-label={
                  closing
                    ? ar
                      ? `تأكيد إغلاق ${fileName}`
                      : `Confirm closing ${fileName}`
                    : ar
                      ? `إغلاق ${fileName}`
                      : `Close ${fileName}`
                }
                onClick={(e) => {
                  e.stopPropagation();
                  requestClose(file);
                }}
                style={{
                  color: closing ? "var(--hud-red)" : "var(--hud-text-mute)",
                  cursor: "pointer",
                  padding: "0 var(--space-1)",
                  background: "transparent",
                  border: "none",
                }}
              >
                {closing ? "?" : "✕"}
              </button>
            </div>
          );
        })}
      </div>

      <header className="hud-panel-head">
        <span className="truncate">{header.path}</span>
        <span className="hud-micro ml-auto" style={{ color: "var(--hud-text-mute)" }}>
          {header.language}
          {header.lines != null && ` · ${header.lines}L`}
          {dirty && ` · ${ar ? "غير محفوظ" : "unsaved"}`}
        </span>
      </header>

      <div className="flex" style={{ gap: "var(--space-2)", padding: "var(--space-2) var(--space-3)" }}>
        <button
          onClick={review}
          className="hud-micro"
          disabled={!dirty || saving}
        >
          {ar ? "معاينة التغيير" : "REVIEW"}
        </button>
        <button
          onClick={save}
          className="hud-micro"
          disabled={!dirty || saving}
          // A stable name: the visible label becomes "…" while a write is in
          // flight, and a control whose accessible name changes to a dot is a
          // control a screen reader user cannot find a second time.
          aria-label={ar ? "حفظ" : "Save"}
          title="Ctrl+S"
          style={{ color: dirty ? "var(--hud-cyan)" : "var(--hud-text-mute)" }}
        >
          {saving ? "…" : ar ? "حفظ" : "SAVE"}
        </button>
        <button
          onClick={revert}
          className="hud-micro"
          disabled={!dirty}
          style={armed === "revert" ? { color: "var(--hud-red)" } : undefined}
        >
          {armed === "revert"
            ? ar
              ? "تأكيد التراجع؟"
              : "CONFIRM REVERT?"
            : ar
              ? "تراجع"
              : "REVERT"}
        </button>
      </div>

      {diff && (
        <div
          style={{
            margin: "0 var(--space-3) var(--space-2)",
            border: "var(--border-w) solid var(--hud-edge-faint)",
            maxHeight: "30vh",
            overflow: "auto",
          }}
        >
          <p className="hud-micro" style={{ padding: "var(--space-2) var(--space-3)" }}>
            {diff.changed
              ? `+${diff.added_lines} / −${diff.removed_lines}`
              : ar
                ? "لا تغيير"
                : "No change"}
          </p>
          {/* dir="ltr": a diff is code, and code must not be reordered by an RTL
              interface — a mirrored diff is unreadable and hides the change. */}
          <pre
            dir="ltr"
            className="hud-mono"
            style={{ fontSize: "var(--font-xs)", padding: "0 var(--space-3) var(--space-2)", whiteSpace: "pre-wrap" }}
          >
            {(diff.diff || "").split("\n").map((line, i) => (
              <div
                key={i}
                style={{
                  color: line.startsWith("+")
                    ? "var(--hud-green)"
                    : line.startsWith("-")
                      ? "var(--hud-red)"
                      : "var(--hud-text-mute)",
                }}
              >
                {line}
              </div>
            ))}
          </pre>
        </div>
      )}

      <label className="sr-only" htmlFor="code-editor">
        {ar ? "محرر الملفات" : "File editor"}
      </label>
      <textarea
        id="code-editor"
        value={content}
        readOnly={tooBig}
        onChange={(e) => onSetContent?.(e.target.value)}
        spellCheck={false}
        dir="ltr"
        className="flex-1 min-h-0 hud-mono w-full"
        style={{
          background: "var(--bg0)",
          border: "var(--border-w) solid var(--hud-edge-faint)",
          borderInline: "none",
          color: "var(--hud-text)",
          padding: "var(--space-3)",
          fontSize: "var(--font-sm)",
          lineHeight: 1.5,
          resize: "none",
          opacity: tooBig ? 0.5 : 1,
        }}
      />
    </div>
  );
}

export default memo(CodeWorkspace);
