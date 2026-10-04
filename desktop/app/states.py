"""One pure mapping from "everything we know" to what the UI should show.

The shell has three consumers of state — the header pill, the reactive core/orb
and the bottom status line — and previously each of them re-derived its answer
from a raw status string, so they could disagree with each other. This module
is the single source: :func:`derive` takes a snapshot of the inputs
(transport connection, ``status`` frames, the routed intent, the running tool,
audio activity and the last error) and returns a :class:`UiState` plus the
human label that goes with it.

Everything here is pure. No Tk, no sockets, no time — which is what makes the
whole state machine testable without opening a window.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from . import protocol

#: Connection values the transport reports. ``reconnecting`` is a real state
#: (the socket is retrying) and is deliberately distinct from ``offline``
#: (nothing is listening), because the header pill colours them differently.
CONNECTION_CONNECTED = "connected"
CONNECTION_RECONNECTING = "reconnecting"
CONNECTION_OFFLINE = "offline"


class UiState(str, Enum):
    """The nine states the command center can be in."""

    OFFLINE = "offline"
    IDLE = "idle"
    LISTENING = "listening"
    UNDERSTANDING = "understanding"
    PLANNING = "planning"
    EXECUTING = "executing"
    SPEAKING = "speaking"
    AWAITING_CONFIRM = "awaiting_confirm"
    ERROR = "error"


#: States that draw a static frame and must **not** run an ``after()`` loop.
#: The core and the floating orb stop their animation entirely here — an idle
#: assistant must not cost a permanent 30fps timer.
STATIC_STATES = frozenset({UiState.OFFLINE, UiState.IDLE})

#: UiState -> the status vocabulary the shared HUD (``theme.STATES``) uses, so
#: the orb's colours, glyphs and sweep rates keep working unchanged.
ORB_STATUS: dict[UiState, str] = {
    UiState.OFFLINE: "offline",
    UiState.IDLE: "idle",
    UiState.LISTENING: "listening",
    UiState.UNDERSTANDING: "thinking",
    UiState.PLANNING: "planning",
    UiState.EXECUTING: "executing",
    UiState.SPEAKING: "speaking",
    UiState.AWAITING_CONFIRM: "awaiting_confirmation",
    UiState.ERROR: "error",
}

#: Accent per state — the pill, the status line and the core all read it here.
STATE_ACCENT: dict[UiState, str] = {
    UiState.OFFLINE: "red",
    UiState.IDLE: "cyan",
    UiState.LISTENING: "green",
    UiState.UNDERSTANDING: "cyan",
    UiState.PLANNING: "violet",
    UiState.EXECUTING: "cyan",
    UiState.SPEAKING: "cyan",
    UiState.AWAITING_CONFIRM: "amber",
    UiState.ERROR: "red",
}

#: A non-colour channel for every state, so the UI is never colour-only.
STATE_GLYPH: dict[UiState, str] = {
    UiState.OFFLINE: "○",
    UiState.IDLE: "●",
    UiState.LISTENING: "◉",
    UiState.UNDERSTANDING: "◌",
    UiState.PLANNING: "◇",
    UiState.EXECUTING: "▲",
    UiState.SPEAKING: "▶",
    UiState.AWAITING_CONFIRM: "!",
    UiState.ERROR: "✕",
}


@dataclass(frozen=True)
class Inputs:
    """The snapshot :func:`derive` reads. Pure data, so it is trivial to build
    one in a test."""

    connection: str = CONNECTION_CONNECTED
    #: Latest ``status`` frame, already normalised by the protocol layer.
    status: str = ""
    #: Latest ``intent`` frame (router output: chat / code / tools / ...).
    intent: str = ""
    #: Name of the tool currently running, or ``None`` when none is.
    tool: str | None = None
    #: True while ``token`` frames are arriving.
    streaming: bool = False
    #: True while TTS audio is actually playing.
    speaking: bool = False
    #: True while the local microphone is open.
    listening: bool = False
    #: The pending ``require_confirmation`` request id, if any.
    confirm_request: str | None = None
    #: The last error text that has not been superseded by new activity.
    error: str = ""

    def updated(self, **changes) -> "Inputs":
        return replace(self, **changes)


def label(state: UiState, tool: str | None = None, language: str = "en") -> str:
    """Human label for a state, bilingual like the rest of the shell."""
    arabic = language == "ar"
    name = tool or "tool"
    table: dict[UiState, tuple[str, str]] = {
        UiState.OFFLINE: ("Offline", "غير متصل"),
        UiState.IDLE: ("Ready", "جاهز"),
        UiState.LISTENING: ("Listening…", "بستمع…"),
        UiState.UNDERSTANDING: ("Understanding…", "بفهم…"),
        UiState.PLANNING: ("Planning…", "بخطط…"),
        UiState.EXECUTING: (f"Calling tool: {name}…", f"بتنفّذ أمر: {name}…"),
        UiState.SPEAKING: ("Speaking…", "بتكلم…"),
        UiState.AWAITING_CONFIRM: ("Awaiting confirmation…", "في انتظار موافقتك…"),
        UiState.ERROR: ("Error — see the transcript", "خطأ — شوف المحادثة"),
    }
    english, egyptian = table[state]
    return egyptian if arabic else english


def derive(inputs: Inputs) -> tuple[UiState, str]:
    """The whole state machine, in one function.

    Precedence is the design:

    1. **Offline beats everything** — no amount of local activity makes a dead
       transport "ready".
    2. **A pending human decision beats everything else** — if the backend is
       waiting for an Allow/Cancel, that is what the user must see.
    3. Then the turn itself: error, running tool, plan, streamed text, playback,
       router/understanding, listening, and finally idle.

    Returns the state and its English label; callers that need Arabic call
    :func:`label` again with the session language.
    """
    if inputs.connection != CONNECTION_CONNECTED:
        return UiState.OFFLINE, label(UiState.OFFLINE)
    if inputs.confirm_request:
        return UiState.AWAITING_CONFIRM, label(UiState.AWAITING_CONFIRM)
    if inputs.error:
        return UiState.ERROR, label(UiState.ERROR)
    if inputs.tool:
        return UiState.EXECUTING, label(UiState.EXECUTING, inputs.tool)
    if inputs.status == "planning" or inputs.intent == "complex":
        return UiState.PLANNING, label(UiState.PLANNING)
    if inputs.streaming:
        # Tokens on screen: the user is reading an answer being generated, which
        # is a different moment from audio leaving the speaker.
        return UiState.SPEAKING, "Generating response…"
    if inputs.speaking or inputs.status == "speaking":
        return UiState.SPEAKING, label(UiState.SPEAKING)
    if inputs.status in ("thinking", "transcribing") or inputs.intent:
        return UiState.UNDERSTANDING, label(UiState.UNDERSTANDING)
    if inputs.status == "listening" or inputs.listening:
        return UiState.LISTENING, label(UiState.LISTENING)
    return UiState.IDLE, label(UiState.IDLE)


def accent(state: UiState) -> str:
    """Hex accent for a state (via the token names in ``theme.ACCENTS``)."""
    from . import theme  # local import: theme is the heavier module

    return theme.ACCENTS[STATE_ACCENT[state]]


def is_static(state: UiState) -> bool:
    """True when the core/orb must draw once and schedule no further frames."""
    return state in STATIC_STATES


def orb_status(state: UiState) -> str:
    """Map a state onto the shared HUD vocabulary consumed by ``theme``."""
    return ORB_STATUS[state]


#: Raw HUD status string -> UiState. The transport, the backend relay and the
#: web client all speak this vocabulary; :func:`status_to_state` is the single
#: translation into the shell's state machine.
HUD_STATE_MAP: dict[str, UiState] = {
    "disconnected": UiState.OFFLINE,
    "offline": UiState.OFFLINE,
    "connected": UiState.IDLE,
    "idle": UiState.IDLE,
    "ready": UiState.IDLE,
    "done": UiState.IDLE,
    "listening": UiState.LISTENING,
    "transcribing": UiState.UNDERSTANDING,
    "thinking": UiState.UNDERSTANDING,
    "awaiting_command": UiState.UNDERSTANDING,
    "planning": UiState.PLANNING,
    "executing": UiState.EXECUTING,
    "speaking": UiState.SPEAKING,
    "awaiting_confirmation": UiState.AWAITING_CONFIRM,
    "confirming": UiState.AWAITING_CONFIRM,
    "error": UiState.ERROR,
}


def status_to_state(status: str) -> UiState:
    """A raw status string on its own, without the other inputs."""
    mapped = HUD_STATE_MAP.get(str(status))
    if mapped is not None:
        return mapped
    # An unknown status is not an error: fall through the normal precedence so
    # a future server-side state degrades to something sensible.
    return derive(Inputs(status=str(status)))[0]


class StateTracker:
    """Mutable holder the app feeds events into; widgets only read the result.

    It exists so ``app.py`` can fan one event out to the tracker *and* to the
    widgets without either of them having to know the precedence rules.
    """

    def __init__(self) -> None:
        self.inputs = Inputs()

    # -- direct signals ---------------------------------------------------
    def set_connection(self, value: str) -> None:
        self.inputs = self.inputs.updated(connection=value)

    def set_status(self, status: str) -> None:
        changes: dict = {"status": status}
        if status in ("thinking", "planning"):
            # A new turn supersedes whatever the last one ended with.
            changes["error"] = ""
            changes["intent"] = ""
        elif status in ("done", "listening"):
            # The turn is over: leave the *error* visible (it describes the
            # turn that just failed) but stop reporting "understanding" for a
            # router intent nothing is acting on any more.
            changes["intent"] = ""
        self.inputs = self.inputs.updated(**changes)

    def set_intent(self, intent: str) -> None:
        self.inputs = self.inputs.updated(intent=intent, error="")

    def set_mic(self, on: bool) -> None:
        self.inputs = self.inputs.updated(listening=bool(on))

    def set_speaking(self, on: bool) -> None:
        self.inputs = self.inputs.updated(speaking=bool(on))

    def set_streaming(self, on: bool) -> None:
        self.inputs = self.inputs.updated(streaming=bool(on))

    def set_error(self, text: str) -> None:
        self.inputs = self.inputs.updated(error=text or "error", streaming=False)

    def set_tool(self, name: str | None) -> None:
        self.inputs = self.inputs.updated(tool=name, error="")

    def set_confirmation(self, request_id: str | None) -> None:
        self.inputs = self.inputs.updated(confirm_request=request_id)

    def reset_turn(self) -> None:
        """Clear everything that only makes sense inside a turn."""
        self.inputs = self.inputs.updated(
            streaming=False,
            tool=None,
            confirm_request=None,
            error="",
            intent="",
        )

    # -- frames -----------------------------------------------------------
    def on_event(self, event: protocol.ServerEvent) -> None:
        """Fold one server frame into the snapshot."""
        kind = event.type
        if kind is protocol.EventType.CONNECTED:
            self.set_connection(CONNECTION_CONNECTED)
        elif kind is protocol.EventType.DISCONNECTED:
            self.set_connection(CONNECTION_RECONNECTING)
        elif kind is protocol.EventType.STATUS:
            self.set_status(event.status or "thinking")
        elif kind is protocol.EventType.INTENT:
            self.set_intent(str(event.data.get("intent") or ""))
        elif kind is protocol.EventType.TOKEN:
            self.set_streaming(True)
        elif kind in (protocol.EventType.MESSAGE, protocol.EventType.MESSAGE_DONE):
            self.inputs = self.inputs.updated(streaming=False, intent="")
        elif kind is protocol.EventType.ERROR:
            self.inputs = self.inputs.updated(
                error=event.text or "error", streaming=False, intent=""
            )
        elif kind is protocol.EventType.REQUIRE_CONFIRMATION:
            self.set_confirmation(str(event.data.get("request_id") or "") or None)
        elif kind is protocol.EventType.CONFIRMATION_RESULT:
            if event.data.get("resolved", True):
                self.set_confirmation(None)
        elif kind is protocol.EventType.TOOL_STARTED:
            self.set_tool(str(event.data.get("name") or "tool"))
        elif kind is protocol.EventType.TOOL_FINISHED:
            self.set_tool(None)
        elif kind is protocol.EventType.INTERRUPTED:
            self.reset_turn()
        elif kind is protocol.EventType.AUDIO_CHUNK:
            self.set_speaking(True)

    # -- result -----------------------------------------------------------
    def resolve(self, language: str = "en") -> tuple[UiState, str]:
        """The state plus its human label in ``language``.

        ``derive`` computes the English label as part of resolving, and some
        states have a bespoke one (a streaming turn reads "Generating
        response…", not "Speaking…"), so the English path must keep what
        ``derive`` produced rather than re-deriving a generic label. Only the
        Arabic path re-asks the table.
        """
        state, english = derive(self.inputs)
        if language == "ar":
            return state, label(state, self.inputs.tool, "ar")
        return state, english
