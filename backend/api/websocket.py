import asyncio
import base64
import json
import logging
import time
import uuid
from datetime import datetime
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect

from domain.models import SessionState, TokenBucketRateLimiter, SpokenSentenceTracker
from arabic import clean_and_detect, detect_language, get_tts_voice
from agents.events import AgentEvent, AgentEventType, EventBus
from services.wake_word_service import apply_wake_gate
from tools.safety import ConfirmationManager, PendingConfirmation

logger = logging.getLogger("Zaytoon.WebSocket")

MAX_SESSIONS = 100
session_states: dict[str, SessionState] = {}


class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def _send(self, ws: WebSocket, data: dict):
        try:
            raw = json.dumps(data, default=str)
            logger.warning(f"WS >> type={data.get('type','?')} len={len(raw)}")
            await asyncio.wait_for(
                ws.send_text(raw), timeout=5.0
            )
        except Exception:
            self.disconnect(ws)

    async def send(self, ws: WebSocket, data: dict):
        await self._send(ws, data)

    async def broadcast(self, data: dict):
        if not self.active:
            return
        await asyncio.gather(
            *[self._send(ws, data) for ws in self.active.copy()],
            return_exceptions=True,
        )


def _get_complete_sentences(text: str, split_fn) -> list[str]:
    if not text.strip():
        return []
    return [s for s in split_fn(text) if s.strip().rstrip().endswith((".", "!", "?", "\u061F"))]


def _cleanup_stale_sessions():
    now = time.time()
    stale = [sid for sid, s in session_states.items() if now - s._created_at > 1800]
    for sid in stale:
        session_states.pop(sid, None)


async def create_websocket_handler(
    websocket: WebSocket,
    rate_limiter: TokenBucketRateLimiter,
    connection_manager: ConnectionManager,
    stt,
    tts,
    memory_agent,
    short_term_memory,
    agent_service,
    router_agent,
    scheduler,
    reminder_service,
    config,
):
    client_ip = websocket.client.host
    if not rate_limiter.is_allowed(client_ip):
        await websocket.close(code=1008, reason="Rate limit exceeded")
        return

    await connection_manager.connect(websocket)
    session_id = f"sess_{int(time.time())}_{hash(client_ip) % 10000}"

    _cleanup_stale_sessions()
    if len(session_states) >= MAX_SESSIONS:
        oldest = min(session_states.keys(), key=lambda k: session_states[k]._created_at)
        session_states.pop(oldest, None)
    session_states[session_id] = SessionState()

    # Human-in-the-loop channel: forwards approval requests to this socket and
    # resolves them when the client replies with a `tool_confirm` message.
    async def _notify_confirmation(pending: PendingConfirmation) -> None:
        await connection_manager.send(websocket, pending.to_event())

    session_states[session_id].confirmation_manager = ConfirmationManager(
        notify=_notify_confirmation,
        timeout=config.TOOL_CONFIRM_TIMEOUT,
    )

    try:
        await connection_manager.send(websocket, {
            "type": "status", "content": "connected", "session_id": session_id,
        })

        while True:
            raw = await websocket.receive_text()
            logger.warning(f"WS << len={len(raw)} first80={raw[:80]}")
            data = json.loads(raw)
            msg_type = data.get("type", "message")

            if msg_type == "interrupt":
                state = session_states.get(session_id)
                if state:
                    state.cancel()
                    await connection_manager.send(websocket, {"type": "interrupted"})

            elif msg_type == "message":
                user_message = data.get("message", "").strip()
                if not user_message:
                    continue
                if len(user_message) > config.MAX_MESSAGE_LENGTH:
                    await connection_manager.send(websocket, {
                        "type": "error",
                        "content": f"Message too long. Max {config.MAX_MESSAGE_LENGTH} characters.",
                    })
                    continue
                state = session_states.get(session_id)
                if state:
                    state.cancel()
                task = asyncio.create_task(
                    _handle_message(
                        websocket, user_message, session_id, connection_manager,
                        stt, tts, memory_agent, short_term_memory,
                        agent_service, router_agent, scheduler, config,
                    )
                )
                if state:
                    state.current_task = task

            elif msg_type == "audio":
                audio_b64 = data.get("audio", "")
                language = data.get("language", None)
                # Manual voice mode (user pressed mic button) skips wake-word gate
                is_manual_voice = data.get("isManualVoice", False)
                logger.warning(f"Audio msg received: b64_len={len(audio_b64)} lang={language} manual={is_manual_voice}")
                if not audio_b64:
                    continue
                try:
                    if stt.loading:
                        logger.warning("STT is still loading, sending error")
                        await connection_manager.send(websocket, {
                            "type": "error",
                            "content": "Voice model is still downloading. Please wait.",
                        })
                        continue
                    if not stt.available:
                        logger.warning("STT not available, sending error")
                        await connection_manager.send(websocket, {
                            "type": "error",
                            "content": "Speech-to-text not available. Type instead.",
                        })
                        continue
                    audio_bytes = base64.b64decode(audio_b64)
                    logger.warning(f"Audio received: {len(audio_bytes)} bytes, b64_prefix={audio_b64[:12]}, hex_prefix={audio_bytes[:8].hex()}")
                    await connection_manager.send(websocket, {"type": "status", "content": "transcribing"})
                    transcription = stt.transcribe_detailed(
                        audio_bytes, language=language
                    )
                    text = transcription.text
                    if not text:
                        await connection_manager.send(websocket, {"type": "status", "content": "listening"})
                        continue
                    # Whisper's verdict beats the client's hint, so voice
                    # routing and the agent's language kwarg stay in sync.
                    if transcription.detected and transcription.language:
                        language = transcription.language
                        await connection_manager.send(websocket, {
                            "type": "stt_result",
                            "language": language,
                            "voice": transcription.voice_hint,
                            "duration": transcription.duration,
                        })

                    state = session_states.get(session_id)
                    # Skip wake-word gate for manual voice mode (user explicitly pressed mic)
                    wake_enabled = config.WAKE_WORD_REQUIRED and not is_manual_voice
                    wake = apply_wake_gate(
                        text,
                        awaiting_command=bool(state.awaiting_command) if state else False,
                        last_active_ts=state.last_active_ts if state else 0.0,
                        timeout_secs=config.WAKE_SESSION_TIMEOUT,
                        enabled=wake_enabled,
                    )
                    if wake.detected and state:
                        # A wake word was spoken: arm the follow-up window so the
                        # user does not have to repeat it for the next command.
                        state.awaiting_command = not wake.acted
                    if wake.dropped:
                        # Not addressed to Zaytoon. Say so, otherwise the user
                        # just sees the app go quiet and assumes it is broken.
                        if config.WAKE_WORD_HINT:
                            await connection_manager.send(websocket, {
                                "type": "wake_word", "detected": False,
                            })
                        await connection_manager.send(websocket, {
                            "type": "status", "content": "listening",
                        })
                        continue
                    if wake.detected:
                        await connection_manager.send(websocket, {
                            "type": "wake_word", "detected": True,
                        })
                        if not wake.acted:
                            await connection_manager.send(websocket, {
                                "type": "status", "content": "listening",
                            })
                            continue
                    if wake.acted:
                        text = wake.text

                    if state and state.awaiting_command:
                        state.awaiting_command = False
                    if state:
                        state.cancel()
                    task = asyncio.create_task(
                        _handle_message(
                            websocket, text, session_id, connection_manager,
                            stt, tts, memory_agent, short_term_memory,
                            agent_service, router_agent, scheduler, config,
                            language_hint=language,
                        )
                    )
                    if state:
                        state.current_task = task

                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

                    state = session_states.get(session_id)
                except Exception as e:
                    logger.warning(f"Audio processing error: {e}")
                    await connection_manager.send(websocket, {"type": "status", "content": "listening"})

            # NOTE: there is deliberately no `command` message type. An earlier
            # revision accepted {"type":"command","command":...} and routed it
            # straight to `OSToolkit.execute`, bypassing RiskAssessor and the
            # ConfirmationManager entirely. Because a WebSocket handshake is not
            # subject to CORS, any web page the user visited could open
            # ws://127.0.0.1:8000/ws and drive that path. Every legitimate tool
            # call now goes through the gated `ToolExecutor` via the `message`
            # flow above. Do not reintroduce a raw command channel.

            elif msg_type == "reminder_cmd":
                action = data.get("action", "")
                rid = data.get("id")
                lang = data.get("language", "en")
                if action == "list":
                    reminders = await reminder_service.list(session_id=session_id)
                    display = await reminder_service.format_list_for_display(reminders, lang)
                    await connection_manager.send(websocket, {
                        "type": "reminder_list", "reminders": reminders, "display": display, "language": lang,
                    })
                elif action == "delete" and rid is not None:
                    ok = await reminder_service.delete(int(rid))
                    msg = "Deleted." if lang == "en" else "تم الحذف."
                    await connection_manager.send(websocket, {
                        "type": "reminder_deleted", "id": int(rid), "success": ok, "message": msg,
                    })
                elif action == "done" and rid is not None:
                    ok = await reminder_service.mark_completed(int(rid))
                    msg = "Marked as done." if lang == "en" else "تم."
                    await connection_manager.send(websocket, {
                        "type": "reminder_done", "id": int(rid), "success": ok, "message": msg,
                    })

            elif msg_type == "tool_confirm":
                state = session_states.get(session_id)
                request_id = data.get("request_id", "")
                approved = bool(data.get("approved", False))
                manager = state.confirmation_manager if state else None
                resolved = manager.resolve(request_id, approved) if manager else False
                logger.info(
                    "tool_confirm request_id=%s approved=%s resolved=%s",
                    request_id, approved, resolved,
                )
                await connection_manager.send(websocket, {
                    "type": "confirmation_result",
                    "request_id": request_id,
                    "approved": approved,
                    "resolved": resolved,
                })

            elif msg_type == "ping":
                await connection_manager.send(websocket, {"type": "pong"})

    except WebSocketDisconnect:
        connection_manager.disconnect(websocket)
    except json.JSONDecodeError:
        pass
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        connection_manager.disconnect(websocket)
    finally:
        state = session_states.pop(session_id, None)
        if state:
            if state.confirmation_manager:
                state.confirmation_manager.cancel_all("websocket closed")
            state.cancel()


async def _stream_tts_chunks(
    websocket: WebSocket,
    text: str,
    language: str,
    tts,
    connection_manager: ConnectionManager,
    cancel_event: asyncio.Event | None = None,
    spoken_tracker: SpokenSentenceTracker | None = None,
    config=None,
):
    had_audio = False
    try:
        if cancel_event and cancel_event.is_set():
            return

        new_text = text
        if spoken_tracker:
            new_sentences = spoken_tracker.get_new_sentences(text, tts.split_sentences)
            if not new_sentences:
                return
            new_text = " ".join(new_sentences)

        if not new_text.strip():
            return

        if spoken_tracker:
            for s in tts.split_sentences(new_text):
                spoken_tracker.mark_spoken(s)

        if cancel_event and cancel_event.is_set():
            return

        # Route the voice from the text being spoken, so a mid-stream sentence
        # switches language correctly even if the session default differs.
        # `tts.stream_synthesize` also auto-selects; passing the resolved voice
        # keeps `audio_start` and the actual synthesis in agreement.
        voice = tts.resolve_voice(language)

        subtitle_text = new_text[:200]
        if subtitle_text:
            await connection_manager.send(websocket, {"type": "subtitle", "content": subtitle_text})

        if cancel_event and cancel_event.is_set():
            return

        await connection_manager.send(websocket, {"type": "status", "content": "speaking"})
        await connection_manager.send(websocket, {
            "type": "audio_start", "language": language,
            "voice": voice, "format": "mpeg",
        })
        had_audio = True

        chunk_index = 0
        try:
            async for audio_chunk in tts.stream_synthesize(
                new_text, voice=voice, language=language,
                rate=config.TTS_RATE, volume=config.TTS_VOLUME,
            ):
                if cancel_event and cancel_event.is_set():
                    break
                if audio_chunk:
                    b64 = base64.b64encode(audio_chunk).decode()
                    await connection_manager.send(websocket, {
                        "type": "audio_chunk", "data": b64, "index": chunk_index,
                    })
                    chunk_index += 1
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"TTS stream failed: {e}")
            await connection_manager.send(websocket, {
                "type": "error", "content": f"TTS failed: {e}",
            })

        logger.info(f"TTS: {chunk_index} chunks, voice='{voice}'")
    finally:
        if had_audio:
            await connection_manager.send(websocket, {"type": "audio_end"})


class _ProgressRelay:
    """Translate agent events into WebSocket frames.

    This is the seam that keeps the agent independent of its transport. Each
    agent event maps to at most one frame:

    ==================  ==================================================
    thinking            ``status: thinking``  (no separate frame needed)
    planning            ``status: planning`` + ``plan`` with user-facing steps
    tool_started        ``tool_started``    {name, label}
    tool_progress       ``tool_progress``   {name, message}
    tool_finished       ``tool_finished``   {name, success, summary}
    response_chunk      handled by the existing ``token`` stream
    error               ``error``           {content}
    completed           ``status: done``
    ==================  ==================================================

    Only ``label``/``summary`` cross the wire — the fixed, human-written strings
    from ``agents/engine.py``. The model's own reasoning is never emitted, which
    is why this class needs no redaction logic.
    """

    def __init__(self, websocket, connection_manager: ConnectionManager, response_id: str):
        self._ws = websocket
        self._cm = connection_manager
        self._response_id = response_id

    async def __call__(self, event: AgentEvent) -> None:
        kind = event.type
        data = event.data or {}

        if kind is AgentEventType.THINKING:
            await self._cm.send(self._ws, {"type": "status", "content": "thinking"})

        elif kind is AgentEventType.PLANNING:
            steps = [s for s in (data.get("steps") or []) if s]
            await self._cm.send(self._ws, {"type": "status", "content": "planning"})
            if steps:
                await self._cm.send(
                    self._ws,
                    {"type": "plan", "steps": steps, "response_id": self._response_id},
                )

        elif kind is AgentEventType.TOOL_STARTED:
            await self._cm.send(
                self._ws,
                {
                    "type": "tool_started",
                    "name": data.get("name", ""),
                    "label": data.get("label", ""),
                    "response_id": self._response_id,
                },
            )

        elif kind is AgentEventType.TOOL_PROGRESS:
            await self._cm.send(
                self._ws,
                {
                    "type": "tool_progress",
                    "name": data.get("name", ""),
                    "message": data.get("message", ""),
                },
            )

        elif kind is AgentEventType.TOOL_FINISHED:
            await self._cm.send(
                self._ws,
                {
                    "type": "tool_finished",
                    "name": data.get("name", ""),
                    "success": bool(data.get("success")),
                    "summary": str(data.get("summary", ""))[:200],
                },
            )

        elif kind is AgentEventType.RESPONSE_CHUNK:
            # Already delivered by the `token` stream via stream_callback.
            return

        elif kind is AgentEventType.ERROR:
            await self._cm.send(
                self._ws,
                {"type": "error", "content": str(data.get("content", ""))[:500]},
            )

        elif kind is AgentEventType.COMPLETED:
            # The transport owns the terminal `done`; the engine only says the
            # turn is over, because only this layer knows about TTS drain and
            # the message frame that must land first.
            return


async def _handle_message(
    websocket: WebSocket,
    user_message: str,
    session_id: str,
    connection_manager: ConnectionManager,
    stt,
    tts,
    memory_agent,
    short_term_memory,
    agent_service,
    router_agent,
    scheduler,
    config,
    language_hint: Optional[str] = None,
):
    state = session_states.get(session_id)
    if state:
        state.reset()

    cancel_event = state.cancel_event if state else None
    spoken_tracker = state.spoken_tracker if state else None
    response_id = state.current_response_id if state else str(uuid.uuid4())[:8]
    final_content: str = ""
    tts_triggered = False

    cleaning_result = clean_and_detect(user_message)
    cleaned_message = cleaning_result["text"]
    language = cleaning_result["language"]

    # Whisper's language detection is more reliable than sniffing the
    # transcript, so an STT hint wins for short/ambiguous utterances.
    if language_hint:
        from voice import routing
        hinted = routing.normalize_language(language_hint)
        if hinted != "en" or language == "en":
            language = hinted

    logger.warning(f"[HANDLE_MSG] msg='{cleaned_message[:80]}' lang={language} session={session_id}")

    await memory_agent.store_message("user", cleaned_message, language=language, session_id=session_id)
    short_term_memory.put("last_query", cleaned_message, {"label": "user_query", "language": language})

    context = await memory_agent.get_recent_context(session_id=session_id)

    asyncio.create_task(memory_agent.summarize_and_prune(session_id))

    if cancel_event and cancel_event.is_set():
        logger.warning(f"[HANDLE_MSG] CANCELLED before dispatch (msg='{cleaned_message[:40]}')")
        return

    await connection_manager.send(websocket, {"type": "status", "content": "thinking"})

    route_result = await router_agent.route(cleaned_message, context)
    intent = route_result.get("intent", "chat")

    logger.warning(f"[HANDLE_MSG] intent={intent} for msg='{cleaned_message[:60]}'")

    if cancel_event and cancel_event.is_set():
        logger.warning(f"[HANDLE_MSG] CANCELLED after routing (msg='{cleaned_message[:40]}')")
        return

    logger.info(f"Session {session_id}: intent={intent}, lang={language}")

    await connection_manager.send(websocket, {
        "type": "intent", "intent": intent, "language": language, "response_id": response_id,
    })

    token_buffer: list[str] = []

    confirmation_manager = state.confirmation_manager if state else None

    async def flush_tokens():
        if not token_buffer:
            return
        chunk = "".join(token_buffer)
        token_buffer.clear()
        await connection_manager.send(websocket, {
            "type": "token", "content": chunk, "response_id": response_id,
        })

    async def stream_callback(token: str):
        nonlocal tts_triggered, final_content
        if cancel_event and cancel_event.is_set():
            return
        if not token.strip():
            return

        final_content += token
        token_buffer.append(token)

        if len(token_buffer) >= 10 or token in ".!?\n":
            await flush_tokens()

        if not tts_triggered and config.TTS_STREAM_EARLY and len(final_content) > 30:
            complete = _get_complete_sentences(final_content, tts.split_sentences)
            if complete:
                tts_triggered = True
                await flush_tokens()
                await connection_manager.send(websocket, {"type": "status", "content": "speaking"})
                tts_task = asyncio.create_task(
                    _stream_tts_chunks(
                        websocket, complete[0], detect_language(complete[0]),
                        tts, connection_manager, cancel_event, spoken_tracker, config,
                    )
                )
                if state:
                    state.tts_task = tts_task

    # The agent's progress channel. The engine emits semantic events; this
    # subscriber is the only place that knows the wire format, so the agent
    # stays transport-agnostic and can be driven by the desktop shell or a test
    # with equal ease.
    progress = _ProgressRelay(websocket, connection_manager, response_id)
    bus = EventBus()
    bus.subscribe(progress)

    try:
        agent_output = await agent_service.dispatch(
            intent, cleaned_message, context, language, session_id,
            stream_callback=stream_callback,
            confirmation_manager=confirmation_manager,
            event_bus=bus,
            response_id=response_id,
        )
        if agent_output:
            final_content = agent_output
    except asyncio.CancelledError:
        logger.info(f"Session {session_id}: cancelled")
        return

    await flush_tokens()

    if cancel_event and cancel_event.is_set():
        return

    if not final_content.strip():
        await connection_manager.send(websocket, {
            "type": "status", "content": "done", "response_id": response_id,
        })
        return

    if state and state.is_duplicate(final_content):
        logger.warning(f"Session {session_id}: duplicate blocked")
        return

    response_language = detect_language(final_content) or language

    await connection_manager.send(websocket, {
        "type": "message", "content": final_content, "response_id": response_id,
    })

    await memory_agent.store_message(
        "assistant", final_content, language=response_language, session_id=session_id,
    )

    if state and state.tts_task and not state.tts_task.done():
        try:
            await state.tts_task
        except Exception:
            pass

    if not tts_triggered:
        await connection_manager.send(websocket, {"type": "status", "content": "speaking"})
        tts_task = asyncio.create_task(
            _stream_tts_chunks(
                websocket, final_content, response_language,
                tts, connection_manager, cancel_event, spoken_tracker, config,
            )
        )
        if state:
            state.tts_task = tts_task
    elif tts_triggered and spoken_tracker:
        remaining = spoken_tracker.get_new_sentences(final_content, tts.split_sentences)
        if remaining:
            await connection_manager.send(websocket, {"type": "status", "content": "speaking"})
            tts_task = asyncio.create_task(
                _stream_tts_chunks(
                    websocket, " ".join(remaining), response_language,
                    tts, connection_manager, cancel_event, spoken_tracker, config,
                )
            )
            if state:
                state.tts_task = tts_task

    await connection_manager.send(websocket, {
        "type": "status", "content": "done", "response_id": response_id,
    })

    if state:
        state.last_active_ts = time.time()

    await connection_manager.send(websocket, {"type": "status", "content": "listening"})

    logger.info(
        f"Session {session_id}: done intent={intent}, "
        f"tts_early={tts_triggered}, final_len={len(final_content)}"
    )
