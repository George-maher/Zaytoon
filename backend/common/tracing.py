"""One correlation id per request, visible anywhere in that request's call tree.

The trace id used to live only in ``api/websocket.py``'s request table, so a
model attempt logged by ``services/llm_client.py`` could not be tied back to
the turn that caused it — two log lines, no way to know they were the same
request. Threading a parameter through every agent's ``process`` signature to
fix that would touch every call site for one string, so it travels in a
:class:`contextvars.ContextVar` instead: the transport sets it once at the top
of a turn, and everything scheduled within that turn (including the
orchestrator's inner task, because ``asyncio.create_task`` copies the current
context) sees it.

Rules:

* **Set once per request, by the transport.** The WebSocket sets it in
  ``_run_turn``; the ``X-Request-ID`` middleware sets it for REST. Both run in
  a task that exists for exactly one request, so the context dies with the
  request and there is nothing to leak into the next one.
* **Never a substitute for an explicit id.** Callers that already pass a
  ``request_id`` keep it — the trace id is the fallback before the random one,
  so an explicitly identified request is never re-identified.
* The empty string is the "no trace" value, so a caller can do
  ``explicit or get_trace_id() or generated`` without ``None`` checks.
"""

from __future__ import annotations

import contextvars
import uuid

#: The current request's trace id, or ``""`` outside any request.
_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "zaytoon_trace_id", default=""
)


def new_trace_id() -> str:
    """A fresh correlation id (128 bits of hex, truncated like the WS traces)."""
    return uuid.uuid4().hex[:16]


def set_trace_id(value: str) -> contextvars.Token:
    """Bind ``value`` for this task and its children; returns a reset token."""
    return _trace_id.set(value or "")


def reset_trace_id(token: contextvars.Token) -> None:
    """Restore the binding that preceded :func:`set_trace_id`."""
    _trace_id.reset(token)


def get_trace_id() -> str:
    """The current request's trace id, or ``""`` when there is none."""
    return _trace_id.get()


__all__ = ["new_trace_id", "set_trace_id", "reset_trace_id", "get_trace_id"]
