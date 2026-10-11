# Zaytoon AI

**A production-ready multi-agent AI assistant** with voice, memory, scheduling, and streaming TTS. Bilingual Arabic (Egyptian) and English.

**Local-first.** `AI_PROVIDER=ollama` is the default: inference runs against your
own Ollama daemon, needs no key, and Zaytoon never downloads a model for you — a
missing one is reported with the exact `ollama pull` command.

`AI_PROVIDER=openrouter` is the optional cloud path, and its zero-cost policy is
unchanged: every model must be a hard-allow-listed OpenRouter `:free` variant, so
a non-free or un-listed id is rejected before any network request is made.

**There is no fallback between the two.** A stopped daemon does not start cloud
calls, and a rate-limited cloud response does not start running weights on your
machine. Degradation is a reported failure with a bilingual message, never a
silent switch. (Until 2026-10-10 this page claimed local execution "has been
removed entirely", two weeks after `config.py` made it the default — see
`docs/PHASE0_BASELINE.md`, decision D6).

## Quick Start

```bash
# One-command start (requires Python 3.11+ and Node.js):
start.bat          # Windows
./start.sh         # Linux / macOS

# Or manually:
cd backend && .venv/Scripts/activate && python main.py   # Terminal 1 (Windows)
cd backend && source .venv/bin/activate && python main.py # Terminal 1 (POSIX)
cd frontend && npm run dev                                # Terminal 2
```

| Service | URL |
|---------|-----|
| Web UI  | http://localhost:5173 |
| API     | http://127.0.0.1:8000 |
| Health  | http://127.0.0.1:8000/health |
| WS      | ws://localhost:8000/ws |

## Features

- **Multi-Agent Architecture** — Router → FastAgent (chat), PlannerAgent (complex), CodeAgent (code), ToolAgent (system), SchedulerAgent (reminders), MemoryAgent (history)
- **Local-first inference** — Ollama with one model per tier (`general` / `coding` / `fast`), env-configurable, never auto-downloaded.
- **Zero-cost cloud option** — OpenRouter `:free` models only, with an explicit fallback chain (primary → configured free pool); the allow-list and free-form checks both run before any request. `openrouter/free` is explicitly excluded as an uncontrolled auto-router.
- **Tool Safety Sandbox** — high-risk OS actions pause and require explicit human approval over the WebSocket; shell injection is blocked outright
- **Voice Interface** — Speech-to-text (faster-whisper) + streaming text-to-speech (edge-tts) with voice activity detection
- **Dynamic Bilingual TTS** — voice is chosen per message from the detected language: `ar-EG-ShakirNeural` for Egyptian Arabic, `en-GB-RyanNeural` for English, with automatic fallbacks
- **Arabic / English** — Full bilingual support with Egyptian Arabic dialect detection
- **Scheduling** — Parse natural time expressions in Arabic/English, persistent reminders across sessions
- **Streaming Responses** — Real-time token streaming + early TTS trigger for immediate audio feedback
- **Desktop App** — a native window hosting the same React UI through pywebview (WebView2 on Windows, WebKit/GTK on Linux), with a closed OS bridge (file dialogs, reveal-in-OS, notifications, clipboard) and PyInstaller `.exe` builds

## Architecture

```
root/
├── backend/        # Python FastAPI — agents, voice, scheduler, tools, memory
│   ├── api/        # REST + WebSocket endpoints
│   ├── agents/     # 7 agents + a bounded ReAct engine
│   ├── services/   # LLMClient (provider + model fallback chain)
│   ├── voice/      # STT (whisper) + TTS (edge-tts) + language routing
│   ├── scheduler/  # Time parser + reminder manager
│   ├── tools/      # OS toolkit (cmd, file, system) + safety sandbox
│   └── tests/      # 2,600+ pytest tests (contract-level, not implementation)
├── frontend/       # React + Vite web client
└── desktop/        # pywebview desktop host — wraps the React UI, spawns the backend
```

## Setup

```bash
# 1. Python environment
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt      # or requirements.txt for runtime only
cp .env.example .env
# Defaults to AI_PROVIDER=ollama, which needs no key — it needs `ollama serve`
# and the three models pulled (see backend/.env.example for the exact commands).
# Only set AI_PROVIDER=openrouter + OPENROUTER_API_KEY if you want the cloud
# path. Nothing falls back between the two.

# 2. Frontend
cd frontend
npm install

# 3. (Optional) Desktop host
cd desktop && python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py            # or: python main.py --diagnose
```

### Model configuration

These three are also the hard-coded allow-list in `backend/config.py`
(`ALLOWED_FREE_MODELS`). A model that is free but *not* on that list is still
refused, so a typo or an injected model id can never reach the network.

| Setting | Default | Role |
|---------|---------|------|
| `OPENROUTER_PRIMARY_MODEL` | `qwen/qwen3.8-27b:free` | chat, tools, routing |
| `OPENROUTER_REASONING_MODEL` | `cohere/north-mini-code:free` | code, planning, scheduling |
| `OPENROUTER_FALLBACK_MODEL` | `nvidia/nemotron-3-super-120b-a12b:free` | explicit fallback (not auto-router) |
| `OPENROUTER_FREE_FALLBACK_MODELS` | `nvidia/nemotron-3-super-120b-a12b:free,nvidia/nemotron-3.5-lightning:free,cohere/north-mini-code:free,poolside/laguna-s-2.1:free` | ordered fallback pool |

Anything else raises `NonFreeModelError` before any network call. If OpenRouter
is unreachable the assistant says so rather than switching providers — and if you
are on `AI_PROVIDER=ollama`, these four variables are not what runs; the
`OLLAMA_*` models are. `GET /health` reports the provider, the chain, per-model
health and request/response metrics.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.10+, FastAPI, httpx, Uvicorn |
| AI | Ollama (default, local) or OpenRouter `:free` (optional, cloud) |
| STT | faster-whisper (tiny model, int8) |
| TTS | edge-tts — auto-selected: `ar-EG-ShakirNeural` / `en-GB-RyanNeural` |
| Database | SQLite via aiosqlite |
| Frontend | React 18, Vite, Tailwind CSS, GSAP (JavaScript/JSX, not TypeScript) |
| Desktop | Python 3.11+, pywebview (WebView2 / WebKit), websockets, PyInstaller |

## Development

```bash
cd backend && python -m pytest tests/ -q      # 2,300+ tests, ~95 s
cd backend && python scripts/verify_final.py  # live smoke check
cd desktop && python scripts/build_exe.py     # PyInstaller onefile build
```

CI runs the backend suite (Windows **and** Linux), the provider-policy guards, the
frontend lint/test/build, and the desktop shell tests on every push. See
`AGENTS.md` for the full architecture notes; desktop packaging lives in
`desktop/scripts/build_exe.py` and `docs/ARCHITECTURE.md` §5.

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