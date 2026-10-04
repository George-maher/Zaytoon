"""Layout and transcript helpers — pure decisions, tested as data.

The panel's responsive behaviour (which columns exist, how wide they are, how
far text wraps) and the per-role row styling are computed by module-level
functions rather than inside ``<Configure>`` handlers, precisely so the resize
behaviour can be asserted without moving a real window.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import panel, theme  # noqa: E402


class TestComputeWrap:
    def test_default_width_uses_the_base(self):
        # 560 base with a 36 margin: a default window wraps at the base.
        assert panel.compute_wrap(theme.WRAP_BASE + theme.WRAP_MARGIN) == theme.WRAP_BASE

    def test_wide_window_is_bounded(self):
        # A very wide window must not produce unreadably long lines.
        assert panel.compute_wrap(4000) == theme.WRAP_BASE

    def test_narrow_window_has_a_floor(self):
        # A very narrow window must not collapse text into a one-word column.
        assert panel.compute_wrap(50) == theme.WRAP_MIN

    def test_scales_down_with_the_container(self):
        wide = panel.compute_wrap(800)
        narrow = panel.compute_wrap(500)
        assert narrow < wide

    def test_is_monotonic(self):
        widths = [300, 500, 700, 900, 1400]
        wraps = [panel.compute_wrap(w) for w in widths]
        assert wraps == sorted(wraps)


class TestLayoutPlan:
    def test_floor_width_is_transcript_only(self):
        # 420 is the documented minimum window size.
        plan = panel.layout_plan(420)
        assert plan.core is False
        assert plan.side is False
        assert plan.core_width == 0
        assert plan.side_width == 0

    def test_mid_width_gains_the_core(self):
        plan = panel.layout_plan(760)
        assert plan.core is True
        assert plan.side is False
        assert plan.core_width == panel.CORE_WIDTH
        assert plan.side_width == 0

    def test_wide_width_gains_both(self):
        plan = panel.layout_plan(1200)
        assert plan.core is True
        assert plan.side is True
        assert plan.side_width > 0

    def test_thresholds_are_the_documented_ones(self):
        assert panel.CORE_MIN_WIDTH == 640
        assert panel.SIDE_MIN_WIDTH == 900
        assert panel.COMPACT_WIDTH == 700
        assert panel.CORE_MIN_WIDTH < panel.SIDE_MIN_WIDTH

    def test_just_below_and_at_each_threshold(self):
        assert panel.layout_plan(panel.CORE_MIN_WIDTH - 1).core is False
        assert panel.layout_plan(panel.CORE_MIN_WIDTH).core is True
        assert panel.layout_plan(panel.SIDE_MIN_WIDTH - 1).side is False
        assert panel.layout_plan(panel.SIDE_MIN_WIDTH).side is True

    def test_side_can_be_disabled_regardless_of_width(self):
        # The user's SIDE toggle must win over the width rule.
        assert panel.layout_plan(1600, side_enabled=False).side is False
        assert panel.layout_plan(420, side_enabled=False).side is False

    def test_plan_is_immutable(self):
        plan = panel.layout_plan(760)
        try:
            plan.core = False  # type: ignore[misc]
        except AttributeError:
            pass  # frozen dataclass did its job
        else:  # pragma: no cover - a mutable plan would drift
            raise AssertionError("LayoutPlan must be frozen")


class TestRowStyle:
    def test_user_rows_align_right(self):
        style = panel.row_style("user")
        assert style.align == "right"
        assert style.anchor == "e"
        assert style.spine == theme.CYAN

    def test_assistant_rows_align_left_with_the_violet_edge(self):
        style = panel.row_style("assistant")
        assert style.align == "left"
        assert style.spine == theme.VIOLET

    def test_error_rows_are_red(self):
        style = panel.row_style("error")
        assert style.spine == theme.RED

    def test_system_rows_are_muted(self):
        style = panel.row_style("system")
        assert style.spine == theme.TEXT_FAINT
        assert style.text == theme.TEXT_MUTE

    def test_unknown_role_degrades_to_system(self):
        # An unknown role must not raise mid-stream.
        assert panel.row_style("wat") == panel.row_style("system")

    def test_every_role_has_a_six_field_style(self):
        for role in ("user", "assistant", "system", "error"):
            style = panel.row_style(role)
            assert set(style.__dataclass_fields__) == {
                "align", "anchor", "spine", "background", "text", "justify"
            }
            for value in style.__dataclass_fields__:
                assert getattr(style, value) != ""

    def test_colours_are_theme_tokens(self):
        for role in ("user", "assistant", "system", "error"):
            style = panel.row_style(role)
            for value in (style.spine, style.background, style.text):
                assert value.startswith("#"), (role, value)

    def test_justify_agrees_with_alignment(self):
        for role in ("user", "assistant", "system", "error"):
            style = panel.row_style(role)
            assert style.justify == style.align, role


class TestQuickActions:
    def test_quick_actions_are_phrases_the_router_knows(self):
        # These are sent verbatim as chat messages, so they must be ordinary
        # command phrasings rather than desktop-specific jargon.
        labels = [label for label, _message in panel.QUICK_ACTIONS]
        assert labels == ["RUN TESTS", "GIT STATUS", "SYSTEM INFO"]
        for _label, message in panel.QUICK_ACTIONS:
            assert message == message.strip()
            assert message
