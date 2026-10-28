"""Explicit voice-session state machine.

The microphone path used to be an informal collection of booleans
(``awaiting_command``, ``last_active_ts``, ``is_manual_voice``) spread between
the WebSocket handler and the browser hook. That is why two behaviours were
possible and neither was written down:

* a recording that ended while a command was still being processed could
  cancel the command (the handler cancelled ``state.audio_task`` whenever a
  newer recording arrived), and
* an arbitrary utterance could execute merely because voice mode was on (the
  browser attached ``isManualVoice: true`` to every VAD segment, and the backend
  read that flag as "the wake-word gate does not apply").

This module gives the server one owner for the voice lifecycle:

    DISABLED -> STANDBY -> LISTENING -> PROCESSING -> SPEAKING -> STANDBY
                              |               |
                              |               +-> AWAITING_CONFIRMATION -> PROCESSING
                              +-> STANDBY                  (+-> SPEAKING)

``RECOVERING`` is reachable from any working state and only ever leads back to
``STANDBY`` (or ``DISABLED``). The transitions are declared, not implied, so an
illegal move is refused and logged instead of silently leaving the UI claiming
one thing while the pipeline does another.

The wake word remains a *gate*, never an authorisation: the tool registry's
permission tiers and the ``ConfirmationManager`` are untouched by this module.
"""

from __future__ import annotations

import logging
import time
from enum import Enum
from typing import Optional

logger = logging.getLogger("Zaytoon.VoiceSession")


class VoiceState(str, Enum):
    """The seven states the voice pipeline can be in."""

    DISABLED = "disabled"
    STANDBY = "standby"
    LISTENING = "listening"
    PROCESSING = "processing"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    SPEAKING = "speaking"
    RECOVERING = "recovering"


#: Legal destinations from each state. ``DISABLED -> LISTENING`` exists for an
#: explicit one-shot capture (a push-to-talk button, or the legacy
#: ``isManualVoice`` path); continuous voice mode always goes through STANDBY.
LEGAL_TRANSITIONS: dict[VoiceState, frozenset[VoiceState]] = {
    VoiceState.DISABLED: frozenset(
        {VoiceState.STANDBY, VoiceState.LISTENING, VoiceState.RECOVERING}
    ),
    VoiceState.STANDBY: frozenset(
        {
            VoiceState.LISTENING,
            VoiceState.PROCESSING,
            VoiceState.SPEAKING,
            VoiceState.DISABLED,
            VoiceState.RECOVERING,
        }
    ),
    VoiceState.LISTENING: frozenset(
        {
            VoiceState.PROCESSING,
            VoiceState.SPEAKING,
            VoiceState.STANDBY,
            VoiceState.DISABLED,
            VoiceState.RECOVERING,
        }
    ),
    VoiceState.PROCESSING: frozenset(
        {
            VoiceState.AWAITING_CONFIRMATION,
            VoiceState.SPEAKING,
            VoiceState.STANDBY,
            VoiceState.DISABLED,
            VoiceState.RECOVERING,
        }
    ),
    VoiceState.AWAITING_CONFIRMATION: frozenset(
        {
            VoiceState.PROCESSING,
            VoiceState.SPEAKING,
            VoiceState.STANDBY,
            VoiceState.DISABLED,
            VoiceState.RECOVERING,
        }
    ),
    VoiceState.SPEAKING: frozenset(
        {
            VoiceState.STANDBY,
            VoiceState.PROCESSING,
            VoiceState.DISABLED,
            VoiceState.RECOVERING,
        }
    ),
    VoiceState.RECOVERING: frozenset(
        {
            VoiceState.STANDBY,
            VoiceState.PROCESSING,
            VoiceState.LISTENING,
            VoiceState.DISABLED,
        }
    ),
}

#: States in which the microphone is expected to be collecting audio. The
#: browser mirrors this set in ``lib/voiceState.js``; keeping the rule in one
#: named place is what stops "stop recording" from also meaning "cancel".
CAPTURING_STATES = frozenset({VoiceState.STANDBY, VoiceState.LISTENING})

#: A command is accepted but not finished. Nothing in this set may be cancelled
#: by a recording boundary, a decode error, or an overlapping microphone frame.
ACTIVE_COMMAND_STATES = frozenset(
    {
        VoiceState.PROCESSING,
        VoiceState.AWAITING_CONFIRMATION,
        VoiceState.SPEAKING,
    }
)


class VoiceSession:
    """Per-WebSocket voice state, wake arming and utterance idempotency.

    One instance belongs to one ``SessionState`` and therefore to one socket.
    ``awaiting_command``/``last_active_ts`` live here rather than on
    ``SessionState`` because there is exactly one thing they mean: "the wake
    word was said alone, and the next utterance is its command". Keeping a
    second copy is how a typed turn used to arm voice mode for ten seconds.
    """

    def __init__(self) -> None:
        self.state: VoiceState = VoiceState.DISABLED
        self.reason: str = ""
        self.enabled: bool = False
        #: The wake word was spoken without a command; the next utterance is
        #: accepted without repeating it, for at most ``WAKE_SESSION_TIMEOUT``.
        self.awaiting_command: bool = False
        #: When the wake word was heard. Zero when not armed — deliberately not
        #: "when the last turn ended", which made any typed answer arm voice.
        self.last_active_ts: float = 0.0
        #: The utterance currently accepted. Stamped on frames so a client can
        #: tie a state change to the command it belongs to.
        self.utterance_id: str = ""
        self.response_id: str = ""
        #: Utterance ids already accepted by *this* socket, with the monotonic
        #: stamp used for expiry. Bounded by ``UTTERANCE_TTL_S``/``MAX``.
        self._seen: dict[str, float] = {}

    # -- transitions -------------------------------------------------------
    def can_transition(self, target: VoiceState) -> bool:
        return target in LEGAL_TRANSITIONS.get(self.state, frozenset())

    def transition(self, target: VoiceState, reason: str = "") -> bool:
        """Move to ``target`` when legal; refuse and log when it is not.

        Refusing rather than coercing is the point: a UI that says LISTENING
        while the pipeline is PROCESSING is exactly the class of bug this
        module exists to make impossible, and silently accepting the move would
        hide it again.
        """
        if target is self.state:
            self.reason = reason or self.reason
            return True
        if not self.can_transition(target):
            logger.info(
                "voice transition refused: %s -> %s (%s)",
                self.state.value, target.value, reason or "no reason",
            )
            return False
        logger.debug(
            "voice transition: %s -> %s (%s)",
            self.state.value, target.value, reason or "no reason",
        )
        self.state = target
        self.reason = reason
        return True

    # -- lifecycle ---------------------------------------------------------
    def enable(self) -> None:
        """Voice mode on. A running turn keeps its state; a dead one stands by."""
        self.enabled = True
        if self.state in (VoiceState.DISABLED, VoiceState.RECOVERING):
            self.transition(VoiceState.STANDBY, "enabled")

    def disable(self) -> None:
        """Voice mode off. Never cancels a command that was already accepted."""
        self.enabled = False
        self.awaiting_command = False
        self.last_active_ts = 0.0
        self.transition(VoiceState.DISABLED, "disabled")

    def arm_follow_up(self) -> None:
        """The wake word was heard alone: accept the next utterance as its command."""
        self.awaiting_command = True
        self.last_active_ts = time.time()
        # Listening means "wake word accepted, waiting for the command", which
        # is the one thing the state name is allowed to mean on the server.
        self.transition(VoiceState.LISTENING, "wake_word")

    def consume_follow_up(self) -> None:
        """The command has arrived. A third utterance needs the wake word again."""
        self.awaiting_command = False
        self.last_active_ts = 0.0

    def command_accepted(self, utterance_id: str = "", response_id: str = "") -> None:
        self.utterance_id = str(utterance_id or "")
        self.response_id = str(response_id or "")
        self.consume_follow_up()
        self.transition(VoiceState.PROCESSING, "command_accepted")

    def awaiting_confirmation(self) -> None:
        self.transition(VoiceState.AWAITING_CONFIRMATION, "tool_approval")

    def confirmation_resolved(self, approved: bool) -> None:
        # Approved or denied, the turn continues: the engine hands the refusal
        # back to the model as an observation rather than ending the command.
        self.transition(VoiceState.PROCESSING, "approval_denied" if not approved else "approval_granted")

    def speaking(self) -> None:
        self.transition(VoiceState.SPEAKING, "tts")

    def turn_complete(self) -> None:
        """Back to standby unless the user has explicitly turned voice off."""
        if not self.enabled:
            self.transition(VoiceState.DISABLED, "voice_off")
            return
        self.transition(VoiceState.STANDBY, "turn_complete")

    def cancel(self) -> None:
        """An explicit cancel. The caller has already cancelled the turn."""
        self.awaiting_command = False
        self.last_active_ts = 0.0
        self.transition(VoiceState.STANDBY, "cancelled")

    def recover(self, reason: str) -> None:
        self.transition(VoiceState.RECOVERING, reason)

    def recovered(self) -> None:
        self.transition(VoiceState.STANDBY, "recovered")

    # -- idempotency -------------------------------------------------------
    def saw_utterance(self, utterance_id: str, ttl_s: float = 300.0, cap: int = 200) -> bool:
        """Record ``utterance_id``; True when it had already been seen.

        Per-socket. The cross-reconnect ledger lives in ``remember_utterance``
        because a reconnect deliberately creates a fresh ``SessionState``.
        """
        token = (utterance_id or "")[:64]
        if not token:
            return False
        now = time.monotonic()
        for key, stamp in list(self._seen.items()):
            if now - stamp > ttl_s:
                self._seen.pop(key, None)
        if len(self._seen) >= cap:
            for key, _ in sorted(self._seen.items(), key=lambda kv: kv[1])[
                : len(self._seen) - cap + 1
            ]:
                self._seen.pop(key, None)
        seen = token in self._seen
        self._seen[token] = now
        return seen

    # -- wire shape --------------------------------------------------------
    def to_frame(self) -> dict:
        frame = {
            "type": "voice_state",
            "state": self.state.value,
            "enabled": bool(self.enabled),
        }
        if self.reason:
            frame["reason"] = self.reason
        if self.utterance_id:
            frame["utterance_id"] = self.utterance_id
        if self.response_id:
            frame["response_id"] = self.response_id
        if self.awaiting_command:
            frame["awaiting_command"] = True
        return frame


# ---------------------------------------------------------------------------
# Cross-reconnect idempotency
# ---------------------------------------------------------------------------
#: A command that was accepted must not run twice because a socket reconnected
#: and a client replayed its last frame. Session ids change on reconnect, so
#: this ledger is process-wide, time-bounded and capped. Ids are generated per
#: utterance by the client (`crypto.randomUUID`), so a collision is not a
#: practical concern; an id-less frame is accepted exactly as before.
_SEEN_UTTERANCES: dict[str, float] = {}
_UTTERANCE_TTL_S = 300.0
_UTTERANCE_MAX = 500


def remember_utterance(utterance_id: Optional[str]) -> bool:
    """True when this utterance id was already accepted (possibly pre-reconnect)."""
    if not utterance_id or not isinstance(utterance_id, str):
        return False
    token = utterance_id[:64]
    now = time.monotonic()
    for key, stamp in list(_SEEN_UTTERANCES.items()):
        if now - stamp > _UTTERANCE_TTL_S:
            _SEEN_UTTERANCES.pop(key, None)
    if len(_SEEN_UTTERANCES) >= _UTTERANCE_MAX:
        for key, _ in sorted(_SEEN_UTTERANCES.items(), key=lambda kv: kv[1])[
            : len(_SEEN_UTTERANCES) - _UTTERANCE_MAX + 1
        ]:
            _SEEN_UTTERANCES.pop(key, None)
    if token in _SEEN_UTTERANCES:
        return True
    _SEEN_UTTERANCES[token] = now
    return False


def reset_utterance_ledger() -> None:
    """Test/teardown seam: forget every accepted utterance id."""
    _SEEN_UTTERANCES.clear()


__all__ = [
    "VoiceState",
    "VoiceSession",
    "LEGAL_TRANSITIONS",
    "CAPTURING_STATES",
    "ACTIVE_COMMAND_STATES",
    "remember_utterance",
    "reset_utterance_ledger",
]
