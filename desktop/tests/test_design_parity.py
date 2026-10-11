"""The two clients must not drift apart on visual language.

The desktop shell and the browser are separate codebases in separate languages,
so nothing but a test stops the desktop palette from quietly becoming its own
thing again -- which is exactly what happened: the shell drifted to a warmer,
greyer theme with rounded pill chrome while the web moved to a flat cybercore
HUD. The user is supposed to recognise them as the same product.

These tests read ``frontend/src/styles/theme.css`` and compare it with
``app/theme.py``. They are cheap and they fail loudly on drift.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import theme  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
THEME_CSS = REPO / "frontend" / "src" / "styles" / "theme.css"
# `components/HUD`, capitalised. Lower-case here worked on the only runner that
# ever ran this suite — Windows, where path casing is not significant — and
# raised FileNotFoundError on Linux and macOS. `frontend/src/imports.case.test.js`
# pins the same rule for the app's own imports; this is that bug in a test.
HUD_STATE = REPO / "frontend" / "src" / "components" / "HUD" / "hudState.js"


@pytest.fixture(scope="module")
def css() -> str:
    assert THEME_CSS.exists(), f"missing design tokens: {THEME_CSS}"
    return THEME_CSS.read_text(encoding="utf-8")


def css_var(text: str, name: str) -> str:
    match = re.search(rf"--{re.escape(name)}:\s*(#[0-9A-Fa-f]{{6}})\s*;", text)
    assert match, f"token --{name} not found in theme.css"
    return match.group(1).lower()


class TestPaletteParity:
    @pytest.mark.parametrize(
        "css_name,py_name",
        [
            ("hud-void", "VOID"),
            ("hud-deep", "DEEP"),
            ("hud-raised", "RAISED"),
            ("hud-line", "LINE"),
            ("hud-cyan", "CYAN"),
            ("hud-cyan-core", "CYAN_CORE"),
            ("hud-blue", "BLUE"),
            ("hud-indigo", "INDIGO"),
            ("hud-amber", "AMBER"),
            ("hud-green", "GREEN"),
            ("hud-red", "RED"),
            ("hud-text", "TEXT"),
            ("hud-text-dim", "TEXT_DIM"),
            ("hud-text-mute", "TEXT_MUTED"),
        ],
    )
    def test_token_matches(self, css, css_name, py_name):
        assert css_var(css, css_name) == getattr(theme, py_name).lower(), (
            f"--{css_name} ({css_var(css, css_name)}) != theme.{py_name} "
            f"({getattr(theme, py_name)}): the clients have drifted"
        )

    def test_typography_and_state_names_are_documented(self, css):
        # The shared spec is the contract both clients claim to implement.
        spec = REPO / "docs" / "DESIGN_SYSTEM.md"
        assert spec.exists(), "docs/DESIGN_SYSTEM.md is the shared spec; it is missing"
        text = spec.read_text(encoding="utf-8")
        for token in ("--hud-cyan", "--hud-void", "prefers-reduced-motion"):
            assert token in text or token in css, f"{token} is undocumented"
        # Every state word the clients use must be spelled out in the spec, so
        # the vocabulary has a written home and not just two implementations.
        for word in ("READY", "LISTENING", "PROCESSING", "SPEAKING", "OFFLINE"):
            assert word in text, f"state word {word} is not in the design spec"


def _block(source: str, name: str) -> str:
    """Slice out one top-level object literal from the JS source.

    A naive `^  key: {` regex also matches the keys of HUES, which is how
    'cyan'/'amber' ended up being read as states. Only the named block is
    considered.
    """
    start = source.index(f"export const {name} = {{")
    rest = source[start:]
    end = rest.index("\n};")
    return rest[:end]


class TestStateVocabularyParity:
    """Same state names, same glyphs, same words, in both clients."""

    def test_every_web_state_exists_on_the_desktop(self):
        block = _block(HUD_STATE.read_text(encoding="utf-8"), "STATES")
        web_states = set(re.findall(r'^\s{2}(\w+):\s*\{', block, re.M))
        assert len(web_states) >= 10, f"parsed only {web_states} from the web state table"
        missing = web_states - set(theme.STATES)
        assert not missing, f"desktop theme is missing web states: {sorted(missing)}"

    def test_glyphs_match_the_web_table(self):
        block = _block(HUD_STATE.read_text(encoding="utf-8"), "STATES")
        entries = dict(re.findall(r'^\s{2}(\w+):\s*\{[^}]*?glyph:\s*"(.+?)"', block, re.M))
        assert entries, "could not parse glyphs out of hudState.js"
        for name, glyph in entries.items():
            assert theme.state_glyph(name) == glyph, (
                f"state {name}: web glyph {glyph!r} != desktop {theme.state_glyph(name)!r}"
            )

    def test_state_words_match(self):
        block = _block(HUD_STATE.read_text(encoding="utf-8"), "STATES")
        entries = dict(re.findall(r'^\s{2}(\w+):\s*\{\s*word:\s*"(.+?)"', block, re.M))
        assert entries, "could not parse state words out of hudState.js"
        for name, word in entries.items():
            assert theme.status_text(name) == word, (
                f"state {name}: web word {word!r} != desktop {theme.status_text(name)!r}"
            )

    def test_resolve_maps_transport_names_onto_hud_names(self):
        # The protocol says "connected"/"awaiting_confirmation"; the HUD says
        # "idle"/"confirming". Normalising in one place stops the orb and the
        # web core disagreeing about what state the assistant is in.
        assert theme.resolve("connected") in theme.STATES
        assert theme.resolve("awaiting_confirmation") in theme.STATES
        assert theme.resolve("nonsense") == "idle"


class TestChromeGeometry:
    def test_instrument_chrome_is_square(self):
        # Rounded chrome is what made the shell look like a consumer app.
        assert theme.RADIUS == 0
        assert theme.RADIUS_SM <= 2

    def test_no_emoji_in_the_shell(self):
        """Emoji render per-machine and read as consumer UI, not instrumentation."""
        offenders = []
        for path in sorted((REPO / "desktop" / "app").glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), 1):
                if any(ord(ch) > 0x1F000 for ch in line):
                    offenders.append(f"{path.name}:{lineno}")
        assert not offenders, f"emoji in desktop chrome: {offenders}"
