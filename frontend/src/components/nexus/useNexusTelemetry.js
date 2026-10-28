/**
 * Nexus telemetry — one poller, two endpoints, honest dashes.
 *
 * The dashboard is a live panel, so it owns the one place the home screen
 * samples the backend: `GET /system` (what the machine is doing) every
 * SYSTEM_PERIOD_MS, and `GET /health` (what the backend is configured with)
 * every HEALTH_PERIOD_MS. Both endpoints may fail alone — the hook keeps
 * whichever answered and reports one flag, the same contract HUD/SystemStatus
 * documents. A missing value is passed through as `null`, and the UI renders
 * "—" for it; no layer in this screen fabricates a zero.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { getSystem, healthCheck } from "../../lib/api";

const SYSTEM_PERIOD_MS = 3000;
const HEALTH_PERIOD_MS = 10000;
const HISTORY = 40;

/** Push a numeric sample or undefined onto a fixed-length ring. */
function push(history, value) {
  const next = history.slice(-(HISTORY - 1));
  if (value !== undefined && value !== null) next.push(value);
  return next;
}

/** First defined numeric value of the known paths, else null. */
function num(...candidates) {
  for (const v of candidates) {
    if (typeof v === "number" && Number.isFinite(v)) return v;
  }
  return null;
}

/**
 * Reduce GET /system to the counters the dashboard draws.
 * Every key is either a finite number or null — the shape the panels render.
 */
export function reduceSystem(payload) {
  if (!payload || payload.error) return null;
  const res = payload.resources || {};
  return {
    cpu: num(res.cpu?.percent),
    cpuStatus: res.cpu?.status || null,
    memory: num(res.memory?.percent),
    disk: num(res.disk?.percent),
    // The backend reports network as `unavailable` with a reason — surfaces
    // the reason instead of pretending a counter exists.
    netStatus: res.network?.status || null,
    netReason: res.network?.reason || null,
    uptimeSeconds: num(payload.uptime_seconds),
    tasksTotal: num(payload.tasks?.total) ?? null,
    tasksActive: num(payload.tasks?.active) ?? null,
    processesTracked: num(payload.processes?.tracked) ?? null,
    processesRunning: Array.isArray(payload.processes?.running)
      ? payload.processes.running.length
      : null,
    terminalsOpen: num(payload.terminals?.open) ?? null,
    // Composite load index: the mean of every load counter that answered,
    // or null when none did. A derived score of real inputs, computed here so
    // the panels agree on it.
    load: (() => {
      const samples = [num(res.cpu?.percent), num(res.memory?.percent), num(res.disk?.percent)].filter(
        (v) => v !== null,
      );
      if (!samples.length) return null;
      return samples.reduce((a, b) => a + b, 0) / samples.length;
    })(),
  };
}

/** Reduce GET /health to the top-bar figures. */
export function reduceHealth(payload) {
  if (!payload || payload.error) return null;
  // tools/metrics.py's dict: started/completed/running/succeeded/failed...
  const metrics = payload.metrics?.tools || {};
  const llm = payload.llm || {};
  const llmMetrics = llm.metrics || {};
  return {
    version: typeof payload.version === "string" ? payload.version : null,
    status: payload.status || null,
    statusReason: payload.status_reason || null,
    safetyEnabled: payload.safety?.enabled === true,
    toolsLoaded: num(payload.tools_loaded),
    toolCalls: num(metrics.started, metrics.completed),
    toolFailures: num(metrics.failed),
    modelCalls: num(llmMetrics.total ?? llmMetrics.success),
    modelFails: num(llmMetrics.failed),
    // Duration averages vary by shape — flat attempts first, then the
    // per-model latency table, then "—".
    latencyMs: (() => {
      const table = llmMetrics.by_model_latency_ms;
      if (Number.isFinite(llmMetrics.avg_latency_ms)) return llmMetrics.avg_latency_ms;
      if (table && typeof table === "object") {
        const vals = Object.values(table).filter((v) => typeof v === "number");
        if (vals.length) return vals.reduce((a, b) => a + b, 0) / vals.length;
      }
      return null;
    })(),
    // Validity rate: successes over attempted completions. Without attempts
    // there is no rate — null is the honest answer, not 100%.
    successRate: (() => {
      const total = num(llmMetrics.total);
      const success = num(llmMetrics.success);
      if (total === null || success === null || total === 0) return null;
      return (success / total) * 100;
    })(),
  };
}

export default function useNexusTelemetry() {
  const [system, setSystem] = useState(null);
  const [systemError, setSystemError] = useState(null);
  const [health, setHealth] = useState(null);
  const [history, setHistory] = useState({
    cpu: [],
    memory: [],
    disk: [],
    load: [],
  });

  const sysRef = useRef(system);
  sysRef.current = system;

  const sample = useCallback((current, next) => {
    setHistory({
      cpu: push(next.cpuHistory, current.cpu),
      memory: push(next.memoryHistory, current.memory),
      disk: push(next.diskHistory, current.disk),
      load: push(next.loadHistory, current.load),
    });
  }, []);

  useEffect(() => {
    let live = true;
    const tick = async () => {
      const result = await getSystem();
      if (!live) return;
      if (result?.error) {
        setSystemError(result.error);
        return;
      }
      setSystemError(null);
      const next = reduceSystem(result);
      setSystem((prev) => next || prev || undefined);
      if (next) {
        setHistory((h) => sample(next, {
          cpuHistory: h.cpu,
          memoryHistory: h.memory,
          diskHistory: h.disk,
          loadHistory: h.load,
        }));
      }
    };
    tick();
    const id = setInterval(tick, SYSTEM_PERIOD_MS);
    return () => {
      live = false;
      clearInterval(id);
    };
  }, [sample]);

  useEffect(() => {
    let live = true;
    const tick = async () => {
      const result = await healthCheck();
      if (!live) return;
      if (result?.error) return; // keep the last good answer, one failure flag
      const next = reduceHealth(result);
      if (next) setHealth(next);
    };
    tick();
    const id = setInterval(tick, HEALTH_PERIOD_MS);
    return () => {
      live = false;
      clearInterval(id);
    };
  }, []);

  return { system, systemError, health, history };
}
