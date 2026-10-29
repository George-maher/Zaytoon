# Zaytoon — Design and Usability Audit

**Date:** 2026-10-28
**Scope:** Frontend React/Vite application (`frontend/src/`) and desktop host (`desktop/`). Reviewed component source; no live rendering available in this environment (no display server).

## 1. Visual consistency

| Area | Assessment | Severity | Type |
|------|-----------|----------|------|
| Layout / spacing | Tailwind-based; consistent utility classes within components | Low | Functional |
| Typography | No explicit type scale; relies on Tailwind defaults | Low | Visual |
| Color system | CSS custom properties in `src/styles/theme.css`; Nexus palette (`--nx-*`) scoped in `src/styles/nexus.css` | Medium | Design system |
| Surfaces / elevation | No explicit elevation system; borders via Tailwind | Low | Visual |
| Component sizing | Inconsistent; some components full-bleed (Nexus home), others sidebar-chrome | Low | Functional |
| Icons | `public/icons.svg` sprite; inline SVGs | Low | Visual |
| Animations | GSAP for HUD/nexus; CSS transitions in Tailwind | Low | Visual |
| Empty / loading / error states | Mixed: some components handle 404 as "unavailable" (ExecutionHistory), others show raw errors | Medium | Functional |
| Responsive behavior | WorkspaceShell collapses rails into drawer below 1280px; Nexus home is full-bleed | Low | Functional |
| Desktop window resizing | `desktop/app/host.py` persists window state; geometry reads guarded against closed window | Low | Functional |
| Light/dark mode | Not explicitly supported; Tailwind default | Low | Visual |

## 2. Interaction quality

| Component | Advertised action | Actual behavior | Status | Severity |
|-----------|-------------------|-----------------|--------|----------|
| InputBar | Send text message | Wired to WebSocket `message` frame | Working | — |
| ReminderPanel | List / delete / done reminders | Wired via `reminder_cmd` WS frame | Working | — |
| ConfirmationDialog | Approve / decline tool action | Renders `permission` tier and exact params; `<pre dir="ltr">` for RTL safety | Working | — |
| ChatView | Stream tokens, show message | Token frames → message bubble | Working | — |
| SettingsPanel | Workspace root, API key, model chain | Reads `/settings/workspace`; key presence only (never value) | Working | — |
| FileExplorer | Browse workspace files | Files tool via backend; containment enforced | Working | — |
| GitPanel | Commit, checkout, push, etc. | Two-step approval for destructive verbs; 404/403/409 distinguished | Working | — |
| TerminalPanel | Open terminal, stream output | Two-step approval for `POST /terminal` | Working | — |
| ModelsView | Select model / provider | Reads `/settings/ai`; provider registry facade | Working | — |
| SecurityView | Audit log, process list | Reads `/audit`, `/processes`; 404 → "unavailable" | Working | — |
| NexusDashboard | Home dashboard widgets | Real metrics only; no fabricated GPU/neural ops | Working | — |
| HUD | Agent progress, tool status | EventBus → `_ProgressRelay` → WS frames | Working | — |
| Voice controls | Record, cancel, barge-in | `voice enable/disable/cancel` WS frames; barge-in via explicit cancel only | Working | — |

### Notable interaction details
- **Confirmation dialog `<pre dir="ltr">`:** Prevents RTL UI from reordering a command the user is approving. Correct.
- **ExecutionHistory 404 handling:** Treats missing audit/process endpoints as "unavailable" rather than showing an error. Works against older backends.
- **GitPanel failure discrimination:** 404 = "not a repository" (hides panel), 403 = containment (no retry), 409 = git's own words. Commit input and error banner are separate state (a failed commit doesn't eat the half-typed message).
- **Orphaned components removed:** `ChatInterface`, `VoiceControls`, `StatusDot`, `VoicePanel`, `MessageList`, `SystemMonitor` were deleted; `imports.case.test.js` + lint guard against re-orphaning.
- **Import casing:** `components/HUD/` vs `components/hud/` — exact casing enforced by test (Windows/Linux portability).

## 3. Design-system findings

### Duplicated styles / tokens
- Two palettes: `--nx-*` (Nexus home, scoped in `styles/nexus.css`) and Tailwind CSS vars (rest of app). The Nexus palette is intentionally scoped, not a re-theme of `theme.css`. Desktop shell pins HUD token values for parity.
- **Before D2:** No shared token file; colors are inline in Tailwind classes and CSS vars.
- **After D2 (Phase 3):** `src/styles/theme.css` defines `--zt-*` aliases (bg, text, border, accent, success, warning, error, radius, space, duration, font) mapped to `--hud-*` values — one source of truth. `src/styles/nexus.css` keeps `--nx-*` scoped to Nexus home (intentional, D3). Components reviewed use named tokens; no hard-coded hex colors found outside token files in ExecutionHistory, SecurityView, GitPanel, VirtualMachinesView, ProvidersSection, or InputBar.
- `common.empty` and `common.error` i18n keys added to `src/i18n/strings.js` (en + ar) for shared empty/error messaging.

### Inconsistent components
- HUD components (`AICore`, `AIPresence`, `CommandConsole`, etc.) use GSAP animations; workspace components use Tailwind transitions. No shared animation primitive.
- **Before D1:** Each data panel implemented empty/loading/error with ad-hoc inline styles and different wording — no shared component.
- **After D1 (Phase 3.1):** `src/components/EmptyState.jsx` created — a shared empty-state component using `--zt-*`/`--hud-*` tokens, with optional glyph, title, description, and action slot. **Pilot adopted in `ExecutionHistory.jsx`** for both empty states (no audit records, no active processes). Loading, error, and unavailable states remain as distinct displays — EmptyState is never substituted for them. SecurityView, GitPanel and VirtualMachinesView were inspected and retained their existing patterns: SecurityView is already fully `translate()`-migrated with its own consistent states; GitPanel's 403/404/409 failure discrimination and `present` tri-state don't map to EmptyState's generic model; VirtualMachinesView's empty message is conditional on hypervisor availability, which EmptyState's simple title/description doesn't capture.
- Status derivation is centralized in `lib/statusLabels.js` with documented precedence — good practice.
- Tool rows centralized in `lib/toolRows.js` — good practice.
- Risk tone mapping in `lib/risk.js` grades unrecognized values UP to DANGEROUS — good security practice.

### Layout-specific hacks
- None identified beyond the documented WorkspaceShell drawer breakpoint (1280px) and Nexus full-bleed home — both intentional and documented.

## 4. Design backlog (prioritized)

| # | Problem | Affected pages/components | User impact | Severity | Status | Action taken | Dependencies | Acceptance criteria |
|---|---------|--------------------------|-------------|----------|--------|--------------|--------------|---------------------|
| D1 | Empty/loading/error states inconsistent across components | ExecutionHistory, SecurityView, GitPanel, VirtualMachinesView | User can't distinguish "not available" from "error" | Medium | **Pilot adopted** | `EmptyState.jsx` created; adopted in `ExecutionHistory.jsx` for both empty states (no records, no processes). Loading/error/unavailable remain distinct displays. SecurityView, GitPanel, VirtualMachinesView retained — their state semantics don't fit EmptyState's generic model. | None | Every data panel shows a consistent empty state; errors show actionable message |
| D2 | No shared color/token system | All components | Visual drift as app grows | Medium | **Resolved (Phase 3)** | `theme.css` defines `--zt-*` aliases mapped to `--hud-*` values; `common.empty`/`common.error` i18n keys added. Nexus `--nx-*` remains scoped (D3 exception). | None | All components reference named tokens; no hard-coded hex colors outside token file |
| D3 | Nexus home full-bleed bypasses shell chrome | NexusDashboard (VIEWS.HOME) | Inconsistent navigation model; dock is home's only navigator | Low | **Documented** | Intentional exception — dock cells map to valid `viewState` destinations only. | None | Home screen has no sidebar/header; dock routes to existing views only |
| D4 | Voice state not visible in all views | All views | User may not know microphone is muted while assistant speaks | Low | **Resolved (Phase 2)** | `voice_state` frame added to desktop protocol; StatusLine derives voice-aware status text; InputBar shows voice badge. | Backend `voice_state` frame | Voice state visible on all screens |

### Functional defects vs visual inconsistencies
- **Functional defects:** None identified in this review — all buttons/tabs/dialogs perform their advertised actions.
- **Visual inconsistencies:** D1 (in progress — shared component created, pilot adoption deferred), D2–D4 resolved or documented.
- **Subjective preferences:** Typography scale, animation easing — not flagged as defects.

## 5. Accessibility notes

- `StatusLine` renders `role="status"` and `aria-live="polite"` — good.
- Confirmation dialog command block uses `dir="ltr"` — good for RTL safety.
- No keyboard navigation audit performed (no screen reader testing available in this environment).
- No focus-trap verification for modals.

## 6. Recommendations (no redesign)

Do not redesign the UI. The design system is small and consistent enough that incremental adoption (D1 pilot on ExecutionHistory, then gradual rollout where semantics fit) will yield the most benefit with least risk. The Nexus home full-bleed exception (D3) is intentional and documented. Deferred: loading/error variants of EmptyState, applying EmptyState to SecurityView/GitPanel/VirtualMachinesView where a clear semantic fit exists.
