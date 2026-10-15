"""The frame -> widget mapping in ``ZaytoonApp``.

``app.py`` is the only place that translates a server frame into something a
widget does, so it is the only place a frame can be silently dropped. It is
also the module that owns the Tk mainloop, which makes it awkward to test —
so this suite builds the object without running its constructor and hands it
recording doubles for the panel, the orb and the transport.

Nothing here opens a window, touches the network, or spawns a process.
"""

from __future__ import annotations

import base64

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import protocol, states  # noqa: E402
from app.app import MAX_RECENT_ACTIONS, ZaytoonApp  # noqa: E402


def frame(kind: str, **data) -> protocol.ServerEvent:
    event = protocol.ServerEvent.parse(__import__("json").dumps({"type": kind, **data}))
    assert event is not None, kind
    return event


class Recorder:
    """Records every call, including ones on nested attributes.

    ``panel.activity.reset()`` is logged as ``activity.reset`` against the same
    shared list, so a test can assert on the full path without building a
    hand-written double for each collaborator.
    """

    def __init__(self, log: list | None = None, path: str = "") -> None:
        object.__setattr__(self, "_log", log if log is not None else [])
        object.__setattr__(self, "_path", path)

    def __getattr__(self, name: str) -> "Recorder":
        if name.startswith("_"):
            raise AttributeError(name)
        parent = self._path
        return Recorder(self._log, f"{parent}.{name}" if parent else name)

    def __call__(self, *args, **kwargs):
        self._log.append((self._path, args, kwargs))
        return None

    def __bool__(self) -> bool:
        # ``if self.panel.awaiting_confirmation:`` must read as "unset",
        # exactly as an unpopulated attribute on the real panel would.
        return False

    # -- assertions -------------------------------------------------------
    @property
    def calls(self) -> list[tuple[str, tuple, dict]]:
        return list(self._log)

    def named(self, name: str) -> list[tuple]:
        return [args for (called, args, _kw) in self._log if called == name]

    def last(self, name: str) -> tuple[tuple, dict]:
        """The most recent ``(args, kwargs)`` recorded for ``name``."""
        for called, args, kwargs in reversed(self._log):
            if called == name:
                return args, kwargs
        raise AssertionError(f"{name} was never called; saw {[c for c, _a, _k in self._log]}")

    def has(self, name: str) -> bool:
        return any(called == name for called, _a, _k in self._log)


class FakeClient:
    def __init__(self) -> None:
        self.connected = True
        self.sent: list = []

    def ask(self, text, response_id=None):
        self.sent.append(("ask", text, response_id))
        return True

    def interrupt(self):
        self.sent.append(("interrupt",))

    def send_audio(self, pcm, language=""):
        self.sent.append(("audio", language))

    def respond_to_confirmation(self, request_id, approved):
        self.sent.append(("confirm", request_id, approved))


def make_shell():
    """A ZaytoonApp with its constructor skipped and its collaborators faked.

    One builder, called by the `app` fixture and directly by the tests that need
    an attribute changed. It used to exist twice — here and again inside
    `TestTurnScopedAudio` — and the copy omitted `client`, `events`, `mode` and
    `shutting_down`, which the constructor always sets. Nothing local caught it
    (`customtkinter` is absent, so this module is collect-skipped) and CI, where
    the module does run, failed on `'ZaytoonApp' object has no attribute
    'client'`.
    """
    shell = object.__new__(ZaytoonApp)
    shell.events = __import__("queue").Queue()
    shell.mode = None
    shell.shutting_down = False
    shell.status = "disconnected"
    shell.language = "en"
    shell.tracker = states.StateTracker()
    shell.ui_state = states.UiState.OFFLINE
    shell.recent_actions = []
    shell.model_name = ""
    shell.player = None
    shell.mic = None
    shell.tray = Recorder()
    shell.panel = Recorder()
    shell.orb = Recorder()
    shell.client = FakeClient()
    shell.telemetry = Recorder()
    shell._ids = __import__("itertools").count(1)
    # Turn-scoped state, at the values the constructor uses. Here rather than
    # in each test because the audio branches read them on *every* inbound
    # frame; `tests/test_shell_doubles.py` fails if this list falls behind
    # `__init__` again.
    shell._latest_response_id = None
    shell._ignore_audio = False
    # Set when the side panel is placed; only the geometry code reads them.
    shell._panel_saved_geometry = None
    shell._panel_saved_state = None
    # Set by a voice command that changed the status, so the connection frame
    # that follows does not overwrite it.
    shell._status_from_voice = None
    return shell


@pytest.fixture
def app():
    return make_shell()


def deliver(shell, *events) -> None:
    for event in events:
        shell._handle_event(event)


class TestConnectionFrames:
    def test_connected_turns_the_ui_on_and_asks_for_a_snapshot(self, app):
        deliver(app, frame("connected"))
        assert app.tracker.inputs.connection == states.CONNECTION_CONNECTED
        assert app.ui_state is states.UiState.IDLE
        assert app.telemetry.has("request_snapshot")
        assert app.panel.has("set_connection")

    def test_disconnected_goes_offline(self, app):
        deliver(app, frame("connected"), frame("disconnected"))
        assert app.ui_state is states.UiState.OFFLINE

    def test_reconnect_clears_a_stale_confirmation(self, app):
        deliver(app, frame("require_confirmation", request_id="cfm_1"))
        assert app.ui_state is states.UiState.AWAITING_CONFIRM
        deliver(app, frame("disconnected"), frame("connected"))
        assert app.tracker.inputs.confirm_request is None
        assert app.ui_state is states.UiState.IDLE


class TestTurnFrames:
    def test_intent_updates_language_and_task_card(self, app):
        deliver(app, frame("connected"), frame("intent", intent="tools", language="ar"))
        assert app.language == "ar"
        args, _kw = app.panel.last("set_task")
        assert args[0] == "tools"

    def test_tokens_reach_the_transcript(self, app):
        deliver(app, frame("connected"), frame("token", content="Hel"), frame("token", content="lo"))
        assert app.panel.named("append_token") == [("Hel",), ("lo",)]

    def test_a_token_turn_reads_as_generating(self, app):
        deliver(app, frame("connected"), frame("token", content="x"))
        assert app.ui_state is states.UiState.SPEAKING
        _args, kwargs = app.panel.last("set_ui_state")
        assert kwargs["text"] == "Generating response…"

    def test_message_finishes_the_stream(self, app):
        deliver(
            app,
            frame("connected"),
            frame("token", content="partial"),
            frame("message", content="the whole answer", response_id="r1"),
        )
        assert app.panel.named("set_transcript") == [("the whole answer",)]
        assert app.panel.named("set_busy")[-1] == (False,)
        assert app.tracker.inputs.streaming is False

    def test_message_done_alone_still_closes_the_turn(self, app):
        deliver(app, frame("connected"), frame("token", content="x"),
                frame("message_done", content="final"))
        assert app.panel.named("set_transcript") == [("final",)]
        assert app.panel.named("set_typing")[-1] == (False,)

    def test_message_done_with_no_content_still_stops_the_typing_dots(self, app):
        deliver(app, frame("connected"), frame("token", content="x"),
                frame("message_done"))
        assert app.panel.named("set_typing")[-1] == (False,)
        assert app.panel.named("set_busy")[-1] == (False,)


class TestActivityFrames:
    def test_plan_is_forwarded_verbatim(self, app):
        deliver(app, frame("connected"),
                frame("plan", steps=["Read", "Patch", ""]))
        assert app.panel.named("show_plan") == [(["Read", "Patch"],)]

    def test_an_empty_plan_is_not_forwarded(self, app):
        deliver(app, frame("connected"), frame("plan", steps=[]))
        assert not app.panel.has("show_plan")

    def test_tool_lifecycle_reaches_the_feed_and_the_action_log(self, app):
        deliver(
            app,
            frame("connected"),
            frame("tool_started", name="file_write", label="Write notes"),
            frame("tool_progress", name="file_write", message="halfway"),
            frame("tool_finished", name="file_write", success=True, summary="3 lines"),
        )
        assert app.panel.named("tool_started") == [("file_write", "Write notes")]
        assert app.panel.named("tool_progress") == [("file_write", "halfway")]
        assert app.panel.named("tool_finished") == [("file_write", True, "3 lines")]
        assert app.recent_actions[0] == "✓ 3 lines"
        assert app.ui_state is not states.UiState.EXECUTING

    def test_a_failed_tool_is_marked_failed(self, app):
        deliver(app, frame("connected"),
                frame("tool_finished", name="run_tests", success=False, summary="2 failed"))
        assert app.recent_actions[0] == "✕ 2 failed"

    def test_executing_state_while_a_tool_runs(self, app):
        deliver(app, frame("connected"),
                frame("tool_started", name="file_write", label="Write"))
        assert app.ui_state is states.UiState.EXECUTING

    def test_the_action_log_is_bounded(self, app):
        events = [frame("connected")]
        for index in range(MAX_RECENT_ACTIONS + 6):
            events.append(frame("tool_finished", name=f"t{index}", success=True))
        deliver(app, *events)
        assert len(app.recent_actions) == MAX_RECENT_ACTIONS

    def test_interrupt_stops_busy_and_clears_the_feed(self, app):
        deliver(app, frame("connected"),
                frame("tool_started", name="t"),
                frame("interrupted"))
        assert app.panel.named("set_busy")[-1] == (False,)
        assert app.panel.has("activity.reset")
        assert app.tracker.inputs.tool is None


class TestConfirmationFrames:
    def test_require_confirmation_opens_the_dialog_and_blocks(self, app):
        deliver(app, frame("connected"),
                frame("require_confirmation", request_id="cfm_7", level="high"))
        assert app.panel.has("show_confirmation")
        assert app.ui_state is states.UiState.AWAITING_CONFIRM

    def test_resolved_confirmation_unblocks(self, app):
        deliver(app, frame("connected"),
                frame("require_confirmation", request_id="cfm_7"),
                frame("confirmation_result", request_id="cfm_7", resolved=True))
        assert app.ui_state is not states.UiState.AWAITING_CONFIRM
        assert app.panel.named("resolve_confirmation") == [(True,)]

    def test_unresolved_confirmation_is_not_marked_resolved(self, app):
        deliver(app, frame("connected"),
                frame("require_confirmation", request_id="cfm_7"),
                frame("confirmation_result", request_id="cfm_7", resolved=False))
        assert app.panel.named("resolve_confirmation") == [(False,)]


class TestFailureFrames:
    def test_error_reaches_the_transcript_and_the_state(self, app):
        deliver(app, frame("connected"), frame("error", content="no models"))
        assert app.panel.named("add_message") == [("error", "no models")]
        assert app.ui_state is states.UiState.ERROR
        assert app.panel.named("set_busy")[-1] == (False,)

    def test_the_error_survives_the_following_done_frame(self, app):
        deliver(app, frame("connected"), frame("error", content="nope"),
                frame("status", content="done"))
        assert app.ui_state is states.UiState.ERROR

    def test_a_new_turn_clears_the_error(self, app):
        deliver(app, frame("connected"), frame("error", content="nope"),
                frame("status", content="thinking"))
        assert app.ui_state is not states.UiState.ERROR


class TestTransportFrames:
    def test_audio_chunks_reach_the_player(self, app):
        import base64

        payload = base64.b64encode(b"\x00\x01").decode("ascii")
        deliver(app, frame("connected"), frame("audio_chunk", data=payload))
        # No player open: the frame must be ignored, not raise.
        assert app.player is None

    def test_a_missing_player_does_not_crash_the_pump(self, app):
        import base64

        deliver(app, frame("connected"),
                frame("audio_chunk", data=base64.b64encode(b"x").decode("ascii")))

    def test_stt_language_is_honoured(self, app):
        deliver(app, frame("connected"), frame("stt_result", language="ar"))
        assert app.language == "ar"

    def test_unknown_language_is_ignored(self, app):
        deliver(app, frame("connected"), frame("stt_result", language="fr"))
        assert app.language == "en"

    def test_unknown_frame_type_is_ignored_not_fatal(self, app):
        deliver(app, frame("connected"), protocol.ServerEvent(protocol.EventType.UNKNOWN, {}))


class TestTelemetryFrames:
    def test_system_percentages_are_formatted(self, app):
        deliver(app, frame("connected"))
        app._handle(("telemetry", {"system": {"cpu": 12.5, "ram": 41}}))
        assert app.panel.named("set_system") == [("12.5%", "41.0%")]

    def test_missing_system_values_render_as_a_dash(self, app):
        app._handle(("telemetry", {"system": {"cpu": None, "ram": None}}))
        assert app.panel.named("set_system") == [("—", "—")]

    def test_tools_and_model_are_forwarded(self, app):
        app._handle(("telemetry", {"tools": ["a", "b"], "model": "m1"}))
        assert app.panel.named("set_tools") == [(["a", "b"],)]
        assert app.panel.named("set_model") == [("m1",)]

    def test_non_dict_telemetry_is_ignored(self, app):
        app._handle(("telemetry", "not a dict"))
        assert not app.panel.calls


class TestSendingAndInterrupting:
    def test_send_text_starts_a_fresh_turn(self, app):
        app.client.connected = True
        app.send_text("hello")
        assert app.panel.named("add_message") == [("user", "hello")]
        assert app.panel.named("set_busy")[-1] == (True,)
        assert app.ui_state is states.UiState.UNDERSTANDING
        assert app.client.sent[0][0] == "ask"

    def test_send_while_disconnected_reports_instead_of_sending(self, app):
        app.client.connected = False
        app.send_text("hello")
        assert app.client.sent == []
        assert app.panel.named("add_message") == [
            ("system", "Not connected to the backend yet.")
        ]

    def test_interrupt_clears_the_turn_and_the_playback(self, app):
        app.send_text("hello")
        app.interrupt()
        assert app.client.sent[-1] == ("interrupt",)
        assert app.panel.named("set_busy")[-1] == (False,)
        assert app.tracker.inputs.tool is None
        assert app.tracker.inputs.streaming is False

    def test_responding_to_a_confirmation_sends_one_answer(self, app):
        app.respond_to_confirmation("cfm_1", False)
        assert app.client.sent == [("confirm", "cfm_1", False)]
        assert app.tracker.inputs.confirm_request is None

    def test_a_confirmation_response_is_denied_by_default_in_the_payload(self):
        from app import dialogs

        assert dialogs.build_payload("cfm_1", False)["approved"] is False


class TestStateRefreshIsTheSingleFanOut:
    def test_every_handler_ends_in_one_refresh(self, app):
        # If a branch returned without refreshing, the header pill, the core
        # and the status line could disagree — this is the guarantee.
        deliver(app, frame("connected"), frame("status", content="planning"))
        states_seen = app.orb.named("set_status")
        panels_seen = app.panel.named("set_ui_state")
        assert len(states_seen) == len(panels_seen)
        assert states_seen[-1][0] is states.UiState.PLANNING
        assert panels_seen[-1][0] is states.UiState.PLANNING

    def test_the_orb_and_the_panel_are_handed_the_same_state(self, app):
        deliver(app, frame("connected"), frame("tool_started", name="x"))
        orb_state = app.orb.named("set_status")[-1][0]
        panel_state = app.panel.named("set_ui_state")[-1][0]
        assert orb_state is panel_state

    def test_arabic_is_passed_through_to_the_label(self, app):
        app.language = "ar"
        deliver(app, frame("connected"), frame("status", content="thinking"))
        args, kwargs = app.panel.last("set_ui_state")
        assert args[1] == "ar"          # the session language reaches the panel
        assert kwargs["text"] == "بفهم…"  # and the label is resolved in it


# ---------------------------------------------------------------------------
# Turn-scoped speech
# ---------------------------------------------------------------------------
class TestTurnScopedAudio:
    """`audio_start` for a replaced turn suppresses that turn's whole utterance.

    The server stops synthesising a superseded turn at the next chunk boundary,
    but chunks already on the socket still arrive, and this shell plays them as
    they land. Without the guard the previous answer is spoken over the one the
    user is waiting for.
    """

    def _shell(self):
        """The module's `app` fixture, with a recording player.

        Deliberately **not** a second copy of the fixture: the copy that used to
        live here omitted `client`, `events`, `mode` and `shutting_down`, which
        the real `__init__` always sets. Nothing caught it on the machine where
        it was written — `customtkinter` is absent there, so this module is
        collect-skipped — and CI, where the module does run, failed on
        `'ZaytoonApp' object has no attribute 'client'`. A double that is a copy
        of another double drifts from it the same way a copy of a schema drifts
        from the schema.
        """
        shell = make_shell()
        shell.status = "connected"
        shell.player = RecorderPlayer()
        return shell, shell.player

    def test_a_current_turn_plays(self):
        shell, player = self._shell()
        shell._latest_response_id = "r2"
        shell._handle_event(protocol.ServerEvent(
            protocol.EventType.AUDIO_START, {"response_id": "r2"}))
        shell._handle_event(protocol.ServerEvent(
            protocol.EventType.AUDIO_CHUNK,
            {"response_id": "r2", "data": base64.b64encode(b"PCM").decode()}))
        shell._handle_event(protocol.ServerEvent(
            protocol.EventType.AUDIO_END, {"response_id": "r2"}))
        assert player.played and not shell._ignore_audio

    def test_a_superseded_turn_is_silent(self):
        # Every frame must fold cleanly: `_handle_event` runs on the Tk thread.
        shell, player = self._shell()
        shell._latest_response_id = "r2"
        shell._handle_event(protocol.ServerEvent(
            protocol.EventType.AUDIO_START, {"response_id": "r1"}))
        assert shell._ignore_audio is True
        shell._handle_event(protocol.ServerEvent(
            protocol.EventType.AUDIO_CHUNK,
            {"response_id": "r1", "data": base64.b64encode(b"OLD").decode()}))
        shell._handle_event(protocol.ServerEvent(
            protocol.EventType.AUDIO_END, {"response_id": "r1"}))
        assert player.played == [], "a superseded turn was spoken aloud"

    def test_audio_end_clears_the_suppression(self):
        shell, player = self._shell()
        shell._latest_response_id = "r2"
        shell._handle_event(protocol.ServerEvent(
            protocol.EventType.AUDIO_START, {"response_id": "r1"}))
        shell._handle_event(protocol.ServerEvent(
            protocol.EventType.AUDIO_END, {"response_id": "r1"}))
        assert shell._ignore_audio is False
        # The next turn's audio must play: a dropped utterance may not poison
        # the speaker for the rest of the session.
        shell._handle_event(protocol.ServerEvent(
            protocol.EventType.AUDIO_START, {"response_id": "r2"}))
        shell._handle_event(protocol.ServerEvent(
            protocol.EventType.AUDIO_CHUNK,
            {"response_id": "r2", "data": base64.b64encode(b"NEW").decode()}))
        assert player.played == [b"NEW"]

    def test_a_chunk_with_no_start_frame_is_not_played(self):
        # Defensive: a `audio_chunk` arriving before any anchor frame used to
        # read an attribute that did not exist yet.
        shell, player = self._shell()
        shell._handle_event(protocol.ServerEvent(
            protocol.EventType.AUDIO_CHUNK, {"data": base64.b64encode(b"X").decode()}))
        assert player.played == [b"X"]  # no turn in flight: play it, as before

    def test_only_anchor_frames_move_the_current_turn(self):
        shell, _player = self._shell()
        shell._handle_event(protocol.ServerEvent(
            protocol.EventType.MESSAGE, {"content": "hi", "response_id": "r1"}))
        assert shell._latest_response_id == "r1"
        # A payload frame from the *old* turn must not move it back.
        shell._handle_event(protocol.ServerEvent(
            protocol.EventType.AUDIO_START, {"response_id": "r0"}))
        assert shell._latest_response_id == "r1"


class RecorderPlayer:
    """A stand-in for `app.audio.AudioPlayer` that records what it was asked to
    play. The real one decodes MP3 and pushes it at PortAudio, neither of which
    this machine can do — and neither of which is the question here."""
    def play(self, pcm: bytes) -> None:
        self.played.append(pcm)

    def __init__(self):
        self.played: list[bytes] = []
