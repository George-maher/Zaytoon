import pytest

from agents.base_agent import BaseAgent
from agents.fast_agent import FastAgent
from agents.code_agent import CodeAgent
from agents.planner_agent import PlannerAgent
from agents.tool_agent import ToolAgent
from agents.router_agent import RouterAgent
from agents.scheduler_agent import SchedulerAgent
from config import TIER_FAST, TIER_REASONING, config


@pytest.mark.asyncio
async def test_code_agent_high_tokens():
    agent = CodeAgent()
    assert agent.max_tokens == 4096


@pytest.mark.asyncio
async def test_agent_has_name():
    agent = FastAgent()
    assert agent.agent_name == "Fast Agent"


@pytest.mark.asyncio
async def test_agent_uses_openrouter():
    agent = FastAgent()
    assert agent.model_name == config.OPENROUTER_MODEL
    assert agent.api_url == config.LLM_API_URL
    assert "openrouter" in agent.api_url or "chat/completions" in agent.api_url


# ---------------------------------------------------------------------------
# Model tiers drive the fallback chain
# ---------------------------------------------------------------------------
class TestAgentTiers:
    def test_chat_agents_use_the_fast_tier(self):
        assert FastAgent().tier == TIER_FAST
        assert ToolAgent().tier == TIER_FAST

    def test_reasoning_agents_use_the_reasoning_tier(self):
        assert CodeAgent().tier == TIER_REASONING
        assert PlannerAgent().tier == TIER_REASONING
        assert SchedulerAgent().tier == TIER_REASONING

    def test_router_uses_the_fast_tier(self):
        assert RouterAgent().tier == TIER_FAST

    def test_tier_can_be_overridden_per_instance(self):
        agent = FastAgent()
        agent.tier = TIER_REASONING
        assert agent.tier == TIER_REASONING

    def test_all_agents_use_the_shared_client(self):
        agents = [
            FastAgent(), CodeAgent(), PlannerAgent(), ToolAgent(),
            RouterAgent(), SchedulerAgent(),
        ]
        clients = {id(a.llm) for a in agents}
        assert len(clients) == 1


class TestPayloadBuilding:
    def test_payload_respects_token_cap(self):
        agent = FastAgent()
        payload = agent._build_payload([{"role": "user", "content": "x"}])
        assert payload["max_tokens"] <= config.LLM_MAX_TOKENS_CAP
        assert payload.get("stream", False) is False

    def test_stream_payload_sets_stream_flag(self):
        payload = FastAgent()._build_payload(
            [{"role": "user", "content": "x"}], stream=True
        )
        assert payload["stream"] is True


class TestErrorSurfacing:
    async def test_call_api_reports_exhausted_providers(self, monkeypatch):
        from services.llm_client import LLMError

        async def boom(*args, **kwargs):
            raise LLMError("all down")

        agent = FastAgent()
        monkeypatch.setattr(agent.llm, "complete", boom)
        result = await agent.call_api([{"role": "user", "content": "x"}])
        assert "Fast Agent" in result
        assert "failed" in result.lower()

    async def test_get_ai_response_falls_back_to_non_streaming(self, monkeypatch):
        agent = FastAgent()
        calls = []

        async def failing_stream(*args, **kwargs):
            calls.append("stream")
            from services.llm_client import LLMError
            raise LLMError("no endpoints")

        async def working_complete(messages, **kwargs):
            calls.append("complete")
            return "blocking answer"

        monkeypatch.setattr(agent.llm, "stream", failing_stream)
        monkeypatch.setattr(agent.llm, "complete", working_complete)

        async def on_delta(_text):
            return None

        result = await agent.get_ai_response(
            [{"role": "user", "content": "x"}], stream_callback=on_delta
        )
        assert result == "blocking answer"
        assert calls == ["stream", "complete"]

    async def test_get_ai_response_prefers_stream_result(self, monkeypatch):
        agent = FastAgent()

        async def working_stream(messages, on_delta=None, **kwargs):
            await on_delta("tok")
            return "streamed answer"

        async def never_called(messages, **kwargs):
            raise AssertionError("non-streaming should not be used")

        monkeypatch.setattr(agent.llm, "stream", working_stream)
        monkeypatch.setattr(agent.llm, "complete", never_called)

        async def on_delta(_text):
            return None

        result = await agent.get_ai_response(
            [{"role": "user", "content": "x"}], stream_callback=on_delta
        )
        assert result == "streamed answer"

    async def test_empty_stream_triggers_non_streaming_retry(self, monkeypatch):
        agent = FastAgent()

        async def empty_stream(*args, **kwargs):
            return ""

        async def working_complete(messages, **kwargs):
            return "recovered"

        monkeypatch.setattr(agent.llm, "stream", empty_stream)
        monkeypatch.setattr(agent.llm, "complete", working_complete)

        async def on_delta(_text):
            return None

        result = await agent.get_ai_response(
            [{"role": "user", "content": "x"}], stream_callback=on_delta
        )
        assert result == "recovered"
