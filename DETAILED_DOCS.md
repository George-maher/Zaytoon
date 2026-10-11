# Zaytoon — Detailed Documentation Index

There is no second copy of the design in this repository, on purpose: a second
copy is a second thing to keep true. Every document below describes the code
that exists, and each names the files it was derived from.

| Document | What it answers | Authoritative for |
| --- | --- | --- |
| [README.md](../README.md) | What the product is, the layout, the 30-second setup | the front door |
| [QUICK_START.md](QUICK_START.md) | "Get it running on this machine, right now" | — (points at the files) |
| [SETUP_INSTRUCTIONS.md](SETUP_INSTRUCTIONS.md) | Which knobs exist, what each one does, how to verify an install | `backend/config.py`, `backend/.env.example` |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Containers, process managers, TLS, backups — and the two known defects in the frontend image | `docker-compose.yml`, `backend/Dockerfile`, `frontend/Dockerfile` |
| [AGENTS.md](../AGENTS.md) | The agent/contributor guide: how to run things, the WebSocket protocol table, the security boundaries, the conventions a change has to keep | the wire protocol and the rules |
| [docs/ARCHITECTURE.md](ARCHITECTURE.md) | The shape of the system: agents, orchestrator, tool layer, providers, transports | — (narrative; the code decides) |
| [docs/IMPLEMENTATION_MAP.md](IMPLEMENTATION_MAP.md) | Which capability lives in which module — the map from "reminders" to `scheduler/`, from "confirm" to `tools/confirmation.py` | file-level orientation |
| [docs/PHASE0_BASELINE.md](PHASE0_BASELINE.md) | **The audit trail.** Baseline measurements, then one section per phase: what was found, what was decided and why, what was fixed, what was measured, what is still open | the *history* of every decision in this list |
| [docs/DESIGN_SYSTEM.md](DESIGN_SYSTEM.md) | The HUD visual language: tokens, density, panel geometry | `frontend/src/styles`, `frontend/src/hud*` |

## Reading order for a new contributor

1. `README.md` — what the thing is.
2. `docs/IMPLEMENTATION_MAP.md` — where things are.
3. `AGENTS.md` — the rules, including the ones that look like over-caution and
   are not (workspace containment, tool tiers, confirmation floors, the free-model
   allow-list).
4. `docs/PHASE0_BASELINE.md` §1–§4 — the baseline and the first fixes, which is
   where the vocabulary of this codebase ("reported, not swallowed",
   "positive evidence", "refuse rather than approximate") gets defined.

## How claims are documented here

Three habits, because they are what made the audit useful:

- **A number is a measurement with a date and a command.** "2,639 backend tests
  pass" is worth nothing without `cd backend && .venv/bin/python -m pytest tests/`
  and the day it was run.
- **A limitation is written in the same paragraph as the feature.** Example: the
  security skills are read-only *and* the docs say the pattern list is not a
  vulnerability scanner; `/health` reports the provider probe *and* that
  `network` counters are not collected.
- **"Not verified" is a sentence, not a shrug.** The macOS CI leg has never run
  in the sandbox where it was written; the frontend container has never been
  built here. Both are stated where the code is described, so nobody has to
  rediscover them at deploy time.

If a document here contradicts the code, the code is right and the document is a
bug — say so, or fix it.
