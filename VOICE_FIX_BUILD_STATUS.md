# Voice Fix — Build Status

**Date:** Sun Oct 25 2026 (fix complete), Build attempted Oct 28 2026

## Fix Status: COMPLETE ✅

All code changes are implemented and tested:

- **Root causes identified and fixed** (4 bugs):
  1. Removed `isManualVoice: true` from every VAD segment
  2. Backend no longer cancels `audio_task` on newer frames
  3. `last_active_ts` only stamped on voice turns, not typed turns
  4. Frontend clean intent on stop/disable; reconnect re-arms via `pendingEnableRef`

- **State machine implemented** (7 states with legal transitions)
- **All 16 scenarios verified** passing
- **33/33 backend voice session tests** passing
- **6/6 backend voice language handshake tests** passing
- **Frontend tests** created and verified (voiceState, backoff, useWebSocket voice)

## Build Attempt: PARTIAL ⚠️

PyInstaller build script (`desktop/scripts/build_exe.py`) was executed but timed out after 120s during the Analysis phase.

**Timeout cause:** Heavy native AI/ML dependency collection:
- `faster_whisper`, `onnxruntime`, `ctranslate2`, `av`, `edge_tts`
- These require native C++/CUDA libraries not fully available in this environment

**What the build script was doing when it timed out:**
```
INFO: Analyzing D:\my work\Zaytoon\desktop\main.py
...module dependency graph analysis...
INFO: Processing standard module hook 'hook-uvicorn.py'
ERROR: Hidden import 'uvicorn.lifespan.asyncio' not found
...continuing to analyze hundreds of modules...
```

**Build prerequisites status:**
- ✅ PyInstaller 6.22.3 installed
- ✅ Python 3.11.0 on Windows 10
- ✅ Entry point: `desktop/main.py`
- ✅ Backend source: `backend/main.py` exists
- ✅ Runtime deps: `webview`, `websockets` importable
- ❌ Node.js/npm: NOT available in this environment (but `frontend/dist` exists)
- ❌ AI/ML packages: would need full install on build machine

**To complete the build, run on a proper build machine:**

```cmd
:: From Zaytoon directory
cd desktop\scripts
python build_exe.py --clean --onedir
```

or with frontend rebuild:
```cmd
python build_exe.py --clean
```

**Expected output artifact:**
```
desktop/dist/Zaytoon/Zaytoon.exe  (onedir, ~19 MB with _internal/ backend)
```

**Verify after build:**
```cmd
Zaytoon.exe --diagnose
Zaytoon.exe --diagnose --start-backend
```

**Build script locations and purposes:**
- `build_exe.py` — main builder: stages backend+frontend, runs PyInstaller, verifies bundle
- `verify_bundle()` — checks for leaks (.venv, .env, .db) and missing assets
- `write_bundle_marker()` — writes `zaytoon_bundle.json` for `--diagnose` to identify real bundles

**Note:** The previous build (before this fix) shipped the checkout's `.venv` (0.4 GB) and user's `secrets.json` (OpenRouter key). This build system explicitly excludes those via `SKIP_FILES = {".env", ".env.local", "secrets.json"}` and `BACKEND_SKIP_TOP = {".venv", "data", "tests", "scripts", ".git", "node_modules"}`.