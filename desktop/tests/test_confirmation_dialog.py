"""The approval round trip — security-critical, and therefore tested as data.

The dialog itself is a widget; everything it *decides* lives in
``app/dialogs`` as plain functions and one small state object, which is what
makes this testable without opening a window:

* the payload sent back is exactly ``protocol.tool_confirm``;
* a decision can be taken **once**;
* cancel is deny, and a request resolved elsewhere is never answered twice.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import dialogs, protocol  # noqa: E402


def sample(**overrides) -> dict:
    payload = {
        "type": "require_confirmation",
        "request_id": "cfm_01H8ZQ",
        "action": "file_write",
        "params": {"path": "notes/todo.md", "content": "buy milk"},
        "level": "confirm",
        "reasons": ["writes a file", "inside the workspace"],
        "summary": "Write notes/todo.md",
        "text": "Write the file notes/todo.md",
        "session_id": "s-1",
    }
    payload.update(overrides)
    return payload


class TestPayloadBuilder:
    def test_matches_the_protocol_builder(self):
        # One shape for the whole shell: if protocol.tool_confirm changes,
        # this assertion is where it shows up first.
        assert dialogs.build_payload("cfm_1", True) == protocol.tool_confirm("cfm_1", True)

    def test_payload_shape(self):
        assert dialogs.build_payload("cfm_1", True) == {
            "type": "tool_confirm",
            "request_id": "cfm_1",
            "approved": True,
        }

    def test_cancel_is_approved_false_not_missing(self):
        payload = dialogs.build_payload("cfm_1", False)
        assert payload["approved"] is False
        assert "request_id" in payload

    def test_approved_is_coerced_to_a_real_bool(self):
        # A truthy non-bool would still send, but the backend contract is bool.
        assert dialogs.build_payload("cfm_1", 1)["approved"] is True
        assert dialogs.build_payload("cfm_1", 0)["approved"] is False


class TestRiskReading:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("low", "low"),
            ("safe", "low"),
            ("medium", "medium"),
            ("confirm", "medium"),
            ("moderate", "medium"),
            ("warning", "medium"),
            ("high", "high"),
            ("dangerous", "high"),
            (None, "high"),
            ("", "high"),
            ("mystery", "high"),
        ],
    )
    def test_levels(self, raw, expected):
        assert dialogs.risk_level({"level": raw}) == expected

    def test_unknown_risk_is_treated_as_high(self):
        # Under-selling a request whose risk we do not understand is the one
        # failure mode a confirmation dialog must not have.
        assert dialogs.risk_accent(dialogs.risk_level({"level": "??"})) == dialogs.theme.RED

    def test_low_is_green_and_medium_is_amber(self):
        assert dialogs.risk_accent("low") == dialogs.theme.GREEN
        assert dialogs.risk_accent("medium") == dialogs.theme.AMBER


class TestRenderingHelpers:
    def test_command_text_prefers_an_explicit_command(self):
        assert dialogs.command_text({"command": "rm -rf build"}) == "rm -rf build"

    def test_command_text_renders_action_plus_params(self):
        text = dialogs.command_text({"action": "file_write", "params": {"path": "a.txt"}})
        assert text.startswith("file_write")
        assert "a.txt" in text

    def test_command_text_without_params_is_the_action(self):
        assert dialogs.command_text({"action": "get_time"}) == "get_time"

    def test_command_text_tolerates_junk_params(self):
        # Non-serialisable params must not raise inside the dialog.
        text = dialogs.command_text({"action": "x", "params": {"f": object()}})
        assert "x" in text

    def test_directory_from_params_then_payload(self):
        assert dialogs.directory_of({"params": {"cwd": "/tmp/work"}}) == "/tmp/work"
        assert dialogs.directory_of({"directory": "/tmp/other"}) == "/tmp/other"
        assert dialogs.directory_of({}) == ""

    def test_reasons_are_normalised_to_a_clean_list(self):
        assert dialogs.reasons_of({"reasons": [" a ", "", "b"]}) == ["a", "b"]
        assert dialogs.reasons_of({"reasons": "single"}) == ["single"]
        assert dialogs.reasons_of({}) == []

    def test_summary_falls_back_through_the_known_keys(self):
        assert dialogs.summary_of({"summary": "s"}) == "s"
        assert dialogs.summary_of({"text": "t"}) == "t"
        assert dialogs.summary_of({"action": "a"}) == "a"
        assert dialogs.summary_of({}) == ""


class TestConfirmationState:
    def test_active_only_with_a_request_id(self):
        assert dialogs.ConfirmationState(sample()).active is True
        assert dialogs.ConfirmationState({"request_id": ""}).active is False
        assert dialogs.ConfirmationState({}).active is False

    def test_allow_sends_one_payload(self):
        state = dialogs.ConfirmationState(sample())
        assert state.allow() == dialogs.build_payload("cfm_01H8ZQ", True)
        assert state.active is False

    def test_cancel_denies(self):
        state = dialogs.ConfirmationState(sample())
        assert state.cancel() == dialogs.build_payload("cfm_01H8ZQ", False)

    def test_only_one_decision_ever_leaves_the_dialog(self):
        state = dialogs.ConfirmationState(sample())
        first = state.allow()
        assert first is not None
        # A double click, a stray second keypress — nothing more may be sent.
        assert state.cancel() is None
        assert state.allow() is None
        assert first["approved"] is True

    def test_resolved_elsewhere_suppresses_the_answer(self):
        state = dialogs.ConfirmationState(sample())
        state.mark_resolved()
        assert state.active is False
        assert state.allow() is None
        assert state.cancel() is None

    def test_level_is_read_from_the_payload(self):
        assert dialogs.ConfirmationState(sample(level="high")).level == "high"

    def test_payload_is_copied_not_aliased(self):
        original = sample()
        state = dialogs.ConfirmationState(original)
        original["level"] = "low"
        assert state.level == "medium"


class TestSettingsRows:
    def test_rows_are_pure_configuration(self):
        class Cfg:
            toggle_hotkey = "ctrl+space"
            expand_hotkey = "alt+z"
            backend_addr = "127.0.0.1:8000"
            ws_url = "ws://127.0.0.1:8000/ws"
            manage_backend = True
            repo_root = "/repo"

        rows = dialogs.settings_rows(Cfg())
        keys = [key for key, _value in rows]
        assert "TOGGLE HOTKEY" in keys
        assert "BACKEND" in keys
        assert "MANAGES BACKEND" in keys
        values = dict(rows)
        assert values["MANAGES BACKEND"] == "yes"
        assert values["THEME DENSITY"] in ("normal", "compact")

    def test_missing_attributes_do_not_raise(self):
        rows = dialogs.settings_rows(object())
        assert rows, "an empty settings view is useless"
