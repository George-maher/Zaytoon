# Zaytoon — Full System Audit Report

**Date**: 2026-10-01  
**Auditor**: OpenCode Agent  
**Scope**: Complete codebase audit for message duplication, desktop stability, and UI/UX overhaul

---

## 1. Architecture Overview

### Backend (FastAPI + WebSocket)
```
backend/
├── main.py                    # Thin entry point
├── api/
│   ├── main.py               # FastAPI app, REST routes, lifespan
│   └── websocket.py          # WS handler, session management, streaming TTS
├── agents/                   # 8 agents + AgentEngine (bounded ReAct loop)
├── services/
│   ├── llm_client.py         # OpenRouter client: free-only, contract validation
│   ├── model_registry.py     # Free model pool + capabilities
│   ├── completion_validator.py # Deterministic contract checking
│   └── agent_service.py      # Intent dispatch → agent orchestration
├── tools/                    # 30 tools via single ToolRegistry
│   ├── registry.py           # validate → gate → execute (THE chokepoint)
│   ├── safety.py             # RiskAssessor + ConfirmationManager
│   └── sandbox.py            # Unconditional blocks (shell injection, path escape)
├── voice/                    # STT (faster-whisper) + TTS (edge-tts)
├── scheduler/ + reminder/    # Time parsing + reminder service
└── memory/                   # Short-term + preferences (SQLite)
```

### Frontend (React 18 + Vite 5 + Tailwind 3)
```
frontend/src/
├── hooks/useWebSocket.js     # Single WS protocol implementation (THE seam)
├── components/
│   ├── Chat/                 # ChatTranscript, ChatMessage, ActivityFeed
│   ├── HUD/                  # AICore, SystemStatus, MissionStatus, CommandConsole
│   ├── ConfirmationDialog.jsx
│   ├── SettingsPanel.jsx
│   └── ReminderPanel.jsx
├── styles/                   # CSS variables + Tailwind
└── lib/api.js               # REST client
```

### Desktop (Python/customTkinter)
```
desktop/app/
├── app.py                    # Main thread: orb, panel, tray, hotkeys, WS client
├── client.py                 # Blocking WS client on dedicated thread + queue
├── backend.py                # FastAPI child process supervisor
├── panel.py                  # Chat panel with transcript, composer, confirmations
├── orb.py                    # Floating orb with colour-key transparency
├── protocol.py               # Shared WS protocol (builders + typed events)
├── hotkeys.py                # Global hotkeys via `keyboard` package
├── audio.py                  # Mic capture + TTS playback
└── config.py                 # All values env-overridable
```

---

## 2. Critical Bug: Message Duplication — Root Cause Analysis

### Symptoms
- Assistant responses repeated continuously in Web app
- Can also occur in Desktop app
- Particularly bad during streaming/fallback scenarios

### Root Causes Identified

#### A. Backend: Multiple `message` frames for same logical response
**File**: `backend/api/websocket.py`, lines 633-651
```python
# After agent completes, sends final message frame
await connection_manager.send(websocket, {
    "type": "message", "content": final_content, "response_id": response_id,
})
```
**Problem**: If the agent loop retries (fallback to another model), the same logical response can be sent with a **new** `response_id`, bypassing frontend deduplication.

#### B. Backend: Streaming + final message race
**File**: `backend/api/websocket.py`, lines 582-608 (`stream_callback`)
- `token` frames sent during streaming
- `message` frame sent after completion
- If TTS triggers early (line 595-608), `stream_callback` continues but `final_content` keeps growing
- Race: `message` frame sent while `token` frames still in flight

#### C. Frontend: Insufficient deduplication
**File**: `frontend/src/hooks/useWebSocket.js`, lines 212-237
```javascript
case "message":
  if (data.response_id && renderedResponseIds.current.has(data.response_id)) break;
  if (data.response_id) renderedResponseIds.current.add(data.response_id);
  // ... replaces streaming OR adds new
```
**Problems**:
1. Only checks `response_id` — if backend sends new ID for same content, duplicate rendered
2. No content-hash deduplication as fallback
3. `renderedResponseIds` never cleared except `clearChat()`

#### D. Desktop: Stale stream handling
**File**: `desktop/app/client.py`, lines 153-159
```python
if event.type is protocol.EventType.TOKEN:
    response_id = event.data.get("response_id")
    if response_id and self._active_response and response_id != self._active_response:
        return  # Discards stale stream
```
**Problem**: Only handles `TOKEN` frames. `MESSAGE` frames reset `_active_response = None` (line 158-159), so a late `message` frame from a cancelled turn can still render.

#### E. Reconnection creates duplicate sessions
**File**: `backend/api/websocket.py`, line 91
```python
session_id = f"sess_{int(time.time())}_{hash(client_ip) % 10000}"
```
New session = new `SessionState` = new `renderedResponseIds` equivalent = no cross-session deduplication.

---

## 3. Desktop Application Issues

### A. Multiple WebSocket Client Starts
**File**: `desktop/app/app.py`, lines 99-100
```python
def run(self):
    self.client.start()  # Called every run()
```
If `run()` called twice or `start_backend()` triggers restart, `client.start()` can create multiple threads.

### B. Event Pump Race Conditions
**File**: `desktop/app/app.py`, lines 244-259
- `_pump()` drains queue every 30ms
- `_handle()` processes `ServerEvent` objects
- No guard against duplicate event processing

### C. Hotkey Re-registration
**File**: `desktop/app/app.py`, line 98
```python
self._register_hotkeys()  # Called once in run()
```
But `HotkeyManager.register()` doesn't check if already bound (logs warning but continues).

### D. Backend Process Management
**File**: `desktop/app/backend.py` - `ensure_running()` can be called multiple times, spawning multiple processes if port probe races.

---

## 4. OpenRouter Reliability — Current State

### Strengths
- ✅ Free-model only policy enforced at multiple layers
- ✅ Contract validation catches invalid completions (e.g., "User Safety: safe")
- ✅ Attempt budget (`OPENROUTER_MAX_ATTEMPTS=3`) prevents infinite fallback
- ✅ Capability routing (tools → models that support tools)
- ✅ Circuit breaker benches rate-limited models (90s for 429)
- ✅ Structured logging per attempt with request_id

### Gaps
- ⚠️ No request/response correlation ID propagated to frontend
- ⚠️ Retry-after-429 moves to next model but doesn't preserve stream state
- ⚠️ Frontend doesn't show "retrying" state

---

## 5. Concurrency Issues

### Backend
- `session_states` dict accessed from multiple WebSocket handlers — no locks
- `ConnectionManager.active` list modified during iteration (broadcast uses `.copy()`)
- `AgentEngine` uses asyncio but tool execution is sequential (by design)

### Frontend
- `useWebSocket` hook: multiple `useEffect` with overlapping dependencies
- `isSocketOpen()` reads ref twice (TOCTOU fixed by helper)
- Audio recording + VAD on main thread (potential blocking)

### Desktop
- Tkinter main thread + WS thread + audio thread + hotkey thread
- Queue-based communication is correct pattern
- But `client.start()` not idempotent

---

## 6. UI/UX — Current State vs Target

### Current
- Web: HUD-style with cyan accents, monospace fonts, grid background
- Desktop: Similar but customTkinter, floating orb + panel
- Both functional but "dashboard-like" not "command center"

### Target (per requirements)
- Advanced AI command center / tactical computer
- JARVIS-inspired / Batman-style dark tactical
- System telemetry (CPU, MEM, NET, MODEL, LATENCY)
- AI core visualization (IDLE/LISTENING/THINKING/PROCESSING/TOOL/RESPONDING/SPEAKING/ERROR)
- Technical, minimal, professional

---

## 7. Test Coverage

### Backend: 1165 tests
- Router routing order (reachability, not regex)
- Agent engine: loop, events, malformed replies, approval flow
- Tool registry: validate→gate→execute ordering
- LLM client: free-only, contract validation, fallback
- Voice routing: locale, dynamic switching, STT handoff
- Security sandbox: injection, path containment, risk gate

### Frontend: 102 tests + lint + build
- `useWebSocket.test.js`: Protocol reducer (high value — both clients depend on it)
- Component tests

### Desktop: 78 tests
- Backend lifecycle (never touches foreign process)
- Protocol encoding/decoding
- Hotkey handling
- Repo root discovery

---

## 8. Recommended Fixes (Priority Order)

### P0 — Message Duplication (Critical)
1. **Backend**: Single `response_id` per logical turn, propagated to all frames
2. **Backend**: Deduplication at session level + content hash
3. **Frontend**: Content-hash deduplication + `response_id` tracking
4. **Desktop**: Stale frame filtering for ALL frame types, not just TOKEN
5. **Both**: Request/response tracing with unique trace IDs

### P1 — Desktop Stability
1. Make `BackendClient.start()` idempotent
2. Add connection state machine (connecting/connected/disconnecting)
3. Proper cleanup on reconnect (cancel pending confirmations, clear audio)
4. Fix hotkey registration to be idempotent

### P2 — OpenRouter Observability
1. Propagate `request_id` to frontend for correlation
2. Add "retrying" status frame during fallback
3. Frontend shows model being used + attempt number

### P3 — UI/UX Overhaul
1. Shared design system (CSS variables, component primitives)
2. Desktop: Command center layout (system modules | conversation | system status)
3. Web: Matching layout, responsive
4. AI Core visualization with state transitions
5. System telemetry panel (real metrics where possible)

### P4 — Architecture Improvements
1. Structured logging with trace IDs
2. Cancel tokens for all long-running operations
3. Worker threads for CPU-heavy tasks (STT, TTS)
4. Connection pooling for OpenRouter

---

## 9. Files Requiring Changes

### Backend
- `backend/api/websocket.py` — Response ID management, deduplication, status frames
- `backend/services/llm_client.py` — Propagate request_id, add retry status
- `backend/domain/models.py` — Add trace ID to SessionState

### Frontend
- `frontend/src/hooks/useWebSocket.js` — Content-hash deduplication, trace ID tracking
- `frontend/src/components/ChatMessage.jsx` — Add trace ID display
- `frontend/src/styles/index.css` — Design system variables
- New components for AI Core, System Telemetry, Command Center layout

### Desktop
- `desktop/app/client.py` — Stale frame filter for all types, idempotent start
- `desktop/app/app.py` — Connection state machine, cleanup on reconnect
- `desktop/app/panel.py` — Command center layout, AI Core, telemetry
- `desktop/app/protocol.py` — Add trace_id to ServerEvent

### Shared
- `docs/DESIGN_SYSTEM.md` — Formalize design tokens
- New: `shared/design-tokens.json` — Single source of truth

---

## 10. Validation Plan

### Regression Tests for Message Duplication
```python
# Backend
test_single_message_per_user_action()
test_streaming_then_final_no_duplicate()
test_fallback_same_response_id()
test_reconnect_no_duplicate_sessions()

# Frontend
test_token_then_message_single_bubble()
test_duplicate_message_frame_ignored()
test_content_hash_deduplication()

# Desktop
test_stale_message_frame_discarded()
test_reconnect_clears_pending_state()
```

### Integration Tests
- Full WS round-trip with simulated fallback
- Desktop + backend concurrent requests
- Rapid consecutive messages (10 in 1 second)

### Manual Verification
- Web: Send message, verify single response
- Web: Pull network cable during stream, verify reconnect + no dup
- Desktop: Toggle orb/panel rapidly, verify no duplicate listeners
- Desktop: Restart backend, verify shell adopts existing process

---

*End of Audit Report*