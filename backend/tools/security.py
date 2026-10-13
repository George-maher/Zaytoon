"""Authorised, non-destructive assessment: HTTP configuration checks and port probes.

This is the tool section 9 of the brief describes: configuration security checks
and HTTP/API assessment that distinguish a *confirmed* finding from a
*hypothesis*, and that refuse an out-of-scope target. Two tools share it —
:class:`SecurityProbe` (one HTTP request, header/configuration findings) and
:class:`PortScan` (bounded TCP connects, open/closed/filtered with evidence).

Both follow the same order of operations, and it is deliberate:

1. **Validation** — the argument must parse (a URL for the probe, a single
   host and a bounded port list for the scan). No socket is opened.
2. **Authorisation** — :func:`core.security.scope.require_authorized` consults the
   store and raises :class:`OutOfScope` unless *this host* is authorised for
   *this operation*. This happens **before** any connection, so a refused target
   is never contacted at all. A scope check that ran after the request would be a
   log line, not a control.
3. **Permission gate** — the registry's ``confirm`` tier, i.e. the user is asked.
   Step 2 does not replace this: scope answers "is this target in bounds", the
   gate answers "has the human agreed, right now".

What it will not do
-------------------

* **No payloads.** No injection strings, no fuzzing, no auth-bypass attempts, no
  parameter mutation, nothing written after a handshake. Everything reported is
  something the target said unprompted — which is what makes the findings
  *confirmed* rather than speculative, and why it is safe to run against a
  target the user has authorised but not fully understood.
* **No destructive verbs.** ``GET`` and ``HEAD`` only; TCP connects that change
  nothing on the target.
* **No guessing.** A missing security header is reported as "absent" with the
  evidence (the status line and the headers actually received); a port that did
  not answer is reported as "filtered", never as closed. Nothing is upgraded to
  a vulnerability. The module has no vocabulary for "probably vulnerable",
  because the brief's requirement is that hypotheses are not dressed as findings.

Findings carry ``confidence: "confirmed"`` only where the response itself proves
the statement — ``Server: nginx/1.18.0`` is a confirmed banner; "an outdated
nginx is running" is not, and is not claimed. ``remediation`` is always present,
because a finding with no next step is not actionable.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from common.reporting import safe_error
from core.security import findings as findings_module
from core.security import scope as scope_module
from core.security.scope import OutOfScope
from tools.base import BaseTool, ToolResult, ToolValidationError, _prop

logger = logging.getLogger("Zaytoon.Tools.Security")

#: One request per URL. Enough to see the banner; far below anything a rate
#: limiter would notice, so an authorised probe cannot become an accidental
#: denial of service.
REQUEST_TIMEOUT_S = 10.0
MAX_BODY_CHARS = 6000

#: Port-scan bounds. A scan the user authorised must still be *bounded*: 32
#: ports per call keeps one turn's work auditable and a mistyped range
#: ("1-65535") a validation error rather than an evening.
MAX_SCAN_PORTS = 32
#: Per-port connect timeout. Short: a filtered port answers by not answering,
#: and waiting longer than this per port only makes the honest "filtered"
#: verdict slower to reach.
CONNECT_TIMEOUT_S = 1.5
#: Concurrent connects. Bounded so an authorised scan of a lab host is a
#: handful of parallel sockets, not a flood.
SCAN_CONCURRENCY = 8
#: Banner bytes we will read — only what the *server* sends first. Zaytoon
#: sends nothing after the handshake, so this is a read of unprompted data,
#: never a payload.
BANNER_BYTES = 256
BANNER_WAIT_S = 1.0

#: Ports whose number has a well-known service. The name is offered as
#: context ("22 is commonly ssh") and never as a claim about what is
#: actually running — only a banner or an application-layer response could
#: prove that, and this tool makes no such request.
COMMON_SERVICES = {
    21: "ftp", 22: "ssh", 23: "telnet", 25: "smtp", 53: "dns",
    80: "http", 110: "pop3", 143: "imap", 443: "https", 445: "smb",
    3306: "mysql", 3389: "rdp", 5432: "postgresql", 6379: "redis",
    8080: "http-alt", 9200: "elasticsearch", 11434: "ollama",
}

#: Headers whose absence is worth reporting, with the reason and the fix.
#: A report that lists twenty missing headers is noise; these are the ones with
#: a clear consequence for a web-facing service.
SECURITY_HEADERS: Tuple[Tuple[str, str, str], ...] = (
    (
        "strict-transport-security",
        "Transport",
        "Without HSTS a browser will happily follow a plain-http link to this "
        "host, so the TLS session can be stripped on the first request.",
    ),
    (
        "content-security-policy",
        "Injection",
        "Without CSP the browser has no policy to fall back on if the page "
        "reflects input, so reflected XSS becomes script execution.",
    ),
    (
        "x-content-type-options",
        "Injection",
        "Without `nosniff` a browser may treat a response as a type the server "
        "did not intend, which turns a file upload into a script.",
    ),
    (
        "x-frame-options",
        "Clickjacking",
        "Without frame protection the page can be embedded in an attacker's "
        "page and its buttons clicked through. CSP's frame-ancestors also fixes "
        "this, so the finding is suppressed when CSP already sets it.",
    ),
    (
        "referrer-policy",
        "Disclosure",
        "Without a policy the full URL of this page leaks to every third party "
        "it loads, including any query string.",
    ),
)

#: Header values that are presence without effect — a header that exists and
#: says nothing. Reporting these as "present" is how a scan produces a clean
#: bill of health for a host that is not configured.
INEFFECTIVE_VALUES = {
    "access-control-allow-origin": ("*",),
    "x-frame-options": ("allowall", "allow-all"),
    "content-security-policy": ("", "default-src *"),
    "strict-transport-security": ("max-age=0",),
}


class SecurityProbe(BaseTool):
    """One non-destructive HTTP request against an authorised target."""

    #: `confirm`, not `safe`: an authorised probe still tells the user it is
    #: about to touch a host. `dangerous` would be wrong — nothing here changes
    #: state on the target.
    permission = "confirm"

    def name(self) -> str:
        return "security_probe"

    def aliases(self) -> List[str]:
        return ["security_scan", "authorized_probe", "check_target"]

    def description(self) -> str:
        return (
            "Run a single non-destructive HTTP check against a target the user "
            "has explicitly authorised, and report configuration findings with "
            "evidence and remediation. Sends no payloads and changes nothing on "
            "the target. Refuses any host that is not authorised for "
            "'http.probe' — authorise targets in the Security Workspace first."
        )

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": _prop(
                    "string",
                    "The http(s) URL to assess. Its host must be covered by an "
                    "active authorization.",
                ),
                "include_body": _prop(
                    "boolean",
                    "Also return a bounded excerpt of the response body "
                    "(default false).",
                    default=False,
                ),
            },
            "required": ["url"],
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        url = params.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ToolValidationError("'url' is required")
        parsed = urlparse(url.strip())
        if parsed.scheme not in ("http", "https"):
            raise ToolValidationError(
                "'url' must start with http:// or https:// — this tool "
                "assesses reachable services, and a file:// or data: URL is a "
                "local read dressed as one."
            )
        if not parsed.hostname:
            raise ToolValidationError("'url' has no host")

    def risk_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        # The URL only. There is no payload field to hand the classifier, and
        # the body of a response is never an input to the decision.
        return {"url": params.get("url", "")}

    async def execute(
        self, url: str = "", include_body: bool = False, **_: Any
    ) -> ToolResult:
        requested = url.strip()
        parsed = urlparse(requested)
        host = (parsed.hostname or "").lower()

        # ---- 2. Authorisation, before any socket exists ------------------
        try:
            authorization = await scope_module.require_authorized(
                host, scope_module.OP_HTTP_PROBE
            )
        except OutOfScope as exc:
            # A refusal, not an error: the tool did what it is supposed to do,
            # and the message already tells the user how to authorise.
            return ToolResult(
                success=False,
                output="",
                error=str(exc),
                data={
                    "refused": True,
                    "reason": "out_of_scope",
                    "host": host,
                    "operation": scope_module.OP_HTTP_PROBE,
                },
            )
        except scope_module.ScopeError as exc:
            return ToolResult.fail(f"Scope check failed: {exc}")

        import httpx

        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(REQUEST_TIMEOUT_S, connect=5.0),
                follow_redirects=False,
                headers={
                    "User-Agent": "Zaytoon-SecurityProbe/1.0 (authorised assessment)",
                    "Accept": "*/*",
                },
            ) as client:
                response = await client.get(requested)
        except httpx.TimeoutException:
            return ToolResult.fail(
                f"No response from {requested} within {REQUEST_TIMEOUT_S:.0f}s. "
                "The host may be down, or filtering this source."
            )
        except httpx.HTTPError as exc:
            return ToolResult.fail(
                f"Could not reach {requested}: {type(exc).__name__}: {exc}"
            )
        except Exception as exc:  # noqa: BLE001 - reported, never a traceback
            safe_error(exc, operation="security_probe", component="tools.security")
            return ToolResult.fail(f"Probe of {requested} failed: {type(exc).__name__}")

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        headers = {key.lower(): value for key, value in response.headers.items()}
        findings = self._findings(headers, response, parsed)

        # Evidence outlives the turn. `record` never raises and runs *after*
        # the observation exists — a persistence failure must not cost the
        # user the probe's answer, only the later review of it.
        await findings_module.record(
            "security_probe",
            host,
            findings,
            authorization_id=authorization.id,
            operation=scope_module.OP_HTTP_PROBE,
        )

        body_excerpt = ""
        if include_body:
            try:
                body_excerpt = response.text[:MAX_BODY_CHARS]
            except Exception:  # noqa: BLE001 - a body is a bonus, never the result
                body_excerpt = ""

        report = self._render(
            requested, response, headers, findings, elapsed_ms,
            authorization_id=authorization.id,
            authorization_target=authorization.target,
            body_excerpt=body_excerpt,
        )
        return ToolResult.ok(
            report,
            {
                "url": str(response.url),
                "requested_url": requested,
                "status": response.status_code,
                "authorization_id": authorization.id,
                "authorized_target": authorization.target,
                "findings": findings,
                "headers": headers,
                "elapsed_ms": round(elapsed_ms, 1),
                "body_excerpt": body_excerpt,
            },
        )

    # ------------------------------------------------------------------
    def _findings(
        self, headers: Dict[str, str], response: Any, parsed: Any
    ) -> List[Dict[str, Any]]:
        """Confirmed observations only, each with evidence and remediation.

        Nothing here infers beyond the response. Every finding's ``evidence`` is
        a string a reader can check against the raw headers.
        """
        findings: List[Dict[str, Any]] = []
        is_https = parsed.scheme == "https"
        csp = headers.get("content-security-policy", "")

        for header, category, consequence in SECURITY_HEADERS:
            if header in headers:
                value = headers[header]
                if header == "x-frame-options" and "frame-ancestors" in csp:
                    # CSP already covers it; reporting this would be a false
                    # positive, which is worse than a missed low-severity item.
                    continue
                if value.strip().lower() in INEFFECTIVE_VALUES.get(header, ()):
                    findings.append(
                        {
                            "id": f"header.ineffective.{header}",
                            "title": f"{header} is set but has no effect",
                            "severity": "low",
                            "confidence": "confirmed",
                            "category": category,
                            "evidence": f"{header}: {value}",
                            "consequence": consequence,
                            "remediation": (
                                f"Set {header} to a value that enforces the "
                                f"policy, or remove it — a header that is "
                                f"present and ineffective reads as configured "
                                f"to every automated check."
                            ),
                        }
                    )
                continue

            # HSTS on a plain-http URL is not a finding: the header is only
            # honoured over TLS, so its absence there says nothing.
            if header == "strict-transport-security" and not is_https:
                continue

            findings.append(
                {
                    "id": f"header.missing.{header}",
                    "title": f"{header} is not set",
                    "severity": "medium" if header in ("content-security-policy", "strict-transport-security") else "low",
                    "confidence": "confirmed",
                    "category": category,
                    "evidence": (
                        f"HTTP {response.status_code}; the response headers "
                        f"received do not include {header}."
                    ),
                    "consequence": consequence,
                    "remediation": f"Set the {header} response header.",
                }
            )

        # A banner that names its version is a confirmed disclosure; it is not
        # by itself a vulnerability, and the text says so.
        for header in ("server", "x-powered-by"):
            value = headers.get(header, "")
            if value and any(ch.isdigit() for ch in value):
                findings.append(
                    {
                        "id": f"disclosure.{header}",
                        "title": f"{header} discloses a software version",
                        "severity": "info",
                        "confidence": "confirmed",
                        "category": "Disclosure",
                        "evidence": f"{header}: {value}",
                        "consequence": (
                            "A version string lets an attacker match the host "
                            "against public advisories without touching it. It "
                            "is not a vulnerability on its own — whether this "
                            "build is affected was not tested."
                        ),
                        "remediation": (
                            f"Suppress or genericise {header} at the proxy or "
                            f"application, and keep the component patched."
                        ),
                    }
                )

        if is_https:
            # Reaching this point at all *is* the evidence: httpx verifies the
            # certificate chain against the system trust store by default, so a
            # response over https means the chain validated. Recorded so the
            # report shows what was checked, and scoped so it claims no more
            # than that.
            findings.append(
                {
                    "id": "tls.handshake",
                    "title": "TLS handshake completed with a validated certificate",
                    "severity": "info",
                    "confidence": "confirmed",
                    "category": "Transport",
                    "evidence": (
                        "The https request completed with certificate "
                        "verification enabled (httpx default), so the chain "
                        "validated against the system trust store."
                    ),
                    "consequence": (
                        "This is not an assessment of the cipher suite, key "
                        "strength, certificate lifetime, or revocation status."
                    ),
                    "remediation": "None required.",
                }
            )

        if response.status_code in (301, 302, 307, 308):
            location = response.headers.get("location", "")
            if location.lower().startswith("http://") and is_https:
                findings.append(
                    {
                        "id": "transport.downgrade",
                        "title": "An https endpoint redirects to plain http",
                        "severity": "high",
                        "confidence": "confirmed",
                        "category": "Transport",
                        "evidence": f"HTTP {response.status_code} → Location: {location}",
                        "consequence": (
                            "The session leaves TLS on the next request, so "
                            "everything after the redirect is readable in "
                            "transit and modifiable by anyone on the path."
                        ),
                        "remediation": (
                            "Redirect to the https URL, and set HSTS so the "
                            "first request cannot be downgraded either."
                        ),
                    }
                )

        order = {"high": 0, "medium": 1, "low": 2, "info": 3}
        findings.sort(key=lambda item: (order.get(item["severity"], 9), item["id"]))
        return findings

    @staticmethod
    def _render(
        requested: str,
        response: Any,
        headers: Dict[str, str],
        findings: List[Dict[str, Any]],
        elapsed_ms: float,
        *,
        authorization_id: str,
        authorization_target: str,
        body_excerpt: str,
    ) -> str:
        counts: Dict[str, int] = {}
        for finding in findings:
            counts[finding["severity"]] = counts.get(finding["severity"], 0) + 1
        summary = ", ".join(
            f"{count} {level}" for level, count in sorted(counts.items()) if level != "info"
        ) or "no configuration findings"

        lines = [
            f"Authorised assessment of {requested}",
            f"Authorisation: {authorization_id} (target pattern '{authorization_target}')",
            f"Result: HTTP {response.status_code} in {elapsed_ms:.0f} ms",
            f"Findings: {summary}",
            "",
        ]
        for index, finding in enumerate(findings, 1):
            lines += [
                f"{index}. [{finding['severity'].upper()}] {finding['title']}"
                f"  ({finding['confidence']}, {finding['category']})",
                f"   Evidence   : {finding['evidence']}",
                f"   Consequence: {finding['consequence']}",
                f"   Remediation: {finding['remediation']}",
                "",
            ]
        lines.append("Note: this is a single non-destructive request. Absence of a")
        lines.append("finding is not evidence of absence — no payloads were sent and")
        lines.append("no authentication or authorisation logic was exercised.")
        if body_excerpt:
            lines += ["", "--- body excerpt ---", body_excerpt]
        return "\n".join(lines)


class PortScan(BaseTool):
    """A bounded TCP connect scan against an authorised target.

    Same three-step order as :class:`SecurityProbe`, and for the same reason:
    validation (no socket), authorisation (``require_authorized`` for
    ``port.probe`` — a refused host is never contacted at all), then the
    registry's ``confirm`` gate.

    What it will not do
    -------------------

    * **No payloads.** A TCP handshake and, optionally, a read of what the
      *server* sends first. Zaytoon writes nothing after connecting, so a
      banner grab here is a read of unprompted data, not a probe payload.
    * **No service fingerprinting.** An open port's number is reported with
      its common service name as context ("22 is commonly ssh"), never as a
      claim about what is running.
    * **No breadth.** ``MAX_SCAN_PORTS`` ports per call. A scan is a question
      about named services, not a sweep.

    States, and what each one *means*:
    ``open`` — the handshake completed: a service answered. ``closed`` — the
    host actively refused: nothing is listening (or a firewall rejects
    locally). ``filtered`` — no answer within the timeout: inconclusive by
    definition; it is never reported as "closed" or as a finding.
    """

    #: `confirm`, same reasoning as SecurityProbe: the user is told before any
    #: socket opens, even for a target they authorised in the workspace.
    permission = "confirm"

    def name(self) -> str:
        return "port_scan"

    def aliases(self) -> List[str]:
        return ["port_probe", "scan_ports", "tcp_scan"]

    def description(self) -> str:
        return (
            "Run a bounded TCP connect scan (with an optional server-first "
            "banner read) against a target the user has explicitly authorised. "
            "Reports each named port as open, closed or filtered with evidence. "
            "Sends no payloads and changes nothing on the target. Refuses any "
            "host not authorised for 'port.probe' — authorise targets in the "
            "Security Workspace first."
        )

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "host": _prop(
                    "string",
                    "The hostname or IP address to scan. Must be covered by an "
                    "active authorization for 'port.probe'.",
                ),
                "ports": _prop(
                    "array",
                    f"Ports to check: integers and/or 'start-end' range strings. "
                    f"At most {MAX_SCAN_PORTS} ports after expansion, e.g. "
                    f"[22, 80, \"8000-8005\"].",
                    items={"type": ["integer", "string"]},
                ),
                "read_banner": _prop(
                    "boolean",
                    "Also read up to a few hundred bytes of what an open service "
                    "sends first, without sending anything (default true).",
                    default=True,
                ),
            },
            "required": ["host", "ports"],
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        host = params.get("host")
        if not isinstance(host, str) or not host.strip():
            raise ToolValidationError("'host' is required")
        host = host.strip()
        if any(ch.isspace() for ch in host) or "/" in host or ":" in host and not self._looks_like_ipv6(host):
            # A URL, a CIDR or a scheme is a different tool's argument. The
            # scope store accepts CIDR patterns for *authorizations*; a scan
            # target is one host, and accepting a network here would make the
            # per-host scope check meaningless.
            raise ToolValidationError(
                "'host' must be a single hostname or IP address — a URL or a "
                "CIDR network is not a scan target."
            )
        ports = params.get("ports")
        if not isinstance(ports, (list, tuple)) or not ports:
            raise ToolValidationError("'ports' must be a non-empty list")
        # Pre-check: a single range string that exceeds the cap must give the
        # precise "at most 32 ports" error, not "no valid ports".
        for item in ports:
            if isinstance(item, str) and "-" in item:
                left, _, right = item.partition("-")
                try:
                    start, end = int(left.strip()), int(right.strip())
                except ValueError:
                    continue
                if start > end:
                    raise ToolValidationError(
                        f"range '{item}' is backwards (start > end)"
                    )
                if end - start + 1 > MAX_SCAN_PORTS:
                    raise ToolValidationError(
                        f"at most {MAX_SCAN_PORTS} ports per call (range '{item}' "
                        f"expands to {end - start + 1}); split the scan."
                    )
        expanded = self._expand_ports(ports)
        if not expanded:
            raise ToolValidationError("'ports' contains no valid ports")
        if len(expanded) > MAX_SCAN_PORTS:
            raise ToolValidationError(
                f"at most {MAX_SCAN_PORTS} ports per call (got {len(expanded)}); "
                "split the scan or name the ports you actually mean."
            )

    @staticmethod
    def _looks_like_ipv6(value: str) -> bool:
        return value.count(":") >= 2

    @staticmethod
    def _expand_ports(ports: Any) -> List[int]:
        """Ints and 'start-end' strings → a sorted, de-duplicated int list.

        A bare number is never accepted as a range endpoint out of order
        (``'90-80'``), and port 0 is refused: it is reserved, and no service
        answers on it. A range larger than the per-call cap is an error,
        not a silent shrink — ``validate`` checks the cap after expansion
        and must be able to tell the user *why* the list is too long.
        """
        result: List[int] = []
        for item in ports:
            if isinstance(item, bool):
                continue
            if isinstance(item, int):
                candidates = [item]
            elif isinstance(item, str):
                text = item.strip()
                if "-" in text:
                    left, _, right = text.partition("-")
                    try:
                        start, end = int(left.strip()), int(right.strip())
                    except ValueError:
                        continue
                    if start > end or end - start + 1 > MAX_SCAN_PORTS:
                        continue  # validate will catch the oversized count
                    candidates = list(range(start, end + 1))
                else:
                    try:
                        candidates = [int(text)]
                    except ValueError:
                        continue
            else:
                continue
            for port in candidates:
                if 1 <= port <= 65535 and port not in result:
                    result.append(port)
        return sorted(result)

    def risk_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        # Host and the port list only — exactly what the user is agreeing to.
        return {
            "host": params.get("host", ""),
            "ports": params.get("ports", []),
        }

    async def execute(
        self, host: str = "", ports: Optional[List[Any]] = None, read_banner: bool = True, **_: Any
    ) -> ToolResult:
        target = host.strip()
        expanded = self._expand_ports(ports or [])

        # ---- 2. Authorisation, before any socket exists ------------------
        try:
            authorization = await scope_module.require_authorized(
                target, scope_module.OP_PORT_PROBE
            )
        except OutOfScope as exc:
            return ToolResult(
                success=False,
                output="",
                error=str(exc),
                data={
                    "refused": True,
                    "reason": "out_of_scope",
                    "host": target,
                    "operation": scope_module.OP_PORT_PROBE,
                },
            )
        except scope_module.ScopeError as exc:
            return ToolResult.fail(f"Scope check failed: {exc}")

        import asyncio

        semaphore = asyncio.Semaphore(SCAN_CONCURRENCY)

        async def _probe(port: int) -> Dict[str, Any]:
            async with semaphore:
                started = time.perf_counter()
                banner = None
                try:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(target, port),
                        timeout=CONNECT_TIMEOUT_S,
                    )
                except asyncio.TimeoutError:
                    state = "filtered"
                except ConnectionRefusedError:
                    state = "closed"
                except OSError as exc:
                    # DNS failure, network unreachable, permission error —
                    # none of which is "closed". Named, never swallowed.
                    return {
                        "port": port,
                        "state": "filtered",
                        "detail": type(exc).__name__,
                        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1),
                        "banner": None,
                    }
                else:
                    state = "open"
                    banner = None
                    try:
                        if read_banner:
                            raw = await asyncio.wait_for(
                                reader.read(BANNER_BYTES), timeout=BANNER_WAIT_S
                            )
                            banner = raw.decode("utf-8", "replace").strip() or None
                    except (asyncio.TimeoutError, OSError):
                        # No banner is the common case (HTTP services wait to
                        # be spoken to, and Zaytoon sends nothing).
                        banner = None
                    finally:
                        writer.close()
                        try:
                            await writer.wait_closed()
                        except OSError:
                            pass
                return {
                    "port": port,
                    "state": state,
                    "detail": None,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1),
                    "banner": banner,
                }

        # `gather` without return_exceptions: every failure mode inside
        # `_probe` is already converted to a structured result, so nothing
        # here can raise and cancel its siblings — the same reasoning as
        # `AgentEngine._run_batch`. The semaphore inside `_probe` is what
        # bounds the simultaneous sockets; gathering is what makes them
        # actually simultaneous.
        results = await asyncio.gather(*(_probe(port) for port in expanded))
        results = list(results)
        open_ports = [r for r in results if r["state"] == "open"]

        findings: List[Dict[str, Any]] = []
        for entry in open_ports:
            port = entry["port"]
            service = COMMON_SERVICES.get(port)
            findings.append(
                {
                    "id": f"port.open.{port}",
                    "title": f"Port {port} accepted a TCP connection"
                    + (f" ({service})" if service else ""),
                    "severity": "info",
                    "confidence": "confirmed",
                    "category": "Exposure",
                    "evidence": (
                        f"TCP connect to {target}:{port} completed in "
                        f"{entry['elapsed_ms']:.0f} ms"
                        + (f"; server-first banner: {entry['banner']!r}" if entry["banner"] else "")
                    ),
                    "consequence": (
                        f"A service is reachable on port {port} from this machine."
                        + (
                            f" Port {port} is commonly {service}, but the service "
                            "itself was not identified — no application-layer "
                            "request was made."
                            if service
                            else ""
                        )
                    ),
                    "remediation": (
                        "Confirm this service is meant to be reachable from "
                        "here; firewall or close it otherwise."
                    ),
                    "data": entry,
                }
            )
            banner = entry.get("banner") or ""
            if banner and any(ch.isdigit() for ch in banner):
                findings.append(
                    {
                        "id": f"disclosure.port.{port}",
                        "title": f"The service on port {port} discloses a software version",
                        "severity": "info",
                        "confidence": "confirmed",
                        "category": "Disclosure",
                        "evidence": f"{target}:{port} sent: {banner!r}",
                        "consequence": (
                            "A version string lets an attacker match the service "
                            "against public advisories without touching it. It is "
                            "not a vulnerability on its own — whether this build "
                            "is affected was not tested."
                        ),
                        "remediation": (
                            "Suppress or genericise the banner, and keep the "
                            "component patched."
                        ),
                    }
                )

        await findings_module.record(
            "port_scan",
            target,
            findings,
            authorization_id=authorization.id,
            operation=scope_module.OP_PORT_PROBE,
        )

        report = self._render(target, results, findings, authorization)
        return ToolResult.ok(
            report,
            {
                "host": target,
                "authorization_id": authorization.id,
                "authorized_target": authorization.target,
                "results": results,
                "findings": findings,
                "open": [entry["port"] for entry in open_ports],
                "closed": [r["port"] for r in results if r["state"] == "closed"],
                "filtered": [r["port"] for r in results if r["state"] == "filtered"],
            },
        )

    @staticmethod
    def _render(
        target: str,
        results: List[Dict[str, Any]],
        findings: List[Dict[str, Any]],
        authorization: Any,
    ) -> str:
        lines = [
            f"Authorised port scan of {target}",
            f"Authorisation: {authorization.id} (target pattern '{authorization.target}')",
            "",
        ]
        for entry in results:
            port = entry["port"]
            service = COMMON_SERVICES.get(port)
            suffix = f" (commonly {service})" if service else ""
            if entry["state"] == "open":
                extra = f" — banner: {entry['banner']!r}" if entry.get("banner") else ""
                lines.append(
                    f"  {port}/tcp OPEN{suffix} in {entry['elapsed_ms']:.0f} ms{extra}"
                )
            elif entry["state"] == "closed":
                lines.append(f"  {port}/tcp closed (host refused the connection)")
            else:
                detail = f" ({entry['detail']})" if entry.get("detail") else ""
                lines.append(
                    f"  {port}/tcp filtered or no answer within "
                    f"{CONNECT_TIMEOUT_S:.1f}s{detail} — inconclusive"
                )
        if findings:
            lines += ["", "Observations:"]
            for index, finding in enumerate(findings, 1):
                lines += [
                    f"{index}. [{finding['severity'].upper()}] {finding['title']}",
                    f"   Evidence   : {finding['evidence']}",
                    f"   Consequence: {finding['consequence']}",
                    f"   Remediation: {finding['remediation']}",
                ]
        lines += [
            "",
            "Note: a TCP handshake and an optional read of server-first bytes.",
            "Nothing was sent after connecting. 'Filtered' means the host did",
            "not answer in time — it is not evidence that nothing is there.",
        ]
        return "\n".join(lines)


def build_tools() -> List[BaseTool]:
    return [SecurityProbe(), PortScan()]
