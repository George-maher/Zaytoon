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
  if (!res.ok) {
    // `status` rides along on the failure and on nothing else: the panel
    // cannot tell "there is no repository here" (404) from a containment
    // refusal (403) or git's own answer (409) without it, and treating them
    // alike means one of them gets rendered as a confident falsehood.
    return {
      error: data.detail || data.error || `HTTP ${res.status}`,
      status: res.status,
    };
  }
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

export async function gitCommit(message, { confirm = null, path = "." } = {}) {
  const res = await fetch(`${API_BASE}/git/commit`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    // `confirm` is the server's preview token: the second call of the two-step
    // approval every consequential verb here performs. Null on the first call,
    // which answers with `{requires_confirmation, confirm, summary}` instead of
    // an effect — see GitPanel's confirmation row.
    body: JSON.stringify({ message, path, confirm: confirm || undefined }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.detail || data.error || `HTTP ${res.status}` };
  return data;
}

export async function gitCheckout(ref, { create = false, confirm = null, path = "." } = {}) {
  const res = await fetch(`${API_BASE}/git/checkout`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ref, create, path, confirm: confirm || undefined }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.detail || data.error || `HTTP ${res.status}` };
  return data;
}

export async function gitPush({ remote = "origin", branch = "", confirm = null, path = "." } = {}) {
  const res = await fetch(`${API_BASE}/git/push`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ remote, branch, path, confirm: confirm || undefined }),
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

export async function gitMerge(branch, { message = "", confirm = null, path = "." } = {}) {
  const res = await fetch(`${API_BASE}/git/merge`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ branch, message, path, confirm: confirm || undefined }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.detail || data.error || `HTTP ${res.status}` };
  return data;
}

// `staged: true` unstages (reversible). `staged: false` discards working-tree
// changes (not reversible) — the server gates only the discard behind the
// two-step approval, so a plain unstage call needs no `confirm`.
export async function gitRestore(files, { staged = false, confirm = null, path = "." } = {}) {
  const res = await fetch(`${API_BASE}/git/restore`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ files, staged, path, confirm: confirm || undefined }),
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
 * Open a session — in two steps without `confirm`: the first call answers
 * with `{requires_confirmation, confirm, summary}` (nothing has run) and the
 * caller must show that preview and re-call with the token. A 403 means the
 * sandbox refused the command — chaining, metacharacters — which is a policy
 * decision rather than a typo, so the UI says so rather than inviting a
 * retry.
 */
export async function openTerminal(
  command,
  { cwd = "", name = "", env = null, timeout = null, confirm = null } = {},
) {
  const payload = { command, cwd, name };
  if (env && Object.keys(env).length) payload.env = env;
  if (timeout != null) payload.timeout = timeout;
  if (confirm) payload.confirm = confirm;
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

/**
 * The permission vocabulary a skill manifest may declare.
 *
 * Served by the backend rather than hard-coded here: the manifest validator
 * rejects an unknown name, so a second copy in the client is a list that drifts
 * into offering the user a permission the backend will refuse.
 */
export async function getSkillPermissions() {
  const res = await fetch(`${API_BASE}/skills/permissions`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { permissions: [], max_enabled: 0, error: data.error || `HTTP ${res.status}` };
  return data;
}

/**
 * Create a skill. It lands **disabled** — writing one is not activating it.
 *
 * `permissions` is what the skill *declares* it wants; the backend validates it
 * against the vocabulary above and refuses an unknown name. Declaring is not
 * granting: enforcement stays in the tool registry's gate.
 */
export async function createSkill(
  name,
  { description = "", instructions = "", permissions = [], tags = [] } = {},
) {
  const res = await fetch(`${API_BASE}/skills`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, description, instructions, permissions, tags }),
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

/**
 * Dry-run a skill: validate manifest, import tool modules, run policy check.
 *
 * The skill does not need to be enabled — testing a skill you haven't switched
 * on is the point. The route answers 200 with `ok: false` rather than an
 * HTTP error: a skill that fails its test is a *result to display*, not a
 * broken request. The response shape mirrors the backend's `test_skill`:
 * {ok, manifest_ok, instructions_ok, tools_ok, enabled, modules, tools,
 *  policy, errors, warnings}.
 */
export async function testSkill(name) {
  const res = await fetch(`${API_BASE}/skills/${encodeURIComponent(name)}/test`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { ok: false, error: data.error || `HTTP ${res.status}` };
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

/**
 * Generate one real completion — the only acceptable proof of setup.
 *
 * `POST /test` proves an endpoint answers; this proves a model can actually
 * be asked something and reply, which is why the first-run wizard completes
 * on this and nothing else. A wrong key scope, a missing model and an
 * exhausted balance each look exactly like success until a completion is
 * attempted.
 *
 * A failure is a 200 with `ok: false` and the reason — the endpoint being
 * broken is the answer, not a server error. The 409 ("no models recorded,
 * refresh first") and 404 ("no such provider") cases carry their own text in
 * `{ error }`; both come through verbatim so the UI can show the instruction
 * rather than a status code.
 */
export async function testChatProvider(id, { message = "", model = "" } = {}) {
  const res = await fetch(`${API_BASE}/providers/${encodeURIComponent(id)}/test-chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, model }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { ok: false, connected: false, error: data.error || `HTTP ${res.status}` };
  return data;
}

/**
 * Is first-run setup done? Answered only by a real chat test.
 *
 * `setup_required: null` on failure, never `true`: this status is read on
 * every boot, and a backend that predates the route (404) or is simply down
 * must not be rendered as "setup required" — an unreadable status is not
 * evidence that setup is missing. The wizard and the setup banner both skip
 * entirely on `null` and let the user discover the real problem where it is
 * reported.
 */
export async function getSetupStatus() {
  const res = await fetch(`${API_BASE}/setup/status`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { setup_required: null, error: data.error || `HTTP ${res.status}` };
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

/**
 * The applications this build may launch, as display metadata.
 *
 * `GET /computer/apps` → `{ apps, categories, total, resolved, ... }`. The
 * backend owns the list because it *is* the allow-list
 * `POST /command/open-app` enforces; a copy kept here would drift the first
 * time someone edited one of them, and a grid that offers a name the launcher
 * refuses is a row of buttons that fail.
 *
 * `{ resolve: true }` additionally asks for `installed` / `via` per app —
 * whether this machine has **positive evidence** of it. Two properties worth
 * keeping, because both fail silently:
 *
 * - the flag is explicit rather than defaulted, so the filesystem walk behind
 *   it is a cost someone has to ask for at every call site;
 * - the backend tags its answer `resolved: true` only when it actually
 *   computed one. `resolved: false` after a `resolve: true` request is "I did
 *   not find out" (`resolveError` says why), never a quiet fallback to
 *   silence — so this never has to guess whether the verdicts are present.
 *
 * `installed: false` is *not* a claim that an application is missing; see
 * `resolve_app_detail`. A portable copy with no Start Menu entry stays
 * invisible, and the UI says so rather than calling it absent.
 *
 * A 404 means the backend predates the route: report it rather than throw, so
 * the Computer view can say so plainly instead of showing an empty grid.
 */
export async function getComputerApps({ resolve: resolveAvailability = false } = {}) {
  const suffix = resolveAvailability ? "?resolve=true" : "";
  const res = await fetch(`${API_BASE}/computer/apps${suffix}`);
  if (res.status === 404) return { unavailable: true, apps: [], categories: [] };
  if (!res.ok) throw new Error(`Apps fetch failed: ${res.status}`);
  const data = await res.json().catch(() => ({}));
  return {
    unavailable: false,
    apps: Array.isArray(data.apps) ? data.apps : [],
    categories: Array.isArray(data.categories) ? data.categories : [],
    total: typeof data.total === "number" ? data.total : 0,
    resolved: data.resolved === true,
    found: typeof data.found === "number" ? data.found : 0,
    resolveError: typeof data.resolve_error === "string" ? data.resolve_error : null,
  };
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

/**
 * The last lines a tracked process printed.
 *
 * `GET /processes/{id}/logs?tail=N` → `{ id, status, lines }`. This is the
 * one computer action with no UI at all until now: the endpoint existed, the
 * processes were captured, and there was no way to read either — so "start a
 * dev server and watch it" ended at "the process is running".
 *
 * Only processes Zaytoon started can be queried (same ownership rule as
 * `stop`), and a 404 therefore means "unknown id" as often as "no such
 * route": both are reported rather than thrown, because a missing tail of
 * output must not break the panel showing the process itself.
 */
export async function getProcessLogs(id, tail = 40) {
  const n = Number.isFinite(Number(tail)) && Number(tail) > 0 ? Number(tail) : 40;
  const res = await fetch(
    `${API_BASE}/processes/${encodeURIComponent(id)}/logs?tail=${Math.min(n, 500)}`,
  );
  if (res.status === 404) return { unavailable: true, lines: [] };
  if (!res.ok) throw new Error(`Process logs failed: ${res.status}`);
  const data = await res.json().catch(() => ({}));
  // The route answers `200 {"error": ...}` for an id it does not know — a
  // refusal, not a successful read of an empty log. Reading `lines` alone
  // would render "No output captured", which claims a quiet process rather
  // than an unknown one.
  if (typeof data.error === "string" && data.error) {
    return { unavailable: true, lines: [], error: data.error };
  }
  return {
    unavailable: false,
    id: data.id ?? id,
    status: typeof data.status === "string" ? data.status : "",
    lines: Array.isArray(data.lines) ? data.lines : [],
  };
}

// ---------------------------------------------------------------------------
// Security workspace — target authorisations and the authorised probe
// ---------------------------------------------------------------------------
export async function getSecurityAuthorizations() {
  const res = await fetch(`${API_BASE}/security/authorizations`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    return { authorizations: [], operations: [], active_count: 0, error: data.error || `HTTP ${res.status}` };
  }
  return data;
}

/**
 * Record a target authorisation.
 *
 * A 422 means the pattern was refused — a whole-internet wildcard, a public
 * suffix, a single label. The message names the reason and is shown verbatim.
 */
export async function authorizeSecurityTarget({ targets, operations, ttl_days = null, note = "" }) {
  const res = await fetch(`${API_BASE}/security/authorizations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ targets, operations, ttl_days, note }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}` };
  return data;
}

export async function revokeSecurityAuthorization(id) {
  const res = await fetch(`${API_BASE}/security/authorizations/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.error || `HTTP ${res.status}` };
  return data;
}

/**
 * Run one non-destructive probe.
 *
 * Two steps, because `security_probe` is a `confirm`-tier tool and the approval
 * has to be real. The first call opens no socket at all and returns a token
 * plus what the probe *would* do; the second carries the user's answer. Scope
 * refusal comes back as `{refused: true, message}` rather than an error, since
 * that is the control working as intended and not a failure.
 */
export async function runSecurityProbe(url, { includeBody = false } = {}) {
  const first = await fetch(`${API_BASE}/security/probe`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url, include_body: includeBody }),
  });
  const preview = await first.json().catch(() => ({}));
  if (!first.ok) return { error: preview.error || `HTTP ${first.status}` };
  if (preview.refused) return preview;
  if (!preview.confirm) return { error: "The backend did not return a confirmation token." };

  const second = await fetch(`${API_BASE}/security/probe`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url, include_body: includeBody, confirm: preview.confirm }),
  });
  const result = await second.json().catch(() => ({}));
  if (!second.ok) return { error: result.error || `HTTP ${second.status}` };
  if (result.success === false && result.error && !result.data) return { error: result.error };
  const data = result.data || {};
  return {
    output: result.output || "",
    status: data.status,
    findings: data.findings || [],
    authorization_id: data.authorization_id || preview.authorization_id,
    authorized_target: data.authorized_target || preview.authorized_target,
    error: result.error,
  };
}

// ---------------------------------------------------------------------------
// Virtual machines — VMware discovery, inventory and lifecycle
// ---------------------------------------------------------------------------
// Every lifecycle call is two-stage on purpose. The first request resolves the
// machine and describes what would happen, opening nothing; the second carries
// only the token, and the backend runs the operation that was previewed. So the
// page shows the user a *named* machine and a specific action, and the approve
// click is for that and nothing else.
export async function getHypervisors() {
  const res = await fetch(`${API_BASE}/virtualization/hypervisors`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { hypervisors: [], any_available: false, error: `HTTP ${res.status}` };
  return data;
}

export async function getVirtualMachines(runningOnly = false) {
  const params = runningOnly ? "?running_only=true" : "";
  const res = await fetch(`${API_BASE}/virtualization/vms${params}`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { vms: [], count: 0, success: false, error: `HTTP ${res.status}` };
  return data;
}

export async function getVirtualMachine(ref) {
  const res = await fetch(
    `${API_BASE}/virtualization/vms/one?ref=${encodeURIComponent(ref)}`,
  );
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { success: false, error: data.error || `HTTP ${res.status}` };
  return data;
}

export async function getGuestConfig() {
  const res = await fetch(`${API_BASE}/virtualization/guest`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { configured: false, problems: [], error: `HTTP ${res.status}` };
  return data;
}

/**
 * Step 1 — what would happen, to nothing.
 *
 * Returns the resolved machine, the tool's tier and any warnings. A refusal
 * (an unknown or ambiguous name) comes back as `{success: false, error}` and is
 * shown as-is: the backend's message lists the candidates, which is the only
 * useful thing to say when a name did not identify one machine.
 */
export async function previewVmAction(ref, action, { mode, gui, waitForReady } = {}) {
  const body = { ref, action };
  if (mode) body.mode = mode;
  if (gui !== undefined) body.gui = gui;
  if (waitForReady !== undefined) body.wait_for_ready = waitForReady;
  const res = await fetch(`${API_BASE}/virtualization/vms/action`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { success: false, error: data.error || `HTTP ${res.status}` };
  return data;
}

/** Step 2 — carry the user's answer. The operation comes from the token. */
export async function confirmVmAction(token, { ref, action } = {}) {
  const res = await fetch(`${API_BASE}/virtualization/vms/action`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirm: token, ref, action }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { success: false, error: data.error || `HTTP ${res.status}` };
  return data;
}

/** Step 1 of running a command in a guest. */
export async function previewGuestExec(command, { vm, host, timeout } = {}) {
  const body = { command };
  if (vm) body.vm = vm;
  if (host) body.host = host;
  if (timeout) body.timeout = timeout;
  const res = await fetch(`${API_BASE}/virtualization/guest/exec`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { success: false, error: data.error || `HTTP ${res.status}` };
  return data;
}

export async function confirmGuestExec(token, command) {
  const res = await fetch(`${API_BASE}/virtualization/guest/exec`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirm: token, command }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { success: false, error: data.error || `HTTP ${res.status}` };
  return data;
}
