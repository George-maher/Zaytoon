"""Source ledger: what counts as a source, and what `[n]` is allowed to mean.

Stamping instead of refusing (the 2026-10-11 decision), which puts the burden on
two provable things: the ledger may only contain what a tool *actually returned*,
and a citation with nothing behind it has to be caught. Everything else — whether
the model phrased a claim well — is out of scope here.
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.engine import AgentEngine  # noqa: E402
from agents.sources import SourceLedger  # noqa: E402
from config import config  # noqa: E402
from tools.base import BaseTool, ToolResult  # noqa: E402
from tools.registry import ToolRegistry  # noqa: E402


def run(coro):
    return asyncio.run(coro)


FETCH_RESULT = {
    "success": True,
    "output": "EXTERNAL CONTENT …",
    "data": {
        "url": "https://docs.pypi.org/httpx/",
        "status": 200,
        "content_type": "text/html",
        "retrieved_at": "2026-10-11T05:00:00Z",
    },
}

SEARCH_RESULT = {
    "success": True,
    "output": "2 result(s)…",
    "data": {
        "results": [
            {"title": "httpx", "href": "https://python-httpx.org/"},
            {"title": "issues", "href": "https://github.com/encode/httpx/issues"},
        ]
    },
}


class TestRecording:
    def test_a_fetched_page_is_a_source(self):
        ledger = SourceLedger()
        added = ledger.ingest("fetch_url", {"url": "x"}, FETCH_RESULT)
        assert len(added) == 1
        assert added[0].read is True
        assert added[0].url == "https://docs.pypi.org/httpx"  # trailing slash folded

    def test_search_results_are_sources_of_a_lesser_kind(self):
        ledger = SourceLedger()
        added = ledger.ingest("search_web", {"query": "httpx"}, SEARCH_RESULT)
        assert len(added) == 2
        assert all(not item.read for item in added)
        assert "snippet only" in added[0].line()
        assert ledger.queries == ["httpx"]

    def test_a_failed_or_unrelated_tool_contributes_nothing(self):
        ledger = SourceLedger()
        assert ledger.ingest("fetch_url", {}, {"success": False, "error": "x"}) == []
        assert ledger.ingest("open_app", {}, {"success": True, "data": {"url": "y"}}) == []
        assert ledger.ingest("fetch_url", {}, {"success": True}) == []
        assert ledger.count == 0

    def test_the_same_page_twice_is_one_citation(self):
        ledger = SourceLedger()
        ledger.ingest("search_web", {"query": "q"}, SEARCH_RESULT)
        ledger.ingest("search_web", {"query": "q"}, SEARCH_RESULT)
        assert ledger.count == 2, "a repeated query duplicated every result"
        assert ledger.queries == ["q"]

    def test_reading_a_seen_result_upgrades_it_in_place(self):
        ledger = SourceLedger()
        ledger.ingest("search_web", {"query": "q"}, SEARCH_RESULT)
        before = ledger.items[0]
        added = ledger.ingest(
            "fetch_url", {}, {**FETCH_RESULT, "data": {"url": "https://python-httpx.org/"}}
        )
        assert added == [], "the upgrade must not mint a second number"
        assert ledger.count == 2
        assert before.read is True
        assert before.index == 1

    def test_a_weird_outcome_never_raises(self):
        """A ledger failure must not become a failed turn."""
        ledger = SourceLedger()
        for junk in (None, "text", 7, {"data": {"results": "not a list"}}):
            assert ledger.ingest("search_web", {}, junk) == []
        assert ledger.count == 0


class TestPromptBlock:
    def test_numbers_are_stable_and_pages_come_first(self):
        ledger = SourceLedger()
        ledger.ingest("search_web", {"query": "q"}, SEARCH_RESULT)
        ledger.ingest("fetch_url", {}, FETCH_RESULT)
        block = ledger.prompt_block()
        assert "[3] docs.pypi.org" in block and "[3]" in block
        assert block.index("[3]") < block.index("[1]")  # the read page is listed first
        assert "Cite as [n]" in block

    def test_the_list_is_capped_and_says_so(self):
        ledger = SourceLedger()
        many = {
            "success": True,
            "data": {"results": [{"title": f"t{i}", "href": f"https://a.test/{i}"} for i in range(30)]},
        }
        ledger.ingest("search_web", {"query": "q"}, many)
        block = ledger.prompt_block()
        assert ledger.count == 30
        assert "more result(s) not listed" in block
        assert block.count("[") - block.count("[n]") == 12

    def test_no_sources_means_no_block(self):
        assert SourceLedger().prompt_block() == ""

    def test_the_arabic_block_carries_the_same_rule(self):
        ledger = SourceLedger()
        ledger.ingest("fetch_url", {}, FETCH_RESULT)
        block = ledger.prompt_block("ar")
        assert "مصادر" in block and "غير موثّق" in block


class TestStamping:
    def test_the_answer_gains_sources_and_method(self):
        ledger = SourceLedger()
        ledger.ingest("search_web", {"query": "httpx docs"}, SEARCH_RESULT)
        ledger.ingest("fetch_url", {}, FETCH_RESULT)
        text, notes = ledger.stamp("httpx 0.28 supports HTTP/2 [3].", "en")
        assert text.startswith("httpx 0.28 supports HTTP/2 [3].")
        assert "**Sources**" in text
        assert "Method: 1 search query, 1 page(s) read in full, 2 snippet(s)" in text
        assert notes == []

    def test_a_citation_with_nothing_behind_it_is_called_out(self):
        ledger = SourceLedger()
        ledger.ingest("fetch_url", {}, FETCH_RESULT)
        text, notes = ledger.stamp("Prices doubled [7] last year [2].", "en")
        assert "[7]" in text and "[2]" in text  # the claims stay; they are just not backed
        assert "cite no source read in this turn" in text
        assert notes == ["cited 2 unknown source(s)"]

    def test_an_unsourced_turn_is_left_alone(self):
        """No appendix, no warning, no noise, when nothing was read."""
        ledger = SourceLedger()
        text, notes = ledger.stamp("It is 2026-10-11 06:00.", "en")
        assert text == "It is 2026-10-11 06:00."
        assert notes == []

    def test_markdown_links_are_not_citations(self):
        ledger = SourceLedger()
        ledger.ingest("fetch_url", {}, FETCH_RESULT)
        text, notes = ledger.stamp("see [httpx docs](https://x.test/a) [1]", "en")
        assert notes == [], "a link's bracket was read as a citation"

    def test_arabic_warning(self):
        ledger = SourceLedger()
        text, notes = ledger.stamp("كل شيء تمام [4]", "ar")
        assert "ملاحظة" in text
        assert notes


class _Fake(BaseTool):
    """A tool that returns a canned outcome, so the loop is driven for real."""

    permission = "safe"

    def __init__(self, name, result):
        super().__init__()
        self._name = name
        self._result = result

    def name(self):
        return self._name

    def description(self):
        return "stub"

    async def execute(self, **params):
        return self._result


@pytest.fixture(autouse=True)
def _model_path_only(monkeypatch):
    """Same isolation as tests/test_agent_bounds.py: the fast path answers before
    the loop, and a source ledger only exists for turns that reach the loop."""
    monkeypatch.setattr(config, "FAST_PATH_ENABLED", False)


class TestEngineIntegration:
    def _engine(self, replies, *tools):
        registry = ToolRegistry()
        for tool in tools:
            registry.register(tool)
        eng = AgentEngine(llm=None, registry=registry, metrics=None)
        log = []
        queue = list(replies)

        async def _ask(messages, *a, **k):
            log.append([dict(m) for m in messages])
            return queue.pop(0) if queue else "All done."

        eng._ask = _ask  # noqa: SLF001
        return eng, log

    def test_a_fetched_page_reaches_the_answer_as_a_citation(self):
        eng, log = self._engine(
            ['{"action": "fetch_url", "params": {"url": "https://x.test/a"}}',
             "The version is 0.28 [1]."],
            _Fake("fetch_url", ToolResult(True, "EXTERNAL CONTENT …", data={"url": "https://x.test/a", "status": 200, "content_type": "text/html"})),
        )
        result = run(eng.run("what version is httpx?"))
        assert "**Sources**" in result.text
        assert "[1] x.test/a" in result.text or "[1] x.test" in result.text
        assert "Method:" in result.text
        assert [s["n"] for s in result.sources] == [1]
        assert result.sources[0]["kind"] == "page"
        # The model was shown the ledger before it answered, so [1] means the
        # same thing to both sides.
        assert "SOURCES for this turn" in log[1][-1]["content"]

    def test_a_citation_to_nothing_is_reported_too(self):
        eng, _ = self._engine(
            ['{"action": "fetch_url", "params": {"url": "https://x.test/a"}}',
             "It doubled [9]."],
            _Fake("fetch_url", ToolResult(True, "EXTERNAL", data={"url": "https://x.test/a"})),
        )
        result = run(eng.run("did it double?"))
        assert "cite no source" in result.text
        assert any("unknown source" in e for e in result.errors)

    def test_a_tool_only_turn_has_no_source_noise(self):
        eng, _ = self._engine(
            ['{"action": "get_time", "params": {}}', "It is late."],
            _Fake("get_time", ToolResult(True, "2026-10-11 06:00")),
        )
        result = run(eng.run("what time is it?"))
        assert "Sources" not in result.text
        assert result.sources == []

    def test_the_budget_path_is_stamped_as_well(self):
        """Running out of steps is when a confident summary is most dangerous."""
        reply = '{"action": "fetch_url", "params": {"url": "https://x.test/a"}}'
        eng, _ = self._engine(
            [reply] * 8,
            _Fake("fetch_url", ToolResult(True, "EXTERNAL", data={"url": "https://x.test/a"})),
        )
        result = run(eng.run("read everything"))
        assert result.stopped_reason == "max_iterations"
        assert "**Sources**" in result.text
        assert len(result.sources) == 1
