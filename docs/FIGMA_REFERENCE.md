# Figma Reference — "Jarvis New ui" (NEXUS AI)

Authoritative visual reference for the Zaytoon frontend redesign.

- **Source:** https://www.figma.com/make/TwV6NwcyWZqkfDMSc43Er9/Jarvis-New-ui--Community-
- **File key:** `TwV6NwcyWZqkfDMSc43Er9`
- **Verified on:** 2026-10-19, by driving the live Make preview in headless Chrome over
  the DevTools Protocol (clicking real controls and screenshotting the rendered app),
  plus reading computed styles out of the running document.

Everything below was **read from the rendered reference**, not inferred. Where a
behaviour could not be observed it is marked as such.

> **Access note.** The Figma editor shell itself is client-rendered and its REST API
> needs a token (`GET /v1/files/<key>` → 403 unauthenticated). No token was available,
> so the editor canvas was never read directly. What *was* fully accessible is the
> published Make preview, which is served from a public `figma.site` URL inside the
> editor's preview iframe — that is the actual application, rendered and interactive.
> The design below comes from that running app.

---

## 1. Design tokens (measured)

### Colour

| Token | Value | Usage |
|---|---|---|
| `nx-bg` | `#010818` → measured `rgb(1,8,18)` | page background base |
| `nx-bg-gradient` | `radial-gradient(at 50% 20%, #071428 0%, #020d1a 40%, #010812 70%, #000508 100%)` | page background |
| `nx-grid` | `linear-gradient(rgba(0,245,255,.024) 1px, transparent 1px), linear-gradient(90deg, rgba(0,245,255,.024) 1px, transparent 1px)` | HUD grid overlay |
| `nx-cyan` (primary) | `#00f5ff` (`rgb(0,245,255)`) | headings, active state, icons, clock |
| `nx-text` | `rgba(220,235,255,.9)` | default body text |
| `nx-text-dim` | `rgba(255,255,255,.4)` / `.3` / `.25` | labels, sub-labels |
| `nx-panel` | `rgba(0,8,20,.5)` | cards / metric tiles |
| `nx-panel-2` | `rgba(0,10,25,.6)` | dock, elevated chrome |
| `nx-border` | `rgba(0,245,255,.12 … .2)` | 1px hairlines everywhere |
| `nx-accent-purple` | `#a855f7` (`purple-500`) | MEMORY series |
| `nx-accent-amber` | `#f59e0b` (`amber-500`) | GPU series, warnings |
| `nx-accent-sky` | `#0ea5e9` (`sky-500`) | NETWORK series |
| `nx-success` | `#22c55e` (`green-500`) | OPTIMAL / good / complete |
| `nx-danger` | `#ec4899` (`pink-500`) | destructive accents |

Accent usage is consistent: **cyan = system/default**, **purple = memory**,
**amber = gpu/warn**, **sky = network**, **green = ok**. Metric tiles and progress
bars each take their series colour, applied as `border: 1px solid <c>/.145`,
`background: <c>/.04`, and a `linear-gradient(90deg, <c>/.5, <c>)` fill.

Measured transparency ladder used for cyan: `.04` (fill) `.05` `.07` `.1` `.12`
`.145` `.15` `.19` `.2` `.3` `.4` `.5` `.6` — i.e. hairline borders sit at ~.15–.2
and glow shadows at `.05`.

### Typography

Three families, loaded as webfonts (all Google Fonts):

| Family | Role | Measured |
|---|---|---|
| **Orbitron** | display / identity | `NEXUS AI` 13px +1.95px tracking; clock 15px; state word (`STANDBY`) ~16–24px with wide tracking; big `100%` numerals |
| **Share Tech Mono** | data, labels, micro-copy | 7–12px, mostly 400/500, tracking `.8–1.32px` on headers |
| **Rajdhani** | UI body, buttons, panels | base 16px/24, buttons 16px **500**, inputs 13px |

Rules observed:

- Section titles are Orbitron **or** Rajdhani depending on panel, with
  `text-transform: uppercase` applied in markup (the strings are typed uppercase).
- Micro-labels (`NEURAL OPS`, `UPTIME:`, `TOP PROCESSES`) are Share Tech Mono at
  **8–10px** with the label colour `rgba(255,255,255,.3)`.
- The HUD look comes from *tiny* type + wide tracking, not from large type.

### Shape & spacing

| Element | Measured |
|---|---|
| metric tile | `border-radius: 14px`, `padding: 12px`, `gap: 8px`, `border: 1px solid rgba(0,245,255,.145)`, `background: rgba(0,8,20,.5)` |
| action button (`SCAN`) | `padding: 8px 16px`, `radius 14px`, `border 1px rgba(0,245,255,.19)`, `bg rgba(0,245,255,.04)`, Rajdhani 500 |
| quick chip (`scan`) | `padding: 4px 10px`, `radius 10px`, `border 1px rgba(0,245,255,.15)` |
| chat bubble | `padding: 10px 14px`, `radius: 4px 16px 16px`, `border 1px rgba(0,245,255,.2)`, `box-shadow: 0 0 12px rgba(0,245,255,.05)` |
| dock | `padding: 8px 16px`, `radius 16px`, `border 1px rgba(0,245,255,.12)`, `box-shadow: 0 0 30px rgba(0,0,0,.5)`, `gap: 6px`, buttons `44×44` |
| top-bar icon buttons | `32×32`, `radius 8–12` |
| shell | left rail ~300px, right rail ~300px, centre flexible; dock `493px` wide, centred |

### Motion

- Rings around the orb carry drifting percentage labels and a slow rotation.
- Sparklines animate (CPU/MEMORY/NETWORK/GPU update every ~1s).
- The waveform under the top of the bottom bar animates continuously.
- Toasts slide in from the top-right and auto-dismiss (close button also present).
- State cycling on the orb (`STANDBY → LISTENING → …`) is click-driven.

---

## 2. Screens observed in the reference

| # | Screen | How reached | Verified |
|---|---|---|---|
| 1 | **Dashboard (home)** | default | ✅ screenshot + DOM |
| 2 | Left rail → **MEMORY CORE** | `MEMORY` tab | ✅ |
| 3 | Right rail → **DATA SEARCH** | `SEARCH` tab | ✅ |
| 4 | **APP LAUNCHER** overlay | top-bar grid button | ✅ |
| 5 | **SYSTEM CONFIGURATION** modal | top-bar gear | ✅ (4 tabs) |
| 6 | **GESTURE CONTROL** full-screen | top-bar hand button | ✅ |
| 7 | **NEXUS SPECTRUM SCANNER** full-screen | `SCAN` action | ✅ |
| 8 | **Toast notifications** | top-bar bell / any action | ✅ |

### 1. Dashboard

Three columns + two chrome bars.

**Top bar** — circular logo mark, `NEXUS AI` (Orbitron) over `OS v3.7.2 — ALPHA`
(Share Tech Mono, cyan/45%); a state pill with a dot + `STANDBY`; four status chips
(`ONLINE 99.9%`, `SECURE AES-256`, `LOAD 42%`, `NET 1.2GB/s`) each with its own icon;
the clock (`Orbitron 15px`, cyan) over the date; then four icon buttons — bell,
hand, grid, gear.

**KPI strip** (centred, above the orb) — `12T NEURAL OPS`, `128K CONTEXT`,
`98.7% ACCURACY`, `42MS LATENCY`. Value in the series colour, label Share Tech Mono
8px at 30% white, inside one rounded container.

**Left rail — SYSTEM MONITOR** (`● LIVE`)
- `System Health` card: circular progress gauge (value inside), `UPTIME: 4h 1m`,
  thermometer + `54°C`, `OPTIMAL` right-aligned in green.
- 2×2 metric tiles: `CPU`, `MEMORY`, `NETWORK`, `GPU` — icon + label, big value +
  `%`, a thin progress bar, and a sparkline in the series colour.
- `TOP PROCESSES` list: name / % (series colour) / memory (Share Tech Mono).

**Left rail — MEMORY CORE** (alternative tab)
- `MEMORY CORE` heading with two mini-actions `CLOUD SYNC`, `GIT BACKUP`.
- Status row: `LOCAL SYNCED (4)`, `CLOUD ACTIVE (4)`, `GIT PUSHED (4)`.
- Search field `Search memories...`, tag chips (`system`, `config`, `voice`, `dev`,
  `research`, `quantum`, `api`, `preferences`, `ui`).
- Memory cards: title, preview text, tag chips, relative time (`2d ago`), status dot.
- Dashed `+ ADD MEMORY` button.

**Centre — the orb**
- 4 concentric rings, each with a drifting percentage read-out placed around it.
- Crosshair reticle in the middle labelled `NEXUS`.
- State word (`STANDBY` / `LISTENING`) in Orbitron, subtitle beneath
  (`NEXUS NEURAL CORE ONLINE` / `VOICE RECOGNITION ACTIVE`).
- Four mini-tabs: `NEURAL`, `VOICE`, `MEMORY`, `NETWORK`.
- Hint: `CLICK ORB TO CYCLE STATE`.
- Four action buttons: `SCAN`, `ANALYZE`, `DEPLOY`, `SECURE`.

**Right rail — COMMAND CONSOLE**
- Header `>_ COMMAND CONSOLE` with export + clear icon buttons.
- Message bubbles: assistant bubbles left (dot marker, cyan hairline, radius
  `4px 16px 16px`), user bubbles right (purple tint, `16px 16px 16px 4px`).
  Timestamp bottom-right inside the bubble, Share Tech Mono ~9px.
- Quick chips: `scan`, `status`, `weather`, `deploy`, `analyze`, `help`.
- Input row: `>` prefix, `Enter command or speak...`, send button.

**Right rail — DATA SEARCH** (alternative tab)
- Header `DATA SEARCH` + `● NEURAL CONNECTED`.
- Search field + `GO`.
- Filter chips `ALL` / `IMAGES` / `DOCS`.
- `TRENDING IN DATASTREAM` chips, `RECENT SEARCHES` list with clock icons.

**Bottom bar** — speaker/volume button left, animated waveform centred, `IDLE`
label + circular mic button right.

**Dock** — `NEXUS APP DOCK` label (Share Tech Mono, two lines) at left, a floating
pill with 9 icon buttons (`44×44`, each in its own accent colour), `SWIPE UP`
hint at right.

### 4. App launcher

`APP LAUNCHER` / `24 APPLICATIONS AVAILABLE`, category filters
`ALL SYSTEM AI MEDIA COMMS TOOLS`, then a responsive grid of 24 tiles
(`Terminal`, `Settings`, `Monitor`, `Security`, `Storage`, `Network`, `NEXUS Core`,
`AI Vision`, `Analyzer`, `Code AI`, `Memory`, `Vault`, `Holoweb`, `Spotify`,
`HoloVid`, `NexMap`, `Mail`, `Comms`, `Cloud Sync`, `Files`, `Calendar`, `ChronOS`,
`Power AI`, `Favorites`) — icon + name in a `rounded-2xl` card.

### 5. System configuration modal

`SYSTEM CONFIGURATION` / `NEXUS AI OS v3.7.2 — Advanced Settings`, red close button.
Left nav: `USER PROFILE`, `AI MODEL`, `API KEYS`, `SECURITY`.
User profile form: avatar tile, name + handle, then `USERNAME`, `FULL NAME`,
`ASSISTANT NAME` fields. Footer: `CANCEL` (reset icon) and `SAVE CHANGES` (floppy
icon, cyan). The modal dims the app behind it.

### 6. Gesture control

`GESTURE CONTROL` / `HAND SKELETON TRACKING v2.1 — NEURAL CV ENGINE`.
Left: a framed viewport with `CAMERA ACTIVE — 60fps`, an animated hand-skeleton
wireframe, and a pill showing the active gesture.
Right: `DETECTION STATUS` + `CONFIDENCE` with a progress bar; `ACTIVE GESTURE`
card; `GESTURE REFERENCE` list — `SWIPE RIGHT / Navigate · Select`,
`SWIPE LEFT / Go Back · Dismiss`, `TWO FINGERS / Scroll · Zoom`,
`OPEN PALM / Pause · Stop AI`, `PINCH / Close Panel`, `POINT UP / Activate Voice`.

### 7. Spectrum scanner

`NEXUS SPECTRUM SCANNER` / `FULL SYSTEM ANALYSIS v2.4.1`, `● COMPLETE 100%`, red
close. Left: a framed particle/grid viewport with corner brackets and a
`SCAN PROGRESS … 100%` bar. Right: `ANALYSIS RESULTS` — rows of
label/value/status-chip (`NEURAL NETWORK`, `SECURITY LAYER`, `MEMORY USAGE`,
`NETWORK HEALTH`, `API ENDPOINTS`, `VOICE ENGINE`, `GESTURE MODULE`,
`DATA INTEGRITY`), then a green `SCAN COMPLETE / All systems operational.` card.

### 8. Toasts

Top-right stack, `rounded` card with a cyan hairline, circular info icon, bold
title + one line of detail, and a close `×`. Observed titles: `System Alert`,
`NEXUS Launching`, `Gesture Detected`, `Voice Active`, `Scan Complete`,
`ANALYZE Complete`, `DEPLOY Complete`, `SECURE Complete`.

---

## 3. Responsive behaviour of the reference (measured)

The reference is a **fixed desktop layout**. Driving the preview iframe through
inner widths 1231 → 997 → 731 → 531 → 331 → 111 px showed:

- `document.scrollWidth === innerWidth` at every size — i.e. the shell uses
  `overflow: hidden` rather than reflowing.
- The dock stays a fixed 493px and slides off-screen (negative `x`) as the viewport
  narrows.
- At ~531px the centre orb is clipped away entirely and the top bar's `AES-256`
  chip wraps — the layout is cut, not adapted.

So **the reference defines no tablet/mobile layout**. Our implementation matches the
reference on desktop and degrades gracefully instead of clipping; that is a deliberate
deviation, since "clip the content" is not a design decision we should copy.

---

## 4. Screen → route mapping

| Reference screen | Implementation | Backing capability |
|---|---|---|
| Dashboard (home) | `components/nexus/NexusDashboard` (HOME view) | `GET /system`, `GET /health`, WS session |
| SYSTEM MONITOR rail | `NexusMonitor` | same poller; "—" for unreported values |
| MEMORY CORE rail | Memory view of the console rail / MemoryAgent | `memory` intent, WS |
| DATA SEARCH rail | Search via chat / `search_web` tool | tool layer (gated) |
| COMMAND CONSOLE rail | `NexusConsole` | WS session, same transcript |
| APP LAUNCHER | CommandPalette + ComputerView app grid | `GET /computer/apps`, `open_app` |
| SYSTEM CONFIGURATION | SettingsPanel + workspace SETTINGS tab | `GET/POST /settings/*`, provider registry |
| Toasts | existing error/status frames (no fake toasts added) | real events only |
| GESTURE CONTROL | **not implemented** — no camera/hand-tracking capability exists | — |
| NEXUS SPECTRUM SCANNER | **not implemented** — no backing analysis exists | — |
| Dock buttons 1–9 | App dock (navigation) — real destinations | `viewState` |
| SCAN/ANALYZE/DEPLOY/SECURE | **not implemented** as decorative fakes; their capabilities exist as chat/tools instead | — |

Reference-only surfaces with no honest backing capability are listed as
"not implemented" rather than faked; the decorative KPI strip (12T NEURAL OPS
etc.) is likewise not reproduced — the dock strip instead shows real counters
(LOAD / TOOLS / VALID / LATENCY).

## 5. Implementation status (2026-10-19)

The retarget is applied globally through the shared token layer:

- `frontend/src/styles/theme.css` — every `--hud-*` value now carries the
  measured reference palette (`#010812` void, `#00f5ff` accent, series
  colours purple/amber/sky/green/pink), the reference page gradient, and the
  Share Tech Mono data face. Input tokens keep WCAG AA (≥4.5) — the ratios
  are computed from the tokens by `input.visibility.test.js`, not asserted by
  hand.
- `frontend/tailwind.config.js`, `frontend/index.html` (Share Tech Mono),
  `frontend/src/styles/nexus.css` (`--nx-*` aligned to the same values).
- `desktop/app/theme.py` + its three test files — the desktop shell keeps
  token-for-token parity with the CSS (`test_design_parity.py`).
- Panel geometry follows the reference's rounded tiles: `--radius-panel` 16px,
  `--radius-card` 12px on `.hud-panel`; `docs/DESIGN_SYSTEM.md` §2 and the
  `hud.design.test.jsx` ratchet were updated together with the CSS.

Deliberate deviations, all recorded:

1. **No decorative fakes.** KPI values, scanner, gesture control and the
   SCAN/ANALYZE/DEPLOY/SECURE buttons are not reproduced without a real
   capability behind them (see §4).
2. **Graceful responsive behaviour.** The reference clips content below
   ~900px; this app collapses rails into a drawer instead (measured — the
   reference has no tablet/mobile layout at all).
3. **Real data only.** Every number shown comes from `/system`, `/health` or
   the session; a value the backend does not report renders as "—".
