import { memo } from "react";
import Panel, { TelemetryRow } from "./HUDPanel";
import { STATES } from "./hudState";

/**
 * MissionStatus — routing and turn state.
 *
 * Deliberately does NOT show the plan or the activity log: those belong to
 * ActivityFeed, which already renders them along with the screen-reader
 * announcement. An earlier draft of this file duplicated both, which put the
 * same information in two panels with two different visual treatments.
 *
 * Every row is live state from the hook — the router's `intent`, the resolved
 * state name, the tool the engine is currently inside, and the outstanding
 * reminder count. Nothing here is decorative.
 */
function MissionStatus({
  intent,
  state = "idle",
  activeTool,
  awaitingCommand,
  pendingConfirmation,
  reminders = [],
  language = "en",
}) {
  const ar = language === "ar";
  const s = STATES[state] || STATES.idle;
  const pending = reminders.filter(
    (r) => !r.done && r.status !== "completed" && r.status !== "done",
  );

  return (
    <Panel title={ar ? "المهمة" : "MISSION"} code="MSN" led state={state}>
      <TelemetryRow
        label={ar ? "المسار" : "ROUTE"}
        value={intent ? String(intent).toUpperCase() : (ar ? "خامل" : "IDLE")}
        tone={intent ? "accent" : "default"}
      />
      <TelemetryRow
        label={ar ? "الحالة" : "STATE"}
        value={s.word}
        tone={
          s.hue === "red" ? "error"
            : s.hue === "amber" ? "warn"
              : s.hue === "green" ? "ok"
                : "accent"
        }
      />
      <TelemetryRow
        label={ar ? "الأداة" : "TOOL"}
        value={activeTool ? (activeTool.label || activeTool.name) : null}
        tone={activeTool ? "warn" : "default"}
      />
      <TelemetryRow
        label={ar ? "تذكيرات" : "REMINDERS"}
        value={pending.length || null}
        tone={pending.length ? "warn" : "default"}
      />
      {(awaitingCommand || pendingConfirmation) && (
        <>
          <div className="hud-rule" style={{ margin: "var(--space-2) 0" }} />
          <p
            className="font-mono"
            style={{ fontSize: "var(--font-sm)", color: "var(--hud-amber)", lineHeight: 1.45 }}
          >
            {pendingConfirmation
              ? (ar ? "في انتظار موافقتك" : "AWAITING HUMAN APPROVAL")
              : (ar ? "في انتظار الأمر" : "AWAITING COMMAND")}
          </p>
        </>
      )}
    </Panel>
  );
}

export default memo(MissionStatus);
