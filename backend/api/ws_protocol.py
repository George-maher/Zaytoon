"""The WebSocket wire protocol, in one place.

Why this module exists
----------------------
The frame vocabulary used to live only in the shape of ``if``/``elif`` branches
in ``api/websocket.py`` and a ``switch`` in ``frontend/src/hooks/useWebSocket.js``.
Two consequences, both of which had already happened:

* nothing could check the two sides against each other, so a frame type the
  backend stopped sending (or started sending) was only discoverable by reading
  both files carefully;
* a purely cosmetic typo — ``"stt_result"`` written ``"stt-result"`` at one call
  site — is valid Python, serialises fine, and is dropped by the client's
  ``default`` branch in silence. There is no error at either end.

So the vocabulary is declared here, and the *tests* are what enforce it: every
frame the handler emits is looked up in :data:`OUTBOUND`, and every client frame
listed in :data:`INBOUND` is checked for its required fields. This is a contract
document that fails CI when it drifts, which is the only kind that stays true.

Deliberately not a runtime validator
------------------------------------
:func:`validate_outbound` is cheap and is called on the send path, but a
mismatch *logs* rather than raises: a protocol bug should never be able to drop
a user's reply, and the test suite is where it must be caught. Inbound frames
are not rejected for an unknown type either — the handler logs and ignores
them, because an older client that sends a frame a newer server dropped should
degrade, not disconnect mid-conversation.

Not a serialisation frame
-------------------------
Everything is JSON with a flat envelope: ``{"type": ..., ...fields}``. There is
no envelope nesting (no ``{"payload": {...}}``) because both clients already
read the flattened form and the core event relay (``CoreEvent.to_dict``) is
flattened to match it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Optional, Tuple

logger = logging.getLogger("Zaytoon.WSProtocol")


@dataclass(frozen=True)
class FrameSpec:
    """One frame type: who may send it, and what has to be in it.

    ``required`` is a tuple of field names that must be present for the frame to
    mean anything. A frame carrying a field nobody reads is a warning sign, not
    an error — the payloads are assembled from several call sites and some
    fields are for humans reading a log.
    """

    type: str
    direction: str  # "in" | "out"
    required: Tuple[str, ...] = ()
    optional: Tuple[str, ...] = ()
    note: str = ""

    def missing(self, frame: Dict) -> Tuple[str, ...]:
        """Fields this frame needs but does not carry."""
        return tuple(name for name in self.required if frame.get(name) is None)


def _specs(*frames: FrameSpec) -> Dict[str, FrameSpec]:
    return {frame.type: frame for frame in frames}


# --------------------------------------------------------------------------
# Inbound — what a client may send. This is the complete list; the handler's
# branches and this table are the same set, and `test_ws_protocol.py` asserts
# it by driving each type through the handler.
# --------------------------------------------------------------------------
INBOUND: Dict[str, FrameSpec] = _specs(
    FrameSpec(
        "message", "in", required=("message",),
        optional=("language", "client_msg_id"),
        note="the only way to start a turn; every other frame is either control "
             "or a reply to something the server asked for",
    ),
    FrameSpec(
        "audio", "in", required=("audio",),
        optional=("language", "isManualVoice"),
        note="base64 audio; refused above MAX_AUDIO_FRAME_B64 before decoding. "
             "The client's key is `audio`, not `data`",
    ),
    FrameSpec("interrupt", "in", note="cancels the in-flight turn"),
    FrameSpec(
        "tool_confirm", "in", required=("request_id", "approved"),
        note="approval for one specific pending request. The *id* is the "
             "binding: it must be the id the server sent in `require_"
             "confirmation`, so an approval cannot be replayed onto a later "
             "request for the same tool",
    ),
    FrameSpec("task", "in",
              note="stop this session's running task; no fields, the session "
                   "is the subject"),
    FrameSpec("terminal", "in", required=("session_id",),
              note="subscribe this socket to one terminal's output"),
    FrameSpec("reminder_cmd", "in", required=("action",),
              optional=("id", "language"),
              note="list | create | delete | done"),
    FrameSpec("ping", "in", note="liveness; the reply is `pong`"),
)

# --------------------------------------------------------------------------
# Outbound — what the server may send. Core-bus events (`task.*`, `file.changed`,
# `terminal.output`, `model.*`, `tool.*` …) are *not* listed individually: they
# are relayed verbatim from `core.events.CoreEventType`, and adding one there
# must not require editing this table. `CORE_EVENT_PASSTHROUGH` marks that
# allowance explicitly rather than leaving it as an unexplained exception.
# --------------------------------------------------------------------------
OUTBOUND: Dict[str, FrameSpec] = _specs(
    FrameSpec("status", "out", required=("content",),
              optional=("session_id", "principal", "scope", "authenticated",
                        "response_id", "trace_id"),
              note="the state machine the UI draws its ring from"),
    FrameSpec("token", "out", required=("content",),
              optional=("response_id", "trace_id")),
    FrameSpec("message", "out", required=("content",),
              optional=("response_id", "trace_id", "sources", "language")),
    FrameSpec("intent", "out", required=("intent",), optional=("response_id", "trace_id")),
    FrameSpec("plan", "out", optional=("steps",)),
    FrameSpec("stt_result", "out", optional=("language", "voice", "duration"),
              note="emitted only when the spoken language is known"),
    FrameSpec("wake_word", "out", optional=("detected",)),
    FrameSpec("subtitle", "out", required=("content",), optional=("response_id",)),
    FrameSpec("audio_start", "out", optional=("response_id", "voice", "mime", "rate")),
    FrameSpec("audio_chunk", "out", required=("data",), optional=("response_id",)),
    FrameSpec("audio_end", "out", optional=("response_id", "reason")),
    FrameSpec("sources", "out"),
    FrameSpec("tool_started", "out", optional=("name", "label", "args")),
    FrameSpec("tool_progress", "out", optional=("name", "message")),
    FrameSpec("tool_finished", "out", optional=("name", "success", "summary", "output")),
    FrameSpec("confirmation_result", "out",
              optional=("request_id", "approved", "resolved")),
    FrameSpec("require_confirmation", "out", required=("request_id",),
              optional=("action", "params", "level", "permission", "reasons",
                        "summary", "text", "session_id"),
              note="fields come from PendingConfirmation.to_event()"),
    FrameSpec("interrupted", "out", optional=("response_id", "reason")),
    FrameSpec("task_result", "out",
              optional=("task_id", "status", "result", "cancelled", "error")),
    FrameSpec("terminal_output", "out", optional=("terminal_id", "data")),
    FrameSpec("trace", "out", optional=("trace_id", "status")),
    FrameSpec("reminder_list", "out"), FrameSpec("reminder", "out"),
    FrameSpec("reminder_deleted", "out"), FrameSpec("reminder_done", "out"),
    FrameSpec("pong", "out"),
    FrameSpec("error", "out", required=("content",), optional=("code", "detail")),
)

#: Frame types that arrive from the core bus and keep their dotted names.
CORE_EVENT_PASSTHROUGH: FrozenSet[str] = frozenset({
    "task.created", "task.updated", "task.step", "task.log", "task.completed",
    "file.changed", "terminal.output",
    "tool.started", "tool.output", "tool.completed", "tool.failed",
    "model.started", "model.token", "model.completed", "model.failed",
    "permission.requested", "permission.resolved", "error",
    "skill.installed", "skill.removed", "provider.changed", "reminder.fired",
    "memory.compacted", "session.cleaned",
})

#: Frames that exist for exactly one direction. Guards against the classic
#: copy-paste where the server starts emitting a client's frame type.
INBOUND_NAMES = frozenset(INBOUND)
OUTBOUND_NAMES = frozenset(OUTBOUND)

#: Frame types the *transport* owns end-to-end; both sides may send them.
_BIDIRECTIONAL = frozenset({"interrupt", "ping"})


def validate_outbound(frame: Dict) -> Optional[str]:
    """Return a description of why ``frame`` looks wrong, or ``None`` if it is fine.

    Called on the send path with the frame about to go out. Logs, never raises —
    see the module docstring for why.
    """
    if not isinstance(frame, dict):
        return f"outbound frame is {type(frame).__name__}, not a dict"
    kind = frame.get("type")
    if not kind:
        return "outbound frame has no `type`"
    if kind in CORE_EVENT_PASSTHROUGH:
        return None
    spec = OUTBOUND.get(kind)
    if spec is None:
        if kind in _BIDIRECTIONAL:
            # `interrupt`/`ping` are owned by the transport and either side may
            # originate them; their field requirements come from whichever
            # table declares them.
            return None
        if kind in INBOUND_NAMES:
            return f"{kind!r} is an inbound frame type; the server must not send it"
        return f"unknown outbound frame type {kind!r}"
    missing = spec.missing(frame)
    if missing:
        return f"{kind!r} is missing required field(s): {', '.join(missing)}"
    return None


def validate_inbound(frame: Dict) -> Optional[str]:
    """Return a description of why a *client* frame looks wrong, or ``None``.

    A frame with an unknown type is not an error: it is reported so the handler
    can log it, and the handler then ignores it. Unknown ``type`` values are how
    two deployed versions of a client and a server coexist.
    """
    if not isinstance(frame, dict):
        return "inbound frame is not a JSON object"
    kind = frame.get("type")
    if not kind:
        return "inbound frame has no `type`"
    spec = INBOUND.get(kind)
    if spec is None:
        return f"unknown inbound frame type {kind!r}"
    missing = spec.missing(frame)
    if missing:
        return f"{kind!r} is missing required field(s): {', '.join(missing)}"
    return None


def describe() -> Dict[str, Dict[str, Dict]]:
    """The protocol as data, for a diagnostics endpoint and for tests."""
    def _table(specs: Dict[str, FrameSpec]) -> Dict[str, Dict]:
        return {
            name: {"required": list(spec.required), "optional": list(spec.optional),
                   "note": spec.note}
            for name, spec in sorted(specs.items())
        }

    return {
        "inbound": _table(INBOUND),
        "outbound": _table(OUTBOUND),
        "core_events": {name: {} for name in sorted(CORE_EVENT_PASSTHROUGH)},
    }


__all__ = [
    "FrameSpec",
    "INBOUND",
    "OUTBOUND",
    "INBOUND_NAMES",
    "OUTBOUND_NAMES",
    "CORE_EVENT_PASSTHROUGH",
    "validate_inbound",
    "validate_outbound",
    "describe",
]
