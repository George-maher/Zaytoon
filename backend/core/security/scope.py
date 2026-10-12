"""Target authorization for active security assessment.

The requirement this module exists for: *enforce scope restrictions in the
execution layer, not only in model instructions.*

A rule that lives in a prompt is not a restriction, it is a suggestion. An LLM
asked to "only test the authorised target" will usually comply and can always be
talked out of it — by a fetched page, by a skill's instructions, by a confused
turn. So the check here is a **function called from inside the tool**, before the
tool touches the network, and it consults a store the model cannot write to by
asserting something. There is no tool that grants an authorization. The model
cannot widen its own scope; only the user, through ``POST /security/authorizations``
or the Security Workspace, can.

What this module is not
-----------------------

* Not a substitute for the permission gate. A target that is authorised is still
  subject to ``RiskAssessor``, the ``ConfirmationManager`` and the sandbox. Both
  checks must pass. Authorization answers *"is this target in scope"*; the gate
  answers *"is the user being asked before this happens"*.
* Not ``services/auth.py``'s ``PermissionScope``. That is a *capability* scope for
  a paired device (``chat``, ``files.read``) and says nothing about targets. Two
  different questions; this module does not touch that vocabulary.

The matching rules, and why each one is strict
----------------------------------------------

* **Exact host, or a ``*.`` subdomain wildcard.** ``example.com`` authorises
  ``example.com``; ``*.example.com`` authorises ``api.example.com`` and *not*
  ``example.com`` itself and *not* ``notexample.com``. Suffix matching on the raw
  string is the classic bypass — ``evil-example.com`` ends with
  ``example.com`` — so the comparison is on the label boundary, after both sides
  are normalised to lower case and a trailing dot is stripped (``example.com.``
  is the same host to DNS and a different string to ``==``).
* **CIDR for addresses**, parsed by ``ipaddress``, never a string prefix.
  ``10.0.0.0/8`` is a network; ``10.0.0.1`` is one host.
* **No wildcard for the whole internet.** ``*`` and a bare ``*.`` are refused at
  creation: an authorization that covers everything is not an authorization, and
  accepting it would make the stored record a lie the UI then displays.
* **Private and loopback targets are permitted here** — deliberately, and the
  opposite of ``tools/netguard.py``. Assessment of a local lab is the intended
  use; ``netguard`` is what stops an *unauthorised* fetch from reaching the same
  address. The two are not in tension because they answer different questions.
* **Expiry is checked at use, not just at creation.** An authorization that
  expired is expired, whatever the row says.

Findings produced under an authorization are recorded with its id, so a report
can state which authorization permitted which probe.
"""

from __future__ import annotations

import ipaddress
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlparse

from core import db

#: Operations an authorization can cover. A closed vocabulary, because an open
#: one would let a typo in a tool name produce an operation nothing ever checks.
OP_HTTP_PROBE = "http.probe"
OP_HTTP_FETCH = "http.fetch"
OP_PORT_PROBE = "port.probe"
OP_LOCAL_SCAN = "local.scan"

ALL_OPERATIONS: tuple[str, ...] = (
    OP_HTTP_PROBE,
    OP_HTTP_FETCH,
    OP_PORT_PROBE,
    OP_LOCAL_SCAN,
)

#: Longest an authorization may run. A year is already far past the point where
#: the user remembers granting it; an unbounded one is a permission that
#: silently became permanent.
MAX_TTL_DAYS = 365


class ScopeError(ValueError):
    """The authorization request itself is malformed."""


class OutOfScope(PermissionError):
    """The target, or the operation, is not authorised.

    A ``PermissionError`` on purpose: this is a refusal to act, and callers that
    already handle "not allowed" keep working. The message is written to be
    shown to the user verbatim — it names the target and what to do about it,
    because "denied" with no next step is how a security feature becomes
    something people switch off.
    """


# ---------------------------------------------------------------------------
# Target parsing and matching
# ---------------------------------------------------------------------------
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")


def extract_host(target: str) -> str:
    """The host of ``target``, which may be a URL, a hostname, or an address.

    Accepts what a user actually types — ``https://api.example.com/v1``,
    ``api.example.com:8443``, ``10.0.0.5`` — and reduces it to the host. A bare
    hostname has no scheme, so it is *not* parsed as a URL: ``urlparse`` reads
    ``example.com:8443`` as scheme ``example.com``, which would silently compare
    the wrong string. Stripping the port first avoids that.
    """
    value = (target or "").strip()
    if not value:
        raise ScopeError("target is required")

    if _SCHEME_RE.match(value):
        parsed = urlparse(value)
        host = parsed.hostname or ""
    else:
        # No scheme: drop any userinfo and port by hand, then take the head.
        value = value.split("@")[-1]
        if value.startswith("["):  # [::1]:8080
            host = value[1 : value.find("]")] if "]" in value else value
        else:
            host = value.split("/")[0].split(":")[0]

    host = host.strip().strip(".").lower()
    if not host:
        raise ScopeError(f"could not read a host from {target!r}")
    return host


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


#: Public suffixes that a wildcard may not sit directly on.
#:
#: `*.co.uk` authorises every registered domain under a public suffix — the
#: whole UK commercial namespace — while looking like a single scope. A complete
#: Public Suffix List is a maintained dataset and a dependency this module will
#: not take; this is the common set, and the check is a guard rather than a
#: claim of completeness. `normalize_pattern` reports which list refused a
#: pattern, so a missing entry shows up as a permissive wildcard rather than as
#: a mystery.
_GUARDED_PUBLIC_SUFFIXES = frozenset({
    "co.uk", "org.uk", "me.uk", "ac.uk", "gov.uk", "net.uk", "sch.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au",
    "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp",
    "co.nz", "net.nz", "org.nz", "govt.nz",
    "com.br", "com.cn", "com.hk", "com.sg", "com.mx", "com.tr", "com.tw",
    "co.in", "co.za", "co.kr", "co.il", "co.id",
    "org.br", "net.br", "gov.br", "edu.br",
    "gov.in", "nic.in", "ac.in", "edu.cn", "gov.cn",
    "github.io", "pages.dev", "vercel.app", "netlify.app",
    "herokuapp.com", "azurewebsites.net", "cloudfront.net",
    "s3.amazonaws.com", "workers.dev", "online",
})


def normalize_pattern(pattern: str) -> str:
    """Validate and canonicalise one authorization target.

    The rules are the module docstring's; this is where they are enforced, so
    that a bad pattern is refused when the user writes it rather than silently
    never matching later.
    """
    raw = (pattern or "").strip().lower().rstrip(".")
    if not raw:
        raise ScopeError("a target is required")

    # A URL is reduced to its host *before* any '/' is read as CIDR syntax:
    # `https://example.com/v1` contains slashes and is not a network.
    if _SCHEME_RE.match(raw):
        raw = extract_host(raw)

    # A network in CIDR form, or a single address.
    if "/" in raw and not raw.startswith("*."):
        try:
            ipaddress.ip_network(raw, strict=False)
        except ValueError as exc:
            raise ScopeError(f"{pattern!r} is not a valid network: {exc}") from exc
        return raw

    if raw in ("*", "*.", "*.*"):
        raise ScopeError(
            "a wildcard for every host is not an authorization. Name the target."
        )

    host = extract_host(raw)
    if _is_ip(host):
        return host

    if host.startswith("*."):
        suffix = host[2:]
        if not suffix or "." not in suffix:
            raise ScopeError(
                f"{pattern!r}: a subdomain wildcard needs a registered domain, "
                f"e.g. '*.example.com'. A bare '*.{suffix}' would authorise "
                f"every host under a public suffix."
            )
        if suffix in _GUARDED_PUBLIC_SUFFIXES:
            raise ScopeError(
                f"{pattern!r}: '{suffix}' is a public suffix, so this wildcard "
                f"would authorise every registered domain under it rather than "
                f"one target. Name the hosts you mean, or the registrable "
                f"domain (e.g. '*.your-domain.co.uk')."
            )
        return f"*.{suffix}"

    if "*" in host:
        raise ScopeError(
            f"{pattern!r}: '*' is only allowed as a leading '*.' label. A "
            f"wildcard in the middle of a host matches more than it looks like."
        )
    if "." not in host:
        raise ScopeError(
            f"{pattern!r}: '{host}' is a single label, so it is ambiguous — "
            f"DNS search domains could resolve it anywhere. Give a full "
            f"hostname, an address, or an explicit CIDR."
        )
    return host


def pattern_matches(pattern: str, host: str) -> bool:
    """Does ``host`` fall inside ``pattern``?

    Pure and total: it answers a question and never raises, because it is called
    on the refusal path where an exception would replace a clear "out of scope"
    with a stack trace.
    """
    host = (host or "").strip().strip(".").lower()
    if not host:
        return False
    pattern = (pattern or "").strip().lower()
    if not pattern:
        return False

    # Network or address pattern.
    if "/" in pattern and not pattern.startswith("*."):
        try:
            network = ipaddress.ip_network(pattern, strict=False)
            try:
                return ipaddress.ip_address(host) in network
            except ValueError:
                # The host might be a name that resolves into the network, but
                # matching on a name can't answer that without resolving, and
                # resolving here would make the check depend on DNS. Refuse.
                return False
        except ValueError:
            return False

    if pattern.startswith("*."):
        suffix = pattern[2:]
        # Label-boundary match: `api.example.com` matches `*.example.com`;
        # `example.com` does not (a wildcard does not cover its own apex), and
        # `notexample.com` does not (it lacks the separating dot).
        return host.endswith("." + suffix)

    # Exact match, plus an address against an address.
    if host == pattern:
        return True
    try:
        return ipaddress.ip_address(host) == ipaddress.ip_address(pattern)
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------
@dataclass
class Authorization:
    id: str
    target: str
    operations: tuple[str, ...]
    note: str = ""
    created_at: str = ""
    expires_at: Optional[str] = None
    revoked_at: Optional[str] = None

    def covers(self, host: str, operation: str) -> bool:
        return operation in self.operations and pattern_matches(self.target, host)

    @property
    def expired(self) -> bool:
        if not self.expires_at:
            return False
        return _parse_stamp(self.expires_at) <= _now()

    @property
    def active(self) -> bool:
        return not self.revoked_at and not self.expired

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "target": self.target,
            "operations": list(self.operations),
            "note": self.note,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "revoked_at": self.revoked_at,
            "active": self.active,
            "expired": self.expired,
        }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _parse_stamp(value: str) -> datetime:
    """Parse a stored timestamp, or report it as long expired.

    A row whose timestamp is unparseable must not be treated as live: the safe
    reading of "I cannot tell whether this is still valid" is "no".
    """
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
async def create(
    targets: Sequence[str] | str,
    operations: Sequence[str],
    *,
    note: str = "",
    ttl_days: Optional[float] = None,
) -> Authorization:
    """Record an authorization for user-stated targets and operations.

    ``ttl_days=None`` means no expiry. That default is deliberate: an
    authorization with a silent short expiry that a long-running assessment
    trips over produces a mid-task refusal the user reads as a bug. The TTL is
    the user's choice, and the UI offers it.
    """
    if isinstance(targets, str):
        targets = [targets]
    patterns = []
    for target in targets or ():
        normalized = normalize_pattern(target)
        if normalized not in patterns:
            patterns.append(normalized)
    if not patterns:
        raise ScopeError("at least one target is required")

    chosen = []
    for operation in operations or ():
        name = (operation or "").strip()
        if name not in ALL_OPERATIONS:
            raise ScopeError(
                f"unknown operation {name!r}. Known: {', '.join(ALL_OPERATIONS)}"
            )
        if name not in chosen:
            chosen.append(name)
    if not chosen:
        raise ScopeError("at least one operation is required")

    if ttl_days is not None and ttl_days <= 0:
        raise ScopeError("ttl_days must be greater than zero")
    if ttl_days is not None and ttl_days > MAX_TTL_DAYS:
        raise ScopeError(f"ttl_days must be at most {MAX_TTL_DAYS}")

    created = _now()
    expires_at = None if ttl_days is None else _stamp(created + timedelta(days=ttl_days))

    records: List[Authorization] = []
    for pattern in patterns:
        record = Authorization(
            id=f"auth_{uuid.uuid4().hex[:12]}",
            target=pattern,
            operations=tuple(chosen),
            note=note.strip(),
            created_at=_stamp(created),
            expires_at=expires_at,
        )
        ok = await db.execute(
            """
            INSERT INTO security_authorizations
                (id, target, operations, note, created_at, expires_at, revoked_at)
            VALUES (?,?,?,?,?,?,NULL)
            """,
            (
                record.id,
                record.target,
                ",".join(record.operations),
                record.note,
                record.created_at,
                record.expires_at,
            ),
        )
        if not ok:
            raise ScopeError("could not persist the authorization")
        records.append(record)
    return records[0]


def _row_to_record(row: Dict[str, Any]) -> Authorization:
    return Authorization(
        id=row["id"],
        target=row["target"],
        operations=tuple(
            op for op in (row.get("operations") or "").split(",") if op
        ),
        note=row.get("note") or "",
        created_at=row.get("created_at") or "",
        expires_at=row.get("expires_at"),
        revoked_at=row.get("revoked_at"),
    )


async def list_all(include_inactive: bool = False) -> List[Authorization]:
    rows = await db.fetch_all(
        "SELECT * FROM security_authorizations ORDER BY created_at DESC"
    )
    records = [_row_to_record(row) for row in rows]
    if include_inactive:
        return records
    return [record for record in records if record.active]


async def revoke(authorization_id: str) -> bool:
    """Mark an authorization revoked. Kept, not deleted, so the audit trail survives."""
    changed = await db.execute_affected(
        "UPDATE security_authorizations SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
        (_stamp(_now()), authorization_id),
    )
    return changed > 0


async def find_for(host: str, operation: str) -> Optional[Authorization]:
    """The first live authorization covering ``host`` + ``operation``, if any."""
    for record in await list_all():
        if record.covers(host, operation):
            return record
    return None


async def require_authorized(target: str, operation: str) -> Authorization:
    """Raise :class:`OutOfScope` unless ``target`` is authorised for ``operation``.

    This is the function an active-assessment tool calls *before* it opens a
    socket. It is async and hits the store on every call rather than caching:
    a revocation in the UI has to take effect on the next probe, and a cache is
    exactly how "I revoked it but it kept scanning" happens.

    The message names the target, the operation, and the exact call that fixes
    it, because this string is what the user sees in the transcript.
    """
    if operation not in ALL_OPERATIONS:
        raise ScopeError(
            f"unknown operation {operation!r}. Known: {', '.join(ALL_OPERATIONS)}"
        )
    try:
        host = extract_host(target)
    except ScopeError as exc:
        raise OutOfScope(
            f"Refused: {exc}. Active assessment needs a named target."
        ) from exc

    record = await find_for(host, operation)
    if record is not None:
        return record

    raise OutOfScope(
        f"Refused: '{host}' is not authorised for '{operation}'. "
        f"Authorise it first in the Security Workspace, or via "
        f"POST /security/authorizations with "
        f'{{"targets": ["{host}"], "operations": ["{operation}"]}}.'
    )


async def describe() -> Dict[str, Any]:
    """Serializable view for the API and the workspace page."""
    active = await list_all()
    everything = await list_all(include_inactive=True)
    return {
        "authorizations": [record.to_dict() for record in everything],
        "active_count": len(active),
        "operations": list(ALL_OPERATIONS),
        "max_ttl_days": MAX_TTL_DAYS,
    }


__all__ = [
    "ALL_OPERATIONS",
    "Authorization",
    "MAX_TTL_DAYS",
    "OP_HTTP_FETCH",
    "OP_HTTP_PROBE",
    "OP_LOCAL_SCAN",
    "OP_PORT_PROBE",
    "OutOfScope",
    "ScopeError",
    "create",
    "describe",
    "extract_host",
    "find_for",
    "list_all",
    "normalize_pattern",
    "pattern_matches",
    "require_authorized",
    "revoke",
]
