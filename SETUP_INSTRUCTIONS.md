# Zaytoon — Setup Instructions

For the 5-minute path see [QUICK_START.md](QUICK_START.md). This is the version
with the knobs named, so that when something does not come up you know which of
these files is the one to edit.

## Prerequisites

| Need | Version / note |
| --- | --- |
| Python | 3.11 (what CI runs). The backend uses `tomllib`-era stdlib and `asyncio` APIs available from 3.10 up. |
| Node | 20 (what `frontend/Dockerfile` builds with). `npm install` in `frontend/`. |
| Ollama | optional at install time; required for the default provider. `ollama serve` plus the models named in `backend/.env`. |
| OpenRouter key | only when `AI_PROVIDER=openrouter`. Nothing falls back between the two providers. |

## 1. Backend

```bash
cd backend
python -m venv .venv
.venv/bin/activate                    # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt   # or requirements.txt for runtime only
cp .env.example .env
python main.py                        # http://127.0.0.1:8000
```

**Run it from `backend/`.** `main.py` depends on that directory being the import
root; from the repository root the modules are not found.

What `.env` actually controls (see `backend/config.py` for the authoritative
defaults and validation):

- `AI_PROVIDER` — `ollama` (default) or `openrouter`.
- `OLLAMA_BASE_URL`, `OLLAMA_GENERAL_MODEL`, `OLLAMA_CODING_MODEL`, `OLLAMA_FAST_MODEL`.
- `LLM_REQUIRE_FREE_MODELS=true` — with it on, a model id that is not in
  `config.ALLOWED_FREE_MODELS` is logged as a `FREE-MODEL POLICY VIOLATION` at
  startup and then **rejected per request, before any network call**. The app
  still boots: a UI that comes up and tells you which model id is wrong is more
  useful than a process that exits, and `/health` reports the same list as
  `llm.rejected_models` so the settings panel can show it.
- `BACKEND_HOST` defaults to `127.0.0.1`. Only change it to `0.0.0.0` when
  something outside the machine must reach it — see [DEPLOYMENT.md](DEPLOYMENT.md).
- `WORKSPACE_ROOT` (default `~/ZaytoonWorkspace`) and `WORKSPACE_ENFORCED=true`.
  File tools refuse paths outside the workspace; `WORKSPACE_ENFORCED=false`
  turns that off and is not a supported production setting.
- `WHISPER_MODEL` / `WHISPER_DEVICE` — STT loads `tiny` on CPU on first use.
  Zaytoon downloads nothing; a missing weight file is reported as STT
  unavailable and voice input is disabled, chat keeps working.

## 2. Frontend

```bash
cd frontend
npm install
npm run dev       # http://localhost:5173, proxies to the backend
npm run build     # dist/ — what the desktop host loads
npm test          # vitest
```

## 3. Desktop host (optional)

```bash
cd frontend && npm run build          # produce dist/ first
cd ../desktop && python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py              # or: python main.py --diagnose
.venv/bin/python scripts/smoke_desktop.py   # live proof: window + bridge + CORS
```

It is a pywebview host: the OS's own WebView2 on Windows, GTK WebKit on Linux.
It spawns the backend and loads `frontend/dist/index.html` (or the Vite dev
server with `--dev`). The React app's own `useWebSocket.js` owns the backend
connection — the host does not duplicate it.

## 4. Verify the install

```bash
cd backend && .venv/bin/python -m pytest tests/ -q          # whole suite, offline
cd frontend && npm test                                     # vitest
curl -s localhost:8000/health | python3 -m json.tool         # while running
```

The backend suite needs no provider, no network and no browser. If it needs
those things to pass, that is a bug worth reporting rather than a setup step.

## Notes on what is deliberately not here

- **No model is downloaded by any of these steps.** Model pulls are your
  `ollama pull` command, or the explicit Pull button in the models panel.
- **No microservices, no queue, no database server.** One FastAPI process, one
  SQLite file (`backend/data/zaytoon.db` via `DATABASE_URL`), one React app.
- **The skills directory is opt-in.** Shipped skills under `skills/` install
  disabled; `backend/scripts/install_shipped_skills.py --list` shows them and
  enabling is a separate flag.
