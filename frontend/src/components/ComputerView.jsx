import { memo, useCallback, useEffect, useRef, useState } from "react";
import Panel, { TelemetryRow } from "./HUD/HUDPanel";
import SystemStatus from "./HUD/SystemStatus";
import StatusRing from "./HUD/StatusRing";
import { desktop } from "../lib/desktop";
import {
  getComputerApps,
  getFileRoot,
  getProcesses,
  getProcessLogs,
  listDirectory,
  openApp,
  stopProcess,
} from "../lib/api";

/**
 * Computer — the machine itself: what it is, what may be launched on it, what
 * is running, and where things live.
 *
 * Four panels, each fed by an endpoint that already existed except for
 * `GET /computer/apps`:
 *
 *   SYSTEM      `GET /health` + `GET /system`   (reused: HUD/SystemStatus)
 *   APPS        `GET /computer/apps?resolve=true` (the launcher's allow-list,
 *               plus positive evidence of what is actually on this machine)
 *   PROCESSES   `GET /processes` + `/processes/{id}/logs`
 *               (only what Zaytoon started — and, once asked, what it printed)
 *   FILES       `GET /files/root` + `/files/list` (workspace-contained)
 *
 * Three rules this view keeps, none of which are visible in a screenshot:
 *
 * 1. **Every action goes through the gated tool layer.** Launching a tile is
 *    `POST /command/open-app`, stopping a process is
 *    `POST /processes/{id}/stop`, and both are the existing routes — this
 *    screen adds no way to do anything the assistant could not already do.
 * 2. **It claims no more than it knows.** The app catalogue is the set of
 *    names the launcher accepts; availability is a separate, opt-in answer
 *    that reports *evidence* rather than a guess, and a tile with no evidence
 *    says "not found here" rather than "not installed" — a portable copy with
 *    no Start Menu entry is invisible to the probe, and calling it absent
 *    would be the worse error. The process list is only what Zaytoon started —
 *    said out loud, because "no processes" would otherwise read as "nothing is
 *    running".
 * 3. **A failed refresh clears the panel.** Stale rows left on screen read as
 *    current, which is the one thing a status panel must never do.
 */

const CATEGORY_LABELS = {
  browser: { en: "BROWSERS", ar: "المتصفحات" },
  editor: { en: "EDITORS", ar: "المحررات" },
  terminal: { en: "TERMINALS", ar: "الأطراف" },
  system: { en: "SYSTEM", ar: "النظام" },
  media: { en: "MEDIA", ar: "الوسائط" },
  chat: { en: "CHAT", ar: "المحادثة" },
  office: { en: "OFFICE", ar: "المكتب" },
};

/**
 * Apps by category, in the backend's declared order, then any category it did
 * not list — a grouping this build has not heard of must still show its apps
 * rather than silently dropping them.
 */
function groupByCategory(apps, declared) {
  if (!Array.isArray(apps)) return [];
  const byKey = new Map();
  for (const app of apps) {
    const key = app.category || "other";
    if (!byKey.has(key)) byKey.set(key, { key, apps: [] });
    byKey.get(key).apps.push(app);
  }
  const seen = new Set();
  const ordered = [];
  for (const key of declared) {
    const group = byKey.get(key);
    if (group) {
      ordered.push(group);
      seen.add(key);
    }
  }
  for (const group of byKey.values()) if (!seen.has(group.key)) ordered.push(group);
  return ordered;
}

/**
 * Bilingual category heading. An unrecognised key falls back to the key
 * itself: a category the backend added must still be *shown*, even before
 * someone adds its translation. Hiding apps behind a missing label would be
 * the worse failure.
 */
function categoryLabel(key, ar) {
  const entry = CATEGORY_LABELS[key];
  if (entry) return ar ? entry.ar : entry.en;
  return String(key).toUpperCase();
}

function fmtBytes(n) {
  if (typeof n !== "number" || n < 0) return "—";
  if (n === 0) return "0";
  if (n < 1024) return `${n}B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)}KB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)}MB`;
  return `${(n / 1024 ** 3).toFixed(1)}GB`;
}

function fmtDuration(ms) {
  if (typeof ms !== "number" || ms < 0) return "—";
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  const h = Math.floor(m / 60);
  if (h) return `${h}h ${m % 60}m`;
  if (m) return `${m}m ${s % 60}s`;
  return `${s}s`;
}

/**
 * The compact control that sits in a panel header. Reads the density scale.
 *
 * `ar` matters: tracking out connected Arabic script is a legibility bug, not
 * a style choice, so the letterspacing applies to Latin only.
 */
function PanelButton({ onClick, disabled, children, title, ar = false }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      className="hud-micro"
      style={{
        padding: "var(--space-1) var(--space-2)",
        letterSpacing: ar ? "0" : "0.12em",
        textTransform: "uppercase",
        cursor: disabled ? "default" : "pointer",
        opacity: disabled ? 0.5 : 1,
        border: "var(--border-w) solid var(--hud-edge-faint)",
        color: "var(--hud-text-dim)",
        background: "transparent",
      }}
    >
      {children}
    </button>
  );
}

/**
 * One line of panel feedback.
 *
 * `announce` controls the standalone `role="alert"`: it is right for a load
 * failure, which nobody is watching for, and wrong inside a mounted
 * `role="status"` region — two nested live regions on one message are two
 * announcements of it.
 */
function Note({ tone, children, announce = true }) {
  const color =
    tone === "error" ? "var(--hud-red)" : tone === "ok" ? "var(--hud-green)" : "var(--hud-text-mute)";
  return (
    <p
      role={announce && tone === "error" ? "alert" : undefined}
      style={{
        fontSize: "var(--font-sm)",
        fontFamily: "var(--font-mono)",
        color,
        margin: 0,
      }}
    >
      {children}
    </p>
  );
}

// ---------------------------------------------------------------------------
// Apps
// ---------------------------------------------------------------------------

function AppsPanel({ language }) {
  const ar = language === "ar";
  const [apps, setApps] = useState(null); // null until the first answer
  const [declared, setDeclared] = useState([]);
  const [verdict, setVerdict] = useState(null); // availability, if we asked and got one
  const [err, setErr] = useState(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(null);
  const [notice, setNotice] = useState(null);

  const load = useCallback(async () => {
    try {
      // `resolve: true` is asked for explicitly: it costs a filesystem walk,
      // and the panel is the one place where knowing before you click is
      // worth that cost.
      const next = await getComputerApps({ resolve: true });
      setApps(next.unavailable ? null : next.apps);
      setDeclared(next.categories || []);
      setVerdict(
        next.unavailable
          ? null
          : { resolved: next.resolved === true, found: next.found || 0, error: next.resolveError || null },
      );
      setErr(next.unavailable ? next : null);
      setLoaded(true);
    } catch (e) {
      // Clear rather than keep the previous catalogue on screen: a stale list
      // of launchable apps looks exactly like a current one.
      setApps(null);
      setDeclared([]);
      setVerdict(null);
      setErr(e?.message || String(e));
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const launch = useCallback(
    async (app) => {
      if (busy) return;
      setBusy(app.id);
      setNotice(null);
      try {
        // The real allow-list and the real gate: the endpoint is the same one
        // the assistant uses, so what happens here is what happens there.
        const res = await openApp(app.id);
        const ok = res?.success === true;
        setNotice({
          ok,
          text: ok
            ? ar
              ? `تم تشغيل ${app.label}`
              : `LAUNCHED ${app.label}`
            : `${ar ? "تعذّر التشغيل" : "COULD NOT LAUNCH"} ${app.label}: ${
                res?.error || res?.output || "—"
              }`,
        });
      } catch (e) {
        setNotice({
          ok: false,
          text: `${ar ? "تعذّر الاتصال بالخدمة" : "REQUEST FAILED"}: ${e?.message || e}`,
        });
      } finally {
        setBusy(null);
      }
    },
    [ar, busy],
  );

  const groups = groupByCategory(apps, declared);

  return (
    <Panel
      title={ar ? "التطبيقات" : "APPS"}
      code="APP"
      led
      trailing={
        <PanelButton ar={ar} onClick={load} disabled={!loaded || busy !== null} title={ar ? "تحديث" : "Refresh"}>
          {ar ? "تحديث" : "REFRESH"}
        </PanelButton>
      }
    >
      {/* The live region is mounted from the start: a screen reader only
          announces a change to a region that already exists, so rendering the
          container together with its first message would announce nothing. */}
      <div role="status" aria-live="polite" style={{ marginBottom: notice ? "var(--space-2)" : undefined }}>
        {notice && <Note tone={notice.ok ? "ok" : "error"} announce={false}>{notice.text}</Note>}
      </div>

      {!loaded && <Note>{ar ? "جارٍ التحميل…" : "LOADING…"}</Note>}

      {loaded && err && (
        <Note tone="error">
          {err && typeof err === "object"
            ? ar
              ? "قائمة التطبيقات غير متاحة على هذا الخادم"
              : "App catalogue unavailable on this backend"
            : `${ar ? "تعذّر جلب قائمة التطبيقات" : "App fetch failed"}: ${err}`}
        </Note>
      )}

      {loaded && !err && apps && apps.length === 0 && (
        <Note>{ar ? "لا توجد تطبيقات" : "No applications listed"}</Note>
      )}

      {/* Headline first: a count answers "do I have anything here" without
          making the reader scan twenty-six tiles for the lit ones. */}
      {loaded && !err && verdict?.resolved && apps?.length > 0 && (
        <p
          className="hud-micro"
          style={{
            fontSize: "var(--font-xs)",
            letterSpacing: ar ? "0" : "0.16em",
            color: "var(--hud-green)",
            margin: "0 0 var(--space-2)",
          }}
        >
          {ar ? "موجود على هذا الجهاز" : "FOUND ON THIS MACHINE"}{" "}
          {verdict.found} / {apps.length}
        </p>
      )}

      {loaded &&
        !err &&
        groups.map((group) => (
          <div key={group.key} style={{ marginBottom: "var(--space-3)" }}>
            <p
              className="hud-micro"
              style={{
                fontSize: "var(--font-xs)",
                letterSpacing: ar ? "0" : "0.16em",
                color: "var(--hud-text-mute)",
                margin: "0 0 var(--space-1)",
              }}
            >
              {categoryLabel(group.key, ar)}
            </p>
            <div
              style={{
                display: "grid",
                gap: "var(--space-1)",
                gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 132px), 1fr))",
              }}
            >
              {group.apps.map((app) => {
                // Only light a tile when the backend actually said it looked.
                // `resolved` is the gate: a payload that carries no verdict
                // must render the plain tile it always did, never a hollow dot
                // that reads as "not installed".
                const known = verdict?.resolved === true;
                const present = known && app.installed === true;
                const stateText = present
                  ? ar
                    ? "موجود على هذا الجهاز"
                    : "found on this machine"
                  : ar
                    ? "لم يُعثر على هدف تشغيل هنا — وليس دليلاً على أنه غير مثبّت"
                    : "no launch target found here — not proof it is missing";
                return (
                  <button
                    key={app.id}
                    type="button"
                    onClick={() => launch(app)}
                    disabled={busy !== null}
                    title={known ? `${app.id} — ${stateText}` : app.id}
                    style={{
                      padding: "var(--space-2)",
                      fontSize: "var(--font-sm)",
                      fontFamily: "var(--font-mono)",
                      textAlign: "start",
                      cursor: busy ? "default" : "pointer",
                      opacity: busy && busy !== app.id ? 0.5 : 1,
                      border: "var(--border-w) solid var(--hud-edge-faint)",
                      color: busy === app.id ? "var(--hud-cyan)" : "var(--hud-text)",
                      background: "transparent",
                    }}
                  >
                    <span
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: "var(--space-2)",
                        minWidth: 0,
                      }}
                    >
                      {known && (
                        <span
                          aria-hidden="true"
                          className="flex-none"
                          style={{
                            color: present
                              ? "var(--hud-green)"
                              : "var(--hud-text-dim)",
                          }}
                        >
                          {present ? "●" : "○"}
                        </span>
                      )}
                      <span className="truncate" style={{ minWidth: 0 }}>
                        {busy === app.id ? "…" : app.label}
                      </span>
                    </span>
                    {/* Colour alone cannot carry this: the dot is decorative,
                        the sentence is what a screen reader gets. */}
                    {known && <span className="sr-only">— {stateText}</span>}
                  </button>
                );
              })}
            </div>
          </div>
        ))}

      {loaded && !err && apps && apps.length > 0 && (
        <p
          style={{
            fontSize: "var(--font-xs)",
            fontFamily: "var(--font-mono)",
            color: "var(--hud-text-mute)",
            margin: 0,
          }}
        >
          {verdict?.resolved ? (
            <>
              <span style={{ color: "var(--hud-green)" }}>●</span>{" "}
              {ar ? "موجود على هذا الجهاز" : "found on this machine"} {" · "}
              <span style={{ color: "var(--hud-text-dim)" }}>○</span>{" "}
              {ar
                ? "لم يُعثر على هدف تشغيل هنا — وليس دليلاً على أنه غير مثبّت"
                : "no launch target found here — not proof it is missing"}
            </>
          ) : (
            (ar
              ? "هذا ما يسمح به الإقلاع — لا يدل على ما هو مثبّت"
              : "This is what launching allows — not a claim about what is installed")
          )}
          {verdict?.error
            ? ar
              ? " · تعذّر فحص التوفر"
              : " · availability probe failed"
            : ""}
        </p>
      )}
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// Processes
// ---------------------------------------------------------------------------

function ProcessesPanel({ language }) {
  const ar = language === "ar";
  const [list, setList] = useState(null);
  const [err, setErr] = useState(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(null);
  // A refused stop is an outcome of *this* action, not a failed read — mixing
  // the two would clear a perfectly good process list because one STOP was
  // declined, which reads as "the processes are gone".
  const [notice, setNotice] = useState(null);
  // Output is fetched on demand, per process. Nothing calls
  // `GET /processes/{id}/logs` until the user asks, so opening this panel
  // costs one request for the list and no more.
  const [openId, setOpenId] = useState(null);
  const [logState, setLogState] = useState({});

  const load = useCallback(async () => {
    try {
      const next = await getProcesses();
      setList(next);
      setErr(null);
    } catch (e) {
      setList(null);
      setErr(e?.message || String(e));
    }
    setLoaded(true);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const onToggleLogs = useCallback(
    async (id) => {
      const key = String(id);
      if (openId === key) {
        setOpenId(null);
        return;
      }
      // Refetched on every open rather than cached: a dev server's output
      // from a minute ago is a stale list of messages, and a status panel
      // must never show something that reads as current but is not.
      setOpenId(key);
      setLogState((m) => ({ ...m, [key]: { loading: true, lines: [], error: null } }));
      try {
        const res = await getProcessLogs(id, 60);
        setLogState((m) => ({
          ...m,
          [key]: res.unavailable
            ? {
                loading: false,
                lines: [],
                error: ar ? "المخرجات غير متاحة" : "Output unavailable",
              }
            : { loading: false, lines: res.lines, error: null },
        }));
      } catch (e) {
        setLogState((m) => ({
          ...m,
          [key]: { loading: false, lines: [], error: e?.message || String(e) },
        }));
      }
    },
    [ar, openId],
  );

  const onStop = useCallback(
    async (id) => {
      setBusy(id);
      setNotice(null);
      try {
        const res = await stopProcess(id);
        if (res.unavailable) {
          setNotice({ ok: false, text: ar ? "التوقف غير متاح" : "Stop unavailable" });
        } else if (res.stopped) {
          setNotice({ ok: true, text: ar ? "تم الإيقاف" : "STOPPED" });
        } else {
          // The registry's refusal: Zaytoon only stops what it started.
          setNotice({ ok: false, text: `${ar ? "رُفض" : "REFUSED"}: ${res.error || "—"}` });
        }
        await load();
      } catch (e) {
        setNotice({ ok: false, text: e?.message || String(e) });
      } finally {
        setBusy(null);
      }
    },
    [ar, load],
  );

  const processes = Array.isArray(list?.processes) ? list.processes : [];

  return (
    <Panel
      title={ar ? "العمليات" : "PROCESSES"}
      code="PRC"
      led
      accent={err ? "var(--hud-red)" : list?.unavailable ? "var(--hud-amber)" : "var(--hud-cyan)"}
      trailing={
        <PanelButton ar={ar} onClick={load} disabled={!loaded || busy !== null} title={ar ? "تحديث" : "Refresh"}>
          {ar ? "تحديث" : "REFRESH"}
        </PanelButton>
      }
    >
      {/* Mounted from the start so a refusal is announced; see AppsPanel. */}
      <div role="status" aria-live="polite" style={{ marginBottom: notice ? "var(--space-2)" : undefined }}>
        {notice && <Note tone={notice.ok ? "ok" : "error"} announce={false}>{notice.text}</Note>}
      </div>

      {!loaded && <Note>{ar ? "جارٍ التحميل…" : "LOADING…"}</Note>}

      {loaded && err && <Note tone="error">{`${ar ? "تعذّر جلب العمليات" : "Process fetch failed"}: ${err}`}</Note>}

      {loaded && !err && list?.unavailable && (
        <Note tone="error">
          {ar ? "تتبّع العمليات غير متاح على هذا الخادم" : "Process tracking unavailable on this backend"}
        </Note>
      )}

      {loaded && !err && !list?.unavailable && processes.length === 0 && (
        <Note>{ar ? "لا توجد عمليات خلفية" : "No background processes"}</Note>
      )}

      {loaded &&
        !err &&
        !list?.unavailable &&
        processes.map((p) => {
          const id = p.id ?? p.pid ?? p.name;
          const key = String(id);
          const isOpen = openId === key;
          const logs = logState[key];
          return (
            <div key={key}>
              <div
                className="flex items-center"
                style={{
                  gap: "var(--space-2)",
                  padding: "var(--space-1) 0",
                  borderBottom: isOpen
                    ? 0
                    : "var(--border-w) solid var(--hud-edge-faint)",
                  fontSize: "var(--font-sm)",
                  fontFamily: "var(--font-mono)",
                }}
              >
                <span className="truncate" style={{ color: "var(--hud-text)" }} title={p.command || p.name}>
                  {p.name || p.command || id}
                </span>
                <span className="flex-none" style={{ color: "var(--hud-text-mute)" }}>
                  {p.status || "—"}
                </span>
                <span className="flex-none" style={{ color: "var(--hud-text-mute)" }} title={p.cwd}>
                  {typeof p.pid === "number" ? `#${p.pid}` : "—"}
                </span>
                <span className="flex-none" style={{ color: "var(--hud-text-mute)" }}>
                  {fmtDuration(p.duration_ms)}
                </span>
                <span style={{ marginInlineStart: "auto", display: "flex", gap: "var(--space-1)" }}>
                  <PanelButton
                    ar={ar}
                    onClick={() => onToggleLogs(id)}
                    disabled={busy !== null || logs?.loading === true}
                    title={ar ? "آخر المخرجات" : "Recent output"}
                  >
                    {logs?.loading === true ? "…" : isOpen ? (ar ? "إخفاء" : "HIDE") : ar ? "مخرجات" : "LOGS"}
                  </PanelButton>
                  <PanelButton ar={ar} onClick={() => onStop(id)} disabled={busy !== null} title={ar ? "إيقاف" : "Stop"}>
                    {busy === id ? "…" : ar ? "إيقاف" : "STOP"}
                  </PanelButton>
                </span>
              </div>

              {/* The one computer action that had no UI: the endpoint
                  existed, the output was captured, and there was no way to
                  read either — so "start a server and watch it" ended at
                  "the process is running". */}
              {isOpen && (
                <div
                  style={{
                    padding: "var(--space-2) 0 var(--space-3)",
                    borderBottom: "var(--border-w) solid var(--hud-edge-faint)",
                  }}
                >
                  {logs?.loading === true && (
                    <p className="hud-micro" style={{ fontSize: "var(--font-xs)", color: "var(--hud-text-mute)", margin: 0 }}>
                      {ar ? "جارٍ التحميل…" : "LOADING…"}
                    </p>
                  )}
                  {logs?.error && (
                    <p className="hud-micro" style={{ fontSize: "var(--font-xs)", color: "var(--hud-red)", margin: 0 }}>
                      {logs.error}
                    </p>
                  )}
                  {logs?.loading !== true && !logs?.error && logs?.lines?.length === 0 && (
                    <p className="hud-micro" style={{ fontSize: "var(--font-xs)", color: "var(--hud-text-mute)", margin: 0 }}>
                      {ar ? "لا توجد مخرجات مسجّلة" : "No output captured"}
                    </p>
                  )}
                  {logs?.lines?.length > 0 && (
                    <pre
                      style={{
                        margin: 0,
                        maxHeight: "180px",
                        overflow: "auto",
                        padding: "var(--space-2)",
                        fontSize: "var(--font-xs)",
                        fontFamily: "var(--font-mono)",
                        color: "var(--hud-text-dim)",
                        background: "color-mix(in srgb, var(--hud-void) 60%, transparent)",
                        border: "var(--border-w) solid var(--hud-edge-faint)",
                        whiteSpace: "pre-wrap",
                        wordBreak: "break-word",
                      }}
                    >
                      {logs.lines.join("\n")}
                    </pre>
                  )}
                </div>
              )}
            </div>
          );
        })}

      {/* Always shown, not only when the list is empty: without it "no
          processes" reads as "the machine is idle". */}
      <p
        style={{
          fontSize: "var(--font-xs)",
          fontFamily: "var(--font-mono)",
          color: "var(--hud-text-mute)",
          margin: "var(--space-2) 0 0",
        }}
      >
        {ar
          ? "تُعرض فقط العمليات التي بدأها زيتون"
          : "Only processes Zaytoon started are tracked"}
      </p>
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// Files
// ---------------------------------------------------------------------------

function FilesPanel({ language, onOpenFile }) {
  const ar = language === "ar";
  const [root, setRoot] = useState(null);
  const [path, setPath] = useState(".");
  const [dir, setDir] = useState(null);
  const [err, setErr] = useState(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  // Bumped by REFRESH so the listing can be re-read without a path change.
  const [nonce, setNonce] = useState(0);
  // Monotonic read id: a slow read of the folder you just left must not land
  // after the read of the folder you are now in and overwrite it.
  const readId = useRef(0);

  const loadRoot = useCallback(async () => {
    try {
      setRoot(await getFileRoot());
    } catch (e) {
      setRoot(null);
      setErr(e?.message || String(e));
    }
  }, []);

  const loadDir = useCallback(async () => {
    const id = (readId.current += 1);
    setBusy(true);
    try {
      const next = await listDirectory(path);
      if (id !== readId.current) return;
      if (next.error) {
        setDir(null);
        setErr(next.error);
      } else {
        setDir(next);
        setErr(null);
      }
    } catch (e) {
      if (id !== readId.current) return;
      setDir(null);
      setErr(e?.message || String(e));
    } finally {
      if (id === readId.current) {
        setBusy(false);
        setLoaded(true);
      }
    }
  }, [path]);

  useEffect(() => {
    loadRoot();
  }, [loadRoot]);

  useEffect(() => {
    loadDir();
  }, [loadDir, nonce]);

  const refresh = useCallback(() => {
    loadRoot();
    setNonce((n) => n + 1);
  }, [loadRoot]);

  const segments = dir?.path && dir.path !== "." ? String(dir.path).split(/[\\/]+/).filter(Boolean) : [];
  const entries = Array.isArray(dir?.entries) ? dir.entries : [];

  return (
    <Panel
      title={ar ? "الملفات" : "FILES"}
      code="FS"
      led
      accent={err ? "var(--hud-red)" : "var(--hud-cyan)"}
      trailing={
        <>
          <PanelButton ar={ar} onClick={refresh} disabled={!loaded || busy} title={ar ? "تحديث" : "Refresh"}>
            {ar ? "تحديث" : "REFRESH"}
          </PanelButton>
          {desktop.available && root?.root ? (
            <PanelButton
              ar={ar}
              onClick={() => desktop.revealInOS(root.root)}
              title={ar ? "إظهار المجلد في النظام" : "Reveal in the file manager"}
            >
              {ar ? "إظهار" : "REVEAL"}
            </PanelButton>
          ) : null}
        </>
      }
    >
      <TelemetryRow label={ar ? "الجذر" : "ROOT"} value={root?.root || "—"} title={root?.root} />
      <TelemetryRow
        label={ar ? "الحالة" : "STATE"}
        value={
          root == null
            ? "—"
            : root.exists === false
              ? ar
                ? "غير موجود"
                : "MISSING"
              : ar
                ? "متصل"
                : "MOUNTED"
        }
        tone={root == null ? "default" : root.exists === false ? "error" : "ok"}
      />

      {/* One gauge, one real measurement. `loaded` gates it because an
          in-flight read has no completion to report — a ring at 0% during
          load would be a fabricated reading, not a pending one. */}
      {loaded && !err && (
        <div
          className="flex items-center"
          style={{ gap: "var(--space-3)", margin: "var(--space-2) 0" }}
        >
          <StatusRing
            value={dir?.truncated === false ? 100 : null}
            tone={dir?.truncated ? "warn" : "ok"}
            label={dir?.truncated
              ? (ar ? "قائمة مختصرة" : "listing truncated")
              : (ar ? "الملفات مرئية بالكامل" : "all entries visible")}
            language={language}
            size={38}
          />
          <p
            className="hud-micro"
            style={{ fontSize: "var(--font-xs)", color: "var(--hud-text-mute)", margin: 0 }}
          >
            {dir?.truncated
              ? (ar
                ? `تُعرض أول ${entries.length} مُدخلاً`
                : `showing the first ${entries.length} entries`)
              : (ar
                ? `${entries.length} مُدخلاً`
                : `${entries.length} ${entries.length === 1 ? "entry" : "entries"}`)}
          </p>
        </div>
      )}

      <div
        className="flex items-center flex-wrap"
        style={{
          gap: "var(--space-1)",
          margin: "var(--space-2) 0 var(--space-1)",
          fontSize: "var(--font-xs)",
          fontFamily: "var(--font-mono)",
        }}
      >
        <button
          type="button"
          onClick={() => setPath(".")}
          className="hud-micro"
          style={{
            padding: "var(--space-1) var(--space-2)",
            border: "var(--border-w) solid var(--hud-edge-faint)",
            color: path === "." ? "var(--hud-cyan)" : "var(--hud-text-dim)",
            background: "transparent",
            cursor: "pointer",
          }}
        >
          {ar ? "الجذر" : "ROOT"}
        </button>
        {segments.map((seg, i) => {
          const target = segments.slice(0, i + 1).join("/");
          const here = i === segments.length - 1;
          return (
            <button
              key={target}
              type="button"
              onClick={() => setPath(target)}
              className="hud-micro"
              style={{
                padding: "var(--space-1) var(--space-2)",
                border: "var(--border-w) solid",
                borderColor: here ? "var(--hud-edge-strong)" : "var(--hud-edge-faint)",
                color: here ? "var(--hud-cyan)" : "var(--hud-text-dim)",
                background: "transparent",
                cursor: "pointer",
              }}
            >
              {seg}
            </button>
          );
        })}
      </div>

      {loaded && err && <Note tone="error">{`${ar ? "تعذّر قراءة المجلد" : "Directory read failed"}: ${err}`}</Note>}
      {!loaded && !err && <Note>{ar ? "جارٍ التحميل…" : "LOADING…"}</Note>}

      {loaded && !err && entries.length === 0 && !busy && (
        <Note>{ar ? "مجلد فارغ" : "Empty folder"}</Note>
      )}

      {entries.map((entry) => (
        <button
          key={entry.path}
          type="button"
          onClick={() => (entry.is_dir ? setPath(entry.path) : onOpenFile?.(entry.path))}
          disabled={entry.is_dir ? busy : !onOpenFile}
          className="flex items-center w-full"
          style={{
            gap: "var(--space-2)",
            padding: "var(--space-1) 0",
            // `border` first: the shorthand would otherwise reset the
            // hairline below it, and a row with no separator reads as one
            // unbroken block of names.
            border: "none",
            borderBottom: "var(--border-w) solid var(--hud-edge-faint)",
            fontSize: "var(--font-sm)",
            fontFamily: "var(--font-mono)",
            textAlign: "start",
            cursor: entry.is_dir ? "pointer" : onOpenFile ? "pointer" : "default",
            background: "transparent",
            color: "var(--hud-text)",
          }}
        >
          <span
            aria-hidden="true"
            className="flex-none"
            style={{
              color: "var(--hud-text-mute)",
              transform: entry.is_dir && ar ? "scaleX(-1)" : undefined,
              width: "0.8em",
            }}
          >
            {entry.is_dir ? "▸" : "–"}
          </span>
          <span className="truncate" title={entry.path}>
            {entry.name}
          </span>
          <span style={{ marginInlineStart: "auto" }} className="flex-none">
            {entry.is_dir ? (ar ? "مجلد" : "DIR") : fmtBytes(entry.size)}
          </span>
        </button>
      ))}

      {dir?.truncated && (
        <Note>{ar ? "قائمة مختصرة" : `Showing the first ${entries.length} entries`}</Note>
      )}
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// The view
// ---------------------------------------------------------------------------

export default memo(function ComputerView({ language = "en", onOpenFile }) {
  const ar = language === "ar";
  return (
    <div
      data-enter="center"
      className="flex-1 min-h-0 overflow-y-auto"
      style={{ padding: "var(--space-4)" }}
    >
      <div
        className="flex items-baseline flex-wrap"
        style={{ gap: "var(--space-3)", marginBottom: "var(--space-3)" }}
      >
        {/* App's header owns the document's single h1; every screen beneath it
            is an h2. */}
        <h2
          className="hud-identity"
          style={{
            fontSize: "var(--font-lg)",
            letterSpacing: ar ? "0" : "0.16em",
            margin: 0,
          }}
        >
          {ar ? "الحاسوب" : "COMPUTER"}
        </h2>
        <p className="hud-micro" style={{ fontSize: "var(--font-xs)", margin: 0 }}>
          {ar ? "ما تراه هذه الآلة وما يُسمح به" : "WHAT THIS MACHINE IS — AND WHAT MAY BE DONE WITH IT"}
        </p>
      </div>

      <div
        style={{
          display: "grid",
          gap: "var(--space-3)",
          gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 340px), 1fr))",
        }}
      >
        <SystemStatus language={language} />
        <AppsPanel language={language} />
        <ProcessesPanel language={language} />
        <FilesPanel language={language} onOpenFile={onOpenFile} />
      </div>
    </div>
  );
});
