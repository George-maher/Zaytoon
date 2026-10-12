# ZAYTOON — Architecture Audit & Fix Report

**Session:** `arena/af56e022-zaytoon` · **Date:** 2026-10-12
**Baseline commit (verified remote = local):** `0e550892d8120bdc8923be12ab142a7f3b5e46e2`
**Working commits on `arena/af56e022-zaytoon`:**

| Commit | Contents | Verified |
|---|---|---|
| `909e8a2` | Five defects: voice language handshake, TTS prosody, skill permissions, WS log level, desktop conftest | 2672 backend / 613 frontend / 366 desktop |
| `edf4815` | This report, first revision | — |
| `5510497` | Confirmation-gate fix, scope-enforced security layer, Security Workspace, three test-only defects | 2792 backend / 627 frontend / 366 desktop |
| `3e60fd6` `8bb6325` | Dev proxy for `/security`; report corrections | 627 frontend |
| `985cc88` `4475d05` | VMware/VM adapter, nine VM tools, guest SSH channel — see §5f | 2887 backend |
| `3c23eca` | The VM REST surface and the Virtual Machines screen — see §5f, §7 fourth batch | 2919 backend / 641 frontend / 366 desktop |

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

## 5f. Virtualization: VMware control and guest execution

**Before this work there was no virtualization support in the repository at
all** — `grep -i 'vmware|vmrun|virtualbox|libvirt|qemu'` over the source matched
nothing. Section 6 of the brief calls VM control a required feature, so this was
the largest genuinely-missing capability, and the most self-contained one to
build well.

### What was built

* `core/virtualization/` — an adapter package. `base.py` holds the contract
  (`Hypervisor`, `VMInfo`, `VMState`, `CommandOutcome`, program resolution);
  `vmware.py` holds every VMware-specific detail; `guest.py` holds the SSH
  channel. A second hypervisor would be a sibling file, not a branch in every
  tool. Nothing above the package knows that VMware exists.
* **Nine tools**, registered through the ordinary registry: `vm_list`,
  `vm_status`, `vm_start`, `vm_stop`, `vm_suspend`, `vm_resume`,
  `vm_open_console`, `vm_guest_check`, `vm_guest_exec`. The registry went from
  38 to 47 tools.
* **Three new named permissions** — `vm.discover`, `vm.manage`,
  `vm.guest.exec` — rather than one. The brief requires VM lifecycle, guest
  command execution and desktop automation to stay distinct, and a single grant
  would let consent for "start Ubuntu" authorise "run this in Ubuntu". The
  vocabulary went from 10 names to 13.
* **Seven HTTP routes** (`core/api/virtualization_api.py`, mounted in
  `api/main.py`): five read-only — discovery, inventory, single-VM, guest config
  report and an action catalogue — plus the two *acting* routes, VM action and
  guest command, both two-step. The app went from 90 to **97 OpenAPI paths**,
  which is the seven routes and nothing else. Without these the nine tools were
  reachable only by a chat turn, which is not a control panel.
* **A Virtual Machines screen** (`frontend/src/components/VirtualMachinesView.jsx`,
  destination `VIEWS.MACHINES`, glyph `◫`) with eight `lib/api.js` helpers. The
  inventory shows each VM's `state_evidence` next to its state, and `UNKNOWN` is
  never dressed as `stopped`. Row actions are filtered to the ones applicable to
  that VM's state, and a `dangerous` action cannot be confirmed until its
  acknowledgement box is ticked.
* **`docs/VMWARE.md`** — the real environment variables, the guest setup, and
  an explicit list of what is not implemented.

### The approval token is bound to the operation, not to the request

The acting routes are two-step: step 1 previews and issues a single-use token,
step 2 executes. The obvious implementation stores the token and then executes
whatever step 2 sends — which is a real bypass, because the thing the user
approved and the thing that runs would then be two different things. So the
record is `(tool, params, action)`, and step 2 **ignores the request body
entirely**: it runs the recorded operation, and it *reports* the recorded
operation, overwriting the result's `action`/`vm_ref` from the record rather than
echoing whatever the caller claimed.

Two guards exist for those two halves, and both were validated by fault
injection:

* Making step 2 honour the request body fails
  `test_step_two_ignores_the_body_and_runs_what_was_approved`.
* Storing the verb but *echoing the caller's claim* fails
  `test_the_answer_names_what_ran_not_what_was_asked` — a caller who previewed
  `start` and then sent `"action": "stop"` would have been told "stop" about
  something that started.

Demonstrated against the live server with a scripted `vmrun` on `PATH`:

```
preview start Ubuntu       -> tier confirm, token vm_d6cb9829f6ca
confirm with body claiming stop Parrot --hard
   reported action:        start       <- what ran
   reported vm_ref:        .../Ubuntu.vmx
   output:                 'Ubuntu' is now running (listed by `vmrun list`)
running VMs afterwards:    ['Ubuntu'] <- Parrot untouched
replaying the same token:  HTTP 409
```

The lesson that cost the most here: a stored tuple is a shape, and changing the
shape means grepping every site that unpacks it. Widening `_PENDING` to three
elements left `tool, recorded = pending` in the guest-exec branch, which raised
`ValueError: too many values to unpack` and surfaced as an HTTP 500 on step 2 —
visible only because the test asserts on the response *body*, not just the status
code. A response assertion that prints the body is what finds these.

### The decisions that matter

**Nothing assumes an install path.** `vmrun` and the UI binary are resolved from
an explicit setting, then `PATH`, then per-platform candidate directories — and
each candidate must stat as a regular *executable file*, so a directory named
`vmrun` is not returned as the program.

**State is reported from evidence, or reported unknown.** The one that matters:
if `vmrun list` cannot be run, a VM's state is `unknown`, **not** `stopped`.
Reported `stopped` without having asked is the kind of confident wrong answer
that makes an agent untrustworthy, and it is asserted against directly
(`test_vmrun_failing_means_unknown_never_stopped`). A `.lck` directory is
surfaced as a hint but never decides state, because a stale lock is
indistinguishable from a live one.

**Ambiguous names are refused.** Exact display name → exact `.vmx` stem →
*unique* substring. Two matches is an error listing both. There is deliberately
no closest-match branch: that is how "start Ubuntu" boots `Ubuntu-16.04-OLD`.

**Readiness is earned.** `vm_start` reports `running` and says, in its own
output, that a running process and a console window are not boot evidence.
`vmrun checkToolsState` is recorded as a *host-side* signal. Readiness is true
only when an authenticated command succeeds inside the guest.
`test_a_running_vm_without_a_probe_is_not_ready` and
`test_a_console_window_is_reported_as_a_window` pin both halves.

**No credentials, by construction.** `GuestTarget` has no password field.
Authentication is key or agent only; `BatchMode=yes` and
`PasswordAuthentication=no` are always passed. Host-key checking is `yes` by
default, `accept-new` is available for a freshly built guest, and `no` is
*unreachable through configuration* — a test asserts the refusal. A missing key
produces an instruction, not a hang.

**Destructive VM operations escalate through the existing table.** A `hard`
stop is DANGEROUS with the reason "hard power-off: discards whatever the guest
has not written", and a guest command is inspected by the same
`HIGH_RISK_COMMAND_PATTERNS` that already guard `execute_local_command`.
`vm_guest_exec` was added to `COMMAND_ACTIONS` and `STRUCTURED_EXEC_ACTIONS`, so
the escalation is the existing machinery rather than a parallel rule set.

### Verification — and its honest limit

Five test files, **138 tests**, all passing:

| File | Tests | Covers |
|---|---|---|
| `tests/core/test_virtualization_vmware.py` | 37 | Discovery, inventory parsing, state classification, resolution refusals, lifecycle argv, timeouts, readiness |
| `tests/test_vm_tools.py` | 47 | Tool tiers, permissions, prerequisite messages, the guest channel over a fake `ssh`, the gate |
| `tests/test_vm_workflows.py` | 10 | The six user requests of §6B as *distinct* operations, and the two never-conflate claims |
| `tests/test_virtualization_routes.py` | 32 | The HTTP surface: read-only routes cannot act, the body cannot redirect an operation, **asserted on the call *and* on the answer**; path recorded rather than display name; soft vs hard tier; single-use / expired / unknown / cross-surface tokens → 409; sweep-after-issue ordering; unknown VM lists candidates; 422s; `/actions` matches the registry; guest exec refuses before any socket |
| `frontend/src/components/VirtualMachinesView.test.jsx` | 12 | Inventory and evidence rendering, `UNKNOWN` never shown as stopped, action filtering by state, the two-stage confirm, and the acknowledgement gate on a dangerous action |

The fakes are **executables on disk**, spawned for real: a scripted `vmrun` and
a scripted `ssh`. That tests the argv the adapter builds and the output it
parses, which a monkeypatched runner would not. Three guards were validated by
injecting the fault each exists for — unknown-state→stopped, ambiguity→first
match, and host-key verification→disableable — and each failed exactly its own
test.

The route fakes (`test_virtualization_routes.py`) follow the same rule and are
deliberately *not* shortcuts: they are installed by
`monkeypatch.setattr(vt, "_ADAPTER_FACTORY", …)`, so the tests run the real route
code, the real registry, the real confirmation manager and the real approval
token map. An earlier attempt to test the route with a hand-built fake assembled
outside the fixtures passed while the real fixtures were failing — it proved the
code path existed, not that the route worked. The three bugs that run then
exposed (a `KeyError` from the `vm_status` data shape, the sweep running before
the prune, and the echoed verb) are all fixed and pinned. The binding guard was
fault-injected and failed exactly its own test.

**The limit, stated plainly: VMware is not installed on this host, so no real VM
was ever started and no real guest was ever connected to.** Everything above is
tested against fakes. That is a strong test of the adapter, of the routes and of
the screen, and a *weak* test of VMware itself, and `docs/VMWARE.md` says so in
its first paragraph. Do not read "138 passing tests" as "verified against
VMware". The live walkthrough above used a scripted `vmrun` — a real executable
spawned for real, which exercises the argv the adapter builds and the parsing it
does, but which is still a script, not VMware.

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

Third batch — virtualization (§5f):

| File | Change |
|---|---|
| `backend/core/virtualization/__init__.py` | **new** — the package surface |
| `backend/core/virtualization/base.py` | **new** — `Hypervisor`, `VMInfo`, `VMState`, `CommandOutcome`, `resolve_program`, `run_command` |
| `backend/core/virtualization/vmware.py` | **new** — the VMware adapter: discovery, inventory, state, lifecycle, readiness |
| `backend/core/virtualization/guest.py` | **new** — `GuestTarget`, `GuestSession`, `GuestResult` |
| `backend/tools/virtualization.py` | **new** — the nine VM tools |
| `backend/core/permissions.py` | `vm.discover`, `vm.manage`, `vm.guest.exec`; capability table |
| `backend/tools/safety.py` | `vm_guest_exec` joins `COMMAND_ACTIONS` + `STRUCTURED_EXEC_ACTIONS`; `vm_stop --hard` → DANGEROUS |
| `backend/tools/registry.py` | `virtualization` module registered |
| `backend/config.py` | Virtualization section (13 settings) |
| `backend/core/skills/manifest.py` | The prose count of the vocabulary removed, not updated |
| **new** `backend/tests/core/test_virtualization_vmware.py` | 37 tests |
| **new** `backend/tests/test_vm_tools.py` | 47 tests |
| **new** `backend/tests/test_vm_workflows.py` | 10 tests |
| **new** `docs/VMWARE.md` | Setup, variables, and the unverified-path warning |
| `backend/tests/core/test_core_permissions.py`, `test_api_core.py` | The vocabulary pin 10 → 13, with the reason recorded |
| `frontend/vite.config.js` | `/security` dev proxy; `allowedHosts` opt-in via `VITE_ALLOWED_HOSTS` |

Fourth batch — the virtualization REST surface and the screen (§5f). Until this
batch the nine tools existed but had **no HTTP route and no UI**, so the only way
to reach them was a chat turn:

| File | Change |
|---|---|
| `backend/core/api/approvals.py` | **new** — `ApprovalTokens` (issue/consume, TTL re-checked at use) and `run_approved_tool`, which passes a real `ConfirmationManager` into `registry.execute` |
| `backend/core/api/registry_api.py` | Refactored onto `approvals.py`; its hand-rolled token map removed, so security and virtualization share one implementation |
| `backend/core/api/virtualization_api.py` | **new** — 7 routes: 5 read-only (including the action catalogue), plus the 2 acting two-step routes |
| `backend/core/api/routers.py`, `backend/api/main.py` | Export and mount the router (97 OpenAPI paths) |
| `backend/core/virtualization/guest.py` | `guest_config_report()` appended — what is configured, **never the secret**; a forbidden host-key policy is reported and coerced to `strict` |
| `backend/tools/virtualization.py` | `VMStatus.execute` catches `VMOperationError` instead of letting it reach the client as a 500 |
| `frontend/src/lib/viewState.js` | `VIEWS.MACHINES` destination ("Virtual Machines" / "الأنظمة الافتراضية", glyph `◫`) |
| `frontend/src/components/VirtualMachinesView.jsx` | **new** — hypervisor state, inventory with evidence, state-filtered actions, two-stage confirm, acknowledgement gate |
| `frontend/src/lib/api.js` | Eight VM helpers |
| `frontend/src/App.jsx` | Mounts `VirtualMachinesView` on `VIEWS.MACHINES` |
| `frontend/vite.config.js` | `/virtualization` dev proxy |
| **new** `backend/tests/test_virtualization_routes.py` | 32 tests |
| **new** `frontend/src/components/VirtualMachinesView.test.jsx` | 12 tests |
| `frontend/src/App.runtime.test.jsx` | The Virtual Machines nav entry now proves its screen mounts (17 total) |

Every new test was run against the reverted fix and observed to fail before
being accepted — 5, 4 and 6 failures respectively for the voice, skill and
desktop guards; 1 for the token TTL (§5c); 1 for the App wiring of the Security
screen; 1 for the strengthened tier test (§5d); 1 for the approval token's
binding to the recorded operation; 1 for the acknowledgement gate that stops a
`dangerous` action being confirmed unticked; and the "never contacted" guard was
validated by injecting the fault *before* the check, since an injection placed
after it proved nothing.

Three of the four defects found in this batch were found **by the tests, not by
reading the code**: the `vm_status` payload shape, the prune/sweep ordering, and
the echoed verb that would have told a caller "stop" about something that
started. The fourth — a 2-tuple unpack that raised `ValueError` into an HTTP 500 —
was found because the assertion prints the response body. That is the pattern
worth keeping: assert on the body, and print it.

## 8. Requirement status

| Area | Status | Evidence |
|---|---|---|
| Remote commit inspection | **PASS** | SHA verified, `0 0` divergence |
| Backend tests | **PASS** | 2919 passed / 5 skipped (baseline 2650/5; +269 across four batches) |
| Frontend lint/test/build | **PASS** | eslint clean; 641 passed; build 503 kB / 144 kB gzip (crossed the 500 kB warning threshold with this screen; see §9) |
| Desktop tests (Linux) | **PASS** | 366 passed / 2 skipped (was a collection error) |
| Voice — language handshake | **PASS** | reproduced, fixed, 8 tests, guard verified |
| Voice — TTS prosody | **PASS** | reproduced, fixed, 5 tests |
| Voice — real microphone/audio | **NOT TESTED** | no audio device; no interactive desktop |
| Voice — Arabic audio playback | **NOT TESTED** | needs a sound device + network to edge-tts |
| Web search | **BLOCKED — infrastructure, not code** | **Corrected.** The earlier explanation (a missing `trust_env`, so `HTTPS_PROXY` was ignored) was wrong: there are **no proxy variables set at all** in this environment, so no proxy was being bypassed. Egress here is an **allow-list**: `pypi.org` and `registry.npmjs.org` answer 200 while `api.github.com`, the DuckDuckGo hosts and `huggingface.co` fail with `ConnectError`. Search therefore cannot reach any provider from this sandbox, and no code change can alter that. The tool still degrades honestly (`Search failed: …`) rather than inventing results. **Unverified on a machine with real egress.** |
| Computer control | **PARTIAL** | 47 tools over `ToolRegistry`; app launching/interception is Windows-first and unverifiable here |
| Skills management | **PARTIAL** | create/validate/enable/disable/delete + permission declaration work end-to-end (verified against a live server); natural-language creation, drafts, version history and per-skill execution logs are **not implemented** |
| Security / permissions (the gate) | **PASS** | the declared tier now reaches the prompt for every tool, including skill-registered ones; 8 tests, and 19 pre-existing duck-typed doubles still pass |
| Security — target scope enforcement | **PASS** | enforced in `tools/security.py` before any socket, not in prompts; 65 scope tests + 19 probe tests + 28 route tests; **no registered tool can reach `scope.create`** |
| Security — non-destructive assessment + evidence | **PASS** | confirmed-only findings with evidence and remediation; Scenario F all five legs PASS live |
| Security workspace UI | **PARTIAL — running, not yet human-verified** | the page exists, is wired into `App.jsx`, and its nav entry proves its screen mounts (12 tests). A dev server is running in this session on port 3000 with the backend on 8000, so the page can be opened and driven now (see below). I have confirmed the served app and its `/security` data path over HTTP, but **I have no browser here**, so nobody has clicked it yet — the last mile is yours to confirm. |
| JARVIS UI/UX, orb, RTL | **PARTIAL** | pre-existing HUD/orb/theme and i18n exist and are untouched; I did **not** redesign the UI, so I make no claim about it. The new page follows the existing token/glyph system and switches language without reload (tested). |
| Virtual machines — VMware discovery & lifecycle | **PARTIAL — implemented, tested against fakes, never run against VMware** | 47 adapter + tool tests, 10 workflow tests, 32 route tests. Discovery, inventory, state-with-evidence, lifecycle argv, timeouts and refusal-to-guess all covered, and the two-step approval was walked live against a scripted `vmrun`. **VMware is not installed here**, so no real VM was started: the real-VMware path is unverified and `docs/VMWARE.md` leads with that. |
| Virtual machines — guest command execution | **PARTIAL — implemented, never run against a real guest** | Key-only SSH channel with host-key verification; stdout/stderr/exit code/duration captured; prerequisite failures (no account, no address, no Tools) each reported precisely. The route refuses before any socket when the channel is unconfigured — verified live. No guest existed to connect to. |
| Virtual machines — snapshots, networking, deletion | **FAIL (not implemented)** | Deliberately absent rather than half-built; list in `docs/VMWARE.md`. |
| Virtual Machines screen | **PARTIAL — running, not yet human-verified** | `VirtualMachinesView.jsx` + `VIEWS.MACHINES` + 8 API helpers, 12 tests, eslint clean, and the nav entry proves the screen mounts (17 App tests). A dev server is live on port 3000 with the backend on 8000 and a scripted `vmrun`, so the page can be opened now; **nobody has clicked it**, because there is no browser here. |
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
3. **Run the Security Workspace and the Virtual Machines screen in a real
   browser** against a live backend. Both are wired, tested and reachable; both
   have only ever been fetched over HTTP, never rendered by a person.
4. **Code-split the bundle.** The main chunk reached **503 kB** (144 kB gzip)
   with this work and now trips Vite's 500 kB warning — a threshold, not a
   defect, but it is the first time this project has crossed it and the next
   screen will make it worse. `React.lazy` per destination is the fix and it is
   an afternoon's work, not a rewrite; it does not belong in the same change as
   a new subsystem.
5. **Skills**: natural-language creation, draft saving, version history,
   per-skill execution logs (the UI currently has
   create/edit/enable/disable/delete only).
6. **Real-device voice validation** on Windows and macOS — and an interactive
   LLM turn, which needs Ollama, absent here.
7. **`security_probe` covers configuration only.** It checks response headers
   from one non-destructive request. It does not test authentication,
   authorisation logic, input handling or any payload-driven class of bug, and
   the report it prints says so. Widening it is a separate piece of work with
   its own scope rules.
8. **Verify the VMware path on a host that has VMware.** This is the single
   largest unverified area of the virtualization work, and it cannot be closed
   here. The specific things to confirm first, in order: that `vmrun list`
   output parses on Windows (paths with spaces and the `Total running VMs:`
   header), that `-T ws` is right for the installed edition, and that
   `getGuestIPAddress` returns a usable address for the guest being targeted.
9. VM snapshots, network/resource changes and VM deletion — see
   `docs/VMWARE.md`.
10. Cosmetic: `tests/test_process_manager.py` intermittently emits
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

### Virtualization (no VMware needed — the fakes are executables)

```bash
cd backend && PYTHONPATH=../.github python -m pytest \
  tests/core/test_virtualization_vmware.py \
  tests/test_vm_tools.py \
  tests/test_vm_workflows.py -q          # 94 passed

# What the tools do on a host with no VMware: an actionable message, and no
# security refusal (blocked is for decisions, not for missing software).
cd backend && PYTHONPATH=../.github python - <<'EOF'
import asyncio, api.main as api_main
async def main():
    for tool, params in (("vm_list", {}), ("vm_start", {"vm": "Ubuntu"})):
        r = await api_main.tool_executor.execute(tool, params)
        print(tool, "| success:", r.get("success"),
              "| blocked:", r.get("blocked"),
              "| needs confirm:", r.get("requires_confirmation"))
asyncio.run(main())
EOF
# -> vm_list  | success: False | blocked: None | needs confirm: None
# -> vm_start | success: False | blocked: None | needs confirm: True

# The vocabulary and the tool count, live.
curl -s localhost:8000/permissions | python -c \
  'import sys,json; d=json.load(sys.stdin); print(len(d["vocabulary"]), "grants"); print([t["name"] for t in d["tools"] if t["name"].startswith("vm_")])'

# On a machine that does have VMware, this is the first thing to try.
export VMWARE_VMRUN="/usr/bin/vmrun"   # or leave unset to be discovered
```

### Virtualization — the HTTP surface, and the binding guard, live

Nothing here needs VMware either. The script below is a `vmrun` stand-in that
tracks which VMs it started, so the lifecycle is *observable* rather than merely
acknowledged. It must be a real executable on `PATH`, because the adapter spawns
it.

```bash
mkdir -p /tmp/fakevms/ubuntu /tmp/fakevms/parrot
printf 'guestOS = "ubuntu-64"\n' > /tmp/fakevms/ubuntu/Ubuntu.vmx
printf 'guestOS = "debian-64"\n' > /tmp/fakevms/parrot/Parrot.vmx
cat > /tmp/fake-vmrun <<'SH'
#!/usr/bin/env python3
import json, os, sys
STATE = "/tmp/fake-vmrun-state.json"
argv = sys.argv[1:]
verb = argv[2] if argv[0] == "-T" else argv[0]
rest = argv[3:] if argv[:1] == ["-T"] else argv[1:]
state = json.load(open(STATE)) if os.path.exists(STATE) else {"running": []}
running = state.setdefault("running", [])
if verb == "list":
    print("Total running VMs: %d" % len(running)); [print(p) for p in running]; sys.exit(0)
if verb in ("start", "stop"):
    t = rest[0]
    running.append(t) if verb == "start" and t not in running else None
    running.remove(t) if verb == "stop" and t in running else None
    json.dump(state, open(STATE, "w")); print("%s %s" % (verb, t)); sys.exit(0)
if verb == "getGuestIPAddress":
    print("10.0.0.9"); sys.exit(0)
print("vmrun: %s ok" % verb)
SH
chmod +x /tmp/fake-vmrun

# Serve with the fake hypervisor and an inventory directory that has VMs in it.
cd backend && CORE_DB_PATH=/tmp/vm-live.sqlite VMWARE_VMRUN=/tmp/fake-vmrun \
  VM_INVENTORY_DIRS=/tmp/fakevms PYTHONPATH=../.github \
  ../.venv/bin/python -m uvicorn api.main:app --port 8000 &

B=http://127.0.0.1:8000
curl -s $B/virtualization/hypervisors | head -c 200
curl -s $B/virtualization/vms        # each state carries its state_evidence

# Preview a start, then spend the token on a DIFFERENT operation. What runs, and
# what the answer says ran, must both be the recorded `start Ubuntu`.
P=$(curl -s -X POST $B/virtualization/vms/action -H 'Content-Type: application/json' \
      -d '{"action":"start","ref":"Ubuntu"}')
T=$(echo "$P" | python -c 'import sys,json; print(json.load(sys.stdin)["confirm"])')
curl -s -X POST $B/virtualization/vms/action -H 'Content-Type: application/json' \
  -d "{\"action\":\"stop\",\"ref\":\"Parrot\",\"mode\":\"hard\",\"confirm\":\"$T\"}"
#   -> "action": "start", "vm_ref": ".../Ubuntu.vmx"   (the body is ignored)
curl -s $B/virtualization/vms | python -c \
  'import sys,json; print([v["name"] for v in json.load(sys.stdin)["vms"] if v["state"]=="running"])'
#   -> ['Ubuntu']      Parrot untouched
curl -s -o /dev/null -w '%{http_code}\n' -X POST $B/virtualization/vms/action \
  -H 'Content-Type: application/json' -d "{\"action\":\"start\",\"ref\":\"Ubuntu\",\"confirm\":\"$T\"}"
#   -> 409   the token is single-use
```

The tests for this surface, and for the screen that drives it:

```bash
cd backend && PYTHONPATH=../.github ../.venv/bin/python -m pytest tests/test_virtualization_routes.py -q   # 32 passed
cd frontend && npx vitest run src/components/VirtualMachinesView.test.jsx                                # 12 passed
```

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
| `frontend/src/lib/viewState.test.js` | Vocabulary pin extended for the two new destinations | extended, not relaxed |
| `frontend/src/App.runtime.test.jsx` | Security and Virtual Machines destinations added to the nav walk, asserting each screen mounts | extended |
| `backend/tests/core/test_core_permissions.py`, `test_api_core.py` | Vocabulary pin 10 → 13 | updated, with the reason recorded in the test |

The permission-vocabulary pin is the one number that changed because the
product changed rather than because a test was inconvenient: `vm.discover`,
`vm.manage` and `vm.guest.exec` are three genuinely distinct grants, and the
test now says so instead of just counting. The same count appeared in three
places and in one prose docstring; the docstring now names no number, because a
number in prose is a number that will be wrong.

No test file was deleted, and no failing test was removed to reach green. Three
frontend assertions were *rewritten*, and all three are recorded here because a
rewritten assertion is where a weakened one would hide:

* `VirtualMachinesView.test.jsx` first used `getByText("Ubuntu")` and
  `getByText("CONFIRM")`. Both threw — legitimately: each machine name appears
  twice (inventory and guest selector), and the confirm button's accessible name
  is `CONFIRM · <action>`, built from two text nodes. The fix was to query the
  list by its `aria-label` and the button by role; the assertions became
  *stronger*, because they now pin which region each name came from.
* The same file's failure count dropped 7 → 0 only after the duplicate was
  *explained*, not after the assertion was loosened.

The one apparent relaxation — `viewState.test.js` — is an extension: real
destinations with mounted screens were added, and the test pins the vocabulary
exactly, so both lists grew. It is not a loosened assertion; it is the same
strict `toEqual` over more entries.

**On the response-shape assertions.** `test_virtualization_routes.py` asserts
with the body included (`assert body["success"] is True, body`). That single
habit is what made three defects legible instead of mysterious: a literal
`{ref}` in a message, a verb echoed from the request, and a 500 whose only clue
was a `ValueError` in the server log.

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
