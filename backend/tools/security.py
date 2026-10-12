"""Authorised, non-destructive HTTP assessment.

This is the tool section 9 of the brief describes: configuration security checks
and HTTP/API assessment that distinguish a *confirmed* finding from a
*hypothesis*, and that refuse an out-of-scope target.

The order of operations in :meth:`SecurityProbe.execute` is the whole point, and
it is deliberate:

1. **Validation** — the URL must parse. No socket is opened.
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
  parameter mutation. It issues one plain request per URL and reads the answer.
  Everything it reports is something the server said unprompted — which is what
  makes its findings *confirmed* rather than speculative, and why it is safe to
  run against a target the user has authorised but not fully understood.
* **No destructive verbs.** ``GET`` and ``HEAD`` only.
* **No guessing.** A missing security header is reported as "absent" with the
  evidence (the status line and the headers actually received). It is never
  upgraded to a vulnerability. The module has no vocabulary for "probably
  vulnerable", because the brief's requirement is that hypotheses are not
  dressed as findings.

Findings carry ``confidence: "confirmed"`` only where the response itself proves
the statement — ``Server: nginx/1.18.0`` is a confirmed banner; "an outdated
nginx is running" is not, and is not claimed. ``remediation`` is always present,
because a finding with no next step is not actionable.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse

from common.reporting import safe_error
from core.security import scope as scope_module
from core.security.scope import OutOfScope
from tools.base import BaseTool, ToolResult, ToolValidationError, _prop

logger = logging.getLogger("Zaytoon.Tools.Security")

#: One request per URL. Enough to see the banner; far below anything a rate
#: limiter would notice, so an authorised probe cannot become an accidental
#: denial of service.
REQUEST_TIMEOUT_S = 10.0
MAX_BODY_CHARS = 6000

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


def build_tools() -> List[BaseTool]:
    return [SecurityProbe()]
