import { memo, useCallback, useEffect, useState } from "react";
import * as api from "../../lib/api";

/**
 * Source control for the open project.
 *
 * **It hides itself when there is no repository, and only then.** `404` from
 * `/git/status` means "not a repository here", which is not an error — it is
 * the answer to "should this panel exist". Showing a red failure on a folder
 * that simply is not under version control trains people to ignore red.
 *
 * The converse matters just as much and was the actual bug: the status load
 * used to fold *every* failure into `404`, so the three cases below never
 * reached the screen and the panel simply left, asserting that the folder was
 * not a repository. They are handled distinctly now, because they mean
 * different things to different people:
 *
 * - **403** — the path is outside the workspace. A containment decision. No
 *   retry offered; there is nothing the user can do from here.
 * - **409** — git refused. Its own message is shown verbatim, because git's
 *   refusals ("you have unstaged changes", "nothing to commit") are specific
 *   and actionable in a way any paraphrase would not be.
 * - **422** — the request was malformed. That is a bug in this component, so
 *   the message is logged rather than shown as though the user caused it.
 *
 * Staging is explicit and one-row-at-a-time; "Stage all" is a secondary action
 * in the action row, because bulk-staging is how unrelated work ends up in
 * someone else's commit. The column flags drive the buttons now: porcelain
 * `M ` (staged, worktree clean) offers Unstage only, ` M` offers Stage only,
 * and `MM` correctly offers both.
 *
 * Two irreversible-ish verbs get extra ceremony: **discard** and **merge**
 * are arm-then-click (first click selects the victim or the branch, second
 * performs it — a merge can conflict, so it earns the same ceremony), and a
 * failed fast-forward pull offers an explicit "retry as merge" rather than
 * silently creating a merge commit.
 * Every button's work goes through `run()`, so one busy flag disables the
 * whole panel and one error slot renders git's own words once.
 */
const STATUS_TONE = {
  modified: "var(--hud-amber, var(--hud-text))",
  added: "var(--hud-green, var(--hud-text))",
  deleted: "var(--hud-red)",
  renamed: "var(--hud-cyan)",
  untracked: "var(--hud-text-mute)",
  both: "var(--hud-amber, var(--hud-text))",
};

function GitPanel({ path = ".", language = "en", onError }) {
  const ar = language === "ar";
  const [status, setStatus] = useState(null);
  /** `null` = unknown, `false` = no repo here (hide), `true` = a repository. */
  const [present, setPresent] = useState(null);
  const [busy, setBusy] = useState(false);
  // Two separate strings. Sharing one was a real bug: a failed commit would
  // clear the user's half-typed message, or the message would be rendered as
  // an error banner — and both happen on the *same* state change.
  const [commitMessage, setCommitMessage] = useState("");
  const [error, setError] = useState("");
  /**
   * The visible history: `{ commits, error, loading }`.
   *
   * Three states, three answers. A bare `[]` used to mean "no commits yet",
   * "still loading" and "the request failed" at once — so a repository with a
   * long history whose log request failed rendered as an empty list, which
   * reads as a brand-new repository. The distinction costs one field and
   * removes a claim the panel cannot back.
   */
  const [logView, setLogView] = useState({ commits: [], error: "", loading: true });
  const [branches, setBranches] = useState([]);
  /**
   * Why the status could not be read: `{ message, status }`, or `null`.
   *
   * Kept apart from `error` (which reports a *write* the user just asked for)
   * because they need different responses — one gets a RETRY, the other gets
   * git's own words on screen.
   */
  const [loadError, setLoadError] = useState(null);
  /** The entry whose discard is armed, or `""`. */
  const [armed, setArmed] = useState("");
  /** `{ path, text, empty, error, loading }` while the diff pane is open. */
  const [diffView, setDiffView] = useState(null);
  /** Set when a fast-forward pull was refused: offer the explicit merge. */
  const [mergeRetry, setMergeRetry] = useState(false);
  /** The merge target row is open (armed, nothing sent) and its branch. */
  const [merging, setMerging] = useState(false);
  const [mergeTarget, setMergeTarget] = useState("");

  const refresh = useCallback(async () => {
    setLoadError(null);
    let result;
    try {
      result = await api.getGitStatus(path);
    } catch (err) {
      // A fetch that throws never returned a status code, so there is nothing
      // to classify — report it plainly and leave the retry available rather
      // than leaving the panel on "Reading…" forever.
      setPresent(true);
      setStatus(null);
      setLoadError({ message: String(err?.message || err), status: 0 });
      return;
    }
    if (result.error) {
      if (result.status === 404) {
        // 404 *is* the "no repository" answer — a statement about the folder,
        // not a failure.
        setPresent(false);
        setStatus(null);
        return;
      }
      // Everything else is a different question with a different answer. The
      // old code mapped all of them to `present = false`, so a 403
      // containment refusal or a 409 from git itself hid the panel behind
      // "This folder is not a git repository" — the confident falsehood the
      // rest of this panel goes out of its way to avoid. Keep the panel, show
      // the real reason, and refuse the retry only where a retry cannot help.
      setPresent(true);
      setStatus(null);
      setLoadError({ message: result.error, status: result.status ?? 0 });
      return;
    }
    setPresent(true);
    setStatus(result);
  }, [path]);

  const loadBranches = useCallback(async () => {
    try {
      const result = await api.getGitBranches(path);
      setBranches(result.error ? [] : result.branches || []);
    } catch {
      // The branch dropdown is a convenience; losing it must not break the
      // panel (the status header still shows the current branch).
      setBranches([]);
    }
  }, [path]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    if (!present) return;
    let live = true;
    // A different path means the history on screen describes another
    // repository — drop it rather than leave it up until the answer arrives.
    setLogView({ commits: [], error: "", loading: true });
    api.getGitLog(path, 10).then((result) => {
      if (!live) return;
      setLogView({
        commits: result.commits || [],
        error: result.error || "",
        loading: false,
      });
    });
    if (live) loadBranches();
    return () => {
      live = false;
    };
  }, [present, path, loadBranches]);

  const run = useCallback(
    async (action) => {
      setBusy(true);
      setError("");
      setArmed("");
      const result = await action();
      setBusy(false);
      if (result.error) {
        onError?.(result.error);
        setError(result.error);
        return result;
      }
      // A successful write makes any earlier divergence claim stale.
      setMergeRetry(false);
      // Every write returns fresh state, so the panel never shows a tree it
      // knows to be stale — which is the one thing a source-control panel must
      // never do.
      if (result.status) setStatus(result.status);
      else await refresh();
      api.getGitLog(path, 10).then((r) =>
        setLogView({
          commits: r.commits || [],
          error: r.error || "",
          loading: false,
        }),
      );
      loadBranches();
      return result;
    },
    [refresh, path, onError, loadBranches],
  );

  const doPull = useCallback(
    async (ffOnly = true) => {
      const result = await run(() => api.gitPull({ ffOnly, path }));
      if (!result) return;
      if (result.error) {
        // Only a refused fast-forward earns the merge offer; "no tracking
        // information" is a different problem with no merge that would help.
        if (ffOnly && /fast-forward|diverg/i.test(result.error)) setMergeRetry(true);
      } else {
        setMergeRetry(false);
      }
    },
    [run, path],
  );

  const showDiff = useCallback(
    async (entry) => {
      setArmed("");
      setDiffView({ path: entry.path, loading: true, text: "", empty: false, error: "" });
      try {
        const result = await api.getGitDiff(path, {
          staged: !!entry.staged,
          file: entry.path,
        });
        setDiffView({
          path: entry.path,
          loading: false,
          text: result.diff || "",
          empty: !!result.empty,
          error: result.error || "",
        });
      } catch (err) {
        setDiffView({
          path: entry.path,
          loading: false,
          text: "",
          empty: false,
          error: String(err?.message || err),
        });
      }
    },
    [path],
  );

  if (present === false) {
    return (
      <p className="hud-micro" style={{ padding: "var(--space-3)", color: "var(--hud-text-mute)" }}>
        {ar ? "هذا المجلد ليس مستودع git" : "This folder is not a git repository"}
      </p>
    );
  }

  if (!status) {
    return (
      <div
        style={{
          padding: "var(--space-3)",
          display: "flex",
          flexDirection: "column",
          gap: "var(--space-2)",
          alignItems: "flex-start",
        }}
      >
        <p
          role={loadError ? "alert" : "status"}
          className="hud-micro"
          style={{
            color: loadError ? "var(--hud-red)" : "var(--hud-text-mute)",
          }}
        >
          {loadError ? loadError.message : ar ? "جارٍ القراءة…" : "Reading…"}
        </p>
        {/* 403 is a containment decision: re-asking for the same path cannot
            change the boundary, so offering RETRY would only invite the user
            to press a button that promises nothing. Every other failure can
            pass — git may be busy, the backend may have been restarting — and
            there was no way at all to try again before this. */}
        {loadError && loadError.status !== 403 && (
          <button
            type="button"
            className="hud-micro"
            onClick={() => refresh()}
            style={{
              background: "transparent",
              border: "var(--border-w) solid var(--hud-edge-faint)",
              color: "var(--hud-cyan)",
              padding: "0 var(--space-2)",
            }}
          >
            {ar ? "أعد المحاولة" : "RETRY"}
          </button>
        )}
      </div>
    );
  }

  const entryLabel = (entry) => (
    <span style={{ color: STATUS_TONE[entry.status] || "var(--hud-text)" }}>
      {entry.status}
    </span>
  );

  // Remote-tracking branches are not checkout targets from here: `git checkout
  // origin/x` lands on a detached HEAD, which is a trap for someone who
  // expected a branch switch.
  const localBranches = branches.filter((b) => !b.remote);
  const canSelectBranch =
    !!status.branch &&
    localBranches.length > 0 &&
    localBranches.some((b) => b.name === status.branch);
  // A merge needs somewhere to merge *from*: the other local branches. With
  // one branch there is nothing to offer, so the button is disabled rather
  // than armed into an empty row.
  const mergeCandidates = localBranches.filter((b) => b.name && b.name !== status.branch);

  const actionButton = {
    background: "transparent",
    border: "var(--border-w) solid var(--hud-edge-faint)",
    color: "var(--hud-cyan)",
    padding: "0 var(--space-2)",
  };
  const disabledStyle = { ...actionButton, color: "var(--hud-text-mute)", opacity: 0.6 };

  return (
    <section className="flex flex-col min-h-0" aria-label={ar ? "المصدر" : "Source control"}>
      <header
        className="flex-none flex items-center justify-between"
        style={{ padding: "var(--space-2)", borderBottom: "var(--border-w) solid var(--hud-edge-faint)" }}
      >
        <span className="hud-micro truncate" style={{ color: "var(--hud-cyan)" }}>
          {status.branch || (ar ? "بلا فرع" : "no branch")}
        </span>
        <span className="hud-micro" style={{ color: "var(--hud-text-mute)" }}>
          {status.clean
            ? ar ? "نظيف" : "clean"
            : `${status.staged}/${status.entries.length}`}
        </span>
      </header>

      {error && (
        <div className="flex-none flex items-center gap-2" style={{ padding: "var(--space-1) var(--space-2)" }}>
          <p role="alert" className="hud-micro flex-1" style={{ color: "var(--hud-red)" }}>
            {error}
          </p>
          {mergeRetry && (
            <button
              type="button"
              className="hud-micro flex-none"
              disabled={busy}
              onClick={() => doPull(false)}
              style={actionButton}
            >
              {ar ? "أعد المحاولة بدمج" : "Retry as merge"}
            </button>
          )}
        </div>
      )}

      {/* Branch switch + network verbs. Fetch is the safe one and sits first. */}
      <div
        className="flex-none flex items-center gap-2 flex-wrap"
        style={{ padding: "var(--space-1) var(--space-2)" }}
      >
        {canSelectBranch ? (
          <select
            aria-label={ar ? "فرع" : "Branch"}
            className="hud-micro"
            disabled={busy}
            value={status.branch}
            onChange={(e) => {
              if (e.target.value && e.target.value !== status.branch) {
                run(() => api.gitCheckout(e.target.value, { path }));
              }
            }}
            style={{
              background: "var(--hud-surface, transparent)",
              border: "var(--border-w) solid var(--hud-edge-faint)",
              color: "var(--hud-text)",
            }}
          >
            {localBranches.map((b) => (
              <option key={b.name || b.sha} value={b.name}>
                {b.name}
                {b.current ? (ar ? " (الحالي)" : " (current)") : ""}
              </option>
            ))}
          </select>
        ) : null}
        <button
          type="button"
          className="hud-micro"
          disabled={busy}
          onClick={() => run(() => api.gitFetch({ path }))}
          style={busy ? disabledStyle : actionButton}
          title={ar ? "تحديث المراجع عن بُعد" : "Update remote-tracking refs"}
        >
          {ar ? "جلب" : "Fetch"}
        </button>
        <button
          type="button"
          className="hud-micro"
          disabled={busy}
          onClick={() => doPull(true)}
          style={busy ? disabledStyle : actionButton}
          title={ar ? "سحب (تقديم سريع)" : "Pull (fast-forward)"}
        >
          {ar ? "سحب" : "Pull"}
        </button>
        <button
          type="button"
          className="hud-micro"
          disabled={busy}
          onClick={() => run(() => api.gitPush({ path }))}
          style={busy ? disabledStyle : actionButton}
          title={ar ? "دفع إلى المستودع البعيد" : "Push to the remote"}
        >
          {ar ? "دفع" : "Push"}
        </button>
        <button
          type="button"
          className="hud-micro"
          // Nothing to merge with one branch — disabled rather than armed
          // into an empty row, which is a click wasted on a dead end.
          disabled={busy || mergeCandidates.length === 0}
          onClick={() => {
            setMergeTarget(mergeCandidates[0]?.name || "");
            setMerging(true);
          }}
          style={busy || mergeCandidates.length === 0 ? disabledStyle : actionButton}
          title={
            ar
              ? "دمج فرع آخر محلي في الفرع الحالي"
              : "Merge another local branch into the current one"
          }
        >
          {ar ? "دمج" : "Merge"}
        </button>
        <button
          type="button"
          className="hud-micro"
          // Disabled on a clean tree: staging nothing is always wrong, and
          // pressing it only to be told so wastes the interaction.
          disabled={busy || status.clean}
          onClick={() => run(() => api.gitAdd([], { all: true, path }))}
          style={busy || status.clean ? disabledStyle : actionButton}
        >
          {ar ? "تسطير الكل" : "Stage all"}
        </button>
      </div>

      {/* Arm-then-click, like discard: a merge can conflict, so the first
          click only chooses the branch and the second sends it. */}
      {merging && (
        <div
          className="flex-none flex items-center gap-2 flex-wrap"
          style={{
            padding: "var(--space-1) var(--space-2)",
            borderTop: "var(--border-w) solid var(--hud-edge-faint)",
            background: "var(--hud-raised)",
          }}
        >
          <span className="hud-micro" style={{ color: "var(--hud-amber)" }}>
            {ar ? "دمج" : "Merge"}
          </span>
          <select
            aria-label={ar ? "فرع الدمج" : "Branch to merge"}
            className="hud-micro"
            disabled={busy}
            value={mergeTarget}
            onChange={(e) => setMergeTarget(e.target.value)}
            style={{
              background: "var(--hud-surface, transparent)",
              border: "var(--border-w) solid var(--hud-edge-faint)",
              color: "var(--hud-text)",
            }}
          >
            {mergeCandidates.map((b) => (
              <option key={b.name} value={b.name}>
                {b.name}
              </option>
            ))}
          </select>
          <span className="hud-micro" style={{ color: "var(--hud-text-mute)" }}>
            {ar ? "في" : "into"} {status.branch}
          </span>
          <button
            type="button"
            className="hud-micro"
            // The target must still be a candidate: branches can refresh
            // while the row is open, and merging a branch that just became
            // current would be a surprise.
            disabled={
              busy ||
              !mergeTarget ||
              !mergeCandidates.some((b) => b.name === mergeTarget)
            }
            onClick={() => {
              setMerging(false);
              run(() => api.gitMerge(mergeTarget, { path }));
            }}
            style={{ color: "var(--hud-amber)" }}
          >
            {ar ? "تأكيد" : "MERGE"}
          </button>
          <button
            type="button"
            className="hud-micro"
            disabled={busy}
            onClick={() => setMerging(false)}
            style={{ color: "var(--hud-text-mute)" }}
          >
            {ar ? "إلغاء" : "CANCEL"}
          </button>
        </div>
      )}

      <div className="flex-none flex flex-col" style={{ maxHeight: "40%", overflowY: "auto" }}>
        {status.entries.length === 0 ? (
          <p className="hud-micro" style={{ padding: "var(--space-2)", color: "var(--hud-text-mute)" }}>
            {ar ? "لا تغييرات" : "No changes"}
          </p>
        ) : (
          status.entries.map((entry) => (
            <div
              key={`${entry.staged}-${entry.path}`}
              className="flex items-center gap-2"
              style={{ padding: "var(--space-1) var(--space-2)" }}
            >
              <span className="hud-micro flex-none" style={{ minWidth: "5rem" }}>
                {entryLabel(entry)}
              </span>
              <span
                className="hud-micro flex-1 truncate"
                style={{ color: "var(--hud-text-mute)", direction: "ltr", textAlign: "left" }}
                title={entry.path}
              >
                {entry.path}
              </span>
              {/* Stage appears when there is worktree change to stage — for
                  `MM` that is alongside Unstage, which is correct. */}
              {(!entry.staged || entry.unstaged) && (
                <button
                  type="button"
                  className="hud-micro flex-none"
                  disabled={busy}
                  onClick={() => run(() => api.gitAdd([entry.path], { path }))}
                  style={{ color: "var(--hud-cyan)" }}
                >
                  {ar ? "أضف" : "Stage"}
                </button>
              )}
              {entry.staged && (
                <button
                  type="button"
                  className="hud-micro flex-none"
                  disabled={busy}
                  onClick={() => run(() => api.gitRestore([entry.path], { staged: true, path }))}
                  style={{ color: "var(--hud-cyan)" }}
                >
                  {ar ? "إزالة" : "Unstage"}
                </button>
              )}
              <button
                type="button"
                className="hud-micro flex-none"
                disabled={busy}
                onClick={() => showDiff(entry)}
                style={{ color: "var(--hud-text-mute)" }}
              >
                {ar ? "فرق" : "Diff"}
              </button>
              {entry.unstaged && entry.status !== "untracked" && (
                armed === entry.path ? (
                  <span className="hud-micro flex-none flex items-center gap-1">
                    <button
                      type="button"
                      className="hud-micro"
                      disabled={busy}
                      aria-label={`Confirm discard ${entry.path}`}
                      onClick={() =>
                        run(() => api.gitRestore([entry.path], { staged: false, path }))
                      }
                      style={{ color: "var(--hud-red)" }}
                    >
                      {ar ? "تأكيد" : "Confirm"}
                    </button>
                    <button
                      type="button"
                      className="hud-micro"
                      disabled={busy}
                      onClick={() => setArmed("")}
                      style={{ color: "var(--hud-text-mute)" }}
                    >
                      {ar ? "إلغاء" : "Cancel"}
                    </button>
                  </span>
                ) : (
                  <button
                    type="button"
                    className="hud-micro flex-none"
                    disabled={busy}
                    aria-label={`Discard ${entry.path}`}
                    onClick={() => setArmed(entry.path)}
                    style={{ color: "var(--hud-red)" }}
                  >
                    {ar ? "تجاهل" : "Discard"}
                  </button>
                )
              )}
            </div>
          ))
        )}
      </div>

      {diffView && (
        <div
          className="flex-none"
          style={{
            padding: "var(--space-1) var(--space-2)",
            borderTop: "var(--border-w) solid var(--hud-edge-faint)",
          }}
        >
          <div className="flex items-center justify-between gap-2">
            <span
              className="hud-micro truncate"
              style={{ color: "var(--hud-cyan)", direction: "ltr", textAlign: "left" }}
              title={diffView.path}
            >
              {diffView.path}
            </span>
            <button
              type="button"
              className="hud-micro flex-none"
              onClick={() => setDiffView(null)}
              style={{ color: "var(--hud-text-mute)" }}
            >
              {ar ? "إغلاق" : "Close"}
            </button>
          </div>
          {diffView.loading ? (
            <p className="hud-micro" style={{ color: "var(--hud-text-mute)" }}>
              {ar ? "جارٍ حساب الفرق…" : "Computing diff…"}
            </p>
          ) : diffView.error ? (
            <p role="alert" className="hud-micro" style={{ color: "var(--hud-red)" }}>
              {diffView.error}
            </p>
          ) : diffView.empty ? (
            // An untracked file has nothing to diff against yet — say so
            // rather than showing an empty box that reads as "no changes".
            <p className="hud-micro" style={{ color: "var(--hud-text-mute)" }}>
              {ar
                ? "لا يوجد فرق نصي — قد يكون الملف غير متتبع بعد"
                : "No textual diff — the file may not be tracked yet"}
            </p>
          ) : (
            <pre
              className="hud-micro"
              style={{
                direction: "ltr",
                textAlign: "left",
                margin: 0,
                padding: "var(--space-1) 0",
                overflow: "auto",
                maxHeight: "12rem",
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
                color: "var(--hud-text)",
              }}
            >
              {diffView.text}
            </pre>
          )}
        </div>
      )}

      <div className="flex-none flex items-center gap-2" style={{ padding: "var(--space-2)" }}>
        <input
          className="hud-input flex-1"
          value={commitMessage}
          onChange={(e) => setCommitMessage(e.target.value)}
          placeholder={ar ? "رسالة الالتزام" : "commit message"}
          aria-label={ar ? "رسالة الالتزام" : "Commit message"}
        />
        <button
          type="button"
          className="hud-micro flex-none"
          // Disabled rather than merely failing on click: a commit with nothing
          // staged is always wrong, and letting someone press it only to be
          // told so wastes the interaction.
          disabled={busy || status.staged === 0 || !commitMessage.trim()}
          onClick={() => run(() => api.gitCommit(commitMessage.trim(), { path }))}
          style={{
            color:
              status.staged > 0 && commitMessage.trim() ? "var(--hud-cyan)" : "var(--hud-text-mute)",
          }}
        >
          {ar ? "التزام" : "Commit"}
        </button>
      </div>

      <ol
        className="flex-1 flex flex-col min-h-0 overflow-y-auto"
        style={{ padding: "var(--space-2)", listStyle: "none" }}
        aria-label={ar ? "السجل" : "History"}
      >
        {/* Three states, three answers — see `logView`. Collapsing them into an
            empty list made a failed request indistinguishable from an empty
            repository, which is a claim the panel was in no position to make. */}
        {logView.loading ? (
          <li className="hud-micro" style={{ padding: "var(--space-1) 0", color: "var(--hud-text-mute)" }}>
            {ar ? "جارٍ تحميل السجل…" : "Loading history…"}
          </li>
        ) : logView.error ? (
          <li
            className="hud-micro"
            style={{ padding: "var(--space-1) 0", color: "var(--hud-red)" }}
          >
            {logView.error}
          </li>
        ) : logView.commits.length === 0 ? (
          <li className="hud-micro" style={{ padding: "var(--space-1) 0", color: "var(--hud-text-mute)" }}>
            {ar ? "لا التزامات بعد في هذا الفرع" : "No commits yet on this branch"}
          </li>
        ) : (
          logView.commits.map((commit) => (
            <li key={commit.sha} className="hud-micro" style={{ padding: "var(--space-1) 0" }}>
              <span style={{ color: "var(--hud-text-mute)", direction: "ltr" }}>{commit.short}</span>{" "}
              {commit.subject}
            </li>
          ))
        )}
      </ol>
    </section>
  );
}

export default memo(GitPanel);
