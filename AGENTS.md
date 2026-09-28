# Zaytoon — Agent Guide

## Startup

```bash
# Backend (must run from backend/ — sys.path depends on it)
cd backend
cp .env.example .env          # then edit OPENROUTER_API_KEY with your key
.venv\Scripts\activate && pip install -r requirements.txt
python main.py                # http://127.0.0.1:8000

# Frontend (separate terminal)
cd frontend && npm run dev    # http://localhost:5173
```

Prerequisites:
- **OpenRouter API key** — get one at https://openrouter.ai/keys and set it in `.env` as `OPENROUTER_API_KEY`. It is **required**: local model execution has been removed, so with no key the assistant cannot answer.
- Model chain: `OPENROUTER_PRIMARY_MODEL` (default `qwen/qwen3.8-27b:free`) → `OPENROUTER_FALLBACK_MODEL` (`openrouter/free`). Code/planning/scheduling use `OPENROUTER_REASONING_MODEL` (`thinkingmachines/inkling-small:free`) as the chain head. All three are hard-allow-listed.
- See "LLM providers" below for the free-model policy and fallback behaviour.

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
├── services/llm_client.py  # OpenRouter client: free-model allow-list, request/
│                           #   response validation, fallback chain, metrics
├── domain/models.py        # Enums (Intent, Language, AgentType), dataclasses,
│                           #   TokenBucketRateLimiter, SessionState, SpokenSentenceTracker
├── common/exceptions.py    # ZaytoonError hierarchy
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
├── tests/                  # 21 test files, pytest-asyncio
├── scripts/verify_final.py        # Free-only + voice routing + sandbox smoke check
├── scripts/smoke_confirmation.py  # Manual WS confirmation round-trip check
└── data/zaytoon.db         # SQLite (auto-created by MemoryAgent)
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

30 tools, listed in the agent prompt by generating them from the registry
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
- **All agents** go through `services/llm_client.LLMClient`; they never build URLs or pick models themselves. OpenRouter is the only provider, so there is a single endpoint and one SSE parser.
- **OpenRouter API key is required.** `LLMClient.build_chain()` raises a message telling you to set it when the key is absent — there is no local fallback to degrade to.
- **Keyword router**: Fast intent matching via regex before LLM fallback (skips ~80% of LLM router calls).
- **Rate limiter**: 60 req/min per IP (configurable via `RATE_LIMIT_MAX` / `RATE_LIMIT_WINDOW`). Stale entries cleaned every 5 min.
- **Session management**: max 100 concurrent sessions; sessions older than 30 min cleaned. WebSocket `"interrupt"` message cancels active response + TTS.
- **Tools whitelist**: `system_command` accepts any single command (argv, no
  shell), but shell metacharacters and chaining are refused outright. The
  legacy `OSToolkit` `cmd` tool still has a 10-command read-only allow-list and
  is only reachable through `tools/windows.py`.
- **Frontend WS proxy**: Vite proxies `/ws` → `ws://localhost:8000`. The hook uses `localhost:8000/ws` directly when on localhost, otherwise the serving port.

## LLM providers & fallback

**OpenRouter is the only provider.** Local model execution (Ollama, llama.cpp,
any on-device GGUF) has been removed: nothing is downloaded to or run on the
user's machine. Every request goes to
`https://openrouter.ai/api/v1/chat/completions`.

`services/llm_client.py` owns model selection. `BaseAgent.tier` picks the head of
the chain; `LLMClient.build_chain()` returns the ordered endpoints.

| Tier | Chain |
|------|-------|
| `fast` (chat, tools, router) | `qwen/qwen3.8-27b:free` → `openrouter/free` |
| `reasoning` (code, planner, scheduler) | `thinkingmachines/inkling-small:free` → `openrouter/free` |

**There is no local fallback.** If OpenRouter is unreachable the assistant
cannot answer, and says so — it never silently switches providers. With no
`OPENROUTER_API_KEY` at all, `build_chain()` raises a message telling you to set
one.

### Zero-cost policy (two layers, both before any socket opens)

1. **Allow-list** — `config.ALLOWED_FREE_MODELS` is a hard-coded frozenset of the
   three models above. Being free is *necessary but not sufficient*: an
   un-allow-listed `vendor/model:free` is still refused.
2. **Free form** — the id must end in `:free` or be exactly `openrouter/free`.

Checked in `ProviderEndpoint.__post_init__` and re-checked in `build_payload()`,
so a typo such as `OPENROUTER_PRIMARY_MODEL=openai/gpt-4o` can never produce a
charge. `GET /health` reports `rejected_models`.

### Request / response validation

- `ProviderEndpoint.validate_request()` runs before dispatch: URL host, the
  `Authorization` / `HTTP-Referer` / `X-Title` headers, message shape and roles,
  `max_tokens`, `temperature` range, `stream` type. Failures raise
  `LLMRequestError` **without opening a socket**.
- `ProviderEndpoint.validate_completion()` structurally verifies the 200 body:
  content-type, JSON decode, `error` payloads returned with a 200, `choices`
  present/typed/non-empty, `message` object, and `content` either a string,
  `null` (tool calls) or a multimodal block list. Anything else raises
  `LLMResponseError` and the chain moves on instead of crashing the agent.
- SSE frames are parsed defensively; keep-alives, truncated JSON, and frames
  with a missing/typed-wrong `delta` are skipped rather than raising.

### Fallback, health, metrics

- **Fatal** (401/402/403/404/422 …) → skip the model immediately, no retry.
- **Rate limited** (429) and **server error** (5xx) → short backoff on the same
  model, then advance to `openrouter/free`.
- **Timeouts / connect errors** → advance immediately.
- A failing model is benched for `LLM_PROVIDER_COOLDOWN` seconds. If *every*
  model is benched the breaker is ignored rather than hard-failing.
- **Streaming**: a failure *before* the first delta retries the next model, still
  streaming, so early-TTS keeps working. A failure *after* deltas were emitted
  switches to a non-streaming call; the WebSocket layer's final `message` event
  overwrites the streamed bubble and `SpokenSentenceTracker` dedupes TTS overlap.
  If that recovery also fails, the already-streamed partial text is returned
  rather than an error replacing visible text.
- `LLMClient.metrics` counts success / rate_limited / server_error / malformed /
  timeouts plus average latency, per model. Exposed on `GET /health` under
  `llm.metrics`.
- Every outcome is logged with a stable prefix: `LLM OK`, `LLM RATE_LIMITED`,
  `LLM SERVER_ERROR`, `LLM FATAL`, `LLM MALFORMED`, `LLM TIMEOUT`,
  `LLM STREAM OK`, `LLM EXHAUSTED`.

> Note: `thinkingmachines/inkling-small:free` currently returns HTTP 403 for
> non-agentic clients ("only available on agentic harnesses"). 403 is fatal, so
> the chain falls through to `openrouter/free` transparently — but it costs a
> round-trip, so consider a different `OPENROUTER_REASONING_MODEL` if you have
> one that is allow-listed.

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

## Tests

```bash
# backend (1090 tests)
cd backend
.venv\Scripts\activate && python -m pytest tests/ -v

# desktop shell (78 tests)
cd desktop
..\backend\.venv\Scripts\python.exe -m pytest tests/ -v

# frontend (102 tests + lint + build)
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
  `/tasks`, `/command/*`. Added after a one-line edit to `health()` removed the
  `get_llm_client()` call it depends on and nothing caught it.
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
│   ├── orb.py             # floating orb: Tk Canvas, colour-key transparency
│   ├── panel.py           # expanded chat panel
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
  only.
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
