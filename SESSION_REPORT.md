# ZAYTOON — Architecture Audit & Fix Report

**Session:** `arena/af56e022-zaytoon` · **Date:** 2026-10-12
**Baseline commit (verified remote = local):** `0e550892d8120bdc8923be12ab142a7f3b5e46e2`
**Working commit:** `909e8a2` on `arena/af56e022-zaytoon` (pushed)

---

## 0. Required-model workflow — honest accounting

The task required **Claude Opus** (architect/reviewer) and **Claude Haiku**
(fast bounded tasks), with no silent substitution.

**Status: NOT AVAILABLE — no model-selection mechanism was exposed to this session.**

I had no tool, parameter, or configuration that selects a model. I could not
observe which model produced which output, so I cannot claim Opus was the
architect or that Haiku did the repository sweeps. **I am reporting this as a
limitation rather than claiming compliance.** The division of labour was
therefore performed by one agent, and every finding below is backed by an
executed reproduction instead of by an assertion of authority. Points that would
have gone to Opus for sign-off (the voice-handshake fix, the Skills permission
contract) are the ones with tests proven to fail before the fix — that evidence
substitutes for the missing review, and it is weaker.

---

## 1. Latest verified GitHub commit SHA

`git fetch origin` succeeded. Full SHA: **`0e550892d8120bdc8923be12ab142a7f3b5e46e2`**.

## 2. Local-versus-remote differences

| Check | Result |
|---|---|
| Remote | `https://github.com/George-maher/Zaytoon.git` |
| Default branch | `main` (`origin/HEAD -> origin/main`) |
| `git rev-parse origin/main` | `0e55089…46e2` |
| `git rev-parse HEAD` (before work) | `0e55089…46e2` |
| `git rev-list --left-right --count HEAD...origin/main` | `0 0` — no divergence |
| Uncommitted changes at checkout | none (`git status --porcelain` empty) |
| `git diff HEAD origin/main` | empty |

**The local checkout was identical to the latest remote commit.** There were no
remote-only commits. The repository is a **shallow clone with a single commit**
(`git rev-list --count HEAD` = 1), so *no earlier history is reachable* — the
`AUDIT_REPORT.md` / `FINAL_REPORT.md` documents describe states that cannot be
diffed against this tree. That is a genuine limit on "inspect the diff introduced
by the latest commits": there is no parent to diff.

## 3. Architecture map (as found — ~102k LOC)

```
Zaytoon
├── backend/    (63k LOC, FastAPI + WebSocket)
│   ├── api/          main.py (REST + lifespan), websocket.py (the turn protocol)
│   ├── agents/       engine.py (bounded ReAct loop), router/fast/planner/code/tool/memory
│   ├── tools/        registry.py (validate → risk gate → execute, THE chokepoint),
│   │                 safety.py (RiskAssessor + ConfirmationManager), sandbox.py,
│   │                 netguard.py (SSRF: resolves the address, not the string)
│   ├── voice/        stt.py (faster-whisper), tts.py (edge-tts), routing.py (ar/en)
│   ├── core/         db, tasks, projects, skills, providers, permissions,
│   │                 workspace (files/git/terminal), api/*
│   └── tests/        2672 tests
├── frontend/   (25k LOC, React 18 + Vite + Tailwind) — App.jsx shell, HUD/,
│   │           views, hooks/useWebSocket.js (the single protocol seam)
├── desktop/    (14k LOC, pywebview host) — app/{core,host,bridge,panel,orb,audio}
└── skills/     3 shipped skills (dependency-review, file-audit, log-review)
```

Security architecture was already real and is preserved: `ToolRegistry.execute`
is the only dispatch path, skills are floored at `confirm` via a sticky
registry-side floor, the WS handler has **no** raw command channel (deliberately,
with a comment explaining the CORS-bypass it would reopen), and `netguard`
refuses loopback/link-local/reserved by *resolved address class*, re-checking
each redirect hop.

## 4. Audit findings by severity

| # | Severity | Finding |
|---|---|---|
| 1 | **Critical** | Spoken Arabic could never switch the app to Arabic — dead language handshake (§6) |
| 2 | High | Streaming TTS could be handed an empty prosody value → no audio |
| 3 | High | Skill permissions accepted from the client and silently discarded |
| 4 | Medium | Per-frame WS tracing at WARNING; audio base64 logged |
| 5 | Medium | Desktop suite: PortAudio probed only when Tk present → collection error |
| 6 | Low | Three undefined names (`Optional`, `List`, `iskAssessor`) |
| 7 | Low | Dead expression `slept.append(...)` in a test (would `NameError`) |

No leftover `TODO`/`FIXME` markers exist anywhere (`grep` count: 0), which is
consistent with the codebase's unusually heavy explanatory-comment style.

## 5. Root causes of the major defects

**#1 — two correct components, one dead wire.** `transcribe_detailed` was
correct. The client's `stt_result` case was correct. The gate between them read
`detected` as *"is this language trustworthy?"* when it means *"did the caller
send a hint?"*. The browser always sends a hint, so the branch was unreachable in
production. The bug was invisible to unit tests because both sides passed.

**#3 — a route that validated a field it never forwarded.** `create()` had no
`permissions` parameter, so the route's `data.get(...)` read returned nothing.
The UI rendered `skill.permissions` faithfully — from a field that was always
empty. Everything looked wired.

**#2 — two sibling functions that drifted.** `synthesize` had three-step
fallback; `stream_synthesize` had two. Only one of them ran by default.

## 6. Root cause detail: the voice defect

`backend/voice/stt.py` computes:

```python
no_hint_supplied = not language      # was named `auto_detected`
...
detected=no_hint_supplied
```

`backend/api/websocket.py` (before):

```python
if transcription.detected and transcription.language:   # never true in production
    await connection_manager.send(websocket, {"type": "stt_result", ...})
```

`frontend/src/hooks/useWebSocket.js`:

```javascript
case "stt_result":
  if (data.language) setLanguage(data.language);   // the ONLY caller
```

`setLanguage` drives RTL (`root.setAttribute("dir", …)`) and the agent/TTS
language. Result: Egyptian Arabic speech → Arabic answer **read aloud in a
British English voice**, LTR layout, no error logged. Reproduced with
`backend/scripts/probe_voice_language.py`, which drives the real handler with a
real `audio` frame:

```
client hint='en'   stt_result: []   DEFECT
client hint='ar'   stt_result: []   DEFECT
client hint=None   stt_result: [{language: 'ar', ...}]   (the only path that worked)
```

## 7. Implemented changes and affected files

| File | Change |
|---|---|
| `backend/api/websocket.py` | Report `stt_result` whenever a language is known; per-frame tracing → DEBUG; stop logging audio payload prefixes |
| `backend/voice/stt.py` | `auto_detected` → `no_hint_supplied`; docstring states `detected` is not a confidence score |
| `backend/voice/tts.py` | Streaming prosody gains the profile fallback step |
| `backend/core/skills/registry.py` | `create(permissions=, tags=)`; validates against the vocabulary |
| `backend/core/api/registry_api.py` | Forward `permissions`/`tags`; add `GET /skills/permissions` |
| `backend/core/system_resources.py` | Add `List` to typing import |
| `backend/tools/system.py` | Add `Optional` to typing import |
| `backend/tools/executor.py` | `iskAssessor` → `RiskAssessor` |
| `frontend/src/lib/api.js` | `getSkillPermissions()`; `createSkill` sends the selection |
| `frontend/src/components/SkillsView.jsx` | Permission checkbox list in the create form |
| `desktop/tests/conftest.py` | Independent probes; policy extracted to a pure function |
| `backend/tests/test_reminder_lifecycle.py` | Dead `slept` expression replaced with a real assertion |
| **new** `backend/tests/test_voice_language_handshake.py` | 8 tests |
| **new** `backend/tests/test_voice_output_contract.py` | 5 tests |
| **new** `backend/tests/test_skill_permissions_contract.py` | 9 tests |
| **new** `desktop/tests/test_native_skip_policy.py` | 6 tests |
| **new** `backend/scripts/probe_voice_language.py` | Reproduction script |

Every new test was run against the reverted fix and observed to fail (5, 4 and
6 failures respectively) before being accepted.

## 8. Requirement status

| Area | Status | Evidence |
|---|---|---|
| Remote commit inspection | **PASS** | SHA verified, `0 0` divergence |
| Backend tests | **PASS** | 2672 passed / 5 skipped (baseline 2650/5) |
| Frontend lint/test/build | **PASS** | eslint clean; 613 passed; build 476 kB |
| Desktop tests (Linux) | **PASS** | 366 passed / 2 skipped (was a collection error) |
| Voice — language handshake | **PASS** | reproduced, fixed, 8 tests, guard verified |
| Voice — TTS prosody | **PASS** | reproduced, fixed, 5 tests |
| Voice — real microphone/audio | **NOT TESTED** | no audio device; no interactive desktop |
| Voice — Arabic audio playback | **NOT TESTED** | needs a sound device + network to edge-tts |
| Web search | **BLOCKED** | no httpx client is constructed with `trust_env`, so `HTTPS_PROXY` is ignored and `ddgs` cannot reach any provider (`ConnectError … Network is unreachable`). The tool degrades honestly (`Search failed: …`) instead of fabricating results. **Fix identified, not applied.** |
| Computer control | **PARTIAL** | 37 tools over `ToolRegistry`; app launching/interception is Windows-first and unverifiable here |
| Skills management | **PARTIAL** | create/validate/enable/disable/delete + permission declaration now work end-to-end (verified against a live server); natural-language creation, drafts, version history and per-skill execution logs are **not implemented** |
| Security / permissions | **PASS** | tool gate unchanged; new test pins that declaring permissions does not grant them |
| Security workspace UI | **FAIL (missing)** | no dedicated page; the three shipped skills cover passive review only |
| JARVIS UI/UX, orb, RTL | **PARTIAL** | pre-existing HUD/orb/theme and i18n exist and are untouched; I did **not** redesign the UI, so I make no claim about it |
| Windows / macOS | **NOT TESTED** | Linux only; CI has all three legs but was not run |
| Performance | **NOT TESTED** | no measurements taken |

## 9. Remaining work, in priority order

1. **Web search is unusable behind a proxy** — make the search client inherit
   `HTTPS_PROXY` (a dedicated `httpx` client with `trust_env=True` passed to
   `DDGS`, or `ddgs`'s own proxy option). This is the single largest gap between
   Zaytoon and the stated mission, and it is a small change.
2. **A `ZAYTOON_ALLOWED_HOSTS`-style egress allowlist** before search is enabled
   in any packaged build.
3. Skills: natural-language creation, draft saving, version history, per-skill
   execution logs (the UI currently has create/edit/enable/disable/delete only).
4. A dedicated Security Workspace page.
5. Real-device voice validation on Windows and macOS.

## 10. Exact commands to reproduce

```bash
# Backend (2672 passed, 5 skipped)
cd backend && PYTHONPATH=../.github python -m pytest tests/ -q

# The voice defect, before/after
cd backend && python scripts/probe_voice_language.py

# The two regression guards
cd backend && PYTHONPATH=../.github python -m pytest \
  tests/test_voice_language_handshake.py tests/test_voice_output_contract.py -q

# Skills permission contract
cd backend && PYTHONPATH=../.github python -m pytest \
  tests/test_skill_permissions_contract.py -q

# Frontend
cd frontend && npm run lint && npm run test && npm run build

# Desktop
cd desktop && PYTHONPATH=../.github:../backend python -m pytest tests/ -q

# Live end-to-end (Skills permissions, verified during this session)
cd backend && python -m uvicorn api.main:app --port 8000 &
curl -s localhost:8000/skills/permissions
curl -s -X POST localhost:8000/skills -H 'Content-Type: application/json' \
  -d '{"name":"demo","permissions":["filesystem.read"]}'
```

## 11. Change-management compliance

No reset, force-push, clean, or history rewrite. No user changes existed to
preserve (working tree was clean). No test was deleted or weakened; the one test
edited gained a real assertion where it previously had a dead name. No
dependency was added to the product — `pytest-timeout` was absent so the
unsupported `--timeout` flag was dropped rather than installed. No credential was
requested or recorded. The one new script is a probe, not shipped code.
