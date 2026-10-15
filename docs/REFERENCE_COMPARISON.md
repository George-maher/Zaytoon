# Zaytoon vs. the reference J.A.R.V.I.S. project — evidence-based comparison

Date: 2026-10-15 · Zaytoon at `1652fb4` (+ the working tree described in
`docs/FINAL_REPORT.md`) · Reference: the `Jarvis` folder in Google Drive
(`README.md` 15 791 B, `backend/app/*.py`, `src/i18n/*`, `src/components/*`).

This document exists to prevent two failure modes: copying the reference on the
strength of its README, and dismissing it because it is smaller. Every cell
below is traceable to a file. Where the reference's own README contradicts its
code, the code wins and the contradiction is named.

---

## 0. What the reference actually is (its own words, verified)

The reference's README is unusually candid, and quoting it is the fastest way to
set the comparison on a factual footing:

| Claim in `README.md` | Verified status |
|---|---|
| "**Frontend**: fully implemented, builds cleanly (`npm run build` verified)" | True. `src/` is a complete React+TS app. |
| "**Python backend**: fully written … **not verified in this environment** (no Python runtime available to this tool)" | True — and it means the backend, its tests and the desktop shell have **never been executed**. |
| "**Frontend ↔ Python backend wiring**: … does **not yet** call the FastAPI REST/WebSocket endpoints" | True. `README.md` §"What actually works right now": the shipped app talks to Ollama/OpenRouter **from the browser** and persists to `localStorage`. |
| "System Status / Logs pages: live (**simulated in-browser**) CPU/RAM gauges" | True, and self-labelled. |
| "**Offline Mock provider**: … chat still works with a clearly-labeled offline response" | True. |
| "Voice page: … using the browser's **Web Speech API** … documented as a stand-in for the backend's faster-whisper/edge-tts pipeline" | True. |

So the reference is a **frontend simulation with an unexecuted backend**, and
Zaytoon is the inverse: a verified backend (3145 tests) whose frontend is
thinner. That single asymmetry drives most of the rows below.

---

## 1. The matrix

Legend — **✓** verified by reading the reference's source or its own run
output; **~** present but weaker; **—** absent; **n/e** not evidenced in the
reference material available.

| Area | Reference (evidence) | Zaytoon (evidence) | Verdict |
|---|---|---|---|
| **Architecture** | One FastAPI app (`app/main.py`), routers `api.py` + `ws.py`, one process-wide `RuntimeModeManager` (`agents.py` docstring). Clean layering, ~65 KB of Python. **✓** | Layered: `api/` (HTTP+WS), `core/` (tasks, workspace, skills, events, permissions), `agents/` (8 + engine), `tools/` (36 + registry), `services/`, `voice/`, `memory/`. ~102 kLOC. **✓** | Reference shows the shape more clearly per kilobyte; Zaytoon has the subsystems the shape implies. No structural borrow needed. |
| **WebSocket** | `ws.py` (3 945 B): one socket, `chat` \| `cancel`, `task_id = uuid4()` per turn, frames `token`/`done`/`error`, routing `decision` attached to `done`. **No** auth, origin check, dedupe, size limit, per-session isolation, or confirmation binding. **✓** | `api/websocket.py` + typed `api/ws_protocol.py` (`FrameSpec` per frame, direction, required/optional, size ceilings); handshake policy incl. device token + origin allow-list; `client_msg_id` dedupe; size checked **before** base64 decode; one owner per event class; turn id minted once; confirmation tokens bound to the request. Protocol suite: **63 tests / 12 classes**. **✓** | Zaytoon is strictly stronger. The reference's one idea — routing decision **on the frame that ends the turn** — was adopted in a stronger form (see *Models*). |
| **Voice** | `voice.py`: faster-whisper lazy-loaded on CPU/int8 with `vad_filter`; `synthesize()` returns MP3 **bytes**; `voice_available()` sets `stt/tts` true **when the import succeeds**. Frontend uses Web Speech API in the browser build. **✓** | edge-tts + faster-whisper; `/voice/status` reports readiness *and the reason* (`last_stream_error`, network probe, model absent); `/health.voice` compact form; audio frames bound to the turn that produced them on both clients. **✓** | The reference's capability probe is the weaker version of what Zaytoon now does — an import check cannot tell "no network" from "works". **Not** copied. The bytes-returning `synthesize()` is the cleaner API shape (below). |
| **Agent** | Router (regex `classify_task`) → Planner (deterministic split on `\n; then`) → Executor with retries; cancel events per task; hard per-turn timeout via `asyncio.wait`; `sanitize_tool_output()` wraps tool output in an untrusted-data fence and neutralises `system:`/`assistant:`/`user:` role markers. **✓** | `AgentEngine` bounded ReAct loop (iteration/depth caps, heartbeats, metrics, per-call audit), `Orchestrator` owning turn lifecycle + task records, intent routing, parallel safe calls with barriers, tree-wide cancellation. `render_observation()` labels sandbox refusals, declined approvals and **denied permissions** distinctly. **✓** | Zaytoon's loop is far stronger; the reference's **uniform untrusted-output fence** is the one idea worth having and is listed as deferred (Zaytoon fences inside `fetch_url` only — `tools/fetch.py` docstring). |
| **Models** | `RuntimeModeManager.decide()` returns `{mode, provider_id, model_id, reason}`; the reason is persisted **on the assistant message row** and sent on the `done` frame; `Chat.tsx` renders provider/model pills plus `routingReason` per bubble. **✓** | Registry-based routing with capability matching + free-only policy for OpenRouter (`assert_free_model`); `model.started/completed/failed` published per **attempt**; those events now carry the turn's `trace_id`; the browser stamps the model onto the turn's bubble. **✓** | Same idea, stronger join. **Gap:** the reference persists provenance with the message; Zaytoon stamps the live bubble — reload loses it. Listed as deferred (below). |
| **Skills** | No skill system. Tools are a static `TOOL_DEFS` list in `tools.py`. **✓** | `core/skills/`: manifests with schema validation, declared permissions checked against `required_for`/`gaps_for`, `SKILL_STRICT_PERMISSIONS`, trust levels, atomic install/update, run history, static validation that never executes untrusted code. Shipped skills under `skills/`. **✓** | No comparison to make; Zaytoon is the only one of the two with this. |
| **Memory** | `storage.py`: async SQLite with conversations, messages + **FTS**, memory rows, tasks, tool_logs, reminders, audit_log, preferences. The UI's "Remember this" flow is explicit (nothing saved automatically). **✓** | `memory/` (short-term + preferences) + `agents/memory_agent.py` + `core/db.py` schema for tasks/projects/providers/skills. **✓** | Comparable. The reference's FTS index on messages is a genuine improvement over Zaytoon's plain storage; deferred. |
| **Tasks** | Tasks exist as rows + `AgentTaskRun`/`AgentStep` dataclasses; no lifecycle validation, no persisted step log, no file-change record. **✓** | `core/tasks.py` with validated transitions (`can_transition_to` raises), steps, log lines, files changed, tools used, provider/model, `TaskGraph` DAG with concurrency + cycle detection at construction. **✓** | Zaytoon is strictly stronger. |
| **Tools** | 11 tools, one `ToolRegistry.execute(tool_id, args, approved)`; gate → confirmation → handler; audit-logged; approved-workspace path checks; approved-app allowlist (never extended by a model). `ToolResult.verified` is set by **reading the file back** after a write. **✓** | 36 tools, one registry: `validate → gates → execute`, sandbox with unconditional blocks, tier gate, permission-decision gate, sticky tier floors for third-party tools, audit hook, `mutates_files` declarations. `ToolResult.verified` **adopted this session** — `file_write` now re-reads and compares, and reports failure when the bytes did not land. **✓** | The reference's verification idea was adopted (the one place its tools layer was better); everything else Zaytoon already had. |
| **Security** | `security.py`: keyring → Fernet fallback for keys; `PermissionGate` = per-tool boolean switches + binary allowlist + workspace path prefix check; `requires_confirmation` for 7 tools. No risk tiers, no sandbox, no token binding, no origin check on the socket. **✓** | Named permission vocabulary (13) **with decisions now enforced at the tool gate**; `RiskAssessor` tiers reconciled with declared tiers; `tools/sandbox.py` unconditional blocks (shell injection, path escape); `core/security/scope.py` authorisation records with TTL/revoke enforced in the execution layer; confirmation tokens bound to request+action; audit log. **✓** | Zaytoon is strictly stronger on every axis except key storage, where they are equivalent in intent (DPAPI/0600 vs keyring/Fernet). |
| **Performance** | No measurements published; the reference's own README says the backend was never run. **n/e** | No performance number is claimed anywhere in this report or the final one except measurements taken with a stated method. | **Not comparable — marked unverified on both sides.** Any comparison here would be fiction. |
| **Frontend** | React 18 + TS + Tailwind + GSAP; 12 pages, setup wizard, context-based state, localStorage persistence, direct provider calls. **✓** | React 18 + Vite + Tailwind; HUD design system; views for chat, workspace (editor/terminal/diff), models, skills, security, VMs, tasks; **talks to the real backend** over REST + WS. | Different products: the reference is a richer *shell*, Zaytoon is a real client. Reference's Setup-Wizard-on-first-launch is worth having; deferred. |
| **UI/UX (RTL/i18n)** | `src/i18n/strings.ts` (7.7 KB) + `I18nContext.tsx`: `t(key)`, `dir`, writes `documentElement.lang/dir`. Full RTL mirroring claimed. **✓** | `src/i18n/strings.js` + context + provider (built this session); `App.jsx` owns `documentElement.lang/dir`; per-message script detection for mixed-language content (`isArabic()` in `ChatMessage`); **597 inline bilingual ternaries remain** across 31 components, with a ratchet test that stops the number growing. **✓** | Mechanism now matches the reference; **coverage does not yet** — the migration is the largest deferred item, with counts. |
| **Desktop** | `desktop/app.py`: pywebview shell that boots the backend in a thread, waits for `/api/health`, opens the window, shuts down cleanly. Never executed. **✓** | `desktop/app/`: pywebview shell + **Tk-independent protocol/client/state modules**, tray, hotkeys, audio playback, backend supervisor; its own test suite (370 passed / 2 skipped here) including a vocabulary-drift test against the backend. | Zaytoon's shell is testable without a display, which is why its tests run in CI; the reference's is one file that has never been run. |
| **Testing** | `backend/tests/` described as "pytest suite covering routing/fallback policy, … using a `MockProvider`" — **never run**. Frontend has no test runner configured. **✓** | Backend **3145 passed, 5 skipped**; frontend **704 passed** + lint + build (`npm run verify` EXIT 0); desktop **370 passed, 2 skipped**; protocol suite 63 tests/12 classes; permission-enforcement suite 13; routing-events 5. | Not comparable. The reference's "tests exist" is exactly the claim this project's rules forbid counting. |
| **Reliability** | Timeouts + retries per turn; no backpressure, no reconnection story, no dedupe, no cancellation of in-flight tool work. **✓** | Cancellation reaches the child turn task; disconnect cleanup; backpressure/size limits; dedupe; replay-safe writes; error taxonomy on the wire (`blocked`/`denied`/`cancelled`/`requires_confirmation`); honest `verified` flags. | Zaytoon stronger; the reference's `verified` idea is the transferable one and has been taken. |
| **Documentation** | README.md 15.8 KB, candid about what is unverified; no architecture doc beyond it. **✓** | `README.md`, `docs/ARCHITECTURE.md` (updated this session with the permission, turn-identity and protocol contracts), `docs/DESIGN_SYSTEM.md`, `docs/PROVIDERS.md`, `docs/PHASE0_BASELINE.md`, `AGENTS.md`, audit reports. | Both honest. The reference's README sets the standard for status disclosure; Zaytoon's docs now carry the same discipline per contract. |

---

## 2. What was adopted, and what was deliberately not

**Adopted (with the reason it is better in Zaytoon's shape):**

1. **Routing provenance per answer.** The reference attaches `{provider, model,
   reason}` to the turn's `done` frame and persists it with the message. Zaytoon
   already published per-attempt routing events; the missing piece was a join
   key, so the events now carry the turn's `trace_id` and the client stamps the
   bubble that trace names. A late attempt from a replaced turn cannot relabel
   the answer on screen — the reference's version has no equivalent guard
   because it has no superseded turns.
2. **`ToolResult.verified`.** A write verified by reading the file back, so
   "Wrote x" is an observation rather than the absence of an exception. Taken
   because Zaytoon's own system prompt already promises "never claim an action
   unless an observation confirms it" — the tool layer was the place that
   promise was unpaid.
3. **Every decision the user can make, visible where it acts.** The reference's
   Tools & Permissions page pairs each switch with what it governs. Zaytoon's
   permission *decisions* existed with no UI at all; the Security Workspace now
   renders them from the same endpoint the gate reads, and says which decision
   refuses and which merely records.
4. **A translation table.** `t(key)` over a two-language dictionary instead of a
   ternary per string.

**Considered and rejected, with the reason:**

| Idea | Why not |
|---|---|
| Web Speech API as the voice path | The reference's own README calls it "a stand-in". The product's requirement is the backend pipeline (edge-tts/faster-whisper); a browser stand-in would be exactly the "simulated capability" this project forbids. |
| Browser-side provider calls + `localStorage` persistence | That is the reference's architecture *because* its backend was never wired. Zaytoon's backend is the authority for routing, credentials and permissions; moving any of it into the browser would remove an authorisation boundary. |
| Offline mock provider | A mock that answers in production is indistinguishable from a working model to everyone except the person reading the badge. Zaytoon reports an error instead. |
| Simulated CPU/RAM gauges | Explicitly forbidden by the mission; Zaytoon's telemetry is read from the host or shows `—`. |
| Boolean per-tool permission switches | The reference's gate is a flat on/off per tool. Zaytoon's named-permission vocabulary is strictly more expressive (a capability can be revoked once for every tool that needs it), and per-tool booleans cannot express `ask`-vs-`allow`. |
| `keyring`/Fernet key storage | Equivalent in intent; Zaytoon already uses DPAPI on Windows and a 0600 file elsewhere, with the limitation documented. Swapping in Fernet would add a dependency for no security gain on the target platform. |

---

## 3. Deferred from this comparison (with what "deferred" means)

These are real gaps between the two projects' best ideas, recorded so that
"adopted" is not read as "everything":

| Item | Source | Size / evidence |
|---|---|---|
| **Centralized-translation coverage** | The reference has ~100 % of its strings behind `t(key)`; Zaytoon has the mechanism and one migrated surface. | **597** inline bilingual ternaries in 31 files (`src/components/ComputerView.jsx` 49, `SettingsPanel.jsx` 48, `workspace/FileExplorer.jsx` 39, `workspace/GitPanel.jsx` 37, `HUD/SystemStatus.jsx` 33, …). A ratchet test prevents growth; the migration itself is mechanical and unbounded here. |
| **Uniform untrusted-output fence in the agent loop** | `agents.py::sanitize_tool_output` | Zaytoon fences inside `fetch_url` only. A loop-level fence would also cover `file_read` of a hostile repository. Deferred because a blanket wrapper changes every observation the model sees and needs its own prompt-diff testing. |
| **Route provenance persisted with the message** | `storage.py::add_message(provider_id, model_id, routing_reason)` | Zaytoon's store has no provider/model columns on messages, so the badge is live-only. Needs a migration + the client reading it back. |
| **FTS index over message history** | `storage.py` | A schema change with a migration; low risk, no blocker. |
| **Setup wizard on first launch** | `README.md` "Setup Wizard on first launch" | Product work, not correctness. |
| **REST transcribe/speak endpoints returning bytes** | `voice.py::synthesize() -> bytes`, `api.py` `/api/voice/transcribe` | Zaytoon streams audio over the socket; an HTTP path would help a client that cannot stream. Not implemented, not claimed. |

---

## 4. The one-line summary

The reference is a better-looking product with an unexecuted backend; Zaytoon is
a verified backend with a thinner shell. Everything the reference does better
that is *transferable* — routing provenance, verified effects, a permission
screen, a translation table — has been taken and adapted to a system that can
prove it works; everything it does that depends on not having a backend
(browser-side providers, mocks, simulated telemetry, a Web Speech stand-in) was
rejected with that reason written down.
