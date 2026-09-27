import os
import sys
import time
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response

from agents import (
    RouterAgent, PlannerAgent, FastAgent, CodeAgent,
    ToolAgent, MemoryAgent, SchedulerAgent,
)
from voice import SpeechToText, TextToSpeech
from memory import ShortTermMemory, UserPreferences
from scheduler import SchedulerManager, TimeParser
from reminder import ReminderService, NotificationManager
from tools.base import OSToolkit
from tools.executor import ToolExecutor
from config import config
from arabic import detect_language, get_tts_voice, clean_and_detect
from agents.base_agent import close_shared_client
from services.llm_client import get_llm_client
from tools.safety import ConfirmationManager, RiskAssessor

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
    logger.info(
        "LLM provider=%s endpoint=%s (key=%s)", config.PROVIDER,
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
        # Fail closed and loudly: these would be refused on the request path.
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
    logger.info("Zaytoon AI started on %s:%s", config.BACKEND_HOST, config.BACKEND_PORT)
    yield
    await scheduler.stop()
    await close_shared_client()
    await get_llm_client().aclose()
    logger.info("Zaytoon AI backend shut down")


app = FastAPI(title="Zaytoon AI Assistant", version="3.0.0", lifespan=lifespan)

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


@app.get("/health")
async def health():
    client = get_llm_client()
    # build_chain() raises when the key is missing or a model is not
    # allow-listed. /health must stay answerable in exactly those situations,
    # so report the error instead of propagating it.
    try:
        chain = [
            {"model": e.model, "provider": e.provider, "url": e.url}
            for e in client.build_chain()
        ]
        chain_error = None
    except Exception as e:
        chain = []
        chain_error = str(e)

    return {
        "status": "ok" if config.llm_configured else "degraded",
        "version": "3.0.0",
        "llm": {
            **config.describe_llm(),
            "chain": chain,
            "chain_error": chain_error,
            "model_health": client.health_snapshot(),
            "metrics": client.metrics.to_dict(),
        },
        "agents": ["router", "planner", "fast", "code", "tool", "memory", "scheduler"],
        "safety": {
            "enabled": config.TOOL_SAFETY_ENABLED,
            "confirm_timeout": config.TOOL_CONFIRM_TIMEOUT,
            "always_confirm": sorted(risk_assessor.always_confirm),
        },
        "voice": {"stt": stt.available, "tts": tts._available},
        "workspace": config.describe_workspace(),
        "tools_loaded": len(os_toolkit.list_tools()),
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
        return {"error": "not found"}, 404
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
            return {"error": "Invalid time format. Use ISO 8601."}, 400
    else:
        return {"error": "scheduled_time is required"}, 400

    result = await reminder_service.create(
        task=task,
        scheduled_time=scheduled,
        recurring=recurring,
        session_id=session_id,
    )
    if not result["success"]:
        return {"error": result["error"]}, 400
    return result["reminder"]


@app.delete("/tasks/{task_id}")
async def delete_task(task_id: int):
    ok = await reminder_service.delete(task_id)
    if not ok:
        return {"error": "not found"}, 404
    return {"status": "deleted", "id": task_id}


@app.patch("/tasks/{task_id}/done")
async def mark_task_done(task_id: int):
    ok = await reminder_service.mark_completed(task_id)
    if not ok:
        return {"error": "not found"}, 404
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
        info["error"] = str(e)
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
        return {"error": "No workspace path provided"}, 400

    candidate = Path(os.path.expandvars(raw)).expanduser()
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        resolved = candidate.resolve()
    except OSError as e:
        logger.warning("Rejected workspace %r: %s", raw, e)
        return {"error": f"Cannot use that directory: {e}"}, 400

    if not resolved.is_dir():
        return {"error": "Not a directory"}, 400

    config.WORKSPACE_ROOT = str(resolved)
    # Persist so a restart keeps the user's choice.
    try:
        await user_prefs.set("workspace_root", str(resolved))
    except Exception as e:  # preferences are best-effort
        logger.warning("Could not persist workspace preference: %s", e)

    logger.info("Workspace root set to %s", resolved)
    return {"status": "saved", **config.describe_workspace()}


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
    command = (data.get("command") or "").strip()
    if not command:
        return {"success": False, "error": "No command provided"}

    # The REST surface has no approval UI, so high-risk commands are denied
    # rather than silently executed. The WebSocket path supports confirmation.
    assessment = risk_assessor.assess("system_command", {"command": command})
    if assessment.requires_confirmation:
        logger.warning("REST /command/run blocked %s-level command: %s",
                       assessment.permission, command)
        return {
            "success": False,
            "requires_confirmation": True,
            "level": assessment.level.value,
            "permission": assessment.permission,
            "reasons": assessment.reasons,
            "summary": assessment.summary,
            "error": "High-risk command requires confirmation. Use the WebSocket API.",
        }

    result = await tool_executor.execute("system_command", {"command": command})
    return result


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
