import { memo, useCallback, useMemo, useState } from "react";

/**
 * The integrated terminal.
 *
 * Output arrives over the WebSocket (`terminal_output` frames), not by polling:
 * a dev server's output has to appear as it happens, and a poll interval either
 * adds latency or hammers the backend. Polling is used **once**, to catch up
 * when a panel is opened on a session that was already running.
 *
 * The shell rules are the backend's, not the UI's: `split_command` refuses
 * chaining and metacharacters, and a 403 comes back with the reason. The input
 * is rendered verbatim from that message rather than paraphrased, because "that
 * is outside the workspace" and "the command was refused" need different
 * corrections from the user.
 */
function TerminalPanel({
  sessions,
  lines,
  onOpen,
  onSend,
  onClose,
  onRefreshOutput,
  onWatch,
  language = "en",
}) {
  const [activeId, setActiveId] = useState(sessions[0]?.id || null);
  const [command, setCommand] = useState("");
  const [error, setError] = useState(null);

  const ar = language === "ar";
  const active = sessions.find((s) => s.id === activeId) || sessions[0] || null;

  const submit = useCallback(async () => {
    const text = command.trim();
    if (!text) return;
    setError(null);
    const result = await onOpen(text);
    if (result?.error) {
      setError(result.error);
      return;
    }
    setCommand("");
    // Watch immediately: the session exists but nothing has been said about it
    // on this socket, so the panel would sit blank until the next frame.
    if (result?.id) {
      setActiveId(result.id);
      onWatch?.(result.id);
      onRefreshOutput?.(result.id);
    }
  }, [command, onOpen, onWatch, onRefreshOutput]);

  const select = useCallback(
    (id) => {
      setActiveId(id);
      onWatch?.(id);
      onRefreshOutput?.(id);
    },
    [onWatch, onRefreshOutput],
  );

  const rows = useMemo(() => lines || [], [lines]);

  if (sessions.length === 0) {
    return (
      <div className="hud-panel flex-1 flex flex-col min-h-0">
        <header className="hud-panel-head">
          <span>{ar ? "الطرفية" : "TERMINAL"}</span>
        </header>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
          style={{ padding: "var(--space-3)" }}
        >
          <label className="hud-micro" htmlFor="terminal-command" style={{ display: "block" }}>
            {ar ? "أمر جديد" : "New command"}
          </label>
          <input
            id="terminal-command"
            value={command}
            onChange={(e) => setCommand(e.target.value)}
            placeholder="npm run dev"
            aria-describedby="terminal-help"
            /* The shared class carries the fill, hairline, monospace face and
               placeholder colour; only the vertical offset stays inline. */
            className="w-full hud-input"
            style={{ marginTop: "var(--space-2)" }}
          />
          <p id="terminal-help" className="hud-micro" style={{ marginTop: "var(--space-2)", color: "var(--hud-text-mute)" }}>
            {ar
              ? "بدون رموز الصدفة: لا أنابيب، ولا اقتباس، ولا >&."
              : "No shell syntax: no pipes, quotes or chaining."}
          </p>
          {error && (
            <p role="alert" style={{ marginTop: "var(--space-2)", color: "var(--hud-red)" }}>
              {error}
            </p>
          )}
        </form>
      </div>
    );
  }

  return (
    <div className="hud-panel flex-1 flex flex-col min-h-0">
      <header className="hud-panel-head">
        <span>{ar ? "الطرفية" : "TERMINAL"}</span>
        {sessions.length > 1 && (
          <select
            value={active?.id || ""}
            onChange={(e) => select(e.target.value)}
            aria-label={ar ? "اختر الجلسة" : "Select a session"}
            className="hud-micro"
            style={{
              background: "var(--bg0)",
              border: "var(--border-w) solid var(--hud-edge-faint)",
              color: "var(--hud-text-dim)",
              padding: "var(--space-1)",
              fontSize: "var(--font-xs)",
            }}
          >
            {sessions.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name} ({s.status})
              </option>
            ))}
          </select>
        )}
        {active && (
          <button
            onClick={() => onClose?.(active.id)}
            className="hud-micro ml-auto"
            style={{
              padding: "var(--space-1) var(--space-2)",
              border: "var(--border-w) solid var(--hud-edge-faint)",
              color: "var(--hud-text-dim)",
            }}
          >
            {ar ? "إغلاق" : "CLOSE"}
          </button>
        )}
      </header>

      {active && (
        <p className="hud-micro truncate" style={{ padding: "0 var(--space-3) var(--space-2)", color: "var(--hud-text-mute)" }}>
          {active.command} · pid {active.pid ?? "—"} · {active.status}
          {active.exit_code != null && ` → ${active.exit_code}`}
        </p>
      )}

      {/* role="log" + aria-live: a screen reader should hear output as it
          arrives, not only when the panel is re-read from the top. */}
      <div
        role="log"
        aria-live="polite"
        aria-label={ar ? "مخرجات الطرفية" : "Terminal output"}
        className="flex-1 overflow-y-auto min-h-0 hud-mono"
        style={{
          padding: "var(--space-2) var(--space-3)",
          background: "var(--bg0)",
          fontSize: "var(--font-xs)",
          lineHeight: 1.45,
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
        }}
      >
        {rows.length === 0 && (
          <span style={{ color: "var(--hud-text-mute)" }}>
            {ar ? "(لا مخرجات بعد)" : "(no output yet)"}
          </span>
        )}
        {rows.map((entry, index) => (
          <div
            key={`${entry.at ?? index}-${index}`}
            style={{ color: entry.stream === "stderr" ? "var(--hud-amber)" : "var(--hud-text-dim)" }}
          >
            {entry.text}
          </div>
        ))}
      </div>

      {error && (
        <p role="alert" style={{ padding: "var(--space-2) var(--space-3)", color: "var(--hud-red)" }}>
          {error}
        </p>
      )}

      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (active) onSend?.(active.id, `${command}\n`);
          setCommand("");
        }}
        style={{ padding: "var(--space-2) var(--space-3) var(--space-3)" }}
      >
        <label className="hud-micro" htmlFor="terminal-input" style={{ display: "block" }}>
          {ar ? "إرسال للعملية" : "Send to the process"}
        </label>
        <input
          id="terminal-input"
          value={command}
          onChange={(e) => setCommand(e.target.value)}
          disabled={!active || active.status !== "running"}
          /* The running/not-running dimming is the :disabled state from the
             shared class, not a second opacity rule here. */
          className="w-full hud-input"
          style={{ marginTop: "var(--space-2)" }}
        />
      </form>
    </div>
  );
}

export default memo(TerminalPanel);