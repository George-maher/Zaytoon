"""pytest plugin: turn failures into GitHub workflow commands, so they are readable.

Why this exists: a Actions job writes its log to a host that some environments
cannot reach (the log endpoint redirects off-host), and the *only* failure detail
that survives is a check annotation — `Process completed with exit code 1`. That
made the macOS and Windows legs of this workflow undiagnosable from an agent
sandbox, which is how three red legs sat for a day. Annotations come back from
`/repos/{owner}/{repo}/check-runs/{id}/annotations`, so a plugin that prints
`::error …` lines converts "run the CI again and hope" into one API call.

The plugin only ever *adds* output. It changes no test result, no exit code, and
nothing about which tests run — an annotation-only diagnosis path, so it cannot
hide a failure by describing it.
"""

from __future__ import annotations

import os
import platform
import sys

#: Enough to name every distinct breakage in one pass, small enough that a
#: cascading failure does not flood the check run (GitHub trims past ~50).
MAX_ANNOTATIONS = 40

_emitted = 0


def _escape_data(text: str) -> str:
    """Escape the message body of a workflow command."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_param(text: str) -> str:
    """Escape a `key=value` parameter — commas and colons break the parser."""
    return (
        _escape_data(text)
        .replace(",", "%2C")
        .replace(":", "%3A")
    )


def _emit(command: str, params: dict, message: str) -> None:
    rendered = ",".join(f"{key}={_escape_param(str(value))}" for key, value in params.items())
    line = f"::{command} {rendered}::{_escape_data(message)}" if rendered else f"::{command}::{_escape_data(message)}"
    # The leading newline is not cosmetic: with `pytest -q` the reporter writes
    # progress characters to the line as tests finish, so a command written at an
    # arbitrary moment lands *after* a dot — `F::error file=…` — and the runner's
    # line-anchored matcher never sees it. The diagnostic would then exist in the
    # log and be invisible in the API, which is the exact failure mode this file
    # exists to remove.
    sys.stdout.write("\n" + line + "\n")
    sys.stdout.flush()


def _detail(report) -> tuple:
    """`(message, path, line)` for a report, preferring the crisp crash line."""
    longrepr = getattr(report, "longrepr", None)
    reprcrash = getattr(longrepr, "reprcrash", None)
    if reprcrash is not None and getattr(reprcrash, "message", ""):
        return str(reprcrash.message), str(reprcrash.path or ""), int(reprcrash.lineno or 0)
    text = str(longrepr or "").strip()
    if not text:
        return "<no detail reported>", "", 0
    tail = [line for line in text.splitlines() if line.strip()]
    return tail[-1], "", 0


def pytest_sessionstart(session) -> None:
    """One line of host fingerprint, so a host-specific failure is attributable.

    `platform.system()` is what the parity code branches on; recording it next to
    the Python and pytest versions turns "fails only on the mac leg" into a fact
    about that runner rather than a guess about ours.
    """
    import pytest

    version = pytest.__version__
    _emit(
        "notice",
        {"title": "Zaytoon CI host"},
        (
            f"platform={platform.system()}/{platform.release()}/{platform.machine()} "
            f"python={sys.version.split()[0]} pytest={version} cwd={os.getcwd()}"
        ),
    )


def pytest_runtest_logreport(report) -> None:
    global _emitted
    if not report.failed or _emitted >= MAX_ANNOTATIONS:
        return
    _emitted += 1
    message, path, line = _detail(report)
    location = getattr(report, "location", None) or ("", 0, "")
    if path:
        # `reprcrash.path` is absolute; annotations are most useful relative to
        # the checkout, which is also what GitHub links a file:line against.
        try:
            candidate = os.path.relpath(path, os.getcwd())
        except ValueError:  # different drive on Windows
            candidate = path
        if not candidate.startswith(".."):
            path = candidate
    _emit(
        "error",
        {
            "file": path or location[0] or "<unknown>",
            "line": line or (location[1] if location[1] else 0) or 0,
            "title": f"{report.when}: {report.nodeid}",
        },
        message[:400],
    )


def pytest_collectreport(report) -> None:
    """A module that will not import is the one failure a traceback cannot show."""
    global _emitted
    if not report.failed or _emitted >= MAX_ANNOTATIONS:
        return
    _emitted += 1
    message, _, _ = _detail(report)
    _emit(
        "error",
        {"file": str(report.fspath or "<unknown>"), "title": "collect: import or fixture error"},
        f"{report.nodeid or ''} {message}"[:400],
    )
