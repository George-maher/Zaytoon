"""Connection identity, origin policy and authentication for ``/ws``.

Three separate jobs that all belong to the *handshake*, not to the message
loop:

**Identity.** A session id used to be ``f"sess_{int(time.time())}_{hash(ip) % 10000}"``
— two clients behind the same NAT (or simply two browser tabs on one machine)
opening a socket in the same second produced the *same* id, and the second
connection then overwrote ``session_states[session_id]`` and
``_SESSION_ORCHESTRATORS[session_id]``. From that moment the two sockets shared
one cancel event, one confirmation manager and one orchestrator: approving a
tool in tab A resolved tab B's prompt, and closing either tab tore the other
one down. The rate limiter was already per-IP; the session id now carries the
uniqueness the old scheme only looked like it had, while the peer address is
kept separately for logs.

**Origin.** A WebSocket handshake is not subject to CORS, so any page the user
visits can open ``ws://127.0.0.1:8000/ws``. That is why the old ``command``
frame type was removed. The remaining surface is still worth closing:  a
foreign origin could read every reply, list memory, drive the *safe*-tier
tools and raise approval prompts for the ones that need consent, all without
the user knowing which page asked. Browser clients must therefore present an
origin on the allow-list. A client that sends **no** origin at all is not a
browser (the desktop shell and the probes are not), and is judged by address
and token instead.

**Authentication.** ``services/auth.py`` has issued scoped, revocable device
tokens since it was written and nothing ever verified one: the WebSocket
handler never called ``authenticate_ws``. A supplied-but-invalid or revoked
token is now refused, and ``WS_REQUIRE_AUTH=1`` makes a valid token mandatory.
Loopback peers stay anonymous by default, because the desktop shell and the
dev frontend are the same user on the same machine and demanding a pairing
step there would be security theatre with a setup cost.

The policy is deliberately expressed as data (``WS_ALLOWED_ORIGINS``,
``WS_REQUIRE_AUTH``, ``WS_ALLOW_REMOTE``) so a deployment that puts the backend
behind a proxy on another host can open exactly the one door it needs.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Tuple

logger = logging.getLogger("Zaytoon.WSAuth")

#: WebSocket close code for a policy refusal (RFC 6455 §7.4.1
#: "Policy Violation"). The client must not retry blindly on this one.
CLOSE_POLICY_VIOLATION = 1008

#: Undocumented-but-observed: some browsers send the literal string ``null``
#: as the origin of a ``file://`` page or a sandboxed iframe. It is not an
#: origin, so it is treated as "no origin" and judged by address and token —
#: never as an allow-listed value.
_NULL_ORIGINS = {"null", ""}

#: Defaults for a browser origin. Ports are matched as a range because the dev
#: frontend (5173), ``vite preview`` (4173), the desktop host's pinned range
#: (5175–5179 — see ``desktop/app/host.py``) and the Docker/nginx port (3000,
#: 80) are all legitimate *origins* for the same app. The host must still be
#: the loopback interface: ``http://evil.example:5173`` is a different origin
#: and is refused.
_LOOPBACK_HOSTS = r"(?:localhost|127\.0\.0\.1|\[::1\]|0\.0\.0\.0)"
_DEFAULT_ORIGIN_PATTERNS = (
    rf"^https?://{_LOOPBACK_HOSTS}(?::\d+)?$",
    # The packaged desktop shell loads the UI from disk; Electron/pywebview
    # variants report either no origin or ``null``, both handled above. A
    # ``file://`` origin is accepted for the same reason a loopback one is:
    # it can only be produced by software already running on this machine.
    r"^file://",
)


@dataclass(frozen=True)
class AuthDecision:
    """The outcome of the handshake policy.

    ``ok=False`` carries a ``reason`` that is safe to put in a log line and in
    the close frame: it names the policy, never the token, the header dump or
    the address of anything but the peer itself.
    """

    ok: bool
    reason: str = ""
    device_id: Optional[str] = None
    scope: Optional[str] = None
    origin: Optional[str] = None
    authenticated: bool = False


# ---------------------------------------------------------------------------
# Header / query access that tolerates a test double
# ---------------------------------------------------------------------------
def _headers(websocket: Any) -> Any:
    """``websocket.headers`` when present, else an empty mapping.

    Tests drive the handler with minimal doubles whose only connection
    attribute is ``client.host``. A policy module that raises
    ``AttributeError`` on those would turn every unrelated test into a
    handshake failure, so absence is treated as "no headers sent".
    """
    return getattr(websocket, "headers", None) or {}


def _header(websocket: Any, name: str) -> str:
    headers = _headers(websocket)
    try:
        value = headers.get(name)
    except Exception:  # noqa: BLE001 - a header object that is not a mapping
        return ""
    return (value or "").strip() if isinstance(value, str) else ""


def _query(websocket: Any, name: str) -> str:
    params = getattr(websocket, "query_params", None)
    if params is None:
        return ""
    try:
        value = params.get(name)
    except Exception:  # noqa: BLE001 - see _header
        return ""
    return (value or "").strip() if isinstance(value, str) else ""


def peer_host(websocket: Any) -> str:
    """The peer address, or ``""`` when the transport cannot say."""
    client = getattr(websocket, "client", None)
    host = getattr(client, "host", None)
    return host if isinstance(host, str) else ""


def is_loopback(host: str) -> bool:
    """True for a peer on the loopback interface.

    ``0.0.0.0``/``::`` appear as the peer of some in-process test transports;
    they are not a remote address, so they are treated as local rather than
    silently refusing the developer's own tooling.
    """
    if not host:
        return False
    if host in ("localhost", "::1", "0.0.0.0", "::"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # A hostname that is not an IP: only the local names are local.
        return host.lower().endswith(".localhost")


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def new_session_id() -> str:
    """A session id that cannot collide with a concurrent one.

    96 bits of randomness. The old ``time + hash(ip)`` scheme was nominally
    unique per *client* per *second* — which is exactly the collision it hit,
    because two tabs of the same browser are one client and connect within the
    same second.
    """
    return f"sess_{uuid.uuid4().hex[:16]}"


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------
def _flag(value: Any) -> bool:
    """Read a boolean setting without trusting a truthy non-boolean.

    ``bool(value)`` is what a naive read does, and it is wrong in both
    directions here: a test double's attribute is a ``Mock`` (truthy, so every
    policy would turn itself on), and an environment string left as ``"false"``
    would read as True. Only a real boolean, or one of the unambiguous string
    spellings, counts as enabled; everything else — including a Mock — is off.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "enabled")
    if isinstance(value, int):
        return value == 1
    return False


def allowed_origin_patterns(config: Any) -> List[str]:
    """Default browser origins, plus whatever ``WS_ALLOWED_ORIGINS`` adds.

    The extra entries are treated as *regexes* so a deployment can allow a
    whole subnet without listing every port. They are matched against the full
    origin string (scheme + host + port), anchored at both ends by default.
    """
    extra = getattr(config, "WS_ALLOWED_ORIGINS", "")
    if isinstance(extra, str):
        extra_items: List[str] = [p.strip() for p in extra.split(",") if p.strip()]
    elif isinstance(extra, (list, tuple)):
        # A non-string element is ignored rather than stringified: a regex made
        # out of `repr(Mock)` would silently never match, and a pattern that
        # cannot match looks exactly like a pattern that is refusing traffic.
        extra_items = [p.strip() for p in extra if isinstance(p, str) and p.strip()]
    else:
        extra_items = []
    return list(_DEFAULT_ORIGIN_PATTERNS) + extra_items


def origin_allowed(origin: str, config: Any) -> bool:
    """Whether a browser-supplied ``Origin`` may open this socket."""
    if not origin:
        return True  # not a browser; judged by address and token instead
    if origin.lower() in _NULL_ORIGINS:
        return True
    for pattern in allowed_origin_patterns(config):
        try:
            if re.fullmatch(pattern, origin, flags=re.IGNORECASE):
                return True
        except re.error:
            # A malformed pattern must not open the door: log it and keep the
            # rest of the list meaningful.
            logger.warning("Ignoring malformed WS_ALLOWED_ORIGINS entry: %r", pattern)
    return False


def _token_from_handshake(websocket: Any) -> Tuple[str, str]:
    """``(device_id, token)`` from the query string or the Authorization header.

    Query parameters first: a browser cannot set headers on a WebSocket
    handshake, so that is the only channel the web client has. The header is
    supported for native clients (the desktop shell, probes) that can.
    """
    device_id = _query(websocket, "device_id")
    token = _query(websocket, "token")
    if token:
        return device_id, token

    header = _header(websocket, "authorization")
    if header.lower().startswith("bearer "):
        return device_id, header[7:].strip()
    return device_id, _header(websocket, "x-device-token")


def authorize_websocket(websocket: Any, config: Any) -> AuthDecision:
    """Decide whether this handshake may proceed.

    Order matters: origin first (cheapest, and it is the browser-specific
    check), then the token — a supplied token is always verified even when one
    is not required, so a *revoked* credential cannot keep working merely
    because the deployment did not ask for one.
    """
    origin = _header(websocket, "origin")
    host = peer_host(websocket)
    local = is_loopback(host)

    if not origin_allowed(origin, config):
        logger.warning(
            "Refused WebSocket handshake: origin %r is not allow-listed", origin
        )
        return AuthDecision(False, "Origin not allowed", origin=origin)

    device_id, token = _token_from_handshake(websocket)
    if token:
        from services.auth import PermissionScope, get_device_auth

        auth = get_device_auth()
        permission = auth.authenticate(device_id, token) if device_id else None
        if permission is None and not device_id:
            # A token without an id: try every device, in constant-ish time
            # over a small set. This is what makes a query-string client that
            # only knows its token work at all.
            for candidate in auth.list_devices():
                found = auth.authenticate(candidate["device_id"], token)
                if found is not None:
                    permission = found
                    break
        if permission is None:
            logger.warning(
                "Refused WebSocket handshake: invalid or revoked device token "
                "(device_id=%r)", device_id or "-",
            )
            return AuthDecision(False, "Invalid or revoked device token", origin=origin)
        return AuthDecision(
            True,
            "Authenticated device",
            device_id=permission.device_id,
            scope=permission.scope.value
            if isinstance(permission.scope, PermissionScope)
            else str(permission.scope),
            origin=origin,
            authenticated=True,
        )

    require_auth = _flag(getattr(config, "WS_REQUIRE_AUTH", False))
    if require_auth:
        logger.warning("Refused WebSocket handshake: no device token supplied")
        return AuthDecision(False, "A device token is required", origin=origin)

    if not local and not _flag(getattr(config, "WS_ALLOW_REMOTE", False)):
        # The default deployment binds loopback. Refusing a remote peer here
        # means a misconfigured bind (or a proxy) cannot quietly expose full
        # desktop control to the network; the fix is one env var, and the
        # reason says so.
        logger.warning(
            "Refused WebSocket handshake from non-loopback peer %s "
            "(set WS_ALLOW_REMOTE=1 or pair a device to allow it)", host or "?"
        )
        return AuthDecision(False, "Remote connections require a paired device", origin=origin)

    return AuthDecision(True, "Local session", origin=origin)


__all__ = [
    "AuthDecision",
    "flag_from_config",
    "CLOSE_POLICY_VIOLATION",
    "allowed_origin_patterns",
    "authorize_websocket",
    "is_loopback",
    "new_session_id",
    "origin_allowed",
    "peer_host",
]


def flag_from_config(config: Any, name: str, default: bool = False) -> bool:
    """Public form of :func:`_flag` for callers outside this module.

    ``api/main.py`` needs the same reading for the pairing policy, and a second
    ``bool(getattr(...))`` there is how the two policies drift apart.
    """
    return _flag(getattr(config, name, default))
