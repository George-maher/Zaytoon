import { memo, useCallback, useEffect, useState } from "react";
import * as api from "../lib/api";

/**
 * VirtualMachinesView — the host's virtual machines, and what can be done to them.
 *
 * The backend could already drive VMware; there was no screen, so the capability
 * was reachable only by asking the assistant in chat. This is that screen.
 *
 * Three things it refuses to do, because each of them is how a VM tool ends up
 * being untrustworthy:
 *
 *  - **It does not invent a state.** A VM's state comes from the backend
 *    together with the observation that produced it, and the observation is
 *    shown next to the state. "Unknown" is displayed as unknown rather than
 *    dressed up as "stopped" — those look the same in a status column and mean
 *    completely different things.
 *
 *  - **It does not act in one click.** Every lifecycle verb previews first: the
 *    machine is resolved by name, the tier is read from the registry, and any
 *    warning is shown. The separate confirm button carries only the token, so
 *    the operation that runs is the operation that was displayed. A `dangerous`
 *    tier (a hard power-off) requires ticking an acknowledgement first, which is
 *    the difference between a confirmation and a formality.
 *
 *  - **It does not claim the guest is ready.** A running VM is not a booted
 *    guest, and the page says so where it would otherwise be tempting to imply
 *    otherwise.
 *
 * Guest command execution lives here too, behind the same two-stage gate, but
 * only when an account is actually configured — otherwise the panel shows the
 * prerequisite instead of an input box that cannot work.
 */

const ACTION_LABELS = {
  start: { en: "Start", ar: "تشغيل" },
  stop: { en: "Stop", ar: "إيقاف" },
  suspend: { en: "Suspend", ar: "تعليق" },
  resume: { en: "Resume", ar: "استئناف" },
  open_console: { en: "Open console", ar: "فتح الشاشة" },
};

const STATE_STYLE = {
  running: "var(--hud-green)",
  suspended: "var(--hud-amber)",
  stopped: "var(--hud-text-mute)",
  unavailable: "var(--hud-red)",
  unknown: "var(--hud-amber)",
};

function VirtualMachinesView({ language = "en" }) {
  const ar = language === "ar";
  const [hypervisors, setHypervisors] = useState({ hypervisors: [], any_available: false });
  const [guest, setGuest] = useState({ configured: false, problems: [] });
  const [vms, setVms] = useState([]);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  //: The preview awaiting an answer: {token, ref, action, tier, message, warnings}.
  const [pending, setPending] = useState(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);

  const [execTarget, setExecTarget] = useState("");
  const [command, setCommand] = useState("");
  const [execPending, setExecPending] = useState(null);
  const [execResult, setExecResult] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    const [hosts, machines, guestInfo] = await Promise.all([
      api.getHypervisors(),
      api.getVirtualMachines(),
      api.getGuestConfig(),
    ]);
    setHypervisors(hosts);
    setGuest(guestInfo);
    if (machines.success) {
      setVms(machines.vms || []);
      setError(null);
    } else {
      setVms([]);
      setError(machines.error || null);
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const preview = useCallback(
    async (ref, action, extra = {}) => {
      setBusy(true);
      setResult(null);
      setAcknowledged(false);
      const data = await api.previewVmAction(ref, action, extra);
      if (data.success === false) {
        setError(data.error);
        setPending(null);
      } else {
        setError(null);
        setPending(data);
      }
      setBusy(false);
    },
    [],
  );

  const confirm = useCallback(async () => {
    if (!pending) return;
    setBusy(true);
    const data = await api.confirmVmAction(pending.confirm, {
      ref: pending.params?.vm,
      action: pending.action,
    });
    setResult(data);
    setPending(null);
    setAcknowledged(false);
    setBusy(false);
    await load();
  }, [pending, load]);

  const runExec = useCallback(async () => {
    setBusy(true);
    setExecResult(null);
    const data = await api.previewGuestExec(command, { vm: execTarget || undefined });
    if (data.success === false) {
      setExecResult(data);
      setExecPending(null);
    } else if (data.requires_confirmation) {
      setExecPending(data);
    } else {
      setExecResult(data);
    }
    setBusy(false);
  }, [command, execTarget]);

  const confirmExec = useCallback(async () => {
    if (!execPending) return;
    setBusy(true);
    const data = await api.confirmGuestExec(execPending.confirm, command);
    setExecResult(data);
    setExecPending(null);
    setBusy(false);
  }, [execPending, command]);

  const vmware = (hypervisors.hypervisors || [])[0] || { available: false, reason: "" };

  return (
    <div
      className="flex-1 min-h-0 overflow-y-auto"
      style={{ padding: "var(--space-4)", display: "flex", flexDirection: "column", gap: "var(--space-4)" }}
    >
      <header>
        <h1 className="hud-label" style={{ fontSize: "var(--font-lg)" }}>
          {ar ? "الأنظمة الافتراضية" : "VIRTUAL MACHINES"}
        </h1>
        <p className="hud-micro" style={{ color: "var(--hud-text-mute)" }}>
          {ar
            ? "أمر التشغيل ليس دليلًا على إقلاع النظام، ومفتاح التشغيل ليس موافقة على تنفيذ الأوامر داخله."
            : "A running VM process is not boot evidence, and consent to power a machine on is not consent to run commands inside it."}
        </p>
      </header>

      {/* ---- Is a hypervisor actually here? ---- */}
      <section className="hud-panel">
        <header className="hud-panel-head">
          <span>{ar ? "برنامج التشغيل الافتراضي" : "HYPERVISOR"}</span>
          <span
            className="hud-micro ml-auto"
            style={{ color: vmware.available ? "var(--hud-green)" : "var(--hud-amber)" }}
          >
            {vmware.available
              ? (ar ? "متاح" : "AVAILABLE")
              : (ar ? "غير متاح" : "NOT AVAILABLE")}
          </span>
        </header>
        <div style={{ padding: "var(--space-3)" }}>
          <p className="hud-mono" dir="ltr" style={{ fontSize: "var(--font-xs)" }}>
            {vmware.label || "VMware Workstation"}
            {vmware.programs?.vmrun?.path ? ` · ${vmware.programs.vmrun.path}` : ""}
          </p>
          {!vmware.available && (
            <p role="alert" style={{ fontSize: "var(--font-xs)", color: "var(--hud-amber)", marginTop: "var(--space-2)" }}>
              {vmware.reason}
            </p>
          )}
          {vmware.available && vmware.platform_note && (
            <p className="hud-micro" style={{ color: "var(--hud-text-mute)" }}>
              {vmware.platform_note}
            </p>
          )}
        </div>
      </section>

      {error && (
        <p role="alert" className="hud-panel" style={{ padding: "var(--space-3)", color: "var(--hud-red)", fontSize: "var(--font-sm)" }}>
          {error}
        </p>
      )}

      {/* ---- The inventory ---- */}
      <section className="hud-panel">
        <header className="hud-panel-head">
          <span>{ar ? "الأنظمة المكتشفة" : "DISCOVERED MACHINES"}</span>
          <button
            type="button"
            onClick={load}
            disabled={busy}
            className="hud-micro ml-auto"
            style={{ padding: "var(--space-1) var(--space-2)", cursor: "pointer", background: "transparent", border: "var(--border-w) solid var(--hud-edge-faint)", color: "var(--hud-text-dim)" }}
          >
            {ar ? "إعادة الفحص" : "RESCAN"}
          </button>
        </header>
        {loading ? (
          <p className="hud-micro" style={{ padding: "var(--space-3)", color: "var(--hud-text-mute)" }}>
            {ar ? "جارٍ الفحص…" : "SCANNING…"}
          </p>
        ) : vms.length === 0 ? (
          <p className="hud-micro" style={{ padding: "var(--space-3)", color: "var(--hud-text-mute)" }}>
            {vmware.available
              ? (ar
                  ? "لا أنظمة معروفة. وجّه VM_INVENTORY_DIRS إلى المجلدات التي تحوي ملفات .vmx."
                  : "No machines known. Point VM_INVENTORY_DIRS at the directories holding the .vmx files.")
              : (ar
                  ? "لا يمكن سرد الأنظمة: لم يُعثر على vmrun، لذا الحالة غير معروفة — وليست «متوقفة»."
                  : "Nothing can be listed: vmrun was not found, so the state is unknown — not 'stopped'.")}
          </p>
        ) : (
          <ul
            aria-label={ar ? "الأنظمة المكتشفة" : "Discovered machines"}
            style={{ listStyle: "none", margin: 0, padding: 0 }}
          >
            {vms.map((vm) => (
              <li
                key={vm.path}
                style={{ borderTop: "var(--border-w) solid var(--hud-edge-faint)", padding: "var(--space-3)" }}
              >
                <div className="flex items-center flex-wrap" style={{ gap: "var(--space-2)" }}>
                  <span
                    aria-hidden="true"
                    className={vm.state === "running" ? "hud-hex hud-hex--ok" : "hud-hex hud-hex--hollow hud-hex--idle"}
                  />
                  <span style={{ fontSize: "var(--font-sm)", color: "var(--hud-text)" }}>{vm.name}</span>
                  <span
                    className="hud-micro"
                    style={{ color: STATE_STYLE[vm.state] || "var(--hud-text-mute)" }}
                  >
                    {(vm.state || "unknown").toUpperCase()}
                  </span>
                  {vm.guest_os && (
                    <span className="hud-micro" style={{ color: "var(--hud-text-mute)" }}>
                      guestOS={vm.guest_os}
                    </span>
                  )}
                </div>
                {/* The observation, not just the verdict. */}
                <p className="hud-micro hud-mono" dir="ltr" style={{ color: "var(--hud-text-mute)", fontSize: "var(--font-xs)" }}>
                  {vm.state_evidence}
                </p>
                <p className="hud-micro hud-mono" dir="ltr" style={{ color: "var(--hud-text-dim)", fontSize: "var(--font-xs)" }}>
                  {vm.path}
                </p>

                <div className="flex flex-wrap" style={{ gap: "var(--space-2)", marginTop: "var(--space-2)" }}>
                  {["start", "stop", "suspend", "resume", "open_console"].map((action) => {
                    // Hide the two verbs that cannot apply, so the row offers
                    // the operations that make sense rather than all five.
                    const impossible =
                      (action === "start" && vm.state === "running") ||
                      (action === "stop" && vm.state === "stopped") ||
                      (action === "resume" && vm.state !== "suspended") ||
                      (action === "suspend" && vm.state !== "running");
                    if (impossible) return null;
                    return (
                      <button
                        key={action}
                        type="button"
                        disabled={busy}
                        onClick={() => preview(vm.path, action)}
                        className="hud-micro"
                        style={{ padding: "var(--space-1) var(--space-2)", cursor: "pointer", background: "transparent", border: "var(--border-w) solid var(--hud-edge-faint)", color: "var(--hud-text-dim)" }}
                      >
                        {ar ? ACTION_LABELS[action].ar : ACTION_LABELS[action].en.toUpperCase()}
                      </button>
                    );
                  })}
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* ---- The preview, awaiting an answer ---- */}
      {pending && (
        <section
          className="hud-panel"
          style={{ padding: "var(--space-3)", borderColor: pending.dangerous ? "var(--hud-red)" : "var(--hud-edge-faint)" }}
        >
          <p className="hud-micro" style={{ color: pending.dangerous ? "var(--hud-red)" : "var(--hud-cyan)" }}>
            {ar ? "تأكيد مطلوب" : "CONFIRMATION REQUIRED"} · {pending.tier?.toUpperCase()}
          </p>
          <p style={{ fontSize: "var(--font-sm)", marginTop: "var(--space-2)" }}>{pending.message}</p>
          <p className="hud-micro hud-mono" dir="ltr" style={{ color: "var(--hud-text-mute)" }}>
            {pending.vm?.name} · {pending.vm?.path}
          </p>
          {(pending.warnings || []).map((w) => (
            <p key={w} role="alert" style={{ fontSize: "var(--font-xs)", color: "var(--hud-amber)", marginTop: "var(--space-2)" }}>
              {w}
            </p>
          ))}
          {pending.dangerous && (
            <label className="flex items-center" style={{ gap: "var(--space-2)", marginTop: "var(--space-2)", fontSize: "var(--font-xs)" }}>
              <input
                type="checkbox"
                checked={acknowledged}
                onChange={(e) => setAcknowledged(e.target.checked)}
              />
              <span>
                {ar
                  ? "أفهم أن هذا الإجراء خطير ولا يمكن التراجع عنه."
                  : "I understand this is a destructive action and cannot be undone."}
              </span>
            </label>
          )}
          <div className="flex" style={{ gap: "var(--space-2)", marginTop: "var(--space-3)" }}>
            <button
              type="button"
              onClick={confirm}
              disabled={busy || (pending.dangerous && !acknowledged)}
              className="hud-micro"
              style={{ padding: "var(--space-2) var(--space-3)", cursor: "pointer", background: "transparent", border: "var(--border-w) solid var(--hud-edge-faint)", color: pending.dangerous ? "var(--hud-red)" : "var(--hud-cyan)" }}
            >
              {ar ? "تنفيذ" : "CONFIRM"} · {pending.action}
            </button>
            <button
              type="button"
              onClick={() => {
                setPending(null);
                setAcknowledged(false);
              }}
              className="hud-micro"
              style={{ padding: "var(--space-2) var(--space-3)", cursor: "pointer", background: "transparent", border: "var(--border-w) solid var(--hud-edge-faint)", color: "var(--hud-text-dim)" }}
            >
              {ar ? "إلغاء" : "CANCEL"}
            </button>
          </div>
        </section>
      )}

      {result && (
        <section className="hud-panel" style={{ padding: "var(--space-3)" }}>
          <p className="hud-micro" style={{ color: result.success ? "var(--hud-green)" : "var(--hud-red)" }}>
            {result.success ? "DONE" : "FAILED"} · {result.action}
          </p>
          <pre
            className="hud-mono"
            dir="ltr"
            style={{ fontSize: "var(--font-xs)", whiteSpace: "pre-wrap", color: "var(--hud-text-dim)", marginTop: "var(--space-2)" }}
          >
            {result.output || result.error || ""}
          </pre>
        </section>
      )}

      {/* ---- Guest execution ---- */}
      <section className="hud-panel">
        <header className="hud-panel-head">
          <span>{ar ? "تنفيذ داخل النظام" : "RUN INSIDE A GUEST"}</span>
          <span
            className="hud-micro ml-auto"
            style={{ color: guest.configured ? "var(--hud-green)" : "var(--hud-amber)" }}
          >
            {guest.configured ? (ar ? "مُهيّأ" : "CONFIGURED") : (ar ? "غير مُهيّأ" : "NOT CONFIGURED")}
          </span>
        </header>
        <div style={{ padding: "var(--space-3)", display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
          <p className="hud-micro" style={{ color: "var(--hud-text-mute)", fontSize: "var(--font-xs)" }}>
            {guest.authentication || ""}
          </p>
          {(guest.problems || []).map((p) => (
            <p key={p} role="alert" style={{ fontSize: "var(--font-xs)", color: "var(--hud-amber)" }}>
              {p}
            </p>
          ))}

          {!guest.configured ? (
            <p className="hud-micro" style={{ color: "var(--hud-text-mute)" }}>
              {ar
                ? "يمكن تشغيل النظام وفتح شاشته، لكن تنفيذ الأوامر داخله يحتاج حسابًا ومفتاحًا."
                : "Zaytoon can start a machine and open its console, but running commands inside needs an account and a key."}
            </p>
          ) : (
            <>
              <label className="hud-micro" htmlFor="vm-exec-target">
                {ar ? "النظام" : "Machine"}
              </label>
              <select
                id="vm-exec-target"
                className="hud-input w-full"
                value={execTarget}
                onChange={(e) => setExecTarget(e.target.value)}
              >
                <option value="">{ar ? "— اختر —" : "— choose —"}</option>
                {vms.map((vm) => (
                  <option key={vm.path} value={vm.path}>
                    {vm.name}
                  </option>
                ))}
              </select>
              <label className="hud-micro" htmlFor="vm-exec-cmd">
                {ar ? "الأمر" : "Command"}
              </label>
              <input
                id="vm-exec-cmd"
                className="hud-input w-full"
                dir="ltr"
                value={command}
                onChange={(e) => setCommand(e.target.value)}
                placeholder="ip -brief addr"
              />
              <button
                type="button"
                onClick={runExec}
                disabled={busy || !command.trim()}
                className="hud-micro"
                style={{ alignSelf: "flex-start", padding: "var(--space-2) var(--space-3)", cursor: "pointer", background: "transparent", border: "var(--border-w) solid var(--hud-edge-faint)", color: "var(--hud-text-dim)" }}
              >
                {ar ? "مراجعة" : "REVIEW COMMAND"}
              </button>

              {execPending && (
                <div style={{ borderTop: "var(--border-w) solid var(--hud-edge-faint)", paddingTop: "var(--space-2)" }}>
                  <p className="hud-micro" style={{ color: "var(--hud-cyan)" }}>
                    {execPending.message}
                  </p>
                  {/* The exact command, because the approval is for this text. */}
                  <pre className="hud-mono" dir="ltr" style={{ fontSize: "var(--font-xs)", color: "var(--hud-text)", whiteSpace: "pre-wrap" }}>
                    {execPending.command}
                  </pre>
                  <p className="hud-micro" style={{ color: "var(--hud-text-mute)" }}>
                    {execPending.note}
                  </p>
                  <button
                    type="button"
                    onClick={confirmExec}
                    disabled={busy}
                    className="hud-micro"
                    style={{ marginTop: "var(--space-2)", padding: "var(--space-2) var(--space-3)", cursor: "pointer", background: "transparent", border: "var(--border-w) solid var(--hud-edge-faint)", color: "var(--hud-cyan)" }}
                  >
                    {ar ? "تشغيل داخل النظام" : "RUN IN GUEST"}
                  </button>
                </div>
              )}

              {execResult && (
                <div style={{ borderTop: "var(--border-w) solid var(--hud-edge-faint)", paddingTop: "var(--space-2)" }}>
                  {execResult.success === false ? (
                    <p role="alert" style={{ fontSize: "var(--font-xs)", color: "var(--hud-red)" }}>
                      {execResult.error}
                    </p>
                  ) : (
                    <>
                      <p className="hud-micro" style={{ color: (execResult.data?.exit_code ?? execResult.data?.returncode) === 0 ? "var(--hud-green)" : "var(--hud-amber)" }}>
                        exit {execResult.data?.exit_code ?? execResult.data?.returncode} · {execResult.data?.duration_s}s
                      </p>
                      <pre className="hud-mono" dir="ltr" style={{ fontSize: "var(--font-xs)", whiteSpace: "pre-wrap", color: "var(--hud-text-dim)" }}>
                        {execResult.data?.stdout || ""}
                        {execResult.data?.stderr || ""}
                      </pre>
                    </>
                  )}
                </div>
              )}
            </>
          )}
        </div>
      </section>
    </div>
  );
}

export default memo(VirtualMachinesView);
