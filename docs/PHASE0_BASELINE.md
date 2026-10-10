# Zaytoon — Phase 0: Discovery & Baseline

Date: 2026-10-10 · Branch: `arena/fdb0f5cf-zaytoon` · HEAD audited: `a01c7c9`
Environment used: Linux, Python 3.11.2, Node 20-series. **No Ollama daemon and no
OpenRouter key were available here**, so nothing in this file is a claim about
live model behaviour.

Legend: **[V]** verified by reading/running the code · **[H]** hypothesis, needs
confirmation · **[R]** recommendation.

---

## 1. Architecture map [V]

```
                         USER (voice / text)
                                   │
        ┌──────────────────────────┴───────────────────────────┐
        ▼                                                      ▼
  desktop/ (pywebview + WebView2)                      frontend/src (React 18)
  app/host.py window · app/bridge.py closed JS⇄Py API  App.jsx · components/
  app/backend.py supervisor · app/protocol.py          hooks/useWebSocket.js ← protocol
        │  spawns                                        hooks/useWorkspace.js · lib/api.js
        │                                                components/HUD/ (state machine)
        ▼                                                components/workspace/ (shell)
   serves frontend/dist or Vite dev URL ◄──────────────────────┘
                                   │  HTTP + one WS protocol
                                   ▼
  backend/api/main.py   FastAPI app factory, 46 route handlers, lifespan,
                        CORS, rate limit, exception handlers, request-id mw
  backend/api/websocket.py  18 frame types, streaming TTS, per-session state,
                        confirmations, dedupe (client_msg_id), interrupt
                                   │
                    ┌──────────────┴───────────────┐
                    ▼                              ▼
        backend/core/orchestrator.py    backend/services/agent_service.py
        one turn → one Task record      intent dispatch (legacy path)
        preamble, cancel, recorder                 │
                    │                              ▼
                    └──────────────► backend/agents/
                                     router_agent (regex fast path → LLM fallback)
                                     fast / planner / code / tool / scheduler / memory
                                     engine.py = bounded ReAct loop (≤8 iters)
                                   │
                     ┌─────────────┴──────────────┐
                     ▼                            ▼
        services/llm_client.py            tools/registry.py  ← THE chokepoint
        (OpenRouter free pool,            validate → risk gate → execute
         Ollama path, attempt             + tools/safety.py RiskAssessor +
         budget, contract check)            ConfirmationManager
         ▲   ▲                             + tools/sandbox.py unconditional blocks
         │   └── services/ai_provider.py /
         │          services/ollama_client.py /
         │          core/providers/{client,router,store}.py
         ▼
  backend/core/  db.py (core.db) · tasks/ (lifecycle+DAG) · projects/ · skills/
                 workspace/{files,git,terminal} · secrets.py · events.py (bus)
                 permissions.py (10 named perms from tiers) · paths.py (layout)
  backend/reminder/ + scheduler/  · memory/  · voice/ (stt/tts/routing)
  backend/common/  audit.py (audit.db) · reporting.py (redact/safe_error) ·
                   concurrency.py · tracing.py

  SQLite, three files, three owners [V]:
    backend/data/zaytoon.db  memory + preferences (held open for process life)
    backend/data/audit.db    one row per tool invocation
    backend/data/core.db     tasks, projects, skills, providers
```

Dependency direction is clean at the top: `api → core → agents → services/tools →
common`. `tools/*` and `core/workspace/*` both delegate containment to
`tools/sandbox.resolve_workspace`, which is genuinely the single definition of
"path → inside/outside". **[V]** (verified: `grep` finds no second resolver;
`core/workspace/files.py` imports it.)

### Strengths worth preserving, not re-litigating

- **The tool gate is real.** `validate → gate → execute`, with unconditional
  sandbox blocks reported as `blocked` (never as approvable), tiers reconciled
  by taking the stricter of declaration and pattern tables, and the gate placed
  *before* the result cache so a cached result cannot bypass approval. [V]
- **No ungated channel.** The WS `command` frame was deleted; the removal is
  protected by an AST-parsing test (`test_workspace_and_injection.py`). [V]
- **Honest failure vocabulary.** `common/reporting.py` redacts before anything
  reaches a log or a client; the registry stopped echoing `str(e)`. [V]
- **`/tasks` semantics, task lifecycle legality, cancellation reaching both the
  work and the record** — the orchestrator is small enough to reason about and
  its invariants are tested. [V]
- **2322 backend tests, 451 frontend tests, real-`git` tests instead of mocks.**
  The suite is unusually good at asserting contracts rather than implementations. [V]

---

## 2. Feature inventory (what actually exists) [V]

| Vision area | State | Evidence |
|---|---|---|
| Bilingual AR(EG)/EN | **Implemented** — detection, prompts, TTS voice per message, STT→TTS language handoff | `arabic.py`, `voice/routing.py`, `tests/test_arabic.py`, `test_prompt_dialect.py` |
| Computer control | **Implemented** — 36 tools in one registry, tiered; app launch, files, terminal, processes, git, media, screenshot | `tools/registry.py`, `/health` → `tools_loaded: 36` (measured) |
| Coding workflows | **Partly** — file/git/terminal/test tools + a code workspace UI (explorer, git panel, terminal); no diff-review or regression-detection loop | `core/workspace/*`, `components/workspace/*` |
| Cybersecurity skills | **Not present** as a capability; only the tool sandbox + audit | — |
| Research / current info | **Thin** — one tool, `search_web` (ddgs), snippets only; no ranking methodology, no source-sourcing contract | `tools/web.py` |
| Skills platform | **Implemented server-side** — manifest, install/create/enable/remove, per-skill tools loaded into the one registry, permission floor (`safe` self-declaration raised to `confirm`); **UI exists and is wired** (create/enable/remove/detail) | `core/skills/*`, `SkillsPanel.jsx` |
| Tasks / background work | **Implemented** — lifecycle, DAG, progress, tracked processes, heartbeats | `core/tasks/*`, `tools/processes.py` |
| Persistence | **Implemented** — 3 SQLite files, migrations, DPAPI/0600 secrets | `core/db.py`, `core/secrets.py` |
| Fast path | **Partly** — regex keyword router before LLM; no per-intent direct-execution short circuit | `agents/router_agent.py` |
| Desktop host | **Implemented** — pywebview, closed bridge, single instance, pinned asset port, backend supervisor | `desktop/app/*` |
| Voice | **Implemented** — VAD, wake word, streaming TTS, STT (gracefully degrades when faster-whisper absent; observed live) | `api/websocket.py`, `voice/*` |

---

## 3. Quality baseline (measured here) [V]

| Gate | Result |
|---|---|
| `pytest tests/` (backend, Linux) | **12 failed, 2309 passed, 1 skipped** in 95.7 s · 2322 collected |
| `npm run lint` (frontend) | passes |
| `vitest run` (frontend) | **1 failed, 450 passed** — and **4 of 36 test files never loaded, hiding 56 more tests** |
| `npm run build` (frontend) | **FAILS** — unresolved imports |
| CI guard "free-only" (run locally) | passes (`validate_free_models() == []`, 14 allow-listed) |
| CI guard "local model execution must stay removed" (run locally) | **FAILS** — `AssertionError: local-model config reintroduced: ['OLLAMA_BASE_URL']` |
| `GET /health` against the real server | 200 in ~1 s after start; `tools_loaded: 36`, `status: "ok"` |
| `POST /chat` with the model daemon down | **79 ms**, honest bilingual failure text, no fabricated answer |
| `import api.main` | 0.69 s |
| Desktop suite | **not run here** — needs pywebview/GUI deps; treated as unverified |
| GitHub Actions, `main` | 3 runs, all `conclusion=failure`, no job logs retrievable via API |

### The 12 backend failures, classified [V]

| # | Failure | Class |
|---|---|---|
| 3 | `test_core_git.py` add/commit ×2, merge-conflict ×1 | **Test is not hermetic** — depends on ambient global `git config user.email`. Passes on a configured machine, fails everywhere else. `core.workspace.git.GitCommandFailed: Author identity unknown` |
| 3 | `test_scheduler_regression.py::TestDateparserIsLastResort` | **Missing declared dependency** — `dateparser` is imported by `scheduler/parser.py:318` and is in neither requirements file |
| 1 | `test_core_skills.py::…symlink…` | **Real defect** — `core/skills/registry.py:remove()` `realpath`s *through* the symlink, then its own containment check rejects it, so it raises where the code below intends `os.unlink(target)`. A symlinked skill is undeletable, and the endpoint maps the `ValueError` to **403** |
| 1 | `test_api_routes.py::…post_rejects_an_unusable_path` | **Real cross-platform defect** — `POST /settings/workspace {"root":"X:/no/such/drive"}` returns 200 on POSIX: a relative-looking root is *accepted and created* inside the source tree. "A path that cannot be created is rejected" is false off-Windows |
| 2 | `test_security_sandbox.py` benign-cmd + different-drive | **Windows-semantics assertions on Linux** — `echo` is not a POSIX argv binary; `Z:\…` is a filename, not another drive. Not a sandbox hole, but the suite is not portable |
| 1 | `test_local_exec.py::…exit=1…` | **Fixture assumption** — the failing command yields exit 2 (`ls: cannot access…`) on Linux, not 1 |
| — | (suspected) 36 tools vs 37 tier names | **Not a defect** — recounted from the live `/health`: 16 safe + 17 confirm + 3 dangerous = 36, consistent [V] |

**None of the 12 is a security-control failure.** Ten are portability/hermeticity
defects, one is a real (small) API defect, one is a real path-validation defect.

---

## 3b. Two findings that reframe everything else

**R0 — GitHub Actions has never run on this repository.** `.github/workflows/ci.yml`
is **not valid YAML**: three step names are written `- name: Guard: the free-only
policy must never be relaxed`, and a plain YAML scalar may not contain `": "`.
`yaml.safe_load` on the committed file: `ScannerError: mapping values are not
allowed here, line 49`. That matches the API evidence exactly — the 3 runs on
`main` each concluded `failure` with **zero jobs produced** and no logs. So no
test, no lint, no build and no guard has ever run in CI, which is why the
case-mismatched imports, the failing a11y contrast and the contradictory provider
guard could all be merged and stay merged. **This was the highest-leverage defect
in the repo and it is invisible to every code review of the code.**

**R1b — the backend test suite writes into the developer's live data.** Importing
`api.main` constructs a module-level `MemoryAgent`, which binds to
`sqlite:///./data/zaytoon.db`; `tests/core` isolates `CORE_DB_PATH`, but nothing
isolated the memory DB or `AUDIT_DB_PATH`. So `pytest` writes conversations,
preferences and audit rows into the same files the running assistant uses — a
`POST /settings/workspace` in a test rebinds the user's containment boundary on
its next start, the "Execution history" page fills with test entries, and running
the suite against a live backend risks `database is locked`. Fixed by isolating
`ZAYTOON_DATA_DIR` + `DATABASE_URL` at conftest import time.

## 4. Risk register (highest impact first)

### R1 — Frontend production build is broken on any case-sensitive filesystem — **Severity: High**
`src/App.jsx`, `src/components/{ChatView,ComputerView,HomeScreen}.jsx`,
`src/hud.design.test.jsx` and `src/components/workspace/…` import
`./components/hud/…` and `./hud/…` (lowercase). The directory is `src/components/HUD/`.
Verified: `npm run build` fails with `Module not found: ./components/hud/AICore`;
4 test files cannot load (**56 tests silently not running**). On Windows/macOS
this works, which is why it was merged. Consequences: `frontend/Dockerfile` and
`docker-compose.yml` cannot build, CI's `ubuntu-latest` frontend job cannot pass,
and the desktop "production" path (`npm run build` first) is broken off-Windows.
Minimal fix: correct the ~20 import specifiers to `HUD`. **[R]** plus a guard so a
case-mismatch cannot return (CI on Linux, or a resolution test).

### R2 — The repo contradicts itself about the primary AI provider — **Severity: High (product + CI)**
- `README.md` (lines 6-8): *"every model is a hard-allow-listed OpenRouter `:free`
  variant. Local model execution (Ollama / llama.cpp / on-device GGUF) has been
  removed entirely."*
- `config.py:135` comment: *"The single supported provider. Local model execution
  has been removed."* — followed by `PROVIDER_OLLAMA = "ollama"` and, at 145,
  `AI_PROVIDER = _env_str("AI_PROVIDER", "ollama")`.
- `AGENTS.md`: Ollama is the default, three local models are prerequisites.
- `.github/workflows/ci.yml`: asserts `not hasattr(config, 'OLLAMA_BASE_URL')` →
  **fails today** (verified locally, exit 1).
So one of {README, CI guard, that comment} is stale and the other two are the truth.
**This is a product decision, not a code decision** — see Q1 below.

### R3 — `/health` reports `"ok"` while the model cannot answer — **Severity: Medium-High (reliability)**
`status` derives from `config.provider_ready`, which for Ollama means "an endpoint
string is set" (no probe, no model-presence check); the Ollama branch also hard-codes
`chain_error = None` and a `routing` block that never validates. Live evidence:
server logged `WARNING AI provider not connected: Connection failed` two seconds
before answering `"status":"ok"` and `"default_model_ready": true`. The desktop
supervisor and any external monitor polling `/health` will call a brain-dead
backend healthy. Also `"uptime": time.time()` (`api/main.py:524`) — a Unix
timestamp in an uptime field; `1791651058 s` (≈56 years) was returned. The UI
reads `/system.uptime_seconds` (correct), so this field is a trap for anyone else.

### R4 — A dead local model is described to the user as a *cloud rate limit* — **Severity: Medium (UX + honesty)**
`services/llm_client.py:1078-1102` raises `AllModelsUnavailableError` with the
actionable text (`MODEL NOT INSTALLED: … ollama pull …` / `… Start it with
\`ollama serve\`.`), but `agents/engine.py:924-940` discards it and substitutes
`_ALL_MODELS_FAILED` — *"All available free AI models are temporarily
unavailable…"*. Measured live: `POST /chat` with Ollama down returns exactly that
sentence. The user is told to wait, for a fault that waiting cannot fix, and the
words "free" / "models" don't even describe the configured provider.
`tests/test_free_routing.py:443` pins the current string, so the fix must replace
that assertion with one that verifies the *intended* behaviour (provider-specific,
actionable), not delete it.

### R5 — Two provider stacks and two transports, with no single owner — **Severity: Medium (maintainability)**
`services/ai_provider.py` (ABC + Ollama/OpenRouter), `services/ollama_client.py`
(transport + health + pull + capability probe), `core/providers/client.py`
(`OpenAICompatibleClient`, also speaks Ollama `/api/tags`), `core/providers/router.py`
(capability routing) and `services/llm_client.py` (1893 lines: policy + chain +
budget + metrics + HTTP + SSE + contract + ollama-specific paths).
Consumers are split: `api/main.py` uses `ai_provider` + `ollama_client`;
`llm_client` uses `ollama_client` **and** `core.providers.client`;
`core/providers/router` is imported by nothing outside its own package and tests [V].
Two modules can therefore disagree about model presence, health and capability,
and a fix applied to one transport silently does not apply to the other.
**[R]** Converge on one seam, but *only* after R2 is decided — which provider is
the primary is what determines which code is the trunk.

### R6 — `api/main.py` is a 1574-line module with 46 route handlers [V]
Model management (`/settings/ai*`, `/models/*`, pull/probe), device pairing
(`/pair`, `/devices`), computer control (`/computer/apps`, `/command/run`),
processes, audit, events, reminders, workspace settings, STT/TTS, chat, and the
WS mount all live in one file, next to `lifespan()` and the exception handlers.
Not a defect per se — everything works — but it is where every future feature
will collide. **[R]** Split into routers by domain (mechanical, no behaviour
change) when the first feature that needs to touch it lands — not before.

### R7 — No bound on concurrent model calls — **Severity: Medium (stability under load)**
Verified by absence: the only semaphores are `PARALLEL_SAFE_TOOLS` (per turn, 4)
and the task-graph one. Nothing caps simultaneous `LLMClient.complete()` calls
across sessions/turns, and `max 100 sessions` is the only ceiling. On a local
runtime with a single GPU/limited RAM, two turns that overlap can OOM or thrash
each other. The spec explicitly asks for bounded Ollama concurrency. **[R]** one
process-wide `asyncio.Semaphore(config.LLM_MAX_CONCURRENCY)` in `llm_client` + a
queued-vs-failed decision.

### R8 — Documentation drift and empty committed files [V]
`QUICK_START.md`, `SETUP_INSTRUCTIONS.md`, `DEPLOYMENT.md`, `DETAILED_DOCS.md` are
**0 bytes** in the tree; root `requirements.txt` is **0 bytes** (so
`pip install -r requirements.txt` at the root installs nothing);
`preview_hud_purple.html` is a stray design artifact at the repo root.
`AGENTS.md` (1227 lines, mostly accurate and genuinely valuable) references files
that do not exist: `common/exceptions.py`, `tests/core/test_api_{providers,tasks,workspace}.py`
(all merged into `test_api_core.py`), `ChatInterface.jsx`/`VoiceControls.jsx`/
`StatusDot.jsx`/`VoicePanel.jsx`/`Chat/VoiceButton.jsx`/`useGsapTimeline.js`
(already deleted), and says "the tool catalogue … real 30-tool registry" where
it is now 36. It also still describes `backend/data` DB names as if `Config/` never
moved. Finally `config.TOOL_ALWAYS_CONFIRM` default contains `registry_write`,
which is not a registered tool name — dead config that *looks* like a control [V].
**[R]** One truth pass over docs + delete/commit-as-real the empty files. Cheapest
defect class in the repo, and the one that most misleads the next contributor
(including me).

### R9 — `skills.remove()` contract inconsistency [V]
Returns `bool`, documents "refuses", raises `ValueError` for the containment case
(and the API turns it into 403 — see R1-table above). Either it refuses by return
value or by exception, not both; the 403 is also the wrong code for a path that
*is* inside the root but happens to be a link.

---

## 5. Decision log

| # | Decision | Rationale | Trade-off | Modules |
|---|---|---|---|---|
| D1 | **Do not restructure anything in Phase 0.** No microservices/queue/ORM/CQRS introduced; keep `tools/registry.py` as the only chokepoint and the current 3-file SQLite layout. | The gate, the containment resolver and the DB split are the load-bearing, tested parts of the system. Consolidating the DBs would re-introduce the `database is locked` failure the split exists to prevent. | Keeps the sprawl in `api/main.py` for now (R6). | all |
| D2 | **Fix the frontend case-mismatch before any UI work** (R1). | It breaks the build, hides 56 tests and blocks Docker/CI. 20-line diff, zero behaviour risk. | None found. | `frontend/src` |
| D3 | **Make the 10 portability failures hermetic instead of skipping them** (git identity, exit-code assumption, Windows-semantics tests). | Skipping would hide real coverage; pinning them to platform facts keeps them meaningful on both OSes. | Test-only churn. | `backend/tests` |
| D4 | **Add `dateparser` to `requirements-dev.txt` and declare it optional at runtime** (or vendor the two patterns we need). | The last-resort path is currently unreachable on a fresh install while docs promise it. | +one dependency to the dev image only. | `requirements*.txt`, `scheduler/parser.py` |
| D5 | **Report `/health` honestly before adding fast paths**: probe the provider, and rename/repair `uptime`. | A fast path that is measured against a lying health endpoint is not measurable. | Slightly slower `/health`; must stay non-throwing and cached. | `api/main.py` |
| D6 | **Keep the provider question (R2) blocked on the owner's answer.** No doc or CI edits about providers until Q1 is answered. | Whichever way it goes, three of {README, CI guard, AGENTS.md, config comments} must change; guessing wastes a full audit cycle. | Nothing else can proceed in that area. | `README.md`, `.github/workflows/ci.yml`, `config.py` |

---

## 6. Proposed roadmap (evidence-ordered; differs from the generic Phase 1-7 order)

1. **P1 Green the build & the gate** — R1, R8-empty-files, git/hermeticity fixes,
   `dateparser`, `skills.remove`, workspace-root validation. *Acceptance: build
   passes on Linux; 0 failed tests on Linux **and** no test weakened; hidden 56
   tests run.*
2. **P2 Truth about providers** — implement the Q1 decision across code, docs,
   CI guard, comments. *Acceptance: CI guard and README agree with `config.py`.*
3. **P3 Health, errors, timeouts, cancellation** (Phase 2 in the brief) — R3, R4,
   then an audit of the 17 silent-swallow sites and the resource-cleanup paths.
   *Acceptance: failure-injection tests; `/health` reports unreachable as unreachable.*
4. **P4 Concurrency + fast path with measurements** — R7, then the deterministic
   direct-execution short circuit (Phase 3+4), before/after numbers for a `get_time`
   / `open_app` class command.
5. **P5 Agent contract hardening** — planning bounds, result verification,
   duplicate side-effect prevention (Phase 4).
6. **P6 Skills platform completion** — edit/validate-with-tests, per-skill
   permission review UI, skill-authored tests (Phase 5).
7. **P7 Research & specialised skills** — source-aware research with dates and
   stated methodology; coding/security workflows (Phase 6).
8. **P8 Full-system verification** — Linux + Windows matrix, desktop smoke,
   `e2e_scenarios.py`, final diff review (Phase 7).

---

## 7. P1 result — green the build and the gate (2026-10-10)

| Gate | Before | After |
|---|---|---|
| `pytest tests/` (Linux) | 12 failed, 2309 passed, 1 skipped | **0 failed, 2333 passed, 3 skipped** (93 s) |
| `npm run build` | **fails** (module not found) | **passes** — 69 modules, 475 kB js |
| `npx vitest run` | 1 failed, 450 passed, 4 files could not load | **607 passed**, 37 files |
| `npm run lint` | clean | clean |
| `yaml.safe_load(ci.yml)` | **ScannerError** — workflow never parsed | parses; 3 jobs, backend matrix `[windows-latest, ubuntu-latest]` |
| CI provider guard (run locally) | **fails** against this tree | replaced by a guard that asserts the real decision and passes |
| `POST /chat` with Ollama stopped | *"All available free AI models are temporarily unavailable…"* (wrong provider, useless advice) | *"The local model provider is not running. Ollama is not reachable at http://localhost:11434: … Start it with `ollama serve`."* — and the Arabic notice names the same fix |

### Changes

**Build & UI correctness** — `frontend/src/{App.jsx,hud.design.test.js}`,
`components/{ChatView,ComputerView,HomeScreen,InputBar}.jsx`: `./components/hud/…`
→ `./components/HUD/…` (15 specifiers). New `frontend/src/imports.case.test.js`:
100 per-file assertions that every relative import matches a real directory entry
by exact name — computed from `readdirSync`, not `existsSync`, so it gives the
same verdict on a case-insensitive filesystem. **Negative control run**: reintroducing
one lowercase specifier turns exactly one assertion red, then reverted.
`styles/theme.css`: `--input-placeholder` `#5c4f6b` → `#8b7c9c`; the token's comment
claimed "8.0:1", the real ratio was **2.53:1** (below AA). `--input-text`'s comment
claimed 19.4:1; it is 16.7:1 (that one passed anyway). Both comments now state the
measured value.

**Test hermeticity** — `tests/core/test_core_git.py`: the `repo` fixture now writes
`user.name`/`user.email` into the repository's own config. The product deliberately
does **not** inject an identity: `-c user.name=…` on a commit would attribute the
user's work to Zaytoon, and `git commit` failing with "Author identity unknown" is
the correct answer for a repo that genuinely has no identity. `tests/test_local_exec.py`:
asserts the exit code the process *actually* returned is echoed, instead of pinning
`exit=1` (cmd's `dir` exits 1, GNU `ls` exits 2 — the number was an OS fact).
`tests/test_security_sandbox.py`: the two assertions that measured `cmd.exe` and
drive letters are scoped `skipif(system != "Windows")` with reasons, and one
platform-neutral containment case is added (a root that does not exist yet is
still a boundary). `tests/test_tool_registry.py::test_every_escape_attempt_is_blocked`
now asserts the *outcome* (nothing read lands outside the root) rather than the
mechanism, per-OS. **Verification that this did not blunt the suite**: re-applying
the historical `str.startswith` containment bug still reddens 4 tests
(`sibling_prefix`, both file tools, the ToolAgent write) — checked and reverted.

**Real defects fixed**
1. `core/skills/registry.py::remove()` — containment was measured *after*
   `realpath`, so a skill reached through a symlink resolved outside the root, was
   refused, and became **undeletable from the UI**. Now judged lexically; the link
   is removed as a link, never followed. New named `SkillOutsideRoot` (the function
   returns `False` for "nothing there" and raises for "not yours to delete", so
   `DELETE /skills/{name}` can answer 404 vs 403 honestly instead of mapping every
   `ValueError` to 403). Two tests added, one tightened.
2. `POST /settings/workspace` — accepted a *relative* root on POSIX: `"X:/no/such/drive"`
   returned **200** and `mkdir`d `./X:/no/such/drive` inside the source tree, then
   made it the containment boundary for every file tool. New rule: absolute after
   expansion, or refused, on every OS. Extracted to
   `services/workspace_settings.py` so the endpoint and startup share one rule.
   A dangling symlink is refused rather than written through.
3. **The saved workspace root was never restored.** `api/main.py` persisted it to
   preferences and its docstring promised a restart would keep it; nothing read
   the row back, so every restart silently moved the assistant off the user's
   project. Now applied in `lifespan` (before any tool can run), non-fatal, and a
   root that has since disappeared warns and keeps the default instead of
   *recreating an empty one* at the old path. 9 tests, including the round trip.
4. `dateparser` — imported by `scheduler/parser.py` as the documented last resort
   and in neither requirements file, so the path was unreachable on a fresh
   install and 3 tests failed. Declared in `requirements-dev.txt`; the missing-
   dependency case now logs once, at `warning`, instead of degrading silently.
5. `agents/engine.py` + `services/llm_client.py` — the provider failure notice.
   `AllModelsUnavailableError` carries a machine-readable `kind`
   (`provider_unreachable` / `model_missing` / `local_model_failed` /
   `pool_exhausted`) and `user_notice()` renders the actionable, bilingual
   sentence; the generic "free models" text remains only the fallback. Verified
   live against the running server, both languages.
6. CI — the file parses now; the "local model execution must stay removed" guard
   (which asserted the *opposite* of the shipped default and therefore could only
   ever fail) is replaced by one that pins the decided policy: Ollama is the
   default, all three tiers have models, `config.py` must not shell out, and
   `VALID_PROVIDERS` is exactly the two. Backend matrix covers Linux; the Docker
   step is scoped to the runner that has a Linux daemon.

**Provider truth (decision Q1: Ollama primary, OpenRouter optional)** —
`README.md`, `config.py` (the "single supported provider / local execution has
been removed" comment that sat four lines above `AI_PROVIDER="ollama"`),
`services/llm_client.py`'s module docstring ("no local fallback and there never
was one"), the two user-visible "Zaytoon calls OpenRouter only" messages
(`api/main.py`, `llm_client.build_chain`), `start.sh`, `start.bat`, root
`.env.example`'s quick-start line, and `AGENTS.md` (a documented
`common/exceptions.py` that does not exist, stale test counts, a "30-tool"
registry, three `test_api_*.py` files merged into one, and an orphans list whose
files were already deleted). `backend/.env.example` was already correct.

### Explicitly *not* done, and why
- **No test deleted, weakened, or marked `xfail`.** Two Windows-only assertions are
  platform-scoped with reasons *and* replaced by a POSIX-meaningful case in the
  same class; `tests/test_free_routing.py:443`'s "free AI models" pin still holds,
  now as the *fallback* branch, with a new test pinning the branch it used to hide.
- `TOOL_ALWAYS_CONFIRM` still lists `registry_write`, which is not a registered
  tool. **Checked before touching it:** `RiskAssessor` consults that set by
  *action name*, including actions the registry has never seen (REST `/command/run`,
  a future tool), so it is a deliberate name-level guard, not dead config. My
  Phase-0 note calling it dead was wrong; corrected here.
- The 4 empty root docs (`QUICK_START.md`, `SETUP_INSTRUCTIONS.md`,
  `DEPLOYMENT.md`, `DETAILED_DOCS.md`), the 0-byte root `requirements.txt`, and
  `preview_hud_purple.html` are **left in place** — they are your files, and
  deleting content is your call, not a cleanup step. Say the word and each goes.
- `/health` still says `ok` with a dead daemon, and still reports
  `"uptime": time.time()`. Deliberately deferred to P3: deciding *what* health
  means (probe on every call? cached? which timeout?) is a design choice, not a
  typo, and it pairs with the timeout/cancellation work in that phase.

### Next (P2 is now finished as part of this; P3 is next)
**P3 — Health, errors, timeouts, cancellation.** Leads, all already evidenced:
(1) `/health` must not report `ok` when the provider is unreachable, and
`uptime` must be a duration; (2) the two `RuntimeError: Event loop is closed`
warnings the suite emits — an `asyncio` subprocess transport and an `aiosqlite`
worker thread, both outliving their loop — are real cleanup bugs, not pytest
noise; (3) the 17 `except Exception: pass/continue` sites; (4) a bounded
provider probe so `/health` does not hang on a hung daemon.
