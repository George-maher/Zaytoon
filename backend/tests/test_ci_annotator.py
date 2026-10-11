"""The CI annotator must annotate — silently and honestly.

`.github/ci_annotate.py` exists because a job's log body can be unreachable
while its annotations are not, so the whole diagnosis path for the macOS and
Windows legs rests on this plugin emitting well-formed workflow commands. These
tests run pytest over a throwaway module with the plugin loaded; the two
properties that matter are that every failure produces exactly one `::error`
line, and that nothing about the run's *result* changes.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ANNOTATOR_DIR = Path(__file__).resolve().parents[2] / ".github"
PLUGIN = ANNOTATOR_DIR / "ci_annotate.py"


pytestmark = pytest.mark.skipif(
    not PLUGIN.exists(), reason="the CI annotator is not part of this checkout"
)


def _run(tmp_path: Path, source: str) -> subprocess.CompletedProcess:
    (tmp_path / "test_subject.py").write_text(source, encoding="utf-8")
    # `dict(**os.environ, PYTHONPATH=...)` raises TypeError when the parent
    # already exports PYTHONPATH — and the CI step that loads this plugin does
    # exactly that, so a test of the CI diagnostics became a red leg by itself.
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ANNOTATOR_DIR)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "test_subject.py",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            "-p",
            "ci_annotate",
        ],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_a_failing_test_is_annotated_with_its_file_and_line(tmp_path):
    result = _run(
        tmp_path,
        "def test_it():\n    assert 1 == 2, 'the reason'\n",
    )
    lines = [line for line in result.stdout.splitlines() if line.startswith("::error")]
    assert len(lines) == 1, result.stdout
    assert "file=test_subject.py" in lines[0], lines[0]
    assert "line=2" in lines[0], lines[0]
    assert "title=call%3A test_subject.py%3A%3Atest_it" in lines[0], lines[0]
    # The assertion's own words survive, so the annotation is enough to fix it.
    assert "AssertionError" in lines[0] and "the reason" in lines[0]


def test_a_green_run_reports_only_the_host_notice(tmp_path):
    result = _run(tmp_path, "def test_it():\n    assert True\n")
    assert result.returncode == 0, result.stdout
    assert "::error" not in result.stdout
    assert "::notice title=Zaytoon CI host::platform=" in result.stdout
    assert "python=" in result.stdout


def test_a_collection_error_is_annotated(tmp_path):
    result = _run(tmp_path, "import zaytoon_module_that_does_not_exist\n")
    assert result.returncode != 0
    assert any(
        line.startswith("::error") and "collect" in line for line in result.stdout.splitlines()
    ), result.stdout


def test_a_failure_cannot_forge_a_second_annotation(tmp_path):
    """Escaping is the security property of this plugin.

    An assertion message is model- and user-controlled text on a path that ends
    up rendered in the Actions UI. If a newline inside it could close the command
    and open a new one, a test failure would be an injection point into the build
    log — including `title=` and any other parameter.
    """
    hostile = (
        "def test_it():\n"
        "    assert False, 'first\\n::error title=pwned::injected\\nend'\n"
    )
    result = _run(tmp_path, hostile)
    error_lines = [line for line in result.stdout.splitlines() if line.startswith("::error")]
    assert len(error_lines) == 1, result.stdout
    assert "pwned" in error_lines[0], "the text is reported, escaped rather than dropped"
    # The mechanism, not just the symptom: the embedded newline is data (`%0A`),
    # so it cannot open a line for the runner's line-anchored matcher to parse.
    assert "%0A" in error_lines[0]
    assert result.stdout.count("\n::error") + result.stdout.startswith("\n::error") >= 1
    assert len([1 for line in result.stdout.splitlines() if line.startswith("::error")]) == 1
    assert "%0A" in error_lines[0]


def test_the_cap_bounds_a_cascading_failure(tmp_path):
    source = "".join(
        f"def test_fail_{index}():\n    assert False, 'boom {index}'\n"
        for index in range(60)
    )
    result = _run(tmp_path, source)
    errors = [line for line in result.stdout.splitlines() if line.startswith("::error")]
    assert len(errors) == 40, len(errors)
