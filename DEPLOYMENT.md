# Zaytoon — Deployment

Read this before exposing a Zaytoon backend to anything other than `127.0.0.1`.
It is a **desktop assistant with a local HTTP API**, not a multi-tenant service:
the tool layer can launch processes, write inside the workspace and read files.
There is no authentication on `/chat`, `/command/run` or `/files/*` — the trust
boundary is "whoever can reach this port is the user".

## Containers

`docker-compose.yml` builds two services:

| Service | Publishes | Notes |
| --- | --- | --- |
| `backend` | `8000:8000` | `BACKEND_HOST=0.0.0.0` is set *by the compose file*, because the container's loopback is not your loopback. `env_file: ./backend/.env`, `restart: unless-stopped`, a `/health` healthcheck and a 30 s `stop_grace_period` (the shutdown sequence stops tracked processes and terminals, then flushes SQLite). |
| `frontend` | `3000:8080`, `3443:8443` | `node:20-alpine` build → `nginx:alpine` serving `dist/`, proxying `/health`, `/chat`, `/ws` and the other API prefixes to `backend:8000`. |

Volumes: `backend-data` → `/app/data` (the SQLite database and the audit log)
and `model-cache` → `/root/.cache/huggingface` (Whisper weights, if STT is used
inside the container). Both are named, so `docker compose down` keeps them and
`down -v` destroys them.

### ⚠ Known defects in the frontend image — verify before deploying

Two independent problems, found by reading the tree on 2026-10-11. Neither is
fixable honestly without someone running Docker, so they are written down rather
than papered over:

1. **`frontend/Dockerfile` cannot build as committed.** It contains
   `COPY certs/ /etc/nginx/certs/` and a comment pointing at
   `scripts/generate-certs.sh`; **neither `frontend/certs/` nor that script
   exists in the repository** (`git ls-files frontend` lists neither). The
   build fails at that `COPY`.
2. **The published ports do not match the redirect.** The embedded nginx config
   answers on `8080` with `return 301 https://$host:8443$request_uri;`, while
   compose publishes `3443:8443`. A browser opening the documented
   `http://localhost:3000` is therefore redirected to `localhost:8443` — a port
   nothing publishes. Publishing `8443:8443` (or redirecting to the published
   port) is the smaller fix.

Consequence: **the supported way to run Zaytoon today is the local app**
(`install.sh` / `start.bat`, or `python main.py` + `npm run dev`), and the
`backend` container on its own. If you need the containerised UI, serve
`frontend/dist/` behind your own TLS-terminating proxy — the React app talks to
the API on relative paths (`/chat`, `/ws`), so any proxy that forwards the API
prefixes works, and the WebSocket needs `Upgrade`/`Connection` headers passed
through.

## Process manager

One process, no daemonising of its own:

```ini
[Unit]
Description=Zaytoon backend
After=network-online.target

[Service]
WorkingDirectory=/opt/Zaytoon/backend        # must be backend/: imports depend on it
ExecStart=/opt/Zaytoon/backend/.venv/bin/python main.py
Environment=BACKEND_HOST=127.0.0.1
Restart=on-failure
RestartSec=3
TimeoutStopSec=40                            # > the 30 s stop_grace_period, so tracked processes are asked, not killed

[Install]
WantedBy=default.target
```

Run it as the user whose files the assistant should see: `WORKSPACE_ROOT`
defaults to `~/ZaytoonWorkspace`, and a service user gets *that* home, not yours.

## TLS and remote access

Terminate TLS outside the app. To reach a home install safely, use a tunnel or
a VPN (Tailscale is the pattern this project's own desktop features assume)
rather than publishing `8000`; the backend has no auth layer to put in front of.
If you do front it with nginx, keep `/ws` on the same host and set
`proxy_read_timeout` well above the idle time between tokens, or a long streamed
answer is cut mid-sentence.

## Upgrades, data, and what to back up

- The database is one file: `backend/data/zaytoon.db`. Column additions are
  applied by `MemoryAgent._migrate` on startup (an `ALTER` that fails is
  reported as `op=migrate.*`, not swallowed). Back up
  `backend/data/` and that is the whole state — preferences, memory,
  reminders, tasks, audit.
- `git pull` then `pip install -r backend/requirements-dev.txt`, `npm ci` in
  `frontend/`, `npm run build`, restart. Read `docs/PHASE0_BASELINE.md` if a
  behaviour changed: it records what each phase decided and why.
- Roll back by checking out the previous tag and restoring `backend/data/`.

## What to check after any deploy

```bash
curl -s localhost:8000/health | python3 -m json.tool | head -40
```

`status`, `status_reason`, `llm.provider`, `llm.configured`, `llm.probe.state`
(`reachable` / `unreachable` / `never_probed`) and `llm.slots` together say
whether the process is up, whether a model can answer, and whether requests are
queueing. `/tools` lists every tool with its tier; the absence of a tool there
means its module failed to import, and the startup log says which.
