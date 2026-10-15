"""Per-permission grants: the `allow` / `ask` / `deny` decisions, enforced.

Why this module exists
----------------------
:mod:`core.permissions` has always had the full vocabulary — eleven named
permissions, and the three decisions a user can make about each one.
`core/api/registry_api.py` has always had the routes that *store* them, in the
`permission_grants` table. The decisions were never **read** by anything on the
execution path. `DECISION_DENY` was defined, validated, rendered, persisted, and
a tool ran anyway.

`registry_api.permission_set` even said so in its docstring: "the enforcement
point is the tool registry's gate, which already exists and already fails
closed". It did not exist. This module is that half — the lookup the gate calls
— and it reads the table the API already writes, so there is one store rather
than a second one beside it.

What a decision means here
--------------------------
``deny``
    Hard refusal. Checked *before* the approval prompt, because asking a user to
    approve something they have already forbidden invites them to override their
    own instruction. This is the only decision that changes behaviour, and it
    can only ever make the product stricter.

``ask``
    The default, and what the product already does: fall through to the tier
    gate (approval for ``confirm``/``dangerous`` actions, silent execution for
    read-only ones).

``allow``
    Stored by the API and reported here, and it deliberately **does not** skip
    the approval prompt. A pre-approval that suppresses the human decision is
    the removal of an authorisation control, and this product does not trade
    that away — a ``confirm``-tier action still asks. The value is kept rather
    than rejected so a settings screen can show what the user chose, but it
    cannot make anything looser than the tier table, and a test asserts exactly
    that.

Failure modes
-------------
An unreadable database is treated as "no decisions", which is the previous
behaviour, and is logged. The alternative — refusing to run anything, or
raising inside the tool path — would turn a storage problem into an outage.
The gate's own floors (validation, sandbox, tier) are unaffected by this module
and keep applying either way.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

from core.permissions import (
    DECISION_ASK,
    DECISION_DENY,
    Permission,
)

logger = logging.getLogger("Zaytoon.Core.Grants")

#: Short-lived cache. The gate is on the hot path (every tool call) and the
#: table changes only when a human changes a setting, but the value must not be
#: stale for a whole session: `_TTL` seconds bounds how long a revoked
#: permission can keep working, and `invalidate()` makes a write take effect
#: immediately in the process that wrote it.
_TTL = 2.0
_cache: Optional[Dict[str, str]] = None
_cache_at: float = 0.0


def invalidate() -> None:
    """Drop the cache. Called after a write, and by the tests."""
    global _cache, _cache_at
    _cache = None
    _cache_at = 0.0


async def _load() -> Dict[str, str]:
    global _cache, _cache_at
    import time

    now = time.monotonic()
    if _cache is not None and (now - _cache_at) < _TTL:
        return _cache

    from core import db

    decisions: Dict[str, str] = {}
    try:
        rows = await db.fetch_all(
            "SELECT permission, decision FROM permission_grants"
        )
        for row in rows:
            name = str(row.get("permission") or "").strip()
            decision = str(row.get("decision") or "").strip().lower()
            if name and decision:
                decisions[name] = decision
    except Exception as exc:  # noqa: BLE001 - a storage problem is not a policy
        logger.warning(
            "Could not read permission grants (%s); none will be applied",
            type(exc).__name__,
        )
        decisions = {}

    _cache = decisions
    _cache_at = now
    return decisions


async def get_grants() -> Dict[str, str]:
    """Every recorded decision, as ``{permission: decision}``."""
    return dict(await _load())


async def decision_for(permission: "Permission | str") -> str:
    """The decision recorded for one permission. Defaults to ``ask``."""
    name = permission.value if isinstance(permission, Permission) else str(permission)
    return (await _load()).get(name.strip(), DECISION_ASK)


async def denied(permissions: Iterable["Permission | str"]) -> List[str]:
    """Which of ``permissions`` the user has forbidden, sorted.

    The gate calls this and refuses when it is non-empty. Sorted so the sentence
    a user reads lists them in a stable order.
    """
    decisions = await _load()
    out = set()
    for permission in permissions:
        name = permission.value if isinstance(permission, Permission) else str(permission)
        name = name.strip()
        if decisions.get(name) == DECISION_DENY:
            out.add(name)
    return sorted(out)


def summarise(decisions: Dict[str, str]) -> Dict[str, List[str]]:
    """Group a decision map by decision name — pure, for the API and the tests."""
    grouped: Dict[str, List[str]] = {DECISION_ASK: [], DECISION_DENY: [], "allow": []}
    for name, decision in sorted(decisions.items()):
        grouped.setdefault(decision, []).append(name)
    return grouped


__all__ = [
    "decision_for",
    "denied",
    "get_grants",
    "invalidate",
    "summarise",
]
