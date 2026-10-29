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
- `EmptyState` glyph is `aria-hidden="true"`; title uses semantic `<h3>`; no interactive elements by default — keyboard accessible.
- No keyboard navigation audit performed (no screen reader testing available in this environment).
- No focus-trap verification for modals.

## 6. Phase 3.2 — Browser-Based Visual QA

### Phase 3.2.1 — Initial setup (environment limitation)

**Visual QA is BLOCKED in this sandbox.** No display server (`DISPLAY` unset), no browser binary installed (firefox, chromium, etc.), and Playwright's Chromium cannot be downloaded (outbound network restricted to github.com/registry.npmjs.org/pypi.org; Playwright binaries serve from `cdn.playwright.dev`).

No rendered UI could be inspected. All findings below are from source inspection, unit tests, lint, and production build — none substitute for visual verification.

### Phase 3.2.2 — Workflow and test validation

Audited the Playwright visual-QA test setup and GitHub Actions workflow for correctness, runnability, and failure detection.

#### Workflow findings and corrections

| Finding | Severity | Correction |
|---------|----------|------------|
| Arabic RTL test used `?lang=ar` URL param, but the app reads language from `localStorage.getItem("zaytoon-language")` (see `src/hooks/useWebSocket.js`). Test would fail in a real browser. | High | Fixed: tests now set `localStorage` before navigation via `page.evaluate()`. |
| Missing scenarios: Mobile Arabic/RTL, ExecutionHistory loading/error/unavailable states, EmptyState visibility with content assertions. | Medium | Fixed: added all missing scenarios with API mocking via `page.route()`. |
| Workflow installed playwright via separate `npm install --save-dev` outside `npm ci`, causing package.json/package-lock.json drift in CI. | Low | Fixed: added `playwright` to `frontend/package.json` devDependencies; `npm ci` now installs it. |
| No explicit `permissions:` block in the workflow. | Low | Fixed: added `permissions: { contents: read }`. |

#### Corrected files

- `frontend/tests/visual-qa.spec.js` — Rewrote with proper language handling (localStorage), API mocking for ExecutionHistory states, and all required scenarios (desktop/mobile × en/ar × empty/loading/error/unavailable/accessibility).
- `.github/workflows/ci-visual-qa.yml` — Added `permissions` block, removed separate playwright install (now via `npm ci` from devDependencies).
- `frontend/package.json` — Added `playwright` to devDependencies.
- `frontend/package-lock.json` — Updated to include playwright (pre-existing `libc` field removals preserved).

#### Test coverage by scenario

| Scenario | Covered | Method |
|----------|---------|--------|
| Desktop English / LTR | ✅ | App loads, `dir=ltr`, `lang=en`, no console errors |
| Desktop Arabic / RTL | ✅ | localStorage set to "ar", `dir=rtl`, `lang=ar`, no console errors |
| Mobile English / LTR | ✅ | No horizontal overflow at 375×667 |
| Mobile Arabic / RTL | ✅ | localStorage set to "ar", no horizontal overflow at 375×667 |
| ExecutionHistory empty state | ✅ | Mock `/audit` → `{entries: [], total: 0}`, EmptyState renders |
| ExecutionHistory loading state | ✅ | Mock `/audit` → never resolves, "LOADING…" shows, EmptyState suppressed |
| ExecutionHistory error state | ✅ | Mock `/audit` → network failure, error message shows, EmptyState suppressed |
| ExecutionHistory unavailable state | ✅ | Mock `/audit` → 404, "Audit log unavailable" shows, EmptyState suppressed |
| EmptyState accessibility | ✅ | Semantic `<h3>` heading, glyph `aria-hidden="true"` |
| Theme token resolution | ✅ | `--zt-*`/`--hud-*` CSS custom properties resolve on `:root` |

#### What remains unverified (requires real browser)

| Item | Why |
|------|-----|
| Rendered EmptyState typography, spacing, glyph alignment | No browser available |
| RTL text shaping and alignment in Arabic | No browser available |
| Responsive behavior at 1280px / 375px viewports | No browser available |
| Color contrast ratios | No browser available |
| Horizontal overflow, clipped content, overlapping elements | No browser available |
| Browser console errors in live app | No browser available |
| Playwright test execution and screenshots | No browser available |

#### Reproducible execution in GitHub Actions

The `.github/workflows/ci-visual-qa.yml` workflow is ready to run in a browser-capable environment. It:
1. Installs Node 20 + dependencies (including playwright from devDependencies)
2. Installs Chromium via `npx playwright install --with-deps chromium`
3. Builds the frontend (`npm run build`)
4. Starts the Vite preview server on port 5173
5. Waits for server readiness (curl loop, 30s timeout)
6. Runs `npx playwright test tests/visual-qa.spec.js`
7. Uploads the Playwright report as an artifact (`if: always()`)

Requirements: Xvfb/display server on Linux, network access to `cdn.playwright.dev` for Chromium download, no secrets needed.

### What was verified without a browser

| Check | Method | Result |
|-------|--------|--------|
| EmptyState uses theme tokens only | Source inspection | ✅ All `--zt-*`/`--hud-*`/`--space-*`/`--font-*` |
| ExecutionHistory five-state distinction | Source inspection | ✅ Empty/loading/error/unavailable/no-records all distinct |
| EmptyState RTL safety | Source inspection | ✅ Inherits `dir` from `<html>`; no directional styles |
| i18n key completeness (en + ar) | Source inspection + ratchet test | ✅ Identical keys in both languages |
| No hardcoded hex outside Nexus (D3) | `grep` across `src/components/` | ✅ Only Nexus scoped palette |
| Frontend tests | `npx vitest run` | ✅ 756 passed / 0 failed (44 files) |
| Frontend lint | `npm run lint` | ✅ Clean (0 errors, 0 warnings) |
| Frontend production build | `npm run build` | ✅ OK |
| Workflow correctness | Source inspection of ci-visual-qa.yml | ✅ Triggers, install, build, readiness, failure detection, artifact upload all verified |
| Test coverage gaps identified | Source inspection of visual-qa.spec.js | ✅ All gaps fixed |

## 7. Phase 3.3 — Accessibility & UX Engineering

### Scope

WCAG 2.2 Level AA target (engineering target, not formal certification). Prioritized by impact on a user's ability to understand, navigate, and operate Zaytoon:

- **P0:** A user cannot access or complete a critical interaction because of an accessibility blocker.
- **P1:** Keyboard, screen-reader, focus, form-feedback, or serious usability problems.
- **P2:** Significant consistency, responsiveness, clarity, or interaction-quality problems.
- **P3:** Minor polish that does not introduce unnecessary complexity.

### Findings and fixes

| ID | Severity | Finding | Affected files | Fix |
|----|----------|---------|----------------|-----|
| A1 | P1 | ExecutionHistory loading state (`LOADING…`) is a plain `<p>` — no `role`/`aria-live`, so screen readers are not notified when loading starts or ends. | `src/components/ExecutionHistory.jsx` | Added `role="status"` + `aria-live="polite"` |
| A2 | P1 | ExecutionHistory error state (`Audit fetch failed: …`) is a plain `<p>` — no `role`/`aria-live`, so fetch failures are not announced. | `src/components/ExecutionHistory.jsx` | Added `role="alert"` + `aria-live="assertive"` |
| A3 | P1 | ExecutionHistory unavailable state (`Audit log unavailable`) is a plain `<p>` — no `role`/`aria-live`. | `src/components/ExecutionHistory.jsx` | Added `role="alert"` + `aria-live="polite"` |
| A4 | P1 | Sidebar collapsed nav items expose only `title`, no `aria-label` — screen readers may not announce icon-only buttons. | `src/components/Sidebar.jsx` | Added `aria-label={item.label}` when collapsed |
| A5 | P2 | `EmptyState` has no `role` on its container — assistive tech cannot identify it as a named region. | `src/components/EmptyState.jsx` | Added `role="region"` + `aria-label` (defaults to `title`, overridable via `ariaLabel` prop) |
| A6 | P2 | `EmptyState` in `ExecutionHistory` lacks explicit accessible names. | `src/components/ExecutionHistory.jsx` | Pass `ariaLabel` from `translate()` to both EmptyState usages |

### What remains unverified

| Item | Why |
|------|-----|
| Rendered focus indicators against actual component backgrounds | No browser available |
| Keyboard navigation through full UI (tabs, dialogs, menus) | No browser available |
| Screen reader announcements for loading/error/unavailable state changes | No browser available |
| RTL text shaping and alignment in Arabic | No browser available |
| Color contrast ratios on rendered components | No browser available |
| Playwright keyboard/accessible-name test execution | No browser available |

### Tests added

| File | Tests | What they verify |
|------|-------|------------------|
| `src/components/ExecutionHistory.test.jsx` | 5 new | Loading state has `role="status"`/`aria-live="polite"`; error state has `role="alert"`/`aria-live="assertive"`; unavailable state has `role="alert"`/`aria-live="polite"`; EmptyState renders as `role="region"` with accessible name in en and ar |
| `src/components/EmptyState.test.jsx` | 10 new | Title/description/glyph rendering; region role + accessible name; `aria-hidden` on glyph; semantic `<h3>` heading; action slot; Arabic labels |
| `src/components/Sidebar.test.jsx` | 3 new | Collapsed nav items have accessible names (en + ar); collapse/expand toggle exposes `aria-expanded` |

### Test results

| Check | Result |
|-------|--------|
| Frontend tests | ✅ 774 passed / 0 failed (46 files) — was 755 / 44 files at baseline |
| Frontend lint | ✅ Clean (0 errors, 0 warnings) |
| Frontend production build | ✅ OK (pre-existing chunk-size warning only) |
| Playwright visual QA | ⬜ BLOCKED — no browser available (see Phase 3.2) |

## 8. Recommendations (no redesign)

Do not redesign the UI. The design system is small and consistent enough that incremental adoption will yield the most benefit with least risk. The Nexus home full-bleed exception (D3) is intentional and documented.

**Immediate next steps:**
1. Run `.github/workflows/ci-visual-qa.yml` in a browser-capable environment to execute the Playwright keyboard/accessibility tests.
2. Consider applying `role="region"` + `aria-label` to other data panels (SecurityView, GitPanel, VirtualMachinesView) where they represent distinct sections.
3. Verify focus indicators are visible against all component backgrounds in a real browser.
