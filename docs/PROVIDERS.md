# AI providers: the multi-provider registry

Zaytoon answers through one provider registry (`core/providers/`), not a pile of
environment variables. A provider is a row: a kind, a base URL, optional
credentials, optional timeout and organisation id. Models are rows against it.
Routing reads the rows; the transport reads the same rows; the settings UI
reads and writes the same rows. There is no second configuration system — the
legacy `GET/POST /settings/ai` is a **facade over the registry**, so an old
client and the new one cannot disagree.

> **Verification status.** Everything on this page is implemented and covered
> by the backend suite (`tests/core/test_provider_client.py`,
> `tests/core/test_core_providers.py`, `tests/core/test_llm_registry_routing.py`,
> `tests/core/test_api_core.py`) against `httpx.MockTransport` — real requests,
> real headers, real payloads, no sockets. **OpenRouter was not reachable with
> live credentials during development, so the hosted path is verified against
> the mock only.** The local path (`openai_compatible` pointed at Ollama's own
> `/v1`) is the intended live proof and is exercised in W10. Treat a live
> OpenRouter run as until-proven.

## Provider kinds

| Kind | What it is | Free-only policy |
|---|---|---|
| `ollama` | A local daemon (`http://localhost:11434`) | n/a |
| `openai_compatible` | Any OpenAI-compatible endpoint — llama.cpp, LM Studio, a NAS | n/a |
| `openrouter` | OpenRouter | **yes, enforced** |

All three speak the same wire protocol (`/v1/chat/completions`), so one
transport client serves them; the *kind* only decides policy.

## Endpoint validation (`validate_provider`)

A base URL is refused — at save time, with the reason — when it:

- is not `http(s)`;
- carries credentials (`user:pass@`);
- carries a query string or fragment (httpx joins paths by raw concatenation, so
  a query would be swallowed into the first appended segment and a fragment
  never reaches the wire);
- targets the **cloud metadata plane**: `169.254.0.0/16` including
  `169.254.169.254` (AWS/OpenStack) and `169.254.171.1` (Azure), IPv6 link-local
  and IPv4-mapped link-local, and `metadata.google.internal`. A request pointed
  there returns the host's cloud credentials **in the error body**, which travels
  into the attempt record and the log.

What is *not* refused: loopback and private addresses. A local endpoint is the
primary use case — blocking it would break the product. Known residual: a
hostname that *resolves* to the link-local plane is not caught (save-time DNS
would be slow, flaky, and re-checkable after the fact).

**Canonicalisation** strips a trailing `/v1` *segment* — parsed, not
string-sliced, so a host literally named `v1` keeps its authority — and is
applied both at validation and in the transport's constructor, so a legacy
database row behaves the same as a fresh one. The client always appends
`/v1/...` itself; users paste what their provider documents.

**Redirects are pinned off** (`follow_redirects=False`, set explicitly rather
than relied on as a default): a registered endpoint must answer for the
authority that was judged at save time. A 302 is reported, never chased —
chasing it would hand the request, including the `Authorization` header, to an
address nobody approved. There is a test that is the tripwire if an httpx
default ever changes.

## Discovery: the whole catalog, refusals visible

`POST /providers/{id}/models/refresh` asks the endpoint what it has and records
**every** model, with one exception that is never an omission:

- A model the **free-only policy** refuses (OpenRouter: not `<vendor>/<model>:free`,
  not allow-listed, or an uncontrolled auto-router) is stored with
  `enabled = 0` and the policy's own words in `policy_reason`. It cannot be
  enabled later — every write that would turn a model on runs the same check as
  a raiser — so the row is a receipt, not a loophole. A catalog where the
  refusals are invisible is a catalog the user cannot audit.
- Refresh reports `discovered / added / refused_by_policy / skipped` so the
  summary line can say "200 discovered, 31 recorded, 169 refused by policy"
  instead of a number that hides a policy decision.

**Capabilities come from claims, not folklore.** OpenAI-compatible catalogs
don't send a `capabilities` field; they send `supported_parameters` and
`architecture`. `tools` in `supported_parameters` grants the `tools`
capability; `response_format` grants `structured_output`; `image` in
`input_modalities` grants `vision`. A model that reports nothing gets the
chat-completions baseline (`chat`, `streaming`) and *nothing more* — granting
assumed capabilities is how a model gets routed to a task it cannot do.

**Pricing and parameters are recorded as metadata** (`models.metadata_json`),
verbatim, never acted on: they are what the endpoint *claims*. A model row is:

```
{ provider_id, id, name, capabilities, context_length, enabled, priority,
  metadata: { pricing?, supported_parameters?, modality?, ... },
  policy_reason, discovered_at }
```

## Routing

`plan()` reads the registry and returns a `RoutingPlan`: the chosen
`{provider_id, provider, base_url, model, capabilities}` plus **every candidate
and the reason it was skipped** — that list is what makes "why did it pick that
model?" answerable (`GET /models/routing`). Order:

1. the default provider (or an explicitly requested one), its models by
   `priority`;
2. each candidate re-checked against the request's required capabilities
   (`has_all`) *and* against the policy — a disabled or policy-refused model is
   not routable no matter what the row says;
3. an enabled provider with no usable models is reported as its own error
   ("all models disabled — some by policy"), which is a different fix from
   "no models recorded".

Tier pins (`GET/POST /models/tiers`) override priority per tier and are
validated at write time.

## Transport

One client (`core/providers/client.py`), three knobs worth knowing:

- **Timeouts.** `timeout_s` per provider (1–600s, NULL = 120s default) bounds
  non-streaming requests. For streams it bounds *connect* and, when tighter,
  the **inter-chunk silence**: `read = min(timeout_s, STREAM_IDLE_TIMEOUT=90s)`.
  A whole stream is unbounded by design (a long generation is normal); silence
  is not — a model that opens a stream and then says nothing fails in
  `read_bound` seconds with `Stream stalled: no data for Ns` instead of holding
  the turn open forever. The guard applies twice, deliberately: as the httpx
  read timeout (covers real sockets) and around each line read with
  `asyncio.wait_for` (covers any transport, including test doubles).
- **`org_id`** (≤120 chars) is sent as the `OpenAI-Organization` header. It is
  an identifier, not a secret, and is stored on the row rather than in the
  secrets file.
- **Native tool calls** (OpenAI `tool_calls` fragments) are accumulated across
  chunks and rendered into Zaytoon's JSON tool-call dialect **off the wire** —
  they reach the validator and the engine, never the screen. This keeps the
  engine's "tool calls are not prose" rule structural rather than prompt-level.

## Failure taxonomy

Every transport failure carries a `kind` in the same vocabulary the attempt
records, cooldowns and metrics use, produced **at the failure site where the
cause is known**:

| Kind | Meaning | Retryable | Cooldown |
|---|---|---|---|
| `rate_limited` | HTTP 429 | yes | long (`LLM_RATE_LIMIT_COOLDOWN`) |
| `server_error` | 5xx / 529 / 408 / 425 | yes | provider default |
| `fatal` | 400 / 401 / 402 / 403 / 404 / 405 / 413 / 415 / 422 | no | — |
| `timeout` | read timeout, stream stall | yes | provider default |
| `connect_error` | connection refused/DNS | yes | provider default |
| `malformed` | non-JSON body, empty choices | no (body) | — |

The registry path used to *string-match* exception messages to classify
(`"Timeout" in detail`), so rewording a message silently re-labelled every
future failure. `_registry_outcome` now reads `exc.kind` first; the status and
message fallbacks remain only for exceptions built without one.

## Setup: reachability is not proof

Two probes, two columns, one rule:

| Route | Proves | Sets |
|---|---|---|
| `POST /providers/{id}/test` | the endpoint answers (`GET /models`) | `verified_at` |
| `POST /providers/{id}/test-chat` | a model actually generates | `chat_verified_at` **and** `verified_at` |

**First-run setup completes only on `test-chat`.** A 200 from a models-list
endpoint says nothing about whether the key may *generate*, whether the model
exists, or whether the balance covers it — and each of those failures is
exactly what a new user is setting up. `GET /setup/status` therefore reads
`chat_verified_at`:

```json
{ "setup_required": true, "providers_total": 1, "providers_enabled": 1,
  "providers_chat_verified": 0, "verified_at": null }
```

The first-run wizard (frontend) completes on this and nothing else, and can be
skipped. **Existing installs are stamped by migration 5** — a provider that
already has recorded models has served this user before, so its
`chat_verified_at` is set from `updated_at`; a provider without models is a
fresh configuration and must still test. The stamp SQL is a named constant
(`SETUP_STAMP_SQL`) precisely because it is the part with a decision in it.

`test-chat` picks the first enabled model (or one from the body), sends one
32-token completion, and returns `{ ok, connected, model, latency_ms, reply,
verified_at }` — or `{ ok: false, error }` with the endpoint's own words. A
failure is a 200, not a 500: the endpoint being broken is the answer.

## The local→hosted guard

The registry default is the user's statement about where this install's
traffic belongs. When the default provider is **local** — `kind: ollama`, or an
`openai_compatible` whose host is loopback/private by address class — and it
cannot answer, the request **refuses** with a controlled bilingual message
(`kind: local_default_blocked`) rather than falling through to a hosted chain:

> Your default AI provider is local and it cannot answer right now. Zaytoon did
> not switch to a hosted model on its own — start the local provider, or choose
> a hosted one in Settings.

Handing the turn to a cloud model there would send data out of the machine the
user never chose to send — a fallback that is silent by definition. What is
unchanged: hosted→local fallback, and fallback between models within a provider
(both visible in Settings and in the attempt log). An unreadable registry never
triggers the guard — it must never be the *reason* a request fails.

## Credentials

`api_key` goes to `core/secrets` (DPAPI on Windows, 0600 elsewhere) and **no
route returns it**. A provider row carries `has_api_key: true/false` and
nothing else; the settings UI's key field is write-only. `org_id` is not a
secret and lives on the row.

## There is no tool path

No tool can create, edit or delete a provider. An endpoint is an SSRF decision:
whoever can register one can point the backend's HTTP client at a host of their
choosing. That authority belongs to the human, at the settings screen — never
to a model selecting a tool. `tests/test_tool_registry.py` contains the
tripwire that fails if one is ever added.

## API reference

| Route | What |
|---|---|
| `GET /providers` | every provider, no key material |
| `POST /providers` | create/update (`provider_id` for update), full validation |
| `DELETE /providers/{id}` | remove (models cascade) |
| `POST /providers/{id}/test` | reachability probe → `verified_at` |
| `POST /providers/{id}/test-chat` | **real completion** → `chat_verified_at` |
| `POST /providers/{id}/models/refresh` | catalog discovery, refusals recorded |
| `GET /models?provider_id=&include_disabled=` | model rows (+ `policy_reason`, `metadata`) |
| `POST /models` / `DELETE /models/{p}/{m}` | hand-manage rows (policy enforced) |
| `GET /models/tiers` / `POST /models/tiers` | per-tier pins |
| `GET /models/routing` | what the router would pick, and why not the others |
| `GET /setup/status` | first-run setup state |

Failures everywhere are `{"error": "..."}` (there is a test pinning that shape
across every failure class).
