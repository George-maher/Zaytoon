"""Reminder round trip: the shell could display reminders but not act on them.

The desktop handled the inbound reminder events yet had no outbound builder, so
the panel was read-only -- clearing a reminder meant switching to the browser
app. These tests pin the outbound half and the honesty of the inbound flag.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import protocol  # noqa: E402


class TestOutboundReminderCommand:
    def test_list_carries_no_id(self):
        assert protocol.reminder_cmd("list") == {"type": "reminder_cmd", "action": "list"}

    @pytest.mark.parametrize("action", ["delete", "done"])
    def test_mutations_carry_an_integer_id(self, action):
        payload = protocol.reminder_cmd(action, 7)
        assert payload == {"type": "reminder_cmd", "action": action, "id": 7}
        assert isinstance(payload["id"], int)

    def test_string_ids_are_normalised(self):
        # The panel may hand back an id parsed from JSON text.
        assert protocol.reminder_cmd("done", "12")["id"] == 12

    def test_mutations_require_an_id(self):
        for action in ("delete", "done"):
            with pytest.raises(ValueError, match="requires a reminder id"):
                protocol.reminder_cmd(action)

    def test_unknown_actions_are_rejected(self):
        # A typo must not reach the backend as a silent no-op.
        with pytest.raises(ValueError, match="unknown reminder action"):
            protocol.reminder_cmd("destroy", 1)

    def test_actions_match_the_backend_handler(self):
        # The backend dispatches on exactly "list" / "delete" / "done"; this
        # tuple is the contract, so a drift shows up here rather than at runtime.
        assert protocol.REMINDER_ACTIONS == ("list", "delete", "done")


class TestInboundReminderDecoding:
    @pytest.mark.parametrize(
        "frame,expected",
        [
            ({"type": "reminder", "id": 1, "text": "stand up"}, protocol.EventType.REMINDER),
            ({"type": "reminder_list", "reminders": []}, protocol.EventType.REMINDER_LIST),
            ({"type": "reminder_done", "id": 1, "success": True}, protocol.EventType.REMINDER_DONE),
            ({"type": "reminder_deleted", "id": 1, "success": True}, protocol.EventType.REMINDER_DELETED),
        ],
    )
    def test_frames_decode(self, frame, expected):
        import json

        event = protocol.ServerEvent.parse(json.dumps(frame))
        assert event is not None
        assert event.type is expected

    def test_success_false_is_visible_to_the_panel(self):
        """The panel must be able to tell a real deletion from a no-op."""
        import json

        event = protocol.ServerEvent.parse(
            json.dumps({"type": "reminder_deleted", "id": 5, "success": False,
                        "message": "No such reminder."})
        )
        assert event.data["success"] is False
        assert event.data["message"] == "No such reminder."


# The backend half of this round trip -- MemoryAgent reporting an honest
# delete/complete result -- lives in backend/tests/test_reminder_honesty.py,
# because `agents` is a backend package and the desktop shell cannot import it.
