# Zaytoon AI

**A production-ready multi-agent AI assistant** with voice, memory, scheduling, and streaming TTS. Bilingual Arabic (Egyptian) and English.

Cloud-only and **zero cost**: every model is a hard-allow-listed OpenRouter
`:free` variant. Local model execution (Ollama / llama.cpp / on-device GGUF) has
been removed entirely, and a non-free or non-allow-listed model is rejected
before any network request is made.

## Quick Start

```bash
# One-command start (requires Python, Node.js):
start.bat

# Or manually:
cd backend && .venv\Scripts\activate && python main.py     # Terminal 1
cd frontend && npm run dev                                   # Terminal 2
```

| Service | URL |
|---------|-----|
| Web UI  | http://localhost:5173 |
| API     | http://127.0.0.1:8000 |
| Health  | http://127.0.0.1:8000/health |
| WS      | ws://localhost:8000/ws |

## Features

- **Multi-Agent Architecture** — Router → FastAgent (chat), PlannerAgent (complex), CodeAgent (code), ToolAgent (system), SchedulerAgent (reminders), MemoryAgent (history)
- **Zero-Cost LLM Stack** — OpenRouter `:free` models only, with an automatic fallback chain (primary → `openrouter/free`); the allow-list and free-form checks both run before any request
- **Tool Safety Sandbox** — high-risk OS actions pause and require explicit human approval over the WebSocket; shell injection is blocked outright
- **Voice Interface** — Speech-to-text (faster-whisper) + streaming text-to-speech (edge-tts) with voice activity detection
- **Dynamic Bilingual TTS** — voice is chosen per message from the detected language: `ar-EG-ShakirNeural` for Egyptian Arabic, `en-GB-RyanNeural` for English, with automatic fallbacks
- **Arabic / English** — Full bilingual support with Egyptian Arabic dialect detection
- **Scheduling** — Parse natural time expressions in Arabic/English, persistent reminders across sessions
- **Streaming Responses** — Real-time token streaming + early TTS trigger for immediate audio feedback
- **Desktop App** — Tauri floating holographic orb with global hotkeys (`Ctrl+Space` to summon from any app) and `.msi` / `.exe` installers

## Architecture

```
root/
├── backend/        # Python FastAPI — agents, voice, scheduler, tools, memory
│   ├── api/        # REST + WebSocket endpoints
│   ├── agents/     # 6 specialized AI agents
│   ├── services/   # LLMClient (provider + model fallback chain)
│   ├── voice/      # STT (whisper) + TTS (edge-tts) + language routing
│   ├── scheduler/  # Time parser + reminder manager
│   ├── tools/      # OS toolkit (cmd, file, system) + safety sandbox
│   └── tests/      # 587 pytest tests
├── frontend/       # React + Vite web client
└── desktop/        # Tauri native desktop client (orb + global hotkeys)
```

## Setup

```bash
# 1. Python environment
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt      # or requirements.txt for runtime only
cp .env.example .env
# Edit .env -> set OPENROUTER_API_KEY (https://openrouter.ai/keys)
# The key is required: there is no local model fallback.

# 2. Frontend
cd frontend
npm install

# 3. (Optional) Desktop orb -- needs a Rust toolchain
cd desktop && npm install && npm run icons && npm run tauri:dev
```

### Model configuration

These three are also the hard-coded allow-list in `backend/config.py`
(`ALLOWED_FREE_MODELS`). A model that is free but *not* on that list is still
refused, so a typo or an injected model id can never reach the network.

| Setting | Default | Role |
|---------|---------|------|
| `OPENROUTER_PRIMARY_MODEL` | `qwen/qwen3.8-27b:free` | chat, tools, routing |
| `OPENROUTER_REASONING_MODEL` | `thinkingmachines/inkling-small:free` | code, planning, scheduling |
| `OPENROUTER_FALLBACK_MODEL` | `openrouter/free` | when the primary is rate-limited |

Anything else raises `NonFreeModelError` before any network call. There is no
local fallback — if OpenRouter is unreachable the assistant says so rather than
switching providers. `GET /health` reports the chain, per-model health, and
request/response metrics.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.10+, FastAPI, httpx, Uvicorn |
| AI | OpenRouter free models only (hard-allow-listed) |
| STT | faster-whisper (tiny model, int8) |
| TTS | edge-tts — auto-selected: `ar-EG-ShakirNeural` / `en-GB-RyanNeural` |
| Database | SQLite via aiosqlite |
| Frontend | React 18, Vite, Tailwind CSS, GSAP |
| Desktop | Tauri 1.x (Rust + React) |

## Development

```bash
cd backend && python -m pytest tests/ -q      # 587 tests
cd backend && python scripts/verify_final.py  # live smoke check
cd desktop && npm run tauri:build             # .msi + .exe installers
```

CI runs the backend suite, the free-only guard, the frontend build, and the
Tauri bundle-config check on every push. See `AGENTS.md` for full architecture
notes and `desktop/README.md` for packaging.

## Security model

Tool execution has two independent layers:

1. **Unconditional blocks** — shell metacharacters/chaining (`&`, `|`, `;`,
   `` ` ``, `$(...)`, `>`, `%VAR%`), path traversal via `os.path.commonpath`,
   script-type app targets, and shell interpreters for `run_code` are refused
   outright. No approval overrides them.
2. **Human-in-the-loop** — destructive or high-risk actions emit a
   `require_confirmation` WebSocket event and block until the user approves.
   Timeout, disconnect, or error all deny (fail closed).

The gate runs *before* the tool result cache, so a cached result can never
bypass an approval.
