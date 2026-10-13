"""Security workspace support.

Target authorization for active assessment lives in :mod:`core.security.scope`.
It is enforced at the point of use, inside the tools that touch a network —
never in a prompt.
"""

from core.security import findings  # noqa: F401
from core.security.scope import (  # noqa: F401
    ALL_OPERATIONS,
    MAX_TTL_DAYS,
    OP_HTTP_FETCH,
    OP_HTTP_PROBE,
    OP_LOCAL_SCAN,
    OP_PORT_PROBE,
    Authorization,
    OutOfScope,
    ScopeError,
    create,
    describe,
    extract_host,
    find_for,
    list_all,
    normalize_pattern,
    pattern_matches,
    require_authorized,
    revoke,
)

__all__ = [
    "ALL_OPERATIONS",
    "MAX_TTL_DAYS",
    "OP_HTTP_FETCH",
    "OP_HTTP_PROBE",
    "OP_LOCAL_SCAN",
    "OP_PORT_PROBE",
    "Authorization",
    "OutOfScope",
    "ScopeError",
    "create",
    "describe",
    "extract_host",
    "find_for",
    "findings",
    "list_all",
    "normalize_pattern",
    "pattern_matches",
    "require_authorized",
    "revoke",
]
