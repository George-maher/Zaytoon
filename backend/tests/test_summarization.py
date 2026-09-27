"""Rolling conversation summarisation.

This exists because ``MemoryAgent.summarize_and_prune`` used to do
``BaseAgent("Summarizer", ...)``. ``BaseAgent`` is an ABC with an abstract
``process`` method, so that call raised ``TypeError: Can't instantiate abstract
class`` on *every* invocation — and a bare ``except Exception`` swallowed it.
Every stored summary was therefore the literal string ``"Previous conversation"``,
permanently, with no error surfaced and no test failing.
"""

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.base_agent import BaseAgent  # noqa: E402
from agents.memory_agent import MemoryAgent  # noqa: E402
from agents.summarizer_agent import SummarizerAgent  # noqa: E402


# ---------------------------------------------------------------------------
# The root cause
# ---------------------------------------------------------------------------
class TestBaseAgentIsAbstract:
    def test_base_agent_cannot_be_instantiated(self):
        with pytest.raises(TypeError):
            BaseAgent("Summarizer", max_tokens=128, temperature=0.1)

    def test_base_agent_declares_process_abstract(self):
        assert "process" in BaseAgent.__abstractmethods__


class TestSummarizerAgent:
    def test_is_a_real_instantiable_agent(self):
        agent = SummarizerAgent()
        assert isinstance(agent, BaseAgent)
        assert callable(agent.process)

    def test_uses_the_fast_tier(self):
        # Summarisation is cheap and latency-sensitive; it must not sit behind
        # the reasoning model.
        from config import TIER_FAST

        assert SummarizerAgent.tier == TIER_FAST

    def test_asks_for_a_short_summary(self):
        agent = SummarizerAgent()
        assert agent.max_tokens <= 256
        assert agent.temperature <= 0.3

    def test_declares_the_fallback_summary_as_a_constant(self):
        assert SummarizerAgent.FALLBACK_SUMMARY == "Previous conversation"


# ---------------------------------------------------------------------------
# The deterministic fallback
# ---------------------------------------------------------------------------
class TestExtractFallback:
    """Used when the LLM is unreachable, so a real summary is still produced."""

    def test_prefers_the_first_user_request(self):
        text = "assistant: Hello! | user: build a react dashboard | user: add auth"
        assert MemoryAgent._extract_summary(text) == "build a react dashboard"

    def test_falls_back_to_raw_text_without_a_user_turn(self):
        assert MemoryAgent._extract_summary("assistant: only me") == "assistant: only me"

    def test_result_is_length_limited(self):
        assert len(MemoryAgent._extract_summary("user: " + "x" * 500)) <= 200

    def test_empty_input_is_not_empty(self):
        assert MemoryAgent._extract_summary("") == "Previous conversation"

    def test_real_content_never_yields_the_old_constant(self):
        """This exact string was the symptom of the bug."""
        text = "user: please open spotify and set a reminder for tomorrow"
        assert MemoryAgent._extract_summary(text) != "Previous conversation"

    def test_result_is_never_blank_for_non_blank_input(self):
        for text in ("user: a", "assistant: b", "|", "user:"):
            assert MemoryAgent._extract_summary(text).strip()


# ---------------------------------------------------------------------------
# The summarisation path
# ---------------------------------------------------------------------------
@pytest.fixture
def patched_llm(monkeypatch):
    def _set(result):
        async def fake(self, messages, *args, **kwargs):
            if isinstance(result, Exception):
                raise result
            return result
        monkeypatch.setattr(SummarizerAgent, "get_ai_response", fake)
    return _set


class TestSummarize:
    async def test_uses_a_real_llm_summary(self, patched_llm):
        patched_llm("opened spotify, set a reminder")
        memory = MemoryAgent()
        assert await memory._summarize("user: hi | assistant: hello") == (
            "opened spotify, set a reminder"
        )

    async def test_strips_surrounding_quotes(self, patched_llm):
        patched_llm('"opened spotify"')
        memory = MemoryAgent()
        assert await memory._summarize("user: hi") == "opened spotify"

    async def test_falls_back_when_the_provider_raises(self, patched_llm):
        patched_llm(RuntimeError("OPENROUTER_API_KEY is not set"))
        memory = MemoryAgent()
        assert await memory._summarize("user: open spotify") == "open spotify"

    async def test_falls_back_when_the_provider_returns_an_error_string(self, patched_llm):
        patched_llm("Summarizer Agent: All model providers failed. Last error: 401")
        memory = MemoryAgent()
        assert await memory._summarize("user: open spotify") == "open spotify"

    async def test_falls_back_when_the_provider_returns_nothing(self, patched_llm):
        patched_llm("   ")
        memory = MemoryAgent()
        assert await memory._summarize("user: open spotify") == "open spotify"

    async def test_falls_back_when_the_summarizer_cannot_be_imported(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "agents.summarizer_agent", None)
        memory = MemoryAgent()
        assert await memory._summarize("user: open spotify") == "open spotify"

    async def test_truncates_an_overlong_summary(self, patched_llm):
        patched_llm("y" * 5000)
        memory = MemoryAgent()
        assert len(await memory._summarize("user: hi")) <= 200

    async def test_fallback_is_logged_not_silent(self, patched_llm, caplog):
        """The original bug was invisible. Assert we now say something."""
        patched_llm(RuntimeError("boom"))
        memory = MemoryAgent()
        with caplog.at_level(logging.WARNING):
            await memory._summarize("user: open spotify")
        assert any("Summariser" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# End to end, against a real database
# ---------------------------------------------------------------------------
class TestSummarizeAndPruneEndToEnd:
    async def test_a_real_summary_reaches_the_database(self, tmp_path, monkeypatch, patched_llm):
        """The end-to-end regression guard for the silent failure."""
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'm.db'}")
        patched_llm("discussed the dashboard layout")

        memory = MemoryAgent()
        for i in range(25):
            await memory.store_message(
                "user" if i % 2 == 0 else "assistant",
                f"message number {i}",
                session_id="s1",
            )

        await memory.summarize_and_prune("s1")
        stored = await memory._get_summary("s1")

        assert stored == "discussed the dashboard layout"
        assert stored != "Previous conversation"
        await memory.close()

    async def test_end_to_end_fallback_still_reports_something_useful(
        self, tmp_path, monkeypatch, patched_llm
    ):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'm2.db'}")
        patched_llm(RuntimeError("no network"))

        memory = MemoryAgent()
        for i in range(25):
            await memory.store_message(
                "user" if i % 2 == 0 else "assistant",
                "deploy the dashboard to staging" if i == 0 else f"msg {i}",
                session_id="s2",
            )

        await memory.summarize_and_prune("s2")
        stored = await memory._get_summary("s2")

        assert stored == "deploy the dashboard to staging"
        await memory.close()

    async def test_short_sessions_are_not_summarised(self, tmp_path, monkeypatch, patched_llm):
        patched_llm("should not be called")
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'm3.db'}")
        memory = MemoryAgent()
        for i in range(5):
            await memory.store_message("user", f"msg {i}", session_id="s3")

        await memory.summarize_and_prune("s3")
        assert await memory._get_summary("s3") is None
        await memory.close()
