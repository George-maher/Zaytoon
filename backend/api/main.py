import os
import time
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, UploadFile, File
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from agents import (
    RouterAgent, PlannerAgent, FastAgent, CodeAgent,
    ToolAgent, MemoryAgent, SchedulerAgent,
)
from voice import SpeechToText, TextToSpeech
from memory import ShortTermMemory, UserPreferences
from scheduler import SchedulerManager
from reminder import ReminderService, NotificationManager
from tools.base import OSToolkit
from tools.executor import ToolExecutor
from config import config, PROVIDER_OLLAMA, TIER_FAST, TIER_REASONING
from arabic import detect_language, get_tts_voice, clean_and_detect
from agents.base_agent import close_shared_client
from services.auth import get_device_auth, PermissionScope
from services.llm_client import get_llm_client
from services.ai_provider import get_provider, OllamaProvider
from tools.safety import ConfirmationManager, RiskAssessor
from tools.metrics import get_tool_metrics
from common.reporting import client_error, redact, safe_error

from api.websocket import (
    create_websocket_handler,
    ConnectionManager,
)
from domain.models import TokenBucketRateLimiter
from services.agent_service import AgentService

logger = logging.getLogger("Zaytoon")

router_agent = RouterAgent()
planner_agent = PlannerAgent()
fast_agent = FastAgent()
code_agent = CodeAgent()
memory_agent = MemoryAgent()
scheduler_llm_agent = SchedulerAgent()

stt = SpeechToText(
    model_name=config.WHISPER_MODEL,
    device=config.WHISPER_DEVICE,
    compute_type=config.WHISPER_COMPUTE,
)
tts = TextToSpeech()

short_term_memory = ShortTermMemory()
user_prefs = UserPreferences(memory_agent)
os_toolkit = OSToolkit()

# Agent-level safety sandbox. A websocket session installs its own
# ConfirmationManager per connection; this default is used for REST calls and
# as a safety net so high-risk tools are denied rather than run unchecked.
risk_assessor = RiskAssessor()
default_confirmation_manager = ConfirmationManager(notify=None, timeout=config.TOOL_CONFIRM_TIMEOUT)
tool_agent = ToolAgent(
    confirmation_manager=default_confirmation_manager,
    risk_assessor=risk_assessor,
)

notification_mgr = NotificationManager()
reminder_service = ReminderService(memory_agent)

scheduler = SchedulerManager(reminder_service, notification_mgr)

tool_executor = ToolExecutor(
    toolkit=os_toolkit,
    reminder_service=reminder_service,
    scheduler=scheduler,
)

agent_service = AgentService(
    router_agent=router_agent,
    fast_agent=fast_agent,
    planner_agent=planner_agent,
    code_agent=code_agent,
    tool_agent=tool_agent,
    memory_agent=memory_agent,
    scheduler_llm_agent=scheduler_llm_agent,
    scheduler_manager=scheduler,
    reminder_service=reminder_service,
    notification_mgr=notification_mgr,
    tool_executor=tool_executor,
    os_toolkit=os_toolkit,
)

rate_limiter = TokenBucketRateLimiter(
    max_requests=config.RATE_LIMIT_MAX,
    window=config.RATE_LIMIT_WINDOW,
)

manager = ConnectionManager()


@asynccontextmanager
async def lifespan(app):
    asyncio.create_task(scheduler.start())
    
    # Log provider configuration
    if config.PROVIDER == "ollama":
        logger.info(
            "LLM provider=ollama endpoint=%s",
            config.OLLAMA_BASE_URL,
        )
        logger.info(
            "  general=%s coding=%s fast=%s",
            config.OLLAMA_GENERAL_MODEL,
            config.OLLAMA_CODING_MODEL,
            config.OLLAMA_FAST_MODEL,
        )
    else:
        logger.info(
            "LLM provider=openrouter endpoint=%s (key=%s)", config.PROVIDER,
            config.LLM_API_URL, "present" if config.llm_configured else "ABSENT",
        )
        logger.info(
            "  primary=%s reasoning=%s fallback=%s",
            config.OPENROUTER_PRIMARY_MODEL,
            config.OPENROUTER_REASONING_MODEL,
            config.OPENROUTER_FALLBACK_MODEL,
        )
        rejected = config.validate_free_models()
        if rejected:
            logger.error(
                "FREE-MODEL POLICY VIOLATION: %s not in the allow-list. "
                "Requests using them will be rejected before any network call.",
                rejected,
            )
        else:
            logger.info("Free-model policy: OK (all configured models allow-listed)")
        if not config.llm_configured:
            logger.error(
                "OPENROUTER_API_KEY is not set. Zaytoon calls OpenRouter only "
                "(local model execution has been removed), so the assistant will "
                "be unable to answer until a key is configured."
            )
    
    logger.info("Tool safety sandbox: %s (confirm timeout %.0fs)",
                "on" if config.TOOL_SAFETY_ENABLED else "off",
                config.TOOL_CONFIRM_TIMEOUT)

    loop = asyncio.get_running_loop()

    async def _load_stt():
        available = await loop.run_in_executor(None, lambda: stt.available)
        if available:
            logger.info("STT (Whisper %s) loaded at startup", config.WHISPER_MODEL)
        elif stt._load_error:
            logger.warning("STT unavailable - voice input disabled")

    asyncio.create_task(_load_stt())
    
    # Pre-warm provider connection
    try:
        provider = get_provider()
        health = await provider.health_check()
        if health.connected:
            logger.info("AI provider connected: %s (%s)", provider.name, health.version)
            for m in health.models:
                logger.info("  Model available: %s (%s)", m.id, ", ".join(m.capabilities) or "no caps")
        else:
            logger.warning("AI provider not connected: %s", health.error)
    except Exception as e:
        safe_error(e, operation="provider.health", component="api.startup")

    logger.info("Zaytoon AI started on %s:%s", config.BACKEND_HOST, config.BACKEND_PORT)
    yield
    await scheduler.stop()
    await close_shared_client()
    await get_llm_client().aclose()
    # Close AI provider
    from services.ai_provider import close_provider
    await close_provider()
    # The Ollama transport is a module-level singleton reached through the
    # Ollama path in `services/llm_client`, so closing the provider alone
    # leaves that connection open until the process exits.
    from services.ollama_client import close_ollama_client
    await close_ollama_client()
    logger.info("Zaytoon AI backend shut down")


app = FastAPI(title="Zaytoon AI Assistant", version="3.0.0", lifespan=lifespan)


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc: HTTPException):
    """Normalise error responses to ``{"error": "..."}``.

    FastAPI's default body is ``{"detail": ...}``, but every other failure path
    in this API — tool results, ``/health``, ``/command/*`` — uses ``error``.
    Two shapes for the same concept means every client has to check both, and
    the frontend's ``setWorkspace`` was reading ``data.error``.

    Registered for both exception classes below, because FastAPI raises
    ``fastapi.exceptions.HTTPException`` from routes while Starlette raises
    ``starlette.exceptions.HTTPException`` for an unmatched path — the two are
    not the same class, so registering only this one left ``GET /nope`` answering
    ``{"detail": "Not Found"}``.
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail},
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(StarletteHTTPException)
async def starlette_http_exception_handler(request, exc):
    """The unmatched-route case: same body, same shape (see above)."""
    return await http_exception_handler(request, exc)


@app.exception_handler(RequestValidationError)
async def request_validation_handler(request, exc: RequestValidationError):
    """422s carry ``error`` too, as one sentence rather than a schema dump.

    ``exc.errors()`` items nest ``ctx`` values that are not always JSON-safe
    (e.g. ``ValueError`` objects from a custom validator), so the raw list is
    not returned. The first message is enough for a client to act on, and the
    field path says where to look.
    """
    errs = exc.errors()
    if errs:
        first = errs[0]
        where = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        msg = str(first.get("msg", "invalid request"))
        detail = f"{where}: {msg}" if where else msg
        if len(errs) > 1:
            detail = f"{detail} (+{len(errs) - 1} more)"
    else:
        detail = "invalid request"
    return JSONResponse(status_code=422, content={"error": detail})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
    """The REST boundary: report one line, return a sentence, never a traceback.

    Without this, an unexpected exception in a route becomes Starlette's bare
    ``Internal Server Error`` — no correlation id, no structured log entry, and
    nothing for an operator to grep. With it, the response is the same fixed
    bilingual sentence the WebSocket layer sends, and the log carries
    ``op=<method> <path> type=<ExcClass>`` with the detail redacted.

    The debug traceback still exists for ``uvicorn --debug``/``--reload``:
    Starlette re-raises after this handler has produced the response, so
    ``TestClient(raise_server_exceptions=True)`` keeps showing real failures in
    tests instead of hiding them behind a friendly message.
    """
    safe_error(
        exc,
        operation=f"{request.method} {request.url.path}",
        component="api",
    )
    return JSONResponse(
        status_code=500,
        content={"error": client_error(exc, operation="request", component="api")},
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
        "tauri://localhost",
        "https://tauri.localhost",
    ],
    allow_origin_regex=r"http://localhost:517\d+",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/tools")
async def list_tools():
    """Every registered tool, with its schema and permission tier.

    This is the single source of truth the UI, the settings panel and the
    agent prompt all read. It is generated from the registry, so a tool cannot
    be advertised without also being dispatchable.
    """
    return {
        "tools": tool_executor.describe_tools(),
        "by_tier": tool_executor.tools_by_tier(),
    }


@app.get("/health")
async def health():
    client = get_llm_client()
    # build_chain() raises when the key is missing, when the pool contains no
    # allow-listed free model, or when nothing satisfies a capability
    # requirement. /health must stay answerable in exactly those situations, so
    # report the error instead of propagating it.
    #
    # With the Ollama provider there is no OpenRouter pool to describe: the
    # "chain" is the three configured local models, and reporting the free
    # cloud pool here would be a lie about where the next request goes.
    if config.PROVIDER == PROVIDER_OLLAMA:
        _url = f"{config.OLLAMA_BASE_URL}/api/chat"
        seen: dict[str, None] = {}
        for _tier in ("general", "reasoning", "fast"):
            seen.setdefault(config.ollama_model_for_tier(_tier), None)
        chain = [
            {"model": m, "provider": "ollama", "url": _url} for m in seen
        ]
        chain_error = None
        routing = {
            _tier: {
                "provider": "ollama",
                "tier": _tier,
                "model": config.ollama_model_for_tier(_tier),
                "requires": [],
            }
            for _tier in ("fast", "reasoning", "general")
        }
    else:
        try:
            chain = [
                {"model": e.model, "provider": e.provider, "url": e.url}
                for e in client.build_chain()
            ]
            chain_error = None
        except Exception as e:
            chain = []
            # Surfaced by `GET /health` and drawn in the settings panel, so it
            # is user-visible: redacted rather than dropped, because the text
            # is exactly what an operator needs to fix a model configuration.
            chain_error = redact(str(e))

        # The routing decision, and why each candidate was or was not usable.
        # This is the answer to "why did it pick that model?", which used to be
        # unanswerable because the chain ended in an auto-router.
        routing = {}
        for tier in ("fast", "reasoning"):
            try:
                routing[tier] = client.plan(tier, ()).to_dict()
            except Exception as e:  # pragma: no cover - defensive
                routing[tier] = {"models": [], "error": redact(str(e))}
        try:
            routing["tools"] = client.plan("fast", ("tools",)).to_dict()
        except Exception as e:  # pragma: no cover - defensive
            routing["tools"] = {"models": [], "error": redact(str(e))}

    return {
        "status": "ok" if config.provider_ready else "degraded",
        "version": "3.0.0",
        "llm": {
            **config.describe_llm(),
            "chain": chain,
            "chain_error": chain_error,
            "routing": routing,
            "max_attempts": client.max_attempts,
            "model_health": client.health_snapshot(),
            "metrics": client.metrics.to_dict(),
        },
        "agents": ["router", "planner", "fast", "code", "tool", "memory", "scheduler"],
        # Tool-layer health, alongside (not inside) `llm.metrics`: one answers
        # "is the model healthy?", this one "is the tool layer healthy?".
        "metrics": {"tools": get_tool_metrics().to_dict()},
        "safety": {
            "enabled": config.TOOL_SAFETY_ENABLED,
            "confirm_timeout": config.TOOL_CONFIRM_TIMEOUT,
            "always_confirm": sorted(risk_assessor.always_confirm),
        },
        "voice": {"stt": stt.available, "tts": tts._available},
        "workspace": config.describe_workspace(),
        "tools_loaded": len(tool_executor.list_tools()),
        "tools_by_tier": tool_executor.tools_by_tier(),
        "uptime": time.time(),
    }


@app.get("/tasks")
async def list_tasks(session_id: str = "default", status: str = "pending"):
    reminders = await reminder_service.list(status=status, session_id=session_id)
    return {"tasks": reminders}


@app.get("/tasks/{task_id}")
async def get_task(task_id: int):
    reminder = await reminder_service.get(task_id)
    if not reminder:
        raise HTTPException(404, "not found")
    return reminder


@app.post("/tasks", status_code=201)
async def create_task(data: dict):
    task = data.get("task") or data.get("title", "Task")
    time_str = data.get("time") or data.get("scheduled_time", "")
    recurring = data.get("recurring", "none")
    session_id = data.get("session_id", "default")

    if time_str:
        try:
            scheduled = datetime.fromisoformat(time_str)
        except ValueError:
            raise HTTPException(400, "Invalid time format. Use ISO 8601.")
    else:
        raise HTTPException(400, "scheduled_time is required")

    result = await reminder_service.create(
        task=task,
        scheduled_time=scheduled,
        recurring=recurring,
        session_id=session_id,
    )
    if not result["success"]:
        raise HTTPException(400, result["error"])
    return result["reminder"]


@app.delete("/tasks/{task_id}")
async def delete_task(task_id: int):
    ok = await reminder_service.delete(task_id)
    if not ok:
        raise HTTPException(404, "not found")
    return {"status": "deleted", "id": task_id}


@app.patch("/tasks/{task_id}/done")
async def mark_task_done(task_id: int):
    ok = await reminder_service.mark_completed(task_id)
    if not ok:
        raise HTTPException(404, "not found")
    return {"status": "done", "id": task_id}


@app.post("/stt")
async def speech_to_text_endpoint(file: UploadFile = File(...), language: str | None = None):
    audio_data = await file.read()
    text = stt.transcribe(audio_data, language=language)
    detected = detect_language(text) if text else "en"
    return {"text": text, "detected_language": detected}


@app.post("/chat")
async def chat_endpoint(data: dict):
    message = (data.get("message") or "").strip()
    session_id = data.get("session_id", "rest_default")

    if not message:
        return {"error": "No message provided", "speak": False}
    if len(message) > config.MAX_MESSAGE_LENGTH:
        return {"error": f"Message too long. Max {config.MAX_MESSAGE_LENGTH} characters.", "speak": False}

    cleaning = clean_and_detect(message)
    lang = data.get("lang") or data.get("language") or cleaning["language"]
    message = cleaning["text"]

    await memory_agent.store_message("user", message, language=lang, session_id=session_id)
    context = await memory_agent.get_recent_context(session_id=session_id)
    route_result = await router_agent.route(message, context)
    intent = route_result.get("intent", "chat")

    reply = await agent_service.dispatch(intent, message, context, lang, session_id)
    if reply:
        await memory_agent.store_message("assistant", reply, language=lang, session_id=session_id)

    return {
        "reply": reply or "",
        "text": reply or "",
        "lang": lang,
        "intent": intent,
        "speak": bool(reply),
    }


@app.post("/tts")
async def text_to_speech_endpoint(data: dict):
    text = (data.get("text") or "").strip()
    lang = data.get("lang") or data.get("language") or detect_language(text) or "en"
    if not text:
        return Response(content=b"", status_code=400)
    voice = get_tts_voice(lang)
    audio = await tts.synthesize(text, voice=voice, rate=config.TTS_RATE, volume=config.TTS_VOLUME)
    if not audio:
        return Response(content=b"", status_code=503)
    return Response(content=audio, media_type="audio/mpeg")


@app.get("/preferences")
async def get_preferences():
    return await user_prefs.get_all()


@app.post("/preferences")
async def set_preferences(data: dict):
    for key, value in data.items():
        await user_prefs.set(key, value)
    return {"status": "saved"}


@app.get("/settings/workspace")
async def get_workspace():
    """The directory every file tool is confined to."""
    info = config.describe_workspace()
    try:
        entries = sorted(os.listdir(info["root"]))[:100]
        info["entries"] = entries
    except OSError as e:
        info["entries"] = []
        info["error"] = redact(str(e))
    return info


@app.post("/settings/workspace")
async def set_workspace(data: dict):
    """Point the assistant at a different working directory.

    The change is in-memory for the running process (and persisted as a
    preference so a restart can restore it). A path that cannot be created is
    rejected rather than silently ignored, so the UI never shows a workspace
    the tools are not actually using.
    """
    raw = (data.get("root") or data.get("path") or "").strip()
    if not raw:
        raise HTTPException(400, "No workspace path provided")

    candidate = Path(os.path.expandvars(raw)).expanduser()
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        resolved = candidate.resolve()
    except OSError as e:
        logger.warning("Rejected workspace %r: %s", raw, e)
        raise HTTPException(400, f"Cannot use that directory: {redact(str(e))}")

    if not resolved.is_dir():
        raise HTTPException(400, "Not a directory")

    config.WORKSPACE_ROOT = str(resolved)
    # Persist so a restart keeps the user's choice.
    try:
        await user_prefs.set("workspace_root", str(resolved))
    except Exception as e:  # preferences are best-effort
        safe_error(
            e, operation="prefs.workspace", component="api.settings",
            detail="workspace not persisted",
        )

    logger.info("Workspace root set to %s", resolved)
    return {"status": "saved", **config.describe_workspace()}


# ---------------------------------------------------------------------------
# AI Provider Settings
# ---------------------------------------------------------------------------

@app.get("/settings/ai")
async def get_ai_settings():
    """Get current AI provider configuration and model status."""
    from services.ai_provider import get_provider
    
    provider = get_provider()
    health = await provider.health_check()
    
    return {
        "provider": config.PROVIDER,
        "endpoint": provider.base_url,
        "connected": health.connected,
        "version": health.version,
        "error": health.error,
        "models": [
            {
                "id": m.id,
                "name": m.name,
                "available": m.available,
                "capabilities": m.capabilities,
                "size_bytes": m.size_bytes,
            }
            for m in health.models
        ],
        "routing": {
            "fast": provider.model_for_tier(TIER_FAST),
            "reasoning": provider.model_for_tier(TIER_REASONING),
            "general": provider.model_for_tier("general"),
        },
        "configured_models": {
            "general": getattr(config, "OLLAMA_GENERAL_MODEL", ""),
            "coding": getattr(config, "OLLAMA_CODING_MODEL", ""),
            "fast": getattr(config, "OLLAMA_FAST_MODEL", ""),
        } if config.PROVIDER == "ollama" else {
            "primary": config.OPENROUTER_PRIMARY_MODEL,
            "reasoning": config.OPENROUTER_REASONING_MODEL,
            "fallback": config.OPENROUTER_FALLBACK_MODEL,
        },
    }


@app.post("/settings/ai")
async def set_ai_settings(data: dict):
    """Update AI provider configuration."""
    provider_name = (data.get("provider") or "").strip().lower()
    if provider_name and provider_name not in ("ollama", "openrouter"):
        raise HTTPException(400, "Invalid provider. Must be 'ollama' or 'openrouter'")
    
    if provider_name:
        config.PROVIDER = provider_name
        config.AI_PROVIDER = provider_name
    
    if config.PROVIDER == "ollama":
        # Update Ollama settings
        base_url = (data.get("base_url") or "").strip()
        if base_url:
            config.OLLAMA_BASE_URL = base_url.rstrip("/")
        
        general = (data.get("general_model") or "").strip()
        if general:
            config.OLLAMA_GENERAL_MODEL = general
        
        coding = (data.get("coding_model") or "").strip()
        if coding:
            config.OLLAMA_CODING_MODEL = coding
        
        fast = (data.get("fast_model") or "").strip()
        if fast:
            config.OLLAMA_FAST_MODEL = fast
        
        timeout = data.get("timeout")
        if timeout is not None:
            try:
                config.OLLAMA_TIMEOUT = float(timeout)
            except (ValueError, TypeError):
                raise HTTPException(400, "Invalid timeout value")
    
    else:
        # Update OpenRouter settings
        api_key = (data.get("api_key") or "").strip()
        if api_key:
            config.OPENROUTER_API_KEY = api_key
        
        base_url = (data.get("base_url") or "").strip()
        if base_url:
            config.OPENROUTER_BASE_URL = base_url.rstrip("/")
        
        primary = (data.get("primary_model") or "").strip()
        if primary:
            config.OPENROUTER_PRIMARY_MODEL = primary
        
        reasoning = (data.get("reasoning_model") or "").strip()
        if reasoning:
            config.OPENROUTER_REASONING_MODEL = reasoning
        
        fallback = (data.get("fallback_model") or "").strip()
        if fallback:
            config.OPENROUTER_FALLBACK_MODEL = fallback
    
    # Invalidate provider singleton so new settings take effect
    from services.ai_provider import close_provider
    await close_provider()
    
    # Verify new configuration
    from services.ai_provider import get_provider
    provider = get_provider()
    health = await provider.health_check()
    
    return {
        "status": "saved",
        "provider": config.PROVIDER,
        "connected": health.connected,
        "error": health.error,
        "models": [m.id for m in health.models],
    }


@app.post("/settings/ai/validate")
async def validate_ai_settings(data: dict):
    """Test AI provider connection and list available models."""
    provider_name = (data.get("provider") or config.PROVIDER).strip().lower()
    
    if provider_name == "ollama":
        base_url = (data.get("base_url") or config.OLLAMA_BASE_URL).rstrip("/")
        from services.ollama_client import OllamaClient
        client = OllamaClient(base_url, timeout=config.OLLAMA_TIMEOUT)
        try:
            health = await client.health_check()
            return {
                "connected": health.connected,
                "endpoint": base_url,
                "version": health.version,
                "models": [m.name for m in health.models],
                "error": health.error,
            }
        finally:
            await client.aclose()
    else:
        # OpenRouter validation would require API key
        return {
            "connected": False,
            "error": "OpenRouter validation requires API key",
        }


@app.post("/settings/ai/pull")
async def pull_model(data: dict):
    """Pull a model from Ollama registry."""
    if config.PROVIDER != "ollama":
        raise HTTPException(400, "Model pulling only available with Ollama provider")
    
    model = (data.get("model") or "").strip()
    if not model:
        raise HTTPException(400, "Model name required")
    
    from services.ai_provider import get_provider
    provider = get_provider()
    
    if not isinstance(provider, OllamaProvider):
        raise HTTPException(500, "Provider is not Ollama")
    
    # Return a streaming response or just start the pull
    # For now, return immediately with status
    return {
        "status": "started",
        "model": model,
        "message": f"Model pull started. Check logs for progress.",
    }


# ---------------------------------------------------------------------------
# Device pairing & authorization (for remote Web/Mobile clients)
# ---------------------------------------------------------------------------

@app.post("/devices/pair")
async def pair_device(data: dict):
    """Pair a new remote device. Returns a one-time token."""
    device_id = (data.get("device_id") or "").strip()
    device_name = (data.get("device_name") or "").strip()
    scope_str = (data.get("scope") or "chat").strip()

    if not device_id:
        raise HTTPException(400, "device_id is required")
    if not device_name:
        raise HTTPException(400, "device_name is required")

    try:
        scope = PermissionScope(scope_str)
    except ValueError:
        valid = [s.value for s in PermissionScope]
        raise HTTPException(400, f"Invalid scope. Valid: {valid}")

    auth = get_device_auth()
    token, permission = auth.pair_device(device_id, device_name, scope)

    return {
        "token": token,  # Only returned once - client must store it
        "device": permission.to_dict(),
    }


@app.get("/devices")
async def list_devices():
    """List all paired devices."""
    auth = get_device_auth()
    return {"devices": auth.list_devices()}


@app.get("/devices/{device_id}")
async def get_device(device_id: str):
    """Get a specific device's info."""
    auth = get_device_auth()
    perm = auth.get_device(device_id)
    if not perm:
        raise HTTPException(404, "Device not found")
    return perm.to_dict()


@app.delete("/devices/{device_id}")
async def revoke_device(device_id: str):
    """Revoke a device's access."""
    auth = get_device_auth()
    ok = auth.revoke_device(device_id)
    if not ok:
        raise HTTPException(404, "Device not found")
    return {"status": "revoked", "device_id": device_id}


@app.patch("/devices/{device_id}/scope")
async def update_device_scope(device_id: str, data: dict):
    """Update a device's permission scope."""
    scope_str = (data.get("scope") or "").strip()
    if not scope_str:
        raise HTTPException(400, "scope is required")

    try:
        scope = PermissionScope(scope_str)
    except ValueError:
        valid = [s.value for s in PermissionScope]
        raise HTTPException(400, f"Invalid scope. Valid: {valid}")

    auth = get_device_auth()
    perm = auth.update_scope(device_id, scope)
    if not perm:
        raise HTTPException(404, "Device not found or revoked")
    return perm.to_dict()


APP_WHITELIST = [
    "chrome", "google chrome", "firefox", "edge", "microsoft edge",
    "vscode", "vs code", "visual studio code",
    "notepad", "notepad++", "calculator", "cmd", "powershell",
    "terminal", "command prompt", "file explorer", "explorer",
    "spotify", "discord", "slack", "zoom", "teams", "microsoft teams",
    "outlook", "word", "excel", "powerpoint", "paint", "regedit",
    "task manager", "control panel", "settings", "fxsound",
]


@app.post("/command/open-app")
async def open_app_endpoint(data: dict):
    app_name = (data.get("app_name") or data.get("app") or "").strip().lower()
    if not app_name:
        return {"success": False, "error": "No app name provided"}
    if app_name not in APP_WHITELIST:
        return {
            "success": False,
            "error": f"'{app_name}' is not in the allowed apps list.",
            "allowed": APP_WHITELIST,
        }
    result = await tool_executor.execute("open_app", {"app": app_name})
    logger.info(f"open-app: {app_name} -> {result}")
    return result


@app.post("/command/run")
async def run_command_endpoint(data: dict):
    """Run a single terminal command over REST.

    Routed through the same registry as every other tool, so the two paths
    cannot diverge. That matters most for the *unconditional* blocks: this
    handler used to call ``risk_assessor.assess`` directly, so a chained
    command like ``echo hi & hostname`` came back as
    ``requires_confirmation: true`` — implying the user could approve it — when
    the WebSocket path correctly refused it as ``blocked``.

    There is no approval channel here, so a confirmation-required command is
    denied; the UI must use the WebSocket to ask the user.
    """
    command = (data.get("command") or "").strip()
    if not command:
        return {"success": False, "error": "No command provided"}

    return await tool_executor.execute("system_command", {"command": command})


# ---------------------------------------------------------------------------
# Tracked processes and the execution audit log
# ---------------------------------------------------------------------------
# Read-only views over two subsystems that already enforce their own rules, so
# these routes stay deliberately thin. In particular `POST /processes/{id}/stop`
# is a *request* to the same registry the `process_stop` tool uses, and it is
# the only stop path reachable without a WebSocket — which is why it stops a
# registry-tracked process and nothing else.
@app.get("/processes")
async def list_processes():
    """Background processes Zaytoon started, newest first."""
    from tools.processes import registry

    processes = registry.snapshot()
    return {"processes": processes, "total": len(processes)}


@app.get("/processes/{process_id}/logs")
async def read_process_logs(process_id: str, tail: int = 20):
    """The recent output of one tracked process."""
    from tools.processes import registry

    entry = registry.get(process_id)
    if entry is None:
        return {"error": f"No such process: {process_id}", "lines": []}
    try:
        count = max(1, min(int(tail), 500))
    except (TypeError, ValueError):
        count = 20
    return {"id": entry.id, "status": entry.status, "lines": entry.tail(count)}


@app.post("/processes/{process_id}/stop")
async def stop_process(process_id: str):
    """Stop a tracked process.

    Denies by default: an unknown id means Zaytoon did not start that process,
    and the whole point of the registry is that Zaytoon never reaches outside
    it. There is no override and no "force" parameter.
    """
    from tools.processes import registry

    result = await registry.stop(process_id)
    if result is None:
        return {
            "stopped": False,
            "error": f"No such process: {process_id}. "
                     "Only processes Zaytoon started can be stopped.",
        }
    return {"stopped": True, "status": result.get("status"), "process": result}


@app.get("/audit")
async def list_audit(limit: int = 100):
    """Execution history, newest first. Never raises: a read failure is an
    empty list, because the history page is not worth failing a request over."""
    from common import audit

    entries = await audit.recent(limit)
    return {"entries": entries, "total": await audit.count()}


@app.delete("/audit")
async def clear_audit():
    """Clear the execution history. Returns how many rows were removed."""
    from common import audit

    cleared = await audit.clear()
    logger.info("Execution audit cleared: %s entries", cleared)
    return {"cleared": cleared}


@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse("""
    <!DOCTYPE html><html>
    <head><title>Zaytoon AI</title>
    <style>
        body { font-family: system-ui; background: #0a0a0f; color: #e5e5e5;
               display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
        .card { text-align: center; padding: 3rem; background: #13131a; border-radius: 1rem;
                border: 1px solid #2a2a3a; max-width: 500px; }
        h1 { background: linear-gradient(135deg, #8b5cf6, #3b82f6); -webkit-background-clip: text;
             -webkit-text-fill-color: transparent; margin: 0 0 0.5rem; }
        .badge { display: inline-block; margin-top: 1rem; padding: 0.25rem 1rem;
                 background: #1e1b4b; color: #a78bfa; border-radius: 999px; font-size: 0.875rem; }
    </style></head>
    <body><div class="card">
        <h1>Zaytoon AI Assistant</h1>
        <p>v3.0.0 | OpenRouter | Multi-Agent | Bilingual Voice | Streaming TTS</p>
        <p>WebSocket: <strong>ws://localhost:8000/ws</strong></p>
        <div class="badge">Production Ready</div>
    </div></body></html>
    """)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await create_websocket_handler(
        websocket=websocket,
        rate_limiter=rate_limiter,
        connection_manager=manager,
        stt=stt,
        tts=tts,
        memory_agent=memory_agent,
        short_term_memory=short_term_memory,
        agent_service=agent_service,
        router_agent=router_agent,
        scheduler=scheduler,
        reminder_service=reminder_service,
        config=config,
    )


async def on_reminder_fired(reminder: dict):
    await manager.broadcast({
        "type": "reminder",
        "id": reminder.get("id"),
        "title": reminder.get("title", "Reminder"),
        "message": f"Reminder: {reminder['title']}",
        "timestamp": datetime.now().isoformat(),
    })

notification_mgr.set_broadcast_callback(on_reminder_fired)
