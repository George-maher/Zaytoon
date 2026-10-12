"""Approvals over REST: a real gesture, in two steps.

Some tools need a genuine confirmation, and the WebSocket path has a channel to
ask on — the client renders a dialog and the user clicks Approve. REST has no
such channel, so a route that simply called such a tool would have to either
bypass the gate (never) or invent an approval (also never).

So the ask is split across two requests:

1. ``{...}``                  — validate, resolve, and describe. **No side
                                effect.** Returns a single-use token.
2. ``{..., "confirm": token}`` — the same call carrying the user's answer. The
                                tool still runs through the process's real
                                ``ToolRegistry``, and that registry's prompt is
                                answered by the one request this endpoint caused
                                — the same ``resolve()`` call the WebSocket
                                handler makes when a user approves. Validation,
                                the reconciled tier gate and the audit record
                                all still run, in order.

What this is *not* is a bypass. Nothing here skips a check; the difference from
the WebSocket path is only where the answer came from, and in both cases it came
from a person who was shown what would happen and chose to proceed.

The token is a gesture in the moment, not a standing permission:

* **single use** — consumed on the second call, and a replay is a 409;
* **short lived** — the TTL is checked *at use*, not merely when the table is
  next pruned. Pruning alone is not expiry: a table that is only pruned when
  someone asks for a fresh token leaves a token valid indefinitely if nobody
  does. That defect existed here and is why :meth:`ApprovalTokens.consume`
  re-checks age rather than trusting the sweep;
* **bound to nothing** — deliberately. Scope and target are re-established on
  the second call, so a token cannot be replayed against a different target.

Tokens live in memory rather than the database, because an approval is a gesture
and a durable one would outlive the page it was given on.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Dict, Optional

DEFAULT_TTL_S = 120.0
DEFAULT_CAP = 256


class ApprovalTokens:
    """A capped, self-expiring store of single-use approval tokens.

    A class rather than a module-level dict so that each surface can hold its
    own store while sharing one implementation — the probe and the VM routes
    must not be able to consume each other's tokens.
    """

    def __init__(
        self,
        *,
        ttl: float = DEFAULT_TTL_S,
        cap: int = DEFAULT_CAP,
        prefix: str = "confirm",
    ) -> None:
        self.ttl = float(ttl)
        self.cap = int(cap)
        self.prefix = prefix
        #: Live tokens, mapped to the monotonic time they were issued.
        self.tokens: Dict[str, float] = {}

    def clear(self) -> None:
        """Drop every token. For tests and for a deliberate reset."""
        self.tokens.clear()

    def prune(self, now: Optional[float] = None) -> None:
        """Drop tokens past their TTL, then trim the oldest down to the cap."""
        now = time.monotonic() if now is None else now
        for key, stamp in list(self.tokens.items()):
            if now - stamp > self.ttl:
                self.tokens.pop(key, None)
        if len(self.tokens) > self.cap:
            # Oldest first, so the cap cannot evict the token being used now.
            excess = len(self.tokens) - self.cap
            for key, _ in sorted(self.tokens.items(), key=lambda kv: kv[1])[:excess]:
                self.tokens.pop(key, None)

    def issue(self, now: Optional[float] = None) -> str:
        """Mint a token and record when it was issued."""
        now = time.monotonic() if now is None else now
        self.prune(now)
        token = f"{self.prefix}_{uuid.uuid4().hex[:12]}"
        self.tokens[token] = now
        return token

    def consume(self, token: str, now: Optional[float] = None) -> bool:
        """Spend a token. True only if it existed and has not expired.

        The age check is the point: see the module docstring.
        """
        if not token:
            return False
        now = time.monotonic() if now is None else now
        issued_at = self.tokens.pop(token, None)
        if issued_at is None:
            return False
        return (now - issued_at) <= self.ttl


async def run_approved_tool(
    tool_name: str,
    params: Dict[str, Any],
    *,
    timeout: float = 15.0,
) -> Dict[str, Any]:
    """Run ``tool_name`` through the real registry, answering its prompt.

    Returns the registry's result untouched, plus ``approved_request_id``: the
    request id of the prompt this call answered. That field is the receipt — it
    exists only because a prompt was actually issued and answered, so its
    presence is what distinguishes a real approval from a gate that was skipped.

    ``registry.execute`` is called rather than ``tool_executor.execute``: the
    latter supplies the process-wide confirmation manager, which has no channel
    to answer on and would therefore refuse every time.
    """
    from api import main as api_main
    from tools.safety import ConfirmationManager

    answered: Dict[str, Any] = {}

    async def _answer(pending) -> None:
        answered["request_id"] = pending.request_id
        answered["action"] = pending.action
        # Deferred rather than resolved inline: `_request` is awaiting the
        # future, and resolving inside the notifier would complete it before
        # the await is reached. Same shape as the WS transport.
        asyncio.get_running_loop().call_soon(
            manager.resolve, pending.request_id, True
        )

    manager = ConfirmationManager(notify=_answer, timeout=timeout)

    result = await api_main.tool_executor.registry.execute(
        tool_name, params, confirmation_manager=manager
    )
    if isinstance(result, dict) and answered.get("request_id"):
        result.setdefault("approved_request_id", answered["request_id"])
    return result


def tier_of(tool_name: str, params: Optional[Dict[str, Any]] = None) -> str:
    """The reconciled tier of a registered tool, for a preview to show.

    Read from the same registry the tool will run on, so a preview cannot
    disagree with the attempt about how consequential the action is.

    ``params`` is not optional decoration. A tier can depend on the arguments —
    ``vm_stop`` is a recoverable shutdown at ``soft`` and a power-pull at
    ``hard`` — so asking the registry for the tier by *name alone* reports the
    gentler of the two and the preview understates what the user is approving.
    The registry's :meth:`assess` runs the tool's ``risk_params`` for exactly
    this reason, and the preview goes through it.
    """
    from api import main as api_main

    try:
        registry = api_main.tool_executor.registry
        return registry.assess(tool_name, params or {}).permission
    except Exception:
        return "unknown"
