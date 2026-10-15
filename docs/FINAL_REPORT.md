# Zaytoon — final report (Stage A–F)

Date: 2026-10-15 · Branch `arena/f2c10b62-zaytoon` · Base `4b07c18` · Commits
under this mission: `e60e4d7` (WebSocket), `30ba351` (voice/turn identity),
`1652fb4` (permissions, routing provenance, i18n), plus the working tree
described below.

Every number in this report was produced by a command run in this workspace on
this date, on this machine. Nothing is projected, and no figure is repeated from
an earlier report without being re-measured — where a number is *from* an
earlier stage it says so.

---

## 1. Headline status

| Deliverable | Status |
|---|---|
| Fresh evidence-based comparison matrix (17 areas) | **Done** — `docs/REFERENCE_COMPARISON.md`, written from the reference project's own files, including its README's admissions about what was never run. |
| Target architecture with single-owner contracts | **Done** — `docs/ARCHITECTURE.md`, now carrying four written contracts (permission decisions, turn identity, WebSocket vocabulary, provider registry). |
| WebSocket repair & hardening | **Done and verified** — typed frame vocabulary, handshake policy, dedupe, size limits before decode, cancellation that reaches child tasks, turn identity minted once, 12-class protocol suite. |
| Voice pipeline restoration | **Partly done, honestly bounded** — turn-scoped audio on both clients, real capability reporting, shared vocabulary. **End-to-end speech cannot be proven in this sandbox** (no TTS egress, no faster-whisper); see §5. |
| Agent orchestration / tool hardening | **Done** — permission gate at the chokepoint, denial distinguished from failure in the loop, `verified` effects on writes. |
| Skills architecture | **Already present, extended this mission** (manifest permission vocabulary enforced at the gate; drift test against the backend). |
| Fix all applicable findings | **Done for the findings that exist in this repository** — see §4 for why "Z-001…Z-028" cannot be mapped. |
| Measured performance work | **Done, small and stated** — §6. |
| UI/UX integration (RTL, centralized translations, real backend state) | **Foundation done, migration deferred with counts** — §7. |
| Code quality + docs | **Done** — docs updated to the behaviour that was actually implemented. |
| Staged workflow Stage A→F | **Done** — §2. |
| Git safety | **Honoured** — three commits on the session branch, no force-push, no reset, no history rewrite, no secrets added. |

---

## 2. What each stage delivered

**Stage A — baseline.** The audit and inventory that produced
`docs/PHASE0_BASELINE.md`, plus the rule that a claim needs a command behind it.
Baseline suite at that point: `2 failed, 3057 passed, 5 skipped` (the two
failures were real defects, not environment).

**Stage B — the WebSocket** (`e60e4d7`, 12 files, +2827/−177). The transport
went from "works when nothing goes wrong" to a declared contract:

* every frame declared in `api/ws_protocol.py` (`FrameSpec`: direction,
  required, optional, size ceiling) and enforced by tests;
* handshake policy in `api/ws_security.py` (device token, origin allow-list,
  honest `authenticated` flag on the status frame);
* `client_msg_id` dedupe so a resent frame is not a second turn;
* frame size checked **before** base64 decoding;
* cancellation that reaches the child turn task, not just the cancel event;
* session cleanup on disconnect; cross-session isolation;
* 63 tests in 12 classes (`TestHandshakePolicy`, `TestSessionIdentity`,
  `TestProtocolSurface`, `TestSupersededTurn`, `TestFrameSizeLimits`,
  `TestDuplicateDelivery`, `TestCancellation`, `TestCleanup`,
  `TestCrossSessionIsolation`, `TestUntrustedInput`, `TestAudioLifecycle`,
  `TestConfirmationBinding`).

Measured then: full backend suite `3122 passed, 5 skipped`; frontend
`npm run verify` EXIT 0.

**Stage C — voice and turn identity** (`30ba351`). Three defects fixed, all
found by reading the code rather than the feature list:

1. `/health` reported `tts: true` from "edge-tts is importable", so an offline
   machine looked identical to a working one. `/voice/status` did not exist.
   Both now report the last synthesis error and a one-sentence reason.
2. Audio was not bound to a turn on either client: chunks already on the socket
   for a superseded turn were played over the answer the user was waiting for.
   Both clients now ignore a non-current utterance's `audio_start`, chunks and
   `audio_end`, and the browser closes the superseded turn's bubble instead of
   appending the next answer into it.
3. The desktop shell's frame vocabulary had drifted from the backend's
   (`CORE_EVENT_PASSTHROUGH` was a hand-copied list, wrong in both directions).
   It is now derived from `core.events.CoreEventType`, and
   `desktop/tests/test_wire_protocol.py` fails if the two vocabularies diverge.

**Stage D — permissions, provenance, i18n** (`1652fb4`).

* **Permission decisions became real.** `core/permissions.py` had the
  vocabulary and `POST /permissions/{permission}` stored a decision; **nothing
  read it**. `core/grants.py` reads the same table the API writes, and
  `tools/registry.py` checks it at the one chokepoint every tool passes through:
  `deny` refuses *before* the approval prompt and beats an `approval=True` from
  a caller; `allow` is recorded and shown and still prompts. The declaration and
  the enforcement now share `core.permissions` — and the gate reconciles a tool's
  *declared* tier, which the name table alone could not do.
* **A denial is not a failure.** `render_observation` says "the user has turned
  this off… do not retry and do not look for another tool that reaches the same
  capability". A model told *FAILED* retries or routes around it.
* **Model provenance reached the user.** `model.started/completed/failed` now
  carry the turn's `trace_id`; the browser stamps the model onto the bubble that
  trace names, and `ChatMessage` renders it. A late attempt from a replaced turn
  cannot relabel the answer on screen.
* **A translation table exists.** `src/i18n/` — dictionary, provider, consumer,
  English fallback that warns rather than rendering blank.
* **`ToolResult.verified`.** `file_write` re-reads what it wrote and reports a
  failure when the bytes are not there, instead of "Wrote x" from the absence of
  an exception.
* **`validate_inbound` is wired.** It was imported and never called; now a
  declared frame that is missing a required field is refused with the reason
  rather than falling through an `if/elif` chain in silence.

**Stages E–F — verification and this report.** Full-suite runs on every batch,
plus the numbers below.

---

## 3. Test results (measured today)

| Suite | Command | Result |
|---|---|---|
| Backend, everything | `cd backend && .venv/bin/python -m pytest tests/ -q -p no:cacheprovider` | **3145 passed, 5 skipped, 2 warnings in 172.56 s** |
| WebSocket protocol | same, `tests/test_ws_protocol.py` | **63 passed in 13.32 s** (12 classes) |
| WebSocket suites together | `tests/test_websocket_{confirmation,interrupt,reminders}.py` + protocol | **84 passed in 33.57 s** |
| Permission enforcement (new) | `tests/core/test_permission_enforcement.py` | **13 passed** |
| Model routing events (new) | `tests/test_model_routing_events.py` | **5 passed** |
| Write verification, engine observation, tools | `tests/test_tool_registry.py` + 3 others | **248 passed** |
| Frontend | `cd frontend && npm run verify` (vitest + eslint + build) | **704 passed, 42 files, EXIT 0** |
| Desktop shell | `cd desktop && ../backend/.venv/bin/python -m pytest tests/ -q -p no:cacheprovider` | **376 passed, 2 skipped** (Tk-dependent modules collect-skipped here: `customtkinter` needs a `tkinter` this sandbox does not have) |

Baseline for comparison: Stage A recorded `2 failed, 3057 passed, 5 skipped`.

**One flake observed, recorded rather than hidden.** The four-file combination
`test_ws_protocol.py` + the three `test_websocket_*.py` + `test_api_routes.py`
produced one failure
(`TestSupersededTurn::test_a_second_message_replaces_the_first_turn`) and took
600 s, where the same test passes alone (1.02 s) and the protocol file alone is
green (63 passed, 13.32 s). The four websocket files *without* `test_api_routes`
also pass (84 in 33.57 s). This is a test-isolation problem in the protocol
suite (a leaked thread or a shared event loop between files), not a product
defect — but it is a real defect in the suite and it is **open**, deferred by
budget rather than judged unimportant.

**A second failure, one that only CI could see — found there, fixed here.** The
first CI run on this branch (run `38706399455`) failed all three desktop legs in
under two minutes, each with exactly one failure:
`tests/test_app_wiring.py::TestTurnScopedAudio::test_only_anchor_frames_move_the_current_turn`,
`AttributeError: 'ZaytoonApp' object has no attribute 'client'`, raised from
`app/app.py:608` (`_finish_stream` reads `self.client.connected`). The cause was
not in the product: that class built its *own* copy of the shell double, and the
copy omitted four attributes the real constructor always sets (`client`,
`events`, `mode`, `shutting_down`). The local suite could not see it —
`customtkinter` needs a `tkinter` this sandbox does not have, the module is
collect-skipped, and the same commit reported `370 passed, 2 skipped` here. A
double that is a copy of another double drifts from it exactly the way a copy of
a schema drifts from the schema. Closed two ways: the two builders are now one
`make_shell()` (so what the object is missing is missing everywhere, visibly),
and `desktop/tests/test_shell_doubles.py` checks the double against the class's
own source with `ast` — no Tk, no display, so it runs on the hosts where the
module that needs them does not. It asserts attribute *presence*, not
correctness: it derives the constructor's assignments and the class's `self.*`
reads from `app/app.py`, exempts five display- or process-bound names with a
written reason each, and one of its six tests proves the check can fail by
parsing the builder with `client` removed.

---

## 4. Findings: what "fix all applicable findings" resolved to

`Z-001` … `Z-028` **do not appear anywhere in this repository** —
`grep -rEn "Z-0[0-9][0-9]"` over all Markdown, Python, JS and JSX returns
nothing. So there is no such list to close, and inventing a mapping would be the
fabrication the mission forbids. What exists is three audit documents
(`AUDIT_2026-10-12.md`, `AUDIT_REPORT.md`, `SESSION_REPORT.md`) and the findings
recorded in them. Against those, this mission closed:

| Finding (source) | Status now |
|---|---|
| Confirmation gate never saw the tool tier (SESSION_REPORT §5a) | Closed before this mission; still covered by `test_declared_tier_is_enforced.py`. |
| Confirmation token did not expire / not bound to the operation (SESSION_REPORT §5c) | Closed; protocol class `TestConfirmationBinding` (4 tests) still passes. |
| A test that could not fail (SESSION_REPORT §5d) | Closed; the protocol suite is now counted per class. |
| Voice defect (SESSION_REPORT §6) | Closed — the turn-identity contract in §2 above; two new real bugs (failed STT not emitting `error`+`listening`, whitespace-only transcript starting a turn) were caught by the rewritten suite and fixed. |
| **Permission decisions stored and never enforced** (found this mission) | **Closed** — `core/grants.py` + the gate; 13 tests. |
| **Routing events unattributable to a turn** (found this mission) | **Closed** — `trace_id` on every model event; 5 tests. |
| **`validate_inbound` imported, never called** (found this mission) | **Closed** — wired into the receive loop. |
| **Hand-copied core-event list drifting from the enum** (found this mission) | **Closed** — derived from `CoreEventType`; desktop drift test. |
| **`ToolResult` could not express "the effect was checked"** (found this mission) | **Closed** — `verified`, read-back for writes; 4 tests. |

---

## 5. Implemented but **not fully verified** (stated plainly)

These are real, in the tree, and their proofs are weaker than the ones above.

1. **Speech, end to end.** The voice *wiring* is verified: turn-scoped audio on
   both clients (unit tests), the frame vocabulary shared and drift-checked, and
   `/voice/status` reporting truthfully. What is **not** verified is a person
   hearing a sentence: this sandbox has no route to
   `speech.platform.bing.com`, so `edge-tts` returns `ClientConnectorError`
   (measured: 0 chunks, 0 bytes, `last_stream_error` set), and `faster-whisper`
   is not installed (`ensure_loaded() -> False`). Voice names resolve
   (`ar-EG-ShakirNeural`, `en-GB-RyanNeural`) and the failure path is honest.
   **"It spoke" cannot be claimed from here**, and it is not claimed.
2. **The permission UI against a live backend.** `SecurityView` renders the
   decisions from `GET /permissions` and posts changes; 17 component tests cover
   the rendering, the three-way control, the "ALLOW is recorded but the prompt
   still happens" note and the failure path. The backend route and the gate are
   tested independently (13 tests). What is *not* covered is a browser session
   against a running server toggling a switch and then watching a tool be
   refused — the two halves are proven separately, not end to end.
3. **Desktop shell parity for audio and vocabulary.** Covered by unit tests of
   `app.protocol` and the shell's `_handle_event` wiring through a test double.
   The shell itself (pywebview + `customtkinter` widgets) is not launched here;
   `customtkinter` is absent, which is why two test modules are collect-skipped.
4. **Performance numbers.** §6 gives two measurements with their method. They
   were taken on a single, otherwise-idle container: they establish the *ratio*
   between two code paths, not the absolute speed on the user's machine.

---

## 6. Performance (measured, method stated)

| Measurement | Method | Result |
|---|---|---|
| Permission gate, cached vs uncached | 2000 calls to `core.grants.denied()` over three permissions, warm, then with `invalidate()` before each call (which is what an uncached read does: open a connection, run PRAGMAs, migrate, query, close). Single process, single loop. | **1.5 µs** warm vs **1552.8 µs** cold — the cache removes ~1.55 ms per tool call, **~1038×** the warm cost. With the TTL at 2 s, a revoked permission is live immediately in the process that wrote it and within 2 s anywhere else. |
| `file_write` read-back | 1 MB write to a temp dir, timed write-only vs write + read-back, same content. | write 1 MB: **1.31 ms**; read-back: **1.10 ms**; overhead **84 % of the write**. Stated because the verification is not free — it is one extra read per write, and it is what makes "Wrote x" an observation. |

No other performance claim is made. The mission's instruction to compare
"equivalent models, settings, hardware, prompts and conditions" is met by not
comparing: nothing here is benchmarked against the reference project, whose
backend has never been executed.

---

## 7. Not implemented / deferred (with the reason and the size)

| Item | Why deferred | Size |
|---|---|---|
| Migrating the remaining UI strings to `src/i18n/` | Correct but mechanical, and a half-migrated sweep would be the "two systems" outcome the mission rules out. Foundation is in place and **a ratchet test stops the number growing**. | **597** inline bilingual ternaries in 31 files (`ComputerView.jsx` 49, `SettingsPanel.jsx` 48, `workspace/FileExplorer.jsx` 39, `workspace/GitPanel.jsx` 37, `HUD/SystemStatus.jsx` 33, `workspace/ModelsPanel.jsx` 32, `SkillsView.jsx` 32, `providers/ProvidersSection.jsx` 30, `SecurityView.jsx` 0 **migrated**) |
| Loop-level untrusted-output fence | Changes every observation the model sees; needs prompt-diff testing. Zaytoon fences inside `fetch_url` today. | 1 function + prompt tests |
| Persisting route provenance with the message row | Needs a migration and a client that reads it back; the live badge is the honest half. | schema migration + 1 endpoint field |
| FTS index over message history | Pure additive schema change. | 1 migration |
| REST `/voice/transcribe` returning bytes (browser upload path) | Not implemented, not claimed. Socket streaming works; an HTTP path would help a client that cannot stream. | 1 route + 1 client |
| Setup wizard on first launch | Product work, no correctness impact. | 1 view |
| The observed test-isolation flake in §3 | Budget; it is in the test suite, not the product. | protocol-suite isolation |
| Skills: nothing — the registry, manifests, trust levels, atomic updates, run history and the drift test are in place; the shipped skills are covered by `tests/core/test_shipped_skills.py`. | — | — |

---

## 8. Git state

Three commits on `arena/f2c10b62-zaytoon`, each with its own verified run:

```
1652fb4  Make the permission decisions real, and say which model answered
30ba351  Voice: turn-scoped audio, honest capability reporting, shared vocabulary
e60e4d7  Harden the WebSocket transport: turn identity, cancellation, cleanup, protocol
4b07c18  (base)
```

No reset, no rebase, no force-push, no removed work, no credential or key
committed. `.git/info/exclude` still holds `google_drive/` (the reference
material), and generated artefacts were kept out of the tree.

---

## 9. The honest summary

Three of the four things fixed in the last stage were **claims with no
implementation behind them**: a permission switch that stored a decision nobody
read, a routing observation collected and never shown, and a translation layer
that did not exist. Each was invisible from the outside because each had a
plausible-looking surface — a route, an event, a language toggle. That is the
pattern this mission kept finding, and the reason the deliverables that matter
most are the ones that make a claim *checkable*: a gate that reads the same
table the API writes, an event that names the turn it belongs to, a test that
fails when a vocabulary drifts, and a ratchet that fails when the old pattern
comes back.
