# Zaytoon — Engineering Roadmap

**Date:** 2026-10-28
**Branch:** `arena/9219dec0-zaytoon` (HEAD `5bda335`)
**Baseline:** All test suites green — Backend 3486 passed / 7 skipped / 0 failed; Frontend 756 passed / 0 failed / lint clean / build successful; Desktop 404 passed / 2 skipped / 0 failed.

## Execution order (adapted to findings)

1. ✅ Reproducible builds and a verified baseline — **DONE** (all three suites green)
2. ✅ Critical security, data-integrity, and startup defects — **DONE** (startup defects D1/D2 found and fixed during runtime verification; all hardening increments from AGENTS.md implemented and tested)
3. ✅ Regression tests for core user workflows — **DONE** (voice barge-in/echo tests fixed; Windows path tests corrected; protocol drift fixed)
4. High-impact architecture and lifecycle fixes
5. Design-system consistency and major usability defects
6. Incremental refactoring of highest-risk modules
7. Additional feature development
8. Lower-priority polish and optimization

---

## Prioritized backlog

### P0 — Critical (security, data integrity, startup)

| ID | Issue | Evidence | Effort | Risk | Modules | Verification | Acceptance |
|----|-------|----------|--------|------|---------|--------------|------------|
| — | None identified | All hardening increments (A1–A6) implemented and tested | — | — | — | — | — |

No P0 items remain. Security hardening (shell injection, path traversal, SSRF/rebinding, secret redaction, origin policy, free-only model enforcement, fail-closed confirmations) is implemented and tested.

---

### P1 — Major functional / architectural defects

| ID | Issue | Evidence | Effort | Risk | Modules | Verification | Acceptance |
|----|-------|----------|--------|------|---------|--------------|------------|
| P1-1 | Voice safety tests timing-dependent (`_when_speaking` 10ms poll missed SPEAKING) | `test_voice_session.py` — 2 tests consistently failing | Small | Low | `tests/test_voice_session.py` | Fixed; tests pass deterministically | Voice safety tests pass |
| P1-2 | Device discovery crashed at backend startup (`RuntimeError: this event loop is already running`) | `api/main.py` lifespan used `run_until_complete` inside uvicorn's running event loop | Small | Low | `backend/api/main.py` | Backend starts cleanly; no `startup.discovery` error | `/health` 200, no startup errors |
| P1-3 | Device discovery failed on Linux (`NotImplementedError` from base `OperatingSystemProvider`) | `core/discovery.py` base provider raised `NotImplementedError` on non-Windows | Small | Low | `backend/core/discovery.py` | Backend starts cleanly on Linux; onboarding completes | `/device/discovery-status` 200 |
| P1-4 | End-to-end verification against live models not possible in this environment | No Ollama / OpenRouter available | — | High | All | `scripts/verify_final.py`, `scripts/e2e_scenarios.py` run green when model available | 9/9 e2e scenarios pass with live model |

P1-1, P1-2, P1-3 are **DONE** (fixed in this pass). P1-4 is environmental — cannot be resolved without a model provider.

---

### P2 — Important maintainability / usability / coverage

| ID | Issue | Evidence | Effort | Risk | Modules | Verification | Acceptance |
|----|-------|----------|--------|------|---------|--------------|------------|
| P2-1 | Windows path tests fail on Linux (no `skipif`) | `test_discovery_sources.py` | Small | Low | `tests/test_discovery_sources.py` | Fixed; tests skip on Linux | Windows-specific tests skip on non-Windows |
| P2-2 | Desktop protocol missing `voice_state` frame name | `test_wire_protocol.py` — `voice_state` arrived as `unknown` | Small | Low | `desktop/app/protocol.py` | Fixed; `TestVocabularyDoesNotDrift` passes | Desktop protocol names all backend outbound frames |
| P2-3 | No Python type checker or formatter configured | `AGENTS.md`: "No formatter or typechecker is configured for Python" | Medium | Medium | `backend/` | Add `mypy`/`ruff` to CI; `mypy backend/` clean | Type errors caught in CI |
| P2-4 | No frontend TypeScript | `AGENTS.md`: "JavaScript/JSX, not TypeScript" | Large | High | `frontend/` | `tsc --noEmit` clean | Runtime type errors reduced |
| P2-5 | No coverage measurement | No `pytest-cov` / frontend coverage tool | Small | Low | `backend/`, `frontend/` | Coverage report generated | Critical paths have measured coverage |
| P2-6 | Empty/loading/error states inconsistent across components | Design audit D1 | Medium | Low | `frontend/src/components/*` | **Pilot adopted** — `EmptyState.jsx` created; adopted in `ExecutionHistory.jsx` for both empty states (no records, no processes). Loading/error/unavailable remain distinct displays. SecurityView/GitPanel/VirtualMachinesView inspected — retained existing patterns (state semantics don't fit EmptyState's generic model). | No raw errors shown to user |
| P2-7 | No shared color/token system | Design audit D2 | Medium | Low | `frontend/src/styles/` | **Resolved (Phase 3)** — `--zt-*` aliases defined in `theme.css` mapped to `--hud-*` values; `common.empty`/`common.error` i18n keys added | Visual consistency across components |

P2-1, P2-2 are **DONE** (fixed in this pass). P2-7 is **DONE** (Phase 3). P2-6 is in progress (shared component created).

---

### P3 — Lower-priority polish / optimization

| ID | Issue | Evidence | Effort | Risk | Modules | Verification | Acceptance |
|----|-------|----------|--------|------|---------|--------------|------------|
| P3-1 | Documentation structure (`docs/architecture/`, `docs/audits/`, `docs/roadmap/`) missing | This audit | Small | Low | `docs/` | Files exist and match current implementation | Docs verified against code |
| P3-2 | Desktop packaging not tested in this environment | No WebView2 / Inno Setup | — | High | `desktop/scripts/build_exe.py` | PyInstaller build produces working `.exe` | Packaging verified on target platform |
| P3-3 | Chunk-size warning in frontend build (>500 kB) | `npm run build` warning | Small | Low | `frontend/` | Build warning resolved | Code-split large chunks |

---

## Completed in this pass

| Fix | File | Impact |
|-----|------|--------|
| Voice safety polling | `backend/tests/test_voice_session.py` | 2 previously-failing tests pass deterministically |
| Windows path test skip | `backend/tests/test_discovery_sources.py` | 2 tests skip on Linux instead of failing |
| Protocol frame name | `desktop/app/protocol.py` | Desktop wire-protocol test passes |
| Startup `run_until_complete` → `await` | `backend/api/main.py` | Backend starts cleanly; no `startup.discovery` error |
| Discovery base provider defaults | `backend/core/discovery.py` | Discovery completes on Linux; no `NotImplementedError` |
| Shared empty-state component | `frontend/src/components/EmptyState.jsx` | Standard empty-state pattern using theme tokens; additive, no existing visuals changed |
| EmptyState pilot adoption | `frontend/src/components/ExecutionHistory.jsx` | EmptyState replaces inline empty-state rendering for no-records and no-processes states; loading/error/unavailable remain distinct displays |
| Common empty/error i18n keys | `frontend/src/i18n/strings.js` | `common.empty` / `common.error` added (en + ar) for shared empty/error messaging |

**Before:** Backend 3484 passed / 4 failed / 5 skipped; Frontend 752 passed / 0 failed; Desktop 403 passed / 1 failed / 2 skipped.
**After:** Backend 3486 passed / 7 skipped / 0 failed; Frontend 756 passed / 0 failed; Desktop 404 passed / 2 skipped / 0 failed.

### Runtime verification summary (Phase 2)

All feasible workflows have an explicit result:

| Workflow | Result | Blocker |
|----------|--------|---------|
| Backend startup + `/health` readiness | PASS | — |
| Device discovery at startup | PASS | — |
| Text chat over WebSocket | PASS | — |
| REST `/chat` fast path (get_time) | PASS | — |
| Unavailable provider handling | PASS | — |
| TTS failure handling | PASS | — |
| Clean shutdown | PASS | — |
| Frontend lint + production build | PASS | — |
| Voice lifecycle (mic + STT + TTS) | BLOCKED | No microphone / model download / network |
| Desktop runtime (window + bridge) | BLOCKED | No WebView2 / display server |
| Desktop packaging (PyInstaller) | BLOCKED | No Inno Setup / WebView2 |
| Live-model e2e scenarios | BLOCKED | No Ollama / OpenRouter key |
| `test_fetch_url.py` live pair | BLOCKED | No outbound network |
| Windows app discovery / launch | BLOCKED | Running on Linux |

---

## Next development phase

**Phase 3 — Design system audit & consistent interface.** D1 shared `EmptyState` component created; D2 token system resolved; D3 documented as intentional; D4 resolved. Phase 3.1 pilot-adopted `EmptyState` in `ExecutionHistory`. Phase 3.2 visual QA pipeline audited and hardened: Playwright test setup (`frontend/tests/visual-qa.spec.js`) rewritten with proper language handling (localStorage), API mocking for ExecutionHistory states, and all required scenarios (desktop/mobile × en/ar × empty/loading/error/unavailable/accessibility). CI workflow (`.github/workflows/ci-visual-qa.yml`) audited — minimal permissions, no dependency drift, proper failure detection and artifact upload. Visual QA remains **BLOCKED for browser execution** — no display server or browser in this environment, and Playwright Chromium cannot be downloaded (outbound network restricted). The reproducible setup is ready for a browser-capable environment. Remaining: run visual QA in browser-capable environment, then gradual rollout to SecurityView/GitPanel/VirtualMachinesView where semantics fit; optional loading/error variants of EmptyState.

**Phase 3.3 — Accessibility & UX Engineering (this pass).** WCAG 2.2 AA target. Fixed: ExecutionHistory loading/error/unavailable states now announce to screen readers via `role="status"`/`role="alert"` + `aria-live` (A1–A3); Sidebar collapsed nav items expose `aria-label` for icon-only buttons (A4); `EmptyState` renders as `role="region"` with accessible `aria-label` (A5–A6). Added 18 tests across `EmptyState.test.jsx`, `ExecutionHistory.test.jsx`, and `Sidebar.test.jsx`. Frontend: 774 passed / 0 failed (46 files); lint clean; build OK. Browser execution still BLOCKED (no display server or browser).

**Phase 4.0 — Personal AI Assistant Foundation (current).** Windows-first persistent
desktop AI assistant:

- ✅ Desktop lifecycle: minimize-to-tray + background behavior
  (`desktop/app/host.py`, `desktop/app/tray.py`, `ZAYTOON_BACKGROUND_MODE`)
- ✅ Wake-word gate verified (`backend/services/wake_word_service.py` — 16 tests pass)
- ✅ Local file/app indexing with roots/exclusions
  (`desktop/app/indexer.py`, `search_items`/`launch_item` bridge methods)
- ✅ Arc Reactor host state exposed via bridge (`get_host_state()`)
- ✅ Desktop tests: 425 passed / 2 skipped; lint clean; frontend 773 passed
- 📋 Google Drive / Gmail OAuth design documented
  (`docs/security/google-drive-gmail-oauth-design.md`)
- 📋 Audio wake-word detector: integration path documented (text gate exists;
  detector is the missing piece — see design notes)
- 📋 Local index + launch wired to frontend UI (bridge methods exist; consumer TBD)

**Phase 9 — Engineering standards.** Adopt only tools that solve demonstrated problems:

1. **Python:** Add `mypy` (type checking) and `ruff` (lint + format) to `backend/requirements-dev.txt`; add `mypy backend/` and `ruff check backend/` to CI. Do not add a formatter that churns existing code — `ruff format --check` first to gauge churn.
2. **Frontend:** ESLint is already clean. Consider `tsc --noEmit` on a subset of `frontend/src/` to evaluate TypeScript adoption cost before committing.
3. **CI:** Add coverage reporting (`pytest-cov` backend, `vitest --coverage` frontend) as informational, not a gate. Add the `ci-visual-qa.yml` workflow to run browser-based visual QA when a browser-capable runner is available.
4. **Pre-commit:** Optional; CI gates are sufficient for this team size.

**Do not:** full frontend redesign, TypeScript migration of the entire frontend, Python formatter that rewrites all files, database schema changes, or provider registry redesign — none are justified by current evidence.
