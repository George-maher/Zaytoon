"""The command-center design tokens, asserted by name.

``app/theme.py`` is the single source for every colour, radius, spacing step
and type size the shell draws with. Two layers coexist there on purpose (see
the module docstring): layer 1 is pinned to ``frontend/src/styles/theme.css``
by ``test_design_parity.py`` and must not move, layer 2 is the redesign.

These tests pin **layer 2** — the values the redesigned widgets read — so a
widget cannot silently fall back to a hard-coded colour when a token is
renamed out from under it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import theme  # noqa: E402

#: Every colour token the redesign specifies, with its required value.
# A literal table, and it has to stay one: deriving these values from `theme`
# would restate the module's own definitions and make the test unbreakable. The
# point is that the *restyle* is recorded twice, here and in
# `frontend/src/styles/theme.css`, and this file is the half that was written by
# hand. It disagreed with `test_design_parity.py` about CYAN for as long as the
# shell had been magenta — one pin saying `#22d3ee`, the other comparing the CSS
# `#d946ef` — and only the Windows leg ran either.
REQUIRED_COLORS = {
    "BG0": "#010812",
    "BG1": "#020d1a",
    "BG2": "#071428",
    "HAIRLINE": "#0b1d30",
    "BORDER_STRONG": "#144e63",
    "TEXT_BASE": "#c6d4e7",
    "TEXT_MUTE": "#6b809c",
    "TEXT_FAINT": "#6b809c",
    # The accent is the Figma reference's system cyan. The name `CYAN` and the
    # `--hud-cyan` token both outlived two colour migrations, and
    # `test_design_parity` pins them together, so the name stays and the value
    # moved with the stylesheet.
    "CYAN": "#00f5ff",
    "GREEN": "#22c55e",
    "AMBER": "#f59e0b",
    "RED": "#ec4899",
    "VIOLET": "#a855f7",
}

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


class TestRequiredTokens:
    @pytest.mark.parametrize("name", sorted(REQUIRED_COLORS))
    def test_token_exists(self, name):
        assert hasattr(theme, name), f"theme.{name} is missing"

    @pytest.mark.parametrize("name,expected", sorted(REQUIRED_COLORS.items()))
    def test_token_value(self, name, expected):
        assert getattr(theme, name).lower() == expected

    @pytest.mark.parametrize("name", sorted(REQUIRED_COLORS))
    def test_token_is_a_six_digit_hex(self, name):
        assert HEX.match(getattr(theme, name)), name


class TestRadiusAndSpacing:
    def test_radius_scale_is_16_12_2(self):
        # The Figma reference rounds its tiles and dock; the scale moved from
        # 8/6 to 16/12 in step with frontend theme.css --radius-panel/card.
        assert theme.RADIUS_PANEL == 16
        assert theme.RADIUS_CARD == 12
        assert theme.RADIUS_CONTROL == 2

    def test_radius_decreases_as_the_element_gets_smaller(self):
        assert theme.RADIUS_PANEL > theme.RADIUS_CARD > theme.RADIUS_CONTROL

    def test_legacy_square_chrome_is_untouched(self):
        # Pinned to theme.css by test_design_parity; the redesign must not
        # reach into layer 1 to "fix" the square look.
        assert theme.RADIUS == 0
        assert theme.RADIUS_SM <= 2

    def test_spacing_scale_is_4_8_12_16_24_32(self):
        assert theme.SPACING == (4, 8, 12, 16, 24, 32)
        assert (theme.SPACE_1, theme.SPACE_2, theme.SPACE_3) == (4, 8, 12)
        assert (theme.SPACE_4, theme.SPACE_5, theme.SPACE_6) == (16, 24, 32)

    def test_spacing_is_strictly_increasing(self):
        assert list(theme.SPACING) == sorted(set(theme.SPACING))


class TestTypography:
    def test_fonts_are_the_two_required_families(self):
        assert theme.FONT_MONO == "Consolas"
        assert theme.FONT_SANS == "Segoe UI"

    def test_type_sizes_sit_in_their_declared_bands(self):
        for size in (theme.FONT_BODY_SM, theme.FONT_BODY, theme.FONT_BODY_LG):
            assert 11 <= size <= 13, size
        for size in (theme.FONT_HEADER_SM, theme.FONT_HEADER, theme.FONT_HEADER_LG):
            assert 18 <= size <= 22, size
        for size in (theme.FONT_SECTION, theme.FONT_SECTION_LG):
            assert 10 <= size <= 11, size

    def test_letterspace_spreads_an_uppercase_label(self):
        assert theme.letterspace("SECTION") == "S E C T I O N"
        assert theme.letterspace("SECTION", 0) == "SECTION"


class TestAccents:
    def test_five_accents_and_no_more(self):
        assert set(theme.ACCENTS) == {"cyan", "green", "amber", "red", "violet"}

    def test_every_accent_is_a_valid_hex(self):
        for name, value in theme.ACCENTS.items():
            assert HEX.match(value), name

    def test_pill_recipe_is_border_plus_tinted_background(self):
        background, border, text = theme.pill_colors(theme.CYAN)
        assert border == theme.HAIRLINE
        assert text == theme.CYAN
        assert HEX.match(background)
        # A 12% tint must not read as the accent itself.
        assert background.lower() != theme.CYAN.lower()


class TestColourMaths:
    def test_mix_endpoints(self):
        assert theme.mix("#000000", "#ffffff", 0.0) == "#000000"
        assert theme.mix("#000000", "#ffffff", 1.0) == "#ffffff"

    def test_mix_is_clamped_not_extrapolated(self):
        assert theme.mix("#000000", "#ffffff", -5) == "#000000"
        assert theme.mix("#000000", "#ffffff", 5) == "#ffffff"

    def test_mix_is_midpoint_at_half(self):
        # 127.5 rounds half-to-even in Python, so the answer is #808080.
        assert theme.mix("#000000", "#ffffff", 0.5) == "#808080"

    def test_tint_composites_over_the_given_surface(self):
        assert theme.tint("#ff0000", 0.0, "#000000") == "#000000"
        assert theme.tint("#ff0000", 1.0, "#000000") == "#ff0000"

    def test_bad_colour_is_rejected_loudly(self):
        with pytest.raises(ValueError):
            theme._hex_to_rgb("#12345")


class TestDensity:
    def test_density_defaults_to_normal(self, monkeypatch):
        monkeypatch.delenv(theme.DENSITY_ENV, raising=False)
        assert theme.density() == "normal"
        assert theme.density_scale() == 1.0

    def test_compact_density_tightens_padding(self, monkeypatch):
        monkeypatch.setenv(theme.DENSITY_ENV, "compact")
        assert theme.density() == "compact"
        assert theme.density_scale() < 1.0

    def test_nonsense_density_falls_back(self, monkeypatch):
        monkeypatch.setenv(theme.DENSITY_ENV, "huge")
        assert theme.density() == "normal"


class TestMotionBudget:
    def test_frame_budget_is_a_real_ceiling(self):
        assert theme.FRAME_MS == 30
        assert theme.ANIM_FPS_MAX == 30
        assert 1000 // theme.ANIM_FPS_MAX >= theme.FRAME_MS

    def test_transitions_read_as_sub_quarter_second(self):
        assert 0 < theme.T_FAST_MS <= theme.T_SLOW_MS <= 250


class TestStateTable:
    def test_every_hud_state_has_hue_glyph_and_rate(self):
        for name, (hue, glyph, rate, _pulse) in theme.STATES.items():
            assert HEX.match(hue), name
            assert glyph, name
            assert rate >= 0.0, name

    def test_rate_zero_means_no_sweep(self):
        # Offline must not sweep: a dead transport showing a live sweep is a lie.
        assert theme.STATES["offline"][2] == 0.0

    def test_status_text_is_available_for_every_state(self):
        for name in theme.STATES:
            assert theme.status_text(name)
