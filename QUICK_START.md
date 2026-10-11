# Zaytoon — Quick Start

Three ways to run it. Pick one; each section is complete on its own.

## 1. Windows / Linux / macOS — the local app

```bash
./install.sh          # Linux/macOS: venvs, deps, .env, database — no model downloads
start.bat             # Windows: same steps
```

Then, from a second terminal:

```bash
cd backend && .venv/bin/activate    # Windows: .venv\Scripts\activate
python main.py                      # http://127.0.0.1:8000
```

and in a third:

```bash
cd frontend && npm install && npm run dev    # http://localhost:5173
```

Open **http://localhost:5173**. If the installer reported a missing dependency,
it named it — it does not guess at a package manager and does not pull an AI
model.

## 2. Ollama first (this is the default provider)

Zaytoon never downloads a model on its own, so pull the ones it expects:

```bash
ollama serve
ollama pull qwen3.5:9b
ollama pull qwen2.5-coder:7b
ollama pull qwen2.5-coder:3b
```

A model that is not installed is reported as `MODEL NOT INSTALLED` with the
exact `pull` command — in `/health`, in the settings panel, and in the reply —
never as a silent failure, and never as a downloaded model.

## 3. Docker

```bash
docker compose up --build     # backend :8000, frontend :3000
curl localhost:8000/health
```

`backend/.env` is read through `env_file`, so the provider settings above apply
inside the container too. See [DEPLOYMENT.md](DEPLOYMENT.md) for volumes, the
health checks, and what is *not* containerised (the desktop host, and the
Whisper weights STT loads on first use).

## The one check that answers most questions

```bash
curl -s localhost:8000/health | python3 -m json.tool | head -30
```

`status`, `status_reason`, `llm.provider`, `llm.configured` and `llm.probe`
together answer "is it running, and can it think". `llm.probe.state` is the
*measured* answer (`reachable` / `unreachable` / `never_probed`); `configured`
only means an endpoint and a key exist, which stays true while the daemon is
stopped.

---

*Desktop host (pywebview, wraps the built React UI):* see
[SETUP_INSTRUCTIONS.md](SETUP_INSTRUCTIONS.md). Everything above was read from
`install.sh`, `start.sh`/`start.bat`, `docker-compose.yml`,
`backend/Dockerfile`, `backend/config.py` and `AGENTS.md` on 2026-10-11; those
files are the source of truth, and this document is the short route through
them.
