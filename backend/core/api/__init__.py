"""REST routes for the core runtime.

Split into modules by domain and mounted from ``api/main.py``. Each one is thin
by design: parse, delegate, serialise. Business rules live in ``core/`` so the
same rules apply whether the caller is the browser, the desktop shell, or a
tool the model invoked.

Two rules every route here follows:

* **Errors are typed, not string-matched at the client.** A missing file is a
  404, a path outside the workspace is a 403, a bad argument is a 422 — so the
  UI can distinguish "you typed the wrong path" from "that is not yours to
  touch", which look identical in a text message.
* **A write never returns secret material.** The provider routes are the ones
  that have to be careful, and there is a test asserting no response body
  anywhere contains a key.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger("Zaytoon.Core.Api")

from fastapi import APIRouter, Body, HTTPException, Query  # noqa: E402

router = APIRouter()


def error_response(exc: Exception, status: int = 400) -> HTTPException:
    """Build an HTTPException carrying a safe message.

    Uses ``safe_error`` so the log line is structured and redacted, and returns
    a client-safe message — never ``str(exc)``, which for a filesystem error is
    an absolute path.
    """
    from common.reporting import client_error, safe_error

    safe_error(exc, operation="core.api", component="core.api")
    return HTTPException(status_code=status, detail=client_error(
        exc, operation="request", component="core.api"
    ))


__all__ = ["error_response", "router"]