"""The final verification script must report, on any machine, or not at all.

`scripts/verify_final.py` ends with `sys.exit(0 if ok else 1)`, which only means
something if every section actually ran. It did not: `build_chain("fast")` raises
`LLMError` when the configured provider has no credentials, which is the state of
a fresh clone (no `backend/.env`) and of CI — so the script died in section 1 with
a traceback and its voice-routing, sandbox and path-containment checks never
executed. A red X that verifies nothing is worse than a skipped check that says
what it skipped.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = BACKEND_ROOT / "scripts" / "verify_final.py"


@pytest.mark.skipif(not SCRIPT.exists(), reason="the verifier is not present")
def test_it_finishes_when_no_provider_is_configured(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path), TMPDIR=str(tmp_path))
    env.pop("OPENROUTER_API_KEY", None)  # a fresh clone has no key
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=str(BACKEND_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    out = result.stdout
    assert result.returncode == 0, (out + result.stderr)[-600:]
    assert "Traceback" not in result.stderr, result.stderr[-400:]
    # The last section ran. Before the fix, nothing after section 1 did.
    assert "sibling prefix rejected" in out
    assert "RESULT: ALL CHECKS PASSED" in out


def test_a_relaxed_policy_still_fails_the_verifier(tmp_path):
    """Skipping what cannot be measured must not have become a way to pass.

    The verifier reads the environment the same way the app does, so pointing
    `OPENROUTER_FALLBACK_MODEL` at the auto-router through the environment — the
    exact regression the guard exists for — has to come back non-zero.
    """
    if not SCRIPT.exists():
        pytest.skip("the verifier is not present")
    env = dict(
        os.environ,
        HOME=str(tmp_path),
        TMPDIR=str(tmp_path),
        OPENROUTER_FALLBACK_MODEL="openrouter/free",
    )
    env.pop("OPENROUTER_API_KEY", None)
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=str(BACKEND_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode != 0, result.stdout[-300:]
    out = result.stdout
    assert "FAIL] openrouter/free is NOT configured as a fallback" in out
    assert "RESULT: FAILURES PRESENT" in out
    # and it still finished, rather than dying at the same point the old version did
    assert "sibling prefix rejected" in out
