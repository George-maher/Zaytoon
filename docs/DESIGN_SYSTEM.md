# Zaytoon — Shared Design System

One visual language, two clients. This document is the contract; the CSS in
`frontend/src/styles/` and `desktop/app/theme.py` are two renderings of it.

The reference is a *cybercore tactical HUD*: near-black, luminous cyan, a
central circular core, peripheral information modules, hairline geometry and
microtext. It is deliberately **not** glassmorphism — the reference has no
translucent blurred panels, and an earlier iteration of this UI used exactly
that, which is why the two are now visually distinct.

---

## 1. Tokens

### Surfaces

| Token | Value | Use |
|---|---|---|
| `--hud-void` | `#03070D` | Page ground, behind everything |
| `--hud-deep` | `#06111A` | Panel fill |
| `--hud-raised` | `#081722` | Raised module, active row |
| `--hud-line` | `#0C1E2C` | Hairline dividers, grid |

### Accents

| Token | Value | Use |
|---|---|---|
| `--hud-cyan` | `#00E5FF` | Primary luminous accent, focus, active state |
| `--hud-cyan-core` | `#00CFFF` | Headline accent, identity |
| `--hud-blue` | `#168DFF` | Secondary structure, inactive ring segments |
| `--hud-indigo` | `#2878FF` | Deepest structural blue |
| `--hud-amber` | `#FFB020` | Caution, awaiting approval, tool execution |
| `--hud-green` | `#00FF9C` | Nominal, connected, listening |
| `--hud-red` | `#FF3B5C` | Offline, error, denial |

The reference's cyan sits closer to a light teal than to azure, which is why
the primary moved from `#00D4FF` to `#00E5FF`/`#00CFFF`.

### Text

| Token | Value | Contrast on `--hud-void` |
|---|---|---|
| `--hud-text` | `#E8F7FF` | ~17:1 — primary prose |
| `--hud-text-dim` | `#A8C7D8` | ~9:1 — secondary, labels |
| `--hud-text-mute` | `#63879A` | ~4.6:1 — telemetry microtext, **lower bound** |

Nothing in the interface may use a colour dimmer than `--hud-text-mute` for
text that carries meaning. Decorative geometry may go lower.

### Lines and glow

```css
--hud-edge:        rgba(0, 210, 255, 0.15);  /* panel border          */
--hud-edge-strong: rgba(0, 210, 255, 0.30);  /* focused / active      */
--hud-edge-faint:  rgba(0, 210, 255, 0.07);  /* grid, tick marks      */
--hud-glow-soft:   rgba(0, 229, 255, 0.18);  /* resting inner glow    */
--hud-glow-hard:   rgba(0, 229, 255, 0.45);  /* active core bloom     */
--hud-amber-glow:  rgba(255, 176, 32, 0.30);
--hud-red-glow:    rgba(255, 59, 92, 0.30);
--hud-green-glow:  rgba(0, 255, 156, 0.28);
```

Glow is used at rest on at most one element per region. A screen where
everything glows has no focal point.

---

## 2. Panel geometry

Panels are **soft rectangles** — the Figma reference (NEXUS AI) rounds every
panel and tile.

- Border: 1px `--hud-edge`. Radius comes from the shared scale
  (`--radius-panel` 16px for shells, `--radius-card` 12px for panels/tiles,
  10px chips, 14px action buttons); a component never invents its own.
- Corners may additionally be bracketed with 8px L-shaped corner rules on
  full-bleed instrument views; the bracket reads as instrumentation rather
  than decoration.
- Fill: `--hud-deep` at 0.72 alpha over the void, **no** `backdrop-filter`.
  Panels are meant to look like glass over a dark instrument bay, which the
  reference achieves with flat fill plus a 1px lit edge, not with blur.
- Header strip: 22px tall, uppercase 9px, letter-spaced `0.22em`, left-aligned,
  with a 1px rule beneath and a 2px accent bar at its left edge.
- Every panel may carry a small state LED and an optional right-aligned
  telemetry read-out.

`clip-path` notches are applied to the *frame*, never to interactive content,
so hit areas stay rectangular and keyboard focus rings are never clipped.

---

## 3. Type

| Role | Family | Size | Tracking | Case |
|---|---|---|---|---|
| Identity | display | 13px | 0.34em | upper |
| Panel header | mono | 9px | 0.22em | upper |
| Telemetry value | mono | 10px | 0.04em | as-is |
| Prose | body | 13px/1.6 | 0 | sentence |
| Micro-label | mono | 8px | 0.18em | upper |

**Not everything is uppercase.** Panel headers, identity and telemetry labels
are; conversation prose and error messages are not. A wall of uppercase
uppercase is what makes "sci-fi UI" illegible, and the reference is legible
because its microtext is *short*.

Minimum rendered size is 8px, used only for non-essential micro-labels. Any
text a user must act on is ≥10px.

---

## 4. The core

Concentric, centred, and the only element allowed to animate freely.

```
  outer segmented ring      1px ticks, 60 segments, 8 active
  telemetry ring            arc showing real progress/state dwell
  sweeping ring             1 revolution / 12s idle → 1.5s thinking
  waveform band             live when listening or speaking
  identity                  "ZAYTOON" + state word
  status LED                colour + shape, never colour alone
```

State is carried by **four** redundant channels — hue, ring speed, the state
word, and the LED's glyph — so it survives colour-blindness and greyscale.

| State | Ring speed | Hue | Word |
|---|---|---|---|
| offline | still | red | OFFLINE |
| idle | 12s | cyan | READY |
| listening | 4s | green | LISTENING |
| thinking | 1.5s | cyan | PROCESSING |
| executing | 2s | amber | RUNNING TOOL |
| speaking | 3s | cyan | SPEAKING |
| awaiting approval | 2s | amber | CONFIRM |
| error | 1s pulse | red | ERROR |

---

## 5. Layout

```
┌──────────────────────────────────────────────────────────┐
│ HUDHeader — identity · reminders · lang · state LED       │
├────────────┬───────────────────────────┬─────────────────┤
│ Left rail  │        AI CORE            │  Right rail     │
│ agents     │  (conversation beneath)   │  model, voice,  │
│ tools      │                           │  backend, tools │
│ telemetry  │                           │                 │
├────────────┴───────────────────────────┴─────────────────┤
│ CommandConsole — activity strip · input · mic · send      │
└──────────────────────────────────────────────────────────┘
```

Rails collapse below 1024px into a tabbed drawer; the core and console are
never removed. Below 640px the console input stays ≥44px tall for touch.

---

## 6. Motion

- Transform and opacity only. No animated `filter`, `box-shadow` or `width`.
- Durations: 120ms micro, 240ms panel, 600ms core transition.
- `prefers-reduced-motion: reduce` freezes every rotation, sweep and pulse to a
  static frame. State remains legible through colour, glyph and text alone.
- Nothing loops faster than 1s, and at most three elements animate
  simultaneously on the core.

---

## 7. Voice

Voice is a first-class HUD region, not a button. Pressing the microphone
enters **direct** capture — there is no wake word in manual mode, and the UI
must not imply one. The prompt is `> SPEAK`, never `> SAY "ZAYTOON" FIRST`.

Recording is indicated by a live waveform plus the word LISTENING plus a
pulsing ring, so it is never colour-only.

---

## 8. Real data

Every dynamic read-out maps to a real source. Decorative geometry (grids,
ticks, corner brackets, background traces) is static and must be visually
distinct from telemetry — telemetry is monospace, brighter, and labelled.

| Read-out | Source |
|---|---|
| State LED, state word | `useWebSocket().status` + voice flags |
| Model in use | `GET /health` → `llm.primary_model` |
| Fallback chain | `GET /health` → `llm.chain` |
| Tool counts by tier | `GET /health` → `tools_by_tier` |
| STT / TTS | `GET /health` → `voice` |
| Sandbox / gate | `GET /health` → `safety` |
| Workspace root | `GET /health` → `workspace` |
| Request metrics | `GET /health` → `llm.metrics` |
| Reminders | `reminder_list` frame |
| Plan / active tool | `plan`, `tool_started` frames |

No read-out is ever a placeholder presented as a measurement. If a value is
unavailable the row shows `—`, which is a real answer.
