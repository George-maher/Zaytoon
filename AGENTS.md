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

Desktop shell (optional, Python/customTkinter — spawns the backend itself):
```bash
cd desktop && python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py            # or: python main.py --diagnose
# Ctrl+Space toggles the floating orb; Alt+Z switches orb <-> panel.
```

Root scripts (via `concurrently`):
- `npm run dev` — starts both backend + frontend
- `npm run test` — runs backend tests
- `npm run test:desktop` — runs the desktop shell tests

## Repo layout

```
backend/
├── main.py                 # Thin entry: sys.path.insert + uvicorn.run("main:app", ...)
├── config.py               # Env-based config (python-dotenv)
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
├── common/exceptions.py    # ZaytoonError hierarchy
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
├── tests/                  # 27 test files, pytest-asyncio
├── scripts/verify_final.py        # Free-only + voice routing + sandbox smoke check
├── scripts/smoke_confirmation.py  # Manual WS confirmation round-trip check
├── scripts/verify_execution.py    # Live probe: tools, audit, dedupe, processes
└── data/zaytoon.db         # SQLite (auto-created by MemoryAgent)
    data/audit.db           # Execution audit (separate file, see Execution layer)
frontend/src/
├── hooks/useWebSocket.js   # WS connection, audio recording, VAD, state, confirmations
├── components/             # ChatMessage, MessageList, InputBar, VoicePanel,
│                           #   StatusDot, SystemMonitor, ReminderPanel, ConfirmationDialog
│   ├── Chat/ActivityFeed.jsx  # Agent progress: plan + tool_started/finished log
│   └── SettingsPanel.jsx   # workspace root, API key, model chain, LLM metrics
└── styles/index.css        # Tailwind + CSS vars + animations
desktop/                    # Python/customTkinter floating orb + chat panel
├── app/                    # Tk main thread; orb, panel, tray, hotkeys, WS client
├── scripts/build_exe.py    # PyInstaller onefile build
└── tests/                  # pytest suite for the shell
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
| `safe` | runs without asking | `get_time`, `system_info`, `file_read`, `search_web`, `open_app`, `git_status`/`git_log`/`git_diff` |
| `confirm` | always asks, whatever the arguments | `file_write`, `create_folder`, `run_npm`, `system_command` (any command), `type_text`, `key_press`, `git_commit` |
| `dangerous` | asks, and is often refused outright | `file_delete`, `delete_folder`, `registry_write`, `run_code`, plus argument-dependent escalation (killing a system-critical process) |

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

## Tests

```bash
# backend (1405 tests)
cd backend
.venv\Scripts\activate && python -m pytest tests/ -v

# desktop shell (476 tests)
cd desktop
..\backend\.venv\Scripts\python.exe -m pytest tests/ -v

# frontend (221 tests + lint + build)
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
> 30-tool registry, because a double that can make a broken thing look fine is
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

### Orphaned components (resolved in Increment 1.5)

Eight root-level components existed but were imported nowhere, which is why the
app had **no text input at all** and no reminders UI. `InputBar`,
`ReminderPanel` and `SystemMonitor` are now wired. Still orphaned, pending
deletion or adoption: `ChatInterface.jsx` and `VoiceControls.jsx` (both 0
bytes), `StatusDot.jsx`, `VoicePanel.jsx`, `Chat/VoiceButton.jsx`,
`useGsapTimeline.js`, and the empty `App.css` / `index.css` / `App.jsx.css`.
These are the remaining ESLint warnings.

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
ask the model (catalogue + observations so far)
  ├─ no tool call ────────────────────► final answer, streamed as prose
  └─ tool call
       ├─ validate → gate → execute    (tools/registry.py)
       ├─ record the result as an observation
       └─ loop                         (≤ DEFAULT_MAX_ITERATIONS = 8)
budget exhausted ──► one forced summary call, so the user still gets an answer
```

Three properties are load-bearing, and each has a test in
`tests/test_agent_engine.py`:

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

## Desktop (Python / customTkinter)

`desktop/` is a **Python** application — a floating always-on-top orb plus an
expanded chat panel, built on customTkinter. It is *not* Tauri, *not* Rust, and
it does *not* embed the React frontend. It is an independent WebSocket client of
the same `ws://127.0.0.1:8000/ws` endpoint the browser app uses.

> Historical note: this directory was migrated off a Tauri/Rust shell. The
> `AGENTS.md` section and `.github/workflows/ci.yml` continued to describe the
> Rust build for several revisions, which made the docs and CI actively wrong.
> `desktop/tests/test_desktop_shell.py::TestNoRustResidue` now fails if that
> drift is reintroduced.

```
desktop/
├── main.py                # entry: --diagnose, --console, then ZaytoonApp
├── start_desktop.bat      # launcher (prefers backend\.venv\Scripts\python.exe)
├── requirements.txt       # customtkinter, sounddevice, numpy, websockets,
│                          #   keyboard, pystray, pillow
├── app/
│   ├── app.py             # Tk main thread + queue.Queue pump (TICK_MS = 30)
│   ├── states.py          # Inputs -> UiState + label (pure, unit-tested)
│   ├── core.py            # reactive Canvas "core" visualisation
│   ├── header.py          # identity, status pills, window controls
│   ├── sidepanel.py       # model, task, tools, CPU/RAM, actions, processes
│   ├── dialogs.py         # confirmation dialog (Allow / Cancel)
│   ├── activity.py        # per-turn activity model (plan, tool rows)
│   ├── orb.py             # floating orb: Tk Canvas, colour-key transparency
│   ├── panel.py           # the shell: layout_plan, compute_wrap, MessageRow
│   ├── backend.py         # FastAPI child-process supervisor
│   ├── client.py          # blocking websockets.sync client + backoff reconnect
│   ├── protocol.py        # the 4 outbound builders + ServerEvent decoding
│   ├── audio.py           # mic capture / TTS playback, ring buffers, VAD
│   ├── hotkeys.py         # global hotkeys via `keyboard`
│   ├── tray.py            # pystray menu
│   ├── config.py          # every value env-overridable
│   └── theme.py           # colours + bilingual status labels
├── icons/source.png       # 1024px orb asset
├── scripts/build_exe.py   # PyInstaller onefile build
└── tests/                 # pytest suite for the shell
```

- **Window:** undecorated (`overrideredirect`), always-on-top, no taskbar
  button. Transparency is a **colour key** (`#000001`), not an alpha channel —
  Tk has no alpha on Windows, so soft edges are faked with canvas stipple.
- **The shell is a command center, not a chat window.** `states.py` is a pure
  `(connection, status frame, intent, tool frames, audio, errors) -> UiState +
  label` function — the single source the header pill, the core visualisation
  and the status line all read, so they cannot disagree. `panel.set_ui_state()`
  is the one fan-out point.
- **Animations self-stop.** `core.AnimationLoop` runs one cancellable chain per
  widget and `set_mode(static)` cancels it outright; `STATIC_STATES = {IDLE,
  OFFLINE}`. The idle breathing ring lost to the "no permanent `after()` loop"
  rule, and `test_core_animation` enforces the rule rather than the decoration.
- **Tk never blocks.** `TelemetryWorker` owns every REST call and the ctypes
  CPU/RAM sampling and pushes through the same `queue.Queue` as the socket —
  no `urllib` or `websockets` call ever happens in a widget callback. CPU is
  read with `GetSystemTimes` and RAM with `GlobalMemoryStatusEx` rather than
  adding `psutil` as a dependency.
- **The confirmation dialog is security-critical UI**, so `ConfirmationState`
  makes it exactly one decision (allow or cancel once), `mark_resolved`
  suppresses a late frame, and closing the window is a denial. It lives in its
  own `CTkToplevel` so it is visible in orb mode.
- `/audit` and `/processes` are **optional**: a 404 hides the card rather than
  showing an error, because the shell must work against a backend that predates
  them.
- **Global hotkeys** via the `keyboard` package (low-level OS hook): `Ctrl+Space`
  toggles the orb, `Alt+Z` switches orb ⇄ panel. Both are env-overridable
  (`ZAYTOON_TOGGLE_HOTKEY`, `ZAYTOON_EXPAND_HOTKEY`). A binding failure degrades
  to the tray menu and reports `HotkeyManager.error` — it never crashes.
- **Backend lifecycle** (`app/backend.py`): probes `127.0.0.1:8000` with a bare
  `socket.connect_ex` (no HTTP client needed). If nothing answers it spawns
  `ZAYTOON_BACKEND_{PYTHON,MAIN,CWD}` resolved in three tiers — explicit
  override → `backend/.venv` → the running interpreter — with `shell=False`,
  `PYTHONUNBUFFERED=1` and `CREATE_NO_WINDOW` on Windows. It polls for up to
  `ZAYTOON_BACKEND_TIMEOUT` (30s) and **rolls the child back** on timeout.
- **The safety property that matters:** `BackendProcess.shutdown()` returns
  immediately unless `self._spawned_by_us` is true, and `ensure_running()`
  *releases* its handle if a foreign process took the port. A backend the user
  started by hand is never killed. Covered by
  `TestBackendNeverTouchesAForeignProcess`.
- **Repository root** is resolved by `config.default_repo_root()`:
  `ZAYTOON_REPO_ROOT` → source layout (`<repo>/desktop/app/config.py`) →
  `sys._MEIPASS` for a frozen build. `scripts/build_exe.py` tells the user to
  set `ZAYTOON_REPO_ROOT`; that variable is read by the code and asserted by
  `TestRepoRootDiscovery::test_build_exe_documents_the_env_var_that_exists`.
- **Protocol** is centralised in `app/protocol.py` so the shell and the backend
  cannot drift. The shell generates its **own** `response_id` per turn, which
  is what makes `client.py`'s stale-stream guard able to drop tokens from a turn
  the user already interrupted.
- **Known gaps:** the shell handles no `reminder*` events and never sends
  `reminder_cmd`, so a fired reminder is silently dropped (`TrayIcon.notify()`
  exists but is unwired). Panel chrome beyond `theme.status_text` is English
  only. `theme.py` has **two layers**: layer 1 (`VOID`, `CYAN`, `RADIUS == 0`)
  is pinned by `test_design_parity.py` to `frontend/src/styles/theme.css`, and
  layer 2 is the redesign's palette that widgets actually render with. They can
  only be unified by moving the CSS first. Visuals cannot be verified headlessly
  — the tests assert tokens and the animation contract, not appearance, so the
  shell still needs a human to look at it.
- **Build:** `python desktop/scripts/build_exe.py` → `desktop/dist/Zaytoon.exe`
  (PyInstaller onefile, portable, no installer). The backend is deliberately
  **not** frozen in — `faster-whisper` + `edge-tts` push a bundle past 500 MB.
  Ship `backend/` as files and point `ZAYTOON_REPO_ROOT` at the install dir.
- **Diagnose:** `python desktop/main.py --diagnose` checks the TCP probe,
  interpreter resolution, optional imports, audio devices, display
  `windowingsystem`, and actually binds then unbinds both hotkeys.

## Notes

- CI is GitHub Actions (`.github/workflows/ci.yml`): three jobs — `backend`
  (Windows, py3.11, policy guards + pytest + live smoke check), `frontend`
  (Node 20, lint/typecheck/build/test), and `desktop` (Python shell tests).
- No formatter or typechecker is configured for Python. The frontend is
  **JavaScript/JSX, not TypeScript** — there is no `tsconfig.json`.
- Dockerfiles exist for both services and `docker-compose.yml` wires them
  (backend + nginx frontend), so the earlier "both are empty" claim is stale.
- `.env` is gitignored; never commit it.
- **The repo was not a git repository** when this analysis was performed.
  Initialise one before starting any of the refactors below, so each increment
  is reviewable.
