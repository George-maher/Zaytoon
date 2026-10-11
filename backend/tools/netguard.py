"""Address policy for anything that reaches out over the network.

``open_url`` hands a URL to the user's browser and ``fetch_url`` reads one on the
user's behalf. Only the second one can be pointed at the machine itself, and a
desktop assistant runs next to services that should never be readable by a model:
Ollama on ``127.0.0.1:11434``, a router admin page, a database's HTTP endpoint,
and on a cloud VM the metadata service at ``169.254.169.254`` — which hands out
credentials. That class of abuse has a name (SSRF) and one defence that works:
resolve the host, then refuse the *address*, before connecting.

The rules, in order of how much they matter:

* **http and https only.** ``file://`` is a local read and ``javascript:`` /
  ``data:`` are script execution, dressed as URLs. Scheme detection is done with
  the RFC 3986 grammar, not ``"://" in url``, because ``javascript:alert(1)`` has
  no slashes — a naive test prefixes ``https://`` onto it and calls that safe.
* **No credentials in the URL.** ``http://user:pass@host`` is how a redirect
  smuggles an attacker's host into an authorization header.
* **Address classes, from the resolved answer, never the string.** ``127.1``,
  ``0x7f000001``, ``127.0.0.1.nip.io`` and ``[::ffff:127.0.0.1]`` all mean
  loopback and none of them match a substring test; every one of them resolves to
  an address whose *class* says loopback. IPv4-mapped and 6to4-embedded forms are
  unwrapped and checked again for the same reason.
* **Each redirect hop, re-checked.** ``https://normal.example`` answering
  ``302 → http://169.254.169.254`` is the oldest bypass in the book.
* **Link-local, loopback, reserved and multicast are refused even in
  ``FETCH_ALLOW_PRIVATE`` mode.** That knob exists so someone can fetch their own
  LAN documentation. Nobody needs the metadata endpoint for that, and the class of
  address that leaks cloud credentials is not a preference.

**Known residual, stated rather than hidden.** There is a window between resolving
a name and connecting, so a hostile DNS record with a short TTL could *rebind* a
permitted name to an internal address. httpx exposes no way to pin a validated IP
while still verifying the TLS certificate against the hostname, so this module
mitigates instead of eliminating: the timeout is short, only GET is issued,
response bodies are capped and never executed, and a refusal is reported in full.
A hostile name on a machine that also runs an unauthenticated local service is the
exact combination to be worried about — that combination is what
``FETCH_ALLOW_PRIVATE=false`` (the default) closes.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from typing import List, NamedTuple, Optional, Set
from urllib.parse import urlparse

from tools.sandbox import SecurityError

#: The only schemes this module will ever approve.
ALLOWED_SCHEMES = ("http", "https")

#: RFC 3986 scheme grammar, used as the *first* half of the test below.
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*$")

#: ``host:443/path``, ``host:80`` — what a colon can mean when it is not a scheme.
_HOST_PORT_TAIL_RE = re.compile(r"\d{1,5}([/?#].*)?", re.S)


def has_explicit_scheme(text: str) -> bool:
    """Is the part before the first colon a scheme, or a host waiting for a port?

    The naive test (``re.match(scheme_grammar)``) reads ``pypi.org:443/project`` as
    the scheme ``pypi.org``, because a dot is legal in a scheme name — and rejects a
    perfectly ordinary URL. The naive fix (require ``"://"``) is worse: it accepts
    ``javascript:alert(1)``. So both are checked: a colon-followed-by-slashes is a
    scheme, a colon-followed-by-a-port-shaped-run-of-digits is a host, and
    anything else with a scheme-grammatical head is treated as a scheme — which
    errs toward refusing, the only safe direction for ``data:`` and ``mailto:``.
    """
    head, sep, tail = (text or "").partition(":")
    if not sep or not head:
        return False
    if tail.startswith("//"):
        return True
    if _SCHEME_RE.match(head) is None:
        return False
    return _HOST_PORT_TAIL_RE.fullmatch(tail) is None

#: Names that mean "this machine" without asking DNS.
_LOCAL_NAMES = frozenset(
    {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
)


class Target(NamedTuple):
    """A URL that has passed the string-level policy."""

    url: str
    host: str
    port: int
    #: Filled in by :func:`resolve_public`; empty until resolution is checked.
    addresses: List[str] = []


def parse_public_url(raw: str, *, allow_private: bool = False) -> Target:
    """Validate syntax, scheme and (for a literal IP) address class. No DNS.

    Raises :class:`~tools.sandbox.SecurityError`, which the tool registry reports as
    a *blocked* result rather than a retryable failure: a policy refusal must not
    look like a typo the model can fix by rephrasing.
    """
    candidate = (raw or "").strip()
    if not candidate:
        raise SecurityError("no URL was given")
    if any(ch.isspace() for ch in candidate):
        # A space inside a URL is how "http://127 .0.0.1" or a header payload
        # sneaks past naive checks, and no legitimate URL contains one.
        raise SecurityError("a URL may not contain whitespace")

    if not has_explicit_scheme(candidate):
        candidate = f"https://{candidate}"

    try:
        parsed = urlparse(candidate)
        port = parsed.port  # property, so a bad port raises here
    except ValueError as exc:
        raise SecurityError(f"not a valid URL: {exc}") from exc

    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise SecurityError(
            f"only {' and '.join(ALLOWED_SCHEMES)} URLs may be fetched "
            f"(got {parsed.scheme or 'no scheme'!r})"
        )
    host = (parsed.hostname or "").strip().rstrip(".").lower()
    if not host:
        raise SecurityError(f"not a valid URL: no host in {raw!r}")
    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        raise SecurityError(
            "credentials in a URL are refused (user:pass@host is a request-"
            "smuggling vector; put them in the request you mean to make)"
        )
    if host.strip("[]") in _LOCAL_NAMES and not allow_private:
        raise SecurityError(f"{host} is this machine, and that is not fetchable")

    target_port = port or (443 if parsed.scheme.lower() == "https" else 80)
    literal = _ip_of(host)
    if literal is not None:
        # A dotted-quad needs no DNS, so refuse it here: a message that arrives
        # before the socket is opened is a cheaper "no".
        reason = _address_refusal(literal, allow_private=allow_private)
        if reason:
            raise SecurityError(f"{host} is not fetchable ({reason})")

    return Target(url=_rebuild(parsed, host, target_port), host=host, port=target_port)


def _address_refusal(ip: str, *, allow_private: bool) -> Optional[str]:
    """Why this address may not be fetched, or ``None`` when it may.

    Always-on refusals are the classes no caller wants reached; ``is_private`` is
    the part ``FETCH_ALLOW_PRIVATE`` governs.
    """
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:  # pragma: no cover - defensive; callers pass parsed hosts
        return "not a parseable address"

    # An IPv4-mapped IPv6 literal (`::ffff:127.0.0.1`) and a 6to4 form
    # (`2002:7f00:0001::`) both *contain* a v4 answer to the same question.
    inner = getattr(address, "ipv4_mapped", None) or _embedded(address)
    if inner is not None and inner != address:
        nested = _address_refusal(str(inner), allow_private=allow_private)
        if nested:
            return f"{inner} inside an IPv6 literal ({nested})"
        address = inner

    for label, verdict in (
        ("loopback", address.is_loopback),
        ("unspecified", address.is_unspecified),
        ("link-local — the cloud metadata endpoint lives here", address.is_link_local),
        ("reserved", address.is_reserved),
        ("multicast", address.is_multicast),
    ):
        if verdict:
            return f"address class {label}"

    if address.is_private and not allow_private:
        return "private address range; set FETCH_ALLOW_PRIVATE to allow your own LAN"
    return None


def _embedded(address) -> Optional[object]:
    try:
        return address.embedded_ipv4
    except (AttributeError, ValueError):  # pragma: no cover - version drift
        return None


def _ip_of(host: str) -> Optional[str]:
    """The host as an IP string, or ``None`` when it is a name."""
    candidate = host.strip("[]")
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate


def _rebuild(parsed, host: str, port: int) -> str:
    """Canonical URL: the host and port we actually checked, nothing else."""
    netloc = host if _ip_of(host) and ":" in host.strip("[]") else host
    default_port = 443 if parsed.scheme == "https" else 80
    if port != default_port:
        netloc = f"{host}:{port}"
    path = parsed.path or "/"
    return f"{parsed.scheme}://{netloc}{path}{'?' + parsed.query if parsed.query else ''}"


async def resolve_public(url: str, *, allow_private: bool = False) -> Target:
    """Parse, resolve, and re-check every address the name answers with.

    Resolution happens here and nowhere else so that "we refused it" is a fact
    about the answer, not about the spelling.
    """
    target = parse_public_url(url, allow_private=allow_private)
    literal = _ip_of(target.host)
    if literal is not None:
        return target._replace(addresses=[literal])

    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, target.host, target.port, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise SecurityError(f"{target.host} does not resolve ({exc})") from exc
    except OSError as exc:  # pragma: no cover - resolver failure
        raise SecurityError(f"{target.host} could not be resolved ({exc})") from exc

    found: Set[str] = {str(info[4][0]) for info in infos or []}
    if not found:
        raise SecurityError(f"{target.host} does not resolve to any address")

    for address in sorted(found):
        reason = _address_refusal(address, allow_private=allow_private)
        if reason:
            raise SecurityError(
                f"{target.host} resolves to {address}, which is not fetchable "
                f"({reason})"
            )
    return target._replace(addresses=sorted(found))


__all__ = [
    "ALLOWED_SCHEMES",
    "SecurityError",
    "Target",
    "parse_public_url",
    "resolve_public",
]
