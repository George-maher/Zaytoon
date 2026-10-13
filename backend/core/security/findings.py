"""Persisted evidence from authorised assessment runs.

A finding that lives only in a transcript is a finding nobody can review
later. This module is the evidence store behind §11C: the assessment tools
write what they *observed* here, and the report endpoint assembles scope,
methodology, results and limitations from these rows.

Three rules, all structural:

* **Only successful probes record.** A refused (out-of-scope) or failed probe
  writes nothing — there was no observation to keep, and a row created by a
  refusal would read as "something was found" in every later report.
* **Recording can never fail a turn.** Same contract as ``common/audit``:
  every function swallows its own errors and logs once. An evidence store is
  a courtesy to the reader; it is not allowed to become the reason an
  authorised probe reports nothing.
* **The vocabulary is enforced here, not by a CHECK constraint.** The tools
  are the only writers and they construct the values; an unknown ``confidence``
  or ``severity`` is clamped to the most conservative reading
  (``informational`` / ``info``) and logged, because a constraint would turn
  a future vocabulary change into a silent row-drop inside a never-raise
  helper — the exact failure mode the migration comment warns about.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from common.reporting import redact, safe_error
from core import db

logger = logging.getLogger("Zaytoon.Core.Security.Findings")

#: Closed vocabularies. `confirmed` means the response itself proves the
#: statement; `suspected` is reserved for an observation that needs more work
#: before it can be claimed (no current tool emits it — the vocabulary exists
#: so a future tool has somewhere honest to put uncertainty).
CONFIDENCE_LEVELS = ("confirmed", "suspected", "informational")
SEVERITIES = ("high", "medium", "low", "info")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clamp(value: Any, allowed: Sequence[str], fallback: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else fallback


async def record(
    tool: str,
    target: str,
    findings: Sequence[Dict[str, Any]],
    *,
    authorization_id: Optional[str] = None,
    operation: Optional[str] = None,
) -> int:
    """Persist one probe's findings. Returns how many rows were written.

    Never raises: a broken table, an unwritable path or a lock logs one line
    and returns 0. The caller has already produced its ToolResult — evidence
    persistence runs *after* the answer, never in front of it.
    """
    if not findings:
        return 0
    stamp = _now()
    written = 0
    for finding in findings:
        try:
            payload = {
                "tool": str(tool)[:64],
                "operation": str(operation or "")[:32] or None,
                "target": str(target)[:255],
                "authorization_id": str(authorization_id or "")[:64] or None,
                "finding_id": str(finding.get("id") or "")[:128] or None,
                "title": redact(str(finding.get("title") or "observation"))[:300],
                "severity": _clamp(finding.get("severity"), SEVERITIES, "info"),
                "confidence": _clamp(
                    finding.get("confidence"), CONFIDENCE_LEVELS, "informational"
                ),
                "category": str(finding.get("category") or "")[:80] or None,
                "evidence": redact(str(finding.get("evidence") or ""))[:2000] or None,
                "consequence": redact(str(finding.get("consequence") or ""))[:2000] or None,
                "remediation": redact(str(finding.get("remediation") or ""))[:2000] or None,
            }
            extra = finding.get("data")
            if isinstance(extra, dict):
                payload["data_json"] = json.dumps(
                    {"data": redact(extra)}, default=str
                )[:4000]
            else:
                payload["data_json"] = None
            ok = await db.execute(
                """
                INSERT INTO security_findings
                    (authorization_id, tool, operation, target, finding_id,
                     title, severity, confidence, category, evidence,
                     consequence, remediation, data_json, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    payload["authorization_id"],
                    payload["tool"],
                    payload["operation"],
                    payload["target"],
                    payload["finding_id"],
                    payload["title"],
                    payload["severity"],
                    payload["confidence"],
                    payload["category"],
                    payload["evidence"],
                    payload["consequence"],
                    payload["remediation"],
                    payload["data_json"],
                    stamp,
                ),
            )
            if ok:
                written += 1
        except Exception as exc:  # noqa: BLE001 — never break the turn
            safe_error(
                exc,
                operation="security.findings.record",
                component="core.security.findings",
            )
            return written
    if written:
        logger.info(
            "Recorded %d finding(s) for %s under %s",
            written, target, authorization_id or "no authorization",
        )
    return written


def _row_to_dict(row: Any) -> Dict[str, Any]:
    data = None
    if row["data_json"]:
        try:
            data = json.loads(row["data_json"]).get("data")
        except (ValueError, TypeError):
            data = None
    return {
        "id": row["id"],
        "authorization_id": row["authorization_id"],
        "tool": row["tool"],
        "operation": row["operation"],
        "target": row["target"],
        "finding_id": row["finding_id"],
        "title": row["title"],
        "severity": row["severity"],
        "confidence": row["confidence"],
        "category": row["category"],
        "evidence": row["evidence"],
        "consequence": row["consequence"],
        "remediation": row["remediation"],
        "data": data,
        "created_at": row["created_at"],
    }


async def list_findings(
    target: Optional[str] = None,
    severity: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Recorded findings, newest first, optionally narrowed.

    Never raises — an unreadable store answers ``[]`` for the same reason the
    audit page answers "unavailable" rather than exploding.
    """
    limit = max(1, min(int(limit or 200), 1000))
    clauses: List[str] = []
    params: List[Any] = []
    if target:
        clauses.append("target = ?")
        params.append(str(target)[:255])
    if severity:
        clauses.append("severity = ?")
        params.append(_clamp(severity, SEVERITIES, "info"))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        rows = await db.fetch_all(
            f"""
            SELECT * FROM security_findings{where}
            ORDER BY id DESC LIMIT ?
            """,
            (*params, limit),
        )
    except Exception as exc:  # noqa: BLE001
        safe_error(
            exc,
            operation="security.findings.list",
            component="core.security.findings",
        )
        return []
    return [_row_to_dict(row) for row in rows]


async def clear(target: Optional[str] = None) -> int:
    """Delete recorded findings (all, or one target's). Returns rows removed."""
    try:
        if target:
            return await db.execute_affected(
                "DELETE FROM security_findings WHERE target = ?",
                (str(target)[:255],),
            )
        return await db.execute_affected("DELETE FROM security_findings", ())
    except Exception as exc:  # noqa: BLE001
        safe_error(
            exc,
            operation="security.findings.clear",
            component="core.security.findings",
        )
        return 0


#: The one paragraph that must accompany every report. It is fixed text, not
#: a model generation, because the limitations of these tools are a property
#: of the tools and do not vary per run.
METHODOLOGY_NOTE = (
    "Findings in this report were produced by Zaytoon's authorised, "
    "non-destructive assessment tools: `security_probe` issues one plain "
    "GET/HEAD request per URL and reads the answer; `port_scan` performs a "
    "TCP connect against each named port. No payloads were sent, no "
    "authentication or authorisation logic was exercised, and no state on "
    "any target was changed. Absence of a finding is NOT evidence of "
    "absence — this is a point-in-time observation, not a penetration test."
)


async def build_report() -> Dict[str, Any]:
    """The assembled report: scope, methodology, findings, limitations.

    Pure function of stored rows plus fixed text — no model is consulted, so
    the report cannot embellish what was observed.
    """
    from core.security import scope

    findings = await list_findings(limit=1000)
    described = await scope.describe()

    by_severity: Dict[str, List[Dict[str, Any]]] = {level: [] for level in SEVERITIES}
    for finding in findings:
        by_severity.get(finding["severity"], by_severity["info"]).append(finding)

    tools_seen: Dict[str, int] = {}
    targets_seen: List[str] = []
    for finding in findings:
        tools_seen[finding["tool"]] = tools_seen.get(finding["tool"], 0) + 1
        if finding["target"] not in targets_seen:
            targets_seen.append(finding["target"])

    return {
        "generated_at": _now(),
        "scope": described,
        "methodology": {
            "tools": tools_seen,
            "targets": targets_seen,
            "note": METHODOLOGY_NOTE,
        },
        "findings": by_severity,
        "counts": {level: len(items) for level, items in by_severity.items()},
        "total": len(findings),
        "limitations": [
            "Single point-in-time observation; no re-test was performed.",
            "No payloads, fuzzing or authentication attempts were made.",
            "A service that filters this source appears closed, not absent.",
            "Version-matched advisories were not consulted; a disclosed "
            "version is recorded as disclosure, not as a vulnerability.",
        ],
    }


def render_markdown(report: Dict[str, Any]) -> str:
    """The report as readable Markdown.

    Deliberately a deterministic render of the dict above, so the same rows
    always produce the same document — a report whose prose varies per
    generation is harder to diff between runs, which is the one thing an
    assessment report is for.
    """
    scope_section = report.get("scope", {})
    active = scope_section.get("authorizations", [])
    lines = [
        "# Zaytoon authorised assessment report",
        "",
        f"Generated: {report.get('generated_at')}",
        f"Total findings: {report.get('total')}",
        "",
        "## Scope",
        "",
    ]
    live = [a for a in active if not a.get("revoked_at") and not a.get("expired")]
    if live:
        for auth in live:
            lines.append(
                f"- `{auth.get('target')}` — "
                f"{', '.join(auth.get('operations') or [])}"
                f" (authorization {auth.get('id')}"
                + (
                    f", expires {auth.get('expires_at')}"
                    if auth.get("expires_at")
                    else ", no expiry"
                )
                + ")"
            )
    else:
        lines.append("_No active authorizations at report time._")
    lines += ["", "## Methodology", ""]
    tools = report.get("methodology", {}).get("tools", {})
    for tool, count in sorted(tools.items()):
        lines.append(f"- `{tool}` ran and recorded {count} observation(s).")
    lines += ["", report.get("methodology", {}).get("note", ""), "", "## Findings", ""]

    counts = report.get("counts", {})
    for level in SEVERITIES:
        items = report.get("findings", {}).get(level, [])
        if not items:
            continue
        lines.append(f"### {level.upper()} ({counts.get(level, len(items))})")
        lines.append("")
        for finding in items:
            lines.append(f"#### {finding['title']}")
            lines.append("")
            lines.append(f"- **Confidence:** {finding['confidence']}")
            if finding.get("category"):
                lines.append(f"- **Category:** {finding['category']}")
            lines.append(f"- **Target:** `{finding['target']}`")
            if finding.get("evidence"):
                lines.append(f"- **Evidence:** {finding['evidence']}")
            if finding.get("consequence"):
                lines.append(f"- **Consequence:** {finding['consequence']}")
            if finding.get("remediation"):
                lines.append(f"- **Remediation:** {finding['remediation']}")
            lines.append("")
    if report.get("total", 0) == 0:
        lines.append("_No observations were recorded._")
        lines.append("")

    lines += ["## Limitations", ""]
    for item in report.get("limitations", []):
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "CONFIDENCE_LEVELS",
    "METHODOLOGY_NOTE",
    "SEVERITIES",
    "build_report",
    "clear",
    "list_findings",
    "record",
    "render_markdown",
]
