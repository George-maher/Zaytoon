"""The voice lifecycle, asserted twice: as a state machine, and over the socket.

Two defects motivate this file, and neither was visible from a unit test of a
single component:

1. **An ordinary utterance executed.** The browser attached
   ``isManualVoice: true`` to every VAD segment, and the server read that flag
   as "the wake-word gate does not apply", so continuous listening skipped the
   gate on every segment.
2. **A command announced its own cancellation.** The receive loop cancelled
   ``state.audio_task`` whenever a newer audio frame arrived, and the browser
   sent an ``interrupt`` before every utterance — so ending a recording could
   kill the command that recording had just produced.

The first half of this module pins the transitions directly (``VoiceSession``),
because a state machine that only works when driven through six layers is not a
state machine. The second half drives the real WebSocket handler over a fake
socket and reads the frames, because the bugs lived in the seam between the
transport and the pipeline: the gate and the state both existed, and neither was
consulted at the moment that mattered.

Nothing here uses a microphone. Real hardware behaviour (device denial, device
loss, echo of our own TTS) is covered where it can be observed — the browser
hook's tests — and the limitation is stated in the report rather than implied.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import MagicMock

import pytest

import api.websocket as wsmod
from services.wake_word_service import apply_wake_gate
from services.voice_session import (
    ACTIVE_COMMAND_STATES,
    CAPTURING_STATES,
    LEGAL_TRANSITIONS,
    VoiceSession,
    VoiceState,
    remember_utterance,
    reset_utterance_ledger,
)
from voice.stt import TranscriptionResult

from tests.test_ws_protocol import (
    FakeWebSocket,
    _after,
    _frame,
    _settle,
    make_agent_service,
    make_config,
    make_stt,
    make_tts,
    run_handler,
    session_states,
)


WAKE_AR = "زيتون"
WAKE_EN = "zaytoon"


def _audio(utterance_id: str, b64: str = "QUJD", **extra) -> str:
    """The audio frame the fixed client sends: no manual-bypass claim."""
    return _frame(type="audio", audio=b64, utterance_id=utterance_id, **extra)


def _voice(action: str) -> str:
    return _frame(type="voice", action=action)


def _transcript(text: str, language: str = "en") -> TranscriptionResult:
    return TranscriptionResult(
        text=text,
        language=language,
        detected=True,
        duration=1.0,
        voice_hint="ar-EG-ShakirNeural" if language == "ar" else "en-GB-RyanNeural",
    )


def _states(ws: FakeWebSocket) -> list[str]:
    return [f.get("state") for f in ws.events("voice_state")]


def _reasons(ws: FakeWebSocket) -> list[str]:
    return [f.get("reason") for f in ws.events("voice_state")]


@pytest.fixture(autouse=True)
def _clean_state():
    """No session, task or remembered utterance may leak between tests."""
    reset_utterance_ledger()
    yield
    session_states.clear()
    wsmod._AUDIO_TASKS.clear()
    wsmod._SESSION_ORCHESTRATORS.clear()
    wsmod._SEEN_MESSAGE_IDS.clear()
    wsmod._request_traces.clear()
    reset_utterance_ledger()


# ---------------------------------------------------------------------------
# 1. The state machine, on its own
# ---------------------------------------------------------------------------
class TestTheStateMachineIsDeclared:
    def test_every_state_is_reachable_from_somewhere(self):
        reached = set()
        for targets in LEGAL_TRANSITIONS.values():
            reached |= targets
        unreachable = set(VoiceState) - reached
        assert not unreachable, (
            f"states nothing can enter: {unreachable} — a state with no inbound "
            "edge is either dead code or a missing transition"
        )

    def test_an_illegal_transition_is_refused_and_does_not_move(self):
        voice = VoiceSession()
        voice.enable()  # DISABLED -> STANDBY
        assert voice.state is VoiceState.STANDBY
        # STANDBY is not allowed to become AWAITING_CONFIRMATION: a confirmation
        # belongs to a command that was accepted.
        assert voice.transition(VoiceState.AWAITING_CONFIRMATION, "bogus") is False
        assert voice.state is VoiceState.STANDBY, (
            "a refused transition still moved the state, so the UI and the "
            "pipeline can disagree"
        )

    def test_a_capturing_state_never_contains_an_accepted_command(self):
        assert not (CAPTURING_STATES & ACTIVE_COMMAND_STATES)

    def test_the_documented_happy_path_is_legal_step_by_step(self):
        voice = VoiceSession()
        voice.enable()
        assert voice.state is VoiceState.STANDBY
        voice.arm_follow_up()
        assert voice.state is VoiceState.LISTENING
        voice.command_accepted("utt-1", "resp-1")
        assert voice.state is VoiceState.PROCESSING
        voice.awaiting_confirmation()
        assert voice.state is VoiceState.AWAITING_CONFIRMATION
        voice.confirmation_resolved(True)
        assert voice.state is VoiceState.PROCESSING
        voice.speaking()
        assert voice.state is VoiceState.SPEAKING
        voice.turn_complete()
        assert voice.state is VoiceState.STANDBY


class TestEnableDisable:
    def test_enabling_starts_in_standby_not_listening(self):
        voice = VoiceSession()
        assert voice.state is VoiceState.DISABLED
        voice.enable()
        assert voice.state is VoiceState.STANDBY
        assert voice.enabled is True

    def test_enabling_mid_command_does_not_reset_the_command(self):
        voice = VoiceSession()
        voice.enable()
        voice.command_accepted("utt-1", "resp-1")
        voice.enable()
        assert voice.state is VoiceState.PROCESSING, (
            "a redundant enable frame reset a running command to standby"
        )

    def test_disabling_clears_the_follow_up_window(self):
        voice = VoiceSession()
        voice.enable()
        voice.arm_follow_up()
        voice.disable()
        assert voice.state is VoiceState.DISABLED
        assert voice.enabled is False
        assert voice.awaiting_command is False
        assert voice.last_active_ts == 0.0

    def test_disabling_a_running_command_does_not_leave_it_processing(self):
        """Voice off must not look like an active command forever.

        It also must not *cancel* it — the turn owns its own cancellation, and
        that is asserted over the socket below. Here the state has to become
        DISABLED, because that is what the UI will render.
        """
        voice = VoiceSession()
        voice.enable()
        voice.command_accepted("utt-1", "resp-1")
        voice.disable()
        assert voice.state is VoiceState.DISABLED

    def test_turn_completion_with_voice_off_stays_disabled(self):
        voice = VoiceSession()
        voice.enable()
        voice.disable()
        voice.turn_complete()
        assert voice.state is VoiceState.DISABLED


class TestTheWakeWindow:
    def test_arming_is_explicit_and_only_the_wake_word_arms_it(self):
        voice = VoiceSession()
        voice.enable()
        assert voice.awaiting_command is False
        voice.arm_follow_up()
        assert voice.awaiting_command is True
        assert voice.last_active_ts > 0
        voice.consume_follow_up()
        assert voice.awaiting_command is False
        assert voice.last_active_ts == 0.0

    def test_a_typed_turn_never_touches_the_arming_flag(self):
        """The regression: a typed answer used to arm the microphone for 10s."""
        voice = VoiceSession()
        voice.enable()
        voice.command_accepted("utt-1", "resp-1")
        voice.turn_complete()
        assert voice.awaiting_command is False
        assert voice.last_active_ts == 0.0


class TestUtteranceIdempotency:
    def test_the_same_id_is_seen_once(self):
        voice = VoiceSession()
        assert voice.saw_utterance("utt-1") is False
        assert voice.saw_utterance("utt-1") is True

    def test_the_ledger_is_bounded(self):
        voice = VoiceSession()
        for i in range(10):
            voice.saw_utterance(f"utt-{i}", cap=5)
        assert len(voice._seen) <= 5

    def test_an_empty_id_is_never_a_duplicate(self):
        voice = VoiceSession()
        assert voice.saw_utterance("") is False
        assert voice.saw_utterance("") is False

    def test_an_id_survives_a_new_session(self):
        """A reconnect creates a new `SessionState`; a replay must still lose."""
        assert remember_utterance("utt-across") is False
        assert remember_utterance("utt-across") is True

    def test_the_process_ledger_is_bounded(self):
        from services import voice_session

        for i in range(voice_session._UTTERANCE_MAX + 10):
            remember_utterance(f"u{i}")
        assert len(voice_session._SEEN_UTTERANCES) <= voice_session._UTTERANCE_MAX


class TestTheWireShape:
    def test_the_frame_says_what_the_ui_needs(self):
        voice = VoiceSession()
        voice.enable()
        voice.arm_follow_up()
        frame = voice.to_frame()
        assert frame["type"] == "voice_state"
        assert frame["state"] == "listening"
        assert frame["enabled"] is True
        assert frame["awaiting_command"] is True

    def test_a_disabled_frame_carries_no_utterance(self):
        frame = VoiceSession().to_frame()
        assert frame["type"] == "voice_state"
        assert frame["state"] == "disabled"
        assert frame["enabled"] is False
        assert "utterance_id" not in frame


# ---------------------------------------------------------------------------
# 2. The gate and the lifecycle, over the real handler
# ---------------------------------------------------------------------------
def _wake_config(**overrides):
    settings = {
        "WAKE_WORD_REQUIRED": True,
        "WAKE_WORD_HINT": True,
        "WAKE_WORD_ALLOW_MANUAL_BYPASS": False,
    }
    settings.update(overrides)
    return make_config(**settings)


class TestWakeWordIsAGate:
    def test_ordinary_speech_never_reaches_the_agent(self):
        async def scenario():
            captured: dict = {}
            stt = make_stt(_transcript("what is the weather like"))
            ws = FakeWebSocket([_audio("utt-1")])
            ws.incoming.append(_settle(ws))
            await run_handler(
                ws,
                config=_wake_config(),
                stt=stt,
                agent_service=make_agent_service(capture=captured),
            )
            return ws, captured

        ws, captured = asyncio.run(scenario())
        assert "message" not in captured, (
            "an utterance with no wake word reached the agent, so ordinary "
            "speech in the room executed"
        )
        assert not ws.events("message")
        assert ws.one("wake_word")["detected"] is False
        assert ws.one("voice_state")["state"] in ("standby", "listening")

    def test_wake_word_and_command_in_one_utterance_executes_the_command(self):
        async def scenario():
            captured: dict = {}
            stt = make_stt(_transcript("زيتون افتح كروم", language="ar"))
            ws = FakeWebSocket([_audio("utt-1")])
            ws.incoming.append(_settle(ws))
            await run_handler(
                ws,
                config=_wake_config(),
                stt=stt,
                agent_service=make_agent_service(capture=captured),
            )
            return ws, captured

        ws, captured = asyncio.run(scenario())
        assert captured.get("message") == "افتح كروم", (
            f"the wake word was not stripped from the command: {captured!r}"
        )
        assert ws.events("wake_word")[0]["detected"] is True
        assert ws.events("message"), [f.get("type") for f in ws.sent]

    def test_wake_word_alone_arms_the_next_utterance(self):
        async def scenario():
            captured: dict = {}
            stt = make_stt()
            stt.transcribe_detailed = MagicMock(
                side_effect=[
                    _transcript("zaytoon"),
                    _transcript("open chrome"),
                ]
            )
            ws = FakeWebSocket([_audio("utt-1")])
            ws.incoming.append(_settle(ws))
            ws.incoming.append(_audio("utt-2"))
            ws.incoming.append(_settle(ws))
            await run_handler(
                ws,
                config=_wake_config(),
                stt=stt,
                agent_service=make_agent_service(capture=captured),
            )
            return ws, captured

        ws, captured = asyncio.run(scenario())
        assert captured.get("message") == "open chrome", (
            "the follow-up utterance was not accepted as the command that "
            f"followed the wake word: {captured!r}"
        )
        states = _states(ws)
        assert "listening" in states, states
        assert states[-1] == "standby", states

    def test_the_manual_bypass_is_off_by_default(self):
        async def scenario():
            captured: dict = {}
            ws = FakeWebSocket([
                _audio("utt-1", isManualVoice=True),
            ])
            ws.incoming.append(_settle(ws))
            await run_handler(
                ws,
                config=_wake_config(),
                stt=make_stt(_transcript("open chrome")),
                agent_service=make_agent_service(capture=captured),
            )
            return ws, captured

        _, captured = asyncio.run(scenario())
        assert "message" not in captured, (
            "isManualVoice still bypassed the wake gate with the default "
            "configuration, which is exactly how continuous listening executed "
            "every segment"
        )

    def test_the_manual_bypass_works_when_the_deployment_opts_in(self):
        async def scenario():
            captured: dict = {}
            ws = FakeWebSocket([_audio("utt-1", isManualVoice=True)])
            ws.incoming.append(_settle(ws))
            await run_handler(
                ws,
                config=_wake_config(WAKE_WORD_ALLOW_MANUAL_BYPASS=True),
                stt=make_stt(_transcript("open chrome")),
                agent_service=make_agent_service(capture=captured),
            )
            return ws, captured

        _, captured = asyncio.run(scenario())
        assert captured.get("message") == "open chrome"


class TestOneUtteranceAtATime:
    def test_stopping_the_recording_does_not_cancel_the_command(self):
        """The reported bug: "the command I just spoke never finished"."""

        async def scenario():
            captured: dict = {}
            ws = FakeWebSocket([_audio("utt-1")])
            # The user lets go of the microphone while the turn is running.
            ws.incoming.append(_after(0.1, _voice("stop_recording")))
            ws.incoming.append(_settle(ws))
            await run_handler(
                ws,
                config=_wake_config(),
                stt=make_stt(_transcript("zaytoon open chrome")),
                agent_service=make_agent_service(capture=captured, delay=0.4),
            )
            return ws, captured

        ws, captured = asyncio.run(scenario())
        assert captured.get("message") == "open chrome"
        assert ws.events("message"), (
            "the accepted command was cancelled by the recording stopping: "
            f"{[f.get('type') for f in ws.sent]}"
        )
        assert not ws.events("interrupted")

    def test_an_overlapping_recording_is_refused_not_a_cancellation(self):
        async def scenario():
            captured: dict = {}
            ws = FakeWebSocket([_audio("utt-1")])
            ws.incoming.append(_after(0.05, _audio("utt-2")))
            ws.incoming.append(_settle(ws))
            await run_handler(
                ws,
                config=_wake_config(),
                stt=make_stt(_transcript("zaytoon open chrome")),
                agent_service=make_agent_service(capture=captured, delay=0.4),
            )
            return ws, captured

        ws, captured = asyncio.run(scenario())
        assert captured.get("message") == "open chrome"
        assert "busy" in _reasons(ws), _reasons(ws)
        errors = [f for f in ws.events("error") if "Still working" in f.get("content", "")]
        assert errors, [f.get("content") for f in ws.events("error")]
        assert ws.events("message"), "the first command was lost to the second frame"

    def test_a_replayed_utterance_runs_once(self):
        async def scenario():
            captured: dict = {}
            ws = FakeWebSocket([_audio("utt-same")])
            # Wait for the first turn to finish before replaying: a frame that
            # arrives while the command is still running is refused as *busy*
            # (a different, also-correct outcome), and this test is about the
            # replay path.
            ws.incoming.append(_settle(ws))
            ws.incoming.append(_audio("utt-same"))
            ws.incoming.append(_settle(ws))
            await run_handler(
                ws,
                config=_wake_config(),
                stt=make_stt(_transcript("zaytoon open chrome")),
                agent_service=make_agent_service(capture=captured),
            )
            return ws, captured

        ws, captured = asyncio.run(scenario())
        assert captured.get("message") == "open chrome"
        assert "replayed_utterance" in _reasons(ws), _reasons(ws)
        assert len(ws.events("message")) == 1, ws.events("message")

    def test_the_same_words_with_a_new_id_are_a_new_command(self):
        """Dedupe is by id, never by text: "open it again" is a real request."""
        async def scenario():
            captured: list[str] = []
            service = make_agent_service(capture={}, delay=0.05)
            ws = FakeWebSocket([_audio("utt-1")])
            ws.incoming.append(_settle(ws))
            ws.incoming.append(_audio("utt-2"))
            ws.incoming.append(_settle(ws))
            await run_handler(
                ws,
                config=_wake_config(),
                stt=make_stt(_transcript("zaytoon open chrome")),
                agent_service=service,
            )
            return ws

        ws = asyncio.run(scenario())
        assert len(ws.events("message")) == 2, ws.events("message")


class TestExplicitCancel:
    def test_cancel_stops_the_command_and_returns_to_standby(self):
        async def scenario():
            captured: dict = {}
            ws = FakeWebSocket([_audio("utt-1")])
            ws.incoming.append(_after(0.15, _voice("cancel")))
            ws.incoming.append(_settle(ws))
            await run_handler(
                ws,
                config=_wake_config(),
                stt=make_stt(_transcript("zaytoon open chrome")),
                agent_service=make_agent_service(capture=captured, delay=0.6),
            )
            return ws, captured

        ws, _ = asyncio.run(scenario())
        assert ws.events("interrupted"), [f.get("type") for f in ws.sent]
        assert not ws.events("message"), "a cancelled command still answered"
        assert _states(ws)[-1] == "standby", _states(ws)
        assert "cancelled" in _reasons(ws), _reasons(ws)

    def test_interrupt_is_the_same_operation_as_voice_cancel(self):
        async def scenario():
            ws = FakeWebSocket([_audio("utt-1")])
            ws.incoming.append(_after(0.15, _frame(type="interrupt")))
            ws.incoming.append(_settle(ws))
            await run_handler(
                ws,
                config=_wake_config(),
                stt=make_stt(_transcript("zaytoon open chrome")),
                agent_service=make_agent_service(delay=0.6),
            )
            return ws

        ws = asyncio.run(scenario())
        assert ws.events("interrupted")
        assert not ws.events("message")


class TestTheSocketSurvives:
    def test_a_typed_message_still_works_after_a_voice_turn(self):
        async def scenario():
            replies = iter(["spoken answer", "typed answer"])
            service = make_agent_service(reply=lambda _m: next(replies))
            ws = FakeWebSocket([_audio("utt-1")])
            ws.incoming.append(_settle(ws))
            ws.incoming.append(_frame(type="message", message="hello"))
            ws.incoming.append(_settle(ws))
            await run_handler(
                ws,
                config=_wake_config(),
                stt=make_stt(_transcript("zaytoon what time is it")),
                agent_service=service,
            )
            return ws

        ws = asyncio.run(scenario())
        contents = [f["content"] for f in ws.events("message")]
        assert contents == ["spoken answer", "typed answer"], contents

    def test_voice_mode_turns_off_but_the_socket_stays_open(self):
        async def scenario():
            ws = FakeWebSocket([_frame(type="message", message="hi")])
            ws.incoming.append(_voice("enable"))
            ws.incoming.append(_voice("disable"))
            ws.incoming.append(_settle(ws))
            await run_handler(ws, config=_wake_config())
            return ws

        ws = asyncio.run(scenario())
        states = _states(ws)
        assert states[0] == "disabled"  # socket_open
        assert "standby" in states
        assert states[-1] == "disabled", states
        assert ws.closed is None, "disabling voice closed the socket"
        assert ws.events("message"), "the socket stopped serving turns after a disable"

    def test_an_stt_failure_reports_and_recovers_to_standby(self):
        async def scenario():
            ws = FakeWebSocket([_audio("utt-1")])
            ws.incoming.append(_settle(ws))
            await run_handler(
                ws,
                config=_wake_config(),
                stt=make_stt(error=RuntimeError("whisper exploded")),
            )
            return ws

        ws = asyncio.run(scenario())
        assert ws.events("error"), [f.get("type") for f in ws.sent]
        states = _states(ws)
        assert "recovering" in states, states
        assert states[-1] == "standby", (
            "a failed transcription left the voice UI in RECOVERING with no "
            f"path out: {states}"
        )
        assert not ws.events("message")


class TestConfirmationPausesVoice:
    def test_the_state_is_awaiting_confirmation_while_a_person_decides(self):
        """The state has to be visible, or the UI shows a working spinner.

        Driven at the state-machine level because the engine's confirmation
        round trip is covered by ``test_ws_protocol.TestConfirmationBinding``;
        what this file owns is that the voice state follows it.
        """
        voice = VoiceSession()
        voice.enable()
        voice.command_accepted("utt-1", "resp-1")
        voice.awaiting_confirmation()
        assert voice.to_frame()["state"] == "awaiting_confirmation"
        voice.confirmation_resolved(False)  # a decline still continues the turn
        assert voice.state is VoiceState.PROCESSING


# ---------------------------------------------------------------------------
# 3. What counts as an activation — in both languages
# ---------------------------------------------------------------------------
class TestWhatIsNotAnActivation:
    """The gate is a boundary, not a keyword search.

    "زيتونة" (an olive) is an ordinary Arabic word, and "I asked Zaytoon
    yesterday" is an ordinary English sentence. If either activated the
    assistant, the microphone would execute the rest of a conversation nobody
    addressed to it — the reported failure, in its most common form.
    """

    @pytest.mark.parametrize(
        "utterance",
        [
            "",
            "   ",
            "\n\n",
            "زيتونة",
            "زيتونية",
            "حبة زيتون",
            "الزيتون غالي النهارده",
            "zaytoonish",
            "I asked Zaytoon yesterday about the report",
            "what did zaytoon say",
            "hey there",
        ],
    )
    def test_ordinary_speech_is_dropped_before_any_turn_exists(self, utterance):
        result = apply_wake_gate(
            utterance,
            awaiting_command=False,
            last_active_ts=0.0,
            timeout_secs=10.0,
        )
        assert result.acted is False
        assert result.dropped is True, (
            f"{utterance!r} was treated as a command; the gate is matching "
            "inside words or in the middle of a sentence"
        )
        assert result.text == ""

    def test_the_wake_word_alone_is_an_activation_without_a_command(self):
        result = apply_wake_gate(
            "زيتون", awaiting_command=False, last_active_ts=0.0, timeout_secs=10.0
        )
        assert result.detected is True
        assert result.acted is False
        assert result.dropped is False

    def test_an_arabic_wake_word_and_command_arrive_together(self):
        result = apply_wake_gate(
            "يا زيتون، افتح يوتيوب",
            awaiting_command=False,
            last_active_ts=0.0,
            timeout_secs=10.0,
        )
        assert result.acted is True
        assert result.detected is True
        assert result.text == "افتح يوتيوب"

    def test_an_armed_follow_up_needs_no_wake_word(self):
        result = apply_wake_gate(
            "افتح كروم",
            awaiting_command=True,
            last_active_ts=time.time(),
            timeout_secs=10.0,
        )
        assert result.acted is True
        assert result.text == "افتح كروم"

    def test_an_expired_follow_up_needs_the_wake_word_again(self):
        result = apply_wake_gate(
            "افتح كروم",
            awaiting_command=False,
            last_active_ts=time.time() - 60,
            timeout_secs=10.0,
        )
        assert result.dropped is True


class TestTheArabicWakeWordOverTheSocket:
    def test_the_wake_word_alone_arms_and_dispatches_nothing(self):
        async def scenario():
            captured: dict = {}
            ws = FakeWebSocket([_audio("utt-1", language="ar")])
            ws.incoming.append(_settle(ws))
            await run_handler(
                ws,
                config=_wake_config(),
                stt=make_stt(_transcript("زيتون", language="ar")),
                agent_service=make_agent_service(capture=captured),
            )
            return ws, captured

        ws, captured = asyncio.run(scenario())
        assert "message" not in captured
        assert not ws.events("message")
        assert ws.one("wake_word")["detected"] is True
        assert _states(ws)[-1] == "listening", (
            "the microphone was not armed for the follow-up: " f"{_states(ws)}"
        )

    def test_the_arabic_follow_up_is_the_command(self):
        async def scenario():
            captured: dict = {}
            stt = make_stt()
            stt.transcribe_detailed = MagicMock(
                side_effect=[
                    _transcript("زيتون", language="ar"),
                    _transcript("افتح كروم", language="ar"),
                ]
            )
            ws = FakeWebSocket([_audio("utt-1", language="ar")])
            ws.incoming.append(_settle(ws))
            ws.incoming.append(_audio("utt-2", language="ar"))
            ws.incoming.append(_settle(ws))
            await run_handler(
                ws,
                config=_wake_config(),
                stt=stt,
                agent_service=make_agent_service(capture=captured),
            )
            return ws, captured

        ws, captured = asyncio.run(scenario())
        assert captured.get("message") == "افتح كروم", (
            f"the Arabic follow-up was not accepted as the command: {captured!r}"
        )
        assert captured.get("language") == "ar"
        assert ws.events("message"), [f.get("type") for f in ws.sent]
        assert _states(ws)[-1] == "standby", _states(ws)


# ---------------------------------------------------------------------------
# 4. The assistant must not hear itself
# ---------------------------------------------------------------------------
def _slow_tts(delay: float = 0.4) -> MagicMock:
    """A TTS double that stays audibly speaking long enough to test the window."""
    tts = make_tts()

    async def _stream(text, **kwargs):
        await asyncio.sleep(delay)
        yield b"PCM"

    tts.stream_synthesize = _stream
    return tts


def _when_speaking(ws: FakeWebSocket, producer, timeout: float = 5.0):
    """An inbound item produced once the turn is actually in ``SPEAKING``.

    Timing a frame against a slow TTS is still a race: the turn reaches
    ``SPEAKING`` only after the orchestrator's own work (a task record, project
    detection), which is not a fixed duration. Waiting on the state makes the
    test about the guard rather than about the scheduler.
    """

    async def _wait():
        session_id = next((f.get("session_id") for f in ws.sent if f.get("session_id")), None)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = session_states.get(session_id) if session_id else None
            if state is not None and state.voice.state is VoiceState.SPEAKING:
                return producer() if callable(producer) else producer
            await asyncio.sleep(0.01)
        raise AssertionError("the turn never reached SPEAKING")

    return _wait


class TestTtsNeverActivatesItself:
    def test_speaking_is_not_a_capturing_state(self):
        assert VoiceState.SPEAKING not in CAPTURING_STATES
        voice = VoiceSession()
        voice.enable()
        voice.command_accepted("utt-1", "resp-1")
        voice.speaking()
        frame = voice.to_frame()
        assert frame["state"] == "speaking"
        assert frame["enabled"] is True

    def test_audio_arriving_while_speaking_is_refused(self):
        """Defence in depth: the client is told to mute, and the server refuses.

        The microphone picking up the assistant's own TTS is the shortest path
        to self-activation: the echo transcribes as a command, the command is
        answered, and the answer is spoken into the microphone again.
        """

        async def scenario():
            captured: dict = {}
            ws = FakeWebSocket()
            ws.incoming.append(_voice("enable"))
            ws.incoming.append(_frame(type="message", message="hello"))
            # The echo arrives while the assistant is still speaking.
            ws.incoming.append(_when_speaking(ws, _audio("utt-echo")))
            ws.incoming.append(_settle(ws))
            await run_handler(
                ws,
                config=_wake_config(),
                stt=make_stt(_transcript("zaytoon open chrome")),
                tts=_slow_tts(),
                agent_service=make_agent_service(capture=captured),
            )
            return ws, captured

        ws, captured = asyncio.run(scenario())
        assert captured.get("message") == "hello", (
            "the echoed audio was dispatched as a new command: "
            f"{captured!r}"
        )
        assert "speaking" in _reasons(ws), _reasons(ws)
        assert len(ws.events("message")) == 1, ws.events("message")

    def test_barge_in_is_the_explicit_cancel_frame(self):
        """While speaking, `voice cancel` still works — it is not audio."""

        async def scenario():
            ws = FakeWebSocket()
            ws.incoming.append(_voice("enable"))
            ws.incoming.append(_frame(type="message", message="hello"))
            ws.incoming.append(_when_speaking(ws, _voice("cancel")))
            ws.incoming.append(_settle(ws))
            await run_handler(
                ws,
                config=_wake_config(),
                tts=_slow_tts(),
                agent_service=make_agent_service(),
            )
            return ws

        ws = asyncio.run(scenario())
        assert _states(ws)[-1] == "standby", _states(ws)
        assert "cancelled" in _reasons(ws), _reasons(ws)


