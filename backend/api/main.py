import os
import re
import time
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Dict

from fastapi import FastAPI, HTTPException, Query, WebSocket, UploadFile, File
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
from tools.executor import ToolExecutor, build_shortcut_index, resolve_app_detail
from config import config, PROVIDER_OLLAMA, TIER_FAST, TIER_REASONING
from arabic import detect_language, get_tts_voice, clean_and_detect
from agents.base_agent import close_shared_client
from services.auth import get_device_auth, PermissionScope
from services.llm_client import get_llm_client, llm_gate
from services.ai_provider import get_provider, OllamaProvider
from tools.safety import ConfirmationManager, RiskAssessor
from tools.metrics import get_tool_metrics
from common.reporting import client_error, redact, safe_error
from common.tracing import new_trace_id, reset_trace_id, set_trace_id

from api.websocket import (
    create_websocket_handler,
    ConnectionManager,
)
from domain.models import TokenBucketRateLimiter
from services.agent_service import AgentService
from services import workspace_settings
from services.provider_health import provider_health

# The Zaytoon core runtime: task system, projects, files, skills, providers,
# permissions, terminal. Routers are mounted below; see `core/` for the design.
from core.api.routers import (
    files_router,
    git_router,
    projects_router,
    providers_router,
    skills_router,
    system_router,
    tasks_router,
    terminal_router,
)
from core.orchestrator import Orchestrator
from core.providers import ensure_builtins as ensure_builtin_providers
from core.skills import sync_to_db as sync_skills_to_db
from core.workspace.terminal import get_manager as get_terminal_manager

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

notification_mgr = NotificationManager()
reminder_service = ReminderService(memory_agent)

scheduler = SchedulerManager(reminder_service, notification_mgr)

# One live registry for the whole process: `GET /tools`, `/health`'s
# `tools_loaded`, the agent's tool catalogue and every execution all read the
# same instance, so a tool cannot be advertised without being dispatchable —
# and skill tools loaded into it at startup appear everywhere at once.
tool_executor = ToolExecutor(
    toolkit=os_toolkit,
    reminder_service=reminder_service,
    scheduler=scheduler,
    risk_assessor=risk_assessor,
)
tool_agent = ToolAgent(
    confirmation_manager=default_confirmation_manager,
    risk_assessor=risk_assessor,
    registry=tool_executor.registry,
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

#: Sequences a request end to end and keeps the task record. The WebSocket
#: handler supplies the socket; this owns the work — which is what lets a REST
#: request and a WebSocket turn share one implementation instead of two copies.
orchestrator = Orchestrator(
    agent_service=agent_service,
    router_agent=router_agent,
    memory_agent=memory_agent,
)


@asynccontextmanager
async def lifespan(app):
    asyncio.create_task(scheduler.start())

    # A choice saved through the settings screen beats the environment
    # default: the environment gives the *first* value, a deliberate save is
    # newer information. Best-effort — a corrupt row logs and applies
    # nothing, so a damaged preference degrades to the defaults instead of
    # refusing to boot.
    from services.ai_settings import apply_persisted_ai_settings
    try:
        await apply_persisted_ai_settings()
    except Exception as e:
        safe_error(e, operation="startup.ai_settings", component="api.startup")

    # Before any tool can run: the workspace root is the containment boundary of
    # every file tool, so a saved choice has to be in place when the first turn
    # starts — and a saved path that no longer exists must warn and fall back
    # rather than stop the backend from booting.
    try:
        await workspace_settings.restore(user_prefs)
    except Exception as e:
        safe_error(e, operation="startup.workspace", component="api.startup")

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
                "OPENROUTER_API_KEY is not set, so the assistant cannot answer. "
                "Set it in backend/.env, or run locally instead with "
                "AI_PROVIDER=ollama (no key needed, `ollama serve` required). "
                "There is no automatic switch between the two."
            )
    
    logger.info("Tool safety sandbox: %s (confirm timeout %.0fs)",
                "on" if config.TOOL_SAFETY_ENABLED else "off",
                config.TOOL_CONFIRM_TIMEOUT)

    # Core registry bootstrapping. Both are best-effort and both are logged
    # rather than fatal: a fresh install with an unwritable data directory
    # should still serve /health and let the user diagnose it, not refuse to
    # start.
    try:
        created = await ensure_builtin_providers()
        if created:
            logger.info("Registered %d built-in AI provider(s)", created)
    except Exception as e:
        safe_error(e, operation="startup.providers", component="api.startup")

    try:
        found = await sync_skills_to_db()
        if found:
            logger.info("Loaded %d skill(s) from the skills directory", found)
    except Exception as e:
        safe_error(e, operation="startup.skills", component="api.startup")

    # Skill tools join the one live registry: contained imports, a `confirm`
    # tier floor, and every failure reported rather than raised — a broken
    # skill must not stop the app from booting.
    try:
        from core.skills import load_skill_tools

        skill_report = await load_skill_tools(tool_executor.registry)
        if skill_report["tools"]:
            logger.info(
                "Loaded %d skill tool(s): %s",
                len(skill_report["tools"]),
                ", ".join(skill_report["tools"]),
            )
        # A startup that loaded nothing because every skill was broken has to be
        # readable in one line, not reconstructed from per-module warnings.
        if skill_report["errors"] or skill_report.get("policy"):
            logger.warning(
                "Skill tools incomplete: %d refusal(s), %d undeclared-permission "
                "gap(s)%s",
                len(skill_report["errors"]),
                len(skill_report.get("policy", [])),
                "" if config.SKILL_STRICT_PERMISSIONS
                else " (SKILL_STRICT_PERMISSIONS=false: loaded anyway)",
            )
    except Exception as e:
        safe_error(e, operation="startup.skill_tools", component="api.startup")

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
    # Terminals before the scheduler: a dev server the user started should be
    # told to stop while the event loop is still running, rather than being
    # orphaned when the process exits.
    try:
        await get_terminal_manager().close_all("backend shutting down")
    except Exception as e:
        safe_error(e, operation="shutdown.terminals", component="api.shutdown")
    # Same reasoning, different table: interactive shells and `process_start`
    # children are separate registries, and each had to be closed on its own.
    if config.PROCESS_STOP_ON_SHUTDOWN:
        try:
            from tools.processes import registry as process_registry

            stopped = await process_registry.close_all("backend shutting down")
            if stopped:
                logger.info("Stopped %d tracked process(es)", stopped)
        except Exception as e:
            safe_error(e, operation="shutdown.processes", component="api.shutdown")
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
    # Last, because everything above may still log through it: the memory agent
    # owns the only aiosqlite connection, and closing it after the loop is gone
    # is what produced `RuntimeError: Event loop is closed` at the end of every
    # test run — a warning that trained everyone to ignore the output.
    try:
        await memory_agent.close()
    except Exception as e:
        safe_error(e, operation="shutdown.memory", component="api.shutdown")
    logger.info("Zaytoon AI backend shut down")


#: The one version string: it appeared twice (here and in `/health`), and a
#: bump that missed one left the two disagreeing for whoever compared them.
APP_VERSION = "3.0.0"

#: Process start, for `/health`. Monotonic for the duration, wall-clock for the
#: "when did this process come up" the log and the UI can compare against.
_STARTED_MONOTONIC = time.monotonic()
_STARTED_AT = datetime.now(timezone.utc)


def _uptime_seconds() -> float:
    return round(time.monotonic() - _STARTED_MONOTONIC, 3)


app = FastAPI(title="Zaytoon AI Assistant", version=APP_VERSION, lifespan=lifespan)


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

# ---------------------------------------------------------------------------
# Core runtime routers
# ---------------------------------------------------------------------------
# Mounted without a prefix, so the paths are exactly what the architecture
# document specifies: /tasks, /projects, /files, /terminal, /skills, /providers,
# /models, /permissions, /system.
app.include_router(tasks_router)
app.include_router(projects_router)
app.include_router(files_router)
app.include_router(git_router)
app.include_router(terminal_router)
app.include_router(skills_router)
app.include_router(providers_router)
app.include_router(system_router)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
    ],
    # The desktop host serves its production build from a port in
    # desktop/app/host.py's DESKTOP_PORT_RANGE (5175–5179) — a *pinned* port,
    # because pywebview's random default produced an origin the API rejected
    # and every REST call from the shipped UI failed silently. Keep the two
    # ranges in step.
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):517\d+",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_id_middleware(request, call_next):
    """Give every HTTP request a correlation id, in and out.

    The header the caller sends is honoured (so a proxy or client can carry its
    own trace through to our logs), but only after checking it is a plain
    printable token of reasonable length — a header value lands verbatim in log
    lines and response headers, and neither is a place to put arbitrary bytes.
    A request without a usable one gets a fresh id.

    The binding lives in a ContextVar for the duration of the request, which
    is what lets `LLMClient` stamp its `LLM_ATTEMPT` log lines with the same
    id without threading a parameter through every agent. WebSockets are not
    covered by this middleware; the WebSocket handler binds its own trace per
    turn (`_run_turn`).
    """
    incoming = (request.headers.get("x-request-id") or "").strip()
    safe = all(ch.isprintable() and ch not in "\r\n" for ch in incoming)
    rid = incoming[:64] if (incoming and safe) else new_trace_id()

    token = set_trace_id(rid)
    try:
        response = await call_next(request)
    finally:
        reset_trace_id(token)
    response.headers["X-Request-ID"] = rid
    return response


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

    # The verdict has to combine two different questions that were previously
    # conflated. `provider_ready` is "is it configured?"; the probe is "did a
    # real handshake just succeed?". Reporting only the first made a stopped
    # daemon look healthy to the desktop supervisor and to the HUD.
    probe = await provider_health.status()
    usable = (
        config.provider_ready
        and probe["state"] in ("reachable",)
        and not probe.get("missing_models")
    )
    if not config.provider_ready:
        status = "degraded"
    elif usable:
        status = "ok"
    elif probe["state"] == "never_probed":
        # Nothing has been tried yet on a metered provider, so there is no
        # evidence either way. `ok` would be a guess; `degraded` would be an
        # alarm. Say what is actually known.
        status = "unverified"
    else:
        status = "degraded"

    return {
        "status": status,
        "status_reason": (
            "" if status == "ok"
            else "provider not configured" if not config.provider_ready
            else "no request has been made yet" if status == "unverified"
            else probe.get("error") or "provider unreachable"
        ),
        "version": APP_VERSION,
        "llm": {
            **config.describe_llm(),
            "chain": chain,
            "chain_error": chain_error,
            "routing": routing,
            "max_attempts": client.max_attempts,
            "model_health": client.health_snapshot(),
            "metrics": client.metrics.to_dict(),
            # "Why is it slow?" deserves a number. With a cap in place, the
            # difference between "the model is slow" and "eight of my own
            # requests are queued behind each other" is only visible here.
            "slots": llm_gate().stats(),
            # The probe's own report card, next to the counters it complements:
            # `configured` answers "is there an endpoint and a key", `probe`
            # answers "can it answer right now".
            "probe": probe,
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
        # The core registry is a separate answer to a separate question: "is the
        # model healthy?" is `llm`, and "can I open a folder / add a provider?"
        # is `core`. Reporting only the first made the whole workspace look
        # absent when it was merely on a different code path.
        "core": await _core_health(),
        # `uptime` was `time.time()` — a Unix epoch in a field named for a
        # duration, which read as ~56 years of uptime. `GET /system` already
        # answers this correctly (`uptime_seconds` from psutil/boot time); this
        # is the process's own clock, monotonic so a system-clock change cannot
        # make the app appear to have started in the future.
        "uptime": _uptime_seconds(),
        "uptime_seconds": _uptime_seconds(),
        "started_at": _STARTED_AT.isoformat(timespec="seconds"),
    }


async def _core_health() -> dict:
    """What the core runtime has: providers, models, skills, projects."""
    from core.providers import default_provider, list_models, list_providers
    from core.projects import list_projects
    from core.skills import discover

    providers = await list_providers()
    models = await list_models(enabled_only=True)
    active = await default_provider()
    skills = discover()
    projects = await list_projects()
    return {
        "providers": len(providers),
        "models": len(models),
        "default_provider": active["name"] if active else None,
        "default_model_ready": bool(active),
        "skills": len(skills["skills"]),
        "skill_errors": len(skills["errors"]),
        "projects": len(projects),
    }


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------
# These used to live at /tasks, which now means agent tasks (see
# ``core/api/tasks_api.py``). The move is deliberate: a task system and a
# reminder list cannot both own "/tasks", and "/tasks" should mean what the spec
# says it means. `/reminders` is the new home; every frontend call site was
# updated with it.
@app.get("/reminders")
async def list_reminders(session_id: str = "", status: str = "pending"):
    """Pending reminders, workspace-wide unless a session is named.

    ``session_id`` records *who asked* when a reminder is created; it is not a
    visibility partition — the socket id changes on every reconnect, so
    filtering by it would hide a reminder from the next session's panel. An
    explicit ``session_id`` query still filters.
    """
    reminders = await reminder_service.list(status=status, session_id=session_id or None)
    return {"tasks": reminders, "reminders": reminders}


@app.get("/reminders/{task_id}")
async def get_reminder(task_id: int):
    reminder = await reminder_service.get(task_id)
    if not reminder:
        raise HTTPException(404, "not found")
    return reminder


@app.post("/reminders", status_code=201)
async def create_reminder(data: dict):
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


@app.delete("/reminders/{task_id}")
async def delete_reminder(task_id: int):
    ok = await reminder_service.delete(task_id)
    if not ok:
        raise HTTPException(404, "not found")
    return {"status": "deleted", "id": task_id}


@app.patch("/reminders/{task_id}/done")
async def mark_reminder_done(task_id: int):
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

    # The same sequence the WebSocket runs — task record, project preamble,
    # skill context, route, dispatch, transitions — so a REST client gets the
    # context and the task trail instead of a second, divergent copy of the
    # turn. Memory stays here: the transport stores the user's message *before*
    # the orchestrator fetches context, and the reply *after* it returns.
    result = await orchestrator.run_turn(
        message, session_id, language_hint=lang
    )

    if result.error:
        # `sources` here for the same reason it is on the success path: a client
        # should never have to know which branch it is parsing. An error turn that
        # did read pages reports them, because "it failed, and here is what it had
        # already read" is more useful than either half alone.
        return {"error": result.error, "speak": False, "sources": list(result.sources)}

    reply = result.reply or ""
    if reply:
        await memory_agent.store_message("assistant", reply, language=lang, session_id=session_id)

    return {
        "reply": reply,
        "text": reply,
        "lang": lang,
        "intent": result.intent,
        "speak": bool(reply),
        # The same list the `sources` WebSocket frame carries. Additive: a client
        # that ignores it is no worse off, and one that shows it can answer
        # "where did this come from?" without the model's say-so. An empty list
        # means no page or snippet backed this turn — not that the answer is
        # unverified *and* silent.
        "sources": list(result.sources),
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

    Validation lives in `services/workspace_settings` because the startup path
    applies the same rule: an endpoint that is stricter (or looser) than the
    restore is how a setting works once and then silently stops working. A path
    that cannot be used is rejected with the reason, never silently ignored, so
    the UI cannot show a workspace the tools are not actually using.
    """
    raw = data.get("root") or data.get("path") or ""
    try:
        resolved, note = workspace_settings.resolve_root(str(raw))
    except workspace_settings.WorkspaceRejected as e:
        logger.warning("Rejected workspace %r: %s", raw, e)
        raise HTTPException(400, redact(str(e))) from e

    workspace_settings.apply_root(resolved)
    persisted = await workspace_settings.persist(resolved, user_prefs)

    logger.info("Workspace root set to %s", resolved)
    return {
        "status": "saved",
        "note": note,
        "persisted": persisted,
        **config.describe_workspace(),
    }


# ---------------------------------------------------------------------------
# AI Provider Settings
# ---------------------------------------------------------------------------

@app.get("/settings/ai")
async def get_ai_settings():
    """Get current AI provider configuration and model status."""
    from services.ai_provider import get_provider
    from services.ai_settings import is_persisted
    
    provider = get_provider()
    health = await provider.health_check()
    
    return {
        "provider": config.PROVIDER,
        "endpoint": provider.base_url,
        "connected": health.connected,
        # True when the screen is showing a choice the user saved (which
        # overrode the environment default at startup), false for env-only.
        "persisted": await is_persisted(),
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

    # Persist the choice — this is what makes it survive a restart. The key
    # goes to credential storage (core.secrets), never into kv; everything
    # else goes to the settings row. Both are deliberate: a save that
    # silently reverted on restart is the bug users report as "did not
    # stick", and a key in kv would be a secret in a plain table.
    from services.ai_settings import persist_ai_settings, save_api_key
    api_key = (data.get("api_key") or "").strip()
    if api_key and config.PROVIDER == "openrouter":
        save_api_key(api_key)
    saved = await persist_ai_settings()

    # Invalidate provider singleton so new settings take effect
    from services.ai_provider import close_provider
    await close_provider()
    # ...and drop the cached health verdict, which describes the *old* endpoint.
    # A user who just repointed the provider must not watch /health report the
    # previous daemon for another TTL window.
    provider_health.invalidate()
    
    # Verify new configuration
    from services.ai_provider import get_provider
    provider = get_provider()
    health = await provider.health_check()
    
    return {
        "status": "saved",
        "provider": config.PROVIDER,
        "persisted": saved,
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


# Model ids accepted by the pull endpoint: a whitelist, so a model name can
# never become a shell fragment or a path. `library/llama3:8b-instruct-q4_K_M`
# and `hf.co/user/Repo:Q4_K_M` are the shapes that must pass; `..`, spaces,
# `&`, `|`, `%` and a leading `-` must not.
PULL_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*(:[A-Za-z0-9._-]+)?$")

#: Pulls this process has run, keyed by model. Module state on purpose: a
#: pull outlives the request that started it, and polling must see the same
#: record. Bounded so polling cannot grow the map forever.
_PULLS: Dict[str, Dict] = {}


async def _run_pull(provider, model: str) -> None:
    """Consume a pull stream into its status record. Never raises.

    A pull has no deadline — a 40 GB download is legitimately slow — so the
    honesty here is in the *record*, not in a timeout: every terminal state
    (`done`, `failed`) says what actually happened, and a stream that ends
    without Ollama's `success` line is reported as the failure it was.
    """
    state = _PULLS.get(model)
    if state is None:
        return
    try:
        async for progress in provider.pull_model(model):
            state["status"] = progress.status
            state["total"] = progress.total
            state["completed"] = progress.completed
        if state["state"] == "running":
            if state["status"] == "success":
                state["state"] = "done"
            else:
                state["state"] = "failed"
                state["error"] = "Pull stream ended without a success status"
    except asyncio.CancelledError:
        state["state"] = "failed"
        state["error"] = "Pull cancelled"
        raise
    except Exception as e:
        safe_error(e, operation="settings.ai.pull", component="api.settings")
        state["state"] = "failed"
        state["error"] = redact(str(e))[:300]
    finally:
        state["finished_at"] = time.time()
        if state["state"] == "done":
            # The next `GET /settings/ai` must see the model just installed.
            try:
                provider.invalidate_cache()
            except Exception as e:  # noqa: BLE001 - best-effort cache refresh
                safe_error(e, operation="settings.invalidate", component="api.startup")


@app.post("/settings/ai/pull")
async def pull_model(data: dict):
    """Start pulling a model — explicit, one at a time, never automatic.

    Nothing else in Zaytoon calls `pull_model`; a health check or a probe
    may *report* that a model is missing, and the user then asks for it.
    Pulling gigabytes is a decision, not an installation step, which is why
    `tests/test_api_routes.py` scans the tree to keep it that way.
    """
    if config.PROVIDER != PROVIDER_OLLAMA:
        raise HTTPException(400, "Model pulling only available with Ollama provider")

    model = (data.get("model") or "").strip()
    if not model:
        raise HTTPException(400, "Model name required")
    if ".." in model or not PULL_MODEL_RE.match(model):
        raise HTTPException(400, "Invalid model name")

    existing = _PULLS.get(model)
    if existing is not None and existing["state"] == "running":
        raise HTTPException(409, f"A pull of {model} is already running")

    provider = get_provider()
    if not isinstance(provider, OllamaProvider):
        raise HTTPException(500, "Provider is not Ollama")

    # Bound the record: drop finished entries before the map outgrows it.
    if len(_PULLS) >= 8:
        for key in [k for k, v in _PULLS.items() if v["state"] != "running"]:
            del _PULLS[key]

    _PULLS[model] = {
        "model": model,
        "state": "running",
        "status": "starting",
        "total": 0,
        "completed": 0,
        "error": None,
        "started_at": time.time(),
        "finished_at": None,
    }
    asyncio.create_task(_run_pull(provider, model))
    return {"status": "started", "model": model, "state": "running"}


@app.get("/settings/ai/pull")
async def pull_status(model: str = ""):
    """Poll a pull's progress, or list every pull this process has run."""
    if model:
        state = _PULLS.get(model)
        if state is None:
            raise HTTPException(404, f"No pull recorded for {model}")
        return state
    return {
        "pulls": sorted(
            _PULLS.values(), key=lambda s: s["started_at"], reverse=True
        )
    }


@app.post("/settings/ai/probe")
async def probe_model(data: dict):
    """Probe a model's real capabilities and record them in the registry.

    A claim in a capability list is only worth what measured it: this asks
    the model to stream and to call a tool, then writes what happened. The
    probe sends test prompts — it never downloads anything, and it only
    ever touches an *installed* model, so a miss says "pull it first"
    rather than quietly recording a model that is not there.
    """
    from core.providers import (
        ProviderError,
        default_provider,
        get_provider as get_provider_row,
        list_models,
        save_model,
    )
    from services.ollama_client import OllamaClient

    wanted = (data.get("model") or "").strip()
    if not wanted:
        raise HTTPException(400, "Model name required")
    provider_id = (data.get("provider_id") or "").strip()

    if provider_id:
        row = await get_provider_row(provider_id)
        if row is None:
            raise HTTPException(404, f"No such provider: {provider_id}")
    else:
        row = await default_provider()
        if row is None:
            raise HTTPException(400, "No AI provider is configured")
        provider_id = row["id"]

    if row["kind"] != "ollama":
        raise HTTPException(
            400, "Capability probing is only supported for Ollama providers"
        )

    # The default provider follows the live setting (a base_url edited in
    # the UI is newer than the row written at install time); an explicitly
    # named provider is probed at its own endpoint.
    default = await default_provider()
    if default is not None and provider_id == default["id"]:
        base_url = config.OLLAMA_BASE_URL.rstrip("/")
    else:
        base_url = (row.get("base_url") or config.OLLAMA_BASE_URL).rstrip("/")

    client = OllamaClient(base_url, timeout=config.OLLAMA_TIMEOUT)
    try:
        health = await client.health_check()
        if not health.connected:
            raise HTTPException(
                400, f"Cannot probe: Ollama unreachable at {base_url}"
            )
        installed = None
        for m in health.models:
            if m.id == wanted or m.name == wanted:
                installed = m.id or m.name
                break
        if installed is None:
            raise HTTPException(
                404,
                f"Model {wanted} is not installed on {base_url} — pull it "
                "first with POST /settings/ai/pull",
            )
        caps = await client.probe_capabilities(installed)
    finally:
        await client.aclose()

    capability_list = []
    if (
        caps.supports_streaming
        or caps.supports_tools
        or caps.supports_structured_output
    ):
        # It answered a probe at all — that *is* a chat model.
        capability_list.append("chat")
    if caps.supports_streaming:
        capability_list.append("streaming")
    if caps.supports_tools:
        capability_list.append("tools")
    if caps.supports_structured_output:
        capability_list.append("structured_output")

    recorded = False
    try:
        existing = {
            m["id"]: m
            for m in await list_models(provider_id, enabled_only=False)
        }
        prior = existing.get(installed)
        await save_model(
            provider_id,
            installed,
            capabilities=capability_list,
            context_length=caps.context_window or None,
            # A probe updates *capabilities* only: a row the user disabled
            # stays disabled, and their priority order is not the probe's
            # to rewrite (`save_model`'s upsert overwrites both).
            enabled=bool(prior["enabled"]) if prior else True,
            priority=prior["priority"] if prior else 100,
        )
        recorded = True
    except ProviderError as e:
        logger.warning("Probe result not recorded: %s", e)

    return {
        "model": installed,
        "provider_id": provider_id,
        "capabilities": capability_list,
        "supports": {
            "streaming": caps.supports_streaming,
            "tools": caps.supports_tools,
            "structured_output": caps.supports_structured_output,
        },
        "context_window": caps.context_window,
        "recorded": recorded,
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

#: Display metadata for the allow-list above.
#:
#: One entry per *application*; `names` holds every spelling
#: `POST /command/open-app` accepts for it (the canonical one first, which is
#: what `id` is and what a client should send). Two invariants, both asserted in
#: `tests/test_api_routes.py::TestComputerAppsRoute`:
#:
#:   * every allow-listed name appears in exactly one entry, so the Computer
#:     view can never hide an app the launcher would accept;
#:   * every name an entry carries is on the allow-list, so the view can never
#:     advertise an app the launcher will refuse.
#:
#: The second rule is why this cannot be a constant duplicated into the
#: frontend: the panel must be a *view* of the allow-list, not a second copy of
#: it that drifts the first time someone edits one of them.
#:
#: Which of these apps are *installed* is a separate question and an opt-in
#: one — `GET /computer/apps?resolve=true`. `PATH` alone answers it wrongly in
#: both directions (Word is installed and is not on PATH; `cmd` is on PATH
#: because it belongs to Windows), so the probe walks `PATH` *and* the Start
#: Menu using the same resolver the launcher uses, and reports **positive
#: evidence** rather than a guess: see `tools.applications.resolve_app_detail`
#: for why `found: false` is "I found nothing", not "not installed". The
#: default response stays static because a filesystem walk is a cost, and
#: because a caller that only wants the list should not pay for it.
APP_CATALOG = [
    {"id": "chrome", "label": "Google Chrome", "category": "browser",
     "names": ["chrome", "google chrome"]},
    {"id": "firefox", "label": "Firefox", "category": "browser",
     "names": ["firefox"]},
    {"id": "edge", "label": "Microsoft Edge", "category": "browser",
     "names": ["edge", "microsoft edge"]},
    {"id": "vscode", "label": "Visual Studio Code", "category": "editor",
     "names": ["vscode", "vs code", "visual studio code"]},
    {"id": "notepad", "label": "Notepad", "category": "editor",
     "names": ["notepad"]},
    {"id": "notepad++", "label": "Notepad++", "category": "editor",
     "names": ["notepad++"]},
    {"id": "cmd", "label": "Command Prompt", "category": "terminal",
     "names": ["cmd", "command prompt"]},
    {"id": "powershell", "label": "PowerShell", "category": "terminal",
     "names": ["powershell"]},
    {"id": "terminal", "label": "Terminal", "category": "terminal",
     "names": ["terminal"]},
    {"id": "explorer", "label": "File Explorer", "category": "system",
     "names": ["file explorer", "explorer"]},
    {"id": "calculator", "label": "Calculator", "category": "system",
     "names": ["calculator"]},
    {"id": "settings", "label": "Settings", "category": "system",
     "names": ["settings"]},
    {"id": "control panel", "label": "Control Panel", "category": "system",
     "names": ["control panel"]},
    {"id": "task manager", "label": "Task Manager", "category": "system",
     "names": ["task manager"]},
    {"id": "regedit", "label": "Registry Editor", "category": "system",
     "names": ["regedit"]},
    {"id": "spotify", "label": "Spotify", "category": "media",
     "names": ["spotify"]},
    {"id": "paint", "label": "Paint", "category": "media",
     "names": ["paint"]},
    {"id": "fxsound", "label": "FxSound", "category": "media",
     "names": ["fxsound"]},
    {"id": "discord", "label": "Discord", "category": "chat",
     "names": ["discord"]},
    {"id": "slack", "label": "Slack", "category": "chat",
     "names": ["slack"]},
    {"id": "zoom", "label": "Zoom", "category": "chat",
     "names": ["zoom"]},
    {"id": "teams", "label": "Microsoft Teams", "category": "chat",
     "names": ["teams", "microsoft teams"]},
    {"id": "outlook", "label": "Outlook", "category": "office",
     "names": ["outlook"]},
    {"id": "word", "label": "Word", "category": "office",
     "names": ["word"]},
    {"id": "excel", "label": "Excel", "category": "office",
     "names": ["excel"]},
    {"id": "powerpoint", "label": "PowerPoint", "category": "office",
     "names": ["powerpoint"]},
]


@app.get("/computer/apps")
async def computer_apps(resolve: bool = False):
    """The applications this build may launch, as display metadata.

    Read-only: it reports the allow-list `POST /command/open-app` enforces, so
    a panel built from it cannot offer a name the launcher will refuse.

    `?resolve=true` additionally reports, per application, whether this machine
    has **positive evidence** of it (`installed`, `via`) — produced by the very
    resolver the launcher will call, so the panel and the launcher cannot
    disagree about what "available" means. Two things keep this honest:

    * it is opt-in, because it costs a filesystem walk of the Start Menu, and
      a caller that only wants the list should not pay for it;
    * the batch shares one walk (`build_shortcut_index` passed down as
      `shortcut_index`), rather than twenty callers walking the same tree.

    `installed: false` is not a claim that the application is missing — see
    `resolve_app_detail`. The response says which answer it carried in
    `resolved`, so a caller never has to infer it from the fields, and reports
    `resolve_error` when it asked and could not find out.
    """
    apps = [
        {"id": e["id"], "label": e["label"], "category": e["category"]}
        for e in APP_CATALOG
    ]
    # First-seen order, so the panel's grouping never reshuffles between calls.
    categories = list(dict.fromkeys(e["category"] for e in APP_CATALOG))

    if not resolve:
        return {"apps": apps, "categories": categories, "total": len(apps),
                "resolved": False}

    try:
        # `resolve_app_detail` is sync filesystem work; off the event loop it
        # goes, and once for the whole batch.
        index = await asyncio.to_thread(build_shortcut_index)
        for app in apps:
            detail = await asyncio.to_thread(
                resolve_app_detail, app["id"], shortcut_index=index
            )
            app["installed"] = bool(detail["found"])
            app["via"] = detail["via"]
    except Exception as exc:  # noqa: BLE001 - degrade, never 500
        # One unreadable directory must not take the panel with it. The
        # Computer screen loads four panels at once, and a route that raises
        # here renders as a blank section — so the verdict is withheld and
        # *says* it was withheld. `resolved: false` with a reason is the same
        # honest answer the field always gives: "I did not find out".
        logger.warning("availability probe failed: %s", exc)
        for app in apps:
            app.pop("installed", None)
            app.pop("via", None)
        return {
            "apps": apps,
            "categories": categories,
            "total": len(apps),
            "resolved": False,
            "resolve_error": "probe failed",
        }

    return {
        "apps": apps,
        "categories": categories,
        "total": len(apps),
        "resolved": True,
        "found": sum(1 for a in apps if a["installed"]),
    }


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


@app.get("/events/recent")
async def recent_events(limit: int = 20, event_type: str = Query("", alias="type")):
    """The core event bus's ring buffer, for diagnostics (§33).

    `core.events` already keeps the last 100 events in-process; this makes
    them inspectable *after* a turn fails instead of only while a socket is
    attached — a debugger's view, not a message queue.

    - **Bounded by construction**: `EventBus.recent()` clamps `limit` to the
      buffer's own cap, so no query shape can widen it.
    - **Redacted on the way out** (`redact_payload`): a `task.log` message can
      quote a command line that carried a credential, and a diagnostics
      endpoint is exactly where such a line gets pasted.
    - An unknown `type` returns an empty list with 200: filtering for an event
      class that has not happened yet is a normal diagnostic query, not a
      client error.
    """
    from common.reporting import redact_payload
    from core.events import get_bus

    bus = get_bus()
    events = [redact_payload(event.to_dict()) for event in bus.recent(limit)]
    if event_type:
        events = [event for event in events if event.get("type") == event_type]
    return {
        "events": events,
        "count": len(events),
        "subscribers": bus.subscriber_count,
    }


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
