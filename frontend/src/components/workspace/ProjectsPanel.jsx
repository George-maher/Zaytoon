import { memo, useCallback, useEffect, useMemo, useState } from "react";
import * as api from "../../lib/api";

/**
 * Projects: open, pin, correct — and now detect and inspect.
 *
 * The commands section is the interesting part. Detection produces *defaults* —
 * `npm run test` for a Node project — and a wrong default produces a confusing
 * failure a long way from its cause. So they are editable, and the edit is a
 * single PATCH that only touches the fields named, so fixing the test command
 * does not clear the run command.
 *
 * **Detect is a preview, not a registration.** It answers "what *is* this
 * folder?" from the server without opening it; the detected block then offers
 * an explicit open, because detecting and committing are two different
 * decisions. The input defaults to the workspace root (fetched once), so the
 * common case — "what is at the top of my workspace?" — is one click.
 *
 * **The context block is shown verbatim.** `GET /projects/{id}/context`
 * returns the exact text the assistant will be given, so it can be corrected
 * here rather than discovered later as a confident mistake.
 */
function ProjectsPanel({
  projects,
  activeProject,
  onOpen,
  onPin,
  onSetCommands,
  onClose,
  language = "en",
}) {
  const [path, setPath] = useState("");
  /** The workspace root — the default target for detect. Empty if unknown. */
  const [root, setRoot] = useState("");
  const [detected, setDetected] = useState(null);
  const [detecting, setDetecting] = useState(false);
  const [recent, setRecent] = useState([]);
  const [context, setContext] = useState("");
  const [error, setError] = useState(null);
  const [editing, setEditing] = useState(null);
  const ar = language === "ar";

  // The two background reads. `getWorkspace` throws on failure by design (see
  // `lib/api.js`); a missing root only means detect falls back to ".". A failed
  // recents load *is* reported — silently showing no history would read as
  // "you have never opened anything".
  useEffect(() => {
    let live = true;
    api
      .getWorkspace()
      .then((info) => {
        if (!live || !info?.root) return;
        setRoot(info.root);
        // The input's default is the workspace root — but only while it is
        // untouched, so a path the user has already typed always wins.
        setPath((prev) => (prev.trim() ? prev : info.root));
      })
      .catch(() => {
        /* no root: detect falls back to "." */
      });
    api
      .getRecentProjects(10)
      .then((result) => {
        if (!live) return;
        if (result?.error) setError(result.error);
        else setRecent(Array.isArray(result?.projects) ? result.projects : []);
      })
      .catch(() => {
        if (live) setError("Could not reach the backend.");
      });
    return () => {
      live = false;
    };
  }, []);

  // The context block for whatever project is active — the same text the
  // model receives, so it can be checked and corrected.
  useEffect(() => {
    const id = activeProject?.id;
    if (!id) {
      setContext("");
      return undefined;
    }
    let live = true;
    setContext("");
    api
      .getProjectContext(id)
      .then((result) => {
        if (!live) return;
        if (result?.error) setError(result.error);
        else setContext(result.context || "");
      })
      .catch(() => {
        if (live) setError("Could not reach the backend.");
      });
    return () => {
      live = false;
    };
  }, [activeProject?.id]);

  /** One place where "open this path" reports the server's refusal. */
  const openPath = useCallback(
    async (target) => {
      const result = await onOpen(target);
      if (result?.error) {
        setError(result.error);
        return null;
      }
      setError(null);
      return result;
    },
    [onOpen],
  );

  const open = useCallback(
    async (event) => {
      event.preventDefault();
      const result = await openPath(path);
      if (result) {
        setPath("");
        setDetected(null);
      }
    },
    [path, openPath],
  );

  const detect = useCallback(
    async (event) => {
      event.preventDefault();
      // An empty input means the workspace root — that *is* the default.
      const target = path.trim() || root || ".";
      setDetecting(true);
      setError(null);
      const result = await api.detectProject(target).catch(() => ({
        error: "Could not reach the backend.",
      }));
      setDetecting(false);
      if (result.error) {
        setDetected(null);
        setError(result.error);
        return;
      }
      setDetected({ ...result, path: result.path || target });
    },
    [path, root],
  );

  const openDetected = useCallback(async () => {
    if (!detected) return null;
    const result = await openPath(detected.path);
    if (result) {
      setDetected(null);
      setPath("");
    }
    return result;
  }, [detected, openPath]);

  const pinned = useMemo(() => projects.filter((p) => p.pinned), [projects]);
  const others = useMemo(() => projects.filter((p) => !p.pinned), [projects]);

  const saveCommands = useCallback(
    async (event) => {
      event.preventDefault();
      const result = await onSetCommands(editing.id, {
        run_command: editing.run_command,
        test_command: editing.test_command,
        build_command: editing.build_command,
      });
      if (result?.error) setError(result.error);
      else setEditing(null);
    },
    [editing, onSetCommands],
  );

  const startEdit = useCallback((project) => {
    setEditing({
      id: project.id,
      run_command: project.run_command || "",
      test_command: project.test_command || "",
      build_command: project.build_command || "",
    });
    setError(null);
  }, []);

  const section = (title, rows) =>
    rows.length === 0 ? null : (
      <section>
        <h4 className="hud-micro" style={{ padding: "var(--space-2) var(--space-3)" }}>
          {title}
        </h4>
        {rows.map((project) => (
          <div
            key={project.id}
            className="flex items-center"
            style={{
              padding: "var(--space-2) var(--space-3)",
              gap: "var(--space-2)",
              borderTop: "var(--border-w) solid var(--hud-edge-faint)",
              background:
                activeProject?.id === project.id ? "var(--hud-glow-soft)" : "transparent",
            }}
          >
            <div className="flex-1 min-w-0">
              <p className="truncate" style={{ fontSize: "var(--font-sm)" }}>
                {project.name}
              </p>
              <p className="hud-micro truncate" style={{ color: "var(--hud-text-mute)" }}>
                {project.kind}
                {project.framework && ` · ${project.framework}`}
                {project.is_git && project.branch && ` · ${project.branch}`}
              </p>
              {(project.test_command || project.run_command) && (
                <p className="hud-micro truncate" style={{ color: "var(--hud-text-mute)" }}>
                  {project.run_command && `▶ ${project.run_command}`}
                  {project.test_command && ` · ✓ ${project.test_command}`}
                </p>
              )}
              {project.kind === "unknown" && (
                <p className="hud-micro" style={{ color: "var(--hud-amber)" }}>
                  {ar
                    ? "نوع غير معروف — اسأل قبل الافتراض"
                    : "unrecognised type — ask before assuming"}
                </p>
              )}
            </div>
            <button onClick={() => startEdit(project)} className="hud-micro">
              {ar ? "أوامر" : "CMDS"}
            </button>
            <button
              onClick={() => onPin(project.id, !project.pinned)}
              className="hud-micro"
              aria-pressed={project.pinned}
              style={{ color: project.pinned ? "var(--hud-amber)" : "var(--hud-text-mute)" }}
            >
              {project.pinned ? "★" : "☆"}
            </button>
            <button
              onClick={() => onClose(project.id)}
              className="hud-micro"
              style={{ color: "var(--hud-red)" }}
            >
              {ar ? "حذف" : "DEL"}
            </button>
          </div>
        ))}
      </section>
    );

  return (
    <div className="hud-panel flex-1 flex flex-col min-h-0 overflow-y-auto">
      <header className="hud-panel-head">
        <span>{ar ? "المشاريع" : "PROJECTS"}</span>
        <span className="hud-micro ml-auto">{projects.length}</span>
      </header>

      {error && (
        <p role="alert" style={{ padding: "var(--space-2) var(--space-3)", color: "var(--hud-red)" }}>
          {error}
        </p>
      )}

      <form onSubmit={open} style={{ padding: "var(--space-3)" }}>
        <label className="hud-micro" htmlFor="project-path" style={{ display: "block" }}>
          {ar ? "مجلد (داخل مساحة العمل)" : "Folder (inside the workspace)"}
        </label>
        <input
          id="project-path"
          value={path}
          onChange={(e) => setPath(e.target.value)}
          placeholder={root || "."}
          className="w-full"
          style={{ marginTop: "var(--space-2)" }}
        />
        <p className="hud-micro" style={{ marginTop: "var(--space-2)", color: "var(--hud-text-mute)" }}>
          {ar
            ? "المسار يجب أن يكون داخل مساحة العمل."
            : "A path outside the workspace is refused, and the message says where the boundary is."}
        </p>
        <div className="flex items-center" style={{ gap: "var(--space-2)", marginTop: "var(--space-2)" }}>
          <button type="submit" className="hud-micro">
            {ar ? "فتح" : "OPEN"}
          </button>
          <button
            type="button"
            className="hud-micro"
            onClick={detect}
            disabled={detecting}
            aria-label={ar ? "كشف المشروع" : "Detect project"}
          >
            {ar ? "كشف" : "DETECT"}
          </button>
        </div>
      </form>

      {detected && (
        <section
          aria-label={ar ? "نتيجة الكشف" : "Detection result"}
          style={{
            padding: "var(--space-3)",
            borderTop: "var(--border-w) solid var(--hud-edge-faint)",
            background: "var(--hud-glow-soft)",
          }}
        >
          <p className="hud-micro" style={{ color: "var(--hud-cyan)" }}>
            {ar ? "تم الكشف" : "DETECTED"}
          </p>
          <p
            className="hud-micro truncate"
            style={{ color: "var(--hud-text-dim)", direction: "ltr", textAlign: "left" }}
            title={detected.path}
          >
            {detected.path}
          </p>
          <p className="hud-micro" style={{ color: "var(--hud-text)" }}>
            {detected.kind}
            {detected.framework && ` · ${detected.framework}`}
          </p>
          <p className="hud-micro" style={{ color: "var(--hud-text-mute)" }}>
            {[detected.language, detected.package_manager].filter(Boolean).join(" · ")}
          </p>
          {detected.is_git && detected.branch && (
            <p className="hud-micro" style={{ color: "var(--hud-text-mute)" }}>
              {ar ? "فرع" : "branch"} {detected.branch}
            </p>
          )}
          {(detected.run_command || detected.test_command) && (
            <p className="hud-micro truncate" style={{ color: "var(--hud-text-mute)" }}>
              {detected.run_command && `▶ ${detected.run_command}`}
              {detected.test_command && ` · ✓ ${detected.test_command}`}
            </p>
          )}
          {detected.kind === "unknown" && (
            <p className="hud-micro" style={{ color: "var(--hud-amber)" }}>
              {ar ? "نوع غير معروف — اسأل قبل الافتراض" : "unrecognised type — ask before assuming"}
            </p>
          )}
          <button
            type="button"
            className="hud-micro"
            style={{ marginTop: "var(--space-2)" }}
            onClick={openDetected}
          >
            {ar ? "فتح المكتشف" : "OPEN DETECTED"}
          </button>
        </section>
      )}

      {editing && (
        <form
          onSubmit={saveCommands}
          className="flex flex-col"
          style={{ padding: "var(--space-3)", gap: "var(--space-2)" }}
        >
          <p className="hud-micro" style={{ color: "var(--hud-cyan)" }}>
            {ar ? "تصحيح الأوامر المكتشفة" : "Correct the detected commands"}
          </p>
          {["run_command", "test_command", "build_command"].map((field) => (
            <div key={field}>
              <label className="hud-micro" htmlFor={`cmd-${field}`}>
                {field}
              </label>
              <input
                id={`cmd-${field}`}
                value={editing[field]}
                onChange={(e) => setEditing({ ...editing, [field]: e.target.value })}
              />
            </div>
          ))}
          <button type="submit" className="hud-micro">
            {ar ? "حفظ" : "SAVE"}
          </button>
        </form>
      )}

      {context && (
        <section aria-label={ar ? "سياق المشروع" : "Project context"}>
          <h4 className="hud-micro" style={{ padding: "var(--space-2) var(--space-3)" }}>
            {ar ? "سياق المشروع" : "PROJECT CONTEXT"}
          </h4>
          <div style={{ padding: "0 var(--space-3) var(--space-3)" }}>
            {/* Line by line, whitespace preserved: this is the exact block the
                model receives, indentation and all — a reflowed summary would
                be a different document from the one being sent. */}
            {context
              .split("\n")
              .filter((line) => line.trim() !== "")
              .map((line, index) => (
                <p
                  key={`${index}:${line}`}
                  className="hud-micro"
                  dir="ltr"
                  style={{
                    whiteSpace: "pre-wrap",
                    color: "var(--hud-text-dim)",
                    wordBreak: "break-word",
                  }}
                >
                  {line}
                </p>
              ))}
          </div>
        </section>
      )}

      {section(ar ? "مثبّتة" : "PINNED", pinned)}
      {section(ar ? "المفتوحة مؤخرًا" : "RECENT", others)}

      {recent.length > 0 && (
        <section>
          <h4 className="hud-micro" style={{ padding: "var(--space-2) var(--space-3)" }}>
            {ar ? "السجل" : "HISTORY"}
          </h4>
          {recent.map((entry) => (
            <button
              key={entry.id || entry.path}
              type="button"
              onClick={() => openPath(entry.path)}
              className="hud-micro flex items-center w-full text-left"
              style={{
                padding: "var(--space-2) var(--space-3)",
                gap: "var(--space-2)",
                borderTop: "var(--border-w) solid var(--hud-edge-faint)",
                color: "var(--hud-text-dim)",
              }}
              title={entry.path}
            >
              <span className="truncate" style={{ color: "var(--hud-text)" }}>
                {entry.name || entry.path}
              </span>
              {entry.name && (
                <span
                  className="flex-1 min-w-0 truncate"
                  style={{
                    color: "var(--hud-text-mute)",
                    direction: "ltr",
                    textAlign: "right",
                  }}
                >
                  {entry.path}
                </span>
              )}
            </button>
          ))}
        </section>
      )}

      {projects.length === 0 && (
        <p className="hud-micro" style={{ padding: "var(--space-4)", color: "var(--hud-text-mute)" }}>
          {ar
            ? "لم يُفتح أي مشروع بعد."
            : "No projects opened yet. Open a folder above."}
        </p>
      )}
    </div>
  );
}

export default memo(ProjectsPanel);
