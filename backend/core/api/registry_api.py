"""``/skills``, ``/providers``, ``/models``, ``/permissions``, ``/terminal``."""

from __future__ import annotations

import logging
import os
import time
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query

from core.api.approvals import ApprovalTokens, run_approved_tool
from core.permissions import DECISION_ASK, VALID_DECISIONS, describe as describe_permissions
from core.providers import (
    VALID_TIERS,
    Capability,
    ProviderError,
    delete_model,
    delete_provider,
    ensure_builtins,
    get_provider,
    get_tier_assignments,
    list_models,
    list_providers,
    mark_chat_verified,
    mark_verified,
    plan,
    provider_health,
    refresh_models,
    save_model,
    save_provider,
    set_tier_assignment,
)
from config import config
from core import paths
from core.events import CoreEventType, bus as core_bus
from core.skills import (
    MAX_ENABLED,
    ManifestError,
    SkillExists,
    SkillLimitExceeded,
    SkillOutsideRoot,
    create,
    install,
    list_skills,
    load,
    read_instructions,
    remove,
    set_enabled,
    skill_detail,
    skills_root,
    sync_to_db,
)

logger = logging.getLogger("Zaytoon.Core.Api.Registry")

skills_router = APIRouter()
providers_router = APIRouter()
terminal_router = APIRouter()
system_router = APIRouter()


def _announce_skill_change(manifest: Dict[str, Any]) -> None:
    """Notify the live registry that a skill's *tools* may have changed.

    ``create`` and ``install`` write to disk and then ``sync_to_db`` mirrors the
    row, but neither reaches the live ``ToolRegistry`` — the subscriber in
    ``api/main.py`` reloads skill tools only on ``SKILL_CHANGED``. Without this
    an enabled skill's ``tools/*.py`` would not be callable until a restart,
    which is the exact gap the resync subscriber exists to close for
    enable/disable/delete.

    Only an **enabled** skill is announced: a disabled one contributes no tools,
    so there is nothing to reload, and firing the event anyway would make every
    create/install pay a full resync for a no-op.
    """
    if not manifest.get("enabled"):
        return
    core_bus.publish(
        CoreEventType.SKILL_CHANGED,
        skill=manifest.get("name"),
        enabled=True,
    )


# ===========================================================================
# Skills
# ===========================================================================
@skills_router.get("/skills")
async def skills_list() -> Dict[str, Any]:
    """Installed skills, plus the ones that failed to load and why."""
    return await list_skills()


@skills_router.get("/skills/permissions")
async def skills_permissions() -> Dict[str, Any]:
    """The permission vocabulary a skill manifest may declare.

    Declared *before* ``/skills/{name}`` on purpose: FastAPI matches routes in
    declaration order, so with the parameterised route first this path would be
    read as a skill literally named "permissions" and answer 404.

    The list is served from ``ALL_PERMISSIONS`` rather than restated in the
    client, so the picker in the Skills page cannot drift from what the
    manifest validator accepts — the drift failure mode being a user choosing
    a permission the backend then rejects with a message about a name they
    never typed.
    """
    from core.permissions import ALL_PERMISSIONS

    return {
        "permissions": [
            {"name": permission.value, "label": permission.label}
            for permission in sorted(ALL_PERMISSIONS, key=lambda p: p.value)
        ],
        "max_enabled": MAX_ENABLED,
    }


@skills_router.get("/skills/{name}")
async def skill_get(name: str) -> Dict[str, Any]:
    detail = skill_detail(name)
    if detail is None:
        raise HTTPException(404, f"No such skill: {name}")
    return detail


@skills_router.post("/skills/{name}/test")
async def skill_test(name: str) -> Dict[str, Any]:
    """Dry-run a skill: validate its manifest and load its tool modules.

    A skill is not a command — it contributes *instructions* plus zero or more
    *tools* — so there is no single "execute" to run. What a user actually
    needs after editing one is the answer to three questions, and all of them
    are answerable without side effects:

    1. **Does it still validate?** The manifest is re-read from disk, so an
       edit that broke the schema is reported before the next turn discovers it.
    2. **Do its tool modules still import?** Each ``tools/*.py`` is imported
       and its ``build_tools()`` is called in a **scratch registry**, never the
       live one — a skill must not gain callable status by being tested. A
       build_tools() that raises, a bad return type or a name colliding with a
       built-in each show up here as a per-file error.
    3. **Does it declare what it needs?** The policy gap check runs exactly as
       it does at load time (``test_skill`` reuses the loader's own helpers),
       so "the dry run passed" and "startup would load it" cannot disagree.

    A skill with no tool modules is *ok*, not a failure — that is the common
    case. The route answers 200 with ``ok: false`` rather than an HTTP error:
    a skill that fails its test is a *result to display*, not a broken request.
    """
    directory = os.path.join(skills_root(), name)
    if not os.path.isdir(directory):
        raise HTTPException(404, f"No such skill: {name}")

    errors: List[Any] = []
    warnings: List[Any] = []

    # 1. Manifest — the headline. Everything below depends on it.
    try:
        manifest = load(directory)
        manifest_ok = True
    except ManifestError as exc:
        return {
            "skill": name,
            "ok": False,
            "manifest_ok": False,
            "instructions_ok": False,
            "tools_ok": False,
            "enabled": None,
            "modules": [],
            "tools": [],
            "policy": [],
            "errors": [{"skill": name, "file": "skill.json", "error": str(exc)}],
            "warnings": [],
        }

    # 2. Instructions. Empty is a *warning*, not a failure:
    #    `build_skill_context` silently skips an empty instruction set, so the
    #    skill still loads and its tools still run — it just contributes
    #    nothing to the prompt, which the user should see but is not "broken".
    try:
        text = read_instructions(manifest)
        instructions_ok = bool(text and text.strip())
    except Exception as exc:  # unreadable file, bad encoding
        instructions_ok = False
        errors.append(
            {"skill": name, "file": "INSTRUCTIONS.md", "error": type(exc).__name__}
        )
    if not instructions_ok and not errors:
        warnings.append(
            {
                "skill": name,
                "file": "INSTRUCTIONS.md",
                "error": "file is missing or empty; the skill contributes no instructions",
            }
        )

    # 3. Tool modules — loaded into a scratch registry, enabled or not.
    from core.skills.tool_loader import test_skill

    tool_report = await test_skill(name)
    errors.extend(tool_report["errors"])

    return {
        "skill": name,
        "ok": manifest_ok and not errors,
        "manifest_ok": manifest_ok,
        "instructions_ok": instructions_ok,
        "tools_ok": not tool_report["errors"],
        "enabled": manifest.enabled,
        "modules": tool_report["modules"],
        "tools": tool_report["tools"],
        "policy": tool_report["policy"],
        "errors": errors,
        "warnings": warnings,
    }


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
            # These two were accepted by the client and dropped here, so a skill
            # created asking for `filesystem.read` came back with
            # `permissions: []` — the UI then reported "asks for: (nothing)"
            # and the requirement that a skill's requested permissions be
            # visible before it is enabled could not be met. `create` validates
            # them against the known vocabulary, so a bad name is a 422.
            permissions=data.get("permissions"),
            tags=data.get("tags"),
        )
    except SkillExists as exc:
        raise HTTPException(409, str(exc)) from exc
    except ManifestError as exc:
        raise HTTPException(422, str(exc)) from exc
    await sync_to_db()
    _announce_skill_change(created)
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
    _announce_skill_change(installed)
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
    except SkillOutsideRoot as exc:
        # A named exception, because "not yours to delete" and "the caller sent
        # something malformed" are different answers. Catching every ValueError
        # and calling it 403 turned a caller bug into a permission decision the
        # user cannot retry.
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
            timeout_s=data.get("timeout_s"),
            org_id=data.get("org_id"),
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
    """Probe the endpoint. Reports reachability, latency and what it offers.

    A pass is *recorded* (``verified_at``): this is the only thing that may
    move a provider from "unverified" to "verified", and the UI must never
    show a success that no real test produced.
    """
    result = await provider_health(provider_id)
    # A failed probe is a 200 with `connected: false`, not a 500: the endpoint
    # being down is the answer, and the UI needs the reason text, not a stack.
    if result.get("connected") and result.get("provider_id"):
        result["verified_at"] = await mark_verified(result["provider_id"])
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


@providers_router.post("/providers/{provider_id}/test-chat")
async def provider_test_chat(
    provider_id: str, data: Optional[Dict[str, Any]] = Body(default=None)
) -> Dict[str, Any]:
    """Generate one real completion — the only acceptable proof of setup.

    ``POST /test`` proves the endpoint answers; this proves a model can
    actually be asked something and reply. The first-run wizard completes on
    this and nothing else, because a 200 from a models-list endpoint says
    nothing about whether the key may generate, the model exists, or the
    answer is well-formed — and every one of those failures is what a new
    user is actually setting up. A failed test is a 200 with ``ok: false``
    and the reason: the endpoint being broken is the answer, not a server
    error, and the UI needs the text to show.
    """
    from core.providers.client import (
        DEFAULT_TIMEOUT,
        OpenAICompatibleClient,
        ProviderTransportError,
    )
    from core.providers.store import resolve_api_key

    provider = await get_provider(provider_id)
    if provider is None:
        raise HTTPException(404, f"No such provider: {provider_id}")

    payload: Dict[str, Any] = data or {}
    model_id = str(payload.get("model") or "").strip()
    if not model_id:
        rows = await list_models(provider_id=provider_id, enabled_only=True)
        model_id = rows[0]["id"] if rows else ""
    if not model_id:
        raise HTTPException(
            409,
            "No models recorded for this provider — refresh its models first.",
        )

    message = str(payload.get("message") or "Reply with the single word: OK")[:500]
    key = await resolve_api_key(provider_id) if provider["requires_key"] else None
    client = OpenAICompatibleClient(
        provider["base_url"],
        api_key=key,
        kind=provider["kind"],
        timeout=float(provider.get("timeout_s") or DEFAULT_TIMEOUT),
        org_id=provider.get("org_id"),
    )
    started = time.monotonic()
    try:
        reply = await client.complete(
            [{"role": "user", "content": message}],
            model=model_id,
            max_tokens=32,
            temperature=0.0,
        )
    except ProviderTransportError as exc:
        latency = int((time.monotonic() - started) * 1000)
        logger.info("test-chat failed for %s: %s", provider["name"], exc)
        return {
            "ok": False,
            "connected": False,
            "provider_id": provider_id,
            "provider": provider["name"],
            "model": model_id,
            "latency_ms": latency,
            "error": str(exc),
        }
    finally:
        await client.aclose()

    latency = int((time.monotonic() - started) * 1000)
    stamp = await mark_chat_verified(provider_id)
    return {
        "ok": True,
        "connected": True,
        "provider_id": provider_id,
        "provider": provider["name"],
        "model": model_id,
        "latency_ms": latency,
        # What the model said, bounded: this is a connectivity proof, not a
        # transcript. The preview exists so the wizard can show "the model
        # replied" and never for a chat log.
        "reply": (reply or "").strip()[:200],
        "verified_at": stamp,
    }


@providers_router.get("/setup/status")
async def setup_status() -> Dict[str, Any]:
    """Is first-run setup done? Answered only by a real chat test.

    The setup banner and the wizard both read this. A provider that merely
    exists — or answered a reachability probe — does not complete setup:
    wrong key scope, a missing model and an exhausted balance each look
    exactly like success until a completion is attempted.
    """
    providers = await list_providers()
    stamps = sorted(
        p["chat_verified_at"] for p in providers if p.get("chat_verified_at")
    )
    return {
        "setup_required": not stamps,
        "providers_total": len(providers),
        "providers_enabled": sum(1 for p in providers if p.get("enabled")),
        "providers_chat_verified": len(stamps),
        "verified_at": stamps[-1] if stamps else None,
    }


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

# ===========================================================================
# Security workspace — target authorisation
# ===========================================================================
security_router = APIRouter()


@security_router.get("/security/authorizations")
async def security_authorizations() -> Dict[str, Any]:
    """Every target authorisation, active and lapsed, with the operation list."""
    from core.security import scope

    return await scope.describe()


@security_router.post("/security/authorizations", status_code=201)
async def security_authorize(data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Record an authorisation for user-stated targets.

    This is the **only** way an active assessment becomes permitted. There is
    deliberately no tool that reaches it: a model must not be able to widen its
    own scope, and a test asserts no registered tool imports this module.

    ``targets`` takes a host, an address, a CIDR, or a ``*.domain`` wildcard. A
    pattern covering every host is refused by the validator — an authorisation
    for everything is not an authorisation, and storing one would make the
    record a lie the UI then displays as a real scope.
    """
    from core.security import scope

    targets = data.get("targets")
    if isinstance(targets, str):
        targets = [targets]
    if not targets:
        single = data.get("target")
        targets = [single] if single else []
    operations = data.get("operations")
    if isinstance(operations, str):
        operations = [operations]
    if not operations:
        operations = [scope.OP_HTTP_PROBE]

    ttl = data.get("ttl_days")
    try:
        ttl_days = None if ttl in (None, "") else float(ttl)
    except (TypeError, ValueError):
        raise HTTPException(422, "ttl_days must be a number") from None

    try:
        created = await scope.create(
            targets,
            operations,
            note=str(data.get("note") or ""),
            ttl_days=ttl_days,
        )
    except scope.ScopeError as exc:
        raise HTTPException(422, str(exc)) from exc
    return created.to_dict()


@security_router.delete("/security/authorizations/{authorization_id}")
async def security_revoke(authorization_id: str) -> Dict[str, Any]:
    """Revoke an authorisation.

    The row is kept with a ``revoked_at`` stamp rather than deleted: a report
    that says which authorisation permitted a probe has to stay true afterwards,
    which it cannot if the record is gone.
    """
    from core.security import scope

    if not await scope.revoke(authorization_id):
        raise HTTPException(404, f"No active authorisation: {authorization_id}")
    return {"id": authorization_id, "revoked": True}


# ===========================================================================
# The probe endpoint: a real approval, in two steps
# ===========================================================================
# `security_probe` is a `confirm`-tier tool, so a single REST call that ran it
# would have to either bypass the gate (never) or invent an approval (also
# never). The WebSocket path solves this by asking the user and awaiting the
# answer; REST has no channel to ask on. So the ask is split across two requests
# that the Security Workspace performs as two clicks:
#
#   1. `{url}`                 → scope check only; no socket is opened. Returns
#                                what the probe *would* do, plus a token.
#   2. `{url, confirm: token}` → the same call, carrying the user's answer.
#
# The gate still runs on step 2. It is answered by calling
# `ConfirmationManager.resolve()` on the one request this endpoint caused —
# which is the same call the WebSocket handler makes when the user clicks
# Approve, except the click happened on this page. Nothing is skipped:
# validation, scope authorisation, the tier gate and the audit record all run
# in order, and the approval is recorded against a request the manager itself
# issued.
#
# A token is single-use and short-lived. Tokens are held in memory rather than
# the database: an approval is a gesture in the moment, and a durable one that
# outlived the page it was given on would be a standing permission, which is
# what the authorisation record is for.
#
# The machinery moved to `core.api.approvals` when the VM routes needed the same
# thing. These names are kept — and kept pointing at the *same objects* — so the
# probe's own behaviour and its tests are unchanged by the move, while there is
# only one implementation of "mint, expire, spend".
_PROBE_APPROVALS = ApprovalTokens(ttl=120.0, cap=256, prefix="probe")
_PROBE_TOKENS: Dict[str, float] = _PROBE_APPROVALS.tokens
_PROBE_TOKEN_TTL_S = _PROBE_APPROVALS.ttl
_PROBE_TOKEN_MAX = _PROBE_APPROVALS.cap
_prune_probe_tokens = _PROBE_APPROVALS.prune


@security_router.post("/security/probe")
async def security_probe_run(data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """A non-destructive HTTP probe against a target the user authorised.

    Two-step. Step 1 opens no socket at all, so a preview can never have a side
    effect on the target.
    """
    from core.security import scope

    url = (data.get("url") or "").strip()
    if not url:
        raise HTTPException(422, "'url' is required")
    include_body = bool(data.get("include_body", False))
    confirm = (data.get("confirm") or "").strip()

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        raise HTTPException(422, "'url' must be an absolute http(s) URL")

    # -- Scope, before anything else. The same call the tool will make, so the
    #    preview cannot disagree with the attempt.
    try:
        authorization = await scope.require_authorized(host, scope.OP_HTTP_PROBE)
    except scope.OutOfScope as exc:
        return {"refused": True, "reason": "out_of_scope", "message": str(exc)}
    except scope.ScopeError as exc:
        raise HTTPException(422, str(exc)) from exc

    if not confirm:
        token = _PROBE_APPROVALS.issue()
        return {
            "requires_confirmation": True,
            "confirm": token,
            "url": url,
            "host": host,
            "authorization_id": authorization.id,
            "authorized_target": authorization.target,
            "message": (
                f"Sends one non-destructive GET to {host}, under authorisation "
                f"{authorization.id}. Nothing is written on the target."
            ),
        }

    if not _PROBE_APPROVALS.consume(confirm):
        raise HTTPException(
            409, "Unknown or expired confirmation token. Request the probe again."
        )

    # -- Step 2: the user answered. Run it through the process's real registry.
    result = await run_approved_tool(
        "security_probe", {"url": url, "include_body": include_body}
    )
    if isinstance(result, dict):
        result.setdefault("authorization_id", authorization.id)
        result.setdefault("authorized_target", authorization.target)
    return result
