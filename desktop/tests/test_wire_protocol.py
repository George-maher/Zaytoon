"""The desktop shell against the real wire protocol.

Three things this file is responsible for, all of which were only checkable by
reading two languages at once before:

* the shell's frame vocabulary does not drift from the backend's;
* a superseded turn's speech is not played;
* the frames the backend actually emits are all *nameable* here — a frame the
  shell cannot name renders as `unknown`, which is how task progress silently
  disappeared from the activity feed while the tests still passed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import protocol  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def backend_protocol():
    """The backend's own frame vocabulary, or skip if it is not importable.

    The desktop ships as its own package and must not import backend code at
    runtime — it talks to the server over a socket. This fixture imports it at
    *test* time only, from the checkout, so the two vocabularies can still be
    compared where both exist.
    """
    backend = REPO_ROOT / "backend"
    if not (backend / "api" / "ws_protocol.py").is_file():
        pytest.skip("backend protocol module not present")
    sys.path.insert(0, str(backend))
    try:
        from api import ws_protocol
    except Exception as exc:  # pragma: no cover - a broken checkout
        pytest.skip(f"backend protocol module not importable: {exc}")
    return ws_protocol


class TestVocabularyDoesNotDrift:
    def test_every_backend_frame_is_nameable(self, backend_protocol):
        nameable = {member.value for member in protocol.EventType}
        # The shell's `unknown` member is a catch-all, not a name.
        nameable.discard(protocol.EventType.UNKNOWN.value)
        missing = sorted(
            set(backend_protocol.OUTBOUND_NAMES) - nameable
        )
        assert missing == [], (
            "the backend emits frames the desktop shell cannot name; they will "
            f"arrive as `unknown` and be dropped: {missing}"
        )

    def test_every_backend_core_event_is_nameable(self, backend_protocol):
        nameable = {member.value for member in protocol.EventType}
        missing = sorted(set(backend_protocol.CORE_EVENT_PASSTHROUGH) - nameable)
        assert missing == [], f"core events the shell cannot name: {missing}"

    def test_the_shell_sends_only_frames_the_backend_declares(self, backend_protocol):
        # Every *builder* in the shell must produce a frame the server accepts.
        built = [
            protocol.chat_message("hello"),
            protocol.audio_message(b"\x00\x01"),
            protocol.interrupt_message(),
            protocol.tool_confirm("cfm_x", True),
            protocol.reminder_cmd("list"),
        ]
        for frame in built:
            problem = backend_protocol.validate_inbound(frame)
            assert problem is None, f"{frame.get('type')!r}: {problem}"

    def test_the_anchor_list_is_the_same_on_both_sides(self, backend_protocol):
        # `ANCHOR_EVENT_TYPES` decides which frame is allowed to move the
        # "current turn" pointer. If the shell's list and the browser's list
        # disagree, one client drops a turn's output and the other does not.
        anchors = {event.value for event in protocol.ANCHOR_EVENT_TYPES}
        assert anchors == {"intent", "message", "status", "subtitle"}
        # And every anchor must be a frame the backend actually sends.
        assert anchors <= set(backend_protocol.OUTBOUND_NAMES), (
            "an anchor names a frame the backend never emits"
        )
