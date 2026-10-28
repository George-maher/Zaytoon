# Zaytoon Windows installer

One artifact, two install modes, no dependencies on the target machine.

## What it is

`dist/Zaytoon-Setup-<version>.exe` — an Inno Setup 6 installer (~85 MB)
wrapping the PyInstaller **onedir** bundle at `desktop/dist/Zaytoon/`:

```
Zaytoon.exe               windowed shell (~19 MB) — also the backend worker
_internal/
  backend/                the backend source tree (filtered copy, no .venv/.env/data)
  frontend/               the production React build (vite output)
  zaytoon_bundle.json     marker: build time, python version, onedir/onefile
  …                       frozen third-party dependencies (fastapi, uvicorn,
                          faster-whisper, edge-tts, onnxruntime, …)
```

The installed app starts its own backend (`Zaytoon.exe --serve`, in-process
uvicorn). No checkout, no virtualenv, no system Python, no env var.

## Reproduce the whole chain

```bat
:: prerequisites (once)
winget install JRSoftware.InnoSetup          :: ISCC.exe, usually %LOCALAPPDATA%\Programs\Inno Setup 6\
backend\.venv\Scripts\pip install pyinstaller pillow

:: 1. icon (only after changing desktop/icons/source.png)
backend\.venv\Scripts\python.exe desktop\icons\make_icon.py

:: 2. bundle: npm ci + npm run build + filtered staging + PyInstaller + verify
backend\.venv\Scripts\python.exe desktop\scripts\build_exe.py
::    add --skip-frontend to reuse an existing frontend/dist
::    add --console for a debug shell with stdout

:: 3. installer: render installer\zaytoon.iss, compile with ISCC
backend\.venv\Scripts\python.exe scripts\create_iss.py --compile
```

Outputs:

- `desktop/dist/Zaytoon/` — the bundle (~300 MB)
- `installer/dist/Zaytoon-Setup-<version>.exe` — the installer (~85 MB)

Version comes from `desktop/app/__init__.py` — one constant, both artifacts.

## Verify before shipping

```bat
desktop\dist\Zaytoon\Zaytoon.exe --diagnose               :: verdict: ready (backend DOWN is a WARN, not a FAIL)
desktop\dist\Zaytoon\Zaytoon.exe --diagnose --start-backend  :: spawns the bundled backend, probes /health for real
```

`--diagnose` exits non-zero only on real problems (missing backend entry,
missing frontend build, unwritable state dirs, missing WebView2 runtime).
`--start-backend` additionally spawns `Zaytoon.exe --serve`, waits for
`GET /health` 200 and prints where the child's log went
(`%LOCALAPPDATA%\Zaytoon\logs\backend.log`).

Silent round trip (per-user):

```bat
Zaytoon-Setup-3.0.0.exe /VERYSILENT /CURRENTUSER /DIR="%TEMP%\zt"
"%TEMP%\zt\Zaytoon.exe" --diagnose
"%TEMP%\zt\unins000.exe" /VERYSILENT
```

Last verified 2026-10-21: install → `verdict: ready` → uninstall left 0 files,
0 registry entries, and `%LOCALAPPDATA%\Zaytoon` (the user's keys, workspace
and databases) untouched.

## Behaviour worth knowing

- **Per-user vs all-users** — one installer; Inno's `{autopf}` default plus
  `PrivilegesRequiredOverridesAllowed=dialog commandline` lets the user (or a
  silent `/CURRENTUSER` / `/ALLUSERS`) choose.
- **Upgrades** — same `AppId`, `UsePreviousAppDir=yes`, `CloseApplications=yes`:
  a new setup replaces the previous install in place and asks a running
  instance to close instead of failing on a locked exe.
- **Uninstall preserves user data** in `%LOCALAPPDATA%\Zaytoon` unless the user
  creates `delete-user-data.flag` in the install dir first.
- **WebView2** — detected in `[Code]` (EdgeUpdate `PV` key, then the fixed
  path; `0.0.0.0` counts as absent). Missing ⇒ the wizard offers the
  Microsoft download instead of failing after install.
- **Secrets never ship** — the staging step excludes `.env`, `secrets.json`,
  `.venv/`, `data/`, `*.pyc`, `*.db`, `*.log`, and `verify_bundle` fails the
  build if any reappear. Don't re-add a wholesale copy step.
