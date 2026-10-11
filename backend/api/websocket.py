import asyncio
import base64
import json
import logging
import time
import uuid
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect

from domain.models import SessionState, TokenBucketRateLimiter, SpokenSentenceTracker
from arabic import clean_and_detect, detect_language
from agents.events import AgentEvent, AgentEventType, EventBus
from common.reporting import client_error, safe_error
from common.tracing import set_trace_id
from services.wake_word_service import apply_wake_gate
from tools.safety import ConfirmationManager, PendingConfirmation
from core.events import bus as core_bus
from core.orchestrator import Orchestrator
from core.workspace.terminal import get_manager as get_terminal_manager

logger = logging.getLogger("Zaytoon.WebSocket")

MAX_SESSIONS = 100
session_states: dict[str, SessionState] = {}

#: The turn runner for each live session, keyed by session id. Each WebSocket
#: connection builds its own from *that handler's* dependencies — the transport
#: and the orchestrator must share one AgentService/RouterAgent instance, which
#: is also what lets a test that stubs the agents exercise the real turn
#: sequence instead of the module singleton's. Keyed by session so the `task`
#: cancel frame can reach the same instance's handle registry.
_SESSION_ORCHESTRATORS: dict[str, Orchestrator] = {}

# Global request tracer: trace_id -> {user_message, response_id, started_at}
_request_traces: dict[str, dict] = {}
_MAX_TRACES = 1000

#: Terminal sink unsubscribes, keyed by socket identity. A subscription with no
#: way to detach is a leak that only shows up as a growing list of dead sockets,
#: so they are tracked here and released when the socket closes.
_TERMINAL_SUBSCRIPTIONS: dict = {}

# Client-generated message ids already accepted, per session. A client that
# retries a send — a double click, a reconnect replaying its last frame, a
# socket library redelivering — must produce exactly ONE logical turn, and
# content-based duplicate detection cannot see that: the second copy arrives
# *before* the first turn has produced any content to compare against.
#
# Deliberately opt-in and short-lived. A frame without a `client_msg_id` is
# accepted exactly as before, so an older client (the desktop shell) is
# unaffected, and an id expires quickly because a resend that takes longer than
# this is a new request, not a retry.
_SEEN_MESSAGE_IDS: dict[str, "dict[str, float]"] = {}
_SEEN_ID_TTL_S = 60.0
_SEEN_ID_MAX = 100


def is_replayed_message(session_id: str, message_id: Optional[str]) -> bool:
    """True when this ``message_id`` is a resend of one already accepted.

    Prunes expired and surplus ids on the way, so the table is bounded by the
    TTL and the cap rather than by session lifetime. Not async and not locked:
    it is called from the single-threaded event loop between awaits, so no other
    task can interleave inside it.
    """
    if not message_id or not isinstance(message_id, str):
        return False  # no id: nothing to dedupe against, accept as before
    token = message_id[:64]

    now = time.monotonic()
    seen = _SEEN_MESSAGE_IDS.setdefault(session_id, {})
    for key, stamp in list(seen.items()):
        if now - stamp > _SEEN_ID_TTL_S:
            seen.pop(key, None)
    if len(seen) >= _SEEN_ID_MAX:
        # Pruned *before* this id is inserted, so the room for it is reserved
        # here — otherwise the table settles at MAX+1 and the cap is a lie.
        for key, _ in sorted(seen.items(), key=lambda kv: kv[1])[
            : len(seen) - _SEEN_ID_MAX + 1
        ]:
            seen.pop(key, None)

    if token in seen:
        return True
    seen[token] = now
    return False


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
            logger.warning(f"WS >> type={data.get('type','?')} len={len(raw)} trace={data.get('trace_id','-')}")
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

    # Also cleanup old traces
    if len(_request_traces) > _MAX_TRACES:
        # Remove oldest traces
        sorted_traces = sorted(_request_traces.items(), key=lambda x: x[1].get("started_at", 0))
        for trace_id, _ in sorted_traces[:len(_request_traces) - _MAX_TRACES // 2]:
            _request_traces.pop(trace_id, None)


def _create_trace(user_message: str, session_id: str) -> str:
    """Create a trace record for a user request."""
    trace_id = f"tr_{uuid.uuid4().hex[:12]}"
    _request_traces[trace_id] = {
        "user_message": user_message[:200],
        "session_id": session_id,
        "response_id": None,
        "started_at": time.time(),
        "status": "started",
    }
    return trace_id


def _update_trace(trace_id: str, **updates):
    """Update a trace record."""
    if trace_id in _request_traces:
        _request_traces[trace_id].update(updates)


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
    _SESSION_ORCHESTRATORS[session_id] = Orchestrator(
        agent_service=agent_service,
        router_agent=router_agent,
        memory_agent=memory_agent,
    )

    # Core events (task.*, file.changed, terminal.output, …) reach this socket as
    # frames. The core bus is process-wide and every session subscribes to it, so
    # a task started from the browser is visible in the desktop shell and vice
    # versa — which is the point of having one core. Each subscriber is filtered
    # to its own session where the event carries a session id, so two open
    # clients do not receive each other's task progress.
    async def _relay_core(event) -> None:
        data = event.data or {}
        session_filter = data.get("session_id")
        if session_filter and session_filter != session_id:
            return
        await connection_manager.send(websocket, event.to_dict())

    unsubscribe_core = core_bus.subscribe(_relay_core)

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
            # One malformed frame must not end the session. This parses *inside*
            # the loop deliberately: the handler used to sit outside it, so a
            # single non-JSON line fell through to the outer ``except`` and
            # dropped the connection — cancelling the in-flight turn with it.
            # Both clients tolerate a bad frame from *us* (``useWebSocket``
            # ignores an unparseable frame, ``ServerEvent.parse`` returns None);
            # the same courtesy in the other direction is what keeps a transient
            # glitch from becoming a full disconnect.
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                await connection_manager.send(websocket, {
                    "type": "error",
                    "content": "Malformed message ignored.",
                })
                continue
            if not isinstance(data, dict):
                await connection_manager.send(websocket, {
                    "type": "error",
                    "content": "Malformed message ignored.",
                })
                continue
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
                # A resent frame is dropped before anything else happens, so it
                # cannot cancel the in-flight turn, allocate a trace, or reach
                # the router. Silently ignored: the client that resent it
                # already has the original turn's answers.
                client_msg_id = data.get("client_msg_id")
                if is_replayed_message(session_id, client_msg_id):
                    logger.warning(
                        f"Session {session_id}: resent message id dropped "
                        f"(id={str(client_msg_id)[:64]})"
                    )
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
                
                # Create trace for this user action
                trace_id = _create_trace(user_message, session_id)
                response_id = state.current_response_id if state else str(uuid.uuid4())[:8]
                _update_trace(trace_id, response_id=response_id, status="routing")
                
                task = asyncio.create_task(
                    _handle_message(
                        websocket, user_message, session_id, connection_manager,
                        stt, tts, memory_agent, short_term_memory,
                        agent_service, router_agent, scheduler, config,
                        trace_id=trace_id,
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
                    
                    # Create trace for voice message
                    trace_id = _create_trace(text, session_id)
                    response_id = state.current_response_id if state else str(uuid.uuid4())[:8]
                    _update_trace(trace_id, response_id=response_id, status="routing")
                    
                    task = asyncio.create_task(
                        _handle_message(
                            websocket, text, session_id, connection_manager,
                            stt, tts, memory_agent, short_term_memory,
                            agent_service, router_agent, scheduler, config,
                            language_hint=language,
                            trace_id=trace_id,
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
                    safe_error(e, operation="audio", component="api.websocket")
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
                # Storage-backed and user-facing, and handled *outside* the
                # turn task — without this guard an exception here would fall
                # through to the loop's catch-all and drop the socket, so the
                # panel would lose its connection over a failed reminder read.
                await _handle_reminder_cmd(
                    websocket, connection_manager, reminder_service, data, session_id
                )

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

            elif msg_type == "task":
                # Cancel whatever this session is running: the current turn
                # *and* its active task. Separate from `interrupt`, which stops
                # the turn only — a task outlives a turn (a test suite can run
                # after the answer was delivered), so it needs its own handle.
                stopped = await _cancel_session_work(session_id)
                await connection_manager.send(websocket, {
                    "type": "task_result",
                    "cancelled": bool(stopped),
                    "error": "" if stopped else "Nothing is running for this session.",
                })

            elif msg_type == "terminal":
                # Subscribe to one terminal session's output for this socket.
                # Subscribing rather than filtering a global stream means a
                # client is only sent lines from a terminal it asked about.
                target = data.get("session_id")
                if target:
                    await _handle_terminal_sub(websocket, connection_manager, target)

            else:
                await connection_manager.send(websocket, {
                    "type": "error",
                    "content": f"Unknown message type: {msg_type!r}",
                })

    except WebSocketDisconnect:
        connection_manager.disconnect(websocket)
    except json.JSONDecodeError:
        pass
    except Exception as e:
        safe_error(e, operation="ws.receive", component="api.websocket")
        connection_manager.disconnect(websocket)
    finally:
        # Unsubscribe before tearing the session down: a core event arriving
        # after this point would find a closed socket and log a send failure for
        # every one until the subscription was removed.
        unsubscribe_core()
        for detach in _TERMINAL_SUBSCRIPTIONS.pop(id(websocket), []):
            detach()
        _SESSION_ORCHESTRATORS.pop(session_id, None)
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
    trace_id: str = None,
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

        def add_trace(frame: dict) -> dict:
            if trace_id:
                frame["trace_id"] = trace_id
            return frame

        subtitle_text = new_text[:200]
        if subtitle_text:
            await connection_manager.send(websocket, add_trace({"type": "subtitle", "content": subtitle_text}))

        if cancel_event and cancel_event.is_set():
            return

        await connection_manager.send(websocket, add_trace({"type": "status", "content": "speaking"}))
        await connection_manager.send(websocket, add_trace({
            "type": "audio_start", "language": language,
            "voice": voice, "format": "mpeg",
        }))
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
                    await connection_manager.send(websocket, add_trace({
                        "type": "audio_chunk", "data": b64, "index": chunk_index,
                    }))
                    chunk_index += 1
        except asyncio.CancelledError:
            raise
        except Exception as e:
            safe_error(e, operation="tts", component="voice.tts")
            await connection_manager.send(websocket, add_trace({
                "type": "error",
                "content": client_error(
                    e, operation="tts", component="voice.tts",
                    language=language if language in ("en", "ar") else "en",
                ),
            }))

        logger.info(f"TTS: {chunk_index} chunks, voice='{voice}'")
    finally:
        if had_audio:
            await connection_manager.send(websocket, add_trace({"type": "audio_end"}))


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
    sources               ``sources``         {sources: [{n, url, kind, …}]}, capped
    response_chunk      handled by the existing ``token`` stream
    error               ``error``           {content}
    completed           ``status: done``
    ==================  ==================================================

    Only ``label``/``summary`` cross the wire — the fixed, human-written strings
    from ``agents/engine.py``. The model's own reasoning is never emitted, which
    is why this class needs no redaction logic.
    """

    def __init__(self, websocket, connection_manager: ConnectionManager, response_id: str, trace_id: str = None):
        self._ws = websocket
        self._cm = connection_manager
        self._response_id = response_id
        self._trace_id = trace_id

    async def __call__(self, event: AgentEvent) -> None:
        kind = event.type
        data = event.data or {}
        trace_id = self._trace_id

        def add_trace(frame: dict) -> dict:
            if trace_id:
                frame["trace_id"] = trace_id
            return frame

        if kind is AgentEventType.THINKING:
            await self._cm.send(self._ws, add_trace({"type": "status", "content": "thinking"}))

        elif kind is AgentEventType.PLANNING:
            steps = [s for s in (data.get("steps") or []) if s]
            await self._cm.send(self._ws, add_trace({"type": "status", "content": "planning"}))
            if steps:
                await self._cm.send(
                    self._ws,
                    add_trace({"type": "plan", "steps": steps, "response_id": self._response_id}),
                )

        elif kind is AgentEventType.TOOL_STARTED:
            await self._cm.send(
                self._ws,
                add_trace({
                    "type": "tool_started",
                    "name": data.get("name", ""),
                    "label": data.get("label", ""),
                    "response_id": self._response_id,
                }),
            )

        elif kind is AgentEventType.TOOL_PROGRESS:
            await self._cm.send(
                self._ws,
                add_trace({
                    "type": "tool_progress",
                    "name": data.get("name", ""),
                    "message": data.get("message", ""),
                }),
            )

        elif kind is AgentEventType.TOOL_FINISHED:
            await self._cm.send(
                self._ws,
                add_trace({
                    "type": "tool_finished",
                    "name": data.get("name", ""),
                    "success": bool(data.get("success")),
                    "summary": str(data.get("summary", ""))[:200],
                }),
            )

        elif kind is AgentEventType.SOURCES:
            # Structured so a client that wants a citation panel does not have to
            # parse the prose back apart — the same list is already appended to
            # the text, which is what older clients (and the desktop view) show.
            # An unknown `type` is ignored by a client that predates it.
            await self._cm.send(
                self._ws,
                add_trace(
                    {
                        "type": "sources",
                        "sources": list(data.get("sources") or [])[:12],
                    }
                ),
            )

        elif kind is AgentEventType.RESPONSE_CHUNK:
            # Already delivered by the `token` stream via stream_callback.
            return

        elif kind is AgentEventType.ERROR:
            await self._cm.send(
                self._ws,
                add_trace({"type": "error", "content": str(data.get("content", ""))[:500]}),
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
    trace_id: Optional[str] = None,
):
    """The turn boundary: catch everything, report one line, never leak.

    The body lives in :func:`_run_turn`. This wrapper exists because the turn
    is scheduled with ``asyncio.create_task``, where an escaping exception is
    not surfaced to anyone — the client simply never receives another frame and
    the UI stays "thinking" forever. Here the failure is reported three ways:

    * **to the operator**, as one structured line (``safe_error``) plus the
      full traceback in the log;
    * **to the client**, as a fixed bilingual sentence that contains no
      exception text, no path and no implementation name;
    * **to the tracer**, as ``status=error`` with the elapsed time.

    ``REQUEST_DONE`` is logged for *every* exit — success, crash, cancel —
    because a duration that only appears on the happy path is a duration you
    cannot compare against.
    """
    started = time.perf_counter()
    status = "error"
    try:
        try:
            outcome = await _run_turn(
                websocket, user_message, session_id, connection_manager,
                stt, tts, memory_agent, short_term_memory,
                agent_service, router_agent, scheduler, config,
                language_hint=language_hint, trace_id=trace_id,
            )
            # The turn reports its own failure when it has already told the
            # client (the `error` + `done` frames go out inside `_run_turn`):
            # re-raising here would send those frames a second time through
            # this boundary's except.
            status = outcome or "ok"
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - the outermost boundary
            safe_error(exc, operation="turn", component="api.websocket")
            logger.debug("turn failed", exc_info=True)
            content = client_error(
                exc, operation="turn", component="api.websocket",
                language=_language_for(user_message),
            )
            await connection_manager.send(websocket, {
                "type": "error", "content": content, "trace_id": trace_id,
            })
            # A turn that dies must still terminate: without this the client
            # waits on a `done` that is never coming.
            await connection_manager.send(websocket, {
                "type": "status", "content": "done",
                "response_id": None, "trace_id": trace_id,
            })
            _update_trace(trace_id, status="error")
    finally:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        _update_trace(trace_id, duration_ms=elapsed_ms)
        logger.info(
            "REQUEST_DONE trace=%s session=%s ms=%d status=%s",
            trace_id or "-", session_id, elapsed_ms, status,
        )


async def _cancel_session_work(session_id: str) -> bool:
    """Cancel a session's turn and its active task.

    The registry is consulted first: live turns run on the connection's own
    orchestrator (see ``_SESSION_ORCHESTRATORS``), so cancelling via the module
    singleton would set an event nobody is reading. The singleton remains as
    the fallback for a turn started outside a connection (REST), and both
    paths are imported lazily rather than injected: this handler is
    constructed with a long positional argument list already, and adding a
    parameter for one frame would touch the signature every caller and test
    passes.
    """
    runner = _SESSION_ORCHESTRATORS.get(session_id)
    if runner is None:
        try:
            from api.main import orchestrator as runner
        except Exception as exc:  # noqa: BLE001 - a failed cancel must not raise
            logger.warning(
                "Could not cancel session %s: %s", session_id, type(exc).__name__
            )
            return False
    try:
        return await runner.cancel_session_task(session_id)
    except Exception as exc:  # noqa: BLE001 - a failed cancel must not raise
        logger.warning(
            "Could not cancel session %s: %s", session_id, type(exc).__name__
        )
        return False


async def _handle_terminal_sub(
    websocket: WebSocket,
    connection_manager: ConnectionManager,
    session_id: str,
) -> None:
    """Relay one terminal session's output until it exits.

    The manager's sinks are process-wide, so this filters by session id: two
    clients watching two different terminals both get their own lines and
    neither gets the other's.
    """
    manager = get_terminal_manager()
    session = manager.get(session_id)
    if session is None:
        await connection_manager.send(websocket, {
            "type": "error",
            "content": f"No such terminal: {session_id}",
        })
        return

    async def sink(current, entry):
        if current.id != session_id:
            return
        await connection_manager.send(websocket, {
            "type": "terminal_output",
            "session_id": current.id,
            "stream": entry.get("stream", "stdout"),
            "text": entry.get("text", ""),
        })

    unsubscribe = manager.add_sink(sink)
    _TERMINAL_SUBSCRIPTIONS.setdefault(id(websocket), []).append(unsubscribe)

    # Replayed on subscribe, so a client that connects late still sees what it
    # missed rather than an empty panel until the next line arrives.
    for entry in session.tail(200):
        await connection_manager.send(websocket, {
            "type": "terminal_output",
            "session_id": session_id,
            "stream": entry.get("stream", "stdout"),
            "text": entry.get("text", ""),
            "replay": True,
        })

    logger.info("Socket subscribed to terminal %s", session_id)


async def _handle_reminder_cmd(
    websocket: WebSocket,
    connection_manager: ConnectionManager,
    reminder_service,
    data: dict,
    session_id: str,
) -> None:
    """The reminder panel's list / delete / done commands.

    This logic used to live inline in the receive loop. A refactor replaced
    the call site with this function *without ever writing the function*:
    every panel button raised NameError, fell through to the loop's
    catch-all and **dropped the socket** — pressing "refresh" disconnected
    the chat. Hence the defensive answer below: a failed reminder read sends
    a message the panel can render, never a teardown.

    ``session_id`` is kept for call-site compatibility as attribution
    context, but ``list`` deliberately does not filter by it: a reminder must
    outlive the socket that created it (the id changes on every reconnect).
    The REST query keeps an explicit ``session_id`` filter for callers that
    want one session's rows.
    """
    action = data.get("action", "")
    rid = data.get("id")
    lang = data.get("language", "en")
    try:
        if action == "list":
            reminders = await reminder_service.list()
            display = await reminder_service.format_list_for_display(reminders, lang)
            await connection_manager.send(websocket, {
                "type": "reminder_list",
                "reminders": reminders,
                "display": display,
                "language": lang,
            })
            return

        if action in ("delete", "done") and rid is not None:
            reminder_id = int(rid)
            if action == "delete":
                ok = await reminder_service.delete(reminder_id)
                frame_type = "reminder_deleted"
            else:
                ok = await reminder_service.mark_completed(reminder_id)
                frame_type = "reminder_done"
            if ok:
                msg = (
                    ("Deleted." if lang == "en" else "تم الحذف.")
                    if action == "delete"
                    else ("Marked as done." if lang == "en" else "تم.")
                )
            else:
                # Never claim an action that did not happen: the client
                # removes the row from its panel on success.
                msg = "No such reminder." if lang == "en" else "لا يوجد تذكير بهذا المعرف."
            await connection_manager.send(websocket, {
                "type": frame_type,
                "id": reminder_id,
                "success": ok,
                "message": msg,
            })
            return

        await connection_manager.send(websocket, {
            "type": "error",
            "content": (
                f"Unknown reminder command: {action!r}" if lang == "en"
                else f"أمر تذكير غير معروف: {action!r}"
            ),
        })
    except (TypeError, ValueError):
        await connection_manager.send(websocket, {
            "type": "error",
            "content": (
                "Reminder id must be a number." if lang == "en"
                else "معرف التذكير يجب أن يكون رقماً."
            ),
        })
    except WebSocketDisconnect:
        raise
    except Exception as e:  # noqa: BLE001 - answered, never fatal
        safe_error(e, operation="ws.reminder_cmd", component="api.websocket")
        await connection_manager.send(websocket, {
            "type": "error",
            "content": (
                "Reminder command failed." if lang == "en"
                else "تعذر تنفيذ أمر التذكير."
            ),
        })


def _language_for(text: str) -> str:
    """Best-effort language for a client-facing sentence; never raises."""
    try:
        return clean_and_detect(text or "")["language"]
    except Exception:  # pragma: no cover - defensive
        return "en"


async def _run_turn(
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
    trace_id: Optional[str] = None,
) -> Optional[str]:
    """Run one turn. Returns ``"error"`` when the turn failed *after* the
    client was told (the ``error`` + ``done`` frames are sent here), so the
    request boundary can log ``status=error`` without re-sending anything.
    Every other outcome returns ``None``.
    """
    state = session_states.get(session_id)
    if state:
        state.reset()

    # Bind the trace for everything this turn schedules: the orchestrator's
    # inner task copies this context (asyncio.create_task copies contextvars),
    # so every model attempt inside it logs `request=<trace>` in the
    # `LLM_ATTEMPT` line and can be tied back to the turn that caused it. No
    # reset on the way out — `_handle_message` runs in a task created per
    # message, so the binding lives exactly as long as the request.
    if trace_id:
        set_trace_id(trace_id)

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

    logger.warning(f"[HANDLE_MSG] msg='{cleaned_message[:80]}' lang={language} session={session_id} trace={trace_id}")

    await memory_agent.store_message("user", cleaned_message, language=language, session_id=session_id)
    short_term_memory.put("last_query", cleaned_message, {"label": "user_query", "language": language})

    asyncio.create_task(memory_agent.summarize_and_prune(session_id))

    if cancel_event and cancel_event.is_set():
        logger.warning(f"[HANDLE_MSG] CANCELLED before dispatch (msg='{cleaned_message[:40]}')")
        _update_trace(trace_id, status="cancelled")
        return

    await connection_manager.send(websocket, {"type": "status", "content": "thinking", "trace_id": trace_id})

    token_buffer: list[str] = []

    confirmation_manager = state.confirmation_manager if state else None

    async def flush_tokens():
        if not token_buffer:
            return
        chunk = "".join(token_buffer)
        token_buffer.clear()
        await connection_manager.send(websocket, {
            "type": "token", "content": chunk, "response_id": response_id, "trace_id": trace_id,
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
                await connection_manager.send(websocket, {"type": "status", "content": "speaking", "trace_id": trace_id})
                tts_task = asyncio.create_task(
                    _stream_tts_chunks(
                        websocket, complete[0], detect_language(complete[0]),
                        tts, connection_manager, cancel_event, spoken_tracker, config,
                        trace_id=trace_id,
                    )
                )
                if state:
                    state.tts_task = tts_task

    # The agent's progress channel. The engine emits semantic events; this
    # subscriber is the only place that knows the wire format, so the agent
    # stays transport-agnostic and can be driven by the desktop shell or a test
    # with equal ease.
    progress = _ProgressRelay(websocket, connection_manager, response_id, trace_id)
    bus = EventBus()
    bus.subscribe(progress)

    async def on_event(kind: str, payload: dict) -> None:
        # The orchestrator's vocabulary, translated frame by frame. Only
        # `intent` maps to a frame: `task_created` / `task_updated` are already
        # on the wire as raw core events (the `_relay_core` subscription
        # forwards `event.to_dict()`), so relaying them here too would render
        # every task twice — one owner per event class.
        if kind != "intent":
            return
        intent = payload.get("intent", "chat")
        logger.warning(f"[HANDLE_MSG] intent={intent} for msg='{cleaned_message[:60]}'")
        logger.info(f"Session {session_id}: intent={intent}, lang={language}")
        await connection_manager.send(websocket, {
            "type": "intent", "intent": intent, "language": language,
            "response_id": response_id, "trace_id": trace_id,
        })

    runner = _SESSION_ORCHESTRATORS.get(session_id)
    if runner is None:
        # Built from this call's own dependencies: nothing else in the process
        # is guaranteed to know what this handler was given.
        runner = Orchestrator(
            agent_service=agent_service,
            router_agent=router_agent,
            memory_agent=memory_agent,
        )
        _SESSION_ORCHESTRATORS[session_id] = runner

    try:
        # `run_turn_in_session` rather than `run_turn`: registering the handle
        # is what lets the `task` cancel frame reach work that is sitting inside
        # a long agent dispatch — cancelling the event alone only takes effect
        # at the orchestrator's own checkpoints, and an eight-iteration tool
        # loop is far past the next one.
        handle = await runner.run_turn_in_session(
            session_id,
            user_message,
            language_hint=language,
            stream_callback=stream_callback,
            event_callback=on_event,
            confirmation_manager=confirmation_manager,
            event_bus=bus,
            response_id=response_id,
            cancel_event=cancel_event,
        )
        result = await handle
    except asyncio.CancelledError:
        logger.info(f"Session {session_id}: cancelled")
        _update_trace(trace_id, status="cancelled")
        return

    if result is not None and result.error:
        # One owner for failure reporting: the orchestrator has already logged
        # it (`safe_error`, one structured line) and turned it into a fixed
        # bilingual sentence carrying no exception text, no path and no
        # implementation name. Checked before the token flush because the old
        # exception path dropped the buffered partials too, so the wire
        # sequence matches it exactly.
        await connection_manager.send(websocket, {
            "type": "error", "content": result.error, "trace_id": trace_id,
        })
        # A turn that dies must still terminate: without this the client waits
        # on a `done` that is never coming.
        await connection_manager.send(websocket, {
            "type": "status", "content": "done",
            "response_id": None, "trace_id": trace_id,
        })
        _update_trace(trace_id, status="error")
        # Reported to the boundary so `REQUEST_DONE` logs `status=error`: the
        # frames already went out, so raising would duplicate them — and a
        # failure the log calls "ok" is a duration you cannot compare against.
        return "error"

    await flush_tokens()

    if (cancel_event and cancel_event.is_set()) or (result is not None and result.cancelled):
        _update_trace(trace_id, status="cancelled")
        return

    if result is not None and result.reply:
        final_content = result.reply
    intent = result.intent if result is not None else "chat"

    if not final_content.strip():
        await connection_manager.send(websocket, {
            "type": "status", "content": "done", "response_id": response_id, "trace_id": trace_id,
        })
        _update_trace(trace_id, status="completed", final_length=0)
        return

    if state and state.is_duplicate(final_content):
        logger.warning(f"Session {session_id}: duplicate blocked")
        _update_trace(trace_id, status="duplicate_blocked")
        return

    response_language = detect_language(final_content) or language

    await connection_manager.send(websocket, {
        "type": "message", "content": final_content, "response_id": response_id, "trace_id": trace_id,
    })

    await memory_agent.store_message(
        "assistant", final_content, language=response_language, session_id=session_id,
    )

    if state and state.tts_task and not state.tts_task.done():
        try:
            await state.tts_task
        except Exception as e:  # noqa: BLE001 - speech is an accessory to the reply
            # A failed synthesis must not eat the answer that was already sent,
            # but it should say why the speaker stayed quiet.
            safe_error(e, operation="ws.tts_join", component="WebSocket")

    tts_task = None
    if not tts_triggered:
        await connection_manager.send(websocket, {"type": "status", "content": "speaking", "trace_id": trace_id})
        tts_task = asyncio.create_task(
            _stream_tts_chunks(
                websocket, final_content, response_language,
                tts, connection_manager, cancel_event, spoken_tracker, config,
                trace_id=trace_id,
            )
        )
        if state:
            state.tts_task = tts_task
    elif tts_triggered and spoken_tracker:
        remaining = spoken_tracker.get_new_sentences(final_content, tts.split_sentences)
        if remaining:
            await connection_manager.send(websocket, {"type": "status", "content": "speaking", "trace_id": trace_id})
            tts_task = asyncio.create_task(
                _stream_tts_chunks(
                    websocket, " ".join(remaining), response_language,
                    tts, connection_manager, cancel_event, spoken_tracker, config,
                    trace_id=trace_id,
                )
            )
            if state:
                state.tts_task = tts_task

    # The terminal status is emitted *after* the TTS stream drains, not when the
    # task is merely created. It was previously sent immediately, so the client
    # received `done` and `listening` before a single audio_chunk had arrived
    # and the UI reported "listening" for the whole time the assistant was
    # audibly speaking. The wait is bounded so a wedged TTS provider cannot
    # leave the turn without a terminal frame.
    if tts_task is not None and not cancel_event.is_set():
        try:
            await asyncio.wait_for(
                asyncio.shield(tts_task),
                timeout=config.TTS_TIMEOUT if hasattr(config, "TTS_TIMEOUT") else 120.0,
            )
        except asyncio.TimeoutError:
            logger.warning("Session %s: TTS drain timed out; sending terminal status", session_id)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            safe_error(exc, operation="tts.drain", component="voice.tts")

    if cancel_event and cancel_event.is_set():
        _update_trace(trace_id, status="cancelled")
        return

    await connection_manager.send(websocket, {
        "type": "status", "content": "done", "response_id": response_id, "trace_id": trace_id,
    })

    if state:
        state.last_active_ts = time.time()

    await connection_manager.send(websocket, {"type": "status", "content": "listening", "trace_id": trace_id})

    _update_trace(trace_id, status="completed", final_length=len(final_content))

    logger.info(
        f"Session {session_id}: done intent={intent}, "
        f"tts_early={tts_triggered}, final_len={len(final_content)} trace={trace_id}"
    )
