# Zaytoon — Architecture

Phase 1 audit + the target architecture, written against the code as it stands.

---

## 1. Audit: what already exists

Zaytoon is **not** a bare chatbot. There is a real execution layer, a real
safety model and a real test suite underneath the chat UI. The gap is not
intelligence — it is that almost none of that capability is reachable as a
*product*: there is no task record, no project concept, no skill system, no
provider registry, and no file API for an editor to sit on.

### Inventory

| Area | State | Notes |
|---|---|---|
| Agents | 8 agents + `AgentEngine` bounded ReAct loop | Works. 1471-line engine, well tested. |
| Tool layer | 36 tools, one `ToolRegistry`, `validate → gate → execute` | The strongest part of the codebase. Keep as-is. |
| Safety | Two layers: unconditional sandbox blocks + human-in-the-loop gate | Correct, tested, not bypassable. Keep. |
| Execution | Parallel safe calls, barriers, heartbeats, metrics, audit log | Recent, solid. |
| Processes | Tracked-only registry, refuses untracked pids | Correct ownership model. |
| Voice | STT/TTS with bilingual routing | Works. |
| Memory | SQLite conversations + preferences | Adequate. |
| Providers | Ollama + OpenRouter, hard-coded, global config mutation | **Not a model manager.** |
| Scheduler/Reminders | Solid | Occupies the `/tasks` REST path. |
| WebSocket | 18 frame types, streaming, confirmations, dedupe, cancel | Good seam. |
| Desktop | pywebview + WebView2 hosting the React UI, 549 tests | Migrated off Tk; closed bridge, backend supervisor kept. |
| Frontend | React 18 + Vite + Tailwind, HUD chat surface | A chat app, not a workspace. |
| Database | `zaytoon.db` (memory), `audit.db` (execution) | No schema for tasks/projects/providers/skills. |

### The five structural gaps

1. **No task system.** A turn exists only for the duration of a WebSocket
   message. There is no id, no lifecycle, no persisted log, no record of which
   files changed. `GET /tasks` is a *reminder* list, which is why the name is
   already taken.
2. **No project concept.** `WORKSPACE_ROOT` is one mutable string. The assistant
   cannot hold two projects, remember one, or know that a directory is a React
   app with `npm test` as its test command.
3. **No skill system.** Extensibility is limited to editing Python in the repo.
4. **No provider registry.** Two providers are compiled in, configured through
   process-global mutation of `config`, with no database and no discovery.
5. **No filesystem API for a UI.** File operations exist only as *agent tools*.
   There is no `GET /files/tree`, no read/write endpoint, no terminal session
   that streams. A code editor has nothing to talk to.

Everything else in the vision — routing, permissions, cancellation, the event
vocabulary, local-first — is either present or has a natural home.

---

## 2. Architecture

```text
                    USER
                      │
      ┌───────────────┴───────────────┐
      ▼                               ▼
  Desktop shell                   Web client
  (Python/Tk, WS client)     (React, WS client)
      └───────────────┬───────────────┘
                      │  one protocol, one core
                      ▼
              ╔═══════════════════════╗
              ║      ZAYTOON CORE     ║   backend/core/
              ║                       ║
              ║  Orchestrator         ║   tasks/  projects/  skills/
              ║    │                  ║   providers/  workspace/
              ║    ├─ Planner         ║
              ║    ├─ Memory          ║
              ║    └─ Context         ║
              ║         │             ║
              ║       Agents          ║   (existing agents/)
              ║         │             ║
              ║   Tool / Skill Router ║   skills/registry.py
              ║         │             ║
              ║  ToolRegistry + Gate  ║   (existing tools/)
              ║         │             ║
              ║  Execution Runtime    ║   processes/  terminal
              ║         │             ║
              ║        OS            ║
              ╚═══════════════════════╝
                      │  events/
                      ▼
              Event bus → WS frames
```

**The one invariant, restated:** the model never touches the OS. Every path
from intent to effect crosses `ToolRegistry.execute`, which validates, then
gates, then runs. Skills do not get a second door — a skill contributes
*instructions and extra tools*, both of which re-enter through the same registry.

---

## 3. The `backend/core/` layer

New, and deliberately additive. Nothing existing moves except where a name
collides.

```text
backend/core/
├── db.py              # schema + migrations, one connection discipline
├── secrets.py         # DPAPI-backed credential storage
├── events.py          # task.* / tool.* / model.* / permission.* bus
├── permissions.py     # named permission vocabulary ← tool tiers
├── tasks/
│   ├── models.py      # TaskStatus, Task, TaskStep
│   ├── store.py       # SQLite persistence
│   ├── manager.py     # lifecycle, cancellation, progress
│   └── graph.py       # DAG runner, bounded concurrency
├── projects/
│   ├── detect.py      # project type / framework / commands
│   ├── store.py       # registry: open, pin, recent
│   └── context.py     # the context block fed to the model
├── skills/
│   ├── manifest.py    # skill.json schema + validation
│   └── registry.py    # discover, install, enable, create
├── providers/
│   ├── store.py       # providers + models tables
│   ├── capabilities.py
│   ├── router.py      # capability → model
│   └── client.py      # any OpenAI-compatible endpoint
└── workspace/
    ├── files.py       # tree, read, write, edit, search, stat
    └── terminal.py    # sessions with streamed output
```

### Task lifecycle (§11, §12, §43)

```text
RECEIVED → ANALYZING → PLANNING → WAITING_FOR_PERMISSION → EXECUTING
                            ↓                                ↓
                        VERIFYING ←──────────────────────────┘
                            ↓
                        COMPLETED
```

plus `FAILED`, `CANCELLED`, `PAUSED`, `WAITING_FOR_USER`.

Transitions are validated by one function (`TaskStatus.can_transition_to`). An
illegal transition raises rather than silently writing, because a task log that
lies about its own state is worse than no task log.

A task carries: id, title, status, progress, started/ended, per-step rows,
log lines, files changed, tools used, provider/model, errors, result.

**The graph.** `TaskGraph` is a DAG of nodes with declared dependencies.
Independent nodes run concurrently under a semaphore; a node whose dependency
failed does not run, and is reported `skipped` with the reason. Cycle
detection is at construction time, not at run time.

### Permission vocabulary (§37)

Ten named permissions, derived from the existing tier so the two cannot drift:

```text
filesystem.read    filesystem.write   filesystem.delete
process.execute    process.manage     network.request
browser.control    git.write          git.destructive
system.modify
```

`permissions.required_for(tool)` is a pure function of the tool's declared
tier and name. A tool may *add* permissions beyond its tier (a `run_code` tool
that also writes needs both) but never remove one.

### Provider registry (§17–§20)

A provider is a row: id, name, kind (`ollama` | `openai_compatible` |
`openrouter`), base URL, whether it needs a key, enabled flag, default flag. A
model is a row: provider, model id, capabilities, context length, priority.

Discovery is `GET {base_url}/models` for OpenAI-compatible endpoints and
`GET {base_url}/api/tags` for Ollama — both probed on demand, never on a timer.

Routing is capability-based: the caller declares
`required_capabilities`; the router picks the highest-priority model whose
recorded capabilities are a superset. A user override (`forced_model`) beats
routing, always, and is reported as such.

**The free-only policy still holds for OpenRouter.** The registry does not
weaken `config.assert_free_model`; an OpenRouter provider refuses any model that
is not allow-listed and in `:free` form, exactly as before.

### Secrets (§39)

API keys never cross the API surface. `GET /providers` returns
`has_api_key: true` and nothing else. Storage is Windows DPAPI via `ctypes`
(no new dependency, and the OS key is the right key); elsewhere a `0600` file
with the limitation documented rather than implied away.

### Workspace (§8, §14, §15)

`workspace/files.py` is a service, not a set of tools — the REST layer and the
tools call the *same* functions, so an editor and the agent cannot disagree
about what a path resolves to. It delegates to the existing
`sandbox.resolve_workspace`; there is still exactly one definition of
containment.

Terminal sessions are the same tracked-process model as `tools/processes.py`,
extended with a line-streaming callback. Sessions are addressed by id and are
cancellable.

### Git (§29, §50)

`core/workspace/git.py` is a **service over the existing runner**, not a second
one. It imports `_check_ref`/`_check_value` from `tools/git.py` and reuses the
same fixed-argv, `shell=False` invocation with a `shutil.which()`-resolved git.
A second runner would have been a second set of opinions about which characters
may appear in a ref, and disagreement there is argument injection.

What the service adds is **structure**. The tools return rendered text because a
model reads text; a source-control panel needs objects — staged versus modified,
which branch is current, what the last ten commits were. Parsing `--short` output
in JavaScript would have been the wrong place for it.

Two details in the parsers are the load-bearing parts:

- `git status --porcelain=v1 -z`. NUL-separated, because a file name may contain
  a space, a quote or Arabic characters, and the default line-based output splits
  it. `tests/core/test_core_git.py` has a repo with `my notes.md` and `ملف.md`
  in it.
- `git log` records separated by **NUL**, not a printable character. NUL is the
  one byte a git commit message cannot contain, so it cannot be corrupted by a
  subject; the fields inside a record are `split("\x1f", 4)` with a **maxsplit**,
  because the subject is the last field and is the only one a user controls.

`%(refname)`, not `%(refname:short)`: a remote-tracking branch's short form is
`origin/master`, which is indistinguishable from a *local* branch of that name.
Only the full ref answers "is this local or remote", which is the question the
endpoint exists to answer.

The REST layer distinguishes the failure modes deliberately — 403 outside the
workspace (a containment decision, no retry offered), 404 not a repository (hide
the panel), 409 git refused (show its own words, they are specific), 422 a
malformed request (a bug in the caller). One generic 500 would make an unversioned
folder look like a defect.

### Portable layout (§32)

`core/paths.py` is the single answer to "where does Zaytoon keep X". Resolution
is: an explicit environment variable, then `ZAYTOON_HOME/<Layout dir>`, then
the checkout's own `backend/`. That ordering is what makes an application update
replace `Application/` and nothing else — the databases, the skills, the
secrets and the workspaces all live outside it.

The module never writes. Resolution is called from import-time health checks,
and an import that touches the filesystem is a side effect nobody can find.
`ensure()` is the one function that creates, and every caller asks for it
explicitly.

Three of its answers are `None`, and that is the interesting part:

- `config_dir()` — `None` in a checkout, which has no separate config directory
  and loads `.env` beside `config.py`.
- `workspaces_dir()` — `None` in a checkout, because the default is
  `~/ZaytoonWorkspace`, and returning a path here would silently change it.
- `skills_dir()` — `None` in a checkout, which falls back to the workspace.

A plausible-looking wrong answer is worse than no answer: someone then goes
looking for a file that does not exist.

`config.py` now delegates its workspace default here, and loads
`ZAYTOON_HOME/Config/.env` with `override=False` — a variable already in the
environment was set deliberately. `common/audit.py` and `core/db.py` follow the
same resolution, and a test asserts they still resolve to **different files**:
sharing one would reintroduce the `database is locked` failure that made the
audit page unusable.

### Installer

`install.sh` and `install.ps1` do five things: detect the platform, check
dependencies, create the layout, install the backend/web/desktop, and write
launchers. They deliberately do **not** download an AI model, and a missing
dependency is a warning rather than an abort — an installer that stops at the
first absent tool leaves a half-installed system.

`Config/.env` is written **only if absent**. Overwriting it would destroy the
user's API keys and workspace choice on a reinstall, which is the single most
destructive thing an installer can do.

`Application/{backend,frontend,desktop}` are symlinks (junctions on Windows)
rather than copies, because a copy installs a stale tree that then looks like a
mystery bug while the user edits the checkout.

---

## 4. API surface (§50)

```text
/tasks            GET  POST            agent tasks (reminders moved to /reminders)
/projects         GET  POST            registry: open, pin, recent, context
/files            GET  tree | read | search | stat
                  POST write | edit | create | mkdir | rename | move | copy | delete
/terminal         GET  list | POST open | POST write | DELETE close
/skills           GET  POST install | POST enable | DELETE
/models           GET  list | POST refresh
/providers        GET  POST | PUT | DELETE | POST test
/permissions      GET  granted decisions
/system           GET  cpu/ram/disk (declared states, never fakes) +
                  uptime_seconds, tasks/processes/terminals, resource sample
/events           GET  recent — the core bus's ring buffer, bounded (≤100)
                  and redacted on the way out (diagnostics, §33)
/git              GET  status | diff | log | branches | remotes
                  POST add | commit | checkout | push
/models/tiers     GET  POST  per-tier model pins (fast/coding/reasoning/default)
/settings         existing workspace + ai (POST /settings/ai/pull is explicit
                  and single-flight; /settings/ai/probe records capabilities)
```

`/git/*` has **no permission gate**, like `/files/*`, and for the same reason:
these are the user's own editor and source-control panel, not the model. The gate
exists to stop the model acting without a human; here the human is the caller.
Containment is unaffected — `resolve_workspace` runs unconditionally, because
"the browser tab asked nicely" is not authorisation and the browser's origin is
not authenticated at all.

Realtime stays on the WebSocket. New frames: `task_created`, `task_updated`,
`task_step`, `task_log`, `terminal_output`, `file_changed`, `skill_changed`.

**`/tasks` changes meaning.** It is currently the reminder list. Reminders move
to `/reminders`; the old shape keeps working under the new path so the frontend
change is mechanical. This is a deliberate break — a task system cannot live at
`/agent-tasks` while `/tasks` means something else.

---

## 5. Desktop: WebView, not Tauri — and it shipped

The spec preferred Tauri/WebView over the then-current Tk shell. Tauri was
declined, for reasons worth stating rather than hiding: it means Rust and a
rebuild of every shell behaviour (backend supervisor, hotkeys, tray, the
security-critical confirmation dialog), and doing that badly is strictly worse
than not doing it.

The follow-up recorded in that increment has since been completed as written:
the workspace UI is wrapped in a WebView window using **pywebview** (WebView2
on Windows, GTK WebKit on Linux) — no Rust, no Electron, no second UI. The
host (`desktop/app/host.py`) owns window state, single-instance, and the
backend lifecycle; `desktop/app/bridge.py` is an explicit, closed JS ⇄ Python
API (no arbitrary execution). The React app remains the one interface, browser
or desktop. The legacy Tk modules and their full test suite are retained, but
`main.py` no longer launches them.

---

## 6. What "do not overengineer" meant here

Rejected on purpose:

- **A message bus framework.** `EventBus` already exists and works. `core/events`
  adds vocabulary to it, not a broker.
- **A workflow engine.** Airflow/Temporal/Prefect are for schedulers that run
  for hours on a cluster. A task graph here is 80 lines and is cancelled by a
  WebSocket frame.
- **Vector memory.** `ddgs` + SQLite FTS covers search. Embeddings are a
  capability in the model registry when someone actually uses one.
- **A plugin marketplace.** Skills are directories with a `skill.json`. Install
  is a copy.
- **ORM.** `aiosqlite` with explicit SQL, following `common/audit.py`, which
  already solved the locking problem by using its own file.

---

## 7. Tests

Every new module is covered by the same standard the existing suite holds
itself to: the contract, not the implementation.

| File | Covers |
|---|---|
| `test_core_db.py` | schema creation, migration idempotence, WAL |
| `test_core_secrets.py` | round-trip, never returned by the API, redaction |
| `test_core_events.py` | fan-out, a raising subscriber cannot break the turn |
| `test_core_tasks.py` | lifecycle legality, persistence, cancellation |
| `test_task_graph.py` | concurrency, barriers, skipped-on-failure, cycles |
| `test_core_projects.py` | detection, registry, context block |
| `test_core_skills.py` | manifest validation, install, enable, create |
| `test_core_providers.py` | CRUD, discovery, capability routing, free-only |
| `test_core_workspace.py` | tree, read, edit, search, containment |
| `test_api_workspace.py` | `/files`, `/projects`, `/terminal` over HTTP |
| `test_api_tasks.py` | `/tasks`, `/reminders` |
| `test_api_providers.py` | `/providers`, `/models`, no secret leakage |
| `test_core_git.py` | real repositories: containment, porcelain `-z` parsing, NUL-separated log records, branch remotes, REST status mapping |
| `test_core_paths.py` | layout precedence, and the `None` answers |
| `test_orchestrator.py` | one turn → one task record; cancellation reaches the work *and* the record |

Frontend:

| File | Covers |
|---|---|
| `taskState.test.js` | status → label, tone, percentage |
| `fileTree.test.js` | tree folding, sorting, and the empty case |
| `useWorkspaceSocket.test.jsx` | the core frames, and bounded terminal output |
| `GitPanel.test.jsx` | 404 hides, 403 is not retryable, 409 shows git's words, a failed commit keeps the typed message |
| `App.runtime.test.jsx` | both rails at desktop width, one drawer carrying **all six** tabs at narrow width |

The git tests make real repositories in a temp workspace rather than mocking
git. The questions worth asking — is the path contained, does a file name with a
space survive, does a refusal map to the right status — cannot be answered by a
double that returns what the test expected. The suite is skipped, not failed,
when git is absent, so a machine without it does not claim the routes work.

The frontend drawer deserves a note: at narrow width the single tab list carries
**both** rails' tabs. Showing only the workspace tabs would hide the task list
and the terminal on a phone, which are the two things a user needs while work is
running. `WorkspaceShell` has one `inspector` function used by both layouts
precisely so the two cannot drift — the previous version had two switches, and
the drawer had already lost the task list once.

The existing 1405 backend tests must stay green. `/tasks` is the one
intentional behavioural break, and the reminder tests move with it.