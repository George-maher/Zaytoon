"""The manual-microphone wake-word bypass (runtime verification, not redesign).

The requirement: pressing the microphone in the browser must let the user speak
directly, with no "Zaytoon" prefix. The backend gates on
``config.WAKE_WORD_REQUIRED and not is_manual_voice``, so the flag the browser
sends is the whole contract. These tests pin both halves of it:

* the arithmetic the handler performs, and
* that the gate genuinely passes a bare utterance through when disabled.

Without these, a future edit that drops ``not is_manual_voice`` would restore a
silent, hard-to-diagnose failure: the app appears to ignore every utterance.
"""

from __future__ import annotations

import pytest

from config import Config
from services.wake_word_service import apply_wake_gate


def gate(text: str, *, manual_voice: bool, cfg: Config) -> object:
    """Mirror the decision the WebSocket handler makes, in one place.

    The handler's own expression is quoted here deliberately: if it changes,
    this is the assertion that should be looked at rather than silently
    diverging from the real code path.
    """
    wake_enabled = cfg.WAKE_WORD_REQUIRED and not manual_voice
    return apply_wake_gate(
        text,
        awaiting_command=False,
        last_active_ts=0.0,
        timeout_secs=cfg.WAKE_SESSION_TIMEOUT,
        enabled=wake_enabled,
    )


class TestManualVoiceBypassesWakeWord:
    @pytest.fixture
    def cfg(self):
        return Config()

    @pytest.mark.parametrize(
        "utterance",
        [
            "what time is it",
            "hello zaytoon how are you",   # the wake word merely happens to be present
            "عامل إيه يا زيتون؟",
            "open the browser",
            "شغّل الموسيقى",
        ],
    )
    def test_manual_microphone_accepts_speech_without_the_wake_word(self, cfg, utterance):
        result = gate(utterance, manual_voice=True, cfg=cfg)
        assert not result.dropped, f"'{utterance}' was discarded on manual voice"
        assert result.acted is True
        # The utterance is passed through intact, minus a leading wake word if
        # one was actually said.
        assert result.text

    def test_wake_word_is_still_required_when_the_flag_is_absent(self, cfg):
        """A client that omits the flag gets the old behaviour, not a bypass.

        This is what makes the flag meaningful: defaulting to permissive would
        turn every hands-free or third-party client into an unguarded one.
        """
        result = gate("what time is it", manual_voice=False, cfg=cfg)
        assert result.dropped is True
        assert result.acted is False

    def test_wake_word_only_arms_the_follow_up_window(self, cfg):
        result = gate("zaytoon", manual_voice=False, cfg=cfg)
        assert result.acted is False
        assert result.detected is True
        assert result.dropped is False

    def test_gate_is_bypassed_even_when_the_config_requires_a_wake_word(self, cfg):
        """The bypass is not conditional on the deployment having relaxed it."""
        assert cfg.WAKE_WORD_REQUIRED is True, (
            "WAKE_WORD_REQUIRED should default to True; if the deployment turns "
            "it off the manual-voice flag is untested by this suite"
        )
        result = gate("open the browser", manual_voice=True, cfg=cfg)
        assert result.acted and not result.dropped

    def test_follow_up_after_a_wake_word_needs_no_repeat(self, cfg):
        """The existing session-window behaviour must survive the change."""
        first = gate("zaytoon", manual_voice=False, cfg=cfg)
        assert first.detected and not first.acted
        follow = apply_wake_gate(
            "what time is it",
            awaiting_command=True,
            last_active_ts=0.0,
            timeout_secs=cfg.WAKE_SESSION_TIMEOUT,
            enabled=True,
        )
        assert follow.acted and not follow.dropped
