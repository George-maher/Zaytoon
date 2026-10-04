/**
 * Turn the flat activity event log into per-tool rows with a live state.
 *
 * The activity feed used to render one line per *event*, so a long tool
 * showed "started", then a string of "· heartbeat" lines, then "finished" —
 * three unrelated-looking rows for one tool, and no way to tell whether a
 * tool was still running. This folds the events back into the thing the user
 * actually thinks in: one row per tool invocation, with a status chip.
 *
 * Pure and time-explicit: every event carries the wall-clock `at` the hook
 * stamped on receipt, and duration is derived from those stamps (or from the
 * backend's own `duration_ms` when it sends one). Nothing here reads a clock,
 * so the rows a test builds are the rows the app renders.
 *
 * Concurrency is the point: two tools running at once produce two `running`
 * rows, and the first one finishing does not touch the other.
 */

const MAX_SUMMARY = 160;

function newRow(e, rows, status = "running") {
  return {
    // Deterministic: position in the row list, so re-deriving the same event
    // list yields the same keys (React reconciliation stays stable).
    key: `${e.name}#${rows.length}`,
    name: e.name,
    label: e.label || e.name,
    status,
    message: "",
    summary: "",
    startedAt: typeof e.at === "number" ? e.at : null,
    finishedAt: null,
    durationMs: typeof e.durationMs === "number" ? e.durationMs : null,
  };
}

/** Last row for this tool that is still running… */
function runningRow(rows, name) {
  for (let i = rows.length - 1; i >= 0; i--) {
    if (rows[i].name === name && rows[i].status === "running") return rows[i];
  }
  return null;
}

/** …else the most recent row for it, else null. */
function lastRow(rows, name) {
  for (let i = rows.length - 1; i >= 0; i--) {
    if (rows[i].name === name) return rows[i];
  }
  return null;
}

/**
 * @param {Array<{kind:string,name:string,label?:string,message?:string,
 *                summary?:string,success?:boolean,at?:number,durationMs?:number}>} activity
 * @returns {Array<{key,name,label,status,message,summary,startedAt,finishedAt,durationMs}>}
 *          `status` is "running" | "ok" | "failed".
 */
export function deriveToolRows(activity = []) {
  const rows = [];
  for (const e of activity) {
    if (!e || !e.name) continue;

    if (e.kind === "started") {
      // A second invocation of the same tool is a new row, never a re-open
      // of the finished one.
      rows.push(newRow(e, rows));
      continue;
    }

    if (e.kind === "progress") {
      // Heartbeat: attach to the running invocation. A progress frame for a
      // tool we never saw start (a long tool whose `started` frame the
      // client joined late) still becomes a running row rather than being
      // dropped.
      let row = runningRow(rows, e.name) || lastRow(rows, e.name);
      if (!row) {
        row = newRow(e, rows);
        rows.push(row);
      }
      row.message = e.message || "";
      continue;
    }

    if (e.kind === "finished") {
      let row = runningRow(rows, e.name) || lastRow(rows, e.name);
      if (!row) {
        row = newRow(e, rows);
        rows.push(row);
      }
      row.status = e.success ? "ok" : "failed";
      row.summary = (e.summary || "").slice(0, MAX_SUMMARY);
      row.finishedAt = typeof e.at === "number" ? e.at : null;
      if (typeof e.durationMs === "number") {
        row.durationMs = e.durationMs;
      } else if (row.startedAt !== null && row.finishedAt !== null) {
        row.durationMs = Math.max(0, row.finishedAt - row.startedAt);
      }
      row.message = "";
    }
  }
  return rows;
}

/** Human duration for a chip: "820ms", "1.4s", or null when unknown. */
export function formatDuration(ms) {
  if (typeof ms !== "number" || !isFinite(ms) || ms < 0) return null;
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

export default deriveToolRows;
