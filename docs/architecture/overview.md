# Zaytoon — Verified Architecture Overview

**Status:** Verified against the current implementation on branch `arena/9219dec0-zaytoon` (HEAD `5bda335`).
**Scope:** This document describes what actually exists in the codebase, not aspirational or planned state. It complements `docs/ARCHITECTURE.md` (which records the design intent and target state) and `AGENTS.md` (implementation conventions).

## 1. System boundary

Zaytoon is a local-first, bilingual (Arabic/English) multi-agent AI assistant. Three deployment modes exist:

| Mode | Description |
|------|-------------|
| Web | React/Vite frontend on Vite dev server (`:5173`), backend on FastAPI/uvicorn (`:8000`) |
| Desktop | pywebview host (WebView2 on Windows, WebKit/GTK on Linux) serving the React UI, spawning/adopting the backend |
| Docker | `docker-compose.yml` wires backend + nginx-served frontend |

No authentication or multi-tenancy exists. The trust boundary is the local machine: the backend listens on loopback, WebSocket connections are origin-policy protected, and destructive actions require human-in-the-loop confirmation.

## 2. Component map

```
USER (text / voice)
      │
      ▼
┌──────────────┐      HTTP + WS      ┌─────────────────────────┐
│  React UI    │◄────────────────────►│   FastAPI Backend       │
│  (frontend/) │   ws://127.0.0.1:8000│   (backend/)            │
└──────────────┘      :5173 → :8000   └───────────┬───────────┘
                                                   │
                                                   ▼
                                          ┌─────────────────┐
                                          │   Desktop host  │
                                          │  (desktop/)     │
                                          │  pywebview host │
                                          │  spawns backend │
                                          └─────────────────┘
```

### Frontend (`frontend/`)
- **Framework:** React 18 + Vite 5 + Tailwind 3, JavaScript/JSX (no TypeScript)
- **State:** JS state modules (`lib/voiceState.js`, `lib/statusLabels.js`, `lib/toolRows.js`, etc.) + React component state
- **WebSocket client:** `src/hooks/useWebSocket.js` — single state machine implementing the protocol; handles audio recording, VAD, confirmations, reconnection
- **REST client:** `src/lib/api.js` — `/health`, `/chat`, `/stt`, `/tts`, `/command/*`, `/settings/workspace`
- **Key surfaces:** ChatView, InputBar, WorkspaceShell (CodeWorkspace, FileExplorer, GitPanel, TerminalPanel, ModelsPanel, ProjectsPanel, SkillsPanel, TaskPanel), SettingsPanel, ConfirmationDialog, ReminderPanel, HUD (NexusDashboard, HUDPanel), SecurityView, VirtualMachinesView, ComputerView, ProvidersSection/SetupWizard
- **Tests:** 752 tests (44 files) under `src/**/*.test.{js,jsx}`, vitest + jsdom

### Backend (`backend/`)
- **Framework:** Python 3.11 + FastAPI + uvicorn; asyncio throughout
- **Entrypoint:** `backend/main.py` → `api/main.py` (app factory, lifespan, CORS, rate limit, exception handlers)
- **HTTP API:** `api/main.py` (46 route handlers) + `core/api/` routers (tasks, projects, files, git, registry, virtualization, approvals)
- **WebSocket:** `api/websocket.py` — 18 frame types, streaming TTS, per-session state, confirmations, dedupe (`client_msg_id`), interrupt
- **Agent dispatch:** `services/agent_service.py` → `agents/` (RouterAgent, FastAgent, PlannerAgent, CodeAgent, ToolAgent, MemoryAgent, SchedulerAgent) + `agents/engine.AgentEngine` (bounded ReAct loop, ≤8 iterations)
- **LLM path:** `services/llm_client.LLMClient` → `services/ai_provider` (OllamaProvider, OpenRouterProvider) + `services/model_registry` + `services/completion_validator`; single attempt budget (`OPENROUTER_MAX_ATTEMPTS=3`), bounded concurrency (`LLM_MAX_CONCURRENCY=2`)
- **Tools:** `tools/registry.py` — `validate() → risk gate → execute()`; 36+ tools; `tools/sandbox.py` (unconditional blocks), `tools/safety.py` (RiskAssessor + ConfirmationManager)
- **Core runtime:** `core/orchestrator.py` (one turn → one task record), `core/db.py` (SQLite + migrations), `core/tasks/` (lifecycle, DAG graph), `core/projects/`, `core/skills/`, `core/providers/`, `core/workspace/` (files, git, terminal), `core/paths.py` (portable layout), `core/permissions.py` (10 named permissions), `core/secrets.py` (DPAPI/0600), `core/events.py` (event bus)
- **Voice:** `voice/stt.py` (faster-whisper), `voice/tts.py` (edge-tts), `voice/routing.py` (bilingual voice selection), `services/wake_word_service.py` (wake-word gate)
- **Scheduler/Reminders:** `scheduler/` (TimeParser, SchedulerManager), `reminder/` (ReminderService)
- **Database:** Three SQLite files — `data/zaytoon.db` (memory + preferences), `data/audit.db` (execution audit), `data/core.db` (tasks, projects, skills, providers). Separate files to avoid lock contention.
- **Tests:** 3486 tests (~72 files) under `tests/` + `tests/core/` (16 files); pytest + pytest-asyncio

### Desktop (`desktop/`)
- **Host:** Python + pywebview; WebView2 (Windows) / WebKit GTK (Linux)
- **Entry:** `desktop/main.py` (`--diagnose`, `--dev`, `--no-backend`, `--serve`)
- **Window:** `app/host.py` (WebViewHost — window, URL, health check, window state, backend lifecycle)
- **Bridge:** `app/bridge.py` — closed JS⇄Python API (window controls, file dialogs, reveal-in-OS, open_path, open_url, notify, clipboard, system info); no arbitrary execution
- **Backend supervisor:** `app/backend.py` — spawns/adopts the backend process, health checks, process-group shutdown on close
- **Protocol:** `app/protocol.py` — centralized frame builders + ServerEvent decoding
- **Path policy:** `app/paths.py` — `open_path`/`reveal_in_os` containment policy (`path_policy.classify()`)
- **Packaging:** `scripts/build_exe.py` — PyInstaller onedir bundle; `installer/zaytoon.iss` — Inno Setup installer
- **Tests:** 404 tests under `desktop/tests/`

## 3. Data stores

| File | Owner | Contents |
|------|-------|----------|
| `data/zaytoon.db` | MemoryAgent | Conversation history, user preferences |
| `data/audit.db` | `common/audit.py` | One row per tool invocation (tool, command, tier, approval, success, exit code, duration, error) |
| `data/core.db` | `core/db.py` | Tasks, projects, skills, providers, permission grants |

All three files are separate to avoid the `database is locked` contention that occurred when audit shared `zaytoon.db` with the memory agent.

## 4. Key design invariants

- **No ungated tool path:** The WebSocket `command` message type was removed. All tool execution crosses `ToolRegistry.execute` (`validate → gate → execute`). The gate runs before the result cache, so a cached result cannot bypass approval.
- **No silent provider fallback:** Ollama (local) does not fall back to OpenRouter (hosted), and vice versa. Degradation is a reported failure.
- **OpenRouter free-only:** Models must carry `:free` and be allow-listed; `openrouter/free` (uncontrolled auto-router) is explicitly refused.
- **Three SQLite files:** Memory, audit, and core each own a file; cross-file consolidation is prohibited by test.
- **Fail-closed confirmations:** Timeout, disconnect, or error on a confirmation request denies the action.
- **No secret logging:** `common/reporting.redact()` masks key-shaped strings; no route returns a secret; a test asserts no read path returns a saved key.

## 5. Verified behaviors (from test suite)

- Tool registry contract: self-describing, schema declares requirements, aliases resolve, validate→gate→execute ordering
- Parallel tool execution: safe-tier calls run concurrently under `asyncio.Semaphore(4)`; confirm/dangerous calls are barriers; failure isolation
- Fast path: regex intent matching for common phrasings (get_time, etc.) with 0 model calls; bypasses only for safe-tier tools
- Wake-word gate: continuous listening requires the wake word; manual bypass is opt-in via config
- Bilingual voice: Egyptian Arabic (`ar-EG-ShakirNeural`) / English (`en-GB-RyanNeural`) with dynamic switching and fallbacks
- Path containment: `os.path.commonpath` (not `str.startswith`); `C:\app_evil` rejected under `C:\app`
- Two-step approval tokens for destructive REST routes (git commit/checkout/push, terminal open)
