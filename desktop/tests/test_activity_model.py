"""The per-turn activity model — plan steps and tool rows as data.

``ActivityModel`` holds what the current turn is doing so the feed can render
it. The rules that matter live here rather than in widget callbacks: a row's
glyph and colour come from its status, a repeated tool does not duplicate, a
plan implies a fresh turn, and nothing grows without bound.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.activity import (  # noqa: E402
    MAX_TOOL_ROWS,
    PLAN_GLYPH,
    TOOL_GLYPHS,
    ActivityModel,
    accent_hex,
)
from app import theme  # noqa: E402


class TestPlan:
    def test_steps_are_numbered_from_01(self):
        model = ActivityModel()
        model.add_plan(["Read the file", "Patch it", "Run tests"])
        rows = model.plan_rows()
        assert [text for _g, _a, text in rows] == [
            "01  Read the file",
            "02  Patch it",
            "03  Run tests",
        ]

    def test_plan_rows_use_the_plan_glyph_and_violet(self):
        model = ActivityModel()
        model.add_plan(["step"])
        glyph, accent, _text = model.plan_rows()[0]
        assert glyph == PLAN_GLYPH[0]
        assert accent == PLAN_GLYPH[1] == "violet"

    def test_blank_steps_are_dropped_and_not_numbered(self):
        model = ActivityModel()
        model.add_plan(["keep", "   ", "", "also keep"])
        assert len(model.plan_rows()) == 2
        assert model.plan_rows()[0][2].startswith("01")
        assert model.plan_rows()[1][2].startswith("02")

    def test_a_new_plan_clears_stale_tool_rows(self):
        # A plan means a new turn's worth of work; old rows would otherwise
        # sit next to steps that come after them.
        model = ActivityModel()
        model.start_tool("file_read")
        model.add_plan(["new turn"])
        assert model.tools == []
        assert len(model.plan_rows()) == 1


class TestToolRows:
    def test_running_tool_is_marked_running(self):
        model = ActivityModel()
        model.start_tool("file_write", "Write notes.md")
        assert model.active_tool == "file_write"
        glyph, accent, text = model.tool_rows()[0]
        assert (glyph, accent) == TOOL_GLYPHS["running"]
        assert text == "Write notes.md"

    def test_finished_success_is_green_tick(self):
        model = ActivityModel()
        model.start_tool("file_write")
        model.finish_tool("file_write", True, "3 lines")
        glyph, accent, text = model.tool_rows()[0]
        assert (glyph, accent) == TOOL_GLYPHS["done"]
        assert "3 lines" in text

    def test_finished_failure_is_red_cross(self):
        model = ActivityModel()
        model.start_tool("run_tests")
        model.finish_tool("run_tests", False, "2 failed")
        glyph, accent, _text = model.tool_rows()[0]
        assert (glyph, accent) == TOOL_GLYPHS["failed"]

    def test_label_falls_back_to_the_tool_name(self):
        model = ActivityModel()
        model.start_tool("get_time")
        assert model.tool_rows()[0][2] == "get_time"

    def test_same_tool_twice_replaces_rather_than_duplicating(self):
        model = ActivityModel()
        model.start_tool("file_read")
        model.start_tool("file_read")
        assert len(model.tools) == 1

    def test_rows_are_capped(self):
        model = ActivityModel()
        for index in range(MAX_TOOL_ROWS + 5):
            model.start_tool(f"tool_{index}")
        assert len(model.tools) == MAX_TOOL_ROWS
        # The most recent ones survive.
        assert model.tools[-1].name == f"tool_{MAX_TOOL_ROWS + 4}"

    def test_active_tool_is_none_when_everything_finished(self):
        model = ActivityModel()
        model.start_tool("a")
        model.finish_tool("a", True)
        assert model.active_tool is None

    def test_active_tool_is_the_latest_running_one(self):
        model = ActivityModel()
        model.start_tool("a")
        model.start_tool("b")
        assert model.active_tool == "b"

    def test_progress_before_started_keeps_the_row_honest(self):
        # A progress frame can beat its started frame across threads.
        model = ActivityModel()
        model.progress_tool("file_write", "halfway")
        assert len(model.tools) == 1
        assert model.tools[0].summary == "halfway"

    def test_finish_without_start_records_it_anyway(self):
        model = ActivityModel()
        row = model.finish_tool("missed_tool", True, "done")
        assert row is not None
        assert row.status == "done"
        assert len(model.tools) == 1


class TestFeedState:
    def test_empty_until_something_happens(self):
        assert ActivityModel().empty is True

    def test_plan_makes_it_non_empty(self):
        model = ActivityModel()
        model.add_plan(["a"])
        assert model.empty is False

    def test_typing_alone_is_not_rows_but_is_not_empty_flagged(self):
        # ``empty`` describes rows; the feed shows dots separately.
        model = ActivityModel()
        model.set_typing(True)
        assert model.rows() == []
        assert model.empty is True

    def test_rows_are_plan_then_tools(self):
        model = ActivityModel()
        model.add_plan(["step"])
        model.start_tool("t")
        rows = model.rows()
        assert len(rows) == 2
        assert rows[0][0] == PLAN_GLYPH[0]

    def test_reset_clears_everything(self):
        model = ActivityModel()
        model.add_plan(["step"])
        model.start_tool("t")
        model.set_typing(True)
        model.reset()
        assert model.empty is True
        assert model.typing is False
        assert model.rows() == []


class TestAccentHex:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("cyan", theme.CYAN),
            ("green", theme.GREEN),
            ("amber", theme.AMBER),
            ("red", theme.RED),
            ("violet", theme.VIOLET),
        ],
    )
    def test_known_accents(self, name, expected):
        assert accent_hex(name) == expected

    def test_unknown_accent_falls_back_to_a_readable_token(self):
        # A literal like "red2" would raise in Tk; the mute token cannot.
        assert accent_hex("not_a_real_accent") == theme.TEXT_MUTE
