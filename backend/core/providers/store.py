"""Provider and model persistence.

A provider is a row, not a class. That is the change from the existing
`services/ai_provider.py`, which has two compiled-in provider classes selected by
a process-global string — workable, and the reason "add a provider using an
endpoint URL" was not a feature.

Rows mean the user adds a provider through the UI and it exists, with no code
change and no restart. It also means secrets and endpoints are separated:
``providers`` is readable and reportable, ``provider_secrets`` is not.

The free-only policy is enforced here for OpenRouter, and *only* there. A user
adding their own OpenAI-compatible endpoint is pointing at infrastructure they
already pay for; refusing their models because they lack a ``:free`` suffix
would be Zaytoon second-guessing the user's own billing. The policy that
exists — never spend the user's money on a model they did not choose — is a
property of *Zaytoon's own* OpenRouter account, and it stays absolute.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from common.reporting import safe_error
from core import db
from core.events import CoreEventType, bus
from core.providers.capabilities import DEFAULT_CAPABILITIES, normalise
from core.secrets import delete_secret, get_secret, has_secret, set_secret

logger = logging.getLogger("Zaytoon.Core.Providers.Store")

#: ``<vendor>/<model>:free``. The only form the OpenRouter pool accepts.
FREE_MODEL_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._:-]+:free$")

PROVIDER_OLLAMA = "ollama"
PROVIDER_OPENROUTER = "openrouter"
PROVIDER_OPENAI_COMPATIBLE = "openai_compatible"

VALID_KINDS = (PROVIDER_OLLAMA, PROVIDER_OPENROUTER, PROVIDER_OPENAI_COMPATIBLE)

#: Providers created on first run, so a fresh install has something to talk to.
#: Only Ollama, because it needs no key and no account — the product is
#: local-first and this is the provider that makes it work out of the box.
BUILTIN_PROVIDERS = (
    {
        "id": "local-ollama",
        "name": "Ollama (local)",
        "kind": PROVIDER_OLLAMA,
        "base_url": "http://localhost:11434",
        "requires_key": False,
        "is_default": True,
    },
)


class ProviderError(ValueError):
    """A provider request could not be satisfied, with a reason."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_provider(
    name: str, kind: str, base_url: str, requires_key: bool
) -> Dict[str, str]:
    """Check a provider definition. Raises :class:`ProviderError` naming the field."""
    clean_name = (name or "").strip()
    if not clean_name:
        raise ProviderError("'name' is required")
    if len(clean_name) > 80:
        raise ProviderError("'name' must be 80 characters or fewer")

    clean_kind = (kind or "").strip().lower()
    if clean_kind not in VALID_KINDS:
        raise ProviderError(
            f"'kind' must be one of: {', '.join(VALID_KINDS)} (got {kind!r})"
        )

    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise ProviderError("'base_url' is required")
    if not url.startswith(("http://", "https://")):
        raise ProviderError(
            f"'base_url' must start with http:// or https:// (got {base_url!r})"
        )
    # A key in the URL would end up in the database, in the log line for this
    # row, and in anything that echoes the endpoint back to a client.
    if "@" in url.split("://", 1)[-1].split("/", 1)[0]:
        raise ProviderError("'base_url' must not embed credentials; use 'api_key'")

    return {"name": clean_name, "kind": clean_kind, "base_url": url, "requires_key": bool(requires_key)}


def slugify(name: str) -> str:
    """A filesystem-safe id from a provider name."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return slug or "provider"


def assert_model_allowed(kind: str, model_id: str) -> str:
    """Refuse a model the provider's policy will not accept.

    For OpenRouter this is the free-only gate, and it is the same rule the rest
    of the codebase already enforces — re-implemented here as a check rather
    than delegated, because this table is where a user-supplied model id enters
    the system, and it must not be a way around ``config.assert_free_model``.
    """
    model = (model_id or "").strip()
    if not model:
        raise ProviderError("'model' is required")
    if kind != PROVIDER_OPENROUTER:
        return model
    from config import is_free_router

    if is_free_router(model):
        raise ProviderError(
            f"{model!r} is an uncontrolled auto-router and is not used: it resolves "
            "to a different arbitrary model on every call."
        )
    if not FREE_MODEL_RE.match(model):
        raise ProviderError(
            f"{model!r} is not an allow-listed free model. Zaytoon's OpenRouter "
            "account must only call '<vendor>/<model>:free'."
        )
    try:
        from config import is_allowed_model

        if not is_allowed_model(model):
            raise ProviderError(
                f"{model!r} is free but not allow-listed, so it is refused before "
                "any request is made."
            )
    except ImportError:  # pragma: no cover - config always present in practice
        pass
    return model


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
async def ensure_builtins() -> int:
    """Create the built-in providers if they are absent. Idempotent."""
    created = 0
    stamp = _now()
    for provider in BUILTIN_PROVIDERS:
        row = await db.fetch_one(
            "SELECT id FROM providers WHERE id = ?", (provider["id"],)
        )
        if row:
            continue
        if await db.execute(
            """
            INSERT INTO providers
                (id, name, kind, base_url, requires_key, enabled, is_default,
                 created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                provider["id"], provider["name"], provider["kind"],
                provider["base_url"], int(provider["requires_key"]), 1,
                int(provider.get("is_default", False)), stamp, stamp,
            ),
        ):
            created += 1
    return created


async def save_provider(
    provider_id: str,
    name: str,
    kind: str,
    base_url: str,
    requires_key: bool = False,
    api_key: Optional[str] = None,
    enabled: bool = True,
    is_default: bool = False,
) -> Dict[str, Any]:
    """Create or update a provider.

    ``api_key`` is written to the secrets store and **never** returned. An empty
    string is ignored rather than clearing the key, because a client that does
    not have the value to hand should not be able to wipe it by omitting it.
    """
    fields = validate_provider(name, kind, base_url, requires_key)
    pid = (provider_id or "").strip() or slugify(fields["name"])
    stamp = _now()

    if requires_key and api_key is None and not has_secret(pid):
        raise ProviderError(
            f"'{fields['name']}' needs an API key. Provide one, or clear 'requires_key'."
        )

    if is_default:
        await db.execute("UPDATE providers SET is_default = 0 WHERE is_default = 1")

    existed = await db.fetch_one("SELECT id FROM providers WHERE id = ?", (pid,))
    ok = await db.execute(
        """
        INSERT INTO providers
            (id, name, kind, base_url, requires_key, enabled, is_default,
             created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name, kind = excluded.kind,
            base_url = excluded.base_url, requires_key = excluded.requires_key,
            enabled = excluded.enabled, is_default = excluded.is_default,
            updated_at = excluded.updated_at
        """,
        (
            pid, fields["name"], fields["kind"], fields["base_url"],
            int(fields["requires_key"]), int(bool(enabled)), int(bool(is_default)),
            stamp, stamp,
        ),
    )
    if not ok:
        raise ProviderError("Could not save the provider")

    if api_key:
        set_secret(pid, api_key)

    if is_default:
        await db.execute("UPDATE providers SET is_default = 1 WHERE id = ?", (pid,))

    bus.publish(CoreEventType.PROVIDER_CHANGED, provider_id=pid, action="saved")
    return await get_provider(pid) or {}


async def get_provider(provider_id: str) -> Optional[Dict[str, Any]]:
    row = await db.fetch_one("SELECT * FROM providers WHERE id = ?", (provider_id,))
    return _provider_row(row) if row else None


async def list_providers() -> List[Dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT * FROM providers ORDER BY is_default DESC, name ASC"
    )
    return [_provider_row(row) for row in rows]


async def default_provider() -> Optional[Dict[str, Any]]:
    row = await db.fetch_one(
        "SELECT * FROM providers WHERE is_default = 1 AND enabled = 1 LIMIT 1"
    )
    if not row:
        row = await db.fetch_one(
            "SELECT * FROM providers WHERE enabled = 1 ORDER BY name ASC LIMIT 1"
        )
    return _provider_row(row) if row else None


async def delete_provider(provider_id: str) -> bool:
    """Remove a provider, its models and its credential."""
    # `execute_affected`: a DELETE matching no rows succeeds as a statement, so
    # this would report "deleted" for a provider that was never there.
    removed = await db.execute_affected(
        "DELETE FROM providers WHERE id = ?", (provider_id,)
    )
    await db.execute("DELETE FROM models WHERE provider_id = ?", (provider_id,))
    delete_secret(provider_id)
    if removed > 0:
        bus.publish(CoreEventType.PROVIDER_CHANGED, provider_id=provider_id, action="deleted")
    return removed > 0


async def resolve_api_key(provider_id: str) -> Optional[str]:
    """The credential for signing a request. Internal use only."""
    return get_secret(provider_id)


def _provider_row(row: Dict[str, Any]) -> Dict[str, Any]:
    # Note the absence of any key material. This dict is what the API returns.
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "kind": row.get("kind"),
        "base_url": row.get("base_url"),
        "requires_key": bool(row.get("requires_key")),
        "has_api_key": has_secret(row.get("id") or ""),
        "enabled": bool(row.get("enabled")),
        "is_default": bool(row.get("is_default")),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
async def save_model(
    provider_id: str,
    model_id: str,
    display_name: Optional[str] = None,
    capabilities: Optional[List[str]] = None,
    context_length: Optional[int] = None,
    enabled: bool = True,
    priority: int = 100,
    validate_policy: bool = True,
) -> bool:
    """Record a model against a provider.

    Refuses a model the provider's policy rejects, so a billable id cannot enter
    the table even by hand and later be picked up by the router.
    """
    provider = await get_provider(provider_id)
    if provider is None:
        raise ProviderError(f"No such provider: {provider_id}")
    model = model_id.strip() if model_id else ""
    if validate_policy:
        model = assert_model_allowed(provider["kind"], model)
    try:
        length = int(context_length) if context_length else None
    except (TypeError, ValueError):
        length = None
    try:
        rank = int(priority)
    except (TypeError, ValueError):
        rank = 100

    return await db.execute(
        """
        INSERT INTO models
            (provider_id, model_id, display_name, capabilities, context_length,
             enabled, priority, discovered_at)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(provider_id, model_id) DO UPDATE SET
            display_name = excluded.display_name,
            capabilities = excluded.capabilities,
            context_length = excluded.context_length,
            enabled = excluded.enabled,
            priority = excluded.priority
        """,
        (
            provider_id, model, display_name or model,
            json.dumps(sorted(normalise(capabilities))),
            length, int(bool(enabled)), rank, _now(),
        ),
    )


async def list_models(
    provider_id: Optional[str] = None,
    enabled_only: bool = True,
    required: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Models, optionally filtered by provider and by required capabilities.

    ``enabled_only`` defaults to **True**. A model the user switched off is one
    they do not want routed to, so the default listing — used by the router, by
    ``GET /models`` and by the tests — excludes it. Listing everything is the
    explicit ``enabled_only=False`` request, which is what the models screen
    wants when it is showing a disabled row with a toggle beside it.
    """
    clauses: List[str] = []
    params: List[Any] = []
    if provider_id:
        clauses.append("provider_id = ?")
        params.append(provider_id)
    if enabled_only:
        clauses.append("enabled = 1")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    rows = await db.fetch_all(
        f"SELECT * FROM models {where} ORDER BY priority ASC, model_id ASC", params
    )
    models = [_model_row(row) for row in rows]
    if required:
        wanted = [str(r).strip().lower() for r in required if str(r).strip()]
        models = [
            m for m in models
            if all(cap in m["capabilities"] for cap in wanted)
        ]
    return models


async def delete_model(provider_id: str, model_id: str) -> bool:
    """Remove one model. Reports whether a row was actually removed."""
    return await db.execute_affected(
        "DELETE FROM models WHERE provider_id = ? AND model_id = ?",
        (provider_id, model_id),
    ) > 0


# ---------------------------------------------------------------------------
# Tier assignments
# ---------------------------------------------------------------------------
#: The tiers a user may pin a model to. ``default`` is the catch-all for any
#: tier without its own assignment (including the legacy ``fallback`` tier),
#: so "pin everything unless I say otherwise" needs no extra mechanism.
VALID_TIERS = ("fast", "coding", "reasoning", "default")

_TIER_ASSIGNMENTS_KEY = "models.tiers"


async def get_tier_assignments() -> Dict[str, Dict[str, str]]:
    """Persisted ``tier → {provider_id, model_id}`` pins.

    An unreadable row reads as *no assignments* rather than raising: this is
    consulted on the request path, and a corrupt preference must steer
    nowhere — not abort the turn that merely asked a question.
    """
    try:
        raw = await db.kv_get(_TIER_ASSIGNMENTS_KEY)
    except Exception as exc:  # noqa: BLE001 - see the docstring
        logger.warning("Tier assignment read failed: %s", type(exc).__name__)
        return {}
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("Tier assignments in kv are not valid JSON; ignoring")
        return {}
    assignments: Dict[str, Dict[str, str]] = {}
    if isinstance(parsed, dict):
        for tier, value in parsed.items():
            if tier not in VALID_TIERS or not isinstance(value, dict):
                continue
            provider_id = str(value.get("provider_id") or "").strip()
            model_id = str(value.get("model_id") or "").strip()
            if provider_id and model_id:
                assignments[tier] = {"provider_id": provider_id, "model_id": model_id}
    return assignments


async def set_tier_assignment(
    tier: str, provider_id: Optional[str], model_id: Optional[str]
) -> Dict[str, Dict[str, str]]:
    """Pin a tier to one model — or clear the pin with a falsy ``model_id``.

    Validated at write time so a pin cannot name something the router would
    refuse anyway: unknown provider or model is an error here rather than a
    silent miss at request time, and the free-only policy runs through
    :func:`assert_model_allowed` — a billable id must not be storable as a
    pin any more than it is storable as a model row.

    Returns the full new assignment map (the response the caller echoes).
    """
    key = (tier or "").strip().lower()
    if key not in VALID_TIERS:
        raise ProviderError(f"'tier' must be one of: {', '.join(VALID_TIERS)} (got {tier!r})")

    assignments = await get_tier_assignments()

    if not model_id:
        assignments.pop(key, None)
    else:
        provider = await get_provider((provider_id or "").strip())
        if provider is None:
            raise ProviderError(f"No such provider: {provider_id}")
        model = model_id.strip()
        assert_model_allowed(provider["kind"], model)
        rows = await list_models(provider_id=provider["id"], enabled_only=False)
        if not any(row["id"] == model for row in rows):
            raise ProviderError(
                f"Model '{model}' is not recorded for provider '{provider['name']}'. "
                "Refresh the model list or add it first."
            )
        assignments[key] = {"provider_id": provider["id"], "model_id": model}

    if not await db.kv_set(_TIER_ASSIGNMENTS_KEY, json.dumps(assignments)):
        raise ProviderError("Could not persist the tier assignment")
    return assignments


def _model_row(row: Dict[str, Any]) -> Dict[str, Any]:
    raw = row.get("capabilities")
    try:
        parsed = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        parsed = None
    capabilities = sorted(normalise(parsed))
    return {
        "provider_id": row.get("provider_id"),
        "id": row.get("model_id"),
        "name": row.get("display_name") or row.get("model_id"),
        "capabilities": capabilities,
        "context_length": row.get("context_length"),
        "enabled": bool(row.get("enabled")),
        "priority": int(row.get("priority") or 100),
        "discovered_at": row.get("discovered_at"),
    }


__all__ = [
    "BUILTIN_PROVIDERS",
    "FREE_MODEL_RE",
    "PROVIDER_OLLAMA",
    "PROVIDER_OPENAI_COMPATIBLE",
    "PROVIDER_OPENROUTER",
    "ProviderError",
    "VALID_KINDS",
    "VALID_TIERS",
    "assert_model_allowed",
    "default_provider",
    "delete_model",
    "delete_provider",
    "ensure_builtins",
    "get_provider",
    "get_tier_assignments",
    "list_models",
    "list_providers",
    "resolve_api_key",
    "save_model",
    "save_provider",
    "set_tier_assignment",
    "slugify",
    "validate_provider",
]