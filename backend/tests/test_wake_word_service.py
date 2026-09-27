import time

from services.wake_word_service import apply_wake_gate, strip_wake_word, requires_wake_word


def test_strip_wake_word_english():
    assert strip_wake_word("Zaytoon open chrome") == "open chrome"
    assert strip_wake_word("zaytoon, what time is it") == "what time is it"


def test_strip_wake_word_arabic():
    assert strip_wake_word("زيتون افتح كروم") == "افتح كروم"
    assert strip_wake_word("يا زيتون، إيه الوقت") == "إيه الوقت"


def test_strip_wake_word_only():
    assert strip_wake_word("Zaytoon") == ""
    assert strip_wake_word("زيتون") == ""


def test_strip_wake_word_missing():
    assert strip_wake_word("open chrome") is None
    assert strip_wake_word("hello world") is None
    assert strip_wake_word("") is None


def test_requires_wake_word_session_window():
    now = time.time()
    assert requires_wake_word(False, now, 10.0) is False
    assert requires_wake_word(False, now - 15, 10.0) is True


def test_requires_wake_word_awaiting_command():
    assert requires_wake_word(True, 0, 10.0) is False


# --- apply_wake_gate -------------------------------------------------------
# The three outcomes the WebSocket handler has to distinguish: handled,
# wake-word-only (arm the follow-up window), and discarded.

def _gate(text, *, awaiting=False, last_active=0.0, enabled=True):
    return apply_wake_gate(
        text,
        awaiting_command=awaiting,
        last_active_ts=last_active,
        timeout_secs=10.0,
        enabled=enabled,
    )


def test_gate_handles_spoken_wake_word():
    r = _gate("Zaytoon what time is it")
    assert r.acted and r.detected and not r.dropped
    assert r.text == "what time is it"


def test_gate_arms_window_on_wake_word_only():
    r = _gate("Zaytoon")
    assert not r.acted and r.detected and not r.dropped


def test_gate_drops_utterance_without_wake_word():
    r = _gate("what time is it")
    assert r.dropped and not r.acted and not r.detected


def test_gate_drops_arabic_utterance_without_wake_word():
    assert _gate("إيه الوقت دلوقتي").dropped


def test_gate_keeps_follow_up_without_repeating_wake_word():
    r = _gate("and tomorrow", awaiting=True)
    assert r.acted and r.text == "and tomorrow"
    assert not r.detected and not r.dropped


def test_gate_keeps_follow_up_inside_activity_window():
    r = _gate("and tomorrow", last_active=time.time())
    assert r.acted and r.text == "and tomorrow"


def test_gate_re_arms_once_activity_window_expires():
    r = _gate("and tomorrow", last_active=time.time() - 60.0)
    assert r.dropped


def test_gate_disabled_passes_everything_through():
    r = _gate("what time is it", enabled=False)
    assert r.acted and r.text == "what time is it"
    assert not r.dropped and not r.detected


def test_gate_disabled_keeps_wake_prefix_only_when_gated():
    r = _gate("Zaytoon open chrome", enabled=False)
    # With the gate off the wake word is just part of the text, not stripped.
    assert r.acted and r.text == "Zaytoon open chrome"


def test_wake_gate_config_flags_exist():
    from config import config as app_config

    assert isinstance(app_config.WAKE_WORD_REQUIRED, bool)
    assert isinstance(app_config.WAKE_WORD_HINT, bool)
