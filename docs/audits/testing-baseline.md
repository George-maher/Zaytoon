# Zaytoon — Testing Baseline

**Date:** 2026-10-28
**Branch:** `arena/9219dec0-zaytoon` (HEAD `5bda335`)
**Environment:** Linux, Python 3.11.2, Node 22.22.3, npm 10.9.8
**No Ollama daemon, no OpenRouter key, no display server, no WebView2 runtime.** Live model behavior, desktop packaging, and live network tests are not claimed.

---

## 1. Backend test suite

### Command
```bash
cd /home/user/Zaytoon/backend
python -m pytest tests/ -q
```
(Uses `pytest.ini`: `asyncio_mode = auto`, `testpaths = tests`, `slow` marker for network tests skipped when offline.)

### Virtualenv setup
```bash
python -m venv /tmp/zaytoon-venv
/tmp/zaytoon-venv/bin/pip install --quiet -r backend/requirements.txt -r backend/requirements-dev.txt
```

### Result (after fixes)
```
3486 passed, 7 skipped, 0 failed  (~176s)
```

### Before fixes (baseline)
```
3484 passed, 4 failed, 5 skipped
```

### Fixes applied
1. `tests/test_discovery_sources.py` — `pytest.mark.skipif(sys.platform != "win32")` on two Windows-specific path tests (`_clean_registry_path` env-var expansion, `_is_windows_dir`). `os.path.expandvars` does not expand `%VAR%` on Linux; `os.path.realpath("C:\\Windows")` is not a Windows dir on Linux.
2. `tests/test_voice_session.py` — `test_voice_session.py::_when_speaking` changed `await asyncio.sleep(0.01)` → `await asyncio.sleep(0)`. The 10ms poll interval was too coarse to reliably catch the brief SPEAKING state window under this event loop, causing `test_barge_in_is_the_explicit_cancel_frame` and `test_audio_arriving_while_speaking_is_refused` to time out.

### Skipped tests (7)
- 2 Windows path tests (now correctly skipped on Linux)
- 5 pre-existing skips (network-dependent `slow` marker tests, git-absent tests, etc.)

### Coverage notes
- Coverage not measured (no `pytest-cov` installed; not optimizing for percentage).
- Critical paths with tests: tool registry, safety sandbox, agent engine bounds, parallel tools, request dedupe, WebSocket confirmation/interrupt, orchestrator task lifecycle, core paths/tasks/skills/providers/workspace/git, API routes, free-model routing, voice routing, scheduler (92 regression tests).
- Tests assert contracts, not implementations (per project convention).
- `tests/_sqlite_sweeper.py` stops lingering `aiosqlite` worker threads at session finish (prevents hangs at summary time).
- No pre-existing failures remain.

---

## 2. Frontend test suite

### Command
```bash
cd /home/user/Zaytoon/frontend
npm run test        # vitest run
npm run lint        # eslint .
npm run build       # vite build
```

### Result
```
Tests:  752 passed, 0 failed (44 files, ~49s)
Lint:   clean (0 warnings/errors)
Build:  successful (~1.9s; one chunk-size warning >500 kB)
```

### Test configuration
- `vitest.config.js`: jsdom environment, `http://localhost:5173/` URL, globals, `./src/test/setup.js`, includes `src/**/*.test.{js,jsx}`, `restoreMocks: true`
- `eslint.config.js`: eslint 9 flat config with React plugins

### Coverage notes
- No coverage tool configured. 752 tests across components, hooks, lib modules, and design tests.
- Highest-value test: `useWebSocket.test.js` (protocol reducer shared by browser and desktop).
- `imports.case.test.js` (120 tests) enforces exact import casing for Windows/Linux portability.

---

## 3. Desktop test suite

### Command
```bash
cd /home/user/Zaytoon/desktop
python -m pytest tests/ -q
```
(Requires `desktop/requirements.txt` + `desktop/requirements-dev.txt`: pywebview, websockets, pytest, customtkinter, keyboard, numpy, sounddevice, av.)

### Result (after fixes)
```
404 passed, 2 skipped, 0 failed  (~3s)
```

### Before fixes (baseline)
```
403 passed, 1 failed, 2 skipped
```

### Fix applied
`desktop/app/protocol.py` — added `VOICE_STATE = "voice_state"` to the `EventType` enum. The backend emits `voice_state` frames (outbound WS spec) but the desktop protocol enum lacked it, so frames arrived as `unknown` and were dropped. Fixed by `tests/test_wire_protocol.py::TestVocabularyDoesNotDrift::test_every_backend_frame_is_nameable`.

### Skipped tests (2)
- Pre-existing skips (platform-specific legacy module tests)

---

## 4. End-to-end / live tests not executed

| Test | Reason |
|------|--------|
| `scripts/verify_final.py` | Requires live Ollama or OpenRouter; unavailable |
| `scripts/verify_providers_live.py` | Requires live OpenRouter key; unavailable |
| `scripts/e2e_scenarios.py` | Requires live backend with model; unavailable |
| `scripts/smoke_desktop.py` | Requires WebView2 runtime + display server; unavailable |
| `scripts/smoke_confirmation.py` | Requires live WebSocket backend; unavailable |
| `test_fetch_url.py` live pair (`ZAYTOON_LIVE_NET=1`) | No outbound network to arbitrary hosts in this environment |
| Desktop PyInstaller build (`build_exe.py`) | Requires Inno Setup + WebView2; unavailable |

These are environmental limitations, not code defects. The unit/integration suites provide contract-level verification without live models.

---

## 5. Test quality observations

### Strengths
- Tests assert contracts, not implementations (e.g., tool registry tested against the real 36-tool registry, not doubles)
- Real git repositories in temp workspace for `test_core_git.py` (skipped, not failed, when git absent)
- SQLite `database is locked` regression (audit vs memory sharing one file) prevented by test asserting three separate DB files
- `_sqlite_sweeper.py` prevents `aiosqlite` worker-thread hangs at pytest summary
- `test_no_read_path_returns_the_saved_key` ensures API keys are write-only
- `TestErrorShapeIsUnified` pins `{"error": str}` shape across all failure classes
- Windows path tests now correctly skip on Linux (after fix)

### Limitations
- No end-to-end tests against live models (environmental)
- No coverage measurement configured
- No frontend TypeScript/runtime type checking
- No Python type checker or formatter configured

---

## 6. Runtime verification matrix (real backend, port 8000)

**Date:** 2026-10-28
**Environment:** Linux, Python 3.11.2; backend started with `cd backend && /tmp/zaytoon-venv/bin/python main.py`
**Prerequisites:** backend venv with `requirements.txt` + `requirements-dev.txt`; **no Ollama daemon, no OpenRouter key, no display server, no WebView2 runtime.**

| # | Workflow | Command / method | Result | Evidence |
|---|----------|-----------------|--------|----------|
| R1 | Backend startup + readiness | `python main.py`, then `GET /health` | PASS | 200; `tools_loaded: 52`; `llm.slots.limit: 2`; `llm.routing` present |
| R2 | Device discovery at startup | `python main.py` logs + `GET /device/discovery-status` | PASS | "Initial device inventory saved with 0 applications, 0 projects"; scopes + device_id returned |
| R3 | Text chat over WebSocket | `ws://127.0.0.1:8000/ws` send `{type:"message"}` | PASS | Connected frame → thinking → task lifecycle (received→analyzing→planning→verifying→completed) → intent chat → message → done → listening |
| R4 | REST `/chat` fast path | `POST /chat {"message":"what time is it"}` | PASS | 200; reply "It's 2026-10-28 ..."; intent `tools` |
| R5 | Unavailable provider handling | No Ollama running; send message | PASS | Controlled bilingual error: "All model providers failed. Last error: Ollama is not reachable"; turn terminates done→listening (no crash, no stuck state) |
| R6 | TTS failure handling | Whisper/edge-tts unavailable | PASS | Error frame "no audio could be produced — the voice service is unavailable" → audio_end → turn completes |
| R7 | Graceful degradation | Missing Ollama, Whisper, edge-tts | PASS | Clear ERROR/WARNING log lines; backend still serves /health, /tools, /chat |
| R8 | Clean shutdown | SIGTERM / Ctrl+C | PASS | "Zaytoon AI backend shut down", "Application shutdown complete"; no orphaned processes |
| R9 | Lint + production build | `npm run lint && npm run build` (frontend) | PASS | Lint clean; build ~1.9s; dist/ contains index.html + assets |

### Blocked workflows (environmental — not code defects)

| # | Workflow | Blocker | Command needed when available |
|---|----------|---------|------------------------------|
| B1 | Voice lifecycle (wake word → STT → intent → TTS → standby) | No microphone, no faster-whisper model download, no edge-tts network | `scripts/verify_final.py`, `scripts/smoke_confirmation.py` |
| B2 | Desktop runtime (window, bridge, WebSocket events) | No WebView2 runtime, no display server | `python desktop/scripts/smoke_desktop.py` |
| B3 | Desktop packaging (PyInstaller onedir) | No Inno Setup, no WebView2 | `python desktop/scripts/build_exe.py` |
| B4 | Live model end-to-end (chat, tools, voice) | No Ollama daemon, no OpenRouter key | `scripts/e2e_scenarios.py`, `scripts/verify_providers_live.py` |
| B5 | `test_fetch_url.py` live pair | No outbound network to arbitrary hosts | `ZAYTOON_LIVE_NET=1 pytest tests/test_fetch_url.py` |
| B6 | Windows application discovery / safe launch | Running on Linux | Windows CI runner |

### Defects found and fixed during runtime verification

| # | Defect | Root cause | Fix | Files |
|---|--------|-----------|-----|-------|
| D1 | Device discovery crashed at startup with `RuntimeError: this event loop is already running` | `api/main.py` lifespan called `asyncio.get_event_loop().run_until_complete()` inside uvicorn's already-running event loop | `await` the coroutines directly (lifespan is async) | `backend/api/main.py` |
| D2 | Device discovery failed on Linux with `NotImplementedError` | `core/discovery.py` `_detect_provider()` returns base `OperatingSystemProvider()` on non-Windows; base methods raised `NotImplementedError` | Base provider returns empty/safe defaults (Windows behavior unchanged via `WindowsProvider` overrides) | `backend/core/discovery.py` |

---

## 7. Fixes applied to test scripts

| File | Change | Why |
|------|--------|-----|
| `backend/tests/test_voice_session.py` | `asyncio.sleep(0.01)` → `asyncio.sleep(0)` in `_when_speaking` | 10ms polling missed SPEAKING state; voice safety tests now pass reliably |
| `backend/tests/test_discovery_sources.py` | Added `skipif(sys.platform != "win32")` to 2 tests | Windows path semantics don't apply on Linux |
| `desktop/app/protocol.py` | Added `VOICE_STATE = "voice_state"` to `EventType` | Backend emits `voice_state` frames; desktop protocol must name them |

No test was weakened; all fixes make tests more correct or skip environmentally-inapplicable tests explicitly.
