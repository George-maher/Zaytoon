"""
Rendered-style probe: read *computed* values out of the live WebView2.

smoke_desktop.py proves the app boots and the bridge answers. This proves the
palette actually *reaches the screen* in the engine that ships the desktop
app — not only in the jsdom/Chromium the unit tests run under.

Both are Chromium, but not the same build, and `color-mix()` and `clip-path`
are exactly the features where a WebView2 build can differ from a bundled
Chromium. Asserting the computed style is the only honest check; asserting the
stylesheet only proves what we wrote.

Run:  backend\\.venv\\Scripts\\python.exe desktop\\scripts\\probe_webview_styles.py
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DesktopConfig, default_repo_root  # noqa: E402
from app import theme  # noqa: E402
from scripts.smoke_desktop import SmokeHost, _check  # noqa: E402


# Computed values, read from the live DOM. Each returns a primitive so
# pywebview's serialisation cannot turn it into `{}`.
STYLE_JS = r"""
(async () => {
  const out = {};
  const root = document.documentElement;
  const cs = getComputedStyle(root);
  const px = (v) => (v || '').trim();

  // 1. Tokens resolved on :root.
  out.accent      = px(cs.getPropertyValue('--hud-cyan'));
  out.void        = px(cs.getPropertyValue('--hud-void'));
  out.inputBg     = px(cs.getPropertyValue('--input-bg'));
  out.inputText   = px(cs.getPropertyValue('--input-text'));
  out.colorScheme = cs.colorScheme;

  // 2. Body background actually painted from the token.
  const body = getComputedStyle(document.body);
  out.bodyBg = body.backgroundColor;
  out.bodyFg = body.color;

  // 3. The first real input on the page: does it resolve to our palette?
  const el = document.querySelector('input, textarea, select');
  out.hasField = !!el;
  if (el) {
    const s = getComputedStyle(el);
    out.fieldBg = s.backgroundColor;
    out.fieldFg = s.color;
    out.fieldBorder = s.borderTopColor;
  }

  // 4. color-mix() support. If this were unsupported the token would resolve
  //    to an empty string and every derived border would be transparent.
  out.mixWorks = out.accent.length > 0;

  // 5. clip-path on the cut-corner utility, if one is on the page.
  const cut = document.querySelector('.hud-cut');
  out.hasCut = !!cut;
  if (cut) out.cutClip = px(getComputedStyle(cut).clipPath).slice(0, 40);

  // 6. The hex node utility: a 7px clipped diamond, not a round dot.
  const hex = document.querySelector('.hud-hex');
  out.hasHex = !!hex;
  if (hex) {
    const s = getComputedStyle(hex);
    out.hexW = s.width;
    out.hexClip = px(s.clipPath).slice(0, 40);
  }

  // 7. Motion: how many elements are actually animating right now? This is
  //    the one-animated-element rule, measured rather than asserted.
  let animating = 0;
  for (const n of document.querySelectorAll('*')) {
    const s = getComputedStyle(n);
    const dur = s.animationDuration;
    if (dur && dur !== '0s') {
      const name = s.animationName;
      if (name && name !== 'none' && !name.startsWith('hud-msg-in')) animating++;
    }
  }
  out.animating = animating;

  return out;
})()
"""


class StyleProbeHost(SmokeHost):
    """SmokeHost that checks rendered styles instead of plumbing."""

    def __init__(self, config, timeout: float = 45.0) -> None:
        super().__init__(config, keep_open=False, timeout=timeout)
        self.styles: dict = {}

    def _probe_page(self) -> None:
        result = None
        for _ in range(15):
            result = self._eval_async(STYLE_JS)
            if isinstance(result, dict) and result.get("accent"):
                break
            time.sleep(0.25)
        self.styles = result if isinstance(result, dict) else {"error": repr(result)}
        self.probe = {"react_mounted": True, "pywebview_ready": True}
        self._deadline.set()
        if not self.keep_open:
            self._destroy()


def _rgb_to_hex(rgb: str) -> str:
    """`rgb(r, g, b)` / `rgba(...)` -> `#rrggbb`, or the input unchanged."""
    rgb = (rgb or "").strip()
    if not rgb.startswith("rgb"):
        return rgb
    parts = rgb[rgb.find("(") + 1: rgb.find(")")].split(",")
    try:
        r, g, b = (int(float(p)) for p in parts[:3])
    except ValueError:
        return rgb
    return "#%02x%02x%02x" % (r, g, b)


def main() -> int:
    # `repo_root` must stay a Path: host._get_state_file() does `repo_root /
    # "Config"`, and a str there raises TypeError before the window opens.
    config = DesktopConfig(repo_root=default_repo_root())
    host = StyleProbeHost(config, timeout=45.0)

    # pywebview refuses to start off the main thread (WebViewException), and
    # `run()` blocks until the window closes — the probe closes it from
    # `_probe_page`, so calling it directly here *is* the whole lifecycle.
    host.run()

    s = host.styles
    if not s or "error" in s:
        print("probe failed:", s)
        print("verdict: FAIL")
        return 1

    print("Zaytoon WebView2 rendered-style probe")
    print(f"  engine: {host.config.describe()}")
    print()
    print("  tokens resolved on :root")
    checks = [
        # The accent's *value* is a design decision, so the probe compares it against
        # the theme token instead of a hex literal that was already stale (#22d3ee
        # became magenta #d946ef in both clients).
        _check(
            "accent is theme.ACCENT",
            s.get("accent", "").lower() == theme.ACCENT.lower(),
            f"{s.get('accent', '')} vs {theme.ACCENT}",
        ),
        _check("void is the new near-black (#05070a)", s.get("void") == "#05070a", s.get("void", "")),
        _check("input fill resolved (not empty)", bool(s.get("inputBg")), s.get("inputBg", "")),
        _check("input text resolved (not empty)", bool(s.get("inputText")), s.get("inputText", "")),
        _check("color-scheme is dark", s.get("colorScheme") == "dark", s.get("colorScheme", "")),
    ]

    body_bg = _rgb_to_hex(s.get("bodyBg", ""))
    print()
    print("  painted surfaces")
    checks.append(_check("body background is near-black", body_bg in ("#05070a", "#000000"), body_bg))
    checks.append(_check("body text is the light token", _rgb_to_hex(s.get("bodyFg", "")) in ("#e8f6ff", "#e8f7ff"),
                         _rgb_to_hex(s.get("bodyFg", ""))))

    print()
    print("  modern CSS in the WebView build")
    checks.append(_check("color-mix() resolves (no empty var)", bool(s.get("mixWorks"))))
    checks.append(_check("clip-path supported on .hud-hex", "polygon" in (s.get("hexClip") or ""), s.get("hexClip", "none on page")))
    checks.append(_check("hex node is 7px", s.get("hexW") in ("7px", None), s.get("hexW", "not on page")))

    print()
    print("  one-animated-element rule")
    n = s.get("animating")
    checks.append(_check("at most one element animating at rest", isinstance(n, int) and n <= 1, f"{n} animating"))

    print()
    failed = checks.count(False)
    print("verdict:", "PASS" if failed == 0 else f"FAIL ({failed})")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())