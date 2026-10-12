# ZAYTOON — Architecture Audit & Fix Report

**Session:** `arena/af56e022-zaytoon` · **Date:** 2026-10-12
**Baseline commit (verified remote = local):** `0e550892d8120bdc8923be12ab142a7f3b5e46e2`
**Working commits on `arena/af56e022-zaytoon`:**

| Commit | Contents | Verified |
|---|---|---|
| `909e8a2` | Five defects: voice language handshake, TTS prosody, skill permissions, WS log level, desktop conftest | 2672 backend / 613 frontend / 366 desktop |
| `edf4815` | This report, first revision | — |
| `5510497` | Confirmation-gate fix, scope-enforced security layer, Security Workspace, three test-only defects | 2792 backend / 627 frontend / 366 desktop |

`main` has not been touched; nothing was merged. Two items from the first
revision are corrected here (§8 web search, §11 test changes) and the voice
diagnosis in §6 stands.

---

## 0. Required-model workflow — honest accounting

The task required **Claude Opus** (architect/reviewer) and **Claude Haiku**
(fast bounded tasks), with no silent substitution.

**Status: NOT AVAILABLE — no model-selection mechanism was exposed to this session.**

I had no tool, parameter, or configuration that selects a model. I could not
observe which model produced which output, so I cannot claim Opus was the
architect or that Haiku did the repository sweeps. **I am reporting this as a
limitation rather than claiming compliance**, and it applies to both batches of
work below. The division of labour was therefore performed by one agent, and
every finding is backed by an executed reproduction instead of by an assertion
of authority. Points that would have gone to Opus for sign-off (the
voice-handshake fix, the Skills permission contract, the confirmation-gate fix,
the scope layer) are the ones with tests proven to fail before the fix — that
evidence substitutes for the missing review, and it is weaker.

A concrete example of why the missing review matters, from this batch: the
first diagnosis of the confirmation-gate defect was **wrong**, and a plausible
"fix" for it (adding tool names to the assessor's tables) would have closed the
visible symptom while leaving every skill-registered tool ungated. The wrong
fix was avoided by reproducing the defect before changing anything, not by
having a second reviewer.

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
│   │                 security.py (SecurityProbe, tier `confirm`)          [new]
│   ├── voice/        stt.py (faster-whisper), tts.py (edge-tts), routing.py (ar/en)
│   ├── core/         db, tasks, projects, skills, providers, permissions,
│   │                 workspace (files/git/terminal), api/*,
│   │                 security/scope.py (target authorisation)             [new]
│   └── tests/        2792 tests
├── frontend/   (25k LOC, React 18 + Vite + Tailwind) — App.jsx shell, HUD/,
│   │           views, hooks/useWebSocket.js (the single protocol seam),
│   │           components/SecurityView.jsx (the Security Workspace)       [new]
├── desktop/    (14k LOC, pywebview host) — app/{core,host,bridge,panel,orb,audio}
└── skills/     3 shipped skills (dependency-review, file-audit, log-review)
```

Security architecture was already real and is preserved: `ToolRegistry.execute`
is the only dispatch path, skills are floored at `confirm` via a sticky
registry-side floor, the WS handler has **no** raw command channel (deliberately,
with a comment explaining the CORS-bypass it would reopen), and `netguard`
refuses loopback/link-local/reserved by *resolved address class*, re-checking
each redirect hop.

One exception was found in that path and is fixed — the tier gate was bypassed
for every tool that declares its own tier rather than appearing in a name table
(§5a). Note that `scope.py` deliberately inverts `netguard`'s policy: it
*permits* loopback and private targets, because an authorised assessment of your
own machine is the normal case, and the authorisation record — not the address
class — is what makes it permitted.

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
| 8 | **Critical** | The declared permission tier never reached the confirmation prompt: every tool whose tier comes from its own declaration — `play_media`, `security_probe`, and **every tool a skill registers** — ran with the user never asked (§5a) |
| 9 | High | The probe confirmation token's TTL was not enforced at use: a token stayed valid until someone happened to request another one (§5c) |
| 10 | Medium | A test that could not fail: it asserted the confirmation prompt was shown but never that the approval took effect, so it passed while its own notifier raised (§5d) |
| 11 | Medium | `ZAYTOON_DB_PATH` is read by nothing in the product; three security test fixtures set it instead of `CORE_DB_PATH`, so they isolated nothing while reading as if they did (§5e) |

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

## 5a. Root cause detail: the confirmation gate never saw the tier

This is the most serious defect found, and the first diagnosis of it was wrong
in a way worth recording.

**The wrong diagnosis.** `ToolRegistry.assess` appeared to ignore a tool's
declared `permission`. The suggested "fix" — adding `play_media` and
`security_probe` to `RiskAssessor`'s name tables — would have worked for those
two tools and left the real case broken.

**What was actually true.** `ToolRegistry.assess` was already correct: it
reconciles the tool's own `permission` against the assessor's name-keyed tables
and takes the stricter. Verified directly against
`build_default_registry()`:

| tool | declared | assessed | level | requires_confirmation |
|---|---|---|---|---|
| `security_probe` | confirm | confirm | HIGH | **True** |
| `play_media` | confirm | confirm | HIGH | **True** |
| `file_write` | confirm | confirm | HIGH | True |
| `open_app`, `calculator` | safe | safe | SAFE | False |

**The real defect sat across the seam.** The registry then handed the decision
to `ConfirmationManager.request()`, which *re-derived* the tier with a bare
`RiskAssessor` — the name-keyed tables only. Any tool named in no table was
scored `safe`, so `requires_confirmation` was `False`, so `request()` returned
`True` **without ever calling the notifier**. `play_media`, `security_probe`
and every skill-registered tool were in that set.

Proven empirically: with a manager whose `notify` records the prompts it was
shown, `security_probe` ran and returned six findings while `notify` was never
called once. The gate existed, the registry's decision was correct, and the
prompt never reached the user — a bypass by construction, not by cleverness.

**The fix does not widen the existing method.** `ConfirmationManager` gains
`request_with_assessment(assessment, action, params, session_id)`; `request()`
keeps its exact signature and behaviour, and both delegate to a shared
`_request(assessment, …)`. `ToolRegistry._request_approval` prefers the new
method via `getattr` and falls back to `request`. Widening `request`'s
signature was tried first and broke **nineteen** duck-typed doubles in
`tests/test_tool_registry.py` (and would break embedders), so the
assessment-aware path is deliberately a separate method. Passing the
reconciled assessment also fixes the dialog text: the prompt now shows the real
reason (`"'play_media' is declared confirm-level"`) instead of nothing.

**Verified live, not just in tests.** `POST /security/probe` returned
`approved_request_id: null` before the fix and a real request id after it
(`cfm_06b772ef35e9`), which is the receipt proving a prompt was issued and
answered.

## 5b. The security layer: scope enforced in the execution layer

The requirement was explicit: **scope limits must be enforced in the execution
layer, not in prompts.** What was built:

* `backend/core/security/scope.py` — the authorisation store (table
  `security_authorizations`). Exact host, or `*.` subdomain; a wildcard does
  **not** cover its apex, and `evil-example.com` never matches `example.com`.
  CIDR via `ipaddress`. Case-folded, trailing dots stripped. `*`, a bare `*.`
  and single-label hosts are refused; **wildcards on public suffixes are
  refused** (`_GUARDED_PUBLIC_SUFFIXES` covers `co.uk`, `com.au`, `github.io`,
  `vercel.app`, `s3.amazonaws.com`, …). Expiry is evaluated **at use**.
  Revocation re-reads the store every call — no cache. `require_authorized()`
  raises `OutOfScope` naming the target, the operation and the exact fixing
  call. Private and loopback targets are permitted here **by design**, the
  opposite of `tools/netguard.py`.
* `backend/tools/security.py` — `SecurityProbe`, registered at tier `confirm`.
  Order: validate → `require_authorized` (before any socket exists) → tier gate.
  GET only, one request per URL, 10 s timeout. Every finding is
  `confidence: "confirmed"` with evidence and remediation, or it is absent.
  `risk_params` returns only `{"url": …}`, so no response content can become a
  gate input.
* **No tool can reach `scope.create`.** A test asserts that no registered
  tool's source references it, so a model cannot widen its own scope by
  asserting something. Authorisation is therefore a user action, and the
  Security Workspace is where the user performs it.
* Routes: `GET/POST/DELETE /security/authorizations` and a two-step
  `POST /security/probe`. Step 1 checks scope and opens **no socket**, returning
  a single-use token (TTL 120 s, cap 256, oldest evicted first); step 2 carries
  the user's answer and runs the tool through the process's real registry with a
  route-local `ConfirmationManager` whose notifier calls `resolve()` — the same
  call the WebSocket handler makes when a user clicks Approve. Validation, the
  reconciled tier gate and the audit record all still run.
* Scenario F, all five legs PASS (in-process and over live HTTP): out-of-scope
  refused before any socket; authorised probe of the lab returning six
  evidenced findings; revoke → refused; token reuse → 409; authorising
  `lab.internal.test` does not authorise `127.0.0.1`.
* The vulnerable lab fixture (`backend/scripts/vulnerable_lab.py`) is
  loopback-only and stdlib-only.

## 5c. Root cause detail: the confirmation token did not expire

`_PROBE_TOKENS` was pruned inside the **preview** branch — that is, only when a
*new* preview was requested. Step 2 popped the token without ever comparing its
age to the TTL. So a token survived for as long as nobody asked for another
one: take a token, go to lunch, come back, and it still probed. The documented
120-second TTL was aspirational.

Found by the route test written for it (`test_an_expired_token_is_refused`
failed with `200 == 409` on first run), then fixed by evaluating age **at use** —
the same rule `scope.py` already applies to authorisation expiry. The guard was
validated by reverting the check and watching that one test fail.

## 5d. A test that could not fail

`test_the_prompt_carries_the_reconciled_reason` called an undefined name,
`loop`, inside its notifier:

```python
async def notify(pending):
    seen.append(pending.assessment.reasons)
    loop.call_soon(manager.resolve, pending.request_id, True)   # NameError
```

`ConfirmationManager` **catches a notifier that raises**, logs it through the
redacting logger, and denies the request. So the notifier appended its reason
and *then* raised: `seen` was populated, the approval never happened, the tool
was refused — and the test passed, because it only asserted on `seen`.

`pyflakes` found it (`undefined name 'loop'`). The fix uses
`asyncio.get_running_loop()` **and** asserts `result["success"] is True`, so the
prompt assertion now means something. Re-inserting the original bug makes the
test fail, which it did not before.

## 5e. Root cause detail: a test-isolation variable that did nothing

`ZAYTOON_DB_PATH` is set by three security test fixtures and **read by nothing
in the product**; the real override is `CORE_DB_PATH` (`core/db.py::db_path`).
The tests were still isolated, because `tests/conftest.py` sets `CORE_DB_PATH`
for everything — but the fixtures *read* as if they did the work themselves.

This mattered beyond cosmetics: those fixtures call `db.reset_for_tests()`,
which **drops every table** in whatever `db_path()` resolves to. Anywhere the
conftest fixture is not in scope, the no-op spelling would have pointed a
table-dropping call at the user's real database. All three now set
`CORE_DB_PATH`, and the fix is verified by asserting that
`backend/data/core.db`'s mtime is unchanged across a full suite run.

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

Second batch — the confirmation gate, the security layer and the observations
that came out of testing them:

| File | Change |
|---|---|
| `backend/tools/safety.py` | `request_with_assessment(assessment, …)` added; `request()` unchanged; both delegate to `_request(assessment, …)` |
| `backend/tools/registry.py` | `_request_approval` prefers the assessment-aware method via `getattr`, falls back to `request` |
| `backend/core/security/scope.py`, `__init__.py` | **new** — the authorisation store and `require_authorized` |
| `backend/core/db.py` | `security_authorizations` migration |
| `backend/tools/security.py` | **new** — `SecurityProbe` (tier `confirm`) |
| `backend/core/api/registry_api.py` | `security_router`: list/create/revoke, plus two-step `POST /security/probe`; token TTL now checked at use |
| `backend/core/api/routers.py`, `backend/api/main.py` | Export and mount `security_router` |
| `backend/scripts/vulnerable_lab.py` | **new** — loopback-only lab fixture |
| `frontend/src/lib/viewState.js` | `VIEWS.SECURITY` destination (`ready: true`) |
| `frontend/src/components/SecurityView.jsx` | **new** — authorise / probe / revoke |
| `frontend/src/lib/api.js` | four security helpers; the probe is two calls |
| `frontend/src/App.jsx` | Mounts `SecurityView` on `VIEWS.SECURITY` |
| `frontend/src/lib/viewState.test.js` | Vocabulary pin updated for the new destination |
| `frontend/src/App.runtime.test.jsx` | Security nav entry now proves its screen mounts |
| `frontend/src/components/SecurityView.test.jsx` | **new** — 12 tests |
| **new** `backend/tests/test_declared_tier_is_enforced.py` | 8 tests; the reason test strengthened (see §5d) |
| **new** `backend/tests/core/test_security_scope.py` | 65 tests |
| **new** `backend/tests/test_security_probe.py` | 19 tests |
| **new** `backend/tests/test_security_probe_route.py` | 28 tests |
| `backend/tests/test_security_probe*.py`, `tests/core/test_security_scope.py` | `ZAYTOON_DB_PATH` → `CORE_DB_PATH` (§5e) |

Every new test was run against the reverted fix and observed to fail before
being accepted — 5, 4 and 6 failures respectively for the voice, skill and
desktop guards; 1 for the token TTL (§5c); 1 for the App wiring of the Security
screen; 1 for the strengthened tier test (§5d); and the "never contacted" guard
was validated by injecting the fault *before* the check, since an injection
placed after it proved nothing.

## 8. Requirement status

| Area | Status | Evidence |
|---|---|---|
| Remote commit inspection | **PASS** | SHA verified, `0 0` divergence |
| Backend tests | **PASS** | 2792 passed / 5 skipped (baseline 2650/5) |
| Frontend lint/test/build | **PASS** | eslint clean; 627 passed; build 489 kB / 141 kB gzip |
| Desktop tests (Linux) | **PASS** | 366 passed / 2 skipped (was a collection error) |
| Voice — language handshake | **PASS** | reproduced, fixed, 8 tests, guard verified |
| Voice — TTS prosody | **PASS** | reproduced, fixed, 5 tests |
| Voice — real microphone/audio | **NOT TESTED** | no audio device; no interactive desktop |
| Voice — Arabic audio playback | **NOT TESTED** | needs a sound device + network to edge-tts |
| Web search | **BLOCKED — infrastructure, not code** | **Corrected.** The earlier explanation (a missing `trust_env`, so `HTTPS_PROXY` was ignored) was wrong: there are **no proxy variables set at all** in this environment, so no proxy was being bypassed. Egress here is an **allow-list**: `pypi.org` and `registry.npmjs.org` answer 200 while `api.github.com`, the DuckDuckGo hosts and `huggingface.co` fail with `ConnectError`. Search therefore cannot reach any provider from this sandbox, and no code change can alter that. The tool still degrades honestly (`Search failed: …`) rather than inventing results. **Unverified on a machine with real egress.** |
| Computer control | **PARTIAL** | 38 tools over `ToolRegistry`; app launching/interception is Windows-first and unverifiable here |
| Skills management | **PARTIAL** | create/validate/enable/disable/delete + permission declaration work end-to-end (verified against a live server); natural-language creation, drafts, version history and per-skill execution logs are **not implemented** |
| Security / permissions (the gate) | **PASS** | the declared tier now reaches the prompt for every tool, including skill-registered ones; 8 tests, and 19 pre-existing duck-typed doubles still pass |
| Security — target scope enforcement | **PASS** | enforced in `tools/security.py` before any socket, not in prompts; 65 scope tests + 19 probe tests + 28 route tests; **no registered tool can reach `scope.create`** |
| Security — non-destructive assessment + evidence | **PASS** | confirmed-only findings with evidence and remediation; Scenario F all five legs PASS live |
| Security workspace UI | **PARTIAL — running, not yet human-verified** | the page exists, is wired into `App.jsx`, and its nav entry proves its screen mounts (12 tests). A dev server is running in this session on port 3000 with the backend on 8000, so the page can be opened and driven now (see below). I have confirmed the served app and its `/security` data path over HTTP, but **I have no browser here**, so nobody has clicked it yet — the last mile is yours to confirm. |
| JARVIS UI/UX, orb, RTL | **PARTIAL** | pre-existing HUD/orb/theme and i18n exist and are untouched; I did **not** redesign the UI, so I make no claim about it. The new page follows the existing token/glyph system and switches language without reload (tested). |
| Windows / macOS | **NOT TESTED** | Linux only; CI has all three legs but was not run |
| Performance | **NOT TESTED** | no measurements taken |

### Honest note on the two defect batches

The confirmation-gate defect (#8) was found by *disbelieving* a manual
observation: the live endpoint returned a successful probe with
`approved_request_id: null`, and the successful result is what a working system
looks like. Reporting it as fine would have been wrong. The first diagnosis was
then wrong too, and the wrong fix would have left skill-registered tools
ungated — which is exactly the security-relevant case. Both are recorded above
rather than smoothed over.

## 9. Remaining work, in priority order

1. **Verify web search on a machine with real egress.** This is the single
   largest unverified gap between Zaytoon and the stated mission, and it cannot
   be closed here: every search provider is unreachable through this sandbox's
   allow-list (§8). Nothing in the search code has been proven wrong — it has
   simply never been able to run. Do **not** re-diagnose this as a proxy issue;
   there are no proxy variables, and the earlier note saying otherwise is
   withdrawn.
2. **A `ZAYTOON_ALLOWED_HOSTS`-style egress allowlist** before search is enabled
   in any packaged build. The confirmed infrastructure finding above makes the
   case for it: search is the one subsystem that talks to arbitrary hosts by
   design.
3. **Run the Security Workspace in a real browser** against a live backend.
   The page is wired and unit-tested, but it has not been driven by a human —
   no display here, and no LLM turn is possible without Ollama.
4. Skills: natural-language creation, draft saving, version history, per-skill
   execution logs (the UI currently has create/edit/enable/disable/delete only).
5. Real-device voice validation on Windows and macOS.
6. **`security_probe` covers configuration only.** It checks response headers
   from one non-destructive request. It does not test authentication,
   authorisation logic, input handling or any payload-driven class of bug, and
   the report it prints says so. Widening it is a separate piece of work with
   its own scope rules.
7. Cosmetic: `tests/test_process_manager.py` intermittently emits
   `PytestUnraisableExceptionWarning` (`BaseSubprocessTransport.__del__` after
   the loop closed). Confirmed pre-existing and flaky — it appears in roughly
   one run in three *in isolation*, with no change from this work, and no test
   fails. It makes suite output non-deterministic, nothing more.

## 10. Exact commands to reproduce

```bash
# Backend (2792 passed, 5 skipped)
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

### Scenario F — the authorised assessment, end to end

```bash
# CORE_DB_PATH, not ZAYTOON_DB_PATH: the latter is read by nothing (§5e).
cd backend && CORE_DB_PATH=/tmp/zaytoon.sqlite \
  python -m uvicorn api.main:app --host 0.0.0.0 --port 8000 &

# The lab target. Loopback only, stdlib only.
cd backend && python scripts/vulnerable_lab.py 8099 &

B=http://127.0.0.1:8000

# 1. Nothing authorised yet: refused, and no socket is opened.
curl -s -X POST $B/security/probe -H 'Content-Type: application/json' \
  -d '{"url":"http://127.0.0.1:8099/admin"}'
# -> {"refused":true,"reason":"out_of_scope","message":"Refused: '127.0.0.1' is
#     not authorised for 'http.probe'. Authorise it first ..."}

# 2. Authorise the target as the user.
curl -s -X POST $B/security/authorizations -H 'Content-Type: application/json' \
  -d '{"targets":["127.0.0.1"],"operations":["http.probe"],"note":"final"}'

# 3. Step 1 — the ask. Opens no socket; returns what it would do, plus a token.
T=$(curl -s -X POST $B/security/probe -H 'Content-Type: application/json' \
  -d '{"url":"http://127.0.0.1:8099/admin"}' | python -c 'import sys,json;print(json.load(sys.stdin)["confirm"])')

# 4. Step 2 — the answer. Still gated: the token proves a prompt was answered.
curl -s -X POST $B/security/probe -H 'Content-Type: application/json' \
  -d "{\"url\":\"http://127.0.0.1:8099/admin\",\"confirm\":\"$T\"}"
# -> success true, HTTP 200, 6 findings, approved_request_id cfm_…

# 5. The token is single use.
curl -s -o /dev/null -w '%{http_code}\n' -X POST $B/security/probe \
  -H 'Content-Type: application/json' \
  -d "{\"url\":\"http://127.0.0.1:8099/admin\",\"confirm\":\"$T\"}"   # -> 409

# 6. Revoke, and the next probe is refused again.
ID=$(curl -s $B/security/authorizations | python -c 'import sys,json;print(json.load(sys.stdin)["authorizations"][0]["id"])')
curl -s -X DELETE $B/security/authorizations/$ID
curl -s -X POST $B/security/probe -H 'Content-Type: application/json' \
  -d '{"url":"http://127.0.0.1:8099/admin"}'   # -> {"refused":true,...}

# 7. A wildcard for everything is refused, and so is a public suffix.
curl -s -X POST $B/security/authorizations -H 'Content-Type: application/json' \
  -d '{"targets":["*"],"operations":["http.probe"]}'          # -> 422
curl -s -X POST $B/security/authorizations -H 'Content-Type: application/json' \
  -d '{"targets":["*.co.uk"],"operations":["http.probe"]}'    # -> 422
```

### Seeing the Security Workspace

The dev server is running on port 3000 against the backend on 8000, so this is
live in the session's preview pane. Nothing else needs starting.

```bash
# What is running, and what the page's data path returns
curl -s localhost:8000/security/authorizations
curl -s localhost:3000/security/authorizations          # same data, via the dev proxy

# The proxy entry is new; without it the page only works when the browser can
# reach :8000 directly, which a remote preview cannot do.
# `VITE_ALLOWED_HOSTS` is opt-in so Vite's strict Host check stays the default:
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: evil.example.com' localhost:3000   # 403
```

To try Scenario F by hand: authorise `127.0.0.1`, run a probe against
`http://127.0.0.1:8099/admin`, then revoke it and run it again.

### The new guards

```bash
cd backend && PYTHONPATH=../.github python -m pytest \
  tests/test_declared_tier_is_enforced.py \
  tests/core/test_security_scope.py \
  tests/test_security_probe.py \
  tests/test_security_probe_route.py -q      # 120 passed
```

## 11. Change-management compliance

No reset, force-push, clean, or history rewrite. No user changes existed to
preserve (the working tree was clean at the start). No credential was requested
or recorded. No dependency was added to the product — `pytest-timeout` was
absent, so the unsupported `--timeout` flag was dropped rather than installed.
Nothing was merged to `main`; work stayed on `arena/af56e022-zaytoon`.

**No test was deleted or weakened.** Every test file that changed, and why:

| Test file | Change | Direction |
|---|---|---|
| `backend/tests/test_reminder_lifecycle.py` | Dead `slept.append(...)` replaced with a real assertion | strengthened |
| `desktop/tests/conftest.py` | Tk and PortAudio probed independently | strengthened |
| `backend/tests/test_declared_tier_is_enforced.py` | `asyncio.get_running_loop()`; result assertion added (§5d) | strengthened |
| `backend/tests/test_security_probe*.py`, `tests/core/test_security_scope.py` | `CORE_DB_PATH` (§5e) | corrected |
| `frontend/src/lib/viewState.test.js` | Vocabulary pin extended for the new destination | extended, not relaxed |
| `frontend/src/App.runtime.test.jsx` | Security destination added to the nav walk | extended |

The one apparent relaxation — `viewState.test.js` — is an extension: a real
destination with a mounted screen was added, and the test pins the vocabulary
exactly, so both lists grew. It is not a loosened assertion; it is the same
strict `toEqual` over one more entry.

**On `security_probe`'s tier.** The tool is registered at `confirm` and the
tier gate runs *before* it is allowed to build a client. The endpoint that runs
it does not skip the gate: it answers the manager's prompt. If that ever stops
being true, `test_the_approval_is_real_and_recorded` fails.

**On scope and self-widening.** A model cannot grant itself scope: there is no
tool that reaches `scope.create`, and a test asserts that no registered tool's
source references it. Authorisation is a user action performed in the Security
Workspace. Sensitive and disruptive operations still require confirmation, and
no destructive testing was performed — the lab fixture is loopback-only, GET
only, and its defects are configuration defects planted for the purpose.

**What is *not* claimed.** No macOS or Windows verification was performed or is
implied by any test here. No live microphone, speaker, browser or LLM turn was
exercised. The desktop shell, Spotify, and the JARVIS visual redesign were left
untouched, and §8 says so.
