"""The Zaytoon WebSocket protocol, as builders and typed events.

Keeping every outbound message shape in one place means the desktop shell and
the backend cannot drift: if the server adds a field, there is exactly one
function to update, and the tests assert on the same builders the app calls.

Client -> server: ``message``, ``audio``, ``interrupt``, ``tool_confirm``.
Server -> client: everything else is surfaced as a :class:`ServerEvent`.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# --- outbound -------------------------------------------------------------


def chat_message(text: str, response_id: str | None = None) -> dict:
    """A typed message. The wake word is *not* prepended: the backend gate
    only applies to the voice path, and a typed command is already deliberate."""
    payload: dict[str, Any] = {"type": "message", "message": text}
    if response_id:
        payload["response_id"] = response_id
    return payload


def audio_message(pcm: bytes, language: str = "") -> dict:
    """A recorded utterance. Each message carries its own base64 padding so a
    chunk can be decoded independently of its neighbours."""
    return {
        "type": "audio",
        "audio": base64.b64encode(pcm).decode("ascii"),
        "language": language,
    }


def interrupt_message() -> dict:
    """Cancel the active response and any TTS playback."""
    return {"type": "interrupt"}


def tool_confirm(request_id: str, approved: bool) -> dict:
    """Answer a ``require_confirmation`` round trip."""
    return {
        "type": "tool_confirm",
        "request_id": request_id,
        "approved": bool(approved),
    }


#: Actions the backend accepts on a ``reminder_cmd``. Kept here so the shell
#: and the service cannot drift: the backend dispatches on exactly these.
REMINDER_ACTIONS = ("list", "delete", "done")


def reminder_cmd(action: str, reminder_id: int | None = None) -> dict:
    """Ask the backend to list, complete, or delete a reminder.

    The shell used to render reminders but had no way to *act* on them: it
    handled the inbound ``reminder``/``reminder_list``/``reminder_done``/
    ``reminder_deleted`` events, yet could not send the command that produces
    the list or changes one, so the panel was read-only and the user had to
    fall back to the browser app to clear a reminder.

    ``id`` is required for ``delete`` and ``done`` and omitted for ``list``,
    matching the backend's handler.
    """
    if action not in REMINDER_ACTIONS:
        raise ValueError(
            f"unknown reminder action {action!r}; expected one of {REMINDER_ACTIONS}"
        )
    payload: dict = {"type": "reminder_cmd", "action": action}
    if action in ("delete", "done"):
        if reminder_id is None:
            raise ValueError(f"reminder action {action!r} requires a reminder id")
        payload["id"] = int(reminder_id)
    return payload


# --- inbound --------------------------------------------------------------


class EventType(str, Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    STATUS = "status"
    INTENT = "intent"
    TOKEN = "token"
    MESSAGE = "message"
    #: Sent by some turns in place of/in addition to ``message``; it carries the
    #: same final content, so it is treated as the end of a stream.
    MESSAGE_DONE = "message_done"
    SUBTITLE = "subtitle"
    PLAN = "plan"
    TOOL_STARTED = "tool_started"
    TOOL_PROGRESS = "tool_progress"
    TOOL_FINISHED = "tool_finished"
    STT_RESULT = "stt_result"
    AUDIO_START = "audio_start"
    AUDIO_CHUNK = "audio_chunk"
    AUDIO_END = "audio_end"
    #: The backend's explicit voice lifecycle state (disabled | standby | ...
    #: speaking | recovering). The shell mirrors this so the UI can show the
    #: voice pipeline's actual state rather than inferring it from audio frames.
    VOICE_STATE = "voice_state"
    #: The backend relays the core bus verbatim, so these arrive with dotted
    #: names. Listed because a frame the shell cannot name is a frame it renders
    #: as `unknown`, and an activity feed that drops task progress is the
    #: difference between "working" and "hung".
    PONG = "pong"
    TASK_RESULT = "task_result"
    TRACE = "trace"
    SOURCES = "sources"
    # -- the core bus, relayed verbatim --------------------------------
    # These arrive with their dotted names exactly as `core.events` publishes
    # them. A frame the shell cannot name is decoded as `unknown` and dropped,
    # so this list has to cover the whole vocabulary — `test_wire_protocol.py`
    # checks it against the backend's `CoreEventType` and fails if one is
    # missing, which is how the activity feed silently lost tool progress before.
    TASK_CREATED = "task.created"
    TASK_UPDATED = "task.updated"
    TASK_STEP = "task.step"
    TASK_LOG = "task.log"
    TOOL_STARTED_STREAM = "tool.started"
    TOOL_OUTPUT = "tool.output"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    MODEL_STARTED = "model.started"
    MODEL_TOKEN = "model.token"
    MODEL_COMPLETED = "model.completed"
    MODEL_FAILED = "model.failed"
    PERMISSION_REQUESTED = "permission.requested"
    PERMISSION_RESOLVED = "permission.resolved"
    FILE_CHANGED = "file.changed"
    PROJECT_CHANGED = "project.changed"
    SKILL_CHANGED = "skill.changed"
    PROVIDER_CHANGED = "provider.changed"
    TERMINAL_OUTPUT = "terminal_output"
    TERMINAL_OUTPUT_EVENT = "terminal.output"
    WAKE_WORD = "wake_word"
    REQUIRE_CONFIRMATION = "require_confirmation"
    CONFIRMATION_RESULT = "confirmation_result"
    INTERRUPTED = "interrupted"
    ERROR = "error"
    REMINDER = "reminder"
    REMINDER_LIST = "reminder_list"
    REMINDER_DELETED = "reminder_deleted"
    REMINDER_DONE = "reminder_done"
    UNKNOWN = "unknown"


#: Statuses the backend reports. Anything else is treated as a generic state so
#: a new server-side status cannot crash the UI.
KNOWN_STATUSES = frozenset(
    {
        "connected",
        "disconnected",
        "thinking",
        "planning",
        "transcribing",
        "speaking",
        "listening",
        "done",
        "awaiting_command",
        "awaiting_confirmation",
    }
)


#: Frames that establish which turn is the current one. The browser client keeps
#: the same list (`ANCHOR_FRAMES` in `useWebSocket.js`) and the server declares
#: the field on every frame it emits (`api/ws_protocol.py`). Payload frames —
#: `token`, `audio_start`, `audio_chunk`, `audio_end` — are absent on purpose:
#: one of them can carry a superseded turn's data, and treating it as an anchor
#: would make the staleness check compare a turn against itself.
ANCHOR_EVENT_TYPES = frozenset(
    {
        EventType.INTENT,
        EventType.MESSAGE,
        EventType.STATUS,
        EventType.SUBTITLE,
    }
)


@dataclass
class ServerEvent:
    """One decoded server frame."""

    type: EventType
    data: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        content = self.data.get("content")
        return content if isinstance(content, str) else ""

    @property
    def trace_id(self) -> str | None:
        """Correlation ID for request tracing."""
        return self.data.get("trace_id")

    @property
    def response_id(self) -> str | None:
        """Response ID for stream correlation."""
        return self.data.get("response_id")

    @classmethod
    def parse(cls, raw: str | bytes) -> "ServerEvent | None":
        """Decode one frame. Returns ``None`` for anything unparseable so a
        malformed frame cannot kill the read loop."""
        import json

        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            event_type = EventType(data.get("type", ""))
        except ValueError:
            return ServerEvent(EventType.UNKNOWN, data)
        return ServerEvent(event_type, data)

    @property
    def status(self) -> str:
        """The normalised status, defaulting to ``connected`` state."""
        if self.type is EventType.STATUS:
            content = self.text
            return content if content in KNOWN_STATUSES else "thinking"
        if self.type is EventType.CONNECTED:
            return "connected"
        return ""

    @property
    def is_final(self) -> bool:
        """True on the frame that completes a streamed reply."""
        return self.type in (EventType.MESSAGE, EventType.MESSAGE_DONE, EventType.ERROR)

    @property
    def is_anchor(self) -> bool:
        """True when this frame names the turn the shell should be following."""
        return bool(self.response_id) and self.type in ANCHOR_EVENT_TYPES

    # -- convenience readers for the activity feed -------------------------
    @property
    def plan_steps(self) -> list[str]:
        """``plan`` frame -> the ordered step list (never raises)."""
        if self.type is not EventType.PLAN:
            return []
        raw = self.data.get("steps")
        if not isinstance(raw, (list, tuple)):
            return []
        return [str(step) for step in raw if str(step).strip()]

    @property
    def tool_name(self) -> str:
        name = self.data.get("name")
        return str(name) if name else "tool"

    @property
    def tool_label(self) -> str:
        """Human label for a tool row — falls back to the tool's own name."""
        text = self.data.get("label") or self.data.get("message") or ""
        return str(text) or self.tool_name
