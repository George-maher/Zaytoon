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
