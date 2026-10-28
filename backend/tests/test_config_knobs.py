"""Configuration knobs that must actually be read.

A settings class is a promise: an operator reads ``REMINDER_CHECK_INTERVAL`` in
``config.py``, sets it, and expects a change. Four of these were not read by
anything — the scheduler hard-coded ``15`` beside the knob, and three more
(``OPENROUTER_RATE_LIMIT_BACKOFF``, ``WS_RATE_LIMIT_MAX``/``WINDOW``,
``ASSISTANT_NAME``) were declared and then never consumed. The first tests pin
the wiring; the last pins that the dead ones stay gone, so a later "let's add a
knob for that" cannot quietly reintroduce a lie.
"""

from __future__ import annotations

import os
import sys
import types
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as config_module  # noqa: E402
from config import config  # noqa: E402
from services.agent_service import AgentService  # noqa: E402


class TestReminderCheckInterval:
    def test_the_scheduler_reads_the_configured_interval(self, monkeypatch):
        from scheduler.manager import SchedulerManager

        monkeypatch.setattr(config, "REMINDER_CHECK_INTERVAL", 7)
        manager = SchedulerManager(MagicMock(), MagicMock())
        assert manager._check_interval == 7.0

    def test_an_explicit_interval_still_wins(self):
        """Tests drive a tick with this, so it must not be overridden."""
        from scheduler.manager import SchedulerManager

        manager = SchedulerManager(MagicMock(), MagicMock(), check_interval=0.5)
        assert manager._check_interval == 0.5

    def test_a_broken_config_never_stops_scheduling(self, monkeypatch):
        """Scheduling is the feature; a config error must not disable it."""
        from scheduler.manager import SchedulerManager

        # A stand-in module with no REMINDER_CHECK_INTERVAL on it: the lookup in
        # `__init__` raises, and the fallback keeps the loop alive.
        monkeypatch.setitem(sys.modules, "config", types.ModuleType("config"))
        instance = SchedulerManager(MagicMock(), MagicMock())
        assert instance._check_interval == 15.0


class TestContextCeiling:
    def _service(self, recorded):
        fast = MagicMock()

        async def process(message, context, **kwargs):
            recorded.append(list(context))
            return "ok"

        fast.process = process
        return AgentService(
            router_agent=MagicMock(),
            fast_agent=fast,
            planner_agent=MagicMock(),
            code_agent=MagicMock(),
            tool_agent=MagicMock(),
            memory_agent=MagicMock(),
            scheduler_llm_agent=MagicMock(),
            scheduler_manager=MagicMock(),
            reminder_service=MagicMock(),
            notification_mgr=MagicMock(),
            tool_executor=MagicMock(),
            os_toolkit=MagicMock(),
        )

    async def test_the_history_is_capped_before_it_reaches_an_agent(
        self, monkeypatch
    ):
        monkeypatch.setattr(config, "MAX_CONTEXT_MESSAGES", 2)
        recorded = []
        service = self._service(recorded)
        history = [{"role": "user", "content": f"m{i}"} for i in range(50)]

        await service.dispatch("chat", "hello", history, "en", "sess-1")

        assert recorded == [history[-2:]]

    async def test_a_short_history_is_left_untouched(self, monkeypatch):
        monkeypatch.setattr(config, "MAX_CONTEXT_MESSAGES", 50)
        recorded = []
        service = self._service(recorded)
        history = [{"role": "user", "content": "m0"}]

        await service.dispatch("chat", "hello", history, "en", "sess-1")

        assert recorded == [history]


class TestDeadKnobsAreGone:
    @pytest.mark.parametrize(
        "name",
        [
            "OPENROUTER_RATE_LIMIT_BACKOFF",
            "WS_RATE_LIMIT_MAX",
            "WS_RATE_LIMIT_WINDOW",
            "ASSISTANT_NAME",
        ],
    )
    def test_a_knob_nothing_reads_is_not_advertised(self, name):
        assert not hasattr(config, name), (
            f"{name} is declared but read by nothing — wiring it or dropping it "
            "are both fine; leaving it is the one thing that is not."
        )

    def test_the_names_do_not_reappear_in_the_config_source(self):
        """An attribute is easy to miss in a diff; the literal is not."""
        with open(config_module.__file__, encoding="utf-8") as handle:
            source = handle.read()
        for name in (
            "OPENROUTER_RATE_LIMIT_BACKOFF",
            "WS_RATE_LIMIT_MAX",
            "WS_RATE_LIMIT_WINDOW",
            "ASSISTANT_NAME",
        ):
            assert name not in source, f"{name} is still named in config.py"
