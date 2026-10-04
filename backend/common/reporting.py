"""Centralised, secret-free error reporting.

Every boundary in this codebase (WebSocket handler, agent engine, tool
registry, memory, scheduler, reminder, voice, REST) reports failure through
this module, for one reason: a raw exception is the single most reliable way to
leak a credential into a log file or onto a user's screen. `str(exc)` for an
HTTP client is exactly where the ``Authorization`` header value shows up.

Three functions, each with one job:

======================  ====================================================
:func:`redact`          mask secrets in any text *before* it is stored, logged
                        or handed to a model
:func:`safe_error`      the **internal** boundary: log ONE structured line,
                        return a short message that is safe to show
:func:`client_error`    the **external** boundary: a short bilingual sentence.
                        Never a stack trace, never ``str(exc)``.
======================  ====================================================

The design rule is "report the failure, never the payload": the structured log
line carries the *type* of the exception and a redacted one-line message, and
what the user sees is a human sentence, not an implementation detail.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("Zaytoon.Reporting")

# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
#: ``api_key=...``, ``token: ...``, ``Bearer xyz``, ``password=abc`` and the
#: rest of the word. The match starts at the keyword (no leading ``\b``) so a
#: keyword buried in a longer token — ``OPENROUTER_API_KEY`` — is caught too;
#: over-matching mangles a log line, under-matching leaks a credential.
_SECRET_WORD = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|bearer|authorization)\S*"
)

#: ``keyword <sep> value`` where a *space* separates them. ``_SECRET_WORD``
#: cannot do this: ``\S*`` stops at the space, so ``token: abcdef123456`` was
#: reduced to ``***: abcdef123456`` — the keyword masked, the credential left
#: in place. The value must look like a credential (6+ base64-ish chars), so
#: prose after a keyword such as ``password is required`` is not eaten.
_SECRET_ASSIGN = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|authorization)"
    r"[\s:=]+[A-Za-z0-9_\-.+/=]{6,}"
)

#: ``Authorization: Bearer <jwt>``. Run *before* the keyword passes, which
#: would otherwise mask the word ``Bearer`` and leave the token behind it.
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")

#: Provider-shaped keys: ``sk-...`` (OpenAI/OpenRouter), ``or-...`` (OpenRouter
#: variant), ``ghp-...`` (GitHub) and friends. Requires a long opaque tail so
#: ordinary prose that happens to start with ``or-`` is left alone.
_KEY_LIKE = re.compile(r"\b(?:sk|or|rk|pk|ghp|glpat|xoxp)-[A-Za-z0-9_\-]{8,}")

#: A long opaque final path segment of a URL. This is where webhook secrets
#: live — ``.../webhooks/123456/<token>``, Discord, Slack and CI hooks — and
#: the transport reports them *inside the URL*, so no keyword rule can see
#: them. The segment must be 20+ chars of URL-safe base64 with no dot, which
#: keeps ``.../reports/2026-summary.json`` intact.
_URL_SECRET = re.compile(r"(?i)(https?://[^\s\"'<>]*/)([A-Za-z0-9_\-]{20,})")

#: ``user:password@host`` — credentials embedded in a URL authority.
_URL_USERINFO = re.compile(r"(?i)([a-z][a-z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@")

#: Config / environment names whose *value* is a credential by construction.
_SENSITIVE_NAME = re.compile(
    r"(?i)(?:^|_)(?:API_KEY|KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?"
    r"|AUTHORIZATION|BEARER|AUTH)$"
)

#: Shortest value worth masking. Empty strings and one-character values would
#: otherwise replace half of every message with ``***``.
_MIN_SECRET_LEN = 8


def _sensitive_values() -> list:
    """Current values of credential-shaped config attributes and env vars.

    Computed per call rather than cached: ``POST /settings/ai`` assigns a new
    ``OPENROUTER_API_KEY`` at runtime, and a cache would keep redacting the
    *old* one while the new value went straight into the log.
    """
    values: list = []

    def _consider(name: str, value: Any) -> None:
        if not isinstance(value, str):
            return
        if not _SENSITIVE_NAME.search(name):
            return
        candidate = value.strip()
        if len(candidate) >= _MIN_SECRET_LEN:
            values.append(candidate)

    try:
        import config as config_module

        cfg = getattr(config_module, "config", None)
        for name in dir(cfg if cfg is not None else config_module):
            if name.startswith("__"):
                continue
            try:
                _consider(name, getattr(cfg if cfg is not None else config_module, name))
            except Exception:  # pragma: no cover - defensive
                continue
    except Exception:  # pragma: no cover - config unavailable in a bare test
        pass

    for name, value in os.environ.items():
        _consider(name, value)

    # Longest first, so a value that contains another is masked as a whole.
    values.sort(key=len, reverse=True)
    return values


def redact(text: Any) -> str:
    """Return ``text`` with every known or shape-matching secret replaced.

    Three passes, deliberately in this order:

    1. the literal values of credential-shaped config/env entries (the only
       pass that can catch ``OPENROUTER_API_KEY``'s *value*);
    2. the ``keyword + rest of the word`` pattern;
    3. provider key shapes (``sk-...``).

    ``None`` and empty input come back as ``""`` so callers never have to
    branch.
    """
    if text is None:
        return ""
    result = text if isinstance(text, str) else str(text)
    if not result:
        return ""
    for value in _sensitive_values():
        if value and value in result:
            result = result.replace(value, "***")
    # Order matters: the *value-carrying* patterns run first, because the
    # keyword-only pass would otherwise consume `Bearer`/`token:` and leave
    # the credential that follows it untouched.
    result = _BEARER.sub("***", result)
    result = _SECRET_ASSIGN.sub("***", result)
    result = _SECRET_WORD.sub("***", result)
    result = _KEY_LIKE.sub("***", result)
    # URL rules last: they operate on structure the keyword passes cannot see,
    # and they run after keyword masking so a URL that is *named*
    # (`token=<url>`) is already gone by then.
    result = _URL_USERINFO.sub(r"\1***@", result)
    result = _URL_SECRET.sub(r"\1***", result)
    return result


def _one_line(text: str) -> str:
    """Collapse newlines so one exception stays on one log line."""
    return " ".join(str(text).split())


def _level_for(exc: BaseException) -> int:
    """Log level for an exception.

    Timeouts and transport hiccups are expected in a free-model, flaky-network
    product; logging them at ERROR makes the real errors invisible.
    """
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return logging.WARNING
    return logging.ERROR


# ---------------------------------------------------------------------------
# Internal boundary
# ---------------------------------------------------------------------------
def safe_error(
    exc: BaseException,
    *,
    operation: str,
    component: str,
    detail: str = "",
) -> str:
    """Log **one** structured line for ``exc`` and return a safe message.

    The line is deliberately flat and greppable::

        ERROR op=<operation> comp=<component> type=<ExcClass> ts=<iso> msg=<redacted>

    ``msg`` is ``str(exc)`` with secrets removed and newlines collapsed — it is
    a *log* line, so it may name the file that failed; what this returns is
    separate, and carries only the operation, the exception class and an
    optional caller-supplied detail. That is the difference between
    "understandable in the log" and "safe to repeat to a user".

    ``detail`` is caller-authored (never exception text), so it is safe in both
    places: it appears on the log line as ``detail=`` *and* in the returned
    message. Call sites that only log therefore still keep their context —
    without it, "attempt 2/3" would exist only in a return value nobody reads.
    """
    exc_type = type(exc).__name__
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    message = _one_line(redact(str(exc)))[:400] or exc_type

    logger.log(
        _level_for(exc),
        "ERROR op=%s comp=%s type=%s ts=%s%s msg=%s",
        operation,
        component,
        exc_type,
        ts,
        f" detail={detail}" if detail else "",
        message,
    )

    short = f"{operation} failed: {exc_type}"
    if detail:
        short = f"{operation} failed: {detail} ({exc_type})"
    return short


# ---------------------------------------------------------------------------
# External boundary
# ---------------------------------------------------------------------------
_TIMEOUT = "timeout"
_CONNECTION = "connection"
_GENERIC = "generic"

_CLIENT_MESSAGES = {
    _TIMEOUT: {
        "en": "That timed out. Please try again.",
        "ar": "العملية خد وقت كبير وانتهت. جرّب تاني.",
    },
    _CONNECTION: {
        "en": "The service is unavailable right now. Please try again later.",
        "ar": "الخدمة مش متاحة دلوقتي. جرّب تاني بعد شوية.",
    },
    _GENERIC: {
        "en": "Something went wrong. Please try again.",
        "ar": "حصلت مشكلة في تنفيذ الطلب. جرّب تاني.",
    },
}


def _classify(exc: Optional[BaseException]) -> str:
    if exc is None:
        return _GENERIC
    name = type(exc).__name__.lower()
    if isinstance(exc, TimeoutError) or "timeout" in name or "timedout" in name:
        return _TIMEOUT
    # httpx raises its own hierarchy (ConnectError, ReadError, ...) that does
    # not derive from OSError, so the class name is consulted as well.
    if isinstance(exc, (ConnectionError, OSError)) or "connect" in name or "network" in name:
        return _CONNECTION
    text = str(exc).lower()
    if "timed out" in text or "timeout" in text:
        return _TIMEOUT
    if "unreachable" in text or "connection" in text or "refused" in text:
        return _CONNECTION
    return _GENERIC


def client_error(
    exc: Optional[BaseException],
    *,
    operation: str,
    component: str,
    language: str = "en",
) -> str:
    """A short bilingual sentence for the user. Never a stack trace.

    ``operation``/``component`` are for the log correlation only — they are
    *not* interpolated into the returned sentence, because an internal name
    such as ``_handle_message`` means nothing to a person and says too much to
    a client.

    This function does not log; the boundary that caught the exception logs it
    through :func:`safe_error`, so an error produces exactly one log line.
    """
    kind = _classify(exc)
    bucket = _CLIENT_MESSAGES[kind]

    logger.warning(
        "CLIENT_ERROR op=%s comp=%s type=%s lang=%s kind=%s",
        operation,
        component,
        type(exc).__name__ if exc is not None else "-",
        language if language in ("en", "ar") else "en",
        kind,
    )
    return bucket["ar"] if language == "ar" else bucket["en"]


__all__ = ["redact", "safe_error", "client_error"]
