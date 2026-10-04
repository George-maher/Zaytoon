"""The animation must stop when there is nothing to animate.

``AnimationLoop`` is the shell's only scheduler, and the requirement it exists
to satisfy is behavioural: an idle or offline assistant draws a static frame
and schedules **nothing** — no ``after()`` chain, no permanent 30fps timer, no
overlapping loops for one widget.

The loop takes its ``schedule``/``cancel`` from the constructor precisely so
this can be proven with a recording fake instead of a real window.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import MAX_MISSES, AnimationLoop, budget_verdict, is_static_mode  # noqa: E402
from app.states import STATIC_STATES, UiState  # noqa: E402


class FakeClock:
    """A recorder for ``after``/``after_cancel`` with manual time."""

    def __init__(self) -> None:
        self.scheduled: list[tuple[int, object, object]] = []
        self.cancelled: list[object] = []
        self.live: set = set()
        self._ids = iter(range(1, 10_000))

    def schedule(self, delay_ms, callback):
        token = next(self._ids)
        self.scheduled.append((delay_ms, callback, token))
        self.live.add(token)
        return token

    def cancel(self, token):
        self.cancelled.append(token)
        self.live.discard(token)

    # -- driving ----------------------------------------------------------
    def fire_latest(self) -> None:
        """Run the most recently scheduled callback (as Tk's mainloop would)."""
        _delay, callback, token = self.scheduled[-1]
        self.live.discard(token)
        callback()

    @property
    def pending(self) -> int:
        """Frames scheduled, not yet cancelled, not yet fired."""
        return len(self.live)


class TestBudgetVerdict:
    def test_frame_within_budget_resets_the_streak(self):
        assert budget_verdict(10.0, 30.0, streak=1) == (0, False)

    def test_one_late_frame_is_a_hiccup(self):
        assert budget_verdict(45.0, 30.0, streak=0) == (1, False)

    def test_consecutive_late_frames_degrade(self):
        streak, degrade = budget_verdict(90.0, 30.0, streak=MAX_MISSES - 1)
        assert streak == MAX_MISSES
        assert degrade is True

    def test_streak_is_not_consecutive_milliseconds(self):
        # A late frame after a good one starts over.
        assert budget_verdict(90.0, 30.0, streak=0) == (1, False)


class TestStaticMode:
    @pytest.mark.parametrize("state", sorted(STATIC_STATES, key=lambda s: s.value))
    def test_idle_and_offline_are_static(self, state):
        assert is_static_mode(state)

    @pytest.mark.parametrize(
        "state",
        [s for s in UiState if s not in STATIC_STATES],
    )
    def test_every_other_state_animates(self, state):
        assert not is_static_mode(state)

    @pytest.mark.parametrize("raw", ["idle", "connected", "done", "offline", "disconnected"])
    def test_static_hud_strings(self, raw):
        assert is_static_mode(raw)

    @pytest.mark.parametrize("raw", ["thinking", "listening", "speaking", "executing"])
    def test_active_hud_strings(self, raw):
        assert not is_static_mode(raw)


class TestAnimationLoop:
    def test_static_mode_schedules_nothing_at_all(self):
        """The requirement: idle must not cost a timer."""
        clock = FakeClock()
        loop = AnimationLoop(clock.schedule, clock.cancel, lambda: None)
        loop.set_mode(UiState.IDLE)
        assert loop.running is False
        assert clock.scheduled == []
        assert loop.token is None

    def test_offline_schedules_nothing(self):
        clock = FakeClock()
        loop = AnimationLoop(clock.schedule, clock.cancel, lambda: None)
        loop.set_mode(UiState.OFFLINE)
        assert clock.scheduled == []

    def test_static_string_schedules_nothing(self):
        clock = FakeClock()
        loop = AnimationLoop(clock.schedule, clock.cancel, lambda: None)
        loop.set_mode("disconnected")
        assert clock.scheduled == []

    def test_switching_to_static_cancels_a_running_chain(self):
        clock = FakeClock()
        loop = AnimationLoop(clock.schedule, clock.cancel, lambda: None)
        loop.set_mode(UiState.PLANNING)
        assert loop.running is True
        assert clock.pending == 1

        loop.set_mode(UiState.IDLE)
        assert loop.running is False
        assert clock.cancelled == [1]
        assert loop.token is None

    def test_an_active_state_runs_one_chain(self):
        clock = FakeClock()
        loop = AnimationLoop(clock.schedule, clock.cancel, lambda: None, interval_ms=30)
        loop.start()
        assert loop.running is True
        assert len(clock.scheduled) == 1
        assert clock.scheduled[0][0] == 30

    def test_start_is_idempotent_no_overlapping_loops(self):
        """The guard: a second start() must not spawn a second chain."""
        clock = FakeClock()
        loop = AnimationLoop(clock.schedule, clock.cancel, lambda: None)
        loop.start()
        loop.start()
        loop.start()
        assert len(clock.scheduled) == 1

    def test_each_frame_schedules_exactly_one_next_frame(self):
        clock = FakeClock()
        frames = []
        loop = AnimationLoop(clock.schedule, clock.cancel, lambda: frames.append(1))
        loop.start()
        for _ in range(5):
            clock.fire_latest()
        assert len(frames) == 5
        assert clock.pending == 1  # never two live frames

    def test_frames_keep_the_configured_interval(self):
        clock = FakeClock()
        loop = AnimationLoop(clock.schedule, clock.cancel, lambda: None, interval_ms=45)
        loop.start()
        assert clock.scheduled[-1][0] == 45

    def test_stop_cancels_and_never_reschedules(self):
        clock = FakeClock()
        loop = AnimationLoop(clock.schedule, clock.cancel, lambda: None)
        loop.start()
        loop.stop()
        assert clock.cancelled == [1]
        # Firing the now-cancelled frame must not resurrect the chain.
        clock.fire_latest()
        assert loop.running is False
        assert loop.token is None
        assert clock.pending == 0

    def test_stop_is_safe_when_never_started(self):
        clock = FakeClock()
        loop = AnimationLoop(clock.schedule, clock.cancel, lambda: None)
        loop.stop()  # must not raise
        assert clock.cancelled == []

    def test_stop_is_safe_to_call_twice(self):
        clock = FakeClock()
        loop = AnimationLoop(clock.schedule, clock.cancel, lambda: None)
        loop.start()
        loop.stop()
        loop.stop()
        assert clock.cancelled == [1]

    def test_an_exception_in_a_frame_still_reschedules(self):
        clock = FakeClock()

        def boom():
            raise RuntimeError("a drawing failure must not kill the loop")

        loop = AnimationLoop(clock.schedule, clock.cancel, boom)
        loop.start()
        with pytest.raises(RuntimeError):
            clock.fire_latest()
        assert loop.running is True
        assert clock.pending == 1

    def test_a_failing_schedule_call_ends_the_loop_cleanly(self):
        # The widget is destroyed mid-frame: ``after`` itself raises, and the
        # loop must settle into "not running" instead of looping on errors.
        calls = {"n": 0}
        cancelled: list = []

        def flaky(delay_ms, callback):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("widget destroyed")
            return 1

        def fire():
            # Frame 1: the scheduled callback runs, then the reschedule fails.
            loop._frame()

        loop = AnimationLoop(flaky, cancelled.append, lambda: None)
        loop.start()
        assert loop.running is True
        fire()
        assert loop.running is False
        assert loop.token is None

    def test_frame_after_cancel_does_not_reschedule(self):
        clock = FakeClock()
        holder = {}

        def on_frame():
            holder["loop"].stop()

        loop = AnimationLoop(clock.schedule, clock.cancel, on_frame)
        holder["loop"] = loop
        loop.start()
        clock.fire_latest()
        assert loop.running is False
        assert clock.pending == 0
