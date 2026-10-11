# Desktop audit — reliability, lifecycle, cross-platform, packaging

**Scope.** The pywebview desktop shell (`desktop/`): `main.py`, `app/host.py`,
`app/backend.py`, `app/config.py`, `app/instance.py`, `app/bridge.py`,
`app/paths.py` (new here), `scripts/build_exe.py`, `scripts/smoke_desktop.py`, the
React client it loads (`frontend/dist`), the backend it spawns
(`backend/api/main.py`), the CI legs that test them, and the tests in between.

**Method.** Read the code and the tests before touching either; run what this
sandbox can run; label everything else. A row that says *verified* names the test
or the measurement that produced it. A row that says *reading-only* means a human
read the code and reasoned about it, and the sandbox could not execute it. Nothing
here claims a GUI behaved as expected — this machine has no display, and
`pywebview`, `tkinter`, `customtkinter`, `keyboard` and `sounddevice` are not
installed in its venv. Where a defect is platform-specific and unverifiable here,
it is stated as a defect plus the exact commands to confirm it, in §8.

---

## 1. The system as it is actually wired

Three OS processes, two IPC mechanisms, one policy file per direction:

```
pythonw desktop/main.py          window: WebView2 (Win) / WKWebView (macOS) / GTK-WebKit (Linux)
 ├── WebViewHost (app/host.py)   loads file://…/frontend/dist/index.html, or :5173 with --dev
 │    └── React HUD ─┬─ window.pywebview.api.* → NativeBridge (app/bridge.py) → OS
 │                   └─ fetch / WebSocket → http://127.0.0.1:8000
 └── BackendProcess (app/backend.py)  spawns <venv> python backend/api/main.py on :8000
```

| Edge | Surface | Who owns it |
|---|---|---|
| Bridge | `NativeBridge`'s public methods, `js_api=bridge` | `app/bridge.py`, closed set asserted by `tests/test_bridge.py::TestSecurity`; path/URL policy in `app/paths.py` |
| HTTP | `/health`, `/models`, `/skills`, `/tools`, `/preferences`, `/mcp`, `/system`, `/computer/apps` | React's own `fetch`; the host uses only `GET /health` and (since this pass) `GET /system` for the workspace root |
| WebSocket | `/ws` | `frontend/src/hooks/useWebSocket.js` — streaming, confirmations, reconnect. The host does not duplicate any of it |
| Window state | `zaytoon_window_state.json` in the state dir | `WebViewHost`, saved on close |
| Process control | `SIGTERM`→group, `taskkill /T[/F]`, `BackendProcess.shutdown()` | `app/backend.py` — there is **no HTTP "stop the app" route** in the backend; stopping is a signal, not a request |

CORS, since it is the coupling that broke silently before:
`backend/api/main.py` lists `localhost`/`127.0.0.1` on **5173/5174** explicitly and
adds `allow_origin_regex=r"http://(localhost|127\.0\.0\.1):517\d+"`, which is what
lets pywebview's *pinned* asset-server port (`app/host.py`'s `DESKTOP_PORT_RANGE` =
5175-5179, first free) load the production bundle. `pick_asset_server_port()`
reports a busy port instead of handing it to pywebview, and
`tests/test_host.py::test_the_backend_cors_policy_covers_every_port_in_the_range`
now pins the two halves against each other (they live in different packages and
cannot import each other's code, so the test reads the backend's source as text —
the same technique this suite already uses for `AGENTS.md` and the workflow file).

---

## 2. Baseline measured before this pass

| Suite | Result on this machine, before these changes |
|---|---|
| `desktop/tests` | **322 passed, 2 skipped, 0 failed** (~40 s) |
| `backend/tests` | **2650 passed, 5 skipped, 0 failed** (~111 s) |
| Frontend | `npm test`: **37 files / 612 tests passed**, `npm run lint` clean (re-run here after `npm ci`) |
| CI | green on `main`, incl. `Desktop shell (windows-latest / ubuntu-latest / macos-latest)` running the real desktop suite on all three OSes |

Two things about that desktop number, because both are easy to misread afterwards:

* The 2 skips are platform conditionals, not excuses:
  `test_bridge.py:203` (real clipboard, `skipif sys.platform != "win32"`) and
  `test_desktop_shell.py:536` (`CREATE_NO_WINDOW`, Windows-only). On a Windows
  runner they run; on this Linux box they cannot.
* **8 test modules were not collected here at all** — `test_activity_model.py`,
  `test_app_wiring.py`, `test_confirmation_dialog.py`, `test_core_animation.py`,
  `test_panel_layout.py`, `test_sidepanel_parsers.py`, `test_telemetry.py`,
  `test_audio_pipeline.py` — because importing them on an interpreter without Tk or
  `libportaudio2` is a *collection error*, indistinguishable from a broken
  pipeline. `desktop/tests/conftest.py` skips them by name, **prints the reason**,
  and on GitHub Actions emits a `::notice` annotation so a leg says out loud which
  guarantees it did not check. CI's `Desktop shell` legs install
  `desktop/requirements-dev.txt` (customtkinter, keyboard, numpy, sounddevice, av),
  so those 8 modules do run there.

No pre-existing failure was found in either suite at baseline: every failure this
pass produced was one I introduced, and each is listed in §3 with the test that
caught it.

---

## 3. Findings

Severity = what a user would feel. "Fixed here" = the patch is in this branch and
the named test failed before it.

| # | Finding | Sev. | Root cause | Status |
|---|---|---|---|---|
| **F1** | **`open_path`, `reveal_in_os` and `open_url` accepted any path and any scheme.** All three end in a call the OS treats as *execution*: `os.startfile` runs `.exe`/`.bat`/`.lnk` exactly like a double-click, `open` launches an `.app`, `xdg-open` follows a `.desktop`'s `Exec=` line. The only prior check was `Path.exists()`. One bridge call from any script in the WebView — XSS in the HUD, a hostile string rendered as HTML, a skill's output — could execute a file the agent had been talked into writing, as the user. | **High** | The desktop had its own unpoliced route to the OS **beside** the backend's gated tool layer: the parallel execution path the project rules forbid. | **Fixed.** New `desktop/app/paths.py`: resolve *before* containment (a link inside the workspace pointing at `/etc` is outside it), require a granted root — the backend's own workspace (read from `GET /system`, so there is one policy, not two) plus the desktop state dir — and refuse anything the OS would run (Windows suffix list; POSIX execute bit or `.desktop`). `open_url` accepts `http`/`https` only. Public contract unchanged (still `bool`); the reason is logged, never returned, so a refusal cannot reveal whether a file exists. `ZAYTOON_OPEN_ROOTS` widens it explicitly. 35 tests in `desktop/tests/test_bridge_paths.py`, incl. symlink escape, `..`, an `os.startfile`/`Popen` spy proving a refusal touches nothing, and a tripwire against `backend/config.py` renaming the workspace default. |
| **F2** | The health-check thread called `evaluate_js` (and could `load_html`) from a thread pywebview did not create — WebView2 tolerates it, GTK and Cocoa do not; that class of bug surfaces as an intermittent hang or a crash on the window's thread, never as a failed test. It pushed a `backend-health` CustomEvent that **no file in `frontend/src` listens for**. | **High** (Linux/macOS) | Off-main-thread UI mutation, and a channel whose only consumer was the documentation describing it. | **Fixed.** The thread now records `WebViewHost.backend_up`, reported by `diagnose()`. `test_desktop_lifecycle.py::TestHealthThread` asserts the loop stops inside one sleep-slice *and* that `host.py` no longer contains the off-thread UI calls. |
| **F3** | A start that failed **orphaned the backend**: `run()` registered `backend.shutdown` only after `webview.start()` returned, so anything raising earlier — most commonly `import webview` on a Linux box without WebKitGTK — left the spawned backend holding port 8000 with no window, and the next launch waited 30 s for a backend it would not adopt. | **High** | Cleanup attached to the success path instead of a `finally`. | **Fixed.** `run()` is single-shot (`RuntimeError` on reuse); the `import webview` statement itself is now the first line inside `try:`, and teardown is `finally`. `TestFailedStartDoesNotOrphan` injects a raising fake `webview` at import / `create_window` / `start` and asserts exactly one `shutdown()` in all four scenarios. |
| **F4** | On close the host `wait()`ed on the child with no bound (a wedged child hung window close), escalated with `SIGKILL` only after a timeout, and never reaped the *tree* — so an `uvicorn --reload` worker, or any grandchild, survived a clean exit. | **Medium** | "Own the process" mistaken for "own the process group", and polite→force escalation treated as the same axis as single→tree. | **Fixed.** `SHUTDOWN_GRACE_S=8.0` / `KILL_WAIT_S=5.0` bound every wait; the group is signalled on the **graceful** step (`SIGTERM` to the group, `taskkill /T`) with `force` only escalating it, each with a single-child fallback; `_is_our_group_leader()` guards `killpg` so the polite step can never signal our own group. Pinned by `TestTreeOwnership`, which spawns a real grandchild and asserts it is reaped, and that a foreign backend is left alone. |
| **F5** | Two rules for one path: `config.py` computed the state dir as `%LOCALAPPDATA%\Zaytoon` (and per-OS equivalents) while `WebViewHost._get_state_file()` re-read `ZAYTOON_HOME` at call time, and the default put the file in `<repo>/Config` — unwritable for a Program Files / `/Applications` install. | **Medium** | A portable-checkout default inherited by a packaged-install world. | **Fixed** by making the precedence explicit instead of picking a winner: `ZAYTOON_HOME` → `<repo>/Config` **only if writable** (dev checkouts keep their file exactly where it always was; `TestStateLocation` pins it) → per-OS state dir. `_is_writable()` walks to the nearest existing ancestor so the constructor stays side-effect-free. The host re-reads `ZAYTOON_HOME` at *call* time, deliberately — that is the documented contract four existing tests pin. |
| **F6** | 1 436 lines of retained legacy Tk UI (`tray.py`, `sidepanel.py`, `panel.py`, `dialogs.py`, `hotkeys.py`, `app.py`, `core.py`, `orb.py`, `activity.py`, `header.py`) were still imported by `main.py`, and `config.py` still exported a `TRAY_MODE` that selected between two shells. | Medium (maintainability) | A fallback stack kept alive by its own import-time `sys.path` patching. | **Fixed earlier in this session**: `main.py` no longer imports it, the legacy modules' import failures became guards that return, `desktop/tests/conftest.py` puts the legacy package on `sys.path` for its own tests, `TRAY_MODE` is gone. The modules and their 8 skipped-here test files stay — they are the reference implementation for hotkey/palette behaviour. |
| **F7** | The tray palette hardcoded dark colours that drifted from the shipped `index.css` — the exact failure mode `docs/DESIGN_SYSTEM.md` names (a second source of truth). | Low | Copy instead of generate. | **Fixed and pinned** earlier: `test_design_parity.py` reconciles `palette.py` with `frontend/dist/assets/index.css`. Read it carefully before extending it: it pins `--panel-bg`, `--bg-primary`, `--bg-tertiary` and a `bg_body` key that has no CSS counterpart. `--bg-secondary`/`--border` match by luck, not by assertion. |
| **F8** | `DesktopConfig.host` accepted anything, `0.0.0.0` included — which would put the whole loopback API on the LAN, since the backend binds whatever the host asks for. | Medium (latent) | No validation on a field whose only sane values are two. | **Fixed**: loopback-only, with the reason in the comment; the backend enforces the same on its own bind. |
| **F9** | `app/instance.py`'s single-instance guard existed only on Windows (`CreateMutexW`); the docstring described a guarantee Linux and macOS did not have. | Medium | One branch of a three-platform promise. | **Fixed earlier**: an `flock` held for the process lifetime on POSIX, released by the kernel on death, so a crash cannot leave a stale lock; `tests/test_single_instance.py` covers both. |
| **F10** | The process registry reported a PID as alive without reaping it (zombie on POSIX, stale handle on Windows), so a dead child could look live. | Medium | Missing `waitpid(WNOHANG)`-style reap. | **Fixed earlier**: reap the tracked child, then `pid_exists`; `kill` clears `exit_info` so a refused `kill -0` is not read as "alive" (`test_reaps_a_tracked_child_before_trusting_kill_0`). |
| **F11** | PortAudio abort surfaced as `terminate called without an active exception` / `Aborted (core dumped)` and poisoned the next test's audio state. | Medium | No handling at the C-boundary, no per-test teardown. | **Fixed earlier**: `AudioError` with a message, `terminate_all_backends()` in the teardown, and CI's ubuntu leg installs `libportaudio2` so the audio modules run there instead of vanishing. |
| **F12** | The health interval had a production code path keyed to the test framework (`0.1 s` whenever `pytest` was in `sys.modules`). | Low | Test hook inside shipping code. | **Fixed**: `ZAYTOON_HEALTH_INTERVAL` (floored at 0.5 s) with a comment naming its purpose, and a source-text test that fails if pytest-detection returns to `host.py`. |
| **F13** | `AGENTS.md` documented the health check as dispatching a `backend-health` CustomEvent "so the React UI can show link status without polling itself". No such listener exists in `frontend/src` — `grep` for the event name returns only `host.py` and the doc itself. | Low (docs) | Documentation describing intent rather than behaviour, which is how F2 stayed invisible: a reader could see a documented contract where there was only a sender. | **Fixed**: the AGENTS.md section now says what the thread does (sets `backend_up`, exposed by `diagnose()`), why it must not touch the WebView from that thread, and that `useWebSocket.js` owns liveness and reconnection. |
| **F14** | `scripts/build_exe.py`'s `HIDDEN_IMPORTS` list was hand-maintained and **already stale in the shape that matters**: nothing could notice a missing entry, because every test imports from source. `app/paths.py` — added by F1 in this very pass — was missing from it, which is a `ModuleNotFoundError` on first launch of the frozen exe and a green CI run. | High (release) | A build-time import graph with no test coverage. | **Fixed**: `desktop/tests/test_packaging_imports.py` recomputes `main.py`'s `app.*` import closure with `ast` and asserts every member is declared, that no declared entry is stale, and that `app/__init__.py` does not quietly re-export the world (which would drag Tk into the frozen build). It failed on `app.paths` first, then passed after the entry was added. |
| **F15** | The one artefact that proves the desktop app *really* starts — `scripts/smoke_desktop.py`: real window, real backend, `_probe_page()` evaluating `document.querySelector('#root')` and the bridge through `_eval_async`, a PASS/FAIL table, a watchdog, `--keep/--timeout/--dev` — **is not run by CI at all.** `grep -rn smoke .github/workflows/ci.yml` returns one hit, and it is the *backend* job's "Live smoke check" step, which does not call this script. | Medium (verification) | A diagnostic script and a workflow that never agreed on an interface. | **Not fixed — reported.** Running it needs a display (`xvfb-run`) plus `pywebview` in the runner, and on Windows/macOS runners a window that may not survive the headless-ish service session. This is the single highest-value CI addition for this component and it needs a real decision, not a patch. |
| **F16** | The shipped runtime dependencies (`desktop/requirements.txt`) are `pywebview>=5.0` and `websockets>=12.0` only. The retained Tk stack imports `pystray` (guarded: `pystray = None`, `available = False`) and needs `customtkinter`, `keyboard` — present in `requirements-dev.txt` only; `pyinstaller` is imported nowhere and pinned nowhere. | Low | An optional-fallback design that is honest in code (`try/except`) but undeclared on paper. | **Reported, not "fixed"**: adding `pystray` to the runtime deps would change what a fresh install pulls, and the tray it enables is not reachable from `main.py` any more (F6). Either it is a feature (declare it, wire it) or it is history (delete the modules). That is a product call, flagged in §9. |

Two things deliberately **not** changed, with reasons:

* `backend_is_up()` probes `AF_INET` only, so `ZAYTOON_HOST=::1` reads "not up" and
  a second backend would be spawned. Left alone: `loopback_host()`'s default is
  `127.0.0.1`, and dual-stack probing is a wider behaviour change than an unusual
  override warrants.
* `webview2_runtime_present()` returning `True` off Windows is **correct** — the
  check means "this platform does not need the WebView2 Runtime", which is true on
  Linux and macOS. Not a defect; recorded because it looks like one.
* `_resolve_url()` still returns `str(index.html)` rather than a `file://` URL.
  `tests/test_host.py` pins `isinstance(url, str)`; the comment in `host.py`
  explains that pywebview accepts a path and resolves it itself. Changing the form
  to something prettier is an unverifiable edit to the one thing the shell exists to
  do, so it was left alone.

---

## 4. Lifecycle, measured (Linux, this machine)

Real backend, real `BackendProcess`, three complete launch→ready→close cycles
(`/tmp/lifecycle_measure.py`, `BACKEND_PORT=8123`, no browser involved):

| Metric | Value |
|---|---|
| `ensure_running()` returned | **0.80 s** median (0.74 / 0.80 / 0.99) |
| `GET /health` → 200 | **0.89 s** |
| `shutdown()` returned | **0.21 s** median (0.21 / 0.21 / 0.24) |
| Processes matching `api/main.py`, before → after each close | **1 → 0**, all three cycles |
| Leftovers after the last cycle | none |

What the numbers say: readiness is a genuine poll, not a sleep, so there was no
arbitrary delay to remove; teardown is a group `SIGTERM` this backend answers in
~0.2 s, so the force-kill branch stays the fallback it should be; and repeated
launch/close cycles neither leak nor drift. A cold `import api.main` is
separately **2.49 s**, which is also the floor for a PyInstaller start — one reason
§7 recommends `--onedir`.

Startup work that is *not* the backend — `import webview`, window creation, parsing
`dist/index.html` — is GUI-bound and was not measured here (§8).

---

## 5. Ownership, as coded

| Owner | Resource | Lifetime | Evidence |
|---|---|---|---|
| `BackendProcess` | child process + its process group | spawn → bounded `shutdown()` | F4, `test_desktop_lifecycle.py` (14 tests) |
| `WebViewHost` | the `webview` window, `native` handle | created in `run()`, released in `finally` | F3 |
| health thread | daemon thread + a `connect_ex` per 5 s | `stop()` → `join(2.0)` inside teardown | F2 |
| `instance_lock` | Windows mutex / POSIX `flock` fd | process lifetime, kernel-released | F9 |
| `ProcessRegistry` | handles to agent-started children | reap-then-report | F10 |
| `SpeechRecorder` | PortAudio stream + worker thread | `start()`/`stop()` bounded; `terminate_all_backends()` | F11 |
| `NativeBridge` → OS | `Popen`/`startfile`/`webbrowser` on a path or URL | one-shot, after policy | F1, `test_bridge_paths.py` |

The bridge is synchronous by design (a call completes inside the JS `await`), and
the shell runs no event loop of its own: asyncio lives in the backend, the React
client's WebSocket owns reconnection.

---

## 6. Compatibility matrix

*verified* = the tests ran on that OS. *partially verified* = some of it did.
*not tested here* = the code exists and was read; this machine could not run it.

| Area | Windows | Linux | macOS |
|---|---|---|---|
| Desktop suite (374 tests + 2 platform-conditional) | **verified** — CI `Desktop shell (windows-latest)` | **verified** — here and CI | **verified** — CI `Desktop shell (macos-latest)` |
| Tk / PortAudio modules (8 files) | verified in CI (dev deps installed there) | **not collected here** (see §2); verified in CI | verified in CI |
| Window opens, renders, closes | **not tested** anywhere — no CI job runs a window | not tested here (no display) | not tested |
| Backend spawn + wait for `/health` | verified in CI | **verified here, with timings** (§4) | verified in CI |
| Shutdown reaps the tree, bounded | reading-only (`taskkill /T`, `/F` on escalation) | **verified here** (real grandchild, 3/3 cycles) | reading-only (`SIGTERM` to the group) |
| No orphan when start fails | same code path, not run | **verified here** (fake raising `webview`) | same code path, not run |
| Off-main-thread UI calls impossible | n/a (WebView2 tolerates them) | **fixed + pinned** | **fixed + pinned** |
| State file (`ZAYTOON_HOME` → repo `Config` if writable → per-OS) | **verified** (`%LOCALAPPDATA%` fallback) | **verified** (`XDG_STATE_HOME`) | reading-only (`~/Library/Application Support/Zaytoon` branch); CI's macos leg passes with `ZAYTOON_HOME` set |
| WebView2 runtime diagnostic | **verified** — registry `pv`/`version` probes; the CI image carries the Evergreen SDK | n/a (correct no-op) | n/a (correct no-op) |
| Single-instance guard | verified (mutex) | verified (`flock`) | verified (`flock`) |
| Hotkeys | verified in CI (`RegisterHotKey` on the Tk thread, `WM_HOTKEY` via `peek_message`, re-register on `IDCANCEL`, unregister before `DestroyWindow`) — see `test_desktop_shell.py` | partial: `python3-xlib` path, no display here | **not tested**; `pyobjc`/`Quartz` is neither imported nor declared |
| Tray / notifications | `pystray` + `winotify` **undeclared** at runtime (F16), gracefully absent | partial: `pystray`/AppIndicator, `notify-send` fallback — `libnotify-bin` is **not** installed on the ubuntu runner, so the fallback is untested there too | same undeclared dependency; not tested |
| Microphone / speech | verified: WASAPI loopback, `PA_UnanticipatedHostError` handled | partial: PortAudio + `pactl`/`wpctl` probes (`libportaudio2` installed in CI) | partial: `system_profiler` probe. **AVFoundation microphone permission is not handled, and nothing can handle it: there is no `.app` bundle to carry `NSMicrophoneUsageDescription`** |
| Frozen build | buildable in principle; CI only asserts `check_prerequisites()` reports rather than raises; **no build is run, uploaded or launched** | **absent** — no spec, no job | **absent**, and blocked by the `.app`/signing issue above |

---

## 7. Packaging and release readiness

`desktop/scripts/build_exe.py` today: `check_prerequisites()` (returns a list of
human-readable problems instead of raising — CI asserts this), PyInstaller
`--noconfirm --onefile` (or **`--onedir`, added in this pass** as an opt-in:
`--distpath desktop/dist --workpath desktop/build --specpath desktop/build
--paths desktop`, `--windowed` unless `--console`), the `HIDDEN_IMPORTS` list now
pinned by F14's test, and a printed notice that **"the executable does NOT contain
the backend"** — the user is told to copy the exe next to a checkout or set
`ZAYTOON_REPO_ROOT` / `ZAYTOON_BACKEND_PYTHON` / `ZAYTOON_BACKEND_MAIN`, then
verify with `Zaytoon.exe --diagnose`.

`.gitignore` already excludes `desktop/dist/` and `desktop/build/` ✓. `desktop/icons/`
holds one asset, `source.png`, that **nothing uses**: there is no `.ico`/`.icns`, no
`--icon` flag, no version resource, no installer, no code signing, no notarization,
no `zaytoon.desktop`/AppImage/flatpak for Linux, and no `desktop/README.md` (there is
no such file). `pyinstaller` is not pinned in any requirements file.

**Verdict: not release-ready on any platform, and the gap is not mainly code.**
What a release needs, in order of blocking power:

1. A build that *is* the product: `--onedir` with `backend/` (and its venv, or an
   embedded interpreter) beside it, or a decision that the desktop requires a
   checkout. The current message makes the dependency explicit, which is honest but
   is not an installer.
2. Windows: `--icon` from a generated `.ico`, a version resource, an NSIS/MSIX
   wrapper, `signtool` on the exe. macOS: a real `.app` bundle
   (`NSMicrophoneUsageDescription` is mandatory for the speech feature), codesign +
   notarize. Linux: `.desktop` + icon + either AppImage or flatpak.
3. Pin `pyinstaller` in `desktop/requirements-dev.txt` (the build is otherwise
   irreproducible) and re-enable an artifact upload — there is currently no
   `upload-artifact` step anywhere in the workflow, so nothing survives the job.
4. Smoke the artefact, not the source: `Zaytoon.exe --diagnose`, then
   `scripts/smoke_desktop.py` against the built tree (F15).

---

## 8. Verifying the GUI legs on a real machine

The sandbox cannot do any of this; a laptop can, in a few minutes. Build the venv
first: `cd backend && python -m venv .venv && .venv/Scripts/pip install -r
requirements-dev.txt -r ../desktop/requirements.txt -r ../desktop/requirements-dev.txt`
(installing the desktop dev deps too, so all 18 test modules are collected).

```bat
:: Windows — from a normal interactive session, not a service
backend\.venv\Scripts\python -m pytest desktop\tests -q     :: everything passes; 0 skipped (the two conditional skips run here)
backend\.venv\Scripts\pythonw desktop\main.py                :: window opens, HUD renders
:: close it, then:  tasklist | findstr /i "python pythonw"   -> nothing left
:: relaunch and close 3x; the geometry file must survive:
type %LOCALAPPDATA%\Zaytoon\zaytoon_window_state.json
backend\.venv\Scripts\python desktop\scripts\smoke_desktop.py --timeout 90
```

```bash
# macOS / Linux
backend/.venv/bin/python -m pytest desktop/tests -q
backend/.venv/bin/python desktop/main.py
pgrep -fl "api/main.py"        # before close: 1, after close: 0
# and the state file at the path `--diagnose` prints
backend/.venv/bin/python desktop/main.py --diagnose
```

Expected on success: a window roughly a second after launch with `/health` already
answering; no `api/main.py` process left after close, on every one of three
repetitions; `zaytoon_window_state.json` present at the location `diagnose()`
reports. Expected failure signature this audit removed: a backend alive after the
window is gone, and a second launch that stalls ~30 s waiting for a backend it will
not adopt (F3).

`--diagnose` prints `webview` availability, the WebView2 runtime, ports, the state
file path and `backend_up`; `--no-backend` starts the shell without spawning, and
`--dev` points the window at Vite — those four flags plus `--verbose` are the whole
CLI (`main.py` has no `--setup`; earlier notes claiming one were wrong).

---

## 9. Open items, ranked

1. **No GUI verification exists on any platform, in CI or here.** The suite proves
   logic, wiring, policy, lifecycle and ownership; it cannot prove a window
   renders. `smoke_desktop.py` is 90 % of the way to that proof already (it evaluates
   into the page and PASS/FAILs on it) and needs a runner with a display:
   `xvfb-run` + `pywebview` + GTK on a Linux leg is the cheapest first version, and
   Windows/macOS can reuse the existing `Desktop shell` legs with `--timeout 90`.
   This is the one gap that a new test file cannot close.
2. **macOS microphone permission and macOS hotkeys** are environment-shaped, not
   code-shaped: no `.app` bundle ⇒ no `NSMicrophoneUsageDescription` ⇒ the speech
   feature cannot be granted; `pyobjc`/`Quartz` is neither declared nor imported, so
   the mac hotkey path is a name in a comment. §7.2.
3. **`pystray` remains undeclared at runtime** while the tray that needs it remains
   unreachable from `main.py` (F6/F16). Decide: feature (declare, wire, test) or
   history (delete ~1.5 k lines and their tests). Do not leave it between the two.
4. **CORS is loopback-but-port-wide**: `allow_origin_regex` accepts *any* `:517\d`
   port with `allow_credentials=True`, so any local server that can bind 5170-5179
   and a page loaded in the WebView can read the API. Tightening it means the host
   passing its actually-bound port to the backend at spawn (one env var, e.g.
   `ZAYTOON_DESKTOP_ORIGIN`) and the regex disappearing. Deliberately **not** done
   in this pass: it changes an inter-process contract and needs both suites green at
   once; the new `test_host.py` pin makes it a safe change to make later.
5. **A `SIGKILL`ed launcher still orphans the backend on Linux.** `PR_SET_PDEATHSIG`
   in a `preexec_fn` would fix it, and `preexec_fn` in a threaded process is the
   risky half of that trade (fork-without-exec, locks held across fork). My own
   aborted measurement harness produced exactly this orphan, which is why it is
   written down rather than patched speculatively.
6. **`--onefile` startup is unmeasured.** The docstring implies a hand-me-over
   single artefact; §4's 2.49 s import cost says onedir is the release shape. Now a
   flag, not a rewrite.
7. **51 scattered `sys.platform` / `platform.system()` / `os.name` checks across 16
   files** (desktop: `backend.py`, `bridge.py`, `config.py`, `host.py`,
   `instance.py`; backend tools: `base.py`, `linux.py`, `media.py`, `netguard.py`,
   `processes.py`, `sandbox.py`, `system.py`, `terminal.py`, `windows.py`;
   `core/secrets.py`, `core/system_resources.py`). **Assessment: do not "fix" this
   by rewriting it.** The backend's are per-OS by design behind `tools/base.py`
   (`windows.py`/`linux.py`/`darwin.py`), and the five desktop files above *are* the
   adapter layer for this shell — the branching is where the platform knowledge
   correctly lives. The one place inline branching actively hurts testability is
   `bridge.py`'s `_notify_windows` / `_notify_macos` / `_notify_linux` trio, where an
   adapter would let each be asserted with the platform recorded. Recommendation:
   no new framework; add an adapter where a behaviour is untestable as written —
   currently that one.

---

## 10. Checked, and found *not* to be true

Recorded because a plausible finding that survives into a note is how a wrong one
gets propagated, and because "which controls hold" is part of an audit.

* **There is no `POST /shutdown` route** on the backend, so there was never a
  cross-origin way to shut the app down; the shutdown code in
  `backend/api/main.py` is the app's own lifespan cleanup (terminals, processes,
  memory). The desktop stops the backend with a signal. No same-origin guard on a
  destructive endpoint was needed because no such endpoint exists.
* **CI does not run `smoke_desktop.py`** (F15), and there is no artifact-upload step
  to have commented out, and no manual-only `Package (windows-latest)` job —
  `workflow_dispatch` at the top of the workflow is a manual trigger for the whole
  file, and the packaging assertion inside `Desktop shell` is the importable-
  `check_prerequisites()` step.
* **Nothing pins `pyinstaller==6.21.0`**, and `desktop/requirements.txt` declares no
  `pystray`/`customtkinter`/`keyboard`; those are dev/test deps or absent-by-design
  guarded imports.
* **`webview2_runtime_present()` is not a Windows-only check with a wrong default** —
  see §3.
* **`desktop/README.md` does not exist** (nothing to update there); the desktop's
  documentation is `AGENTS.md` §"The desktop shell" and `docs/ARCHITECTURE.md`.
