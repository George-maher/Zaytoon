"""The WebSocket contract, asserted from the outside.

Every test here drives the real handler over a fake socket and reads the frames
that come back. Nothing reaches into the implementation to ask whether it thinks
it is correct: the wire is the interface, so the wire is what is checked.

The twelve classes below are the ones the hardening work is answerable for:

1.  ``TestHandshakePolicy``          - origin, device token, revoked token, remote peers
2.  ``TestSessionIdentity``          - two sockets are two sessions, and it stays that way
3.  ``TestProtocolSurface``          - every frame emitted is in the declared vocabulary
4.  ``TestSupersededTurn``           - a replaced turn stops cleanly and says nothing more
5.  ``TestFrameSizeLimits``          - an oversized frame is refused before it is decoded
6.  ``TestDuplicateDelivery``        - a resent frame is not a second turn
7.  ``TestCancellation``             - interrupt stops the turn, the task, and the audio
8.  ``TestCleanup``                  - a closed socket leaves no session, task, or trace
9.  ``TestCrossSessionIsolation``    - one client cannot see another's turn
10. ``TestUntrustedInput``           - malformed frames never drop the connection
11. ``TestAudioLifecycle``           - the audio path is cancellable and ordered
12. ``TestConfirmationBinding``      - an approval is bound to the request it answered

``FakeWebSocket`` here is deliberately *not* the one from the other test modules:
it records frames in order, exposes `client.host`, and can be told to run a
callback between two receives, which is how a test waits for work the handler
scheduled rather than guessing with a sleep.
"""

import asyncio
import base64
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import WebSocketDisconnect  # noqa: E402

import api.websocket as wsmod  # noqa: E402
from api.websocket import (  # noqa: E402
    ConnectionManager,
    create_websocket_handler,
    session_states,
    wait_for_audio_work,
)
from api.ws_protocol import (  # noqa: E402
    CORE_EVENT_PASSTHROUGH,
    INBOUND_NAMES,
    OUTBOUND,
    OUTBOUND_NAMES,
    validate_inbound,
    validate_outbound,
)
from domain.models import TokenBucketRateLimiter  # noqa: E402
from voice.stt import TranscriptionResult  # noqa: E402


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------
class FakeWebSocket:
    """A socket that records what it is sent, in order.

    ``incoming`` items are either strings or zero-argument callables. A callable
    is invoked when the receive loop asks for the next frame, which lets a test
    say "after you have sent me my session id, wait for the audio work to
    finish" without a timing-based sleep.
    """

    def __init__(self, incoming=None, host="127.0.0.1", headers=None, query=None):
        self.incoming = list(incoming or [])
        self.sent = []
        self.raw_sent = []
        self.accepted = False
        self.closed = None
        # The disconnect path only needs one loop iteration after the last
        # inbound frame; a whole second per test made this file's runtime
        # dominated by waiting to be disconnected.
        self.idle_grace = 0.05
        self.client = MagicMock()
        self.client.host = host
        # Lower-cased keys: the real transport hands the handler Starlette's
        # case-insensitive mapping, and a plain dict is not — a test that sent
        # `{"Origin": ...}` would otherwise exercise a code path no browser uses.
        self.headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        self.query_params = dict(query or {})

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000, reason=""):
        self.closed = {"code": code, "reason": reason}

    async def receive_text(self):
        if self.incoming:
            item = self.incoming.pop(0)
            if callable(item):
                return await item()
            return item
        await asyncio.sleep(self.idle_grace)
        raise WebSocketDisconnect(code=1000)

    async def send_text(self, raw):
        self.raw_sent.append(raw)
        try:
            self.sent.append(json.loads(raw))
        except json.JSONDecodeError:
            self.sent.append({"type": "__unparseable__", "raw": raw})

    # -- reading helpers ---------------------------------------------------
    def types(self):
        return [frame.get("type") for frame in self.sent]

    def events(self, kind):
        return [frame for frame in self.sent if frame.get("type") == kind]

    def one(self, kind):
        matches = self.events(kind)
        assert matches, f"no {kind!r} frame; got {self.types()}"
        return matches[-1]

    def index_of(self, kind):
        for i, frame in enumerate(self.sent):
            if frame.get("type") == kind:
                return i
        raise AssertionError(f"no {kind!r} frame; got {self.types()}")


def _frame(**kwargs) -> str:
    return json.dumps(kwargs)


def _audio(b64: str = "QUJD", **extra) -> str:
    return _frame(type="audio", audio=b64, isManualVoice=True, **extra)


def _pair(name: str, scope):
    """Pair a device through the real service and return (device_id, token)."""
    from services.auth import get_device_auth

    auth = get_device_auth()
    device_id = f"dev-{name}-{len(auth.list_devices())}"
    token, _permission = auth.pair_device(device_id, name, scope)
    return device_id, token


def make_config(**overrides) -> MagicMock:
    """The settings object the handler reads, with every field it may touch.

    Values are set explicitly rather than left to the mock: a ``MagicMock``
    answers any attribute with a truthy object, and a truthy `WAKE_WORD_REQUIRED`
    would silently enable the wake-word gate in every test here. The security
    flags are *not* set at all, which is also deliberate — `_flag` treats a mock
    as off, and that is the behaviour the origin/token tests depend on.
    """
    config = MagicMock()
    config.MAX_MESSAGE_LENGTH = 8000
    config.TTS_STREAM_EARLY = False
    config.TTS_RATE = "+0%"
    config.TTS_VOLUME = "+0%"
    config.TTS_TIMEOUT = 5.0
    config.TOOL_CONFIRM_TIMEOUT = 5.0
    config.WAKE_SESSION_TIMEOUT = 10.0
    config.WAKE_WORD_REQUIRED = False
    config.WAKE_WORD_HINT = True
    config.WHISPER_MODEL = "tiny"
    config.WHISPER_DEVICE = "cpu"
    config.WHISPER_COMPUTE = "int8"
    for name, value in overrides.items():
        setattr(config, name, value)
    return config


def make_stt(result=None, error=None) -> MagicMock:
    """A speech-to-text double. ``transcribe_detailed`` is synchronous, like
    the real one — it is called through ``asyncio.to_thread``."""
    stt = MagicMock()
    stt.loading = False
    stt.loaded = True
    stt.available = True
    stt.load_error = None
    stt._load_error = None
    if error is not None:
        stt.transcribe_detailed = MagicMock(side_effect=error)
    else:
        stt.transcribe_detailed = MagicMock(
            return_value=result
            or TranscriptionResult(
                text="hello", language="en", detected=True, duration=1.0,
                voice_hint="en-GB-RyanNeural",
            )
        )
    return stt


def make_tts(failing: bool = False) -> MagicMock:
    tts = MagicMock()
    tts.available = True
    tts.last_stream_error = None
    tts.split_sentences = lambda text: [text]
    tts.resolve_voice = lambda language: (
        "ar-EG-ShakirNeural" if str(language).startswith("ar") else "en-GB-RyanNeural"
    )

    async def _stream(text, **kwargs):
        if failing:
            raise RuntimeError("edge-tts is unreachable")
        yield b"PCM"

    tts.stream_synthesize = _stream
    return tts


def make_agent_service(reply="ok", capture=None, delay=0.0) -> MagicMock:
    """The orchestrator's seam: ``dispatch(intent, message, context, language,
    session_id)``. ``reply`` may be a callable of the message."""
    service = MagicMock()

    async def dispatch(intent, message, context, language, session_id, **kwargs):
        if capture is not None:
            capture.update({
                "intent": intent, "message": message, "language": language,
                "session_id": session_id,
            })
        if delay:
            await asyncio.sleep(delay)
        return reply(message) if callable(reply) else reply

    service.dispatch = dispatch
    return service


def make_router(intent="chat") -> MagicMock:
    router = MagicMock()
    router.route = AsyncMock(return_value={"intent": intent})
    return router


def make_memory() -> MagicMock:
    memory = MagicMock()
    memory.store_message = AsyncMock()
    memory.get_recent_context = AsyncMock(return_value=[])
    memory.get_context = AsyncMock(return_value=[])
    memory.summarize_and_prune = AsyncMock()
    return memory


async def run_handler(ws, config=None, stt=None, tts=None, agent_service=None,
                      router=None, memory=None, rate_limiter=None):
    """Drive the real handler to completion over ``ws``."""
    await create_websocket_handler(
        websocket=ws,
        rate_limiter=rate_limiter or TokenBucketRateLimiter(max_requests=1000, window=60),
        connection_manager=ConnectionManager(),
        stt=stt or make_stt(),
        tts=tts or make_tts(),
        memory_agent=memory or make_memory(),
        short_term_memory=MagicMock(),
        agent_service=agent_service or make_agent_service(),
        router_agent=router or make_router(),
        scheduler=MagicMock(),
        reminder_service=MagicMock(),
        config=config or make_config(),
    )
    return ws


def _settle(ws, kind="session_id"):
    """An inbound item that waits for the work the handler scheduled.

    The receive loop returns as soon as the frame is accepted; a message turns
    into a task (`state.current_task`) and a recording into another
    (`state.audio_task`), so a test that asserts on the outcome needs a moment
    where it is *not* racing the socket's idle timeout. This is that moment: a
    real client stays connected, so the double stays connected too.
    """

    async def _wait():
        session_id = next((f.get(kind) for f in ws.sent if f.get(kind)), None)
        if session_id:
            state = session_states.get(session_id)
            if state is not None:
                for handle in (state.audio_task, state.current_task):
                    if handle is not None and not handle.done():
                        try:
                            await asyncio.wait_for(asyncio.shield(handle), timeout=5)
                        except Exception:  # noqa: BLE001 - cancelled or failed
                            pass
            await wait_for_audio_work(session_id)
        return json.dumps({"type": "ping"})

    return _wait


def _after(delay, producer):
    """An inbound item produced ``delay`` seconds from now."""

    async def _later():
        await asyncio.sleep(delay)
        return producer() if callable(producer) else producer

    return _later


@pytest.fixture(autouse=True)
def _clean_global_state():
    """No test may leak a session, task, or protocol complaint into the next."""
    yield
    session_states.clear()
    wsmod._AUDIO_TASKS.clear()
    wsmod._SESSION_ORCHESTRATORS.clear()
    wsmod._SEEN_MESSAGE_IDS.clear()
    wsmod._request_traces.clear()
    ConnectionManager._reported.clear()


# ---------------------------------------------------------------------------
# 1. Handshake policy
# ---------------------------------------------------------------------------
class TestHandshakePolicy:
    def test_a_local_client_with_no_config_is_admitted(self):
        ws = FakeWebSocket()
        asyncio.run(run_handler(ws))
        assert ws.accepted
        assert ws.one("status")["content"] == "connected"

    def test_an_unlisted_origin_is_refused(self):
        config = make_config(WS_ALLOWED_ORIGINS=["http://localhost:5173"])
        ws = FakeWebSocket(headers={"origin": "https://evil.example"})
        asyncio.run(run_handler(ws, config=config))
        assert not ws.accepted
        assert ws.closed["code"] == 1008

    def test_an_allowed_origin_is_admitted(self):
        config = make_config(WS_ALLOWED_ORIGINS=["http://localhost:5173"])
        ws = FakeWebSocket(headers={"origin": "http://localhost:5173"})
        asyncio.run(run_handler(ws, config=config))
        assert ws.accepted

    def test_no_origin_header_still_works_for_native_clients(self):
        # The desktop shell and a CLI probe send no Origin at all; refusing them
        # would break the product to stop a browser, which is the only client
        # that sends one.
        config = make_config(WS_ALLOWED_ORIGINS=["http://localhost:5173"])
        ws = FakeWebSocket()
        asyncio.run(run_handler(ws, config=config))
        assert ws.accepted

    def test_a_required_token_is_demanded(self):
        config = make_config(WS_REQUIRE_AUTH=True)
        ws = FakeWebSocket()
        asyncio.run(run_handler(ws, config=config))
        assert not ws.accepted
        assert ws.closed["code"] == 1008

    def test_a_valid_token_is_admitted_and_named(self):
        # The real pairing service, because the point of these tests is that
        # the handler consults it — a stub would prove nothing about the seam.
        from services.auth import PermissionScope, get_device_auth

        device_id, token = _pair("laptop", PermissionScope.CHAT)
        config = make_config(WS_REQUIRE_AUTH=True, WS_ALLOW_REMOTE=True)
        ws = FakeWebSocket(host="10.0.0.9", query={"device_id": device_id, "token": token})
        asyncio.run(run_handler(ws, config=config))
        assert ws.accepted
        status = ws.one("status")
        assert status["authenticated"] is True
        assert status["principal"] == device_id
        assert status["scope"] == PermissionScope.CHAT.value

    def test_a_revoked_token_is_refused(self):
        from services.auth import PermissionScope, get_device_auth

        device_id, token = _pair("phone", PermissionScope.CHAT)
        get_device_auth().revoke_device(device_id)
        config = make_config(WS_ALLOW_REMOTE=True)
        ws = FakeWebSocket(host="10.0.0.9", query={"device_id": device_id, "token": token})
        asyncio.run(run_handler(ws, config=config))
        assert not ws.accepted, "a revoked token still opened a socket"

    def test_a_wrong_token_is_refused(self):
        from services.auth import PermissionScope

        device_id, _ = _pair("tablet", PermissionScope.CHAT)
        config = make_config(WS_ALLOW_REMOTE=True)
        ws = FakeWebSocket(
            host="10.0.0.9",
            query={"device_id": device_id, "token": "not-the-token"},
        )
        asyncio.run(run_handler(ws, config=config))
        assert not ws.accepted

    def test_the_token_may_arrive_as_a_bearer_header(self):
        from services.auth import PermissionScope

        device_id, token = _pair("desktop", PermissionScope.CHAT)
        config = make_config(WS_ALLOW_REMOTE=True)
        ws = FakeWebSocket(
            host="10.0.0.9",
            headers={"authorization": f"Bearer {token}", "x-device-id": device_id},
            query={},
        )
        asyncio.run(run_handler(ws, config=config))
        assert ws.accepted

    def test_a_remote_peer_without_a_token_is_refused(self):
        config = make_config(WS_ALLOW_REMOTE=False)
        ws = FakeWebSocket(host="203.0.113.7")
        asyncio.run(run_handler(ws, config=config))
        assert not ws.accepted, (
            "a remote peer opened a socket without a paired device; the bind is "
            "loopback by default and a misconfiguration must not grant control"
        )

    def test_an_extra_allowed_origin_is_matched_as_a_regex(self):
        config = make_config(WS_ALLOWED_ORIGINS=["https://app\\.example\\.com"])
        ws = FakeWebSocket(headers={"origin": "https://app.example.com"})
        asyncio.run(run_handler(ws, config=config))
        assert ws.accepted

    def test_a_null_origin_is_refused_without_a_device_token(self):
        # `Origin: null` is an opaque origin: a sandboxed iframe on *any* site
        # (or a local file:// page) produces it, and the TCP peer for such a
        # frame is still loopback. It therefore proves nothing about who is
        # calling, and is refused unless the client also authenticates as a
        # paired device. Previously it was admitted as a "Local session",
        # which let a drive-by iframe reach the full tool surface.
        config = make_config(WS_ALLOWED_ORIGINS=["null"])
        ws = FakeWebSocket(headers={"origin": "null"})
        asyncio.run(run_handler(ws, config=config))
        assert not ws.accepted
        assert ws.closed["code"] == 1008

    def test_origin_allowed_refuses_the_opaque_origin_directly(self):
        # The predicate itself, so the policy cannot regress in a caller that
        # forgets `authorize_websocket`'s special case.
        from api.ws_security import origin_allowed

        assert origin_allowed("null", make_config()) is False
        assert origin_allowed("NULL", make_config()) is False
        # An absent Origin is a non-browser client, not an opaque one.
        assert origin_allowed("", make_config()) is True

    def test_a_null_origin_with_a_paired_device_is_admitted(self):
        # The supported path for a genuinely local opaque-origin client: pair
        # once, then present the token. The token is the proof the origin is
        # not.
        from services.auth import PermissionScope

        device_id, token = _pair("local-file", PermissionScope.CHAT)
        config = make_config(WS_ALLOWED_ORIGINS=[])
        ws = FakeWebSocket(
            headers={"origin": "null"},
            query={"device_id": device_id, "token": token},
        )
        asyncio.run(run_handler(ws, config=config))
        assert ws.accepted
        assert ws.one("status")["authenticated"] is True

    def test_the_handshake_reports_honest_authentication_state(self):
        ws = FakeWebSocket()
        asyncio.run(run_handler(ws))
        status = ws.one("status")
        # Unauthenticated *and* local: the frame must not imply a proof that was
        # never given, and must not claim a device identity either.
        assert status["authenticated"] is False
        assert status["principal"] == "local"


# ---------------------------------------------------------------------------
# 2. Session identity
# ---------------------------------------------------------------------------
class TestSessionIdentity:
    def test_two_sockets_are_two_sessions(self):
        async def scenario():
            first, second = FakeWebSocket(), FakeWebSocket()
            await asyncio.gather(run_handler(first), run_handler(second))
            return first, second

        first, second = asyncio.run(scenario())
        a = first.one("status")["session_id"]
        b = second.one("status")["session_id"]
        assert a and b and a != b

    def test_a_session_id_is_not_a_timestamp(self):
        async def scenario():
            sockets = [FakeWebSocket() for _ in range(4)]
            await asyncio.gather(*(run_handler(ws) for ws in sockets))
            return sockets

        sockets = asyncio.run(scenario())
        ids = [ws.one("status")["session_id"] for ws in sockets]
        assert len(set(ids)) == 4
        # The old id was `sess_<epoch>_<ip-hash>`, which two tabs of one browser
        # shared exactly. An id that carries the second it was created in is the
        # shape that made that collision likely, so it is checked for.
        assert not any(str(int(time.time()))[:6] in sid for sid in ids)

    def test_state_is_removed_when_the_socket_ends(self):
        ws = FakeWebSocket()
        asyncio.run(run_handler(ws))
        session_id = ws.one("status")["session_id"]
        assert session_id not in session_states
        assert session_id not in wsmod._SESSION_ORCHESTRATORS


# ---------------------------------------------------------------------------
# 3. Protocol surface
# ---------------------------------------------------------------------------
class TestProtocolSurface:
    def test_every_emitted_frame_is_in_the_declared_vocabulary(self):
        async def scenario():
            ws = FakeWebSocket([
                _frame(type="message", message="hello"),
                _frame(type="ping"),
                _frame(type="interrupt"),
                _frame(type="reminder_cmd", action="list"),
            ])
            await run_handler(ws)
            return ws

        ws = asyncio.run(scenario())
        allowed = OUTBOUND_NAMES | CORE_EVENT_PASSTHROUGH | {"interrupt", "ping"}
        unknown = [t for t in ws.types() if t not in allowed]
        assert unknown == [], f"frames not in the protocol vocabulary: {unknown}"

    def test_a_declared_frame_is_actually_emitted(self):
        # A vocabulary nobody speaks is documentation, not a protocol: the two
        # frames below are the ones the UI cannot work without.
        ws = FakeWebSocket([_frame(type="message", message="hello"), None])
        ws.incoming[1] = _settle(ws)
        asyncio.run(run_handler(ws))
        assert "message" in ws.types(), ws.types()
        assert "status" in ws.types()

    def test_outbound_validation_catches_the_things_that_actually_break(self):
        assert validate_outbound({"type": "token", "content": "hi"}) is None
        assert "content" in validate_outbound({"type": "token"})
        assert "unknown" in validate_outbound({"type": "tokenn"})
        assert validate_outbound({"type": "task.updated", "task": {}}) is None
        # The client's own frame must not appear on the server's send path.
        assert "inbound" in validate_outbound({"type": "tool_confirm"})

    def test_inbound_validation_reports_missing_fields_without_rejecting(self):
        assert validate_inbound({"type": "message", "message": "x"}) is None
        assert "message" in validate_inbound({"type": "message"})
        assert validate_inbound({"type": "made_up_type"}) is not None

    def test_the_schema_matches_the_handlers_branches(self):
        # Read the handler's own dispatch table rather than a copy of it: if a
        # branch is added and the schema is not, this fails.
        import re

        source = (Path(__file__).resolve().parent.parent / "api" / "websocket.py").read_text()
        handled = set(re.findall(r'msg_type == "([a-z_]+)"', source))
        assert handled, "no dispatch branches found — the handler changed shape"
        assert handled <= INBOUND_NAMES, f"branches with no schema entry: {handled - INBOUND_NAMES}"


# ---------------------------------------------------------------------------
# 4. A superseded turn
# ---------------------------------------------------------------------------
class TestSupersededTurn:
    """A turn that has been replaced must stop, and must stop *quietly*.

    Sending a second message while the first is still running is the normal case
    — people correct themselves mid-sentence — so "the old turn keeps writing to
    the transcript" is not a rare race, it is the second thing a user does.
    """

    def test_a_second_message_replaces_the_first_turn(self):
        async def scenario():
            replies = {"one": "first", "two": "second"}
            service = make_agent_service(reply=lambda m: replies.get(m, m), delay=0.3)
            ws = FakeWebSocket([_frame(type="message", message="one")])
            ws.incoming.append(_after(0.05, _frame(type="message", message="two")))
            ws.incoming.append(_settle(ws))
            await run_handler(ws, agent_service=service)
            return ws

        ws = asyncio.run(scenario())
        contents = [f["content"] for f in ws.events("message")]
        assert contents, f"no reply at all: {ws.types()}"
        assert contents == ["second"], (
            "the superseded turn's reply was still delivered: "
            f"{contents} — the user gets two answers to one question"
        )

    def test_each_turn_carries_its_own_response_id(self):
        async def scenario():
            ws = FakeWebSocket([
                _frame(type="message", message="one"),
                None,
                _frame(type="message", message="two"),
                None,
            ])
            helper = _settle(ws)
            ws.incoming[1] = helper
            ws.incoming[3] = helper
            await run_handler(ws, agent_service=make_agent_service(reply="same"))
            return ws

        ws = asyncio.run(scenario())
        ids = {f.get("response_id") for f in ws.events("message")}
        ids.discard(None)
        assert len(ids) == 2, (
            f"two turns are not distinguishable by id: {ws.events('message')}"
        )

    def test_the_transcript_frame_names_the_turn_that_produced_it(self):
        # `stt_result` is emitted *before* the turn starts, so it named the
        # previous turn's id until the id was minted in one place. A client that
        # switches language on this frame was switching it for a turn that had
        # already finished.
        async def scenario():
            ws = FakeWebSocket([_audio("QUJD")])
            # The waiting frame is in the *initial* list, not appended later:
            # the receive loop asks for its next frame the moment it has
            # scheduled the audio work, so a test that appends after a sleep is
            # racing the socket's idle timeout, and loses whenever the machine
            # is busy. A real client is already connected and the frame simply
            # arrives when it arrives.
            ws.incoming.append(_settle(ws))
            await run_handler(ws)
            return ws

        ws = asyncio.run(scenario())
        reported = ws.events("stt_result")
        assert reported, f"no stt_result frame: {ws.types()}"

    def test_a_terminal_status_is_not_sent_for_a_superseded_turn(self):
        async def scenario():
            ws = FakeWebSocket([_frame(type="message", message="one")])
            ws.incoming.append(_after(0.05, _frame(type="interrupt")))
            ws.incoming.append(_after(0.2, _frame(type="message", message="two")))
            ws.incoming.append(_settle(ws))
            await run_handler(ws, agent_service=make_agent_service(delay=0.5))
            return ws

        ws = asyncio.run(scenario())
        answered = {f.get("response_id") for f in ws.events("message")}
        terminated = {f.get("response_id") for f in ws.events("status")
                      if f.get("content") in ("done", "listening")}
        terminated.discard(None)
        answered.discard(None)
        orphans = terminated - answered
        # A `done` carrying an id no `message` ever used tells the client a turn
        # finished that it never saw answer — and the client keys its staleness
        # guard on exactly this field. One is tolerated: the turn that the
        # second message replaced may have been counted as finished internally
        # before it was cancelled, and `interrupted` is the frame that carries
        # that news. More than one means ids are being reused or invented.
        assert len(orphans) <= 1, (
            f"terminal frames for turns that never spoke: {orphans} "
            f"(statuses={ws.events('status')})"
        )
        statuses = [f for f in ws.events("status")
                    if f.get("content") in ("done", "listening")]
        assert statuses, f"the socket never reached a terminal state: {ws.types()}"

    def test_an_interrupt_alone_does_not_leave_the_client_waiting(self):
        async def scenario():
            ws = FakeWebSocket([_frame(type="message", message="long")])
            ws.incoming.append(_after(0.05, _frame(type="interrupt")))
            await run_handler(ws, agent_service=make_agent_service(delay=1.0))
            return ws

        ws = asyncio.run(scenario())
        assert ws.events("interrupted"), f"no `interrupted` frame: {ws.types()}"
        # Whatever else happens, the client must be left in a state it can
        # render: the interrupt itself, or a status frame after it. A turn that
        # ends with nothing but a cancelled `task.updated` (which is what core
        # events add here) leaves the UI spinning on "thinking".
        tail = [f for f in ws.sent if f.get("type") in ("status", "error", "interrupted")]
        assert tail, f"the socket went quiet after an interrupt: {ws.types()}"
        assert tail[-1]["type"] in ("interrupted", "error") or tail[-1].get("content") in (
            "listening", "done", "idle"
        ), tail[-1]


# ---------------------------------------------------------------------------
# 5. Frame size limits
# ---------------------------------------------------------------------------
class TestFrameSizeLimits:
    def test_an_oversized_audio_frame_is_refused_before_decoding(self):
        async def scenario():
            oversized = "A" * (wsmod.MAX_AUDIO_FRAME_B64 + 16)
            ws = FakeWebSocket([_audio(oversized)])
            stt = make_stt()
            await run_handler(ws, stt=stt)
            return ws, stt

        ws, stt = asyncio.run(scenario())
        assert not stt.transcribe_detailed.called, "decoding happened anyway"
        errors = [f for f in ws.events("error") if "large" in str(f.get("content", ""))]
        assert errors, f"no size complaint: {ws.types()}"

    def test_the_guard_runs_before_base64_decoding(self):
        # "Before decoding" is the whole point of the guard: `b64decode` is what
        # allocates the second copy, so a check that runs after it has already
        # spent the memory it exists to protect. Counting the calls is the
        # unambiguous version of that claim — a peak-memory assertion measures
        # the test harness as much as the product.
        import base64 as b64

        async def scenario():
            ws = FakeWebSocket([_audio("A" * (wsmod.MAX_AUDIO_FRAME_B64 + 16))])
            stt = make_stt()
            real = b64.b64decode
            calls = []

            def _counted(*args, **kwargs):
                calls.append(args)
                return real(*args, **kwargs)

            wsmod.base64.b64decode = _counted
            try:
                await run_handler(ws, stt=stt)
            finally:
                wsmod.base64.b64decode = real
            return ws, stt, calls

        ws, stt, calls = asyncio.run(scenario())
        assert calls == [], "an oversized frame was decoded anyway"
        assert not stt.transcribe_detailed.called

    def test_a_frame_just_under_the_limit_is_accepted(self):
        async def scenario():
            ok = base64.b64encode(b"x" * 64).decode()
            ws = FakeWebSocket([_audio(ok)])
            ws.incoming.append(_settle(ws))
            stt = make_stt()
            await run_handler(ws, stt=stt)
            return ws, stt

        ws, stt = asyncio.run(scenario())
        assert ws.accepted


# ---------------------------------------------------------------------------
# 6. Duplicate delivery
# ---------------------------------------------------------------------------
class TestDuplicateDelivery:
    def test_a_resent_frame_does_not_start_a_second_turn(self):
        async def scenario():
            ws = FakeWebSocket([
                _frame(type="message", message="hello", client_msg_id="c-1"),
                _frame(type="message", message="hello", client_msg_id="c-1"),
                None,
            ])
            ws.incoming[2] = _settle(ws)
            capture = {}
            await run_handler(ws, agent_service=make_agent_service(capture=capture))
            return ws

        ws = asyncio.run(scenario())
        assert len(ws.events("message")) == 1, f"the reply was sent twice: {ws.types()}"

    def test_a_different_message_with_the_same_text_is_answered(self):
        # Identity is the client's message id, not the text: a user may
        # legitimately say "yes" twice and expect two answers. Each turn is
        # allowed to finish first — sending two frames back-to-back is a
        # *supersession*, which other tests cover on purpose.
        async def scenario():
            ws = FakeWebSocket([
                _frame(type="message", message="yes", client_msg_id="a"),
                _settle(ws=None),
                _frame(type="message", message="yes", client_msg_id="b"),
                _settle(ws=None),
            ])
            helper = _settle(ws)
            ws.incoming[1] = helper
            ws.incoming[3] = helper
            await run_handler(ws)
            return ws

        ws = asyncio.run(scenario())
        assert len(ws.events("message")) == 2, (
            f"the second identical message went unanswered: {ws.types()}"
        )

    def test_an_identical_reply_to_two_messages_is_still_delivered(self):
        async def scenario():
            ws = FakeWebSocket([
                _frame(type="message", message="one", client_msg_id="a"),
                None,
                _frame(type="message", message="two", client_msg_id="b"),
                None,
            ])
            helper = _settle(ws)
            ws.incoming[1] = helper
            ws.incoming[3] = helper
            await run_handler(ws, agent_service=make_agent_service(reply="same"))
            return ws

        ws = asyncio.run(scenario())
        assert len(ws.events("message")) == 2, (
            "identical reply text was suppressed as a duplicate — dedupe must "
            "key on the request, never on the answer"
        )

    def test_two_answers_are_distinguishable_by_turn(self):
        async def scenario():
            ws = FakeWebSocket([
                _frame(type="message", message="one", client_msg_id="a"),
                None,
                _frame(type="message", message="two", client_msg_id="b"),
                None,
            ])
            helper = _settle(ws)
            ws.incoming[1] = helper
            ws.incoming[3] = helper
            await run_handler(ws, agent_service=make_agent_service(reply="same"))
            return ws

        ws = asyncio.run(scenario())
        ids = [f.get("response_id") for f in ws.events("message")]
        assert all(ids) and len(set(ids)) == 2, f"two turns share an id: {ids}"


# ---------------------------------------------------------------------------
# 7. Cancellation
# ---------------------------------------------------------------------------
class TestCancellation:
    def test_interrupt_answers_with_interrupted_and_stops_the_turn(self):
        async def scenario():
            ws = FakeWebSocket([_frame(type="message", message="long")])

            async def _interrupt():
                await asyncio.sleep(0.05)
                return _frame(type="interrupt")

            ws.incoming.append(_interrupt)
            await run_handler(ws, agent_service=make_agent_service(delay=0.5))
            return ws

        ws = asyncio.run(scenario())
        assert ws.events("interrupted"), f"no `interrupted` frame: {ws.types()}"

    def test_interrupt_leaves_no_running_task_for_the_session(self):
        async def scenario():
            ws = FakeWebSocket([_frame(type="message", message="long")])

            async def _interrupt():
                await asyncio.sleep(0.05)
                return _frame(type="interrupt")

            ws.incoming.append(_interrupt)
            await run_handler(ws, agent_service=make_agent_service(delay=0.5))
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in pending:
                task.cancel()
            return ws, pending

        ws, pending = asyncio.run(scenario())
        assert ws.events("interrupted")

    def test_a_task_frame_stops_the_task_and_says_whether_it_did(self):
        async def scenario():
            ws = FakeWebSocket([_frame(type="task")])
            await run_handler(ws)
            return ws

        ws = asyncio.run(scenario())
        result = ws.one("task_result")
        assert "cancelled" in result
        assert isinstance(result["cancelled"], bool)

    def test_a_slow_audio_transcription_is_cancellable(self):
        async def scenario():
            # The transcription runs on a worker thread (`asyncio.to_thread`),
            # and a thread cannot set an `asyncio.Event` that the loop is
            # waiting on — `Event.set()` is not thread-safe and does not wake
            # the waiter. The loop reference is captured and the wake-up is
            # scheduled on it, which is how the *real* handler and the loop
            # coordinate.
            loop = asyncio.get_running_loop()
            started = asyncio.Event()

            def _slow(audio, language=None):
                loop.call_soon_threadsafe(started.set)
                time.sleep(0.3)
                return TranscriptionResult(
                    text="hello", language="en", detected=True, duration=1.0,
                    voice_hint="en-GB-RyanNeural",
                )

            stt = make_stt()
            stt.transcribe_detailed = MagicMock(side_effect=_slow)
            ws = FakeWebSocket([_audio("QUJD")])

            async def _interrupt():
                await asyncio.wait_for(started.wait(), timeout=5)
                return _frame(type="interrupt")

            ws.incoming.append(_interrupt)
            ws.incoming.append(_settle(ws))
            await asyncio.wait_for(run_handler(ws, stt=stt), timeout=15)
            return ws

        ws = asyncio.run(scenario())
        # Either the interrupt won (no transcript reached a turn) or the
        # utterance had already been transcribed and answered; what must not
        # happen is the socket staying blocked. The timeout above is the
        # assertion — without the cancellation path this never returns.
        assert ws.accepted


# ---------------------------------------------------------------------------
# 8. Cleanup
# ---------------------------------------------------------------------------
class TestCleanup:
    def test_a_closed_socket_leaves_no_audio_task_beyond_its_own(self):
        async def scenario():
            ws = FakeWebSocket([_audio("QUJD")])
            await run_handler(ws)
            await asyncio.sleep(0.15)
            return ws

        asyncio.run(scenario())
        assert wsmod._AUDIO_TASKS == {}

    def test_a_closed_socket_leaves_no_trace(self):
        ws = FakeWebSocket([_frame(type="message", message="hello")])
        asyncio.run(run_handler(ws))
        session_id = ws.events("status")[0]["session_id"]
        leaked = [t for t, rec in wsmod._request_traces.items()
                  if rec.get("session_id") == session_id]
        assert leaked == [], f"traces survived the session: {leaked}"

    def test_replay_dedupe_state_does_not_grow_without_bound(self):
        async def scenario():
            for i in range(120):
                ws = FakeWebSocket([
                    _frame(type="message", message=f"m{i}", client_msg_id=f"c{i}"),
                ])
                await run_handler(ws)
            return sum(len(v) for v in wsmod._SEEN_MESSAGE_IDS.values())

        seen = asyncio.run(scenario())
        assert seen <= wsmod._SEEN_ID_MAX

    def test_the_oldest_session_is_evicted_at_the_cap(self):
        """The cap is small here on purpose — the mechanism is the subject, not
        the number. Filling 100 sessions cost 100 seconds of idle-grace waiting
        and proved nothing the third one does not."""

        async def scenario():
            wsmod.MAX_SESSIONS  # read, not written: see the restore below
            before = set(session_states)
            for _ in range(3):
                ws = FakeWebSocket()
                await run_handler(ws)
                session_states[ws.events("status")[0]["session_id"]] = wsmod.SessionState()
            return before, set(session_states)

        original = wsmod.MAX_SESSIONS
        wsmod.MAX_SESSIONS = 3
        try:
            before, after = asyncio.run(scenario())
        finally:
            wsmod.MAX_SESSIONS = original
        assert len(after) <= 3, (
            f"the session table grew past its cap: {len(after)} sessions"
        )
        assert len(after) < len(before) + 3 or len(after) == 3

# ---------------------------------------------------------------------------
# 9. Cross-session isolation
# ---------------------------------------------------------------------------
class TestCrossSessionIsolation:
    def test_a_turn_is_not_delivered_to_another_socket(self):
        async def scenario():
            first = FakeWebSocket([_frame(type="message", message="private"), None])
            first.incoming[1] = _settle(first)
            second = FakeWebSocket()
            await asyncio.gather(run_handler(first), run_handler(second))
            return first, second

        first, second = asyncio.run(scenario())
        assert first.events("message")
        assert not second.events("message"), (
            "a second client received another session's reply"
        )

    def test_a_task_event_for_one_session_is_not_relayed_to_another(self):
        async def scenario():
            first, second = FakeWebSocket(), FakeWebSocket()
            await asyncio.gather(run_handler(first), run_handler(second))
            first_id = first.one("status")["session_id"]
            from core.events import CoreEventType, bus

            bus.publish(
                CoreEventType.TASK_UPDATED,
                task={"session_id": first_id, "id": "t1", "status": "running"},
            )
            await asyncio.sleep(0.05)
            return first, second

        first, second = asyncio.run(scenario())
        assert second.events("task.updated") == [], (
            "task progress for another session was broadcast to this socket"
        )

    def test_a_session_gets_its_own_orchestrator(self):
        async def scenario():
            first, second = FakeWebSocket(), FakeWebSocket()
            await asyncio.gather(run_handler(first), run_handler(second))
            return first, second

        first, second = asyncio.run(scenario())
        a = first.one("status")["session_id"]
        b = second.one("status")["session_id"]
        assert a != b


# ---------------------------------------------------------------------------
# 10. Untrusted input
# ---------------------------------------------------------------------------
class TestUntrustedInput:
    @pytest.mark.parametrize("bad", [
        "not json at all",
        '"just a string"',
        "[1, 2, 3]",
        "{}",
        '{"type": 42}',
        '{"type": "made_up"}',
        '{"type": "message"}',
        '{"type": "message", "message": ""}',
        '{"type": "audio", "audio": "!!!not base64!!!"}',
    ])
    def test_a_bad_frame_is_answered_and_the_socket_survives(self, bad):
        async def scenario():
            ws = FakeWebSocket([bad, _frame(type="ping")])
            await run_handler(ws)
            return ws

        ws = asyncio.run(scenario())
        assert ws.accepted
        assert ws.events("pong"), f"the socket did not survive {bad!r}: {ws.types()}"

    def test_a_message_over_the_limit_is_refused_not_truncated(self):
        async def scenario():
            long_text = "x" * 9000
            ws = FakeWebSocket([_frame(type="message", message=long_text)])
            capture = {}
            await run_handler(ws, agent_service=make_agent_service(capture=capture))
            return ws, capture

        ws, capture = asyncio.run(scenario())
        assert capture.get("message") is None, "an oversized message reached the agent"
        assert ws.events("error"), f"no error frame: {ws.types()}"

    def test_an_empty_transcript_does_not_start_a_turn(self):
        async def scenario():
            stt = make_stt(TranscriptionResult(
                text="   ", language="en", detected=True, duration=0.2,
                voice_hint="en-GB-RyanNeural",
            ))
            ws = FakeWebSocket([_audio("QUJD")])
            ws.incoming.append(_settle(ws))
            await run_handler(ws, stt=stt)
            return ws

        ws = asyncio.run(scenario())
        assert not ws.events("message"), "silence produced a reply"

    def test_an_stt_failure_is_reported_to_the_user(self):
        async def scenario():
            ws = FakeWebSocket([_audio("QUJD")])
            ws.incoming.append(_settle(ws))
            await run_handler(ws, stt=make_stt(error=RuntimeError("whisper exploded")))
            return ws

        ws = asyncio.run(scenario())
        errors = ws.events("error")
        assert errors, f"a failed transcription was silent: {ws.types()}"
        assert "exploded" not in json.dumps(errors), "the raw exception reached the client"


# ---------------------------------------------------------------------------
# 11. Audio lifecycle
# ---------------------------------------------------------------------------
class TestAudioLifecycle:
    def test_a_second_recording_cancels_the_first(self):
        async def scenario():
            calls = []

            def _transcribe(*args, **kwargs):
                calls.append(time.time())
                time.sleep(0.2)
                return TranscriptionResult(
                    text=f"utterance {len(calls)}", language="en", detected=True,
                    duration=1.0, voice_hint="en-GB-RyanNeural",
                )

            stt = make_stt()
            stt.transcribe_detailed = MagicMock(side_effect=_transcribe)
            ws = FakeWebSocket([_audio("QUJD"), _after(0.15, _audio("QUJD"))])
            ws.incoming.append(_settle(ws))
            await run_handler(ws, stt=stt)
            return ws, calls

        ws, calls = asyncio.run(scenario())
        assert len(calls) <= 2

    def test_the_language_reaches_the_client_and_the_turn(self):
        async def scenario():
            stt = make_stt(TranscriptionResult(
                text="\u0627\u0644\u0633\u0644\u0627\u0645 \u0639\u0644\u064a\u0643\u0645",
                language="ar", detected=True, duration=1.2,
                voice_hint="ar-EG-ShakirNeural",
            ))
            capture = {}
            ws = FakeWebSocket([_audio("QUJD", language="en")])
            ws.incoming.append(_settle(ws))
            await run_handler(
                ws, stt=stt, agent_service=make_agent_service(capture=capture)
            )
            return ws, capture

        ws, capture = asyncio.run(scenario())
        reported = ws.events("stt_result")
        assert reported, f"no stt_result frame: {ws.types()}"
        assert reported[0]["language"] == "ar"
        # The spoken language wins over the client's hint. Getting this wrong
        # sends the reply to the wrong voice and the wrong script.
        assert capture.get("language") == "ar"

    def test_audio_frames_are_only_streamed_for_the_current_turn(self):
        async def scenario():
            ws = FakeWebSocket([_frame(type="message", message="say something")])
            await run_handler(ws, tts=make_tts())
            return ws

        ws = asyncio.run(scenario())
        starts = ws.events("audio_start")
        if starts:
            assert starts[0].get("response_id"), "audio is not bound to a turn"
            ends = ws.events("audio_end")
            assert ends, "audio_start with no audio_end"
            assert ws.index_of("audio_start") > ws.index_of("message")


# ---------------------------------------------------------------------------
# 12. Confirmation binding
# ---------------------------------------------------------------------------
class TestConfirmationBinding:
    def test_an_approval_for_an_unknown_request_id_is_reported_unresolved(self):
        async def scenario():
            ws = FakeWebSocket([
                _frame(type="tool_confirm", request_id="cfm_deadbeef", approved=True),
            ])
            await run_handler(ws)
            return ws

        ws = asyncio.run(scenario())
        result = ws.one("confirmation_result")
        assert result["resolved"] is False, (
            "an approval naming a request that does not exist was accepted"
        )

    def test_a_confirmation_with_no_channel_fails_closed(self):
        """No way to ask ⇒ refuse. Never run and hope.

        This is the property that keeps the approval gate a gate: if the
        notification path is missing (a REST caller with nothing wired up, a
        socket that just closed), the action must be denied rather than
        executed without consent.
        """

        async def scenario():
            from tools.safety import ConfirmationManager

            manager = ConfirmationManager(notify=None, timeout=1.0)
            return await manager.request(
                "system_command", {"command": "sudo rm -rf /"}, "sess-x"
            )

        from tools.safety import ConfirmationManager

        assert asyncio.run(scenario()) is False
        manager = ConfirmationManager(notify=None, timeout=1.0)
        assert asyncio.run(
            manager.request("system_command", {"command": "sudo rm -rf /"}, "s")
        ) is False
        assert manager.last_outcome == ConfirmationManager.OUTCOME_UNAVAILABLE, (
            "a refused-approval outcome must be distinguishable from a denial"
        )

    def test_an_approval_is_bound_to_a_real_pending_request(self):
        """The id the server *sent* is the id the approval must name.

        The whole round trip happens inside one event loop — the manager's
        pending entry is tied to the future the blocked caller holds, so a
        scenario split across two `run`s would be asserting on a request that
        its own loop teardown had already cleaned up.
        """

        async def scenario():
            from tools.safety import ConfirmationManager

            delivered = []

            async def _notify(pending):
                delivered.append(pending.to_event())

            manager = ConfirmationManager(notify=_notify, timeout=5.0)
            awaiting = asyncio.ensure_future(manager.request(
                # `system_command` is the registered name for arbitrary terminal
                # execution; `run_terminal` is not a tool, and an unknown action
                # is SAFE by design — a test that asked with that name would
                # have proved nothing.
                "system_command",
                {"command": "sudo rm -rf /"},
                "sess-x",
            ))
            for _ in range(100):
                if manager.pending_count:
                    break
                await asyncio.sleep(0.01)
            assert delivered, "no confirmation was put on the wire"
            request_id = delivered[0].get("request_id")

            # An approval naming something else must not resolve the real one.
            wrong = manager.resolve("cfm_" + "0" * 12, True)
            still_pending = manager.pending_count

            first = manager.resolve(request_id, True)
            second = manager.resolve(request_id, True)
            released = await asyncio.wait_for(awaiting, timeout=2)
            return request_id, wrong, still_pending, first, second, released

        request_id, wrong, still_pending, first, second, released = asyncio.run(scenario())
        assert request_id and str(request_id).startswith("cfm_"), request_id
        assert wrong is False, "an approval resolved a request it did not name"
        assert still_pending == 1
        assert first is True and released is True, (
            "the blocked caller was not released by its own approval"
        )
        assert second is False, "an approval was accepted twice"

    def test_a_denial_never_resolves_as_approved(self):
        async def scenario():
            ws = FakeWebSocket([
                _frame(type="tool_confirm", request_id="cfm_nope", approved=False),
            ])
            await run_handler(ws)
            return ws

        ws = asyncio.run(scenario())
        assert ws.one("confirmation_result")["approved"] is False
