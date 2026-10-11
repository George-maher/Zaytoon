"""The state machine: one mapping, three consumers, no drift.

``app/states.py`` is pure — no Tk, no sockets, no clock — so the whole nine
state vocabulary can be exercised directly. These tests are the contract the
header pill, the reactive core and the status line all rely on.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import protocol, states, theme  # noqa: E402
from app.states import CONNECTION_CONNECTED, CONNECTION_OFFLINE, CONNECTION_RECONNECTING
from app.states import Inputs, UiState  # noqa: E402


def frame(etype: protocol.EventType, **data) -> protocol.ServerEvent:
    return protocol.ServerEvent(etype, data)


class TestStateVocabulary:
    def test_exactly_nine_states(self):
        assert len(UiState) == 9

    def test_every_state_has_a_label_accent_and_glyph(self):
        for state in UiState:
            assert states.label(state).strip(), state
            assert states.accent(state).startswith("#"), state
            assert states.STATE_GLYPH[state], state

    def test_states_that_must_not_animate(self):
        # The whole point of the redesign: idle costs no timer at all.
        assert states.is_static(UiState.IDLE)
        assert states.is_static(UiState.OFFLINE)
        for state in UiState:
            if state not in (UiState.IDLE, UiState.OFFLINE):
                assert not states.is_static(state), state

    @pytest.mark.parametrize(
        "state,expected",
        [
            (UiState.OFFLINE, "Offline"),
            (UiState.IDLE, "Ready"),
            (UiState.LISTENING, "Listening…"),
            (UiState.UNDERSTANDING, "Understanding…"),
            (UiState.PLANNING, "Planning…"),
            (UiState.SPEAKING, "Speaking…"),
            (UiState.AWAITING_CONFIRM, "Awaiting confirmation…"),
            (UiState.ERROR, "Error — see the transcript"),
        ],
    )
    def test_spec_labels(self, state, expected):
        assert states.label(state) == expected

    def test_executing_label_names_the_tool(self):
        assert states.label(UiState.EXECUTING, "file_write") == "Calling tool: file_write…"

    def test_arabic_labels_differ_from_english(self):
        for state in UiState:
            english = states.label(state, language="en")
            arabic = states.label(state, language="ar")
            assert english != arabic, state


class TestPrecedence:
    """The order of the checks in ``derive`` is the design."""

    def test_offline_beats_everything(self):
        inputs = Inputs(
            connection=CONNECTION_OFFLINE,
            confirm_request="cfm_1",
            error="boom",
            tool="file_write",
            status="planning",
            streaming=True,
            speaking=True,
            listening=True,
            intent="complex",
        )
        assert states.derive(inputs)[0] is UiState.OFFLINE

    def test_reconnecting_is_offline_too(self):
        assert states.derive(Inputs(connection=CONNECTION_RECONNECTING))[0] is UiState.OFFLINE

    def test_pending_confirmation_beats_an_active_tool(self):
        inputs = Inputs(confirm_request="cfm_1", tool="file_write", error="boom")
        assert states.derive(inputs)[0] is UiState.AWAITING_CONFIRM

    def test_error_beats_a_running_tool(self):
        assert states.derive(Inputs(error="boom", tool="file_write"))[0] is UiState.ERROR

    def test_tool_beats_planning(self):
        inputs = Inputs(tool="file_write", status="planning", intent="complex")
        assert states.derive(inputs)[0] is UiState.EXECUTING

    def test_planning_from_status_or_from_intent(self):
        assert states.derive(Inputs(status="planning"))[0] is UiState.PLANNING
        assert states.derive(Inputs(intent="complex"))[0] is UiState.PLANNING

    def test_streaming_is_generating_not_speaking(self):
        state, text = states.derive(Inputs(streaming=True, speaking=True))
        assert state is UiState.SPEAKING
        assert text == "Generating response…"

    def test_playback_is_speaking(self):
        state, text = states.derive(Inputs(speaking=True))
        assert state is UiState.SPEAKING
        assert text == "Speaking…"

    def test_thinking_and_transcribing_are_understanding(self):
        assert states.derive(Inputs(status="thinking"))[0] is UiState.UNDERSTANDING
        assert states.derive(Inputs(status="transcribing"))[0] is UiState.UNDERSTANDING

    def test_local_mic_is_listening(self):
        assert states.derive(Inputs(listening=True))[0] is UiState.LISTENING

    def test_nothing_acting_is_idle(self):
        assert states.derive(Inputs())[0] is UiState.IDLE


class TestHudMapping:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("disconnected", UiState.OFFLINE),
            ("offline", UiState.OFFLINE),
            ("connected", UiState.IDLE),
            ("done", UiState.IDLE),
            ("listening", UiState.LISTENING),
            ("thinking", UiState.UNDERSTANDING),
            ("transcribing", UiState.UNDERSTANDING),
            ("planning", UiState.PLANNING),
            ("executing", UiState.EXECUTING),
            ("speaking", UiState.SPEAKING),
            ("awaiting_confirmation", UiState.AWAITING_CONFIRM),
            ("error", UiState.ERROR),
        ],
    )
    def test_known_statuses(self, raw, expected):
        assert states.status_to_state(raw) is expected

    def test_unknown_status_degrades_instead_of_raising(self):
        # A future server-side state must not crash the shell.
        assert states.status_to_state("something_new") in set(UiState)

    def test_every_state_round_trips_through_the_orb_vocabulary(self):
        for state in UiState:
            raw = states.orb_status(state)
            assert states.status_to_state(raw) is state, (state, raw)

    def test_accents_come_from_the_theme(self):
        # Deliberately literal: `allowed = set(theme.ACCENTS.values())` would be
        # `accent()` applied to itself and could never fail. The values are the
        # accent's current ones (the reference's accent turned out to be magenta
        # `#d946ef`, not the cyan `#22d3ee` this list used to assert), and updating
        # them is the reviewable half of a palette change.
        #
        # Green is in the set on its own account. It used to be omitted because
        # CYAN and GREEN shared a value, so a distinct entry would have been
        # redundant; the reference-derived palette separates them, and
        # `listening` legitimately paints green.
        allowed = {"#d946ef", "#35d399", "#f2b84b", "#ff6b6b", "#e879f9"}
        for state in UiState:
            assert states.accent(state).lower() in allowed, (state, states.accent(state))
        # The property that is not a restatement of `accent()`: the vocabulary of
        # token *names* is closed too, so a new accent has to be chosen from the
        # palette and recorded in both places rather than invented in `states.py`.
        assert set(theme.ACCENTS) == {"cyan", "green", "amber", "red", "violet"}
        assert set(states.STATE_ACCENT.values()) <= set(theme.ACCENTS)


def event(kind: str, **data) -> protocol.ServerEvent:
    return protocol.ServerEvent.parse(__import__("json").dumps({"type": kind, **data}))


class TestStateTracker:
    def test_connected_then_idle(self):
        tracker = states.StateTracker()
        tracker.on_event(event("connected"))
        assert tracker.inputs.connection == CONNECTION_CONNECTED
        assert tracker.resolve()[0] is UiState.IDLE

    def test_disconnected_goes_offline(self):
        tracker = states.StateTracker()
        tracker.on_event(event("disconnected"))
        assert tracker.resolve()[0] is UiState.OFFLINE

    def test_status_frame_drives_the_state(self):
        tracker = states.StateTracker()
        tracker.on_event(event("status", content="planning"))
        assert tracker.resolve()[0] is UiState.PLANNING

    def test_token_streaming_shows_generating(self):
        tracker = states.StateTracker()
        tracker.on_event(event("token", content="hi"))
        state, text = tracker.resolve()
        assert state is UiState.SPEAKING
        assert text == "Generating response…"

    def test_message_ends_the_stream(self):
        tracker = states.StateTracker()
        tracker.on_event(event("token", content="hi"))
        tracker.on_event(event("message", content="hi there"))
        assert tracker.inputs.streaming is False

    def test_intent_moves_to_understanding(self):
        tracker = states.StateTracker()
        tracker.on_event(event("intent", intent="tools", language="ar"))
        assert tracker.resolve()[0] is UiState.UNDERSTANDING
        assert tracker.resolve("ar")[1] == "بفهم…"

    def test_tool_lifecycle(self):
        tracker = states.StateTracker()
        tracker.on_event(event("tool_started", name="file_write", label="Write"))
        assert tracker.resolve()[0] is UiState.EXECUTING
        assert tracker.resolve()[1] == "Calling tool: file_write…"
        tracker.on_event(event("tool_finished", name="file_write", success=True))
        assert tracker.resolve()[0] is not UiState.EXECUTING

    def test_confirmation_round_trip(self):
        tracker = states.StateTracker()
        tracker.on_event(event("require_confirmation", request_id="cfm_9"))
        assert tracker.resolve()[0] is UiState.AWAITING_CONFIRM
        tracker.on_event(event("confirmation_result", request_id="cfm_9", resolved=True))
        assert tracker.resolve()[0] is not UiState.AWAITING_CONFIRM

    def test_error_is_sticky_until_the_next_turn(self):
        tracker = states.StateTracker()
        tracker.on_event(event("error", content="nope"))
        assert tracker.resolve()[0] is UiState.ERROR
        # A later "done" must not wipe the failure the user has not read yet.
        tracker.on_event(event("status", content="done"))
        assert tracker.resolve()[0] is UiState.ERROR
        # But starting a new turn does clear it.
        tracker.on_event(event("status", content="thinking"))
        assert tracker.resolve()[0] is not UiState.ERROR

    def test_interrupted_clears_the_turn(self):
        tracker = states.StateTracker()
        tracker.on_event(event("tool_started", name="file_write"))
        tracker.on_event(event("error", content="nope"))
        tracker.on_event(event("interrupted"))
        assert tracker.inputs.tool is None
        assert tracker.inputs.error == ""
        assert tracker.resolve()[0] is UiState.IDLE

    def test_reset_turn_clears_only_turn_scoped_inputs(self):
        tracker = states.StateTracker()
        tracker.set_connection(CONNECTION_RECONNECTING)
        tracker.inputs = tracker.inputs.updated(tool="x", streaming=True, error="e")
        tracker.reset_turn()
        assert tracker.inputs.tool is None
        assert tracker.inputs.streaming is False
        assert tracker.inputs.error == ""
        assert tracker.inputs.connection == CONNECTION_RECONNECTING
