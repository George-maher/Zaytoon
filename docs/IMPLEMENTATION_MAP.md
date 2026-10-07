# Zaytoon — Implementation Map

Phase 1 deliverable: the audit before the work, written against the code as it
stands. Spec sections (`§n`) refer to *System Completion, Integration &
Production Hardening*. Living document — phases tick off against it.

---

## 0. Method and regression gates

Audited before changing anything: backend architecture, `backend/core/`,
frontend shell + panels, desktop host, native bridge, tool/execution system,
task/event system, model system, skills, config/paths, installers, security
gates, tests, docs. Three parallel read-only audits (core runtime, frontend,
models/skills/agents) plus `docs/ARCHITECTURE.md`.

**Gates — never regress:**

```text
backend  ≥ 2130 passed, 2 skipped     desktop ≥ 549 passed
frontend ≥ 301 passed, lint clean, build OK
desktop smoke 10/10 · verify_final · install.ps1 -Check · install.sh --check
```

No test may be deleted to reduce failures; existing behavior stays a gate.

---

## 1. Verdict by spec section

| § | Area | Verdict | Evidence |
|---|------|---------|----------|
| 4 | Workspace: open/switch/pin/recent/close projects | **EXISTS** | `core/projects/*` + `/projects*` REST + `ProjectsPanel` (open/pin/close/commands; detect/recent/context endpoints exist, uncalled) |
| 5 | File explorer | **PARTIAL** | `core/workspace/files.py` + `/files/*` REST complete (tree/read/write/mkdir/rename/move/copy/delete/search/stat/diff, containment). **UI is read-only**: `FileExplorer` has no create/rename/delete/move/copy; `/files/search` uncalled |
| 6 | Code workspace | **PARTIAL** | `CodeWorkspace` (textarea edit, diff-first REVIEW, save/discard, backups). No syntax highlighting; no dedicated tests |
| 7 | Terminal | **EXISTS (gaps)** | `core/workspace/terminal.py`: streaming, exit codes, input, cancellation, ring buffer, sandbox, 12-session cap; REST + WS `terminal_output` frames; `TerminalPanel` complete. No env-var/timeout params (inherit-only) |
| 8 | Process manager | **EXISTS (dup)** | `tools/processes.py` (owned-only, stop refuses untracked) + `/processes*` + `ExecutionHistory`. Two near-identical subprocess registries (processes vs terminal) — documented debt, not a correctness bug |
| 9 | Task system | **PARTIAL** | 10-state lifecycle + transition table + persistence + manager (cancel/pause/resume/run) + REST + `TaskPanel`. **No production turn creates a task**; states `EXECUTING`/`WAITING_FOR_*` unreachable; pause is a flag nothing reads; no retry/run; no `parent_task_id` |
| 10 | Task graph | **EXISTS, unwired** | `core/tasks/graph.py`: DAG, bounded concurrency (4), barriers, skip-on-failed-dep, cycles refused — 25 tests, **zero production callers** (safe tool calls already parallelise in `AgentEngine._run_batch`) |
| 11 | Agents/orchestration | **PARTIAL** | 10 agents + `AgentService.dispatch` live. `core/orchestrator.py` (turn → task record, project preamble, skill context, transitions) is **test-only** — WS `_run_turn` and `POST /chat` duplicate its sequence |
| 12 | Provider abstraction | **PARTIAL** | `services/ai_provider.py` (Ollama/OpenRouter, health/settings) + **`core/providers`** (row-based store, any `ollama`/`openai_compatible`/`openrouter`, secrets, discovery). **Two systems**; `core.providers` never serves inference (`OpenAICompatibleClient.complete/stream` have no callers) |
| 13 | Model Manager UI | **EXISTS (gaps)** | `ModelsPanel`: add/test/refresh/delete provider, enable models, routing preview. Gaps: no tier (fast/coding/reasoning) assignment endpoint, `POST /settings/ai/pull` is a **stub**, capability probe (`probe_capabilities`) dead, `POST /settings/ai` not persisted, no per-model probe/edit in UI |
| 14 | Capability routing | **PARTIAL (3 impls)** | Live: `model_registry.select_models` (OpenRouter chain) + Ollama tier lookup (**no capability check**). Preview-only: `core/providers/router.plan` with per-candidate reasons. Ollama discovery never records `reasoning`/`tools`, so `tier=reasoning` cannot resolve |
| 15 | Skills | **PARTIAL** | Manifest/registry/REST/UI + enable cap 12 + anti-injection prompt block. **Skill `tools/*.py` never loaded** (closed list in `tools/registry.build_default_registry`); **instructions never reach a live prompt** (only via unused orchestrator); manifest has no `permissions` field |
| 16 | Tool registry chokepoint | **EXISTS** | `validate → gate → execute`, 36 tools, audit; skill tools (when loaded) must re-enter here |
| 17 | Computer control | **EXISTS** | filesystem/terminal/processes/git/apps/clipboard/notify/system/web + `POST /command/run`; every tool has schema/tier/timeout/audit |
| 18 | Security | **EXISTS — keep** | sandbox unconditional blocks, containment via `resolve_workspace`, gate never looser than tier, secrets never returned, redaction; bridge has no eval/exec/shell |
| 19 | Native bridge | **EXISTS** | 21 explicit methods, security tests, clipboard/notify hardened this session |
| 20 | Command center UI | **PARTIAL** | `WorkspaceShell` 3-column + narrow drawer mounted; **no header nav**; **duplicate header**; right rail = tasks/git/terminal (no system panel); left lacks search/git/tasks; **dock is 4 buttons, not tabs** |
| 21 | AI core state | **EXISTS** | `hudState` 11-state vocabulary + `AICore` + `StatusIndicator` + `statusLabels` precedence — all from real events |
| 22 | Telemetry | **PARTIAL** | Real: link, model chain, latency, requests, rejected, tool tiers, safety, STT/TTS. **Dead row**: `SettingsPanel` reads `llm.metrics.tools` (backend emits `health.metrics.tools`). Unrendered: `llm.metrics.by_model`, `model_health`, `health.core`, `uptime`, `/system`. **Missing**: CPU/memory/disk/network (no source; psutil deliberately absent — stdlib/ctypes sources exist for cpu/mem/disk) |
| 23 | Events + identity | **PARTIAL** | WS `trace_id`/`response_id`/`session_id`, id+content dedupe, `client_msg_id` — solid. **Gaps**: `CoreEvent` envelope has no `event_id`/`timestamp`; core `tool.*`/`model.*`/`permission.*`/`terminal.output` never published live; frontend ignores `model.*`/`permission.*` frames |
| 24 | Tracing | **PARTIAL** | `trace_id` lives only in WS; agents receive `response_id` only; **no agent passes `request_id` to `LLMClient`** → `LLM_ATTEMPT` lines uncorrelatable; `audit.trace_id` holds `response_id`; no REST request-id |
| 25 | Streaming | **EXISTS** | lifecycle + interrupt + TTS early trigger + stale/dup guards; preserve when rewiring |
| 26 | Error handling | **EXISTS** | `common/reporting` (`safe_error`/`client_error`/`redact`), typed errors across core APIs, bilingual user text |
| 27 | Concurrency | **EXISTS** | bounded gather (semaphore 4), graph semaphore, no unbounded threads |
| 28 | Project context | **PARTIAL** | `projects/context.py` builds the block (git state, commands, open file) — **only the unused orchestrator prepends it** |
| 29 | Git | **PARTIAL** | status/log/diff/branches/remotes/add/commit/checkout/push (service+REST+panel). **No pull/fetch/merge/restore**; panel has no branch/diff/checkout/push UI |
| 30 | Installation | **EXISTS** | installers: detect/deps/layout/launchers, config preserved, never download models; WebView2/WebKitGTK checks; desktop app needs no uvicorn/npm |
| 31 | Dev vs production | **EXISTS** | `--dev` (Vite) vs dist build, managed backend lifecycle, user data outside app dir |
| 32 | Configuration | **EXISTS** | env → `ZAYTOON_HOME` → checkout; `Config/.env` override=False |
| 33 | Observability | **PARTIAL** | `/health` (+`core`), `/system`, `/audit`, `/processes`, `LLM_ATTEMPT` logs. No unified diagnostics view; no `GET /events` (bus has `recent()`); REST uncorrelated |
| 34–35 | Tests / E2E | **PARTIAL** | core well covered; **uncovered**: workspace panels except GitPanel, `useWorkspace`, `api.js`; no E2E scenario scripts for the seven acceptance scenarios |
| 36 | Performance | **EXISTS** | streaming, bounded polls (health 20s), no full-reload patterns |
| 37 | Documentation | **PARTIAL** | ARCHITECTURE/AGENTS/README current for desktop; map itself new; updates after phases |

---

## 2. The five structural gaps (the real work)

Everything above funnels into five **unwiring** problems — the code exists, the
product doesn't reach it:

1. **The orchestrator is not on any request path.** Live turns
   (`api/websocket._run_turn`, `POST /chat`) re-implement routing/dispatch and
   therefore: no task record per turn (§9), no project preamble (§28), no skill
   context (§15), no core events (§23), cancellation only reaches the WS layer.
   *Fix: route live turns through `Orchestrator.run_turn_in_session`, keeping
   streaming, TTS, confirmations, dedupe and `trace_id` exactly as they are.*

2. **Custom/local providers cannot serve inference (§12–14).** `core/providers`
   accepts `http://localhost:11434`, `http://127.0.0.1:1234/v1`, discovers
   models, tests connections, previews routing — then nothing uses it.
   *Fix: make `LLMClient` consult `core.providers.router.plan()` for a
   completion target; legacy chain stays as fallback so every existing routing
   test and the free-only policy keep their exact behavior.*

3. **Skills are inert (§15).** Enabled skills neither inject instructions nor
   register tools. *Fix: load enabled skills' `build_tools()` into the one
   `ToolRegistry` with a permission floor, manifest `permissions` surfaced in
   the UI; prompt injection arrives free with gap 1.*

4. **The workspace UI cannot act (§4–6, §29).** The explorer is read-only, git
   is stage/commit only, projects ignore detect/recent/context, and there is no
   system panel or header nav (§20, §22). *Fix: wire the 31 already-written
   `api.js` wrappers that have zero callers; add missing git verbs backend →
   REST → panel.*

5. **Correlation stops at the WebSocket (§23–24).** *Fix: `event_id`/`ts` on
   the `CoreEvent` envelope, `trace_id` → agents → `LLMClient.request_id`,
   `X-Request-ID` middleware, and publish the dead core events without
   double-rendering (the relay owns `tool.*`, the bus owns the rest).*

---

## 3. Phase plan

### Phase 2 — workspace/filesystem/projects/terminal/git
- `FileExplorer`: create folder/file, rename, delete (client-side confirm + existing
  REST), move? (tree drag — skip; via explorer context if cheap), server-side
  search box (`/files/search`), stat/metadata on select, hidden-file toggle.
- `ProjectsPanel`: `POST /projects/detect`, recent section, context view.
- Git service+REST: `pull`, `fetch`, `merge`(no-op ff-only? — decide), `restore`.
- `GitPanel`: branches (list/checkout), diff view, push, pull, stage-all.
- Terminal: optional `env`/`timeout` params on open (documented inherit default).

### Phase 3 — tasks/orchestration/events/tracing
- Wire WS `_run_turn` + `POST /chat` through `Orchestrator` (single sequence:
  task record → preamble → route → dispatch → transitions; memory/TTS/confirm
  untouched). One bus path per event class — no duplicate frames.
- Publish core events live: `task.*` (already), `model.*`, `permission.*`;
  filter what `_ProgressRelay` already owns.
- Tasks: `POST /tasks/{id}/retry` + `POST /tasks/{id}/run` (background run via
  orchestrator), pause honoured by the runner, `parent_task_id` column.
- `TaskGraph` production caller — **decided: option (b)**. Planner output is
  linear prose steps, not a dependency graph, so no production caller emits
  graph-shaped work today. `TaskGraph` stays as the executor *behind*
  `/tasks/{id}/run` for the day a caller does (it is tested and its API is
  unchanged); wiring a planner into it would mean inventing structure the
  model never produced. Revisit when a structured plan format exists.
- Envelope: `event_id`, `ts` on `CoreEvent`; `trace_id` into agent context and
  `LLMClient request_id`; `X-Request-ID` middleware.

### Phase 4 — providers/models — ✅ DONE (backend + frontend)
- `LLMClient`: router-first target resolution → `OpenAICompatibleClient`;
  legacy chain as fallback (free-only + attempt budget + validator untouched).
  `_route_plan(tier, required)` consulted before `_using_ollama()`/`build_chain`;
  one `_AttemptBudget` shared across registry + legacy legs; stream mirrors
  chain semantics (no duplicate emission — failure before first delta falls to
  the chain, after first delta recovers blocking via `complete()`).
  Tests: `tests/core/test_llm_registry_routing.py` (9).
- Tier assignment (fast/coding/reasoning/default) in `kv` (`models.tiers`);
  pin wins only if eligible, else priority; `RoutingPlan.source` ∈
  `tier|forced|priority`; REST `GET/POST /models/tiers`; ModelsPanel dropdowns
  with "auto" default + missing/disabled pin flags. Tests: `TestTierAssignments`
  (14) + `TestTierAssignmentApi` (6).
- Real `POST /settings/ai/pull` (background task, strict-name regex, 409 on
  concurrent, status record + `GET /settings/ai/pull` polling) and
  `POST /settings/ai/probe` (honest 400/404, records capabilities while
  preserving the user's priority/enabled). "Nothing auto-pulls" guard scans
  the tree for `pull_model(` call sites. UI: pull form + status line, per-model
  PROBE (ollama rows only). Tests: `TestPullIsExplicitAndBounded` (8) +
  `TestProbeIsHonest` (4) + `ModelsPanel.test.jsx` (10).
- `/settings/ai` changes persisted across restart (`services/ai_settings.py`:
  settings → `kv`, API key → `core.secrets` under `settings.openrouter`;
  lifespan applies best-effort before provider logging; GET gains `persisted`).
  Tests: `TestAISettingsPersistAcrossRestarts` (4).
- Gate after Phase 4: backend **2245 passed, 2 skipped** · frontend **345**
  tests + lint + build (319.53 kB).

### Phase 5 — skills — ✅ DONE (backend + frontend)
- Loader: `core/skills/tool_loader.py` — `load_skill_tools(registry)` imports
  enabled skills' `tools/*.py` (`build_tools()`), **contained** (realpath +
  commonpath per file, symlink escapes refused, per-skill namespaced module
  names), **failures reported not fatal** (per-file error entries; a tool
  class without a factory is reported, not silent), **effective tier floor =
  `confirm`** (`safe`/unknown raised, `dangerous` kept — a skill cannot
  self-declare silent access). Name collisions with built-ins refused, not
  replaced. Wired at lifespan after `sync_skills_to_db`.
- **One live registry**: `tool_executor` is now constructed before
  `tool_agent` and `ToolAgent` receives `registry=tool_executor.registry` —
  `GET /tools`, `/health.tools_loaded`, the agent catalogue and every
  execution read the same instance, so skill tools appear everywhere at once.
- Manifest `permissions` field: validated against the ten-name
  `core.permissions` vocabulary (unknown names rejected, field named in the
  error), carried through `to_dict()`/`default_manifest()`.
- SkillsPanel: `asks for` line in the list; detail view shows `ASKS FOR` +
  `TOOL FILES` **above** the instructions. Prompt injection already wired via
  Phase 3 (`Orchestrator` → `build_skill_context`).
- Tests: `TestManifestPermissions` (5) + `TestSkillToolLoader` (10,
  symlink test skips where unavailable) in `tests/core/test_core_skills.py`
  (file now 72 passed, 2 skipped) · `SkillsPanel.test.jsx` (5).

### Phase 6 — git/editor/AI coding — ✅ DONE
- **Git UI completion**: the only verb still without a UI was **merge** —
  arm-then-click row (select of local non-current branches, remotes never
  offered), disabled when the repo has only one branch, git's own refusal
  words on conflict. Fetch/pull/push/checkout/stage/unstage/discard/
  restore/diff/log were already wired from Phase 2, and CodeWorkspace's
  diff-first accept/reject save was already in place.
- **`file.changed` had no producer.** The bus, the WS frame, the client
  state and `note_file_change`/`note_tool` all existed — and nothing in
  production ever called them. Wired end to end:
  - `BaseTool.mutates_files` (default `False`; set on `file_write`,
    `create_folder`, `file_delete`, `file_move`) — the tool's own
    declaration, so aliases and skill tools agree with themselves instead
    of a second name list drifting.
  - the engine attaches `params`/`mutates` to the **successful**
    `TOOL_FINISHED` event. Internal fields: the transport builds its frames
    from the three fields it knows, so neither reaches the wire (file
    contents stay in-process).
  - a per-turn **recorder** in the orchestrator subscribes to whichever bus
    the dispatch is given (transport's relay or the default bridge) and
    unsubscribes in the same `finally` — a stale recorder would attribute
    the next turn's tools to this task. Every completed call →
    `task.tools_used`; a mutating success → each path-shaped argument →
    `task.files_changed` + the `file.changed` event. `tools_used` was dead
    for ad-hoc tool turns before this.
  - client: `fileChanges` capped at 50 in the hook; App → WorkspaceShell →
    FileExplorer: amber `AI` badge on the changed row (suffix match, so an
    absolute/backslashed tool spelling lands on the workspace-relative row)
    and a header count.
- Tests: engine `test_a_tool_declares_what_it_changed` · orchestrator
  `TestToolRecording` (4) · FileExplorer +4 · GitPanel merge +4 — backend
  and frontend full suites re-run green (see Phase 9 counts).

### Phase 7 — command center — ✅ DONE
- **Header nav** (Project / Model / System / Settings) replaces the duplicate
  `ZAYTOON` h1 — one h1 in the product (App's). Each button drives the shell
  through one `selectPanel`: an inspector id sets *both* sides below the
  breakpoint but only the inspector when wide (yanking the left rail back to
  its default because someone opened System would lose the place). Settings
  toggles the panel App already owned, via `onOpenSettings`.
- Shell focus **lifted to App** (`tab`/`onTabChange`/`inspectorTab`/
  `onInspectorTabChange`, with the internal `useState` fallback so a
  standalone render needs no plumbing) because the dock lives *outside* the
  shell — header nav and dock write through the same two setters, one source
  of truth for "what is open".
- **Right-rail System panel** (inspector tab `system`), fed by
  `core/system_resources.py` wired into `GET /system` as `resources`: CPU
  delta-sampling (`GetSystemTimes` / `/proc/stat`; the first reading is
  `sampling`, never a fabricated `0%`), memory (`GlobalMemoryStatusEx` /
  `/proc/meminfo`), disk (`shutil.disk_usage` on the workspace), network an
  explicit `unavailable` **with the reason**. `uptime_seconds` added because
  the pre-existing `uptime` is a timestamp, not a duration. The panel polls
  `/health` + `/system` together (a `/system` failure alone drops no health
  rows) and shows per-model mean latency, `model_health`, core counts and the
  `/system` totals (tasks / processes / terminals).
- `LLMMetrics.model_latency_ms` — cumulative per model; the mean in
  `to_dict()` divides by the count `record_success` already increments: one
  counter pair, no drift. "How long does *this* model take?" is the question
  behind every fallback decision, and no counter had it.
- Narrow drawer now renders `extras.right` too — MissionStatus used to vanish
  on a phone while the desktop rail kept it.
- **Dock tab strip** TERM / LOG / DIFF / TASK beside (not instead of)
  MIC/LANG/CONF/STOP: TERM/TASK move the inspector (and the drawer tab below
  the breakpoint); LOG/DIFF scroll `#activity-feed` / `#code-workspace` with
  `scrollIntoView?.()` guarded. The strip's tablist is named "Quick access"
  so it cannot collide with the drawer's "Panels" — two tablists answering
  to one accessible name make every `getByRole` ambiguous, for assistive
  technology too.
- SettingsPanel's dead `llm.metrics.tools` row now reads `health.metrics.tools`
  — the payload's top level; `llm.metrics` never carried it.
- Tests: `tests/core/test_system_resources.py` (8) · `TestSystem.
  test_resources_are_explicit_states_not_zeros` + uptime assertion ·
  `test_llm_client` per-model latency (+2) · `SystemStatus.test.jsx` (3) ·
  `TaskPanel.test.jsx` (6) · `TerminalPanel.test.jsx` (6) · App.runtime dock
  strip + adapted nav assertions — full suites re-run green (Phase 9 counts).

### Phase 8 — hardening — ✅ DONE
- **Duplicate-render guard re-verified after the Phase 3/6 rewiring.** The
  backend comment at `websocket.py` states the rule ("relaying them here too
  would render every task twice — one owner per event class"); the client had
  no test for the tool half of it. `useWebSocket.test.js` now emits *both*
  vocabularies for one tool turn (`tool_started` + dotted `tool.started`,
  `tool_finished` + dotted `tool.completed`) and asserts exactly one
  started/progress/finished row — plus the dotted form *alone* adding nothing.
- **Structured error shape pinned across failure classes.**
  `TestErrorShapeIsUnified` probes one endpoint per class — manual 404, manual
  400, `HTTPException` 422, a real `RequestValidationError` FastAPI raises on
  its own — plus two endpoints never probed before (`POST /tasks/{id}/run`,
  `GET /settings/ai/pull`) and asserts every body is `{"error": str}` with
  FastAPI's `{"detail": ...}` never leaking.
- **Bounded concurrency review** (findings, all live paths already capped):
  tool batch = semaphore(4) + gated barriers (`_run_batch`); DAG =
  `Semaphore(concurrency)`; terminal `MAX_SESSIONS=12`/`MAX_LINES=1000`;
  processes `MAX_TRACKED=32`; one turn per session (cancel-then-start) × ≤100
  sessions; pull 409-concurrent + `_PULLS` cap 8; rate limiter; event buffer
  100 with `recent()` clamping; TTS cache 50; heartbeat cancelled in `finally`.
  The one uncapped `gather` is `ToolAgent.execute_tools`, now marked
  **not on any request path** — only the safety suite calls it; the live path
  is the engine.
- **Secret redaction audit.** `test_no_read_path_returns_the_saved_key`: a key
  saved through `POST /settings/ai` must not appear in the save response,
  `/settings/ai`, `/health`, `/system` or the new diagnostics feed (the write
  path already proved storage ≠ kv). `describe_llm()` documents its own
  guarantee; `GET /settings/ai` carries no key field at all.
- **Skill-tool privilege tests** — `TestSkillToolsAreGated` (5): the floor is
  proven to *bite* at `ToolRegistry.execute`, not merely to be declared —
  a floored skill tool refuses without approval and never runs (`"output" not
  in result`), an explicit decline stands, validation still precedes the prompt
  (a malformed call gets its precise error, never an approval dialog), an
  unrecognised tier string cannot mean "silent", and `dangerous` keeps its gate.
- **`GET /events/recent` (§33)** — the bus's ring buffer as a diagnostics
  feed: bounded by construction (`EventBus.recent()` clamps 0/-5/99999 →
  1..100, a query shape cannot widen it), **redacted structurally** via a new
  `reporting.redact_payload()` (walks nested payloads, masks string *values*,
  leaves keys/counters alone — `redact()` is text-in-text and `str(dict)`
  would destroy the structure diagnostics exists to provide), unknown `type`
  filter answers an empty 200 rather than 404. Tests: `TestEventFeed` (5) +
  `TestRedactPayload` (4).

### Phase 9 — verification — ✅ DONE

- **Gates (final):** backend **2307 passed, 3 skipped** · desktop **549** · frontend
  **376** + lint clean + build OK (329.48 kB) · desktop smoke **PASS**
  (window, React mount, bridge, CORS, health) · `verify_final.py`
  **ALL CHECKS PASSED** (free-only, voice routing, security sandbox) ·
  `install.ps1 -Check` and `install.sh --check` both pass (all deps) ·
  security suite green (`test_security_sandbox`, `test_workspace_and_injection`,
  bridge `TestSecurity`, `TestSkillToolsAreGated`).
- **`backend/scripts/e2e_scenarios.py`** — seven scripted acceptance scenarios
  against the *real* WebSocket with the real local Ollama provider:
  bilingual assistant turn · safe tool turn · confirmation gate (approve
  creates / decline leaves nothing on disk) · reminder round-trip (created,
  listed, deleted) · diagnostics shapes · containment + audit escapes ·
  policy guards. Final result: **7 passed, 0 skipped, 0 failed**. SKIPs are
  reserved for honest provider-unavailable markers (429, "model not
  installed", "ollama serve", …), so a broken flow can never hide behind a
  skip. `run_turn` waits for the terminal `done` frame after the prose
  (the transport owns `done`, *after* TTS drain — breaking on `message`
  read as "no done status").
- **Four real defects the live run caught that no unit test covered:**
  1. **`_handle_reminder_cmd` had no function.** A refactor replaced the WS
     `reminder_cmd` call site with `_handle_reminder_cmd(...)` and never
     wrote it (`git log -S` shows only the call, commit `e0aeffe`). Every
     reminder-panel button raised NameError, fell through the receive
     loop's catch-all and **dropped the socket**; `reminder_list` never
     arrived, so the panel was permanently empty. The handler now exists
     (list/delete/done, bilingual, and a bad command answers with `error`
     rather than disconnecting), and `tests/test_websocket_reminders.py`
     (8 tests) drives the *real* handler so the call site and its
     implementation cannot drift apart again.
  2. **Reminders were partitioned by an ephemeral session id.** Listing
     always filtered `session_id`, but the socket id is
     `sess_<timestamp>_<ip>` — a new value on every reconnect — while the
     firing path (`get_due_reminders`) never filtered. The result: *"Reminder
     set"* confirmed, the scheduler would have fired it, and every panel
     said there were none. **Decision: `session_id` is attribution, not a
     partition** — list defaults to workspace-wide, an explicit id still
     filters. Fixed across `MemoryAgent.list_reminders`,
     `ReminderService.list`, `SchedulerManager.list_reminders`, the REST
     `session_id` query param, the agent-service list action, and the
     frontend `getReminders` (param omitted when null); `App.jsx` now
     refreshes the panel on connect. Guarded by `TestReminderVisibility`
     (2 REST tests).
  3. **`ToolAgent._make_approval` raised `AttributeError` on every
     non-approval branch.** The four notice dicts (`_DECLINED`, `_TIMEOUT`,
     `_UNAVAILABLE`, `_ERROR`) are module-level, but the closure wrote
     `self._DECLINED` — the live decline turn logged
     `op=approval type=AttributeError` and fed an error string to the model
     instead of the bilingual refusal. The WS suite stubs `ToolAgent` and
     the engine suite supplies its own callbacks, so nothing walked these
     branches; `TestApprovalCallbackNotices` (7 tests) now does, outcome by
     outcome.
  4. **A declined turn was driven to `no_tool_taken`.** After a refusal the
     `expect_tool` nag still demanded a tool call, so the model retried
     (re-prompting the user), was declined again, and the turn ended as
     *"I couldn't complete that…"* — a technical-failure message for a turn
     the user deliberately cancelled. `AgentEngine` now sets `gate_refused`
     **only from the gate's own decision** and exempts subsequent prose
     from the tool nag; a model dodging a *needed* tool still gets the
     full nag ladder. Regression: `test_a_decline_exempts_prose_from_the_tool_nag`.

---

## 4. Constraints remembered throughout

- The model never touches the OS: every effect crosses `validate → gate →
  execute`. Skills included.
- Free-only OpenRouter policy and `openrouter/free` refusal stay enforced on
  every path (re-asserted in the store *and* the router).
- No automatic model downloads; pull is an explicit user action.
- One definition of containment (`sandbox.resolve_workspace`); one event bus;
  one tool registry; one completion contract.
- Duplicate rendering is a critical past bug: one owner per event class on the
  wire, `client_msg_id` + content dedupe preserved.
- Bridge: no eval/exec/shell. REST file/git mutations are the human's own
  editor, gated by containment, not weakened for the model.
