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

## 8. P3 result — health truth, error visibility, cancellation and cleanup (2026-10-10)

Scope agreed at the end of §7: the four leads listed there. Investigation during
the phase turned up a fifth item, a live reminder-loss bug, which was promoted
into this phase because every minute it stays unfixed doubles a user's duplicate
notifications and deletes the ones that fail.

### Gate

| Check | Before | After |
|---|---|---|
| `pytest backend/tests -q` | 2333 passed, 3 skipped, **2 warnings** | **2389 passed, 3 skipped, 0 failed, 1 warning** (the surviving warning is `StarletteDeprecationWarning` from a dependency) |
| `/health` with Ollama stopped | `status: "ok"`, `status_reason` absent | `status: "degraded"`, `status_reason: "ConnectError: All connection attempts failed"` |
| `/health` `uptime` | `1791651058` (a Unix epoch, ≈56 years) | `9.496` seconds + `started_at` |
| `/health` worst-case latency, hung daemon | inherits `OLLAMA_TIMEOUT` = 120 s | `HEALTH_PROBE_TIMEOUT_S` + 0.5 s outer guard (2.5 s by default). A test whose transport sleeps 30 s under a 0.4 s budget returns in under 1.9 s |
| `/health` under 10 concurrent pollers | 10 live probes | 1 probe (asserted: 2 HTTP requests, `cached: true` on 9) |
| Recurring reminder rows after *n* fires | 2ⁿ (daily: 1 → 2 → 4 → 8) | *n* + 1 (one fired row + one future row) |
| Reminder whose notification fails | consumed: completed, never delivered again | released to `pending`, retried, `failed` only after `MAX_RETRIES` |
| `process_start` child after a backend restart | orphaned, holds its port, unstop-able (registry is memory-only) | stopped during shutdown (`PROCESS_STOP_ON_SHUTDOWN`, default on) |
| `MemoryAgent` connection | never closed; its aiosqlite worker thread outlived the loop | closed last in `lifespan` shutdown, and per-test in `conftest` |
| Silent `except Exception: pass` in the 11 touched modules | 16 broad swallows | 0 broad swallows, enforced by an AST guard test |
| Broad swallow of a *security* cleanup | `SessionState.cancel()` hid a failed `cancel_all()` | logged at ERROR, cancellation still completes |
| Backend shutdown | (unverified) | `Scheduler manager stopped` → `Zaytoon AI backend shut down`, **0 tracebacks** |

Suite time is unchanged within noise (93.33 s → 91.75 s) while running 56 more
tests; `test_reminder_lifecycle.py` alone would have taken ~60 s of real
`RETRY_DELAYS` sleeping had it not zeroed them.

### New defect found and fixed (R10 — reminder consumption and double reschedule)

**Severity: High. User-visible data loss in both directions.**

`MemoryAgent.get_due_reminders()` completed every row it *read* and inserted the
next occurrence of a recurring one. `SchedulerManager._fire_reminder()` *also*
rescheduled, after a verified delivery. Consequences, each reproduced in a test
before being fixed:

1. **A recurring reminder doubled its future rows** every time it fired
   (1 → 2 → 4 → 8 …). The row inserted by the read path went through
   `add_reminder()` without `session_id` or `timezone_offset`, so it also landed
   in the default session at an hour computed without the user's offset.
2. **A reminder whose notification failed was consumed anyway** — completed at
   read time, and `mark_failed` afterwards only renamed a row nothing selects
   again. Set a reminder, the desktop is asleep, it is gone rather than late.
3. `notify_attempts` existed on `ReminderData`, was shown to callers, and was
   **never persisted** — it reset to 0 on every tick, so "attempt 3 of 3" was
   only ever true inside one function's memory.

The fix follows one rule: **a read must not mutate, and a row has one owner.**
The store now claims (`UPDATE … SET status='firing', attempts=attempts+1,
claimed_at=<token>` as one statement, so overlapping ticks cannot both take a
row); `_fire_reminder` settles it (complete + reschedule on delivery, release on
failure). `claimed_at` is the batch token: without it, "firing" only says
*somebody* is working on the row, so the next reader hands out the same reminder
again while the first send is still in flight — which is exactly what the first
version of this fix did, caught by `test_a_second_read_within_the_tick_sees_nothing_new`.

Also, because a claim is only safe if it is eventually released:
`requeue_stale_claims()` runs in `SchedulerManager.start()`, so a process killed
between claim and delivery re-queues instead of swallowing the reminder.
Re-notifying after a crash is the lesser harm beside losing it.

Due reminders are now delivered concurrently within a tick
(`MAX_CONCURRENT_DELIVERIES = 4`), because `_fire_reminder`'s backoff was awaited
serially: one unreachable notification backend delayed every reminder behind it
by up to 21 s. `deliver_due()` was extracted from `_check_loop` so a tick is
drivable from a test instead of by waiting 15 s.

### Changes

| File | What | Why |
|---|---|---|
| `services/provider_health.py` *(new)* | Cached, timeout-bounded, single-flight provider probe; `reachable` / `unreachable` / `unconfigured` / `never_probed` | `/health.llm.configured` was an assertion. Ollama is probed for real (version + `/api/tags`); OpenRouter reports **observations** (circuit-breaker state, counters) because a metered API must not be pinged from a health check |
| `config.py` | `provider_ready` no longer returns `True` unconditionally for Ollama; `HEALTH_PROBE_{TTL_S,TIMEOUT_S,MODELS}`; `PROCESS_STOP_ON_SHUTDOWN` | the predicate is now labelled for what it measures — configuration, not reach — and the reachability question has an owner |
| `api/main.py` | `/health` status from the probe, `status_reason`, monotonic `uptime` + `started_at`, one `APP_VERSION` constant, `provider_health.invalidate()` on `POST /settings/ai`, `memory_agent.close()` and `close_all()` in shutdown | a repointed provider must not be judged by the previous endpoint's cached verdict; the version literal existed twice |
| `agents/memory_agent.py` | claim-based `get_due_reminders`, `release_reminder`, `requeue_stale_claims`, `attempts`/`claimed_at` migration, `MAX_REMINDERS_PER_TICK = 20` | see R10. The batch is capped so a machine that slept for a week does not deliver 400 notifications at once |
| `reminder/service.py` | `release()`, `requeue_stale_claims()`, `get_due()` reflects the stored status, `notify_attempts` mapped from the persisted count | the data class must not contradict the row |
| `scheduler/manager.py` | `deliver_due()`, concurrent bounded delivery, release-instead-of-consume, stale-claim recovery at `start()` | one owner for the row's fate |
| `tools/processes.py` | `ProcessRegistry.close_all()`, and `stop()` now drains the output pumps instead of cancelling them | `asyncio` closes a subprocess transport only once its pipes reach EOF; cancelling the readers left the fd open until the destructor — a leaked descriptor per stopped process in a long-running backend, and `RuntimeError: Event loop is closed` in the test report |
| 11 modules (engine, websocket, completion_validator, tasks/manager, secrets, metrics, domain/models, orchestrator, stt, api/main, scheduler) | broad `except …: pass` → `safe_error(…)` / an explicit log | see below |
| `tests/conftest.py` | autouse teardown closing the import-time `MemoryAgent` | the leaking connection is bound to whichever loop last ran the app, so the warning was attributed to an unrelated test and looked immune to every fix |
| `.env.example` | 4 new knobs, each with its reasoning | every key in the template is checked to exist in `config.py` |

**Two of the swallows were not harmless**, which is why the pass was not simply
kept with a comment: `SessionState.cancel()` swallowed a failure of
`confirmation_manager.cancel_all()` — the call that prevents a dead session's
pending approvals from being honoured later, now logged at ERROR because a
security-boundary failure at debug level is still a hidden failure; and
`contract_from_registry()` swallowed an error while building the tool map, so
the completion validator reported "no violations" about a contract with **no
tools in it**. The others are genuine best-effort paths (metrics must not fail
the call it measures, a temp-file cleanup must not change a transcription
result) — they keep that property and gain a log line.

`tests/test_error_visibility.py` enforces the invariant structurally rather than
by wording: a `pass` in one of the 11 modules is allowed if it catches only
routine exceptions (`CancelledError`) or a narrow domain error
(`FileNotFoundError`, `IllegalTransition`), and is a failure if it can catch
`Exception`. Verified as a real guard: reintroducing one swallow made exactly
that parameterised case red.

### Tests added (56)

`test_provider_health.py` 16 · `test_reminder_lifecycle.py` 15 ·
`test_error_visibility.py` 16 · `test_process_manager.py` +9
(`TestCloseAll` 6, `TestStoppedProcessesReleaseTheirPipes` 3).

Both new behaviour suites were **mutation-checked**, because a test that passes
before and after a fix proves nothing:

* re-applying the old read-path completion + reschedule turned **9 of 15**
  `test_reminder_lifecycle.py` tests red — the double-row growth, the swallowed
  reminder, the lost `session_id` and the crash recovery all have teeth;
* reintroducing `except Exception: pass` in `core/tasks/manager.py` reddened the
  guard for that file only.

Deliberate non-assertion recorded: the registry-escape style assertion in
`test_security_sandbox.py` (see §7) still outnumbers my own containment test's
contributions; the reminder tests assert on **rows in the database**, not on log
lines, so they survive refactors of the code that writes them.

### Explicitly *not* done, and why

* **No synthetic OpenRouter request in `/health`.** A key-validity ping would
  add a metered network call to the endpoint that reports on network failure, and
  would make "is the cloud up?" a question answered by spending money. The
  cloud path therefore reports observations and says so (`source:
  "observations"`), and `never_probed` is an accepted answer rather than a guess
  at `ok`.
* **`ReentrantAsyncLock` kept.** Removing the side effects from
  `get_due_reminders` retired its original justification — its docstring named
  that method as the pattern it existed for — so the docstring now says the case
  is gone and why the class stays: the storage layer has many writers, and a
  future helper that calls another must not hang the process. Refusing the *nesting*
  is a review-and-test job (`test_reminder_lifecycle.py`), not a reason to swap in
  a stricter primitive mid-phase.
* **The 37-`pass`-sites census was not "fixed".** 40 broad/narrow swallows exist
  in the package; 11 modules are now guarded. The rest are in `config.py`
  (before logging exists — a `safe_error` there would recurse through the module
  that reads config), `scripts/` (operator tools, where a traceback is the
  correct output), and narrow handlers where silence is the specified behaviour.
  Each of those three groups was read, not assumed.
* **No `updated_at` column / TTL-based claim expiry.** A claim older than the
  interval could be re-queued from *inside* the loop, but with one scheduler task
  in this process the startup requeue covers the real failure mode (crash,
  `kill`, restart) and a timer-based reclaim would risk re-delivering a
  notification that is merely slow. Revisit if a second scheduler ever exists.
* **Frontend not shown the probe yet.** `SettingsPanel` keys its AI-core badge on
  `llm.configured`, which is now accurate but incomplete: the richer
  `llm.probe` (`unreachable`, `missing_models`) is in the payload and unused.
  Kept out of this phase so the backend contract could land first; it is a
  display change, listed for P4.
* **No change to confirmation policy, tiers, sandbox or audit** (decision 5).
  `test_process_manager.py`'s ownership test now *asserts* that a foreign pid is
  untouched by `close_all`, which pins the boundary tighter rather than looser.

### Next (P4)

Concurrency bound (R7) with measured before/after, plus the deterministic fast
path — and, now that `/health` carries a truthful probe, surfacing it in the HUD
instead of `llm.configured`.

## 9. P4 result — bounded model calls and a deterministic fast path (2026-10-10)

R7 (open since Phase 0) plus the short circuit the roadmap paired it with. Both
are measured rather than asserted, because the claim "this is faster/safer under
load" is the kind that is easy to write and hard to notice when false.

### Gate

| Check | Before | After |
|---|---|---|
| Simultaneous `LLMClient.complete()`/`stream()` calls | unbounded (only per-turn tool parallelism was capped, at 4, and per turn) | `LLM_MAX_CONCURRENCY` = 2, strict FIFO queue, refused with an explanation after `LLM_QUEUE_TIMEOUT_S` = 45 s |
| Peak in-flight under 12 simultaneous requests | 12 | 2 (asserted for limits 1/2/4 × 1/4/12 arrivals) |
| `GET /health` | said nothing about queueing | `llm.slots: {limit, in_flight, queued}` |
| A `get_time`-class turn (`what time is it`, `take a screenshot`, `system info`, `open spotify`) | 2 model generations + tool | 0 model generations + tool |
| Same, latency (simulated 900 ms provider, 32 turns) | p50 **1813 ms**, 64 model calls | p50 **8 ms**, 0 model calls — 230×, measured by `scripts/measure_fast_path.py` |
| `POST /chat` "what time is it" with **Ollama stopped** | "The local model provider is not running… Start it with `ollama serve`" | `It's 2026-10-10 18:18:07 (Saturday)` — a real answer, no provider needed |
| Same request in Arabic, provider stopped | provider-down message | `الساعة دلوقتي 2026-10-10 18:18:07 (Saturday)` |
| A prompt the matcher does not know, provider stopped | provider-down message | unchanged (verified live: the fall-through still names `ollama serve`) |
| Backend suite | 2389 passed / 3 skipped / 0 failed | **2446 passed / 3 skipped / 0 failed** in 89.5–95.0 s (57 new tests, same wall clock) |

### Changes

| File | What | Why |
|---|---|---|
| `common/concurrency.py` | `BoundedGate` + `QueueTimeout`: bounded, strictly FIFO, re-entrant **per task**, with a bounded wait | Re-entrancy is not a convenience: `stream()`'s documented recovery is a blocking `complete()` *inside its own slot*, so a plain semaphore deadlocks on a normal failure path. Per-task (not per-object) is what keeps a sibling task honest |
| `services/llm_client.py` | `complete()`/`stream()` wrapped by `_gated()`; `_llm_gate()` published; `llm_queue_timeout` added to `_USER_NOTICES` (en + ar) | The cap has to sit on the entry points the agents actually call, and a full gate must produce "the model is busy", never the generic "all models are unavailable" |
| `agents/fast_path.py` *(new)* | Anchored intent table (6 phrasing families, EN + transliterated Egyptian + AR), conjunction guard, 120-char ceiling, `ALLOWED_TOOLS` allow-list, templated bilingual rendering | Everything about the matching is deliberately boring. Fuzzy matching, embeddings or "closest tool" would answer half a request as if it were the whole thing — which is the failure mode this is meant to remove, not add |
| `agents/engine.py` | `_fast_path()` before the prompt is assembled; reuses `_run_one()`; falls through to the model on any pre-execution doubt | Reuse is the point: the same heartbeat, start/finish frames, metrics **and audit record** a model-initiated call produces. A second execution path that audits differently is a hole, not an optimisation |
| `config.py` | `LLM_MAX_CONCURRENCY`, `LLM_QUEUE_TIMEOUT_S`, `FAST_PATH_ENABLED` | the knob and the escape hatch, documented in `.env.example` with the reasoning rather than just the default |
| `api/main.py` | `/health` publishes `llm.slots` | "why is it slow" needs a number |
| `scripts/measure_fast_path.py` *(new)* | Runnable before/after for both features | a claim that lives only in a commit message cannot be re-checked |
| `tests/test_agent_engine.py` | autouse fixture disabling the fast path **in that module** | see below — this is the one place a test was changed |

### The one test-file change, and why it was not a weakening

Thirteen `test_agent_engine.py` cases went red when the shortcut landed. They were
not wrong and were not adjusted to match new behaviour: they stub `_ask()` and
post `"what time is it"`, so an optimisation that answers *before* `_ask` makes
them assert nothing about the loop they claim to cover. The module now disables
`FAST_PATH_ENABLED` explicitly. The shortcut is tested in the other direction, in
`tests/test_fast_path.py`, which enables it and asserts the model is **not**
consulted (`_never_ask` raises if it is).

### Security review of the new path

`fast_path` cannot reach anything the model could not reach without prompting:
the tool must be in `ALLOWED_TOOLS`; the registry's declared tier must be
`safe`; and `RiskAssessor`/`TOOL_ALWAYS_CONFIRM` are consulted via
`_needs_approval()` — if the call would prompt, the turn returns to the model
which prompts. Three tests change those policies to prove it, rather than reading
the code: re-tiering a stub (`open_app`), adding `get_time` to
`TOOL_ALWAYS_CONFIRM` (the engine is deliberately constructed *inside* the
mutation, since `RiskAssessor` snapshots policy at construction), and removing
the tool from the registry. A fourth test asserts every name in `ALLOWED_TOOLS`
is still tier-`safe` in the **live** registry, so re-tiering `open_app` tomorrow
fails the suite instead of silently creating a confirmation bypass.

Decision 5 is intact: no tier, gate, sandbox or audit behaviour changed — the
gate is a *throughput* bound and the fast path is *fewer* calls, not looser
policy.

### Two bugs found in my own new code (both by tests, before commit)

1. **`wait_for` cancels what it awaits.** My first timeout check was
   `if not ticket.done()`, but a cancelled future is `done`, so a timed-out
   waiter fell into the "I was granted a slot" branch and **ran anyway** —
   two bodies inside a gate of one, with `in_flight` lying. Now: granted means
   `done() and not cancelled()`. Pinned by
   `test_a_timed_out_waiter_does_not_run_as_if_it_held_the_slot`, which is the
   test I most expect someone to delete as "over-specified".
2. **A stub that answers once measures nothing.** The first version of
   `measure_fast_path.py` returned a tool call only on the very first
   `_ask`, so 31 of 32 turns degraded into single-generation prose and the
   "before" number was half the real cost (901 ms instead of 1813 ms). The
   simulation now alternates, and the comment says why.

### Explicitly *not* done, and why

* **No real-model numbers.** This machine has no `ollama` binary and no model, so
  every latency figure above is explicitly against a *simulated* 900 ms provider.
  The ratios travel; the milliseconds do not. `scripts/measure_fast_path.py` is
  the artifact to re-run on real hardware.
* **`max 100 sessions` was not lowered or connected to the gate.** The gate
  bounds work in progress; the session cap bounds state. Merging them would make
  an idle session cost a slot.
* **No priority classes.** A confirmation reply does not jump the queue behind a
  new prompt. Add it when someone is actually starved, not before.
* **The fast path was not widened** to `calculator`, `file_read`, `git_status` or
  anything with required arguments: each needs a *parser*, and a parser that
  guesses a path is how an answer becomes an action on the wrong file.
* **No `asyncio.timeout()` wrapper added around model calls** to make nesting
  "safer": wrapping a nested acquisition in `wait_for` moves it to a new task and
  *breaks* re-entrancy — my own test did that by accident, and the fix belongs in
  the caller's discipline, not in another layer.

### Warning-count correction, and a fixture that cost 85 seconds

§8 recorded "1 warning", measured on one run. It is not stable: across the runs of
this phase it was 1 or 2, the extra one being a `PytestUnhandledThreadExceptionWarning`
from an aiosqlite worker *thread* that finished after its event loop was closed.
Reporting a number from a single run as if it were a property was the wrong habit,
and the same flakiness is why the count is given as a range above rather than as a
figure.

What that investigation also produced is worth recording, because it is the more
general lesson: the first version of the suite-wide fix enumerated
`gc.get_objects()` in an autouse teardown to find every live `MemoryAgent`. It
worked — 0 spurious warnings — and it took the suite from 92 s to 174 s, because
the scan ran once per test across 2 400 tests. A hygiene fixture that doubles the
build is worse than the warning it removes. The final version keeps a
`weakref.WeakSet` populated by a `MemoryAgent.__init__` wrapper installed in
`tests/conftest.py`: O(1) per test, no production code changed to serve the tests,
and 89 s.

### Next (P5)

Agent-contract hardening: planning bounds, result verification, duplicate
side-effect prevention — the reminder work in §8 fixed the scheduler's version of
that; the engine's turn-level version is next.


---

## 10. P5 result — the turn contract (2026-10-11)

**Shipped:** three things the loop asserted but did not enforce, all in
`backend/agents/engine.py`, plus 18 tests in `backend/tests/test_agent_bounds.py`.

| Gap found by reading `run()` | Fix |
| --- | --- |
| one reply could dispatch an unlimited number of calls | `MAX_CALLS_PER_ITERATION = 8`, `MAX_CALLS_PER_TURN = 24`; excess is *reported* to the model as an observation, never dropped |
| a call that already succeeded could run again | `_bound_calls` refuses a repeat of the same `(name, canonical JSON params)` effect unless the tool is in `REPEAT_SAFE_TOOLS`; only successes are remembered, so a failed action stays retryable |
| "Done ✅" shipped over three `FAILED` observations | `_verify_claims` appends "N step(s) did not complete: …" in the user's language when the prose claims success and `failed` is non-empty, and records it in `LoopResult.errors` |

**Why the duplicate guard belongs here:** `open_app` is tier `safe`, so the approval gate
never stops it. A model that re-emitted its own call would really launch the app twice —
the same class of bug as the reminder reschedule in §8 (an action with two owners, a state
change the model gets for free by asking again).

**Deny-by-default must not be inverted.** `REPEAT_SAFE_TOOLS` lists tools *known* to be
harmless to repeat (`get_time`, `process_logs`, `file_read`, the git readers). Everything
else, including every tool added later, is treated as effectful. `test_the_repeat_safe_set_is_an_explicit_allow_list`
pins that shape, because flipping it into a deny-list of "dangerous" names would hand every
future tool the assumption that running it twice is fine.

**Audited and left alone:** observation length is already capped (`MAX_OBSERVATION_CHARS`,
4 000, with a truncation notice), and `max_iterations` is genuinely enforced with an honest
`stopped_reason` that `chatml.format_transcript` renders as a visible notice. Both were
verified rather than assumed.

**Verification**

| Check | Result |
| --- | --- |
| `pytest tests/test_agent_bounds.py` | **18 passed** in 0.4 s |
| mutation control (remove per-reply bound / disable duplicate guard / disable claim verification) | **2 tests red each** — all three guards load-bearing |
| full backend suite | **2446 → 2464 passed, 3 skipped, 0 failed** (101 s) |

**Two operational lessons, recorded because neither was a code bug.**

1. The new tests failed at first for an honest reason worth repeating: `"open spotify"` is
   answered by the P4 fast path *before the loop runs*, so a bounds test posting it asserts
   nothing about the loop. Fixed with the same documented isolation fixture
   `tests/test_agent_engine.py` uses (and a fixture in the other direction already covers the
   shortcut), not by loosening an assertion.
2. **Mid-phase the sandbox was rebuilt from the base commit**: `backend/.venv` and
   `frontend/node_modules` vanished, the local branch was rewound to `a01c7c9`, and 11
   previously-committed new files were missing from the worktree. The pushed tip was the only
   complete copy; recovery was `git fetch` → `git reset --mixed 8170b3d` → `git checkout --`
   of the missing paths, leaving the uncommitted P5 work intact (verified with
   `git diff --name-status FETCH_HEAD` before and `git status` after). One test run also hung
   for 600 s during that churn and never reproduced. **Rule for later phases: commit and push
   before starting work — untracked files here are not durable.**


---

## 11. P6 result — a skill's ceiling lives where the skill cannot reach (2026-10-11)

**Shipped** (5 files, +244/−9, plus `backend/tests/core/test_skill_policy.py`, 21 tests):

| File | Change |
| --- | --- |
| `tools/registry.py` | `register(..., floor=)` records a minimum tier in `_floors`; `assess()` applies it via `_apply_floor`; `effective_tier()` / `floor_for()`; `describe()`, `by_tier()` read the registry; floors are sticky and monotone, only `unregister` clears |
| `core/skills/tool_loader.py` | passes `floor=PERMISSION_CONFIRM`, keeps `_raise_floor`, wraps the whole per-tool step so one hostile tool cannot break the never-fatal promise, computes and reports policy gaps, honours `SKILL_STRICT_PERMISSIONS`, refuses a skill whose manifest cannot be read |
| `core/permissions.py` | `gaps_for()` + `SKILL_FLOOR_GRANTS` + `_names()` (str-Enum hashing trap) |
| `tools/base.py` | `permissions()` hook the `core/permissions.py` docstring had already promised but nobody had written |
| `config.py`, `.env.example`, `api/main.py` | `SKILL_STRICT_PERMISSIONS` (default `False`, documented in both files), one startup line summarising refusals and gaps |

**The hole, stated precisely.** `_raise_floor` edited `tool.permission` at registration and nothing
re-checked it. `assess()` reconciles the assessor's tables with the *live* attribute, so a skill
tool that rewrote its own tier — the natural "ask once then stop nagging" implementation — escaped
the gate on the next call. `""` escaped further: `if tool is None or not tool.permission` returned
the assessor's verdict alone, floor and all. Both cases are now tests, and both were verified red
by deleting the fix (M1: 4 failures, M3: 5).

**Enforcement direction.** Nothing here grants a skill more than it had. The gap check can only
*report* or *refuse*, and the manifest may be over-declarative without penalty. The loader still
cannot know what arbitrary Python does; what it can know is what the tool admits and what its tier
implies, and that is what is checked. `SKILL_STRICT_PERMISSIONS` defaults to off deliberately —
silently disabling a skill the user installed is its own regression — with strict mode as the knob
for a skills directory fed by someone else.

**Verification**

| Check | Result |
| --- | --- |
| `pytest tests/core/test_skill_policy.py` | **21 passed** in 0.5 s |
| M1 floor not applied in `assess` | 4 red |
| M2 `effective_tier` reads the object again | 1 red |
| M3 loader stops passing `floor=` | 5 red |
| M4 gap computation disabled / M4b gap not reported | 3 red / 2 red |
| M5 `_env_bool(…, "false")` (truthy string default) | 2 red |
| pre-existing skills + registry + sandbox suites | **316 passed, 3 skipped** |
| full backend suite | **2464 → 2485 passed, 3 skipped, 0 failed** (95 s) |
| `.env.example` ↔ `config.py` key audit | 50 keys, 0 missing |

**Two self-corrections worth keeping:** two of my first mutation controls were no-ops
(`sorted([]) or sorted(…)` still evaluates the real call; an anchor that did not match), which
would have "proved" the tests were vacuous. And three test stubs first failed for a legitimate
reason the file now encodes: `BaseTool.description`/`execute` are abstract, so a skill stub that
omits them never reaches the code under test. Both found by running, not by reading.


---

## 12. P7.1 result — `fetch_url`, and the address policy it cannot escape (2026-10-11)

**User decisions behind this phase:** research may fetch and loop (plan → search → read →
follow-up), `ddgs` stays the search provider with no new service required, claims are
*stamped* rather than refused (`[n]`, retrieved-at, stated method, and an "unverified" label
instead of deletion), and security skills are **passive by construction** — no port scans,
no active tools, whatever the authorization story. P7.1 is the piece everything else leans on:
the new network primitive, since that is the one with a security boundary inside it.

**Files**

| File | What it is |
| --- | --- |
| `backend/tools/netguard.py` (new) | URL and address policy: http(s)-only, no credentials in the URL, no whitespace, and **the address class of every resolved answer** refused before connecting — loopback, unspecified, link-local, reserved, multicast, and (unless `FETCH_ALLOW_PRIVATE`) private, with IPv4-mapped and 6to4 literals unwrapped and checked again |
| `backend/tools/fetch.py` (new) | `fetch_url`: GET only, per-hop redirect revalidation inside a hop budget, `Content-Length` and streamed-byte caps, content-type allow-list, HTML reduced to text, and a framing header on the output |
| `backend/config.py`, `backend/.env.example` | `FETCH_TIMEOUT_S`, `FETCH_MAX_BYTES`, `FETCH_MAX_CHARS`, `FETCH_MAX_REDIRECTS`, `FETCH_ALLOW_PRIVATE` (55 keys total, all present in `config.py`) |
| `backend/tools/registry.py` | `fetch` joins `build_default_registry()` |
| `backend/agents/engine.py` | `fetch_url` added to `REPEAT_SAFE_TOOLS` (P5's dedupe must not serve a truncated stale copy in place of a re-read) |
| `AGENTS.md` | the tier table's `safe` row, the file tree, and a paragraph explaining why a dialog is the wrong guard here |

**Three bugs found while writing it, all real:**

1. `pypi.org:443/project/httpx` — a dot is legal in a scheme name, so RFC-grammar scheme
   detection read `pypi.org` as the *scheme* and refused an ordinary URL. The fix
   recognises `host:port[/…]` explicitly instead of switching to a `"://"` test, which
   would have accepted `javascript:alert(1)`. A refused-URL test for `localhost:8080`
   pins the other side.
2. Spending the redirect budget fell through to rendering the last 302 — an empty body —
   as a **successful** read, handing the model a blank page it could cite. It is now a
   stated failure that names the last location.
3. `ToolResult.fail(error, output="")` takes `output` second, not `data`: four call sites
   were passing metadata dicts into `output`. Only visible by reading the constructor, so
   failure metadata now goes through the explicit constructor.

**Tier decision, recorded because it looks wrong at first glance:** `fetch_url` is `safe`.
The protection is the address policy, not a prompt; a confirmation on every page read is
clicked through within a day and then guards nothing. It is asserted in a test
(`test_registered_in_the_default_registry_as_safe`) so a future "gate all network tools"
sweep has to pass through here and decide on purpose.

**Verification**

| Check | Result |
| --- | --- |
| `pytest tests/test_fetch_url.py` | **55 passed, 2 skipped** in 0.6 s, with `-W error::PytestUnraisableExceptionWarning -W error::RuntimeWarning` |
| Live pair, opt-in (`ZAYTOON_LIVE_NET=1`) | **2 passed** — a real fetch of `pypi.org/simple/httpx/` (status 200, 54 373 bytes read, 600 kept, `retrieved_at` today, framing header present) and `http://127.0.0.1:11434/api/tags` refused as loopback **through the tool** |
| Shorthand loopback forms | `127.1`, `0x7f000001`, `2130706433` all resolve via glibc to `127.0.0.1` and are refused **by resolved address**, with the resolved value in the message |
| CI guards | free-only ok, local-first ok, `tests/test_packaging.py` 25 passed |
| Full backend suite | **2540 passed, 5 skipped, 0 failed** (101 s) — 2485 → 2540 |

**Residual, stated in the code:** DNS rebinding. There is a window between resolving and
connecting, and httpx 0.28 offers no way to pin a validated IP while still verifying the
certificate against the hostname, so this is mitigated (short connect timeout, GET only,
capped body, no execution, refusals reported) rather than eliminated. `FETCH_ALLOW_PRIVATE=false`
closes the combination that matters — a hostile name resolving to a local service — by
refusing the local service outright.


---

## 13. P7.2 result — the source ledger: stamping instead of refusing (2026-10-11)

**Files:** `backend/agents/sources.py` (new, ~300 lines with the reasoning),
`backend/agents/engine.py` (five touch points: `LoopResult.sources`, ledger creation,
`ingest` per outcome, `prompt_block` in the tool-results message, `stamp` on both the
answered and the budget-exhausted path), `backend/tests/test_sources.py` (19 tests),
`AGENTS.md`.

**The decision it implements.** Claims carry `[n]`, a retrieved-at time, and a stated method;
whatever cannot be stamped is labelled unverified rather than removed. That is a different
posture from P5's claim check but the same mechanism — **append the truth, never edit the
model's sentences** — and it is why `stamp()` returns `(text, notes)` instead of rewriting.

**What the ledger refuses to do:**

* **Take the model's word for a source.** Entries are built from `ToolResult.data` of
  `fetch_url` / `search_web` only. A URL the answer mentions is a claim; a URL the turn read
  is evidence. `test_a_failed_or_unrelated_tool_contributes_nothing` pins it.
* **Treat a snippet as a page.** Two kinds, and the citing rule the model is shown says
  dates/versions/numbers must come from a *read* page. `test_the_answer_gains_sources_and_method`
  renders "1 page(s) read in full, 2 snippet(s) not opened" — the honesty is the feature.
* **Mint a second number for the same document.** Dedupe by normalised URL (trailing slash
  folded); re-reading an already-seen result *upgrades* it in place and refreshes the stamp.
  Without this, a search→open sequence produces two citations for one document and the
  appendix lies about how much was read.
* **Become a way to fail a turn.** `ingest` swallows nothing silently but raises nothing:
  an unexpected tool shape means "no citation available", which is the right outcome anyway.
* **Show 30 links where the user's question should be.** The prompt list caps at
  `MAX_LISTED_SOURCES = 12` and says "(…N more result(s) not listed)" rather than truncating
  quietly.

**The one rule that is actually checkable:** `[n]` outside `1..count` — the model inventing
provenance — is called out in the answer and recorded in `LoopResult.errors`. Bracket style,
placement and "this claim should have had a source" are not mechanically decidable, so they
are not pretended to. A markdown link (`[text](url)`) is not a citation, and `test_markdown_links_are_not_citations`
keeps the regex from making it one.

**Verification**

| Check | Result |
| --- | --- |
| `pytest tests/test_sources.py` | **19 passed** in 0.1 s |
| A dedupe removed / B snippet counted as read / C fake citations unchecked | 3 red, 3 red, 3 red |
| D engine stops ingesting / E prompt block not shown | 2 red, 1 red |
| full backend suite | **2540 → 2559 passed, 5 skipped, 0 failed** (99 s) |

**Not done yet, on purpose:** the sources block is in the answer *text*, so both the web chat
and the desktop view render it as markdown; `LoopResult.sources` is not yet surfaced on the
REST/WS payloads, and there is no Sources panel in the dock. That is P7.3 (UI), together with
the multi-query research loop's prompt guidance for the ledger. `SettingsPanel` still ignores
`llm.probe`; R5/R6 deferred; the intermittent 1-warning unraisable in full-suite runs is
carried forward as a known flake (0–2 warnings across runs, no test depends on it).


---

## 14. P7.3 + P7.4 result — sources on the wire, and when to go read something (2026-10-11)

**P7.3 (transport).** `AgentEventType.SOURCES` + `EventBus.sources()`, emitted once after the
final prose, mapping in `_ProgressRelay` to `{"type": "sources", "sources": [...]}`, capped at
the same 12 the prompt block uses. **Nothing is gained by removing the text appendix** — the
prose carries the sources so every client (web, desktop, voice) shows them; the frame exists so
a client that wants a citation popover or panel does not parse prose back apart. A client that
predates the frame ignores an unknown `type`, which is the whole reason this is additive rather
than a protocol change.

**P7.4 (prompt).** Both system prompts now state the research discipline: search, then **open
the page** before quoting a number, date, version or name — a snippet is a lead, not a source;
two or three differently-phrased queries beat one re-run; cite `[n]` from the numbered list the
model is actually shown; anything it cannot place on it starts with `Unverified:`; never invent
a citation. The ledger's own block repeats the citation rule per turn, so the one behaviour worth
being strict about is said twice, while the rest of the answer stays the model's.

**Verification:** +3 tests (`tests/test_sources.py`, 22 total) — frame payload equals
`LoopResult.sources`; a tool-only turn emits no frame; the relay mapping is driven through the
real `_ProgressRelay` and asserts the cap and `trace_id`. Full backend suite **2562 passed /
5 skipped / 0 failed** (98 s). Frontend after the sandbox lost `node_modules`: `npm install`,
vitest **607 passed / 37 files**, `npm run build` ok — and re-verified after restoring
`frontend/package-lock.json`, which `npm install` had pruned 30 optional-platform entries from;
that edit was incidental to a phase about agent events and was reverted (`52ef33d`).
`AGENTS.md`'s WebSocket table gained its `sources` row; this section's claim about the frontend
was written before it was true and then made true by running it.

**Roadmap after P7:** P7.5 passive security skills (logs, configs, lockfiles, hashes and
certificates — no scanners, per the 2026-10-11 decision, and enforced by giving the skills no
active tool to hold), then P8 (OS matrix, `scripts/e2e_scenarios.py`, final diff review).


---

## 15. P7.5 result — passive security skills, checked rather than promised (2026-10-11)

**Decision implemented:** *"passive only, regardless of authorization"* — no scanners, no
active tools, whatever the authorization story. Delivered as three skills under `skills/`:

| Skill | Tool(s) | Reports |
| --- | --- | --- |
| `log-review` | `security_scan_logs` | auth failures, crash signatures, injection shapes, and leaked secret *shapes* (AWS key, JWT, private-key block, bearer token, password in URL) with `file:line`, masked (`AKIA…MPLE`) |
| `dependency-review` | `security_review_dependencies` | unpinned requirements, `git+`/`github:` sources, plain-`http://` registries, missing integrity hashes, npm `postinstall`-style hooks, `go mod replace` |
| `file-audit` | `security_hash_files`, `security_inspect_paths` | digests vs an expected value; mode, world-writable, setuid/setgid, executable bit, symlinks that leave the workspace, binary container sniff |

**How passivity is enforced.** `backend/tests/core/test_shipped_skills.py` parses every
shipped tool module and fails on: an import outside a ten-name allow-list; a call to any of
`os.remove/unlink/rename/replace/chmod/chown/symlink/system/popen/kill/fork/exec*/truncate`,
`shutil.*`, or anything from `subprocess`/`socket`/`http`/`urllib`/`requests`/`httpx`/
`webbrowser`/`pty`/`ctypes`/`ssl`; any `open()` whose mode contains `w`, `a`, `x` or `+`; any
`eval`/`exec`/`compile`/`__import__`. Plus the properties that rot quietly: every manifest
declares exactly `filesystem.read`, every skill installs **disabled**, the P6 gap check finds
nothing undeclared, `load_skill_tools` reports no errors and no policy gaps against the real
files, and the `confirm` floor still applies to their tools.

**Deliberate limits, printed rather than hidden.** No advisory database (that needs the
network, so `npm audit` is not what this is — the offline questions are still where most of
the real signal is). No certificate decoding: `cryptography` is not a dependency and shelling
out to `openssl` means whatever is on PATH decides; the tool says *"PEM certificate (not
decoded…)"* so a skipped check can never be read as a clean one, and there is a test with
that assertion in its failure message. And a flag is a question: `world-writable` is normal on
a temp directory and alarming on an SSH key, so the summary never claims compromise.

**One bug the tests earned:** the audit tools resolved a relative path for the containment
check and then read the *original string* — so `payload.bin` was checked against the workspace
and opened from the process cwd. The fix resolves once and uses the resolved path for
`stat`/`open`, keeping the user's spelling for display (`_resolve`'s docstring). A tool whose
check and its action address different files is the exact class of mistake this phase was
about.

**Verification**

| Check | Result |
| --- | --- |
| `pytest tests/core/test_shipped_skills.py` | **23 passed** (structural + functional, offline) |
| Installer, end to end in a temp data dir | 3 installed, `enabled=False`; discovery clean; nothing loaded while disabled; enabling 2 loads 3 tools with **no errors, no policy gaps**; `effective_tier == confirm` |
| full backend suite | **2562 → 2585 passed, 5 skipped, 0 failed** (97 s) |

**Files:** `skills/{log-review,dependency-review,file-audit}/` (9 files: `skill.json`,
`instructions.md`, `README.md`, `tools/*.py`), `backend/scripts/install_shipped_skills.py`,
`backend/tests/core/test_shipped_skills.py`, `AGENTS.md`, this section.

---

## 16. Phase 8 — three systems, one contract (2026-10-11)

**Decision 2 of the audit** said the product is Windows/macOS/Linux, and that the CI matrix
must show it. Two facts came out of the first pass over that claim:

| Area | Before P8 | Now |
| --- | --- | --- |
| CI matrix | `windows-latest`, `ubuntu-latest` — the OS the product is most likely to run on and the one the Docker image uses; no leg exercised a Mac at all | `windows-latest`, `ubuntu-latest`, **`macos-latest`** |
| `/system` resources | `GetGlobalMemoryStatusEx` (Windows) and `/proc/*` (Linux). The "unsupported platform" comment in the module header was, in fact, the platform the repo's own desktop shell targets most | same two, **plus** `vm_stat` + `sysctl hw.memsize/vm.page_size/kern.cp_time`; every branch returns `percent`/`used_bytes`/`total_bytes` so consumers do not branch on the OS |
| Opening an app | Start-Menu `.lnk` scan (Windows) + `shutil.which` | Windows and Linux unchanged; macOS resolves a **`.app` bundle** by name at depth one in `/Applications` + `~/Applications`, case- and space-insensitive, launched with `open` |
| Volume | one PowerShell script pressing `[char]175` **`level` times** — a *relative* nudge sold as an absolute set — and `[char]175` in both arms of the mute ternary (175 is volume-up, 173 is mute), so `mute=true` turned the volume **up** | backend probed: `pactl` → `amixer` → `osascript` → SendKeys. `level` is absolute (climb 100 up, then `100 - level` down, with the reasoning in the comment); mute is absolute on `pactl`/`amixer`/`osascript` and **refused** on Windows, where the key only toggles and nothing reads the state back |
| The E2E runner | "7/7 PASS", but only on a machine with a model running and only ever on Linux | nine scenarios; SKIP semantics honest with no model; the two platform-blind expectations (a `C:\Windows\…` "escape", an unrunnable confirmation gate) fixed per OS |

### The volume story is the one worth reading twice

The shipped code pressed the volume-up key `level` times: "set volume to 20" on a machine already
at 80% produced ~100% — and the original `GetVolume` never claimed it was relative, so the
*result text* was a percentage nobody had measured. The mute ternary's second branch was a copy of
the first, so muting raised the volume instead.

Windows genuinely has no volume API reachable without a helper binary, so a *relative* control is
the honest limit there. Two ways out: report the uncertainty, or convert the relative control into
an absolute one. The second is real: press **up 100 times** (the OS clamps at the ceiling, extra
presses are no-ops) and you know the position — then press **down `100 - level`** times and you
are at `level`. So `level` became absolute, `100 - level` became the arithmetic that matters, and
`mute` became a refusal with a reason, because a toggle whose state cannot be read is not a setter.
That reasoning is a comment in `_volume_script` (not a docstring the diff would have lost), and the
whole thing is pinned by a test that asserts `175, 100` and `174, 58` for level 42 — the
arithmetic, not the vibe.

### The macOS branches: real argv, faked answers

`open`/`osascript`/`vm_stat`/`sysctl` cannot run here, so `tests/test_posix_parity.py` fakes
`sys.platform`, `shutil.which`, `_MAC_APP_DIRS` and the output of the commands, and pins the
decision logic on Linux:

- **App resolution:** the `.app` scan is depth one, `os.path.isdir`, case- and space-insensitive.
  `NotAnApp.app` (a file) is not a bundle; a helper `Spotify.app` nested inside another bundle must
  not be what "spotify" finds.
- **Counters:** `vm_stat`'s labels end with `.` (a token test that does not strip it matches
  nothing and reports 0 — the first draft of the parser did exactly that), and the compressor line
  is `Pages occupied by compressor`, **not** `compressed`: matching the wrong key silently drops
  tens of gigabytes on a MacBook. Both are pinned, as is "used never exceeds total".
- **Degradation:** an OS with no counters reports `status: "unavailable"` + a reason on macOS,
  FreeBSD and SunOS alike; the first CPU sample is `sampling`, not `0%`.
- **Egress safety:** `volume_argv` coerces with `int()`/`bool()`, so `level="42; rm -rf /"` is
  refused — the argv never receives a caller string.

Seven mutation controls, each reddening at least one test (failures in brackets): drop the
compressor label from the macOS sum [4]; give `vm_stat` keys that differ from the Linux branch [4];
count the down-keys as `level` instead of `100 - level` [1]; let `volume_argv` trust a raw level
string [1]; clamp `pactl`'s 153% amplification to 100 [1]; match a `.app` name without `isdir` [1];
compare bundle names case-sensitively [3]. None of them needed a Mac to prove the logic was load-
bearing.

### The E2E runner, fixed for honesty rather than for greenness

The first live run after P8 was **5 FAIL / 2 PASS / 2 SKIP**. Both "new" failures were the script's
own assumptions, and the fix had to go one of two ways: adjust what is asserted per OS, or report
the platform limitation. The script adjusted the assertions *because the underlying behaviour was
correct*: on Linux `C:\Windows\x` is a legal filename **inside** the workspace (verified: the write
succeeded, the file was created in the workspace, `/etc/passwd`/`../`/`~/` were refused with 403 and
a reason), so the escape probe list is now per-OS. Scenario 3's confirmation gate is proven at the
registry — `POST /command/run` shares the path a WS turn uses, so a confirmation-required command
must come back denied with nothing on disk, and `echo hi & hostname` must be blocked. The gate
still bites without a model.

**Verification**

| Check | Result |
| --- | --- |
| `pytest tests/test_posix_parity.py` | **25 passed** in 0.13 s (offline, no macOS needed) |
| Mutation controls | 5 controls → each reddened ≥1 test (4, 4, 1, 1, 3) |
| Full backend suite | **2610 passed, 5 skipped, 0 failed** in 95.9 s |
| E2E live, backend on this box (Ollama stopped by design — no model downloads) | **5 passed, 4 skipped, 0 failed**; 8. OS capability parity PASS, 9. citation discipline SKIP (provider), 3. SKIP with "registry gate holds" |
| Frontend | untouched by P8; last full run 607 vitest + `npm run build` ✓ |

**Not verified here, and stated rather than claimed:** the macOS CI leg has never run — it exists so
that *someone else's Mac* answers whether `vm_stat`/`sysctl`/`open` behave as assumed on a real one.
The `osascript` mute/volume paths are argv-level only; the desktop shells for macOS/Linux are not
covered by any leg (`desktop` job is pinned to `windows-latest`); Windows SendKeys behaviour is
argued from the documented key codes, not from a Windows run — the sandbox has none.

---

## 17. Phase 9 — the backlog closed, and an audit of the whole tree (2026-10-11)

Five items were carried as "known divergences" because none of them broke a test. That is
what they had in common: each was the half of a contract no test was standing on. All five are
now either fixed or documented as what they are, and a full-project sweep was run at the same
time (results at the end of this section, because the audit is the more useful half).

| Item | State | What it was, concretely |
| --- | --- | --- |
| `sources` not in the `/chat` payload | **fixed** | the same turn had two levels of accountability: the WebSocket could say "these 2 pages, fetched at 05:49Z", REST could not ask at all |
| `open_url` not delegating to `netguard` | **fixed** (+ one real gap found on the way) | two copies of an address policy, one of them missing RFC 6598 |
| `REPEAT_SAFE_TOOLS` untied to the registry | **fixed as a check, not a derivation** | a rename/re-tier would have made it silently wrong in either direction |
| `SettingsPanel` ignoring `llm.probe` | **fixed** | "API key: present" in green while `/health` next door said `unreachable` |
| per-session `MemoryAgent.close()` | **verified, no defect** | the design shares one `MemoryAgent`; a per-session close would have closed SQLite under every other session — now pinned so nobody "fixes" it |

**Also fixed, unprompted, because it was in the path of the audit:**
`MemoryAgent._migrate`'s inner `except Exception: pass` around
`ALTER TABLE context_summary ADD COLUMN session_id` swallowed a failed migration; every later
query then assumed a column that was not there and failed somewhere far away. It now uses the
same guard-and-report idiom as the reminder columns next to it (`duplicate column` stays quiet,
anything else logs `op=migrate.context_summary.session_id`).

### The `/chat` fix, in full

`AgentService.dispatch` returns a **string**, and every transport gets its events from the
agent bus. Rather than widen that signature for one field, `run_turn` subscribes a
`_source_recorder` to the same bus the dispatch is given — the identical shape as the existing
`_tool_recorder`, and detached in the same `finally`. `TurnResult.sources` and
`TurnResult.to_dict()` carry it out, `POST /chat` returns it on **both** branches.

Two properties were tested rather than assumed, because both are easy to get wrong in exactly
this pattern: the recorders must be detached after *every* turn (a leftover would attribute
one turn's sources to the next, and grow by one subscriber per message), and detaching them
must not `unsubscribe_all()` — the session's `_ProgressRelay` lives on the same bus, and
clearing it would make every turn after the first go silent. The test counts subscribers on a
bus that had one of its own before the turn started, twice in a row.

Live proof on the running service (no model, so this was a fast-path turn):

```
POST /chat {"message": "hi there"} → 200
  keys: ['intent', 'lang', 'reply', 'sources', 'speak', 'text']   sources: []
```

### Whole-tree audit — what was swept and what it found

Method: mechanical greps for the escape hatches that matter, plus reading the result rather
than trusting the count.

| Question | Evidence | Verdict |
| --- | --- | --- |
| Does anything hand user input to a shell? | no `shell=True`, no `os.system`, no `eval`/`exec` outside `ast.literal_eval` in the call-argument parser; every subprocess is `create_subprocess_exec` with a fixed argv | clean |
| Do broad handlers hide failures? | 20 handlers whose body is only `pass`: 14 in `desktop/app/app.py` (the *unlaunched* Tk shell, each annotated `# pragma: no cover - <why>`), 5 annotated-defensive in backend, **1 real** — the `memory_agent` migration above | 1 fixed, rest justified |
| Are skipped tests a hiding place? | 5 `skipif`/`xfail` markers in `backend/tests`, each with a stated reason (platform-scoped assertions, opt-in live network) | clean |
| Is there drift in the TODO sense? | `grep -rn "TODO\|FIXME\|XXX\|HACK"` across backend/frontend/desktop → **0** | clean |
| Do the root docs tell the truth? | `DEPLOYMENT.md`, `QUICK_START.md`, `SETUP_INSTRUCTIONS.md`, `DETAILED_DOCS.md` and root `requirements.txt` were **0 bytes**. An empty `requirements.txt` is the worst variant: `pip install -r requirements.txt` *succeeds* and installs nothing | **fixed**: root `requirements.txt` is now `-r backend/requirements.txt`, and the four documents were written — each one only pointing at files, never re-stating a fact that can rot |
| Is the container path sound? | `frontend/Dockerfile` does `COPY certs/ /etc/nginx/certs/` and cites `scripts/generate-certs.sh`; `git ls-files frontend` contains **neither** → the image cannot build. Independently, nginx `return 301 https://$host:8443` while compose publishes `3443:8443` → `http://localhost:3000` redirects to a port nothing publishes | **reported, not blind-fixed** — no Docker in this sandbox, so any change would have been an unverifiable claim. Both are written up in `DEPLOYMENT.md` under "Known defects in the frontend image" with the smaller fix named |

The last row is the one to argue with. It was tempting to add the missing `generate-certs.sh`
and a `certs/.gitkeep` to make the build *pass*: that would convert a loud build failure into a
container that starts, fails to find its certificate, and crash-loops — strictly worse, and
unverifiable from here. An audit earns its keep by reporting the state of things, so this one
does, with the commands a reader can run.

**Verification**

| Check | Result |
| --- | --- |
| `pytest tests/test_p9_convergence.py tests/core/test_p9_sources.py` | **25 passed** in 1.19 s (19 + 6), offline |
| `pytest tests/test_tool_registry.py` (web tools rewritten) | 14 passed in the WebTools class, incl. the new refusal-parity cases |
| full backend suite | **2641 passed, 5 skipped, 0 failed** in 99.3 s |
| frontend | **613 vitest passed** (37 files; +6), `npm run build` ✓ 476 kB / 138 kB gzip |
| E2E, live against this box | **5 passed, 4 skipped, 0 failed** (unchanged from P8; the skips name the missing model and what was still verified) |
| Frontend mutation controls | ignore the probe → 3 red; force tone `ok` → 2 red; drop the latency → 1 red |

**Still open after P9** (each with its own reason, not by omission): the frontend container
defects above; `R5`/`R6` (deferred by decision); README's "2,300+ pytest tests" is a stale
floor rather than a lie; the desktop shell's silent handlers stay unguarded because that suite
cannot run in this sandbox.

---

## 18. Review route for P0 → P9 (how to read the whole diff in one sitting)

The branch is 10 commits on top of `a01c7c9`: **90 files, +11,011 / −311** before P9, and the
great majority of the additions are *tests*. That ratio is the argument for reviewing this
series at all — each phase's evidence is executable, so a reviewer can disagree with a
decision by running something instead of reading prose.

```bash
git fetch origin arena/fdb0f5cf-zaytoon
git log --oneline a01c7c9..origin/arena/fdb0f5cf-zaytoon     # one commit per phase
git diff --stat a01c7c9..origin/arena/fdb0f5cf-zaytoon       # where the weight is
git diff a01c7c9..origin/arena/fdb0f5cf-zaytoon -- backend/tools/netguard.py   # one file at a time
```

Suggested order, shortest route first:

| # | Read | Why it is worth your attention |
| --- | --- | --- |
| 1 | `AGENTS.md` diff | the rules changed: fetch tier, sources frame, skill floors, OS contract, `/chat` payload. Everything else should be checkable against these |
| 2 | `backend/tools/netguard.py`, `backend/tools/fetch.py` | the only genuinely new security surface in the series. Review question: is "one policy, two entry points" actually true, or did a second copy survive? |
| 3 | `backend/tools/system.py` (volume) | the shipped bug: relative key-presses sold as an absolute set, mute key copied into both arms. Review question: is the climb-then-descent reasoning sound on a real Windows box? |
| 4 | `backend/core/system_resources.py`, `backend/tools/applications.py` | the macOS branches, faked here and only *really* tested by the new `macos-latest` CI leg |
| 5 | `backend/skills/*` + `backend/tests/core/test_shipped_skills.py` | passivity enforced by an import/attribute allow-list. Review question: what does the AST guard not see? |
| 6 | `backend/agents/sources.py`, `agents/engine.py` diff, `api/websocket.py` | claim stamping and the `sources` frame; the engine diff is the one to read as a whole because the bounds interact |
| 7 | `backend/scripts/e2e_scenarios.py` | what "verified live" meant, and the two OS-blind expectations it had |
| 8 | `docs/*`, root `README`/`.env.example`, `desktop/*` | documentation truthfulness; the four 0-byte root docs |

**What a reviewer should refuse to accept**, and what the series therefore pins: a green test
suite whose security assertions were skipped by platform, an assertion that passes when the
feature is deleted (every phase here lists its mutation controls for exactly this), a "works
on my machine" claim about a platform the sandbox cannot run, and a swallowed exception.

Three things are knowingly unverified by anyone in this chain: `macos-latest` CI (never
executed), the Windows SendKeys semantics (argued from documented key codes + OS clamping),
and the frontend container (cannot build as committed — see §17).
