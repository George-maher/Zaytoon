const API_BASE = (() => {
  const port = window.location.port;
  // If served by nginx (Docker), use same origin (proxied)
  if (port === "3000" || port === "80" || port === "443" || !port) {
    return "";
  }
  // Dev mode: connect directly to backend
  return `http://${window.location.hostname}:8000`;
})();

export async function healthCheck() {
  const res = await fetch(`${API_BASE}/health`);
  if (!res.ok) throw new Error(`Health check failed: ${res.status}`);
  return res.json();
}

export async function sendMessage(message, lang = "en") {
  const res = await fetch(`${API_BASE}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, lang }),
  });
  if (!res.ok) throw new Error(`Chat failed: ${res.status}`);
  return res.json();
}

export async function speechToText(audioBlob, language = null) {
  const form = new FormData();
  form.append("file", audioBlob, "audio.webm");
  if (language) form.append("language", language);
  const res = await fetch(`${API_BASE}/stt`, { method: "POST", body: form });
  if (!res.ok) throw new Error(`STT failed: ${res.status}`);
  return res.json();
}

export async function textToSpeech(text, lang = "en") {
  const res = await fetch(`${API_BASE}/tts`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, lang }),
  });
  if (!res.ok) throw new Error(`TTS failed: ${res.status}`);
  return res.blob();
}

export async function openApp(appName) {
  const res = await fetch(`${API_BASE}/command/open-app`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ app_name: appName }),
  });
  if (!res.ok) throw new Error(`Open app failed: ${res.status}`);
  return res.json();
}

export async function runCommand(command, params = {}) {
  const res = await fetch(`${API_BASE}/command/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ command, ...params }),
  });
  if (!res.ok) throw new Error(`Command failed: ${res.status}`);
  return res.json();
}

/**
 * The directory every file tool is confined to.
 *
 * This matters more than it looks: containment is `commonpath`-based against
 * this root, so anything outside it is refused. It is NOT the process working
 * directory — the backend runs from `backend/`, so a cwd-based root confined
 * the assistant to its own source tree.
 */
export async function getWorkspace() {
  const res = await fetch(`${API_BASE}/settings/workspace`);
  if (!res.ok) throw new Error(`Workspace fetch failed: ${res.status}`);
  return res.json();
}

/**
 * The AI provider configuration (Ollama locally, OpenRouter in the cloud).
 *
 * Three endpoints, one rule: an error is returned as text, never thrown as a
 * silent failure. The backend's messages are operator-facing instructions --
 * a missing local model arrives as "MODEL NOT INSTALLED ... `ollama pull x`"
 * -- and paraphrasing them in the UI would lose the only actionable part.
 */
export async function getAiSettings() {
  const res = await fetch(`${API_BASE}/settings/ai`);
  if (!res.ok) throw new Error(`AI settings fetch failed: ${res.status}`);
  return res.json();
}

/** Save. Returns `{ error }` on rejection, like `setWorkspace`. */
export async function setAiSettings(payload) {
  const res = await fetch(`${API_BASE}/settings/ai`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || data.detail || `HTTP ${res.status}` };
  return data;
}

/**
 * Probe the connection without saving anything.
 *
 * Deliberately free of side effects: it is the button a user presses before
 * committing a configuration, and it must never be the thing that changes it.
 */
export async function validateAiSettings(payload = {}) {
  const res = await fetch(`${API_BASE}/settings/ai/validate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    return { connected: false, error: data.error || data.detail || `HTTP ${res.status}` };
  }
  return data;
}

/**
 * Repoint the assistant at a different working directory.
 *
 * The backend rejects a path it cannot create rather than silently accepting
 * it, so the UI never shows a workspace the tools are not actually using.
 * Returns `{ error }` on rejection instead of throwing, because a bad path is
 * expected user input rather than an exceptional condition.
 */
export async function setWorkspace(root) {
  const res = await fetch(`${API_BASE}/settings/workspace`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ root }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}` };
  return data;
}

/**
 * Execution audit log.
 *
 * `GET /audit?limit=N` → `{ entries, total }`, `DELETE /audit` → `{ cleared }`.
 *
 * The endpoint is optional: a backend that has not shipped it answers 404,
 * which is reported as `{ unavailable: true }` and rendered as "Audit log
 * unavailable" — *not* as a fetch error, and not as an empty log that looks
 * like "nothing ran". Anything else still throws, because a 500 is a real
 * failure and hiding it would be the dishonest path.
 */
export async function getAudit(limit = 100) {
  const res = await fetch(`${API_BASE}/audit?limit=${encodeURIComponent(limit)}`);
  if (res.status === 404) return { unavailable: true, entries: [], total: 0 };
  if (!res.ok) throw new Error(`Audit fetch failed: ${res.status}`);
  const data = await res.json().catch(() => ({}));
  const entries = Array.isArray(data.entries) ? data.entries : [];
  return {
    unavailable: false,
    entries,
    total: typeof data.total === "number" ? data.total : entries.length,
  };
}

/** Wipe the audit log. Same 404 contract as `getAudit`. */
export async function clearAudit() {
  const res = await fetch(`${API_BASE}/audit`, { method: "DELETE" });
  if (res.status === 404) return { unavailable: true, cleared: 0 };
  if (!res.ok) throw new Error(`Audit clear failed: ${res.status}`);
  const data = await res.json().catch(() => ({}));
  return { unavailable: false, cleared: typeof data.cleared === "number" ? data.cleared : 0 };
}

/**
 * Processes the backend is running or has run.
 * `GET /processes` → `{ processes: [...] }`, optional endpoint (404 → empty
 * + `unavailable`, so the card can simply not render).
 */
export async function getProcesses() {
  const res = await fetch(`${API_BASE}/processes`);
  if (res.status === 404) return { unavailable: true, processes: [] };
  if (!res.ok) throw new Error(`Processes fetch failed: ${res.status}`);
  const data = await res.json().catch(() => ({}));
  return {
    unavailable: false,
    processes: Array.isArray(data.processes) ? data.processes : [],
  };
}

/** `POST /processes/{id}/stop` → `{ stopped }`. */
export async function stopProcess(id) {
  const res = await fetch(`${API_BASE}/processes/${encodeURIComponent(id)}/stop`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
  if (res.status === 404) return { unavailable: true, stopped: false };
  if (!res.ok) throw new Error(`Process stop failed: ${res.status}`);
  const data = await res.json().catch(() => ({}));
  return { unavailable: false, stopped: Boolean(data.stopped) };
}
