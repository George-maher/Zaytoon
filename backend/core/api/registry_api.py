"""``/skills``, ``/providers``, ``/models``, ``/permissions``, ``/terminal``."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query

from core.permissions import DECISION_ASK, VALID_DECISIONS, describe as describe_permissions
from core.providers import (
    VALID_TIERS,
    Capability,
    ProviderError,
    delete_model,
    delete_provider,
    ensure_builtins,
    get_tier_assignments,
    list_models,
    list_providers,
    plan,
    provider_health,
    refresh_models,
    save_model,
    save_provider,
    set_tier_assignment,
)
from config import config
from core import paths
from core.skills import (
    MAX_ENABLED,
    ManifestError,
    SkillExists,
    SkillLimitExceeded,
    create,
    install,
    list_skills,
    remove,
    set_enabled,
    skill_detail,
    sync_to_db,
)

logger = logging.getLogger("Zaytoon.Core.Api.Registry")

skills_router = APIRouter()
providers_router = APIRouter()
terminal_router = APIRouter()
system_router = APIRouter()


# ===========================================================================
# Skills
# ===========================================================================
@skills_router.get("/skills")
async def skills_list() -> Dict[str, Any]:
    """Installed skills, plus the ones that failed to load and why."""
    return await list_skills()


@skills_router.get("/skills/{name}")
async def skill_get(name: str) -> Dict[str, Any]:
    detail = skill_detail(name)
    if detail is None:
        raise HTTPException(404, f"No such skill: {name}")
    return detail


@skills_router.post("/skills", status_code=201)
async def skill_create(data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Create a skill from a name and a description.

    This is what "create a skill that specialises in Laravel security" becomes.
    It writes a valid, loadable skill with a stub — it does not *generate the
    content*, because a generated instruction set the user has not read is a
    prompt-injection vector dressed up as a feature.
    """
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(422, "'name' is required")
    # `default_manifest` writes `enabled: true`, which is right for a skill a
    # user wrote and dropped in the directory. It is wrong for one created
    # through the API: installing is not activating, and a user who creates
    # fifteen skills has not asked for fifteen skills' instructions in every
    # prompt. The row is still written as enabled by sync_to_db, so this is
    # where the decision actually has to be made.
    try:
        created = create(
            name,
            description=(data.get("description") or "").strip(),
            instructions=data.get("instructions"),
            enabled=bool(data.get("enabled", False)),
        )
    except SkillExists as exc:
        raise HTTPException(409, str(exc)) from exc
    except ManifestError as exc:
        raise HTTPException(422, str(exc)) from exc
    await sync_to_db()
    return created


@skills_router.post("/skills/install")
async def skill_install(data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Copy a skill directory into the skills root."""
    source = (data.get("source") or data.get("path") or "").strip()
    if not source:
        raise HTTPException(422, "'source' (a directory containing skill.json) is required")
    try:
        installed = install(source, overwrite=bool(data.get("overwrite")))
    except SkillExists as exc:
        raise HTTPException(409, str(exc)) from exc
    except ManifestError as exc:
        raise HTTPException(422, str(exc)) from exc
    await sync_to_db()
    return {"status": "installed", "skill": installed}


@skills_router.post("/skills/{name}/enable")
async def skill_enable(name: str, data: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """Turn a skill on or off. ``{"enabled": false}`` turns it off."""
    enabled = bool(data.get("enabled", True))
    try:
        await set_enabled(name, enabled)
    except SkillLimitExceeded as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"name": name, "enabled": enabled, "max_enabled": MAX_ENABLED}


@skills_router.delete("/skills/{name}")
async def skill_delete(name: str) -> Dict[str, Any]:
    """Remove an installed skill. The registry refuses paths outside the root."""
    try:
        removed = remove(name)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from exc
    if not removed:
        raise HTTPException(404, f"No such skill: {name}")
    await sync_to_db()
    return {"status": "removed", "name": name}


# ===========================================================================
# Providers and models
# ===========================================================================
@providers_router.get("/providers")
async def providers_list() -> Dict[str, Any]:
    """Every configured provider.

    Note what is **not** here: no key material. Each provider reports
    ``has_api_key`` and nothing more, so there is no route anywhere in this
    application that can return a credential.
    """
    await ensure_builtins()
    providers = await list_providers()
    return {"providers": providers, "count": len(providers)}


@providers_router.post("/providers", status_code=201)
async def provider_create(data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Add or update a provider.

    ``id`` is optional; one is derived from the name. Passing an existing ``id``
    updates in place, which is what the settings form does on save.
    """
    try:
        saved = await save_provider(
            provider_id=(data.get("id") or "").strip(),
            name=(data.get("name") or "").strip(),
            kind=(data.get("kind") or "").strip(),
            base_url=(data.get("base_url") or "").strip(),
            requires_key=bool(data.get("requires_key")),
            api_key=data.get("api_key"),
            enabled=bool(data.get("enabled", True)),
            is_default=bool(data.get("is_default", False)),
        )
    except ProviderError as exc:
        raise HTTPException(422, str(exc)) from exc
    return saved


@providers_router.get("/providers/{provider_id}")
async def provider_get(provider_id: str) -> Dict[str, Any]:
    from core.providers import get_provider

    provider = await get_provider(provider_id)
    if provider is None:
        raise HTTPException(404, f"No such provider: {provider_id}")
    return provider


@providers_router.delete("/providers/{provider_id}")
async def provider_delete(provider_id: str) -> Dict[str, Any]:
    if not await delete_provider(provider_id):
        raise HTTPException(404, f"No such provider: {provider_id}")
    return {"status": "deleted", "id": provider_id}


@providers_router.post("/providers/{provider_id}/test")
async def provider_test(provider_id: str) -> Dict[str, Any]:
    """Probe the endpoint. Reports reachability, latency and what it offers."""
    result = await provider_health(provider_id)
    # A failed probe is a 200 with `connected: false`, not a 500: the endpoint
    # being down is the answer, and the UI needs the reason text, not a stack.
    return result


@providers_router.post("/providers/{provider_id}/models/refresh")
async def provider_refresh(provider_id: str) -> Dict[str, Any]:
    """Ask the endpoint what it has and record the result."""
    try:
        return await refresh_models(provider_id)
    except ProviderError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("Model refresh failed for %s: %s", provider_id, type(exc).__name__)
        raise HTTPException(502, f"Could not reach the endpoint: {type(exc).__name__}") from exc


@providers_router.get("/models")
async def models_list(
    provider_id: Optional[str] = Query(None),
    capabilities: Optional[str] = Query(None, description="Comma-separated"),
    include_disabled: bool = Query(False),
) -> Dict[str, Any]:
    """Known models, optionally filtered by required capabilities."""
    required = [c.strip() for c in (capabilities or "").split(",") if c.strip()] or None
    models = await list_models(
        provider_id=provider_id,
        # `?all=true` shows disabled models with a toggle beside them; the
        # default is the routable set, which is what a picker wants.
        enabled_only=not include_disabled,
        required=required,
    )
    return {
        "models": models,
        "count": len(models),
        "capabilities": [{"capability": c.value, "label": c.label} for c in Capability],
    }


@providers_router.post("/models", status_code=201)
async def model_create(data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Record a model by hand. Refuses one the provider's policy rejects."""
    provider_id = (data.get("provider_id") or "").strip()
    model_id = (data.get("id") or data.get("model") or "").strip()
    if not provider_id or not model_id:
        raise HTTPException(422, "'provider_id' and 'id' are required")
    try:
        await save_model(
            provider_id,
            model_id,
            display_name=data.get("name"),
            capabilities=data.get("capabilities"),
            context_length=data.get("context_length"),
            enabled=bool(data.get("enabled", True)),
            priority=int(data.get("priority", 100)),
        )
    except ProviderError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, f"Invalid model definition: {exc}") from exc
    models = await list_models(provider_id=provider_id, enabled_only=False)
    return {"status": "saved", "model": next(
        (m for m in models if m["id"] == model_id), None
    )}


@providers_router.delete("/models/{provider_id}/{model_id:path}")
async def model_delete(provider_id: str, model_id: str) -> Dict[str, Any]:
    if not await delete_model(provider_id, model_id):
        raise HTTPException(404, f"No such model: {model_id}")
    return {"status": "deleted"}


@providers_router.get("/models/tiers")
async def tier_assignments_list() -> Dict[str, Any]:
    """Which model each tier is pinned to, resolved for display.

    An assignment whose provider or model row has since been deleted is
    reported with ``exists: false`` rather than dropped — the pin is still
    there and still steering, and hiding it would make the router's choice
    inexplicable from the screen that is supposed to explain it.
    """
    assignments = await get_tier_assignments()
    providers = {p["id"]: p for p in await list_providers()}
    models = await list_models(enabled_only=False)
    index = {(m["provider_id"], m["id"]): m for m in models}

    tiers: Dict[str, Any] = {}
    for tier in VALID_TIERS:
        entry = assignments.get(tier)
        if not entry:
            tiers[tier] = None
            continue
        model = index.get((entry["provider_id"], entry["model_id"]))
        provider = providers.get(entry["provider_id"])
        tiers[tier] = {
            "provider_id": entry["provider_id"],
            "model_id": entry["model_id"],
            "provider": (provider or {}).get("name") or entry["provider_id"],
            "name": (model or {}).get("name") or entry["model_id"],
            "enabled": bool(model["enabled"]) if model else False,
            "exists": provider is not None and model is not None,
        }
    return {"tiers": tiers, "available": list(VALID_TIERS)}


@providers_router.post("/models/tiers")
async def tier_assignment_set(data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Pin a tier to a model — or clear the pin (``model_id`` null/absent)."""
    tier = (data.get("tier") or "").strip().lower()
    provider_id = (data.get("provider_id") or "").strip() or None
    model_id = (data.get("model_id") or "").strip() or None
    try:
        assignments = await set_tier_assignment(tier, provider_id, model_id)
    except ProviderError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "status": "saved" if model_id else "cleared",
        "tier": tier,
        "assignment": assignments.get(tier),
        "tiers": {t: assignments.get(t) for t in VALID_TIERS},
    }


@providers_router.get("/models/routing")
async def routing_preview(
    tier: Optional[str] = Query("fast"),
    capabilities: Optional[str] = Query(None),
    provider_id: Optional[str] = Query(None),
    forced_model: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """What would be chosen, and why every other candidate was skipped.

    "Why did it pick that model?" has to be answerable, or routing is guesswork.
    """
    from core.providers import requirements_for

    required = requirements_for(
        tier, [c.strip() for c in (capabilities or "").split(",") if c.strip()]
    )
    decision = await plan(
        required=required, provider_id=provider_id, forced_model=forced_model,
        tier=tier,
    )
    return decision.to_dict()


# ===========================================================================
# Permissions
# ===========================================================================
@system_router.get("/permissions")
async def permissions_list() -> Dict[str, Any]:
    """The vocabulary, plus each tool's requirements and any stored decision."""
    from core import db
    from core.permissions import required_for
    from tools.registry import build_default_registry

    rows = await db.fetch_all("SELECT permission, decision, updated_at FROM permission_grants")
    grants = {row["permission"]: row["decision"] for row in rows}

    registry = build_default_registry()
    tools = []
    for tool in registry.tools():
        tools.append({
            "name": tool.name(),
            "permission_tier": tool.permission,
            "permissions": sorted(p.value for p in required_for(tool.name())),
            "description": tool.description(),
        })

    return {
        "vocabulary": describe_permissions(),
        "grants": grants,
        "default_decision": DECISION_ASK,
        "tools": tools,
    }


@system_router.post("/permissions/{permission}")
async def permission_set(permission: str, data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Store a decision for one permission.

    Recorded as a *decision*, not enforced at this layer: the enforcement point
    is the tool registry's gate, which already exists and already fails closed.
    """
    from core import db

    decision = (data.get("decision") or "").strip().lower()
    if decision not in VALID_DECISIONS:
        raise HTTPException(
            422, f"'decision' must be one of: {', '.join(VALID_DECISIONS)}"
        )
    from datetime import datetime, timezone

    ok = await db.execute(
        "INSERT INTO permission_grants (permission, decision, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(permission) DO UPDATE SET decision = excluded.decision, "
        "updated_at = excluded.updated_at",
        (permission, decision, datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )
    if not ok:
        raise HTTPException(500, "Could not store the decision")
    return {"permission": permission, "decision": decision}


# ===========================================================================
# Terminal
# ===========================================================================
@terminal_router.get("/terminal")
async def terminal_list() -> Dict[str, Any]:
    from core.workspace.terminal import get_manager

    sessions = get_manager().snapshot()
    return {"sessions": sessions, "count": len(sessions)}


@terminal_router.post("/terminal", status_code=201)
async def terminal_open(data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Open a terminal session.

    Goes through the same ``split_command`` sandbox as every other command
    path, so chaining and metacharacters are refused here too. Output is
    streamed over the WebSocket as ``terminal_output`` frames.

    ``env`` overlays the inherited environment (validated and capped — key
    names are echoed back, values never are); ``timeout`` stops the session
    after that many seconds. Both are optional.
    """
    from core.workspace.terminal import TerminalError, get_manager

    command = (data.get("command") or "").strip()
    if not command:
        raise HTTPException(422, "'command' is required")
    try:
        session = await get_manager().open(
            command,
            cwd=(data.get("cwd") or "").strip(),
            name=(data.get("name") or "").strip(),
            env=data.get("env"),
            timeout=data.get("timeout"),
        )
    except TerminalError as exc:
        # A sandbox refusal is a policy decision, not a malformed request, so it
        # is reported as 403 — "approving would not help" rather than "try again".
        raise HTTPException(403 if exc.blocked else 400, str(exc)) from exc
    return session.to_dict(with_lines=False)


@terminal_router.post("/terminal/{session_id}/input")
async def terminal_write(session_id: str, data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    from core.workspace.terminal import TerminalError, get_manager

    payload = data.get("data")
    if not isinstance(payload, str) or not payload:
        raise HTTPException(422, "'data' must be a non-empty string")
    try:
        return await get_manager().write(session_id, payload)
    except TerminalError as exc:
        raise HTTPException(400, str(exc)) from exc


@terminal_router.get("/terminal/{session_id}")
async def terminal_output(session_id: str, tail: int = Query(200, ge=1, le=1000)) -> Dict[str, Any]:
    from core.workspace.terminal import get_manager

    session = get_manager().get(session_id)
    if session is None:
        raise HTTPException(404, f"No such terminal: {session_id}")
    return {"id": session.id, "status": session.status, "lines": session.tail(tail)}


@terminal_router.delete("/terminal/{session_id}")
async def terminal_close(session_id: str) -> Dict[str, Any]:
    """Close a session. Refuses an unknown id, which is never Zaytoon's."""
    from core.workspace.terminal import get_manager

    result = await get_manager().close(session_id)
    if result is None:
        raise HTTPException(404, f"No such terminal: {session_id}")
    return {"status": "closed", "session": result}


#: The moment this process started. `uptime` below used to report
#: `time.time()` — the server's clock, not an uptime — which no panel could
#: render as a duration. Both are kept: one is a timestamp, one is real.
_PROCESS_START = time.time()


# ===========================================================================
# System
# ===========================================================================
@system_router.get("/system")
async def system_info() -> Dict[str, Any]:
    """What the system is doing right now, for the status panel."""
    import os
    import platform
    import time

    from core.tasks import get_manager as task_manager
    from core.workspace.terminal import get_manager as terminal_manager
    from core.system_resources import sample as resource_sample
    from tools.metrics import get_tool_metrics
    from tools.processes import registry as process_registry

    tasks = await task_manager().list(limit=500)
    return {
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "uptime": time.time(),
        # Real seconds since this process started — `uptime` above is a
        # timestamp; this is the duration a status panel can actually show.
        "uptime_seconds": max(time.time() - _PROCESS_START, 0.0),
        # CPU / memory / disk now, each with an explicit status — `ok`,
        # `sampling` (CPU's first reading has nothing to divide yet) or
        # `unavailable` with the reason. Never a fabricated zero.
        "resources": resource_sample(str(config.workspace_path)),
        "tasks": {
            "total": len(tasks),
            "active": sum(1 for t in tasks if t.status.is_active),
        },
        "processes": {
            "tracked": len(process_registry.list()),
            # Names and status only. A command line is redacted on the way into
            # the ring buffer, but it is still the user's own command and does
            # not belong in a general status payload.
            "running": [p.id for p in process_registry.list() if p.status == "running"],
        },
        "terminals": {"open": len(terminal_manager().list())},
        "tools": get_tool_metrics().to_dict(),
        # The *resolved* workspace root, not the cwd. `os.getcwd()` here would
        # report `backend/` to anyone reading `/system/status`, which is both
        # wrong and the opposite of what the containment rules enforce.
        "workspace": str(config.workspace_path),
        "paths": paths.describe(),
    }