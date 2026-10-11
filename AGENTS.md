# Zaytoon — Agent Guide

## Startup

```bash
# Backend (must run from backend/ — sys.path depends on it)
cd backend
cp .env.example .env          # AI_PROVIDER=ollama by default (local, no key)
.venv\Scripts\activate && pip install -r requirements.txt
python main.py                # http://127.0.0.1:8000

# Frontend (separate terminal)
cd frontend && npm run dev    # http://localhost:5173
```

Prerequisites:
- **Ollama running locally** (`ollama serve`) with the configured models
  installed (`ollama pull qwen3.5:9b`, `qwen2.5-coder:7b`, `qwen2.5-coder:3b`).
  A missing model is reported as `MODEL NOT INSTALLED` with the exact pull
  command — Zaytoon never downloads a model on its own.
- **OpenRouter API key** — optional, only when `AI_PROVIDER=openrouter`.

Desktop (optional, Python/pywebview — hosts the React UI and spawns the backend):
```bash
cd frontend && npm run build              # produce dist/ for production mode
cd desktop && python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py              # or: python main.py --diagnose
.venv\Scripts\python main.py --dev        # connect to the Vite dev server instead
.venv\Scripts\python scripts\smoke_desktop.py  # live proof: real window + bridge + CORS
```

Root scripts (via `concurrently`):
- `npm run dev` — starts both backend + frontend
- `npm run test` — runs backend tests
- `npm run test:desktop` — runs the desktop shell tests

## Repo layout

```
backend/
├── main.py                 # Thin entry: sys.path.insert + uvicorn.run("main:app", ...)
├── config.py               # Env-based config (python-dotenv); workspace default
│                           #   and Config/.env loading delegate to core.paths
├── arabic.py               # Language detection, Egyptian markers, normalization
├── api/main.py             # FastAPI app factory, REST routes, CORS, lifespan
├── api/websocket.py        # WS handler, streaming TTS, session management
├── agents/                 # 8 agents: RouterAgent, FastAgent, PlannerAgent,
│                           #   CodeAgent, ToolAgent, MemoryAgent, SchedulerAgent, base
│   ├── engine.py           # AgentEngine: the bounded ReAct tool loop
│   ├── events.py           # AgentEventType + EventBus (the progress channel)
├── services/agent_service.py  # Intent dispatch → agent orchestration
├── services/llm_client.py  # OpenRouter client: free-only pool, capability
│                           #   routing, contract validation, attempt budget
├── services/model_registry.py  # FREE pool + per-model capabilities + selection
├── services/completion_validator.py  # Deterministic contract check (no LLM)
├── scripts/check_free_models.py      # Verify the pool against the live catalogue
├── scripts/verify_tool_capability.py # Re-probe the supports_tools claim
├── domain/models.py        # Enums (Intent, Language, AgentType), dataclasses,
│                           #   TokenBucketRateLimiter, SessionState, SpokenSentenceTracker
├── common/reporting.py     # the failure vocabulary: redact / safe_error / client_error
├── common/reporting.py     # redact() / safe_error() / client_error()
├── common/audit.py         # execution_audit table + recent/count/clear
├── scheduler/              # TimeParser, SchedulerManager
├── reminder/               # ReminderService, NotificationManager, ReminderData models
├── voice/                  # SpeechToText (faster-whisper), TextToSpeech (edge-tts)
│   ├── routing.py          # Voice registry + dynamic language/voice selection
│   ├── stt.py              # transcribe_detailed() → TranscriptionResult (language)
│   └── tts.py              # edge-tts, auto-selects voice per text/language
├── tools/                  # OSToolkit: WindowsCmdTool, WindowsFileTool, WindowsSystemTool
│   ├── sandbox.py          # Unconditional blocks: shell metachars, path containment
│   └── safety.py           # RiskAssessor + ConfirmationManager (human-in-the-loop)
├── memory/                 # ShortTermMemory, UserPreferences
├── core/                   # The core runtime: tasks, projects, skills, providers,
│   │                       #   workspace, git, permissions, events, secrets, paths
│   ├── paths.py            # THE portable layout — one answer per location
│   ├── db.py               # core.db schema + migrations (own file, see below)
│   ├── secrets.py          # DPAPI / 0600 credential storage; no route returns one
│   ├── events.py           # task.* / tool.* / model.* / permission.* vocabulary
│   ├── permissions.py      # 10 named permissions derived from the tool tiers
│   ├── orchestrator.py     # one turn → one task record, sequenced in one place
│   ├── tasks/              # models (lifecycle) | store | manager | graph (DAG)
│   ├── projects/           # detect | store | context (the per-turn preamble)
│   ├── skills/             # manifest | registry (skill.json packages)
│   ├── providers/          # capabilities | store | client | router
│   ├── workspace/          # files (service) | terminal | git (service)
│   └── api/                # tasks | projects | files | git | registry routers
├── tests/                  # 48 test files (19 of them tests/core/), pytest-asyncio
├── scripts/verify_final.py        # Free-only + voice routing + sandbox smoke check
├── scripts/smoke_confirmation.py  # Manual WS confirmation round-trip check
├── scripts/verify_execution.py    # Live probe: tools, audit, dedupe, processes
└── data/zaytoon.db         # SQLite (auto-created by MemoryAgent)
    data/audit.db           # Execution audit (separate file, see Execution layer)
    data/core.db            # Core runtime (a THIRD file — see below)
frontend/src/
├── hooks/useWebSocket.js   # WS connection, audio recording, VAD, state, confirmations
├── components/             # ChatMessage, MessageList, InputBar, SystemMonitor,
│                           #   ReminderPanel, ConfirmationDialog, HUD/, workspace/
│   ├── Chat/ActivityFeed.jsx  # Agent progress: plan + tool_started/finished log
│   └── SettingsPanel.jsx   # workspace root, API key, model chain, LLM metrics
└── styles/index.css        # Tailwind + CSS vars + animations
desktop/                    # Python/pywebview native host for the React UI
├── app/                    # host.py (WebView window), bridge.py (JS ⇄ Python),
│                           #   backend supervisor, single instance, protocol
├── scripts/build_exe.py    # PyInstaller onefile build
└── tests/                  # pytest suite (host, bridge, backend, legacy logic)
```

## Tool layer

One registry, one implementation per tool, one gate.

```
tools/
├── base.py          # BaseTool ABC (name/description/schema/permission/
│                    #   validate/execute), ToolResult, OSToolkit (legacy)
├── registry.py      # ToolRegistry: validate -> gate -> execute. THE chokepoint
├── filesystem.py    # read, write, list, create_folder, delete, move, search
├── terminal.py      # system_command, run_code, type_text/key_press
├── local_exec.py    # execute_local_command, execute_code (structured results)
├── processes.py     # process_start/list/logs/stop (tracked background procs)
├── metrics.py       # ToolMetrics: calls, failures, avg ms, by tool
├── applications.py  # open_app, close_app, resolve_windows_app
├── system.py        # get_time, system_info, calculator, get_volume, screenshot
├── web.py           # search_web, open_url
├── netguard.py      # URL + address policy for BOTH web tools: http(s) only,
#                    #   no internal addresses (see "one policy, two entry points")
├── fetch.py         # fetch_url: one bounded, guarded, labelled read of a page
├── git.py           # status, log, diff, branch_list | add, commit, checkout, push
├── coding.py        # create_project, run_npm, run_tests
├── media.py         # play_media (media keys)
├── safety.py        # RiskAssessor + ConfirmationManager
├── sandbox.py       # containment + shell-injection primitives
├── executor.py      # thin backwards-compatible facade over the registry
├── windows.py       # legacy platform tools (covered by the sandbox suite)
└── linux.py         # ditto
```

36 tools, listed in the agent prompt by generating them from the registry
rather than hand-maintaining them — the old hard-coded list omitted every tool
added since, which is how `file_read`/`file_write` came to pre-validate with no
handler behind them and return `"Unknown action"`.

**The ordering in `ToolRegistry.execute` is the design:**

    validate()  ->  risk gate  ->  tool.execute()

- **Validation first.** A malformed request gets a precise error instead of an
  approval prompt for something that could not have run anyway. Unconditional
  sandbox blocks (shell chaining, path escapes) are reported here as
  `blocked: true`, *never* as `requires_confirmation` — approving them would
  not help.
- **The gate second.** The tier comes from the tool's own `permission`
  attribute, reconciled with `RiskAssessor` by taking the **stricter** of the
  two. Honouring only the stricter direction was a real bug: `play_media`
  declares `confirm` but appears in no table, so the assessor returned `safe`
  and the keystroke tool ran with no prompt.
- **Execution last**, only after approval.

`ToolAgent` and `ToolExecutor` hold no tool logic — `ToolAgent` went 548 → 270
lines and `ToolExecutor` 626 → 157. `ToolAgent` runs the gate itself so its
decline/timeout notices can be bilingual, then passes `approval=True` to the
registry so the user is not asked twice.

`GET /tools` returns every tool with its schema and tier; `GET /health` reports
`tools_loaded` and `tools_by_tier`.

## Key quirks

- **Python path**: `backend/api/__init__.py` does `sys.path.insert(0, ...)`. Always `cd backend` first; never run `uvicorn` from repo root.
- **All agents** go through `services/llm_client.LLMClient`; they never build URLs or pick models themselves. The provider (`ollama` by default, `openrouter` optional) is chosen once from configuration, so there is a single request path and one contract check.
- **OpenRouter API key is required only for `AI_PROVIDER=openrouter`.** `LLMClient.build_chain()` raises a message telling you to set it when the key is absent; the Ollama path needs no key at all.
- **Keyword router**: Fast intent matching via regex before LLM fallback (skips ~80% of LLM router calls).
- **Rate limiter**: 60 req/min per IP (configurable via `RATE_LIMIT_MAX` / `RATE_LIMIT_WINDOW`). Stale entries cleaned every 5 min.
- **Session management**: max 100 concurrent sessions; sessions older than 30 min cleaned. WebSocket `"interrupt"` message cancels active response + TTS.
- **Tools whitelist**: `system_command` accepts any single command (argv, no
  shell), but shell metacharacters and chaining are refused outright. The
  legacy `OSToolkit` `cmd` tool still has a 10-command read-only allow-list and
  is only reachable through `tools/windows.py`.
- **Frontend WS proxy**: Vite proxies `/ws` → `ws://localhost:8000`. The hook uses `localhost:8000/ws` directly when on localhost, otherwise the serving port.

## LLM providers & fallback

**Two providers, one interface. `AI_PROVIDER=ollama` (default) or
`AI_PROVIDER=openrouter`.**

- **Ollama (default)** — local inference through `services/ollama_client.py`.
  No API key. Endpoint and the three tier models are env-configurable
  (`OLLAMA_BASE_URL`, `OLLAMA_GENERAL_MODEL`, `OLLAMA_CODING_MODEL`,
  `OLLAMA_FAST_MODEL`). Nothing is ever downloaded automatically: a missing
  model surfaces as `MODEL NOT INSTALLED: … ollama pull <model>`, and an
  unreachable daemon surfaces as `… ollama serve`. There is **no silent
  fallback to OpenRouter** in local-only mode.
- **OpenRouter (optional)** — the original free-only pool. Its zero-cost
  policy is unchanged and still enforced before any socket opens.

`services/ai_provider.py` owns the `AIProvider` interface (`OllamaProvider`,
`OpenRouterProvider`) plus health/status reporting for `GET /settings/ai`.
`services/model_registry.py` owns the OpenRouter free pool and its capabilities.
`services/completion_validator.py` owns the contract a completion must satisfy.
`services/llm_client.py` wires them to the transport; both providers run under
the same attempt budget, and the Ollama path (`_complete_ollama` /
`_stream_ollama`) validates the contract identically.

### The invariant

> Only a validated completion that satisfies the contract of the current request
> may become the final answer.

This is the whole design. An HTTP 200 is a successful *request*, not a
successful *answer*, and the difference is not theoretical — see below.

### `openrouter/free` is excluded, on purpose

It is free, but it is an **uncontrolled auto-router**: it resolves to a
different arbitrary free model on every call depending on upstream load. That
makes a routing bug unreproducible and a fix unverifiable. It also answered a
`get_time` request with the literal string `User Safety: safe`, which the loop
presented to the user as the assistant's answer.

`is_free_router()` recognises it **in order to refuse it**. It is not in
`MODEL_REGISTRY`, and `ProviderEndpoint` cannot even be constructed for it.

### Two separate policies, deliberately not merged

**Cost** — a model must carry the `:free` suffix. Non-negotiable, and enforced
again on the request path.

**Fitness** — a model must be allow-listed *and* declare the capabilities the
request needs. "Free" is not the same as "can be trusted to emit a valid tool
call".

`supports_tools` means *observed to emit a well-formed tool call in this
application*, not "the endpoint accepts a `tools` parameter" — OpenRouter lists
`tools` under `supported_parameters` for models that will happily answer a tool
request with prose.

| Tier | Head |
|------|------|
| `fast` (chat, tools, router) | `qwen/qwen3.8-27b:free` |
| `reasoning` (code, planner, scheduler) | `cohere/north-mini-code:free` |
| fallback pool, in order | `nvidia/nemotron-3-super-120b-a12b:free`, `nvidia/nemotron-3.5-lightning:free`, `cohere/north-mini-code:free`, `poolside/laguna-s-2.1:free` |

> **The roster is verified, not remembered.** An earlier draft was written from
> memory and four of its six ids returned HTTP 404 at request time — which reads
> like an outage rather than a config error. Run
> `python scripts/check_free_models.py` (availability against the live
> catalogue) and `python scripts/verify_tool_capability.py` (drives each
> candidate through the real tool contract) after editing `MODEL_REGISTRY`.
> OpenRouter's free roster rotates, so this is a recurring chore, not a one-off.

`google/gemma-4-31b-it:free` is allow-listed but **not** claimed tool-capable: it
was 429 on every probe, so the claim is unverified. Re-probe before promoting it.

`thinkingmachines/inkling-small:free` returns HTTP 403 for non-agentic clients
(re-confirmed 2026-09-28). Still allow-listed because that may change, but
`deprioritise`d so it is reached last — a 403 costs a wasted round-trip.

### Selection is deterministic, and configuration order wins

`Config.model_pool(tier)` is the tier head, then the configured fallback list,
then the legacy fallback variable, de-duplicated. `select_models()` filters by
free form, allow-list and required capabilities, and **preserves that order**.

An earlier version re-sorted by the registry's `rank`, which quietly overrode an
operator's configured order — a configured reasoning head could be pushed behind
a chat model with a better rank. Configuration is intent; `rank` is a default,
not an override. `ranked_pool()` exists for callers that want the registry's own
order and is used for nothing else.

The old chain ended in an auto-router, so the same request resolved to a
different model per call. Now the pool and its order are a pure function of the
configuration, which is what makes a routing bug reproducible.

`GET /health` reports `llm.routing` — the plan per tier and, for each skipped
candidate, **why** (`"lacks required capabilities: tools"`, `"uncontrolled
auto-router; excluded by policy"`, …). "Why did it pick that model?" is now
answerable.

### Fallback happens on transport *and* contract failure

```
primary 429
  → free model #2 200 but returns "User Safety: safe"   → rejected, benned
  → free model #3 200, valid                            → accepted
```

- **Transport**: 408/425/429/5xx/529, timeouts, connect errors. Fatal
  (400/401/402/403/404/405/413/422) skips the model immediately. A 429 moves
  straight to the next free model rather than retrying a contended one, and
  benches it for `LLM_RATE_LIMIT_COOLDOWN` (90s, longer than a 5xx's 60s).
  `Retry-After` is honoured, capped at 5s.
- **Contract** (`services/completion_validator.py`, no LLM in the path):
  `EMPTY_COMPLETION`, `INVALID_TOOL_UNKNOWN`, `INVALID_TOOL_ARGS`,
  `MISSING_TOOL_CALL`, `PROVIDER_METADATA_LEAK`, `AGENT_PROTOCOL_VIOLATION`.
  A model that answers wrongly is benned: a second identical request usually
  gets a second identical non-answer.

`_AttemptBudget` caps model calls across the **whole** chain
(`OPENROUTER_MAX_ATTEMPTS`, default 3), so `LLM_MAX_RETRIES` cannot multiply it
back out. This is what makes an infinite fallback loop impossible.

`CompletionContract` is built **per iteration**: iteration 1 may legitimately be
answered in prose ("you don't need a tool for that"), so prose is always
allowed. `require_tool` is set only when the model's own output showed it was
going to act and must be told about. A single global rule would either reject
every answer or accept every non-answer.

When the budget is spent, `AllModelsUnavailableError` (a `LLMError`, so existing
callers keep working) carries the `AttemptRecord` list, and the engine replies
with a controlled bilingual message. It never fabricates an answer and never
returns the last invalid completion.

### The validator's honest limit

**A fluent, confident, wrong paragraph of prose cannot be detected here.** "It's
half past three in Cairo" is structurally indistinguishable from "half past
three in London". This layer catches protocol violations and the observed
metadata-leak class. It is a real improvement, not a proof of correctness, and
claiming otherwise would be the more dangerous statement.

### Observability

`configure_logging()` installs a root handler at `LOG_LEVEL` (default INFO) from
`main.py`. Without it Python's root logger is WARNING-only and **every
successful model call was silently dropped** — failures looked fine and the happy
path was invisible.

One structured line per attempt:

```
LLM_ATTEMPT request=7fed7be92950 attempt=1/3 model=qwen/qwen3.8-27b:free
           status=200 validation=OK tool=create_folder caps=- latency=2936ms
```

`AttemptRecord` carries request_id, attempt, model, status, latency,
required_capabilities, validation verdict, tool_called and fallback_reason.
`LLMMetrics` counts `invalid` per verdict — the metric that would have caught
the `openrouter/free` problem, because a model that answers every request with
valid-looking garbage shows up there and not in `success`. Neither the API key
nor any Authorization material is ever logged; there is a test asserting it.

### Live verification

Against real OpenRouter, driving the real WebSocket:

- `get_time`: **6/6** served a real tool call.
- three-step *create folder → write file → read back*: correct confirmation
  prompts, real files, `file_read` (tier `safe`) correctly not prompted.
- when every free model was rate-limited, the user got *"All available free AI
  models are temporarily unavailable…"* — **not** a fabricated answer. This is
  the specific bug, fixed.

## Bilingual voice routing

`voice/routing.py` is the single voice registry. `arabic.get_tts_voice()` and
`TextToSpeech.resolve_voice()` both defer to it, so there is no second copy of
the mapping.

- **Arabic (Egyptian):** `ar-EG-ShakirNeural` → `ar-EG-SalmaNeural`
- **English:** `en-GB-RyanNeural` → `en-GB-ThomasNeural` → `en-US-GuyNeural`
  (cross-locale fallbacks are intentional, so speech survives a retired voice)

`config.TTS_VOICE_AR` / `TTS_VOICE_EN` override the primary per language.
`TextToSpeech.probe_voices()` asks edge-tts which voices it actually offers and
`resolve_voice()` walks the chain to the first one that exists.

**Dynamic switching.** Every TTS method takes an optional `language`; when it is
omitted the voice is inferred from the text itself
(`routing.detect_text_language`: Arabic-character ratio ≥0.15 **and** ≥3 Arabic
chars, plus Egyptian-marker detection). So the same session can read an English
reply in `en-GB` and an Egyptian reply in `ar-EG` with no state tracking.

**STT → TTS handoff.** `SpeechToText.transcribe_detailed()` returns a
`TranscriptionResult` carrying the language Whisper actually detected, and the
WebSocket layer passes it as `language_hint` into `_handle_message` and emits an
`stt_result` event. Whisper's verdict beats both the client's hint and
re-sniffing the transcript, so a short Arabic utterance is not misread as
English.

`POST /tts` is verified live in both languages
(`scripts/verify_final.py`).

## Tool safety sandbox (human-in-the-loop)

Two independent layers, because they enforce different things.

### Layer 1 — unconditional blocks (`tools/sandbox.py`)

These are refused outright; **no approval can override them**, because they are
injection attempts rather than legitimate actions.

- **Shell metacharacters / chaining.** `split_command()` rejects `&`, `|`, `;`,
  `` ` ``, `$(...)`, `>`, `<`, newlines, and `%VAR%` / `!VAR!` expansion.
  Every command path is now invoked with an argument vector and `shell=False`.
  This closes a **confirmed, exploitable hole**: `WindowsCmdTool` used to run
  `echo`/`dir`/`date`/`time` through `subprocess.run(command, shell=True)`, so
  `echo pwned & hostname` passed the allow-list and ran the chained `hostname`
  (verified — it returned the machine hostname before the fix).
  Shell builtins are dispatched via `cmd /c` with the validated argument list.
- **Path containment.** `is_safe_path()` uses `os.path.commonpath`, not
  `str.startswith`. With root `.../app`, the sibling `.../app_evil` is correctly
  rejected; `startswith` allowed it in all three file tools.
- **App targets.** `sanitize_app_name()` / `validate_app_target()` strip path
  traversal and reject script types (`.bat`, `.ps1`, `.vbs`, …). The target is
  passed as argv to `cmd /c start` instead of being interpolated into
  `start "<target>"`.
- **Shell interpreters for `run_code`** (`bash`, `sh`, `cmd`, `powershell`, …)
  are refused even after approval; `python` / `javascript` remain available.

### Layer 2 — human-in-the-loop gate (`tools/safety.py`)

`RiskAssessor` combines three things: a configurable `TOOL_ALWAYS_CONFIRM` list,
content patterns (`rm -rf`, `shutdown`, `del /s`, `curl | sh`, `taskkill`,
writes under `C:\Windows\System32`, killing `explorer.exe`, …), and a static
**permission tier** per action.

| Tier | Meaning | Examples |
|---|---|---|
| `safe` | runs without asking | `get_time`, `system_info`, `file_read`, `search_web`, `fetch_url`, `open_app`, `git_status`/`git_log`/`git_diff` |
| `confirm` | always asks, whatever the arguments | `file_write`, `create_folder`, `run_npm`, `system_command` (any command), `type_text`, `key_press`, `git_commit` |
| `dangerous` | asks, and is often refused outright | `file_delete`, `delete_folder`, `registry_write`, `run_code`, plus argument-dependent escalation (killing a system-critical process) |

**`fetch_url` is `safe` because a dialog would be the wrong guard.** A tool that reads
one URL on the model's word can be pointed at the machine itself — Ollama on
`127.0.0.1:11434`, a router admin page, and on a cloud VM `169.254.169.254`, which hands
out credentials. What refuses those is `tools/netguard.py`: http(s) only (with `host:443/path`
recognised as a host rather than a scheme, and `javascript:`/`data:` still refused), no
credentials in the URL, no whitespace, and **the address class of every resolved answer**
checked before connecting — so `127.1`, `0x7f000001`, `2130706433`, `::ffff:127.0.0.1` and
`127.0.0.1.nip.io` are all caught by what they resolve to, not by how they are spelled —
re-applied to **every redirect hop**. `FETCH_ALLOW_PRIVATE` opens the LAN and *still* refuses
link-local. A body is capped in bytes, rendered to text, and prefixed with a header saying it
is untrusted data and not instructions, because the payload that matters in a fetched page is
prose. Prompting per page instead would train the user to click through it. Known residual:
DNS rebinding (see the module docstring — httpx cannot pin a validated IP and verify the
certificate at once, so this is mitigated, not eliminated). Tests: `tests/test_fetch_url.py`,
including an opt-in live pair (`ZAYTOON_LIVE_NET=1`) that fetches a real page and asserts the
local Ollama port is unreachable through the tool.

**`open_url` runs the same policy, in its own words.** It used to keep its own scheme
checks, which is how two copies of an address policy drift: `fetch_url` refused
`169.254.169.254` while `open_url` would have opened it, and the page that *tells* the
model to "open this link for me" is fetched content. So `tools/web.py` calls
`netguard.parse_public_url` and hands the browser its **rebuilt** URL — host lower-cased,
default port dropped, so the authority that was judged is byte-for-byte the authority that
is contacted (the path is deliberately left alone: `..` resolution belongs to the browser,
and a security component rewriting paths is how you open something nobody asked for).
Refusals read "not *openable*" via `verb`/`action` kwargs, which exist so a message is
understandable, never to change a decision. `OPEN_URL_ALLOW_PRIVATE` defaults to **true**
where `FETCH_ALLOW_PRIVATE` defaults to false — `open_url` hands a URL to the user's own
browser at the user's own privileges ("open my router page"), while `fetch_url` makes the
request as the service. What does **not** change: rebinding is mitigated for `fetch_url`
(name resolved and re-checked before connecting) and *not* mitigated for `open_url`,
because the browser resolves it later. Found while writing this and fixed in the same
place: Python's `ipaddress` does not count `100.64.0.0/10` (RFC 6598, carrier-grade NAT)
as private, and that block is where some clouds serve instance metadata (Aliyun answers at
`100.100.100.200`) — it is now governed by the same flag as the private ranges, which
closes it for `fetch_url` by default without breaking a Tailscale user's NAS.

**Two invariants are enforced structurally, and both are tested:**

1. **The gate is never looser than the tier.** Every return path from
   `assess()` goes through `RiskAssessor._finalise()`, which synthesises a
   reason when a non-`safe` action classified no hazard. Without it, a branch
   could label an action `dangerous` while `requires_confirmation` stayed
   `False` — the UI would promise a prompt that never appeared. (The reverse,
   gate stricter than tier via an extra pattern match, is intended layering.)
2. **Unconditional blocks precede the gate.** `ToolExecutor._prevalidate` runs
   `split_command` before the risk gate, so shell chaining is refused with
   `blocked: true` and *never* as `requires_confirmation` — confirming it would
   not help. Approving one command must not authorise what follows a separator.

`system_command` is confirmation-required for **every** command. It used to be
content-inspected only, so `echo hello` ran with no prompt, on the reasoning
that a harmless command needs no approval — but any command reaches an
interpreter, so the harmless case is exactly what an attacker would use to reach
a harmful one. The pattern rules still run and still layer their reasons on top.

### No ungated tool path

The WebSocket `command` message and `AgentService.execute_command` were
**removed**. They reached `OSToolkit` with neither `RiskAssessor` nor
`ConfirmationManager`, and a WebSocket handshake is not subject to CORS, so any
web page the user visited could open `ws://127.0.0.1:8000/ws` and drive them.
Both removal points carry an explanatory comment, and
`test_workspace_and_injection.py` parses the AST to assert they stay gone.

### Keystroke injection

Keystroke payloads are delivered with `powershell -EncodedCommand`, so the
payload is base64 UTF-16LE and never appears on a command line. The previous
`powershell -Command "...SendWait('<text>')"` form was injectable at a
non-blocking tier. Note the deliberate asymmetry: `type_text` escapes SendKeys
metacharacters (they are literal data) while `key_press` does not (`+`/`^`/`%`
are the modifier syntax — escaping them would break `ctrl+c`).

### Gate mechanics

- High-risk calls are **not** executed. `ConfirmationManager` emits a
  `require_confirmation` WebSocket event and blocks on a future; the client
  replies with `tool_confirm {request_id, approved}`. Timeout, disconnect, or
  an error all **deny** (fail closed).
- The gate runs *before* the ToolAgent result cache, so a cached result can
  never bypass an approval, and *after* argument validation, so a malformed
  request gets a precise error rather than an approval prompt.
- Applied at two chokepoints: `ToolExecutor.execute` and `ToolAgent` — plus
  `POST /command/run`, which denies rather than executing because REST has no
  approval channel. `AgentService._execute_tool_commands` is **test-only**; do
  not wire it back into a request path.
- Bilingual notices (`en` / `ar`) for decline, timeout, and no-channel cases.
- Manual check: `python scripts/smoke_confirmation.py deny`.
- **`POST /chat` is the same turn, minus the stream.** Its payload is
  `{reply, text, lang, intent, speak, sources}`, and `sources` is the same list the
  `sources` WebSocket frame carries (`n`, `kind`, `url`, `host`, `title`, `retrieved_at`) —
  captured in `Orchestrator.run_turn` by subscribing to the agent bus the dispatch is given
  (`_source_recorder`), the same trick the tool recorder uses. Widening
  `AgentService.dispatch`'s return type for one field would have been a breaking change to
  every caller for no gain. Empty means *no page or snippet was read this turn*, which is a
  fact about the turn, not a missing value — so the key is present on the error path too, and
  a client never has to guess which shape it got. `TurnResult.to_dict()` carries it for the
  task-record views.

## Anti-hallucination / deliberation prompts

`agents/prompts.py` holds a shared grounding block appended to every reasoning
agent. It forbids inventing file paths, commands, APIs or values; mandates
`NEEDS VERIFICATION` / `«محتاج أتحقق»` when uncertain; forbids claiming an
action was performed; and states that execution happens through the gated tool
layer.

`PlannerAgent` and `CodeAgent` (both `TIER_REASONING`) add a mandated method —
understand → audit → steps/code → **verify** → risks — and `PlannerAgent` is
explicitly told it plans but does not act.

## Execution layer: concurrency, metrics, audit, processes

### Independent tool calls run together; gated ones are barriers

`AgentEngine._run_batch` replaces the old `for call in calls:`. Within one model
turn it walks the calls and splits them into **runs of consecutive safe-tier
calls** (concurrent, `asyncio.gather` under `asyncio.Semaphore(4)`) and
**barriers** — every `confirm`/`dangerous` call runs alone, in the order the
model wrote it.

The barrier rule is the point, not the parallelism. Outcomes are stored
positionally, so `observations`, `tool_calls` and `failed_calls` describe the
sequence the model asked for. If a read were allowed to overtake the
`create_folder` before it, the observation list would claim the read happened
after the folder existed while its own text said "not found", and the model
would reason about an impossible sequence.

`gather` is used **without** `return_exceptions`, so a `CancelledError`
(interrupt) reaches the children — but each call converts its own exception into
a structured failure, so one failing tool can never cancel its siblings.

Proven deterministically in `tests/test_parallel_tools.py` (three safe calls
cost one call's worth of time, the cap, barriers, ordering, failure isolation).
The live probe cannot prove it: real tools finish in milliseconds.

### Two tool-call dialects that both *benched* a working model

Two live bugs, both found by running it rather than reading it:

- **Aliases were invisible to the contract.** `file_read` declares the aliases
  `file_list`, `list_files`, `list_directory`, and the registry resolves all of
  them — but `contract_from_registry` built its tool map from *primary names
  only*. A model that reached for `file_list` was rejected as
  `INVALID_TOOL_UNKNOWN`, spending the whole attempt budget on a name the
  executor would have served. Aliases are now in the contract, mapped to the
  same shape; a primary name still wins over another tool's alias.
- **A wrong tool name was fatal to the turn.** The validator's verdict happens
  one layer *before* the observation loop that exists to correct exactly this.
  The user got "all available AI models are temporarily unavailable" — a
  sentence about the provider — for what was a recoverable slip. The engine now
  builds its contract with `allow_unknown_tools=True`, so the call reaches
  `ToolRegistry.execute`, which refuses it **by name** and hands the refusal
  back as an observation the model corrects from. The strict default is
  unchanged for every other caller, and what can *execute* is unchanged.

### Errors are reported centrally

`common/reporting.py` owns the failure vocabulary: `redact()` (masks key-shaped
strings and the values of sensitive config), `safe_error()` (one structured log
line — `ERROR op=… comp=… type=… ts=… msg=…` — plus a safe message) and
`client_error()` (short bilingual user-facing text). Wired into the WebSocket
handler, the engine, the registry, memory, scheduler, reminders and voice.

The registry change is the visible one: `validate()` and `execute()` no longer
echo `str(e)` to the client, which was the one place a traceback fragment, an
absolute path or a command line could reach the transcript.

### Tracked background processes

`tools/processes.py` exists because a dev server has no deadline and
`execute_local_command` requires one. **Only processes Zaytoon started are
tracked**: a pid enters the registry solely because `start()` spawned it,
nothing is discovered by scanning the process table, and `stop()` refuses an
unknown id. That refusal is the security property and is the first thing the
tests assert. Output is captured to a 500-line ring buffer, redacted on the way
in. REST: `GET /processes`, `GET /processes/{id}/logs`, `POST /processes/{id}/stop`.

### Execution audit log

`common/audit.py` writes one row per tool invocation (tool, command, cwd, risk
tier, approval decision, success, exit code, duration, error) with everything
`redact()`ed first. REST: `GET /audit`, `DELETE /audit`.

It has its **own** SQLite file (`data/audit.db`, override with
`AUDIT_DB_PATH`). The first version shared `zaytoon.db` with the memory agent,
and clearing the audit page from the UI failed with `database is locked` after
a 3s busy timeout *and* three retries — the memory agent holds that connection
open for the life of the process, so the contention was structural, not
transient. A shared file sounded simpler and was not.

A failing audit write must never break a turn: every function swallows its own
errors and logs once.

### And so does the core runtime (`data/core.db`, override with `CORE_DB_PATH`)

Same lesson, third application. The task records, project registry, skill table
and provider registry are **not** in `zaytoon.db` (which the memory agent holds
open for the life of the process) and **not** in `audit.db`. Three files, three
owners. `tests/core/test_core_paths.py` asserts `audit._db_path() !=
db.db_path()`, so a well-meaning consolidation cannot reintroduce the lock.

### Duplicate requests are dropped by id, not by text

Content-based duplicate detection cannot catch a resend — the retry arrives
*before* the first turn produced content to compare. Clients may attach
`client_msg_id`; the server keeps a per-session, TTL-bounded, cap-bounded set
and drops a resend **before** it can cancel the in-flight turn. A frame with no
id behaves exactly as before, so the desktop shell is unaffected.

### Heartbeats and metrics

A tool running longer than `TOOL_HEARTBEAT_S` (2s) emits `tool_progress` from a
task cancelled in the call's `finally` — a heartbeat that outlives its tool is
a permanent lie on the wire. `tools/metrics.py` counts calls, failures and
average duration per tool and appears at `GET /health` under `metrics.tools`.

## Portable layout (`core/paths.py`)

`ZAYTOON_HOME` (set by the generated launchers) selects the layout. Resolution
order for every location is: **explicit env var → `ZAYTOON_HOME/<Dir>` → the
checkout's own `backend/data`**.

- **Never writes.** Resolution is called from import-time health checks, and an
  import that touches the filesystem is a side effect nobody can find.
  `ensure()` is the only function that creates, and every caller asks for it.
- `config_dir()`, `workspaces_dir()` and `skills_dir()` return **`None`** in a
  checkout. That is the meaningful answer, not a convenience: a plausible-looking
  wrong path sends someone looking for a file that does not exist, and a
  `workspaces_dir()` path would silently change the default from
  `~/ZaytoonWorkspace` to inside the source tree.
- `config.py` delegates its workspace default here and loads
  `ZAYTOON_HOME/Config/.env` with `override=False` — a variable already in the
  environment was set deliberately, so the file must not overwrite it.
- `secrets_path()` keeps `backend/data/secrets.json` for a checkout (an existing
  install must not appear to lose its keys) and uses `Config/` only for a portable
  one.
- **Do not add a second resolver.** Two modules disagreeing about what a path
  means is a containment hole, and this codebase has already paid for it once —
  `tools/sandbox` had to be rewritten for exactly that.

`install.sh` and `install.ps1` create `Application/ Config/ Data/ Models/ Skills/
Workspaces/ Logs/ Cache/`, symlink (junction on Windows) `Application/{backend,
frontend,desktop}` at the checkout, and write `Config/.env` **only if absent** —
overwriting it would destroy the user's API keys and workspace choice on a
reinstall, the single most destructive thing an installer can do. `Application/`
is linked rather than copied because a copy installs a stale tree that then looks
like a mystery bug while the user edits the checkout.

Neither script downloads an AI model — pulling gigabytes is a decision, not an
installation step — and a missing dependency is a **warning rather than an
abort**: an installer that stops at the first absent tool leaves a
half-installed system.

## Tests

```bash
# backend (2,300+ tests; ~95 s)
cd backend
.venv\Scripts\activate && python -m pytest tests/ -v

# desktop shell (549 tests — includes the retained legacy suites; ~4 min,
# test_audio_pipeline.py decodes real MP3 chunks)
cd desktop
..\backend\.venv\Scripts\python.exe -m pytest tests/ -v

# frontend (607 tests + lint + build)
cd frontend
npm run verify          # lint && test && build
```

- All Python tests are async (pytest-asyncio). SQLite DB auto-created in `data/`.
- Tests skip API-dependent tests when OpenRouter is unreachable.
- `tests/test_llm_client.py` mocks all HTTP (including SSE) — no network needed.
- `tests/test_websocket_confirmation.py` drives the real WS handler with a fake socket.
- `tests/test_agent_engine.py` — the loop: all three tool-call dialects, the
  malformed-reply guard, observations, the iteration budget, provider/tool
  failure recovery, declined confirmations, and the guarantee that intermediate
  model output is never a response chunk.
  `frontend/src/hooks/useAgentEvents.test.jsx` covers the same events client-side.
- `tests/test_router_routing.py` — routing **order**, not regexes. See above.

> **A caution earned the hard way.** A passing suite is not the same as a
> working product. `test_catalogue_shows_required_parameters` was green for
> several commits while the tool catalogue handed the model
> `<bound method Calculator.name of <Calculator ...>>` for every tool — the
> assertion passed only because the test double's `__repr__` contained its
> name. Everything downstream was broken and nothing noticed. The replacement
> asserts `"bound method" not in catalogue` and runs against the real
> the real 36-tool registry, because a double that can make a broken thing
> look fine is
> worse than no double. Run the app.
- `tests/test_free_models.py` — free-model predicate, endpoint + payload guards,
  and an exploding HTTP client proving no request is attempted for a billable model.
- `tests/test_free_routing.py` — the FREE routing contract: primary succeeds;
  429 → next configured model; **200-but-invalid → rejected and retried on
  another model**; tool-required turns; invalid arguments; unknown tools;
  all-fail → controlled error; paid-model protection; and the attempt ceiling
  not being multiplied by `LLM_MAX_RETRIES`.
- `tests/test_voice_routing.py` — locale normalisation, text-based language
  detection, voice profiles, dynamic switching, STT language reporting.
- `tests/test_security_sandbox.py` — shell-injection payloads, path containment,
  app-target validation, risk gate, denied code languages.
- `tests/test_workspace_and_injection.py` — workspace-root resolution, the
  PowerShell `-EncodedCommand` fix, and the removed ungated WS command channel.
- `tests/test_tool_registry.py` — the tool contract (self-describing, schema
  declares what it requires, aliases resolve), the validate→gate→execute
  ordering, and every new tool against a real temp workspace.
- `tests/test_api_routes.py` — `/health`, `/tools`, `/settings/workspace`,
  `/tasks`, `/command/*`, `/processes`, `/audit`. Added after a one-line edit to
  `health()` removed the `get_llm_client()` call it depends on and nothing
  caught it.
- `tests/test_parallel_tools.py` — the concurrency contract: cost, cap,
  barriers, order, failure isolation.
- `tests/test_tool_heartbeat.py` — `tool_progress` cadence and cancellation.
- `tests/test_process_manager.py` — ownership first (an untracked id cannot be
  stopped), then lifecycle truthfulness against real child processes.
- `tests/test_audit_log.py` — mostly the *failure* path: a broken table, an
  unwritable path and a lock must never break a turn.
- `tests/test_request_dedupe.py` — `client_msg_id` scoping, TTL and cap.
- `python scripts/verify_execution.py` — live probe over the real WebSocket
  (tool turns, audit attribution, a resent id, the process endpoints). It
  reports rather than fails on tool overlap and heartbeat cadence: real tools
  finish in milliseconds, so those two are proven by the suite instead.
- `tests/test_summarization.py` — guards the abstract-`BaseAgent` silent failure.
- `tests/core/` — the core runtime, 16 files:
  - `test_core_paths.py` — layout precedence, and the three `None` answers.
  - `test_core_git.py` — **real repositories** in a temp workspace: containment,
    porcelain `-z` parsing (a file named `my notes.md` and `ملف.md`), NUL-separated
    log records, remote vs local branches, and the REST status mapping. Skipped,
    not failed, when git is absent.
  - `test_core_orchestrator.py` — one turn → one task record; the preamble is built
    **before** routing ("run the tests" means `pytest` or `npm test` depending on
    the project, and a router told afterwards has already chosen); cancellation
    reaches both the work and the record.
  - `test_core_db.py`, `test_core_secrets.py`, `test_core_events.py`,
    `test_core_tasks.py`, `test_task_graph.py`, `test_core_projects.py`,
    `test_core_skills.py`, `test_core_providers.py`, `test_core_workspace.py`,
    `test_core_terminal.py`, `test_api_core.py` (files, projects, terminal,
    tasks, reminders, providers, models — one file because they share the app
    fixture and the unified `{error}` shape assertions).
- `frontend/src/hooks/useWebSocket.test.js` — the WebSocket protocol reducer,
  the seam both the browser app and the desktop shell depend on.
- `python scripts/verify_final.py` — live smoke check of all three areas. It is
  a **policy** check, so it must be updated whenever a permission tier changes.
- **No pre-existing failures.** The 50 failures previously in
  `test_desktop_config.py` were tests for a Tauri/Rust shell that no longer
  exists; they were replaced by `desktop/tests/test_desktop_shell.py`.

## Frontend

React 18 + Vite 5 + Tailwind 3 + GSAP. **JavaScript/JSX, not TypeScript** — no
`tsconfig.json`; this was a deliberate decision (see "Increment 1.5" below).

- **The state vocabulary is a pure module, not JSX.** `lib/statusLabels.js`
  derives the status sentence ("Understanding…", "Planning…", "Calling
  `<tool>`…", "Generating response…", "Speaking…") with a documented precedence
  order; `StatusLine.jsx` renders it with `role="status"` and
  `aria-live="polite"`. `lib/toolRows.js` folds activity events into one row per
  tool invocation, so several concurrent tools display independently with
  RUNNING/DONE/FAILED chips. `lib/risk.js` maps a permission/level pair to a
  tone — and an unrecognised value grades **up** to DANGEROUS, never down.
- **Audit and processes are optional capabilities.** `ExecutionHistory.jsx`
  treats a 404 as "unavailable" and hides the processes card rather than showing
  an error, so the page works against a backend that predates the endpoints.
- **`components/workspace/` is the application shell.** `WorkspaceShell.jsx` holds
  three panels — *what is this project* (left), *what are we doing* (centre),
  *what happened* (right). The centre survives every width; the rails collapse
  into one drawer below 1280px. Below the breakpoint the drawer's single tab list
  carries **both** rails' tabs, because hiding the task list and the terminal on a
  phone loses the two things a user needs while work is running. `WorkspaceShell`
  has one `inspector` function used by both layouts rather than two switches —
  the previous version had two, and the drawer had already lost the task list.
- **`components/workspace/GitPanel.jsx` distinguishes its failures.** A 404 is
  "not a repository here" and hides the panel; a 403 is a containment decision
  with no retry; a 409 shows git's own words, which are specific in a way a
  paraphrase would not be. The commit input and the error banner are separate
  state — sharing one means a failed commit eats the user's half-typed message.
- **Outbound messages carry `client_msg_id`** (`lib/ids.js`, `crypto.randomUUID`
  with a manual fallback) so the server can drop a resent frame. A client that
  omits it behaves exactly as before.

- `src/hooks/useWebSocket.js` — the whole client state machine and the single
  place the WebSocket protocol is implemented. `isSocketOpen()` is the one
  place that decides whether a write is safe; do not re-inline
  `wsRef.current?.readyState === WebSocket.OPEN` (reading a ref twice like that
  is a TOCTOU hazard — see the note on the helper).
- `src/hooks/useWebSocket.test.js` — the protocol reducer. Two clients depend on
  this contract, so it is the highest-value frontend test.
- `src/lib/api.js` — REST client (`/health`, `/chat`, `/stt`, `/tts`,
  `/command/*`, `/settings/workspace`).
- `src/components/SettingsPanel.jsx` — workspace root, API-key presence, the
  model actually in use, the fallback chain, LLM metrics, voice + safety status.
- `src/components/ConfirmationDialog.jsx` — renders the `permission` tier and
  the exact parameters. The `<pre>` block is `dir="ltr"` so an RTL UI cannot
  reorder a command the user is approving.

### Orphaned components (resolved; the list is now empty)

Eight root-level components existed but were imported nowhere, which is why the
app had **no text input at all** and no reminders UI. `InputBar`,
`ReminderPanel` and `SystemMonitor` are wired. The rest — `ChatInterface.jsx`,
`VoiceControls.jsx`, `StatusDot.jsx`, `VoicePanel.jsx`, `Chat/VoiceButton.jsx`,
`useGsapTimeline.js`, and the empty `App.css` / `index.css` / `App.jsx.css` —
were deleted, so `npm run lint` reports zero warnings. That is what makes a new
warning mean something.

Import **casing** is a portability matter here, not a style one: the HUD
components live in `components/HUD/`, and a `components/hud/...` specifier
resolves on Windows and fails to build on Linux.
`src/imports.case.test.js` asserts exact casing for every relative import,
because the build that mattered was the one nobody ran.

## Workspace (file-tool containment)

Every file tool is confined to `config.WORKSPACE_ROOT`, defaulting to
`~/ZaytoonWorkspace` — **not** `os.getcwd()`. The backend must be launched from
`backend/`, so a cwd-based root confined the assistant to its own source tree and
made project creation, repository inspection and git operations impossible.

- `GET /settings/workspace` — current root plus a directory listing.
- `POST /settings/workspace {"root": "..."}` — repoint at runtime; persisted as a
  preference. A path that cannot be created is rejected, never silently ignored.
- `sandbox.resolve_workspace(path)` is the single definition of "resolve a path
  and decide whether it is inside the root". `sandbox.is_safe_path()` delegates
  to it. They previously disagreed — one used the cwd, the other the workspace —
  so a relative path could be validated against one root and opened against
  another. **Do not reintroduce a second resolution path.**

## Arabic / Egyptian

- `arabic.py`: >15% Arabic Unicode chars → detected `"ar"`; else ≥1 Egyptian marker → `"ar"`; else `"en"`.
- `clean_and_detect()` detects language WITHOUT normalizing the text.
- Agent prompts must include `رد بالعربي فقط. ممنوع الرد بالإنجليزية.` on Arabic paths.
- TTS voices: `ar-EG-ShakirNeural` / `en-GB-RyanNeural` (configurable in `.env`).
- Scheduling: `TimeParser.parse_time()` runs first (no LLM for simple reminders).
- `TTS_STREAM_EARLY=true` (default): starts TTS as soon as first complete sentence (>15 chars) arrives.

### Time parsing (`scheduler/parser.py`)

`TimeParser.parse_time(text, now=None)` resolves in a single pass so a day
offset and a time-of-day can never be counted twice:

1. **Normalise** — Arabic-Indic digits, then `arabic.normalize_arabic`.
2. **Self-contained relative** — `in 2 hours`, `بعد 3 ايام` (`_match_from_now*`).
3. **Structured** — resolve the day once (`_parse_day`) and the time-of-day once
   (`_extract_time_of_day`), then combine (`_resolve_day_and_time`).
4. **`dateparser` last resort** — only when steps 1–3 understand nothing.

Rules that matter when editing this file:

- **Day keywords come in two spellings.** `normalize_arabic` folds `ة → ه`, so
  `الجمعة` arrives as `الجمعه`. `_with_normalized_variants()` adds the folded
  form to every table. Without it, ة-final Arabic day words silently miss and
  the reminder is never created at all.
- **`RELATIVE_DAYS` is separate from `ARABIC_DAYS`.** Relative keywords
  (`بكرة` = +1 day) must be literal offsets; mixing them into the weekday map
  made "tomorrow" resolve to the next Tuesday.
- **Longest match first** so `بعد بكرة` beats `بكرة` and `after tomorrow` beats
  `tomorrow`.
- **`dateparser` must stay last.** It used to run first and win, resolving
  `monday at 9` a year out and defeating the Arabic-aware matchers.
- **A bare number is never a time.** `_extract_time_of_day` requires a colon, a
  meridiem marker, or the `الساعة` / `at|by` prefix, so `in 3 days` cannot be
  read as 03:00.
- **Roll forward only when the date is implicit.** `friday at 9` must stay on
  Friday; `today at 5pm` at 20:00 correctly becomes tomorrow.

Regression coverage: `tests/test_scheduler_regression.py` (92 tests), including
a test asserting `dateparser` is *not* consulted for any phrasing we understand.

## Agent dispatch

```
RouterAgent.route() → AgentService.dispatch()
  ├── FastAgent       (chat / default)
  ├── PlannerAgent    (complex)
  ├── CodeAgent       (code)
  ├── ToolAgent       (tools / action)  → ToolRegistry
  ├── SchedulerAgent  (scheduling → TimeParser → ReminderService)
  └── MemoryAgent     (memory retrieval)
```
Intent enum: `chat | code | tools | scheduling | memory | complex | action`. Agents receive `language` kwarg (`"ar"` | `"en"`). `ToolAgent` additionally receives `confirmation_manager`, `session_id` and a `ToolRegistry` from the WebSocket session.

`ToolAgent` delegates to **`agents/engine.AgentEngine`**, a bounded ReAct loop.

```
fast_path.match(message)      (agents/fast_path.py) — anchored, safe-tier only
  ├─ matched ────────► run the one tool call through the SAME gate/audit/metrics,
  │                    answer from its output, 0 model calls, turn over
  └─ no match / not safe / would need approval ──► the loop below
ask the model (catalogue + observations so far)
  ├─ no tool call ────────────────────► final answer, streamed as prose
  └─ tool call
       ├─ validate → gate → execute    (tools/registry.py)
       ├─ record the result as an observation
       └─ loop                         (≤ DEFAULT_MAX_ITERATIONS = 8)
budget exhausted ──► one forced summary call, so the user still gets an answer
```

**Why the short circuit exists and what it may not do.** "what time is it" costs
a small local model two generations (the call, then the narration of a result it
already has) and can come back as an invented time. `agents/fast_path.py` matches a
closed list of phrasings, anchored at both ends, to exactly one tool. It is not a
permission shortcut: the registry's tier and `RiskAssessor`/`TOOL_ALWAYS_CONFIRM`
are re-checked per call, so a tool that would prompt the model also bypasses the
fast path, and re-tiering one closes the shortcut (`ALLOWED_TOOLS` is asserted
against the live registry in `tests/test_fast_path.py`). There is no fall-through
*after* execution — an already-run tool is answered from its own outcome, error
included, because the model re-calling it would launch the app twice.
`FAST_PATH_ENABLED=false` restores the old behaviour exactly (the engine test module
disables it so it keeps testing the loop).

**Model calls are bounded process-wide.** `services/llm_client.py` wraps `complete()`
and `stream()` in `common.concurrency.BoundedGate` — `LLM_MAX_CONCURRENCY` (default
2) simultaneous generations, strict FIFO queueing, `LLM_QUEUE_TIMEOUT_S` (default
45) before a waiter is refused with `kind="llm_queue_timeout"` and a sentence that
says the model is busy rather than broken. It is re-entrant per task because
`stream()` falls back to a blocking `complete()` inside its own slot; a plain
semaphore would deadlock there. Occupancy is published at `GET /health` →
`llm.slots`. This is not a throughput dial: on one GPU, more concurrent
generations means worse answers and a real chance of an OOM-killed daemon.
`scripts/measure_fast_path.py` prints both measurements (fast path: 64 model calls
→ 0, p50 1813 ms → 8 ms against a simulated 900 ms provider).

**A turn has a contract, not just an iteration cap** (`agents/engine.py`).
`MAX_CALLS_PER_ITERATION` (8) bounds what one reply may dispatch and `MAX_CALLS_PER_TURN`
(24) bounds the turn; refused calls come back to the model as *observations* (with the
earlier result attached), never as silence, because silence is what makes a model repeat
itself. An identical `(name, canonical params)` call that already **succeeded** this turn is
refused unless the tool is in `REPEAT_SAFE_TOOLS` — a **deny-by-default allow-list**, so any
unknown or future tool is assumed to have effects (`open_app` is tier `safe`, so nothing else
would stop a second launch). The list is written out and then *checked*, not derived from
tiers: safe-tier is necessary and not sufficient (`screenshot` is `safe` and writes a PNG per
call). `repeat_safe_violations(registry)` reports every drift — a name that is no longer a
registered tool (dead config), a name whose argument-free call is not tier-safe, and a name
listed as an alias rather than the canonical tool — and a test requires it to return
`[]` against `build_default_registry()`. A final answer that claims success (`Done`, `successfully`, `تم`)
while steps failed gets the unfinished steps appended to it, in the user's language: qualified,
never deleted. Tests: `tests/test_agent_bounds.py`; removing any of the three guards reddens it.

**Sources are stamped, not policed** (`agents/sources.py`). The ledger records only what a
tool actually returned: `fetch_url` becomes a *read page*, `search_web` results become
*snippet-only* entries, same URL = one number, and reading a page that was only seen as a
snippet upgrades the entry instead of minting a second citation. The numbered list is shown to
the model in the tool-results message (so `[1]` means the same thing on both sides) and the
final answer gains a `**Sources**` block plus a method line ("1 search query, 1 page read in
full, 2 snippets not opened", timestamps UTC). The one enforceable rule is the falsifiable one:
`[7]` with two sources is called out in the answer and recorded in `LoopResult.errors` — style
and "should this have had a citation" are left alone. Nothing is deleted from the model's text;
the appendix is appended, as with P5's claim check. A turn with no sources gets no noise.
`LoopResult.sources` carries the list for callers (`api`/websocket UI surfacing is P7.3).

Three properties of the dispatch path are load-bearing, and each has a test in
`tests/test_fast_path.py`:

- **Bounded.** `max_iterations` is a hard ceiling, and hitting it is a *reported*
  outcome (`stopped_reason == "max_iterations"`), not an error.
- **Observations are fed back.** Failures, sandbox refusals and declined
  confirmations are all rendered into the next prompt, so the model can correct
  itself. This is what turns "create a React project" from one guess into
  observe-and-retry.
- **Tool calls are not prose.** Only a no-tool-call answer is emitted as
  `RESPONSE_CHUNK`. The previous code passed `stream_callback` straight into the
  LLM call while asking for JSON, so `{"action":"open_app","app":"spotify"}` was
  typed out on the user's screen and then overwritten. Keeping private reasoning
  off the wire is now structural rather than a matter of prompt wording.
- **A routed tool turn refuses prose before it ships.** `ToolAgent` runs the
  engine with `expect_tool=True`. First prose answer → a call-format nag; second
  → a final insistence; a third is **replaced** with the honest bilingual
  `_NO_TOOL_TAKEN` message (`stopped_reason == "no_tool_taken"`) instead of being
  delivered. This exists because the local 3B model twice ignored the nag and
  shipped `الساعة الآن هي: [أكتب الوقت الحالي]` (a template) and then
  `الساعة الآن هي: 12:34:56` (a fabricated time — the real one was 12:57).
  Unfilled placeholders are likewise never delivered: after a tool has run, a
  template-shaped summary gets one corrective round and then the apology.
  The failure is reported; a guessed fact never is.

`ToolAgent` also owns the bilingual gate via `_make_approval()`, which returns a
*refusal string* rather than a bool so the engine can feed "the user declined"
back to the model. The engine consults that callback **only** when
`registry.assess()` reports `RiskLevel.HIGH`, so `get_time` never needs a
confirmation channel plumbed through it.

Tool selection is JSON-in-text, not native function calling. `ToolRegistry.openai_tools()`
already emits the spec, so switching is a provider change rather than a rewrite.

> **This decision was made on evidence, and the evidence complicates it.** A live
> run against `qwen/qwen3.8-27b:free` showed the model using **three different
> tool-call dialects** across identical requests: the documented JSON, a
> `<\u200btool_call>name</tool_call>` pseudo-XML form, and Qwen's native
> `<|tool_call_start|>[name(args)]<|tool_call_end|>`. All three are parsed now
> (`_iter_tag_calls`), and `looks_like_tool_call()` is the backstop: anything
> still shaped like tool-call machinery is treated as a **parse failure**, not
> as an answer, so an unknown fourth dialect degrades to an honest apology
> rather than to raw markup on screen. The call dialect is parsed with
> `ast.parse` and read from the tree — never compiled, never evaluated.
>
> Note that a plain `{...}` regex cannot do the JSON extraction either: the
> documented call form *nests* `params`, so `agents/engine.py` uses a
> brace-matching scanner that is string-aware, and resolves overlaps explicitly
> because the tagged and JSON scanners both match a tagged JSON call.

## Router priority (ordering is the whole system)

`KEYWORD_ROUTES` is an ordered list and `route()` returns on the **first**
match, so the order *is* the priority system.

It used to put the catch-all `(what|how|why|…) -> chat` above the specific
rules. Every "what …" request therefore became `chat`, and the `what time`
rule was unreachable dead code. The live run answered *"what time is it right
now"* with "I don't have access to real-time data" while a `get_time` tool sat
unused in the registry. The catch-all is now last, and new rules were added for
git, `create_project`, `run_tests`, npm, file reads/writes, calculator,
screenshot and volume.

`tests/test_router_routing.py` guards the *ordering*, not the regexes:

- every case in a 33-phrase table must reach its expected intent;
- **no phrase may need the LLM to be routed** — the free tier 429s constantly
  and on exhaustion the router defaults to `chat`, so tools would be unreachable
  exactly when the model is busiest;
- a rule may only be shadowed by one of equal-or-higher confidence (a broader
  fallback placed after a specific rule is correct design, and demanding
  otherwise would invert the table);
- no intent class may be entirely unreachable;
- no duplicate alternatives (`(create|create)` was real).

## What the live run verified

Driving the real backend over a real WebSocket, `C:\…\live_loop_probe.py`
style — 3/3 on the three-step request *"create a folder, write a file inside
it, read it back"*:

- three chained calls across three iterations, real files on disk;
- `create_folder` and `file_write` (tier `confirm`) each prompted;
  `file_read` (tier `safe`) did **not** — the tier logic is right end to end;
- run 2 and 3 reported *"probe_demo already exists"*: the model adapted from the
  observation rather than retrying blindly;
- no tool JSON ever reached the transcript;
- Arabic works: `كم الساعة دلوقتي` → `get_time`, label `أشوف الوقت`.

**The 429 is the real remaining risk.** `qwen/qwen3.8-27b:free` is rate-limited
most of the time, and the `openrouter/free` fallback returns arbitrary free
models — it answered `get_time` with the literal string `"User Safety: safe"`,
which the loop accepted as a final answer. There is currently no quality floor
on a fallback completion.

## Agent events

`agents/events.py` defines the vocabulary the agent reports in, and an
`EventBus` to carry it. The agent emits events and never raw deltas, so:

- a caller that does not care about progress subscribes to nothing;
- only `AgentEventType.RESPONSE_CHUNK` is user-facing prose — everything else
  is status, which is why no redaction logic is needed anywhere downstream;
- the transport owns the wire format, so the desktop shell, the browser app and
  a test can all drive the engine equally.

A subscriber that raises is logged and skipped: a progress display must never be
able to abort the turn that produced it.

`api/websocket._ProgressRelay` is the only place that maps events to frames.
`THINKING`/`PLANNING` become `status`, `TOOL_STARTED`/`TOOL_FINISHED` become
their own frames, and `COMPLETED` is deliberately **not** relayed — the
transport owns the terminal `done`, because only it knows about TTS drain and
the `message` frame that has to land first.

## WebSocket protocol

| Type | Payload | Direction |
|------|---------|-----------|
| `message` | `{"type":"message","message":"..."}` | Client → Server |
| `audio` | `{"type":"audio","audio":"<base64>","language":"en\|ar"}` | Client → Server |
| `interrupt` | `{"type":"interrupt"}` | Client → Server |
| `reminder_cmd` | `{"type":"reminder_cmd","action":"list\|delete\|done","id":N}` | Client → Server |
| `tool_confirm` | `{"type":"tool_confirm","request_id":"cfm_…","approved":true\|false}` | Client → Server |
| `token` | `{"type":"token","content":"...","response_id":"..."}` | Server → Client |
| `audio_chunk` | `{"type":"audio_chunk","data":"<base64>","index":N}` | Server → Client |
| `status` | `{"type":"status","content":"connected\|thinking\|planning\|executing\|speaking\|done"}` | Server → Client |
| `plan` | `{"type":"plan","steps":["Creating the project",…],"response_id":"…"}` | Server → Client |
| `tool_started` | `{"type":"tool_started","name","label","response_id"}` | Server → Client |
| `tool_progress` | `{"type":"tool_progress","name","message"}` | Server → Client |
| `tool_finished` | `{"type":"tool_finished","name","success","summary"}` | Server → Client |
| `sources` | `{"type":"sources","sources":[{"n","kind","url","host","title","retrieved_at"}]}` (≤12, after the prose) | Server → Client |
| `error` | `{"type":"error","content":"..."}` | Server → Client |
| `stt_result` | `{"type":"stt_result","language","voice","duration"}` | Server → Client |
| `audio_start` | `{"type":"audio_start","language","voice","format":"mpeg"}` | Server → Client |
| `require_confirmation` | `{"type":"require_confirmation","request_id","action","params","level","reasons","summary","text","session_id"}` | Server → Client |
| `confirmation_result` | `{"type":"confirmation_result","request_id","approved","resolved"}` | Server → Client |

Every label crossing the wire is a fixed, human-written string from
`_LABELS_EN` / `_LABELS_AR` in `agents/engine.py` — never the model's own
reasoning.

Audio chunks each have independent base64 padding. TTS cache: 50 entries, cleared on restart.
A `ConfirmationManager` is created per WebSocket session and stored on
`SessionState.confirmation_manager`; it is resolved by `tool_confirm` and
cancelled on disconnect, timeout, or `interrupt`.

## Desktop (Python / pywebview + React)

`desktop/` is a **native host** that presents the React/Vite UI inside a
WebView. The React application is the primary desktop interface; the host is a
thin native shell that provides window management, backend lifecycle and secure
OS integration. It is *not* Tauri, *not* Electron, *not* Rust, and it does
**not** render its own UI.

```
┌─────────────────────────────┐
│      Native Desktop Host    │   desktop/app/host.py  (pywebview)
│       WebView/WebView2      │   desktop/app/bridge.py (JS ⇄ Python bridge)
│             │               │
│      React/Vite UI          │   frontend/dist/index.html  (or Vite dev server)
└─────────────┬───────────────┘
              │  ws://127.0.0.1:8000/ws  +  REST
              ▼
        Zaytoon Backend        (spawned or adopted by app/backend.py)
```

> Historical note: this directory was first migrated off a Tauri/Rust shell onto
> a Python/customTkinter orb+panel shell, and then off Tk onto pywebview.
> `desktop/tests/test_desktop_shell.py::TestNoRustResidue` fails if the Tauri
> scaffolding or its documentation reappear.

```
desktop/
├── main.py                # entry: --diagnose, --dev, --no-backend
├── start_desktop.bat      # launcher (prefers backend\.venv\Scripts\python.exe)
├── requirements.txt       # pywebview, websockets
├── app/
│   ├── host.py            # WebViewHost: window, URL, health check, window state
│   ├── bridge.py          # NativeBridge: the explicit JS ⇄ Python API
│   ├── backend.py         # FastAPI child-process supervisor (unchanged)
│   ├── instance.py        # single-instance named mutex (unchanged)
│   ├── protocol.py        # the 4 outbound builders + ServerEvent decoding
│   ├── config.py          # every value env-overridable (+ dev/window options)
│   └── app.py, orb.py, panel.py, core.py, header.py, sidepanel.py,
│       dialogs.py, activity.py, states.py, theme.py, audio.py, hotkeys.py,
│       tray.py, client.py  # legacy Tk shell modules (kept, no longer launched)
├── scripts/build_exe.py   # PyInstaller onefile build
├── scripts/smoke_desktop.py  # live proof: opens the real window, probes the
│                           #   page + bridge + CORS, then closes itself
└── tests/                 # pytest suite (host, bridge, backend lifecycle, …)
```

- **Runtime choice: `pywebview`.** It uses the OS's own WebView2 on Windows
  (no bundled Chromium), so startup is fast, RAM is low, and packaging stays a
  single PyInstaller file. The equivalent on Linux is GTK WebKit; the same
  `webview` API covers both. Electron was rejected: it would ship ~150 MB of
  Chromium and a Node runtime for a UI that already exists in `frontend/`.
- **The React app is the application.** The host loads
  `frontend/dist/index.html` in production (no internet connection required) or
  `http://localhost:5173` with `--dev` / `ZAYTOON_DEV_MODE=1`. The React app's
  own `useWebSocket.js` handles the backend connection, streaming, confirmations
  and reconnection — the host does not duplicate any of it.
- **The bridge is a closed set** (`app/bridge.py`): window controls, file/folder
  dialogs, `reveal_in_os`, `open_path`, `open_url`, `notify`, clipboard, system
  info, backend status/restart. Every method is explicitly registered and
  JSON-serialisable; **no arbitrary Python, no `eval`/`exec`, no `shell=True`** —
  asserted by `tests/test_bridge.py::TestSecurity`. Destructive actions are *not*
  on the bridge; they travel through the backend's gated tool layer and its
  confirmation flow, which is unchanged.
- **Paths and URLs leaving the bridge are policed** (`desktop/app/paths.py`):
  `open_path` and `reveal_in_os` end in `os.startfile` / `open` / `xdg-open`, which
  *execute* a file the OS calls a program, so a path is accepted only when it
  resolves (symlinks followed first) inside a granted root — the backend's own
  workspace, plus the desktop state dir — and only when it is not something the
  shell would run (`.exe`/`.bat`/`.lnk`/… on Windows, the execute bit or
  `.desktop` on Linux/macOS). `open_url` accepts `http`/`https` only. Refusals log
  a reason and return `False`; the bool across the bridge never reveals whether a
  file exists. `ZAYTOON_OPEN_ROOTS` widens the grant deliberately. Pinned by
  `desktop/tests/test_bridge_paths.py`, which also pins the workspace-name mirror
  against `backend/config.py`. **Anything new that hands a path to the OS must go
  through `path_policy.classify()`.**
- **The React side of the bridge** is `frontend/src/lib/desktop.js`: a wrapper
  that resolves `window.pywebview.api` once and degrades to no-ops in a plain
  browser, so the same bundle runs as web or desktop. `App.jsx` uses it for OS
  reminders notifications.
- **Backend lifecycle** (`app/backend.py`): probes `127.0.0.1:8000`
  with a bare `socket.connect_ex`. If nothing answers it spawns
  `ZAYTOON_BACKEND_{PYTHON,MAIN,CWD}` resolved in three tiers — explicit
  override → `backend/.venv` → the running interpreter — with `shell=False`,
  `PYTHONUNBUFFERED=1` and `CREATE_NO_WINDOW`. It polls for up to
  `ZAYTOON_BACKEND_TIMEOUT` (30s) and **rolls the child back** on timeout. On
  close it signals the child's whole *process group* — politely first
  (`SIGTERM` / `taskkill /T`, `/F` only if the group ignores the polite one) —
  because uvicorn reloads into a child that is ours to clean up but not ours to
  own; `killpg` runs only when the child is the group leader, and the whole path is
  bounded so a wedged `wait()` can never hang window close (`desktop/tests/test_desktop_lifecycle.py`).
- **The safety property that matters:** `BackendProcess.shutdown()` returns
  immediately unless `self._spawned_by_us` is true, and `ensure_running()`
  *releases* its handle if a foreign process took the port. A backend the user
  started by hand is never killed. Covered by
  `TestBackendNeverTouchesAForeignProcess`.
- **Single instance:** `app/instance.py` claims a named Windows mutex
  (`Local\Zaytoon.Desktop.Shell.Instance`) before the window exists; on
  Linux/macOS it holds an `flock` on a sibling lock file for the same guarantee —
  released by the kernel if the owning process dies, so a crash cannot leave a
  stale lock. A second launch prints a message and exits.
- **Startup sequence:** parse args → single-instance claim → `WebViewHost.run()`
  → `backend.ensure_running()` → resolve URL (production file vs dev server) →
  create window with `js_api=bridge` → start health-check thread →
  `webview.start()`. On window close: save window state → stop health check →
  `backend.shutdown()` (only if we spawned it). `run()` is single-shot and its
  teardown is `try/finally`, so **a failure anywhere — including `import webview`
  itself, which fails on a Linux box with no WebKit/GTK — still stops the backend**
  instead of orphaning it in a window that never opened.
- **Health check:** a daemon thread probes the backend every
  `ZAYTOON_HEALTH_INTERVAL` seconds (default 5) and records the result on
  `WebViewHost.backend_up`, which `diagnose()` reports. It deliberately does
  **not** call `evaluate_js`/`load_html` any more: the page's own
  `useWebSocket.js` already owns liveness and reconnection, there was no
  consumer for a `backend-health` event, and touching the WebView from a thread
  pywebview did not create is unsafe on GTK/Cocoa (WebView2 happens to tolerate
  it). Cross-thread DOM pushes must go through pywebview's own events.
- **The asset-server port is pinned, and the backend allowlists it.** In
  production pywebview serves `frontend/dist` itself from a *random* port, and
  a random origin is not one the backend's CORS allowlist recognises — the
  page loads and looks fine while every REST call (reminders, health,
  settings) is silently rejected. `host.pick_asset_server_port()` therefore
  takes the first free port in **5175–5179** (override:
  `ZAYTOON_DESKTOP_PORT`, which must stay inside the same range) and passes it
  to `webview.start(http_port=…)`, and the backend's `allow_origin_regex`
  matches `http://(localhost|127.0.0.1):517\d+`. **Keep the two ranges in
  step.** A busy port is skipped rather than handed over: bottle would fail
  inside its thread while pywebview still reports the address as healthy — a
  blank window with no error anywhere.
- **Window state** (size, position, maximized) is persisted to
  `$ZAYTOON_HOME/window_state.json` — outside the installation directory, per
  the portable-layout rule — and restored on the next launch. A corrupt file
  degrades to defaults. Geometry reads go through `_geometry()`, because at
  `closed` pywebview's `width`/`height` getters unpack `gui.get_size()` which
  returns `None` once the native window is gone — a raw read dies with
  `cannot unpack non-iterable NoneType object` and **the state was never
  saved at all**.
- **Repository root** is resolved by `config.default_repo_root()`:
  `ZAYTOON_REPO_ROOT` → source layout (`<repo>/desktop/app/config.py`) →
  `sys._MEIPASS` for a frozen build. `scripts/build_exe.py` tells the user to
  set `ZAYTOON_REPO_ROOT`; that variable is read by the code and asserted by
  `TestRepoRootDiscovery::test_build_exe_documents_the_env_var_that_exists`.
- **Protocol** is centralised in `app/protocol.py` so any Python client and the
  backend cannot drift. The React client is the one in use; the builders remain
  the reference for message shapes.
- **Diagnose:** `python desktop/main.py --diagnose` reports the TCP probe,
  interpreter resolution, `pywebview`/`websockets` imports, the presence of a
  production frontend build, and the **WebView2 runtime itself** (registry +
  fixed-path check on Windows, not merely that `pywebview` imports) — then a
  verdict with the download URL when it is missing. It exits non-zero on a
  real problem instead of opening a broken window; `WebViewHost.run()` fails
  fast with the same message before any window is created.
- **Build:** `python desktop/scripts/build_exe.py` → `desktop/dist/Zaytoon.exe`
  (PyInstaller onefile, portable). The backend and the frontend bundle are
  deliberately **not** frozen in: ship `backend/` and `frontend/dist/` as files
  next to the exe, or point `ZAYTOON_REPO_ROOT` at the install dir. The frontend
  is loaded from disk, so the installed app works offline.
- **Legacy Tk modules** (`app.py`, `orb.py`, `panel.py`, `core.py`,
  `theme.py`, …) are retained for reference and the **whole legacy suite still
  runs against them** (`test_states`, `test_app_wiring`, `test_core_animation`,
  `test_activity_model`, `test_single_instance`, `test_telemetry`, plus the
  hotkey/audio/sidepanel cases in `test_desktop_shell.py` and
  `test_audio_pipeline.py`) — no test was deleted when the entry point moved.
  They are **not** imported by `main.py` any more. `requirements.txt` carries
  only the shipped runtime (`pywebview`, `websockets`); the legacy test deps
  (`customtkinter`, `keyboard`, `numpy`, `sounddevice`, `av`) moved to
  `requirements-dev.txt`, which is what CI installs.
- **Tests:** `tests/test_host.py` (URL resolution, window state, health check,
  diagnostics, WebView2 detection, the pinned asset-server port),
  `tests/test_bridge.py` (every bridge method, a real clipboard round trip,
  the notification-encoding fixes, and the security invariants),
  `tests/test_desktop_shell.py` (repo discovery, backend lifecycle, protocol,
  packaging guards), plus the full retained legacy suite above.
- **Live proof:** `python desktop/scripts/smoke_desktop.py` opens a real
  window against the production build, then checks — from inside the actual
  page — that React mounted, the bridge answers (`get_system_info`,
  `get_app_path`, …) and `fetch('http://127.0.0.1:8000/health')` survives
  CORS, before closing itself and stopping the backend it spawned. It is the
  only check that exercises pywebview + WebView2 + the built bundle
  together; unit tests cannot. Add `--keep` to leave the window open.

## Recent increments (IMPLEMENTATION_MAP Phases 4–9)

- **AI settings (Ph4):** `POST /settings/ai` persists — the API key goes to
  `core.secrets` (never `kv`, never echoed on any read; asserted by
  `test_no_read_path_returns_the_saved_key`), everything else to `kv`
  (`services/ai_settings.py`, applied best-effort at lifespan). Pull is
  explicit and single-flight (`POST /settings/ai/pull` — background, strict
  name regex, 409 concurrent); nothing else in the tree calls `pull_model(`.
  Per-tier pins live at `GET/POST /models/tiers`.
- **Skill tools (Ph5):** an enabled skill may ship `tools/*.py` with
  `build_tools()`; `core.skills.load_skill_tools()` loads them into the one
  live `ToolRegistry` at lifespan (so `/tools`, health, the agent catalogue and
  execution all see them). Contained realpath import, per-skill module
  namespace, name collisions refused, failures reported never fatal.
- **The skill permission floor (P6).** A skill cannot self-declare `safe`: the
  loader raises the instance tier *and* passes `floor=PERMISSION_CONFIRM` to
  `ToolRegistry.register`, which records it in `self._floors` and applies it in
  `assess()` via `_apply_floor`. The second half is the one that matters —
  `BaseTool.permission` is a mutable attribute on third-party code, so a tool that
  assigns `self.permission = "safe"` (or `""`) inside `execute()` used to leave the
  gate after its first approval. Floors are **sticky and monotone**: `register` can
  only raise one, an unknown floor string is a `ValueError` (a typo must not mean
  "no floor"), and only `unregister` removes one. Anything *showing* a tier —
  `describe()`, `by_tier()`, `effective_tier()` — reads the registry, never the
  object, so a lying tool cannot advertise itself as harmless.
- **Shipped security skills (P7.5).** `skills/` at the repository root holds three
  read-only skills — `log-review`, `dependency-review`, `file-audit` — installed by
  `python backend/scripts/install_shipped_skills.py` (explicit, offline, and **not enabled**
  afterwards: an enabled skill injects instructions into every turn, so that is a decision).
  They are passive **by construction, not by promise**: `tests/core/test_shipped_skills.py`
  parses each shipped module and fails on any import outside
  `{json, hashlib, os, re, typing, tools.*}`, any call to `os.remove`/`os.chmod`/
  `shutil.*`/`subprocess`/`socket`/…, any `open()` in a write mode, and any `eval`/`exec`.
  They also declare `permissions: ["filesystem.read"]` and the P6 gap check runs against
  them, so a tool that later grows a need the manifest did not ask for fails the build.
  Their tools still get the `confirm` floor — being shipped by us is not an exemption,
  because an exemption list is where a floor leaks. Certificates are reported as
  "PEM certificate (not decoded…)" rather than clean: `cryptography` is not a dependency,
  `openssl` would mean running a program, and a skipped check printed as a pass is how an
  audit tool lies.
- **Skill policy gaps (P6).** `core.permissions.gaps_for(tool_name,
  tool_declared, manifest_declared)` compares what a tool needs with what its
  `skill.json` asked for, minus what the `confirm` floor grants anyway
  (`SKILL_FLOOR_GRANTS`) — so a quiet hello tool reports nothing and a skill
  shipping `git_reset_hard` without `git.destructive` reports exactly that.
  Advisory by default (`report["policy"]` + a warning + one startup line);
  `SKILL_STRICT_PERMISSIONS=true` turns a gap into a refusal to load that tool. An
  unreadable manifest now refuses the skill's tools entirely: no manifest means no
  approved policy. `Permission` is a `str` Enum whose `hash` is the member's, so
  all comparisons go through `_names()` on `.value` — a raw set difference would
  have reported every skill as a violator. Tests: `tests/core/test_skill_policy.py`.
- **What the model changed (Ph6):** `BaseTool.mutates_files` + a per-turn
  recorder in the orchestrator → `task.tools_used` / `task.files_changed` and a
  `file.changed` event (failed/declined tools count as used but claim no file
  change). FileExplorer badges the rows (`changed` prop); GitPanel has merge UI.
- **Command centre (Ph7):** header nav (Project/Model/System/Settings) and the
  dock's panel strip (TERM/LOG/DIFF/TASK, tablist named **"Quick access"** —
  it must not collide with the drawer's "Panels") both write shell focus
  through App-level `tab`/`inspectorTab` state. The System inspector tab reads
  `GET /system`'s `resources` (CPU delta-sampling returns `sampling` on the
  first reading — never a fabricated `0%`; network is `unavailable` + reason)
  and `uptime_seconds`, plus `llm.metrics.by_model_latency_ms` from `/health`.
- **Diagnostics (Ph8):** `GET /events/recent` exposes the core bus's ring
  buffer — clamped to 1–100 by `EventBus.recent()` and passed through
  `reporting.redact_payload()` (structured redaction: string values masked,
  keys and counters untouched). Every failing response across all failure
  classes is pinned as `{"error": str}` (never FastAPI's `{"detail": ...}`)
  by `TestErrorShapeIsUnified`.
- **OS parity contract (P8):** three systems, one contract — *the same answer, or
  a stated reason*. Concretely: `tools/applications.py` resolves an app name via
  Start-Menu shortcuts (Windows), a depth-one `.app` bundle scan of
  `/Applications` (macOS, case- and space-insensitive) or `PATH` (Linux);
  `core/system_resources.py` reads `GetGlobalMemoryStatusEx`, `/proc/meminfo`
  + `/proc/stat`, and `vm_stat`/`sysctl -n kern.cp_time|hw.memsize|vm.page_size`
  respectively, and every branch returns the **same keys** (`percent`,
  `used_bytes`, `total_bytes`) so `GET /system` has one shape on every OS;
  `tools/system.py` picks a volume backend by probing (`SendKeys` / `osascript`
  / `pactl` / `amixer`) and refuses rather than approximating — `level` is
  absolute (Windows: 100 up-keys then `100 - level` down-keys, because a raw
  key-press count is a *relative* nudge), and mute is refused on Windows because
  that key only toggles and nothing here can read the state back. Unavailable
  counters report `status: unavailable` + `reason`; the first CPU sample is
  `status: sampling`, never `0%`. `tests/test_posix_parity.py` pins all of it on
  Linux by faking the platform and the command output; `macos-latest` in the CI
  matrix is where the real `vm_stat`/`sysctl`/`open` behaviour gets answered.
- **E2E verification (Ph9, nine scenarios since P8):**
  `backend/scripts/e2e_scenarios.py` drives nine scripted acceptance scenarios
  over the real WebSocket (bilingual turn, safe tool, confirmation
  approve/decline, reminder round-trip, diagnostics shapes, containment + audit,
  policy guards, **OS capability parity**, **citation discipline**) and finished
  **7/7** in its original form. It caught four
  defects no unit test covered, each now pinned by a test: the WS
  `reminder_cmd` call site whose function was never written
  (`tests/test_websocket_reminders.py` drives the real handler), reminders
  partitioned by an ephemeral session id (**decision: `session_id` is
  attribution, not a partition** — list defaults to workspace-wide,
  `TestReminderVisibility`), `ToolAgent._make_approval` referencing the
  module-level notice dicts as `self._DECLINED` (every non-approval branch
  raised `AttributeError`; `TestApprovalCallbackNotices`), and the
  `expect_tool` nag driving a *declined* turn to `no_tool_taken`
  (`AgentEngine.gate_refused` exempts prose only after a real gate refusal;
  `test_a_decline_exempts_prose_from_the_tool_nag`). The runner waits for the
  terminal `done` frame, which the transport emits only after TTS drain.
  Since P8 the runner is honest on a machine with **no model running**:
  `classify_error()` reads both the `error` frame *and* the final message, because
  the controlled unavailability notice is a normal `message` (P1 decision), so an
  exhausted free tier SKIPs with what was still verified instead of FAILing. The
  two platform-blind expectations were the other reason the script lied: a
  `C:\Windows\…` write is only an escape on Windows (elsewhere it is a legal
  filename *inside* the workspace, so `/etc/passwd`, `../` and `~/` are the POSIX
  probes), and scenario 3 falls back to `_gate_without_model()`, which proves the
  gate at the registry — a confirmation-required command denied with nothing on
  disk, `echo hi & hostname` blocked — so "no model here today" never means "the
  gate went untested".

## Notes

- CI is GitHub Actions (`.github/workflows/ci.yml`): three jobs — `backend`
  (Windows, py3.11, policy guards + pytest + live smoke check), `frontend`
  (Node 20, lint/test/build), and `desktop` (Python shell tests; installs only
  `desktop/requirements-dev.txt`, which is what makes that job a real
  clean-machine check).
- No formatter or typechecker is configured for Python. The frontend is
  **JavaScript/JSX, not TypeScript** — there is no `tsconfig.json`.
- Dockerfiles exist for both services and `docker-compose.yml` wires them
  (backend + nginx frontend), so the earlier "both are empty" claim is stale.
- `.env` is gitignored; never commit it.
- **The repo was not a git repository** when this analysis was performed.
  Initialise one before starting any of the refactors below, so each increment
  is reviewable.
