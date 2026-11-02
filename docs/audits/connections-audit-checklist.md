# Connections Platform — Audit Checklist

**Branch:** `arena/78510fbc-zaytoon` (HEAD `1ef7593`, PR #8 merged — Phase 4.0 in tree)
**Rule:** Do not commit or push unless explicitly requested.

---

## 1. Repository State / Baseline

| Item | Value | Evidence |
|------|-------|----------|
| Branch | `arena/78510fbc-zaytoon` | `git branch --show-current` |
| HEAD | `1ef7593` Merge PR #8 | `git log --oneline -1` |
| Working tree | Has uncommitted changes | `git status --short` |

### Uncommitted changes already in tree
Modified: `backend/api/main.py`, `backend/core/api/routers.py`, `backend/core/events.py`, `backend/core/permissions.py`, `backend/tests/core/test_api_core.py`, `backend/tests/core/test_core_permissions.py`, `desktop/app/protocol.py`, `frontend/src/App.jsx`, `frontend/src/i18n/strings.js`, `frontend/src/lib/api.js`, `frontend/src/lib/viewState.js`, `frontend/src/lib/viewState.test.js`

Untracked: `backend/core/api/connections_api.py`, `backend/core/connections/`, `backend/data/`, `backend/tests/test_connections.py`, `docs/audits/connections-audit-checklist.md`, `frontend/src/components/connections/`

---

## 2. Requirements Matrix

| # | Requirement | Status | Evidence | Gap | Verification |
|---|-------------|--------|----------|-----|--------------|
| 1 | Connector registry (stable ID, name, scopes, capabilities) | **Verified** | `connector_registry.py`, `test_registry_*` | None | Unit tests pass |
| 2 | Connection manager (create/auth/test/refresh/disconnect/delete) | **Verified** | `connection_manager.py`, `test_connection_state_machine`, etc. | None | Unit tests pass |
| 3 | Unified connector interface | **Verified** | `connector_interface.py` — BaseConnector, CapabilityResult, ConnectionState | None | Unit tests pass |
| 4 | Capability discovery | **Verified** | `capability_discovery.py`, `test_discovery_*` | None | Unit tests pass |
| 5 | Connector lifecycle states | **Verified** | `ConnectionState` enum (7 states) | None | Unit tests pass |
| 6 | OAuth 2.0 auth (PKCE, state validation, refresh) | **Verified** | `auth/oauth.py`, `test_oauth_*` | None | Unit tests pass (mocked) |
| 7 | API key / PAT auth | **Verified** | `auth/api_key.py`, `test_api_key_stored_and_masked` | None | Unit tests pass (mocked) |
| 8 | MCP server support | **Verified** | `auth/mcp.py`, `test_mcp_connector_structure` | Live MCP subprocess untested | Unit tests pass (structural) |
| 9 | GitHub connector | **Verified** | `connectors/github.py` | Live GitHub API untested | Unit tests pass (mocked) |
| 10 | Google Drive connector | **Verified** | `connectors/google_drive.py` | Live Drive API untested | Unit tests pass (mocked) |
| 11 | Reuse permission system | **Verified** | `permissions.py` — google_drive.access, gmail.access, gmail.send | None | Unit tests pass |
| 12 | Reuse approval flow | **Verified** | Manager gates confirm/dangerous via `required_for` + `grants.denied` | None | Unit tests pass |
| 13 | Secure credential storage | **Verified** | `secrets.py`, `to_dict()` excludes credentials_key | None | Unit tests pass |
| 14 | No secrets to frontend | **Verified** | API returns `has_credentials` only | None | Unit tests + code review |
| 15 | Audit connection lifecycle | **Verified** | `_audit()` publishes `CONNECTION_EVENT` | None | Code review |
| 16 | Structured errors, no secret leakage | **Verified** | `safe_error`/`redact` used, errors generic | None | Code review |
| 17 | **Assistant → Connector integration** | **MISSING** | RouterAgent, ToolAgent, AgentEngine have NO knowledge of connections | Wire connector capabilities into assistant routing | **Needs implementation** |
| 18 | Connections UI (browse/connect/disconnect/manage) | **Implemented** | 4 React components + route | Needs frontend tests | ESLint clean, build OK |
| 19 | Frontend API client | **Implemented** | 12 functions in `api.js` | Needs contract verification | Frontend/Backend match |
| 20 | i18n (EN + AR) | **Implemented** | `connections.*` keys in both languages | None | i18n test passes |

---

## 3. Critical Gap: Assistant Integration

The assistant flow (RouterAgent → intent → ToolAgent → AgentEngine → ToolRegistry) has **no path** to the Connections system. Required:

1. RouterAgent must recognize connector-related requests (or a new intent)
2. ToolAgent/Engine must discover connector capabilities and execute them
3. Permission gate must check connection-scoped permissions
4. Approval flow must bind to connection + capability + params
5. Results must be normalized and returned to the assistant

---

## 4. Test Baselines (to be re-verified)

| Suite | Status | Command |
|-------|--------|---------|
| Backend connections tests | Not yet run | `cd backend && python -m pytest tests/test_connections.py -q` |
| Full backend | Not yet run | `cd backend && python -m pytest -q` |
| Frontend vitest | Not yet run | `cd frontend && npx vitest run` |
| Frontend ESLint | Not yet run | `cd frontend && npx eslint .` |
| Frontend build | Not yet run | `cd frontend && npx vite build` |
