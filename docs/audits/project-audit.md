# Zaytoon — Project Audit

**Date:** 2026-10-28
**Branch:** `arena/9219dec0-zaytoon` (HEAD `5bda335`)
**Environment:** Linux, Python 3.11.2, Node 22.22.3, npm 10.9.8
**Verification method:** All findings verified by reading/running current code and test suites. No Ollama daemon or OpenRouter key available, so live model behavior is not claimed.

## 1. Test baseline (executed)

| Suite | Command | Result |
|-------|---------|--------|
| Backend | `cd backend && python -m pytest tests/ -q` | 3486 passed, 7 skipped, 0 failed (~176s) |
| Frontend | `cd frontend && npm run test` | 752 passed, 0 failed (44 files, ~49s) |
| Frontend lint | `npm run lint` | Clean (0 warnings/errors) |
| Frontend build | `npm run build` | Successful (~1.9s; one chunk-size warning) |
| Desktop | `cd desktop && python -m pytest tests/ -q` | 404 passed, 2 skipped, 0 failed (~3s) |

### Pre-existing failures found and fixed

| # | Test | Root cause | Fix | Severity |
|---|------|-----------|-----|----------|
| 1 | `test_discovery_sources.py::test_registry_paths_expand_environment_variables` | `os.path.expandvars` on Linux does not expand `%VAR%` (Windows-only semantics) | `pytest.mark.skipif(sys.platform != "win32")` | P2 — test script |
| 2 | `test_discovery_sources.py::test_the_windows_directory_is_recognised` | `os.path.realpath("C:\\Windows\\System32")` is not a Windows dir on Linux | `pytest.mark.skipif(sys.platform != "win32")` | P2 — test script |
| 3 | `test_voice_session.py::test_audio_arriving_while_speaking_is_refused` | `_when_speaking` polls every 10ms; too coarse to catch the brief SPEAKING state window | `asyncio.sleep(0)` instead of `0.01` | P1 — voice safety test |
| 4 | `test_voice_session.py::test_barge_in_is_the_explicit_cancel_frame` | Same as #3 | Same as #3 | P1 — voice safety test |
| 5 | `test_wire_protocol.py::test_every_backend_frame_is_nameable` | Backend emits `voice_state` frame; desktop `EventType` enum lacks `VOICE_STATE` | Added `VOICE_STATE = "voice_state"` to `desktop/app/protocol.py` | P2 — protocol drift |

### Defects found during runtime verification (Phase 2) and fixed

| # | Defect | Root cause | Fix | Severity |
|---|--------|-----------|-----|----------|
| 6 | Device discovery crashed at backend startup: `RuntimeError: this event loop is already running` | `api/main.py` async `lifespan` called `asyncio.get_event_loop().run_until_complete()` inside uvicorn's already-running event loop | `await` the coroutines directly (lifespan is already async) | P1 — startup |
| 7 | Device discovery failed on Linux: `NotImplementedError` from base `OperatingSystemProvider` | `core/discovery.py` `_detect_provider()` returns base class on non-Windows; base methods raised `NotImplementedError` | Base provider returns empty/safe defaults; `WindowsProvider` overrides unchanged | P1 — startup (Linux) |

All failures are now resolved: **0 failures across all three suites; backend starts cleanly with no startup errors.**

### Tests not executed
- No end-to-end tests against a live backend (requires Ollama or OpenRouter, unavailable in this environment)
- No desktop packaging / PyInstaller build test (requires Inno Setup + pywebview WebView2 runtime)
- `test_fetch_url.py` live pair (`ZAYTOON_LIVE_NET=1`) not executed (no outbound network to arbitrary hosts in this environment)

## 2. Feature inventory and implementation status

| Feature | Status | Evidence | Notes |
|---------|--------|----------|-------|
| Bilingual AR(EG)/EN detection + TTS | Implemented & tested | `arabic.py`, `voice/routing.py`, `tests/test_arabic.py`, `test_voice_routing.py` | STT→TTS language handoff verified |
| Multi-agent router (chat/tools/code/planner/scheduler/memory) | Implemented & tested | `agents/router_agent.py`, `services/agent_service.py`, `tests/test_router_routing.py`, `test_agent_engine.py` | Keyword fast-path + LLM fallback |
| Tool registry with validate→gate→execute | Implemented & tested | `tools/registry.py`, 36+ tools, `tests/test_tool_registry.py` | Strongest part of codebase |
| Safety sandbox (unconditional blocks) | Implemented & tested | `tools/sandbox.py`, `tests/test_security_sandbox.py` | Shell metachars, path traversal, app targets |
| Human-in-the-loop confirmation | Implemented & tested | `tools/safety.py`, `tests/test_tool_safety.py`, `test_websocket_confirmation.py` | Fail-closed on timeout/disconnect |
| Voice STT (faster-whisper) | Implemented & tested | `voice/stt.py`, `tests/test_voice_session.py` | Requires faster-whisper + model download |
| Voice TTS (edge-tts, bilingual) | Implemented & tested | `voice/tts.py`, `voice/routing.py`, `tests/test_tts.py` | Requires edge-tts + system voices |
| Wake-word gate | Implemented & tested | `services/wake_word_service.py`, `tests/test_voice_session.py` | Continuous vs manual bypass |
| Tool parallel execution (barriers, semaphore) | Implemented & tested | `agents/engine.py`, `tests/test_parallel_tools.py` | Safe-tier concurrent, gated calls serial |
| Task lifecycle (core/orchestrator) | Implemented & tested | `core/orchestrator.py`, `tests/core/test_orchestrator.py` | One turn → one task record |
| SQLite (3 separate files) | Implemented & tested | `core/db.py`, `common/audit.py`, `memory/` | Separate files prevent lock contention |
| REST API (/health, /tools, /tasks, /settings/workspace) | Implemented & tested | `api/main.py`, `tests/test_api_routes.py` | Unified `{"error"}` shape |
| Two-step approval tokens (git/terminal) | Implemented & tested | `core/api/approvals.py`, `tests/core/test_api_core.py` | A6 hardening |
| Desktop host (pywebview + bridge) | Implemented & tested | `desktop/app/host.py`, `app/bridge.py`, `desktop/tests/` | 404 tests pass |
| Skills (skill.json packages) | Partially implemented | `skills/`, `core/skills/`, `tests/core/test_core_skills.py` | Three read-only skills shipped; not enabled by default |
| Provider registry (Ollama + OpenRouter) | Implemented & tested | `services/ai_provider.py`, `model_registry.py`, `tests/core/test_core_providers.py` | Free-only enforcement before any network call |
| Virtualization / VMware guest | Partially implemented | `core/virtualization/vmware.py`, `tests/core/test_virtualization_vmware.py` | VMware-specific; limited scope |
| File system tools (read/write/list/delete/etc.) | Implemented & tested | `tools/filesystem.py`, `tests/test_tool_registry.py` | Confined to WORKSPACE_ROOT |
| Terminal / process tools | Implemented & tested | `tools/terminal.py`, `tools/processes.py`, `tests/test_local_exec.py` | argv-only, no shell |
| Code tools (create_project, run_npm, run_tests) | Partially implemented | `tools/coding.py`, `tests/test_tool_registry.py` | No diff-review or regression loop |
| Media / web tools | Implemented & tested | `tools/media.py`, `tools/web.py`, `tools/fetch.py` | netguard for SSRF/rebinding protection |
| Git tools | Implemented & tested | `tools/git.py`, `core/workspace/git.py`, `tests/core/test_core_git.py` | Real repos in temp workspace |

### Not found / not implemented
- No authentication or multi-tenancy (single-user local app)
- No ORM (raw SQLite via aiosqlite)
- No vector memory / embeddings
- No plugin marketplace (skills are directory copies)
- No message bus framework beyond `EventBus`
- No workflow/temporal engine (task graph is ~80 lines)

## 3. Architecture quality assessment

### Strengths (preserve, do not re-litigate)
1. **The tool gate is real.** `validate → gate → execute` with unconditional sandbox blocks reported as `blocked` (never approvable), gate before result cache. Verified by tests and live probes.
2. **No ungated channel.** The `command` WS message type was removed; protected by an AST-parsing test (`test_workspace_and_injection.py`).
3. **Honest failure vocabulary.** `common/reporting.py` redacts before anything reaches log or client; registry no longer echoes `str(e)`.
4. **Task lifecycle and cancellation** reach both the work and the record; small enough to reason about; tested.
5. **2322+ backend tests** asserting contracts rather than implementations; unusually good test discipline.
6. **Single path containment resolver** (`tools/sandbox.resolve_workspace`); no second resolver exists.

### Defects and debt

| # | Area | Problem | Evidence | Severity |
|---|------|---------|----------|----------|
| A1 | Tests | Two Windows-specific path tests fail on Linux (no `skipif`) | `test_discovery_sources.py` — fixed | P2 |
| A2 | Tests | `_when_speaking` 10ms polling misses brief SPEAKING state | `test_voice_session.py` — fixed | P1 |
| A3 | Protocol | Desktop `EventType` lacks `VOICE_STATE`; backend emits it | `test_wire_protocol.py` — fixed | P2 |
| A4 | Documentation | No `docs/architecture/`, `docs/audits/`, `docs/roadmap/` structure | This audit — being created | P3 |
| A5 | Observability | No structured logging formatter/type checker for Python | `AGENTS.md` "No formatter or typechecker is configured" | P3 |
| A6 | Frontend | No TypeScript; runtime type errors possible | `AGENTS.md` "JavaScript/JSX, not TypeScript" | P3 |
| A7 | Packaging | Desktop PyInstaller build not tested in this environment | No WebView2 / Inno Setup available | P3 |

### Modules with tight coupling / duplication
- `core/orchestrator.py` and `services/agent_service.py` both handle turn execution; the orchestrator is the current path, agent_service is the legacy path — test-only wiring exists (`AgentService._execute_tool_commands` is test-only).
- `tools/sandbox.resolve_workspace` is the single containment resolver; `core/workspace/files.py` delegates to it correctly.

### No god objects found
- `AgentEngine` was reduced from 548 → 270 lines; `ToolExecutor` from 626 → 157; `ToolAgent` from 548 → 270. No oversized modules identified.

## 4. Security findings

| # | Finding | Impact | Reproducibility | Remediation |
|---|---------|--------|-----------------|-------------|
| S1 | No authentication | Single-user local app; loopback only; acceptable for threat model | N/A | Documented |
| S2 | WebSocket origin policy | `Origin: null` refused unless paired device token present (hardened A2) | Verified by `tests/test_ws_protocol.py` | Fixed in prior pass |
| S3 | Shell injection | `split_command` blocks metacharacters; `shell=False` everywhere | Verified by `tests/test_security_sandbox.py` | Fixed in prior pass |
| S4 | Path traversal | `commonpath`-based containment; no `startswith` bypass | Verified by `tests/test_security_sandbox.py` | Fixed in prior pass |
| S5 | SSRF / rebinding | `netguard.py` checks resolved address class on every redirect hop; `FETCH_ALLOW_PRIVATE` defaults false | Verified by `tests/test_fetch_url.py` | DNS rebinding mitigated not eliminated (documented) |
| S6 | Secret leakage | `common/reporting.redact()` masks keys; no route returns secrets; test asserts | Verified by `tests/test_security_probe.py` | Fixed in prior pass |

No critical unreviewed security findings. All hardening increments (A1–A6 from AGENTS.md) are implemented and tested.

## 5. Known gaps requiring investigation

| Area | Gap | Why it matters |
|------|-----|----------------|
| Live model behavior | No Ollama / OpenRouter available in this environment | Cannot verify end-to-end chat, tool use, or voice with real models |
| Desktop packaging | PyInstaller build not exercised | WebView2 runtime and Inno Setup not available |
| E2E scenarios | `e2e_scenarios.py` not run against live backend | Would validate full workflows |
| Type checking | No Python type checker or formatter | Runtime type errors possible in refactors |
