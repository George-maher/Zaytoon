const API_BASE = (() => {
  const port = window.location.port;
  // If served by nginx (Docker), use same origin (proxied)
  if (port === "3000" || port === "80" || port === "443" || !port) {
    return "";
  }
  // Dev mode: connect directly to backend
  return `http://${window.location.hostname}:8000`;
})();

/**
 * Reminders.
 *
 * These live at `/reminders`, not `/tasks`. They used to be at `/tasks`, which
 * now means agent tasks (`core/api/tasks_api.py`); the two concepts are
 * different enough that sharing a path would mean one of them is always
 * misnamed. Every call site was moved with them.
 */
export async function getReminders(status = "pending", sessionId = null) {
  // No session id by default: reminders outlive the conversation that made
  // them (the socket id changes every reconnect), so the panel asks for all
  // of them. Passing a sessionId filters deliberately.
  const params = new URLSearchParams({ status });
  if (sessionId) params.set("session_id", sessionId);
  const res = await fetch(`${API_BASE}/reminders?${params}`);
  if (!res.ok) throw new Error(`Reminders fetch failed: ${res.status}`);
  const data = await res.json();
  return data.tasks || data.reminders || [];
}

export async function createReminder({ task, time, recurring = "none", sessionId = "default" }) {
  const res = await fetch(`${API_BASE}/reminders`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task, time, recurring, session_id: sessionId }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || data.detail || `HTTP ${res.status}` };
  return data;
}

export async function deleteReminder(id) {
  const res = await fetch(`${API_BASE}/reminders/${encodeURIComponent(id)}`, { method: "DELETE" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}` };
  return { ok: true };
}

export async function completeReminder(id) {
  const res = await fetch(`${API_BASE}/reminders/${encodeURIComponent(id)}/done`, {
    method: "PATCH",
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}` };
  return { ok: true };
}

// ---------------------------------------------------------------------------
// The core runtime: tasks, projects, files, skills, models, terminal
// ---------------------------------------------------------------------------
//
// One rule across all of it: an error is returned as `{ error }`, never thrown
// as a silent failure, because every message here is operator-facing — "that is
// outside the workspace" or "no model can serve this request" — and paraphrasing
// it in the UI would lose the only actionable part.
//
// And one rule that matters more: **no function here returns a credential.**
// The backend never sends one (see `core/secrets.py`); a client that appeared to
// expect one would be a bug waiting to be reported as a leak.

/** @returns {Promise<{tasks: object[], count: number, active: number}>} */
export async function getTasks({ status, sessionId, projectId, limit = 50, logs = false } = {}) {
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  if (sessionId) params.set("session_id", sessionId);
  if (projectId) params.set("project_id", projectId);
  if (logs) params.set("logs", "true");
  params.set("limit", String(limit));

  const res = await fetch(`${API_BASE}/tasks?${params}`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { tasks: [], count: 0, active: 0, error: data.error || `HTTP ${res.status}` };
  return data;
}

export async function getTask(id) {
  const res = await fetch(`${API_BASE}/tasks/${encodeURIComponent(id)}`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}` };
  return data;
}

export async function cancelTask(id) {
  const res = await fetch(`${API_BASE}/tasks/${encodeURIComponent(id)}/cancel`, { method: "POST" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}` };
  return data;
}

export async function deleteTask(id) {
  const res = await fetch(`${API_BASE}/tasks/${encodeURIComponent(id)}`, { method: "DELETE" });
  if (!res.ok) return { error: `HTTP ${res.status}` };
  return { ok: true };
}

export async function getTaskStats() {
  const res = await fetch(`${API_BASE}/tasks/stats`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}` };
  return data;
}

/** The registered projects, pinned first. */
export async function getProjects() {
  const res = await fetch(`${API_BASE}/projects`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { projects: [], error: data.error || `HTTP ${res.status}` };
  return data;
}

export async function getRecentProjects(limit = 10) {
  const res = await fetch(`${API_BASE}/projects/recent?limit=${limit}`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { projects: [], error: data.error || `HTTP ${res.status}` };
  return data;
}

/**
 * Open a folder as a project.
 *
 * A path outside the workspace comes back as `{ error }` with the workspace
 * named in the message, and the UI shows it verbatim — the containment boundary
 * is the one thing a user cannot work around by asking, so they need to see
 * where it is.
 */
export async function openProject(path, { pinned = false } = {}) {
  const res = await fetch(`${API_BASE}/projects`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, pinned }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || data.detail || `HTTP ${res.status}` };
  return data;
}

/** Detect without registering — for the "what is this folder?" button. */
export async function detectProject(path) {
  const res = await fetch(`${API_BASE}/projects/detect`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}` };
  return data;
}

export async function pinProject(id, pinned = true) {
  const res = await fetch(`${API_BASE}/projects/${encodeURIComponent(id)}/pin`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pinned }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}` };
  return data;
}

/**
 * Correct a detected command. Only the keys present are sent, so fixing the
 * test command does not clear the run command.
 */
export async function setProjectCommands(id, commands) {
  const res = await fetch(`${API_BASE}/projects/${encodeURIComponent(id)}/commands`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(commands),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}` };
  return data;
}

/** What the assistant is told about a project. Shown so it can be corrected. */
export async function getProjectContext(id) {
  const res = await fetch(`${API_BASE}/projects/${encodeURIComponent(id)}/context`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}` };
  return data;
}

export async function closeProject(id) {
  const res = await fetch(`${API_BASE}/projects/${encodeURIComponent(id)}`, { method: "DELETE" });
  if (!res.ok) return { error: `HTTP ${res.status}` };
  return { ok: true };
}

// ---------------------------------------------------------------------------
// Files
// ---------------------------------------------------------------------------
export async function getFileRoot() {
  const res = await fetch(`${API_BASE}/files/root`);
  return res.json();
}

export async function getFileTree(path = ".", depth = 2) {
  const params = new URLSearchParams({ path, depth: String(depth) });
  const res = await fetch(`${API_BASE}/files/tree?${params}`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}`, entries: [] };
  return data;
}

export async function listDirectory(path = ".", showHidden = false) {
  const params = new URLSearchParams({ path, show_hidden: String(showHidden) });
  const res = await fetch(`${API_BASE}/files/list?${params}`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}`, entries: [] };
  return data;
}

export async function readFile(path) {
  const res = await fetch(`${API_BASE}/files/read?path=${encodeURIComponent(path)}`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}` };
  return data;
}

/**
 * Write a file. Returns `{ error }` rather than throwing: a 403 ("outside the
 * workspace") is expected user input, not an exceptional condition, and the
 * message says exactly what to fix.
 */
export async function writeFile(path, content, { backup = true } = {}) {
  const res = await fetch(`${API_BASE}/files/write`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, content, backup }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}` };
  return data;
}

/** Diff proposed content against the file, without writing. Powers change review. */
export async function diffFile(path, content) {
  const params = new URLSearchParams({ path, content });
  const res = await fetch(`${API_BASE}/files/diff?${params}`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}` };
  return data;
}

export async function searchFiles(q, { path = ".", glob, regex = false, namesOnly = false, limit = 100 } = {}) {
  const params = new URLSearchParams({ q, path, regex: String(regex), limit: String(limit) });
  if (glob) params.set("glob", glob);
  if (namesOnly) params.set("names_only", "true");

  const res = await fetch(`${API_BASE}/files/search?${params}`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}`, matches: [] };
  return data;
}

export async function statFile(path) {
  const res = await fetch(`${API_BASE}/files/stat?path=${encodeURIComponent(path)}`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}` };
  return data;
}

export async function createFolder(path) {
  const res = await fetch(`${API_BASE}/files/mkdir`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}` };
  return data;
}

export async function renameEntry(path, name) {
  const res = await fetch(`${API_BASE}/files/rename`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, name }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}` };
  return data;
}

export async function moveEntry(source, destination) {
  const res = await fetch(`${API_BASE}/files/move`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source, destination }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}` };
  return data;
}

export async function copyEntry(source, destination) {
  const res = await fetch(`${API_BASE}/files/copy`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source, destination }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}` };
  return data;
}

/** Destructive, so the UI confirms first — see the route's module docstring. */
export async function deleteEntry(path, { recursive = false } = {}) {
  const res = await fetch(`${API_BASE}/files/delete`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, recursive }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}` };
  return data;
}

// ---------------------------------------------------------------------------
// Git
// ---------------------------------------------------------------------------
//
// Errors are returned, not thrown, for the same reason as everywhere else here.
// But note what the status codes mean, because the panel has to render them
// differently: 403 is "outside the workspace" (a containment decision — do not
// offer a retry), 404 is "not a repository here" (hide the panel), 409 is "git
// refused" (show git's own words, it usually explains itself), 422 is a
// malformed request (a bug in the caller).
export async function getGitStatus(path = ".") {
  const res = await fetch(`${API_BASE}/git/status?path=${encodeURIComponent(path)}`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.detail || data.error || `HTTP ${res.status}` };
  return data;
}

export async function getGitLog(path = ".", limit = 20) {
  const params = new URLSearchParams({ path, limit: String(limit) });
  const res = await fetch(`${API_BASE}/git/log?${params}`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { commits: [], error: data.detail || data.error || `HTTP ${res.status}` };
  return data;
}

export async function getGitDiff(path = ".", { ref = "", staged = false, file = "" } = {}) {
  const params = new URLSearchParams({ path });
  if (ref) params.set("ref", ref);
  if (staged) params.set("staged", "true");
  if (file) params.set("file", file);
  const res = await fetch(`${API_BASE}/git/diff?${params}`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { diff: "", error: data.detail || data.error || `HTTP ${res.status}` };
  return data;
}

export async function getGitBranches(path = ".") {
  const res = await fetch(`${API_BASE}/git/branches?path=${encodeURIComponent(path)}`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { branches: [], error: data.detail || data.error || `HTTP ${res.status}` };
  return data;
}

export async function gitAdd(files, { all = false, path = "." } = {}) {
  const res = await fetch(`${API_BASE}/git/add`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ files, all, path }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.detail || data.error || `HTTP ${res.status}` };
  return data;
}

export async function gitCommit(message, { path = "." } = {}) {
  const res = await fetch(`${API_BASE}/git/commit`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, path }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.detail || data.error || `HTTP ${res.status}` };
  return data;
}

export async function gitCheckout(ref, { create = false, path = "." } = {}) {
  const res = await fetch(`${API_BASE}/git/checkout`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ref, create, path }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.detail || data.error || `HTTP ${res.status}` };
  return data;
}

export async function gitPush({ remote = "origin", branch = "", path = "." } = {}) {
  const res = await fetch(`${API_BASE}/git/push`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ remote, branch, path }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.detail || data.error || `HTTP ${res.status}` };
  return data;
}

export async function gitFetch({ remote = "", path = "." } = {}) {
  const res = await fetch(`${API_BASE}/git/fetch`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ remote, path }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.detail || data.error || `HTTP ${res.status}` };
  return data;
}

// Pull fast-forwards by default, so a diverged branch comes back as a 409
// with git's own words rather than an implicit merge commit. The panel shows
// that message and then offers an explicit merge (`ff_only: false`).
export async function gitPull({ remote = "", branch = "", ffOnly = true, path = "." } = {}) {
  const res = await fetch(`${API_BASE}/git/pull`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ remote, branch, ff_only: ffOnly, path }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.detail || data.error || `HTTP ${res.status}` };
  return data;
}

export async function gitMerge(branch, { message = "", path = "." } = {}) {
  const res = await fetch(`${API_BASE}/git/merge`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ branch, message, path }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.detail || data.error || `HTTP ${res.status}` };
  return data;
}

// `staged: true` unstages (reversible). `staged: false` discards working-tree
// changes (not reversible) — the caller must have confirmed with the user.
export async function gitRestore(files, { staged = false, path = "." } = {}) {
  const res = await fetch(`${API_BASE}/git/restore`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ files, staged, path }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.detail || data.error || `HTTP ${res.status}` };
  return data;
}

// ---------------------------------------------------------------------------
// Terminal
// ---------------------------------------------------------------------------
export async function getTerminals() {
  const res = await fetch(`${API_BASE}/terminal`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { sessions: [], error: data.error || `HTTP ${res.status}` };
  return data;
}

/**
 * Open a session. A 403 means the sandbox refused the command — chaining,
 * metacharacters — which is a policy decision rather than a typo, so the UI
 * says so rather than inviting a retry.
 */
export async function openTerminal(command, { cwd = "", name = "", env = null, timeout = null } = {}) {
  const payload = { command, cwd, name };
  if (env && Object.keys(env).length) payload.env = env;
  if (timeout != null) payload.timeout = timeout;
  const res = await fetch(`${API_BASE}/terminal`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || data.detail || `HTTP ${res.status}` };
  return data;
}

export async function getTerminalOutput(id, tail = 200) {
  const res = await fetch(`${API_BASE}/terminal/${encodeURIComponent(id)}?tail=${tail}`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}`, lines: [] };
  return data;
}

export async function writeTerminal(id, data) {
  const res = await fetch(`${API_BASE}/terminal/${encodeURIComponent(id)}/input`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ data }),
  });
  const payload = await res.json().catch(() => ({}));
  if (!res.ok) return { error: payload.error || `HTTP ${res.status}` };
  return payload;
}

export async function closeTerminal(id) {
  const res = await fetch(`${API_BASE}/terminal/${encodeURIComponent(id)}`, { method: "DELETE" });
  if (!res.ok) return { error: `HTTP ${res.status}` };
  return { ok: true };
}

// ---------------------------------------------------------------------------
// Skills
// ---------------------------------------------------------------------------
export async function getSkills() {
  const res = await fetch(`${API_BASE}/skills`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { skills: [], errors: [], error: data.error || `HTTP ${res.status}` };
  return data;
}

export async function getSkill(name) {
  const res = await fetch(`${API_BASE}/skills/${encodeURIComponent(name)}`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}` };
  return data;
}

/** Create a skill. It lands **disabled** — writing one is not activating it. */
export async function createSkill(name, { description = "", instructions = "" } = {}) {
  const res = await fetch(`${API_BASE}/skills`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, description, instructions }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}` };
  return data;
}

export async function installSkill(source, { overwrite = false } = {}) {
  const res = await fetch(`${API_BASE}/skills/install`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source, overwrite }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}` };
  return data;
}

/** A 409 means the enabled-skills limit is reached — disable one first. */
export async function setSkillEnabled(name, enabled = true) {
  const res = await fetch(`${API_BASE}/skills/${encodeURIComponent(name)}/enable`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}` };
  return data;
}

export async function removeSkill(name) {
  const res = await fetch(`${API_BASE}/skills/${encodeURIComponent(name)}`, { method: "DELETE" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}` };
  return data;
}

// ---------------------------------------------------------------------------
// Providers and models
// ---------------------------------------------------------------------------
/**
 * Providers. Each carries `has_api_key` and **no key material** — the backend
 * has no route that returns one, so there is nothing here to leak.
 */
export async function getProviders() {
  const res = await fetch(`${API_BASE}/providers`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { providers: [], error: data.error || `HTTP ${res.status}` };
  return data;
}

export async function saveProvider(payload) {
  const res = await fetch(`${API_BASE}/providers`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}` };
  return data;
}

export async function deleteProvider(id) {
  const res = await fetch(`${API_BASE}/providers/${encodeURIComponent(id)}`, { method: "DELETE" });
  if (!res.ok) return { error: `HTTP ${res.status}` };
  return { ok: true };
}

/** Reachability probe. Reports `connected: false` with a reason, never throws. */
export async function testProvider(id) {
  const res = await fetch(`${API_BASE}/providers/${encodeURIComponent(id)}/test`, { method: "POST" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { connected: false, error: data.error || `HTTP ${res.status}`, models: [] };
  return data;
}

/** Ask an endpoint what it has and record the result. Explicit, never on a timer. */
export async function refreshModels(providerId) {
  const res = await fetch(`${API_BASE}/providers/${encodeURIComponent(providerId)}/models/refresh`, {
    method: "POST",
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}` };
  return data;
}

export async function getModels({ providerId, capabilities, includeDisabled = false } = {}) {
  const params = new URLSearchParams();
  if (providerId) params.set("provider_id", providerId);
  if (capabilities) params.set("capabilities", capabilities);
  if (includeDisabled) params.set("include_disabled", "true");

  const res = await fetch(`${API_BASE}/models?${params}`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { models: [], capabilities: [], error: data.error || `HTTP ${res.status}` };
  return data;
}

export async function saveModel(payload) {
  const res = await fetch(`${API_BASE}/models`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}` };
  return data;
}

export async function deleteModel(providerId, modelId) {
  const res = await fetch(
    `${API_BASE}/models/${encodeURIComponent(providerId)}/${encodeURIComponent(modelId)}`,
    { method: "DELETE" },
  );
  if (!res.ok) return { error: `HTTP ${res.status}` };
  return { ok: true };
}

/**
 * What the router would choose, and why every other candidate was skipped.
 * This is what makes "why did it pick that model?" answerable in the UI.
 */
export async function getRoutingPreview({ tier = "fast", capabilities, providerId, forcedModel } = {}) {
  const params = new URLSearchParams({ tier });
  if (capabilities) params.set("capabilities", capabilities);
  if (providerId) params.set("provider_id", providerId);
  if (forcedModel) params.set("forced_model", forcedModel);

  const res = await fetch(`${API_BASE}/models/routing?${params}`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { candidates: [], error: data.error || `HTTP ${res.status}` };
  return data;
}

/**
 * The pinned model per tier — what the router must use for `fast`,
 * `coding`, `reasoning`, and the `default` catch-all. `null` means "not
 * pinned; priority decides", which is the default and the common case.
 *
 * `available` lists the tier names the backend knows, so a UI never has to
 * hard-code them; when a backend predates the route the caller gets
 * `{ tiers: {}, available: [] }` and shows nothing rather than an error —
 * an unpinned router works exactly as it did before.
 */
export async function getTierAssignments() {
  const res = await fetch(`${API_BASE}/models/tiers`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { tiers: {}, available: [], error: data.error || `HTTP ${res.status}` };
  return data;
}

/** Pin (model given) or clear (both ids omitted) a tier. `{ error }` on refusal. */
export async function setTierAssignment(tier, providerId, modelId) {
  const payload = { tier };
  if (providerId) payload.provider_id = providerId;
  if (modelId) payload.model_id = modelId;

  const res = await fetch(`${API_BASE}/models/tiers`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || data.detail || `HTTP ${res.status}` };
  return data;
}

// ---------------------------------------------------------------------------
// Permissions and system
// ---------------------------------------------------------------------------
export async function getPermissions() {
  const res = await fetch(`${API_BASE}/permissions`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { vocabulary: [], grants: {}, tools: [], error: data.error || `HTTP ${res.status}` };
  return data;
}

export async function setPermission(permission, decision) {
  const res = await fetch(`${API_BASE}/permissions/${encodeURIComponent(permission)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}` };
  return data;
}

export async function getSystem() {
  const res = await fetch(`${API_BASE}/system`);
  return res.json();
}

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
 * Start pulling a model — explicit, one at a time, never automatic.
 *
 * Poll `getPullStatus` for progress. The backend refuses a second pull of a
 * running model (409) and every malformed name (400), so `{ error }` here is
 * a real answer, not a transport failure to retry.
 */
export async function pullModel(model) {
  const res = await fetch(`${API_BASE}/settings/ai/pull`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || data.detail || `HTTP ${res.status}` };
  return data;
}

/** Poll one pull by model, or list every pull this process has run. */
export async function getPullStatus(model = "") {
  const params = model ? `?model=${encodeURIComponent(model)}` : "";
  const res = await fetch(`${API_BASE}/settings/ai/pull${params}`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || data.detail || `HTTP ${res.status}` };
  return data;
}

/**
 * Measure an **installed** model's capabilities and record them in the
 * registry. The probe sends test prompts — it never downloads anything, and
 * a model that is not installed comes back as "pull it first", the one
 * instruction that helps. Returns `{ error }` on refusal.
 */
export async function probeModel(model, providerId) {
  const payload = { model };
  if (providerId) payload.provider_id = providerId;

  const res = await fetch(`${API_BASE}/settings/ai/probe`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || data.detail || `HTTP ${res.status}` };
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
