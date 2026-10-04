"""One user action must produce exactly one logical turn.

The web client stamps every outbound message with a ``client_msg_id``. If the
same frame arrives twice — a double submit, a reconnect replaying its last
message, a socket library redelivering — the second copy must be dropped
*before* it can cancel the in-flight turn, allocate a trace, or reach the
router. Content-based duplicate detection cannot do this job: the resend arrives
long before the first turn has produced any content to compare against, so it
is the id, not the text, that identifies a retry.

The compatibility half matters just as much: a client that sends no id — the
desktop shell does not — must behave exactly as before.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import websocket  # noqa: E402


@pytest.fixture(autouse=True)
def clean_ids():
    websocket._SEEN_MESSAGE_IDS.clear()
    yield
    websocket._SEEN_MESSAGE_IDS.clear()


class TestIdDeduplication:
    def test_a_first_time_id_is_accepted(self):
        assert websocket.is_replayed_message("s1", "abc") is False

    def test_the_same_id_twice_is_a_replay(self):
        assert websocket.is_replayed_message("s1", "abc") is False
        assert websocket.is_replayed_message("s1", "abc") is True

    def test_different_ids_are_both_accepted(self):
        assert websocket.is_replayed_message("s1", "a") is False
        assert websocket.is_replayed_message("s1", "b") is False

    def test_the_scope_is_one_session(self):
        """Two clients may legitimately send the same id; they are not retries."""
        assert websocket.is_replayed_message("s1", "shared") is False
        assert websocket.is_replayed_message("s2", "shared") is False

    def test_no_id_is_never_a_replay(self):
        """A client that does not participate keeps working exactly as before."""
        for _ in range(5):
            assert websocket.is_replayed_message("s1", None) is False
        assert websocket.is_replayed_message("s1", "") is False

    def test_a_non_string_id_is_ignored_rather_than_crashing(self):
        assert websocket.is_replayed_message("s1", 12345) is False
        assert websocket.is_replayed_message("s1", {"a": 1}) is False

    def test_an_absurdly_long_id_is_bounded(self):
        """The id is truncated before it is used as a key, so a client cannot
        grow the table with megabyte-long strings."""
        huge = "x" * 100_000
        assert websocket.is_replayed_message("s1", huge) is False
        assert websocket.is_replayed_message("s1", huge) is True
        key = next(iter(websocket._SEEN_MESSAGE_IDS["s1"]))
        assert len(key) == 64

    def test_an_expired_id_is_accepted_again(self, monkeypatch):
        """A resend after the window is a new request, not a retry."""
        now = [1000.0]
        monkeypatch.setattr(websocket.time, "monotonic", lambda: now[0])
        assert websocket.is_replayed_message("s1", "abc") is False
        assert websocket.is_replayed_message("s1", "abc") is True
        now[0] += websocket._SEEN_ID_TTL_S + 1
        assert websocket.is_replayed_message("s1", "abc") is False

    def test_the_table_is_bounded(self, monkeypatch):
        """Ids expire and surplus is pruned, so a long session cannot grow it
        without limit."""
        now = [1000.0]
        monkeypatch.setattr(websocket.time, "monotonic", lambda: now[0])
        for i in range(websocket._SEEN_ID_MAX * 3):
            websocket.is_replayed_message("s1", f"id-{i}")
        seen = websocket._SEEN_MESSAGE_IDS["s1"]
        assert len(seen) <= websocket._SEEN_ID_MAX

        # And the TTL alone bounds it: nothing older than the window survives.
        now[0] += websocket._SEEN_ID_TTL_S + 1
        websocket.is_replayed_message("s1", "trigger-prune")
        assert len(websocket._SEEN_MESSAGE_IDS["s1"]) == 1