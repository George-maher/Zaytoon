# Zaytoon — Critical Execution and Data Flows

**Status:** Verified against the current implementation. These diagrams trace real code paths (not intended flows).

## 1. Text input → response rendering

```mermaid
sequenceDiagram
    participant U as User
    participant UI as React UI (useWebSocket)
    participant WS as FastAPI WebSocket (api/websocket.py)
    participant ORCH as Orchestrator (core/orchestrator.py)
    participant DISP as AgentService (services/agent_service.py)
    participant ROUTER as RouterAgent (agents/router_agent.py)
    participant AGENT as FastAgent / Planner / etc (agents/)
    participant ENGINE as AgentEngine (agents/engine.py)
    participant REG as ToolRegistry (tools/registry.py)
    participant SANDBOX as Sandbox (tools/sandbox.py)
    participant GATE as RiskAssessor (tools/safety.py)
    participant LLM as LLMClient (services/llm_client.py)
    participant PROV as Provider (ollama / openrouter)

    U->>UI: types message, hits Enter
    UI->>WS: WS frame {type:"message", message, client_msg_id}
    WS->>WS: dedupe check (client_msg_id, per-session TTL set)
    WS->>WS: _begin_turn() — cancels prior turn, mints response_id
    WS->>ORCH: run_turn_in_session(message, stream_callback, ...)
    ORCH->>ROUTER: route(text, context)
    ROUTER-->>ORCH: intent (keyword fast-path or LLM fallback)
    ORCH->>DISP: dispatch(intent, text, context, language, session_id, ...)
    DISP->>AGENT: agent-specific handling
    AGENT->>ENGINE: run ReAct loop (≤8 iterations)
    ENGINE->>LLM: stream/complete with tool catalogue
    LLM->>PROV: HTTP request
    PROV-->>LLM: completion (text or tool-call dialect)
    LLM-->>ENGINE: validated completion (completion_validator)
    ENGINE->>REG: validate() → gate() → execute()
    REG->>SANDBOX: shell metachar / path containment checks
    SANDBOX-->>REG: blocked / ok
    REG->>GATE: risk tier + confirmation assessment
    GATE-->>REG: safe / confirm / dangerous
    alt confirm/dangerous
        REG->>WS: require_confirmation frame
        WS->>U: confirmation dialog
        U->>WS: tool_confirm {approved}
        WS->>REG: execute(approval=True)
    end
    REG-->>ENGINE: ToolResult (success/failure)
    ENGINE->>ENGINE: observation recorded; loop continues
    ENGINE-->>ORCH: TurnResult (reply, intent, sources)
    ORCH->>WS: reply text + token frames (stream_callback)
    WS->>UI: token frames → message frame → status done
    UI->>U: rendered reply
```

### Error handling and cancellation
- **Interrupt:** `WS` receives `{type:"interrupt"}` → `state.cancel()` sets cancel_event + cancels `current_task` / `tts_task` / `audio_task`; engine stops at checkpoints; client receives `interrupted` frame.
- **Provider failure:** LLMClient attempts fallback through the configured free pool; all-fail → `AllModelsUnavailableError` → controlled bilingual error message; no fabricated answer.
- **Tool failure:** structured `ToolResult` with error; returned as observation to the model for self-correction; never crashes the turn.

---

## 2. Voice input → wake-word gate → transcription → execution → response

```mermaid
sequenceDiagram
    participant U as User
    participant UI as React UI (voiceState.js, MediaRecorder)
    participant WS as FastAPI WebSocket
    participant STT as SpeechToText (faster-whisper)
    participant WAKE as WakeWordService (services/wake_word_service.py)
    participant ORCH as Orchestrator
    participant TTS as TextToSpeech (edge-tts)

    U->>UI: speaks into microphone
    UI->>WS: WS frame {type:"audio", audio_b64, language, utterance_id}
    WS->>WS: state.audio_task = _handle_audio() (runs off the receive loop)
    WS->>STT: transcribe_detailed(audio_bytes) [asyncio.to_thread]
    STT-->>WS: TranscriptionResult(text, language, duration, voice_hint)
    WS->>WS: send stt_result frame (language from Whisper verdict)
    WS->>WAKE: apply_wake_gate(text, awaiting_command, last_active_ts, ...)
    alt no wake word / not addressed
        WAKE-->>WS: dropped
        WS->>UI: status listening; turn_complete → standby
    else wake word alone
        WAKE-->>WS: armed (follow-up window)
        WS->>UI: arm_follow_up → listening
    else wake word + command
        WAKE-->>WS: acted (wake.text = command)
        WS->>WS: state.voice.command_accepted() → PROCESSING
        WS->>ORCH: run_turn_in_session(command text, ...)
        ORCH->>...: same flow as Flow 1 (agent → tools → LLM)
        ORCH-->>WS: TurnResult
        WS->>TTS: stream_synthesize(reply) [TTS_STREAM_EARLY: early sentence trigger]
        WS->>WS: state.voice.speaking() → SPEAKING; announce_speaking frame
        TTS-->>WS: audio_chunk frames (base64)
        WS->>UI: audio_chunk → audio element
        UI->>U: audio playback
        WS->>WS: state.voice.turn_complete() → STANDBY
    end

    note over UI, WS: While SPEAKING: audio frames are refused (echo defence);<br/>only explicit interrupt / voice cancel stops the turn
```

### Key voice invariants
- **Echo defence:** While `VoiceState.SPEAKING`, incoming `audio` frames are refused with a `speaking` reason; the microphone is muted on the client. Only an explicit `interrupt` / `voice cancel` frame stops the turn.
- **Wake-word gate:** Continuous listening requires the wake word. `isManualVoice` bypass is opt-in (`WAKE_WORD_ALLOW_MANUAL_BYPASS`) and is `is True`-checked (not truthy) so mocks don't silently enable it.
- **STT → TTS handoff:** Whisper's detected language wins over the client hint; used for TTS voice selection and agent language.
- **Utterance idempotency:** Per-socket (`saw_utterance`) + process-wide (`remember_utterance`) ledgers drop replayed utterances; TTL-bounded and capped.

---

## 3. Natural-language tool request → intent → capability → permission → execution → verification

```mermaid
flowchart TD
    A[User: "create a folder and write a file"] --> B[WebSocket message frame]
    B --> C[RouterAgent.route() — keyword fast path]
    C --> D{Intent matched?}
    D -- yes, safe tier --> E[FastPath: run tool directly through same gate/audit]
    D -- yes / no --> F[AgentEngine ReAct loop ≤8 iters]
    F --> G[LLM call with tool catalogue]
    G --> H{Tool call in reply?}
    H -- no --> I[Final answer as prose]
    H -- yes --> J[ToolRegistry.validate(params)]
    J --> K[Sandbox: shell metachars, path traversal, app targets]
    K -- blocked --> L[Return blocked observation to model]
    K -- ok --> M[RiskAssessor.assess(tier + patterns)]
    M -- safe --> N[Execute]
    M -- confirm/dangerous --> O[require_confirmation WS frame]
    O --> P{User approves?}
    P -- no --> Q[Return declined observation to model]
    P -- yes --> N
    N --> R[ToolResult → observation]
    R --> F
    F --> I
    I --> J2[WS message frame to client]
    J2 --> U[User sees result]

    style E fill:#e1f5e1,stroke:#33a
    style K fill:#ffe6e6,stroke:#d00
    style O fill:#fff4e6,stroke:#f90
```

### Permission tiers
| Tier | Meaning | Examples |
|------|---------|----------|
| `safe` | Runs without asking | `get_time`, `system_info`, `file_read`, `search_web`, `fetch_url`, `open_app`, `git_status/log/diff` |
| `confirm` | Always prompts | `file_write`, `create_folder`, `run_npm`, `system_command` (any command), `type_text`, `key_press`, `git_commit` |
| `dangerous` | Prompts + often refused | `file_delete`, `delete_folder`, `registry_write`, `run_code` |

### Barrier rule
Within one model turn, consecutive safe-tier calls run concurrently under `asyncio.Semaphore(4)`. Every `confirm`/`dangerous` call is a barrier — runs alone, in model-written order. This prevents read-before-write reordering. Verified by `tests/test_parallel_tools.py`.

---

## 4. Application/file discovery → target resolution → safe launch

```mermaid
flowchart TD
    A[User: "open Chrome"] --> B[ToolRegistry: open_app]
    B --> C[tools/applications.py: resolve_windows_app / PATH scan / .app bundle]
    C --> D{Sandbox: sanitize_app_name / validate_app_target}
    D -- traversal / script type --> E[Refused outright — no approval override]
    D -- ok --> F[APP_LAUNCH_ALLOWLIST single list]
    F -- bare name off-list --> G[Escalated to confirm tier]
    F -- explicit path --> H[Verify binary exists; argv to cmd /c start]
    H --> I[Process launched; tracked in tools/processes.py]
    I --> J[GET /processes, /processes/{id}/logs, POST /processes/{id}/stop]

    A2[User: "read ~/notes.md"] --> B2[ToolRegistry: file_read]
    B2 --> C2[Sandbox: resolve_workspace → commonpath containment]
    C2 -- outside workspace --> D2[Refused outright]
    C2 -- inside --> E2[Read file; return contents]
```

### Path containment
Single definition: `tools/sandbox.resolve_workspace(path)` + `is_safe_path()`. Uses `os.path.commonpath`, not `str.startswith`, so `C:\app_evil` is correctly rejected under `C:\app`. No second resolution path may be introduced.

---

## 5. Settings and model configuration → persistence → runtime behavior

```mermaid
sequenceDiagram
    participant U as User (SettingsPanel)
    participant API as FastAPI /core/api/routers.py
    participant SVC as ai_settings / providers store
    participant SECRETS as core/secrets.py (DPAPI, 0600)
    participant KV as core.db (kv table)
    participant LLM as LLMClient

    U->>API: POST /settings/ai {provider, model, key}
    API->>SECRETS: store API key (never in kv, never echoed on read)
    API->>KV: store non-secret settings
    API-->>U: 200 (key never returned in response body)

    U->>API: POST /settings/ai/pull
    API->>LLM: test-chat completion (takes a model slot)
    LLM-->>API: chat_verified_at set on success
    API-->>U: setup status (verified / not verified)

    U->>API: GET /settings/ai/pull?model=...
    API->>LLM: pull model (background, strict name regex, 409 on concurrent)
    LLM-->>API: pull result

    note over U, LLM: API key is write-only; GET /settings/ai never returns it.<br/>Test: test_no_read_path_returns_the_saved_key
```

---

## 6. Desktop startup → health/readiness

```mermaid
sequenceDiagram
    participant U as User
    participant HOST as WebViewHost (desktop/app/host.py)
    participant BRIDGE as NativeBridge (desktop/app/bridge.py)
    participant SUPER as BackendProcess (desktop/app/backend.py)
    participant BE as FastAPI Backend (backend/main.py)
    participant UI as React UI (frontend/dist)

    U->>HOST: double-click Zaytoon.exe
    HOST->>HOST: single-instance mutex / flock
    HOST->>SUPER: ensure_running() — probe 127.0.0.1:8000
    alt no backend answering
        SUPER->>BE: spawn backend (venv or interpreter, shell=False)
        SUPER->>SUPER: poll /health up to 30s
    else backend answering
        SUPER->>SUPER: adopt foreign process (never kill on close)
    end
    HOST->>UI: load frontend/dist (production) or :5173 (--dev)
    HOST->>BRIDGE: js_api=bridge (closed API set)
    HOST->>UI: health-check thread (5s interval, backend_up flag)
    UI->>BE: WS :8000/WS + REST :8000/*
    BE-->>UI: /health 200
    UI->>U: window rendered

    note over HOST, SUPER: On close: shutdown() only if self-spawned;<br/>process-group SIGTERM → taskkill /F if needed
```

---

## 7. Memory / task operations → persistence → retrieval

```mermaid
flowchart TD
    A[Turn completes] --> B[Orchestrator creates Task record]
    B --> C[core/tasks/store.py — SQLite core.db]
    C --> D[TaskStatus: RECEIVED → ANALYZING → ... → COMPLETED/FAILED/CANCELLED]
    D --> E[core/events.py — bus publishes task.* events]
    E --> F[WebSocket _relay_core → task_created / task_updated frames]
    F --> G[UI task panel renders progress]
    
    H[User: "what did I do last week?"] --> I[MemoryAgent.get_recent_context / get_context]
    I --> J[core.db — conversation history table]
    J --> K[Context block prepended to turn]
    K --> L[Model receives conversation history]

    M[Tool executes] --> N[common/audit.py — one row per invocation]
    N --> O[data/audit.db — separate file from zaytoon.db]
    O --> P[GET /audit / DELETE /audit — redacted before output]
```

---

## Cross-flow notes

- **Cancellation reaches both work and record:** `cancel_session()` sets the cancel event AND cancels the asyncio handle; the task record is transitioned to `CANCELLED`/`FAILED`.
- **Observations fed back:** Tool failures, sandbox refusals, and declined confirmations are rendered into the next model prompt so the model can self-correct.
- **Tool calls are not prose:** Only no-tool-call answers are emitted as `RESPONSE_CHUNK` / `token` frames. JSON tool-call syntax is never streamed to the user.
- **Dedupe by id, not text:** `client_msg_id` per-session TTL-bounded set drops resends before they can cancel an in-flight turn.
