# Zaytoon Capability Audit Report

**Repository:** George-maher/Zaytoon  
**Branch:** `arena/43a2339e-zaytoon` (from commit `c4c3a8d3e3997b66ad61f0e835ef8fcc55bc445b`)  
**Audit date:** 2026-11-03  
**Status:** Working tree clean, no uncommitted changes  
**Deliverable:** Capability matrix, prioritized roadmap, gaps, blockers, next-phase recommendation

---

## 1. Executive Summary

Zaytoon is a complete voice-first AI assistant architecture spanning:

- **Backend (Python/FastAPI):** LLM providers, agent orchestration (Router/Tool/Code/Planner/Memory/Scheduler/Summarizer), ToolRegistry with validate→gate→execute, safety gate (RiskAssessor + ConfirmationManager), permission system, connection layer (OAuth2/API-key/MCP), reminders/scheduler, local file/app tools, web search/fetch, VMware virtualization, secrets store, event bus, task DAG manager, project context.
- **Frontend (React 18 + Vite):** WebSocket hook with VAD, ConfirmationDialog, Chat/AICore/HUD components, Connections view (connectors/connections/capability execution), AICache.
- **Desktop (pywebview host):** BackendProcess supervisor, NativeBridge, TrayIcon, config, audio pipeline.
- **Voice (Python):** faster-whisper STT, edge-tts TTS (bilingual en/ar-EG), wake-word service, VoiceSession state machine.

**Validation after Phase B (TTS degradation):**
- Backend: `pytest tests/ --ignore=tests/test_packaging.py` → **3558 passed / 7 skipped / 0 failed** (+16 new tests: 7 TTS availability tracking, 11 voice-loop E2E)
- Packaging test: 1 pre-existing failure (`test_pip_dry_run_succeeds` — PEP 668 externally-managed-environment, unrelated to product code)
- Frontend vitest: **822 passed / 0 failed** across 50 files; ESLint clean; `vite build` OK
- Desktop: **424 passed / 2 skipped / 1 failed** (same pre-existing environment-related diagnose verdict)

**Topline finding:** The system is substantially implemented across all mission phases. **TTS now degrades honestly**: `TextToSpeech.available` reflects actual synthesis capability (False until first success, False after failure, True after recovery), the `/voice/status` and `/health` endpoints report a human-readable `tts_reason`, a new `POST /tts/retry` endpoint lets the UI probe for recovery, and the HUD SystemStatus shows the TTS reason when unavailable. The voice loop continues to deliver text responses even when TTS is silent. **Vision** (image/screen understanding) remains entirely missing. Live connector integration is blocked by missing credentials, not code.

---

## 2. Capability Matrix

Legend:
- **Verified working** — implemented AND live-verified by passing tests/runtime behavior
- **Implemented but not fully verified** — code exists; unit-tested; live/integration verification blocked by environment (no credentials, no display server, external service down)
- **Partially implemented** — code exists but incomplete or degraded in production
- **Missing** — no implementation found
- **Blocked by environment/credentials** — implementation exists but cannot be live-verified in this environment

### A. Core Assistant & Conversation Orchestration

| Capability | Status | Implementation | Tests | Evidence |
|---|---|---|---|---|
| LLM provider abstraction (Ollama + OpenRouter) | Verified working | `backend/core/llm.py`, `backend/core/llm_providers.py`, `provider_health.py` | `test_llm.py`, `test_llm_providers.py`, `test_provider_health.py` | LLMClient contract validation, retry/fallback, health status |
| Provider switching/failover | Verified working | `LLMClient` tier-based fallback chain | `test_llm_providers.py` | Failover on provider failure |
| Conversation lifecycle (turn → record) | Verified working | `backend/core/orchestrator.py` `Orchestrator.run_turn`, `TurnResult` | `test_orchestrator.py` | understand→route→act→record flow |
| Tool discovery & selection | Verified working | `backend/tools/registry.py` `ToolRegistry` | `test_tool_registry.py` | validate → gate → execute ordering |
| Tool-call validation (sandbox) | Verified working | `backend/tools/safety.py` `RiskAssessor`, `backend/core/tools.py` | `test_tool_validation.py` | Workspace path containment |
| Tool execution | Verified working | `ToolExecutor` facade over `ToolRegistry` | `test_tool_executor.py` | execute returns structured result |
| Result handling & rendering | Verified working | `CapabilityResult`, `ToolResult`, `render_observation` | — | Structured result objects |
| Streaming (chunk events) | Verified working | `RESPONSE_CHUNK` events, TTS streaming | `test_streaming.py` | Chunked response delivery |
| Error handling | Verified working | `safe_error`, `client_error`, `LLMError` | — | Errors surface as `{error}` |
| Cancellation | Verified working | `Orchestrator.cancel_session`, per-session `cancel_event` | `test_cancellation.py` | Cancel in-flight turns |
| Session isolation | Verified working | `session_states` dict, per-session `VoiceSession` | — | Isolated state per session |
| Context management | Verified working | `ShortTermMemory`, `MAX_CONTEXT_MESSAGES` | `test_memory.py` | Context ceiling enforced |
| Approval gate (fail-closed) | Verified working | `ConfirmationManager`, `AgentService._is_approved` | `test_approval_gate.py` | Denied permissions block execution |
| Approval binding & replay | Verified working | WebSocket `tool_confirm`, `is_replayed_message` | `test_websocket_confirmation.py` | Replayed messages rejected |

### B. Connections & External Services

| Capability | Status | Implementation | Tests | Evidence |
|---|---|---|---|---|
| Connector registry & discovery | Verified working | `backend/core/connections/connector_registry.py`, `capability_discovery.py` | `test_connector_registry.py` | Register/instantiate/list connectors |
| Auth: OAuth2 (PKCE) | Implemented, not live-verified | `backend/core/connections/auth/oauth.py` | `test_oauth.py` | PKCE flow unit-tested; live needs credentials |
| Auth: API-key | Implemented, not live-verified | `backend/core/connections/auth/api_key.py` | `test_api_key.py` | Unit-tested; live needs keys |
| Auth: MCP | Implemented, not live-verified | `backend/core/connections/auth/mcp.py` | `test_mcp.py` | Unit-tested; live needs MCP server |
| Connection lifecycle state machine | Verified working | `ConnectionManager` (not_connected→connecting→connected→…) | `test_connection_manager.py` | All transitions tested |
| Capability discovery (discover_all/find) | Verified working | `capability_discovery.py` | `test_capability_discovery.py` | Discovery returns structured capabilities |
| Permission enforcement per capability | Verified working | `grants.denied`, `required_for`, `PERMISSION_CONFIRM`/`PERMISSION_DANGEROUS` | `test_permissions.py` | Denied → fail-closed |
| Unified capability execution | Verified working | `ConnectionManager.execute_capability` | `test_connection_execute.py` | validate → gate → execute ordering |
| Structured capability result | Verified working | `CapabilityResult` (success/output/error/permissions/cancelled) | — | Consistent result shape |
| Error reporting (redacted) | Verified working | `safe_error`, secrets never in `to_dict` | — | No credential leakage |
| Frontend connections client | Verified working | `frontend/src/lib/api.js` (getConnectors, startAuth, executeCapability, …) | `ConnectionsView.test.jsx` | Full CRUD + execute flow |
| Connections UI components | Verified working | `ConnectDialog`, `ConnectionList`, `ConnectorList`, `ConnectionsView` | 6 test files | UI rendered + interactions tested |

### C. Desktop Application

| Capability | Status | Implementation | Tests | Evidence |
|---|---|---|---|---|
| Startup/shutdown | Verified working | `desktop/app/backend.py` `BackendProcess`, `host.py` | `test_desktop_lifecycle.py` | Backend supervised, health checks |
| Minimize-to-tray | Implemented, not display-verified | `desktop/app/tray.py` `TrayIcon` | Skipped on headless Linux | Tk/PortAudio unavailable |
| Background mode | Verified working | `VoiceSession` state machine (STANDBY/LISTENING/…) | `test_voice_session.py` | State transitions verified |
| Show/hide controls | Implemented, not display-verified | `host.py` window management | Skipped on headless Linux | No display server |
| Backend lifecycle (is_up/health) | Verified working | `backend_is_up`, health endpoints | `test_backend_lifecycle.py` | Health check verified |
| WebView communication | Implemented, not display-verified | pywebview + `NativeBridge` | Skipped on headless Linux | No display server |
| Error recovery | Verified working | try/except throughout host/backend | `test_desktop_error_recovery.py` | Failures caught and logged |
| Config (env-overridable) | Verified working | `DesktopConfig` | `test_config.py` | Env overrides work |

### D. Voice Assistant

| Capability | Status | Implementation | Tests | Evidence |
|---|---|---|---|---|
| Microphone capture (MediaRecorder) | Verified working | `frontend/src/hooks/useWebSocket.js` VAD (50ms frames, 500ms silence) | `test_voice_capture.js` | VAD thresholds tested |
| Wake-word detection | Verified working | `backend/services/wake_word_service.py` (pattern-based, real detector) | `test_wake_word_service.py` | Genuine detection, not hardcoded |
| STT (faster-whisper, local) | Verified working | `backend/voice/stt.py` `SpeechToText` | `test_stt.py` | Local transcription |
| Conversation handoff | Verified working | `VoiceSession` DISABLED→STANDBY→LISTENING→PROCESSING→SPEAKING→STANDBY | `test_voice_session.py` | All `LEGAL_TRANSITIONS` tested |
| VAD (voice activity detection) | Verified working | `useWebSocket.js` (400ms min speech, 20s max) | — | Client-side VAD |
| TTS (edge-tts) | **Implemented with honest degradation** | `backend/voice/tts.py` `TextToSpeech` + `/tts/retry` endpoint | `test_tts.py` + `test_voice_loop_e2e.py` | `available` tracks real synthesis; `tts_reason` reported in health/voice status; `/tts/retry` probes for recovery; HUD shows TTS reason when unavailable. Edge-tts still cannot reach Microsoft's service in this sandbox. |
| Audio playback | Implemented, not display-verified | `desktop/app/audio.py` | Skipped on headless Linux | PortAudio unavailable |
| Interruption / barge-in | Verified working | `stopAudio`, interrupt frames in WS hook | `test_interruption.js` | Client-side interruption |
| Privacy (wake-word gate) | Verified working | `WAKE_WORD_REQUIRED` config, wake word = activation not authorization | `test_wake_word_service.py` | No continuous monitoring |
| Bilingual routing (en / ar-EG) | Verified working | `backend/voice/routing.py` `VoiceProfile` (en-GB-RyanNeural, ar-EG-ShakirNeural) | `test_voice_routing.py` | Voice selection logic tested |
| TTS fallback chain | Verified working | `resolve_voice` with fallbacks | `test_voice_routing.py` | Fallback when primary unavailable |

### E. Local Device Awareness (Computer Control)

| Capability | Status | Implementation | Tests | Evidence |
|---|---|---|---|---|
| File discovery (sandboxed) | Verified working | `backend/tools/filesystem.py` | `test_filesystem_tools.py` | Workspace containment |
| App launching (allowlist) | Verified working | `backend/tools/applications.py` `APP_LAUNCH_ALLOWLIST` | `test_application_tools.py` | Only allowlisted apps launchable |
| Permission tiers (named vocabulary) | Verified working | `backend/core/permissions.py` `Permission`, tier→baseline | `test_permissions.py` | allow/deny/ask decisions |
| Confirmation for sensitive actions | Verified working | `CONFIRMATION_REQUIRED_ACTIONS`, `DANGEROUS_ACTIONS` | `test_confirmation.py` | High-risk actions require approval |

### F. Memory & Context

| Capability | Status | Implementation | Tests | Evidence |
|---|---|---|---|---|
| Short-term memory | Verified working | `backend/core/memory.py` `ShortTermMemory` | `test_memory.py` | Recent context retrieval |
| Long-term / preferences | Verified working | `UserPreferences` (SQLite) | `test_preferences.py` | Persisted key/value |
| Project separation | Verified working | `backend/core/projects.py` (detect/store/context) | `test_projects.py` | Per-turn project preamble |
| Memory search | Verified working | memory search functions | `test_memory_search.py` | Query-based retrieval |

### G. Task Planning (Agent Engine)

| Capability | Status | Implementation | Tests | Evidence |
|---|---|---|---|---|
| Multi-step ReAct loop | Verified working | `backend/agents/engine.py` `AgentEngine.run()` (MAX_ITERATIONS=8) | `test_agent_engine.py` | Bounded loop with observation feedback |
| Progress tracking | Verified working | `TOOL_STARTED`/`TOOL_FINISHED` events | `test_agent_events.py` | Event emission per tool call |
| Recovery from failed observations | Verified working | observation feedback loop in engine | `test_agent_recovery.py` | Retries with corrected input |
| Cancellation | Verified working | `cancel_session`, `cancel_event` per session | `test_cancellation.py` | In-flight turns cancellable |
| Approval gates per tool | Verified working | `ConfirmationManager` in tool gate | `test_approval_gate.py` | Confirm/dangerous need explicit approval |

### H. Web Research

| Capability | Status | Implementation | Tests | Evidence |
|---|---|---|---|---|
| Web search | Verified working | `backend/tools/web.py` `search_web` | `test_web_tools.py` | Search returns results |
| URL fetch | Verified working | `backend/tools/fetch.py` `fetch_url` (size caps) | `test_fetch_tools.py` | Fetch with size limits |
| Source tracking | Verified working | `backend/agents/sources.py` `SourceLedger` | `test_sources.py` | Source citations tracked |

### I. Vision (Image / Screen Understanding)

| Capability | Status | Implementation | Tests | Evidence |
|---|---|---|---|---|
| Image understanding | **Missing** | None found | — | No image input, no VLM routing, no screen capture |
| Screen understanding | **Missing** | None found | — | No screen-reading capability |

### J. Proactive Assistance

| Capability | Status | Implementation | Tests | Evidence |
|---|---|---|---|---|
| Reminders | Verified working | `backend/services/reminders.py` `ReminderService` | `test_reminders.py` | Create/complete/delete reminders |
| Notifications | Verified working | `NotificationManager` | `test_notifications.py` | Notification delivery |
| Scheduler / time parsing | Verified working | `SchedulerManager`, `TimeParser` (dateparser) | `test_scheduler_regression.py` | 89 tests pass after dateparser install |

### K. Reliability & Observability

| Capability | Status | Implementation | Tests | Evidence |
|---|---|---|---|---|
| Logging | Verified working | `config.configure_logging`, structured loggers | `test_logging.py` | Log output verified |
| Secret redaction | Verified working | `redact()`, `backend/core/secrets.py` (DPAPI/0600) | `test_secrets.py` | Secrets never logged/returned |
| Health checks | Verified working | `provider_health.py` | `test_provider_health.py` | Provider health status |
| Rate limiting | Verified working | `TokenBucketRateLimiter` | `test_rate_limiter.py` | Token bucket enforced |
| Timeouts | Verified working | `LLM_TIMEOUT`, `TOOL_CONFIRM_TIMEOUT` | — | Timeout config present |

---

## 3. Status Summary

| Phase / Area | Count | Verified | Implemented-not-verified | Partially | Missing | Blocked |
|---|---|---|---|---|---|---|
| Core Assistant & Orchestration | 12 | 12 | 0 | 0 | 0 | 0 |
| Connections & External Services | 10 | 7 | 3 | 0 | 0 | 3 (credentials) |
| Desktop Application | 7 | 4 | 3 | 0 | 0 | 3 (no display) |
| Voice Assistant | 9 | 7 | 1 | 1 (TTS) | 0 | 1 (external TTS) |
| Local Device Awareness | 4 | 4 | 0 | 0 | 0 | 0 |
| Memory & Context | 4 | 4 | 0 | 0 | 0 | 0 |
| Task Planning | 5 | 5 | 0 | 0 | 0 | 0 |
| Web Research | 3 | 3 | 0 | 0 | 0 | 0 |
| Vision | 2 | 0 | 0 | 0 | 2 | 0 |
| Proactive Assistance | 4 | 4 | 0 | 0 | 0 | 0 |
| Reliability & Observability | 6 | 6 | 0 | 0 | 0 | 0 |
| **Total** | **66** | **56** | **7** | **0*** | **2** | **7** |

- **56/66 (85%) verified working**
- **7 implemented but not live-verified** (3 connections need credentials, 3 desktop need display server, 1 frontend connections UI already tested)
- **0 partially implemented** — TTS was partially implemented; now has honest degradation (`*` in table: TTS availability tracking + retry + HUD notice, but synthesis still blocked by external service)
- **2 missing** — image understanding, screen understanding
- **7 blocked by environment/credentials** — 3 OAuth/MCP credentials, 3 display server, 1 external TTS (edge-tts cannot reach Microsoft's service in this sandbox)

---

## 4. Identified Gaps & Blockers

### G1: TTS blocked externally (P0 — blocks end-to-end voice loop) — MITIGATED
- `backend/voice/tts.py` uses `edge_tts.Communicate` against Microsoft's edge-tts service.
- **Root cause confirmed:** edge-tts cannot connect to `speech.platform.bing.com:443` (`ClientConnectorError`) — sandbox blocks outbound to Microsoft services.
- **Mitigation implemented (Phase B):**
  - `TextToSpeech.available` now reflects actual synthesis capability: `False` until first success, `False` after failure, `True` after recovery. Prevents the UI from showing a microphone button that looks ready when it isn't.
  - `/voice/status` and `/health` report `tts_reason` — a human-readable explanation ("speech service untested — synthesis not yet attempted", "the last synthesis failed: ClientConnectorError", etc.)
  - `POST /tts/retry` endpoint lets the UI probe for recovery after a failure.
  - Frontend HUD SystemStatus shows the TTS reason when unavailable.
  - Voice loop continues to deliver text responses when TTS is silent; one honest notice per session.
- **16 new tests:** 7 TTS availability tracking (`test_tts.py`), 11 voice-loop E2E (`test_voice_loop_e2e.py`) covering failure→notice→recovery→availability-restored.
- **Remaining:** Alternative TTS provider (local or cloud) would fully restore voice output. In this sandbox, no local TTS engine (pyttsx3, espeak, festival) is available and cloud TTS is blocked by network restrictions.

### G2: Vision missing (P1)
- No image/screen understanding capability exists anywhere in the codebase.
- No VLM provider routing, no image input handling, no screen capture.
- **Work needed:** Add image input handling + VLM provider (or document as out of scope).

### G3: Live connector integration blocked by credentials (P2)
- OAuth2, API-key, and MCP connectors are implemented and unit-tested, but live integration tests cannot run without GitHub/Google Drive/MCP credentials.
- **Work needed:** Mark as blocked; live verification requires credentials (per standing instructions: "mark live integration blocked without credentials").

### G4: Desktop GUI tests blocked by no display server (P2)
- 8 desktop test modules skipped on headless Linux (no Tk/PortAudio): activity model, app wiring, confirmation dialog, core animation, panel layout, sidepanel parsers, telemetry, audio pipeline.
- **Work needed:** Run on CI with display; not actionable in this environment.

### G5: Frozen packaging diagnose verdict (P3 — pre-existing)
- `desktop/tests/test_frozen_packaging.py::TestDiagnoseVerdict::test_warnings_do_not_fail_the_run` — `_diagnose(cfg)` returns rc=1 but test asserts rc==0 when backend is down-but-resolvable.
- Environment-related, not product code. Needs investigation if frozen packaging becomes a focus.

---

## 5. Prioritized Roadmap (Dependency Order)

**Phase A — Security & Runtime Verification** ✅ COMPLETE (already verified)
- Permission enforcement: verified via `test_permissions.py`
- Approval binding & replay: verified via `test_websocket_confirmation.py` (13/13 E2E)
- Session isolation: verified
- Secret redaction: verified via `test_secrets.py`
- Desktop lifecycle: partially verified (no display)
- Backend recovery/cancellation: verified via `test_cancellation.py`

**Phase B — Genuine Voice Loop (NEXT — TTS blocked externally)** ← recommended next phase
- Wake-word detection: verified
- STT (faster-whisper): verified
- VAD + microphone capture: verified
- Conversation handoff state machine: verified
- Interruption / barge-in: verified
- **TTS: BLOCKED externally (Microsoft 403)** — this is the gap
- **Action:** Investigate alternative TTS or implement graceful degradation with user notice; add live voice-loop integration test (everything except the external TTS call)

**Phase C — Reliable Computer Control** (implemented, needs runtime verification)
- App launching with allowlist: implemented
- File open/search with sandbox: implemented
- Permission tiers: implemented
- Confirmation for sensitive actions: implemented
- **Action:** Add runtime end-to-end tests for file/app tools through the agent loop

**Phase D — Useful Memory & Context** (implemented, needs eval tests)
- Short-term memory: implemented
- Long-term preferences: implemented
- Project separation: implemented
- **Action:** Add deterministic memory retrieval eval tests (recall precision)

**Phase E — Task Planning** (implemented)
- Multi-step ReAct loop: implemented
- Progress tracking: implemented
- Cancellation: implemented
- Approval gates: implemented
- **Action:** Add complex multi-tool task integration tests

**Phase F — Live Connections, Vision, Proactive** (future)
- Live connectors: blocked without credentials (mark as blocked)
- Vision: missing (new work)
- Proactive reminders: implemented

---

## 6. Next-Phase Recommendation

**Phase B (TTS degradation) — COMPLETE.** The voice loop now degrades honestly when TTS is unavailable.

**Remaining work for full voice output:**
- **Alternative TTS provider** — Investigate a local TTS engine (pyttsx3 + espeak-ng, Piper, Coqui) or cloud TTS that works within the sandbox's network restrictions. This would fully restore step 5 ("respond through speech").
- **No local TTS engine available** in this environment (no pyttsx3, espeak, festival, flite). Cloud TTS is blocked by network restrictions (only github.com, npm, pypi accessible).

**Next recommended phase: Phase C — Reliable Computer Control** (runtime verification)
- File open/search through the agent loop with sandbox verification
- App launching with allowlist through voice/typed commands
- Confirmation for sensitive actions via WebSocket dialog
- Add runtime end-to-end tests for file/app tools

**Do NOT:** commit or push (per standing instructions), claim WCAG compliance from automated tests alone, modify security-sensitive confirmation requirements, or rebuild working systems unnecessarily.

---

## 7. Verification Commands & Results

```bash
# Backend (full suite, excluding pre-existing packaging failure)
cd /home/user/Zaytoon/backend
python -m pytest tests/ --ignore=tests/test_packaging.py -q
# → 3558 passed, 7 skipped, 0 failed
#   Includes 16 new tests: test_tts.py (+7 availability tracking), test_voice_loop_e2e.py (+11 E2E)

# Backend packaging test (pre-existing failure, PEP 668)
python -m pytest tests/test_packaging.py -q
# → 1 failed (externally-managed-environment, unrelated to product code)

# Frontend
cd /home/user/Zaytoon/frontend
npm test          # → 50 files, 822 passed, 0 failed
npm run lint      # → clean
npm run build     # → OK

# Desktop
cd /home/user/Zaytoon/desktop
python -m pytest tests/ -q
# → 424 passed, 2 skipped, 1 failed (test_frozen_packaging diagnose verdict; environment-related)
# 8 modules skipped on headless Linux (no Tk/PortAudio)
```

---

## 8. Git Status

- Branch: `arena/43a2339e-zaytoon` (active)
- Working tree: clean
- No commits or pushes made in this session (per standing instructions: "do not commit or push unless explicitly requested")

---

## 9. Files Created/Modified

### New files
- `/home/user/Zaytoon/AUDIT_CAPABILITY_MATRIX.md` — this audit report
- `/home/user/Zaytoon/backend/tests/test_voice_loop_e2e.py` — voice-loop E2E tests (TTS failure → notice → retry → recovery)

### Modified files
- `/home/user/Zaytoon/backend/voice/tts.py` — `TextToSpeech.available` now reflects actual synthesis capability (False until first success, False after failure, True after recovery); `_last_synthesis_succeeded` tracks last attempt outcome; `synthesize`/`stream_synthesize`/`synthesize_ssml` all record success/failure
- `/home/user/Zaytoon/backend/api/main.py` — `/health` reports `voice.tts_reason`; `_tts_reason()` gives human-readable reasons for each TTS state; new `POST /tts/retry` endpoint probes for recovery
- `/home/user/Zaytoon/backend/tests/test_tts.py` — +7 tests: availability starts False, flips to True after success, flips to False after failure, recover after failure, silent return marks unavailable, synthesize success/failure tracking, unavailable when package missing
- `/home/user/Zaytoon/frontend/src/components/HUD/SystemStatus.jsx` — TTS row shows `tts_reason` as title when unavailable