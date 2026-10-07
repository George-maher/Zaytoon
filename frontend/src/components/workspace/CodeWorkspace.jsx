import { memo, useCallback, useMemo, useState } from "react";
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
 */
function CodeWorkspace({ openFile, dirty, onSave, onDiscard, onSetContent, language = "en", onError }) {
  const [diff, setDiff] = useState(null);
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
    const result = await onSave?.();
    if (result?.error) onError?.(result.error);
    else setDiff(null);
  }, [onSave, onError]);

  const header = useMemo(() => {
    if (!openFile) return null;
    return {
      path: openFile.path,
      language: openFile.language,
      modified: openFile.modified,
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

  return (
    <div className="hud-panel flex-1 flex flex-col min-h-0">
      <header className="hud-panel-head">
        <span className="truncate">{header.path}</span>
        <span className="hud-micro ml-auto" style={{ color: "var(--hud-text-mute)" }}>
          {header.language}
          {header.lines != null && ` · ${header.lines}L`}
          {dirty && ` · ${ar ? "غير محفوظ" : "unsaved"}`}
        </span>
      </header>

      <div className="flex" style={{ gap: "var(--space-2)", padding: "var(--space-2) var(--space-3)" }}>
        <button onClick={review} className="hud-micro" disabled={!dirty}>
          {ar ? "معاينة التغيير" : "REVIEW"}
        </button>
        <button
          onClick={save}
          className="hud-micro"
          disabled={!dirty}
          style={{ color: dirty ? "var(--hud-cyan)" : "var(--hud-text-mute)" }}
        >
          {ar ? "حفظ" : "SAVE"}
        </button>
        <button onClick={onDiscard} className="hud-micro" disabled={!dirty}>
          {ar ? "تراجع" : "REVERT"}
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
              ? `${ar ? "+" : "+"}${diff.added_lines} / −${diff.removed_lines}`
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