# Google Drive / Gmail OAuth Design

**Status:** Design — Phase 4.0  
**Applies to:** `backend/`, `desktop/`, `frontend/`

## 1. Purpose

Zaytoon needs to read and, with explicit user approval, write files in Google Drive and
messages in Gmail. This document defines the OAuth 2.0 integration: how credentials are
obtained, stored, used, and revoked; how the existing permission and confirmation layers
gate every call; and what the desktop and frontend components must do.

## 2. Goals

- **User consent first.** No Google API call happens without an explicit grant recorded
  in Zaytoon's permission system.
- **Least privilege.** Only the scopes the user actually approved are requested.
- **No silent exfiltration.** Every Drive/Gmail action is subject to the same tool
  registry tier and confirmation flow as local file operations.
- **Revocable.** The user can revoke access from Zaytoon settings and from Google's
  account management page.
- **Local-first default.** OAuth is an *opt-in integration*, not the default path. The
  backend continues to prefer local (Ollama) and free-tier OpenRouter models.

## 3. Non-goals (explicitly out of scope for Phase 4.0)

- Sending user data to Google for model training or AI features — Zaytoon's LLM routing
  never uses Google models unless the operator explicitly configures one.
- Automatic background sync of Drive/Gmail content — access is request-by-request only.
- Gmail sending without a separate confirmation tier — outbound mail is `dangerous`.

## 4. OAuth flow

Zaytoon acts as an OAuth 2.0 **authorization code grant** client against
`accounts.google.com`. The flow:

```
1. User enables "Google Drive" or "Gmail" in Zaytoon settings.
2. Desktop host opens the system browser at Google's authorization endpoint with:
     client_id, redirect_uri (http://localhost:PORT/oauth2callback),
     scope (drive.file or gmail.readonly, etc.),
     access_type=offline, prompt=consent
3. User signs in to Google and approves the scopes.
4. Google redirects to the localhost redirect URI with an authorization code.
5. The desktop host (or a small local callback server) exchanges the code for
   access_token + refresh_token at Google's token endpoint.
6. Tokens are stored in `core/secrets.py` (DPAPI / 0600 credential storage).
7. Zaytoon records the grant as a named permission (see section 5).
```

The redirect URI uses `http://localhost` on a port from the desktop's `DESKTOP_PORT_RANGE`
(5175–5179) so the backend's CORS allowlist already recognises it.

## 5. Permission model

Two new named permissions are added to `backend/core/permissions.py`:

| Permission | Tier | Meaning |
|---|---|---|
| `google_drive.access` | `confirm` | Read files/folders in Drive; write only with separate `filesystem.write` |
| `gmail.access` | `confirm` | Read Gmail messages; send mail is `dangerous` (`gmail.send`) |

These follow the existing pattern: permissions are **derived**, not declared —
`required_for()` maps each Google tool to its permission based on tier. A tool cannot
acquire a tier without acquiring the matching permission, and it cannot subtract
permissions.

### Confirmation behavior

Every Google Drive/Gmail tool call goes through `RiskAssessor` + `ConfirmationManager`:

- **Read-only** (list files, read message): `confirm` tier — the user is prompted once
  per session, then the grant is remembered.
- **Write** (create file, upload, send email): `dangerous` tier — prompted every time,
  fail-closed on timeout/disconnect.

The existing `require_confirmation` WebSocket event carries the action, parameters,
reasons, and summary; the client shows the confirmation dialog before anything is sent
to Google.

## 6. Token storage

Tokens are stored via `backend/core/secrets.py`, which already provides:

- **Windows:** DPAPI (Data Protection API) — encrypted to the user's logon credentials.
- **POSIX:** `0o600`-protected JSON file in the secrets path.
- **No route returns a secret** — `GET` endpoints never echo the token back.

Stored per-integration:

```json
{
  "google_drive": {
    "access_token": "...",
    "refresh_token": "...",
    "expires_at": 1735689600,
    "scope": "https://www.googleapis.com/auth/drive.file",
    "granted_at": "2026-10-29T12:00:00Z"
  },
  "gmail": {
    "access_token": "...",
    "refresh_token": "...",
    "expires_at": 1735689600,
    "scope": "https://www.googleapis.com/auth/gmail.readonly",
    "granted_at": "2026-10-29T12:00:00Z"
  }
}
```

Access tokens are refreshed using the refresh token before they expire; a 401 from
Google triggers an automatic refresh retry once.

## 7. Tool layer

New tools are added under `backend/tools/` following the existing `validate → gate →
execute` ordering:

| Tool | Action | Tier | Permission | Google scope |
|---|---|---|---|---|
| `google_drive.list` | List files/folders | `confirm` | `google_drive.access` | `drive.file` |
| `google_drive.read` | Read file content | `confirm` | `google_drive.access` | `drive.file` |
| `google_drive.create` | Create file/folder | `dangerous` | `google_drive.access` + `filesystem.write` | `drive.file` |
| `gmail.list` | List messages | `confirm` | `gmail.access` | `gmail.readonly` |
| `gmail.read` | Read message | `confirm` | `gmail.access` | `gmail.readonly` |
| `gmail.send` | Send email | `dangerous` | `gmail.access` + `gmail.send` | `gmail.send` |

Each tool's `execute()` builds an authorised `httpx` client with a Bearer token from
secrets, calls the Google REST API (`https://www.googleapis.com/drive/v3/...` or
`https://gmail.googleapis.com/gmail/v1/...`), and returns a `ToolResult`.

## 8. Desktop + frontend integration

### Desktop host (`desktop/app/bridge.py`)

The bridge does **not** add OAuth methods. OAuth happens in the system browser (not the
WebView), so the bridge's existing `open_url()` is sufficient to launch the authorization
URL. After the redirect, a tiny local callback server on the desktop port range captures
the code and the host exchanges it.

Bridge additions:

- `get_oauth_status(provider: str) -> dict` — returns `granted: bool`, `scope`, `expires_at`
  (no token material).
- `revoke_oauth(provider: str) -> bool` — deletes stored tokens and the permission grant.

### Frontend (`frontend/src/`)

A new settings panel section ("Connected services") lists Google Drive and Gmail with:

- **Connect** button → opens the browser auth URL via `bridge.open_url()`.
- **Status** chip — connected / expired / error.
- **Disconnect** button → calls `bridge.revoke_oauth("google_drive")` / `"gmail"`.

No new translation keys are added beyond what the settings panel already uses; every new
user-facing string goes through `src/i18n/strings.js`.

### Backend routes

No new top-level routes are needed: Google tools are registered in the single
`ToolRegistry` and appear at `GET /tools`. The OAuth token exchange happens through the
desktop host's local callback, not through a backend route — the backend only consumes
stored tokens.

## 9. Security properties

| Property | How it's enforced |
|---|---|
| No silent data exfiltration | Every Google call is a gated tool; reads require `confirm`, writes `dangerous` |
| No credential leakage | Tokens stored in `core/secrets.py` (DPAPI / 0600); never returned by any API |
| Scope-limited | Only the scopes the user approved are requested |
| Revocable | User can revoke from Zaytoon settings or Google's account page |
| Fail-closed | Token refresh failure → `requires_confirmation` → user re-consents |
| No auto-sync | Access is request-by-request; no background polling of Drive/Gmail |

## 10. What is NOT done in Phase 4.0

- **No OAuth implementation code yet.** This document is the design. The Phase 4.0 DoD
  requires the design to be *documented*, not implemented.
- **No Gmail send** — outbound mail is `dangerous` and deferred to a later phase.
- **No Google as an LLM provider** — Zaytoon's provider registry is `ollama` /
  `openai_compatible` / `openrouter`; Google is not added as a model provider.

## 11. Files to touch when implementing

- `backend/core/permissions.py` — add `GOOGLE_DRIVE_ACCESS`, `GMAIL_ACCESS`, `GMAIL_SEND`
- `backend/tools/` — new Google tool modules
- `backend/core/secrets.py` — already sufficient; verify `set()`/`get()` for tokens
- `desktop/app/bridge.py` — `get_oauth_status()`, `revoke_oauth()`
- `frontend/src/components/SettingsPanel.jsx` — connected services section
- `frontend/src/i18n/strings.js` — new bilingual strings

## 12. Verification

When implemented:

- `backend/tests/test_oauth_flow.py` — code exchange, token refresh, revocation
- `desktop/tests/test_bridge_oauth.py` — `get_oauth_status`, `revoke_oauth`
- `frontend/src/components/SettingsPanel.test.jsx` — connect/disconnect UI
- Manual: `python scripts/smoke_oauth.py` — end-to-end with a real Google project
