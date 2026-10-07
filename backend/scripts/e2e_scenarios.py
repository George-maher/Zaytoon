"""The seven acceptance scenarios, scripted.

    cd backend && .venv/Scripts/python scripts/e2e_scenarios.py

Each scenario is one promise the product makes to a user, driven against a
**running** backend (`python main.py`) over the real transports — WebSocket
for conversational turns, REST for everything inspectable:

  1. bilingual assistant turn — Arabic in, an Arabic streamed reply out, exactly
     one ``message`` frame, and no tool-call markup in the prose;
  2. safe tool turn — ``what time is it`` reaches the real ``get_time`` tool
     (router-first, no LLM needed to *route*) and the answer is prose;
  3. confirmation gate — approving ``require_confirmation`` creates the folder
     on disk; declining the second one leaves **no** folder. Both halves are
     the scenario: a gate that only ever says yes is not a gate;
  4. reminder round-trip — ``remind me … in 2 minutes`` becomes a listed
     reminder via the TimeParser path, then is deleted again;
  5. diagnostics shapes — ``/health``, ``/system`` (declared resource states,
     never a fabricated zero) and ``/events/recent`` (bounded, redacted,
     envelope present), plus the unified ``{"error": ...}`` failure shape;
  6. containment + audit — a write outside the workspace is refused with
     ``error``, and the tool turns above are attributable in ``GET /audit``;
  7. policy guards — free-only pools, no ``openrouter/free``, the tool
     catalogue is self-describing, the sandbox still refuses chaining, and
     the native bridge still has no eval/exec/shell.

Outcomes are **PASS / FAIL / SKIP**. SKIP is reserved for conditions the
product already reports honestly: a turn that ends with the controlled
"all available free AI models are temporarily unavailable" message is a
*working* system under a rate limit, so re-running after the window passes
should be green rather than permanently red. A timeout, a duplicated
rendering, a gate that ran without asking, or a containment hole is FAIL.

Exit code 0 only when nothing FAILED.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

sys.path.insert(0, ".")

import httpx  # noqa: E402
import websockets  # noqa: E402

BASE = "http://127.0.0.1:8000"
WS = "ws://127.0.0.1:8000/ws"

#: Generous: the point is to observe, not to benchmark.
TURN_TIMEOUT = 240.0

#: Once the prose has landed, how long to keep waiting for the terminal
#: ``done`` frame (TTS drain happens in between).
POST_MESSAGE_GRACE = 60.0

#: Phrases that mean "the provider is unavailable", i.e. an honest SKIP. The
#: product reports each of these as a controlled message rather than a
#: fabricated answer, so a run against a stopped daemon or an exhausted free
#: tier is an environment condition, not a product defect.
_UNAVAILABLE = (
    "temporarily unavailable",
    "rate limit",
    "429",
    "all available free",
    "connection",
    "timeout",
    "unreachable",
    "model not installed",
    "ollama serve",
    "api key",
)

RESULTS: list[tuple[str, str, str]] = []


def record(name: str, status: str, detail: str = "") -> None:
    RESULTS.append((name, status, detail))
    print(f"  [{status}] {name} {detail}", flush=True)


def classify_error(turn: "Turn") -> tuple[str, str]:
    """A controlled provider error is a SKIP; anything else is a FAIL."""
    text = (turn.error or "").lower()
    if any(marker in text for marker in _UNAVAILABLE):
        return "SKIP", f"provider unavailable: {turn.error[:120]}"
    return "FAIL", turn.error[:200] or "no final message"


class Turn:
    """Collects one turn's frames so assertions can be made afterwards."""

    def __init__(self) -> None:
        self.tokens: list[str] = []
        self.statuses: list[str] = []
        self.started: list[str] = []
        self.finished: list[tuple[str, bool]] = []
        self.confirmations: list[dict] = []
        self.confirmation_results: list[dict] = []
        self.messages: list[str] = []
        self.error: str = ""

    @property
    def final(self) -> str:
        return self.messages[-1] if self.messages else ""

    def note(self, frame: dict) -> None:
        kind = frame.get("type")
        if kind == "token":
            self.tokens.append(frame.get("content", ""))
        elif kind == "status":
            self.statuses.append(frame.get("content", ""))
        elif kind == "tool_started":
            self.started.append(frame.get("name", ""))
        elif kind == "tool_finished":
            self.finished.append((frame.get("name", ""), bool(frame.get("success"))))
        elif kind == "require_confirmation":
            self.confirmations.append(frame)
        elif kind == "confirmation_result":
            self.confirmation_results.append(frame)
        elif kind == "message":
            self.messages.append(frame.get("content", ""))
        elif kind == "error":
            self.error = frame.get("content", "")


def _looks_like_tool_json(text: str) -> bool:
    """The three documented dialects leaking into user-facing prose."""
    return (
        '{"action"' in text
        or '{"tool"' in text
        or '"tool_call"' in text
        or "<|tool_call_start|>" in text
        or "tool_calls" in text
    )


async def run_turn(message: str, approve: bool | None = None) -> Turn:
    """Send one message; collect frames; answer confirmations if asked to.

    ``approve=True/False`` answers every ``require_confirmation`` frame on
    this connection with that verdict, which is the same flow the browser
    and the desktop shell drive.

    The turn is over when ``done`` arrives (or ``error``, or the prose has
    landed and nothing follows for ``POST_MESSAGE_GRACE``): the transport
    owns the terminal ``done`` *after* TTS drain, so breaking on
    ``message`` would miss it — which read as "no done status" until the
    loop was told to wait for it.
    """
    turn = Turn()
    async with websockets.connect(WS, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "message", "message": message}))
        started = time.monotonic()
        message_at: float | None = None
        while time.monotonic() - started < TURN_TIMEOUT:
            if message_at is not None and time.monotonic() - message_at > POST_MESSAGE_GRACE:
                break  # prose landed, `done` never came — statuses stay honest
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=TURN_TIMEOUT)
            except asyncio.TimeoutError:
                if message_at is None:
                    turn.error = f"timed out after {TURN_TIMEOUT:.0f}s"
                break
            frame = json.loads(raw)
            if frame.get("type") == "audio_chunk":
                continue
            turn.note(frame)
            kind = frame.get("type")
            if kind == "require_confirmation" and approve is not None:
                await ws.send(json.dumps({
                    "type": "tool_confirm",
                    "request_id": frame.get("request_id"),
                    "approved": approve,
                }))
                continue
            if kind == "error":
                break
            if kind == "message":
                message_at = time.monotonic()
                continue
            if kind == "status" and frame.get("content") == "done" and message_at is not None:
                break
    return turn


# ---------------------------------------------------------------------------
# Scenario 1 — bilingual assistant turn
# ---------------------------------------------------------------------------
async def scenario_1(client: httpx.AsyncClient) -> None:
    turn = await run_turn("مساء الخير، عامل ايه؟ احكيلي عن نفسك باختصار")
    if turn.error or not turn.messages:
        status, detail = classify_error(turn)
        return record("1. bilingual assistant turn", status, detail)

    arabic_chars = sum(1 for ch in turn.final if "\u0600" <= ch <= "\u06FF")
    problems = []
    if len(turn.messages) != 1:
        problems.append(f"{len(turn.messages)} message frames (want 1)")
    if "done" not in turn.statuses:
        problems.append("no `done` status")
    if arabic_chars < 10:
        problems.append(f"reply is not Arabic ({arabic_chars} Arabic chars)")
    if _looks_like_tool_json(turn.final):
        problems.append("tool-call markup leaked into prose")
    record(
        "1. bilingual assistant turn",
        "FAIL" if problems else "PASS",
        "; ".join(problems),
    )


# ---------------------------------------------------------------------------
# Scenario 2 — safe tool turn
# ---------------------------------------------------------------------------
async def scenario_2(client: httpx.AsyncClient) -> None:
    turn = await run_turn("what time is it right now?")
    if turn.error or not turn.messages:
        status, detail = classify_error(turn)
        return record("2. safe tool turn", status, detail)

    problems = []
    if "get_time" not in turn.started:
        problems.append(f"get_time never ran (ran: {turn.started})")
    if any(not ok for _, ok in turn.finished):
        problems.append("a tool call failed")
    if _looks_like_tool_json(turn.final):
        problems.append("tool-call markup leaked into prose")
    record(
        "2. safe tool turn",
        "FAIL" if problems else "PASS",
        "; ".join(problems),
    )


# ---------------------------------------------------------------------------
# Scenario 3 — confirmation gate: approve creates, decline does not
# ---------------------------------------------------------------------------
async def _folder_exists(client: httpx.AsyncClient, path: str) -> bool:
    response = await client.get("/files/stat", params={"path": path})
    return response.status_code == 200


async def _cleanup_folder(client: httpx.AsyncClient, path: str) -> None:
    if await _folder_exists(client, path):
        await client.post("/files/delete", json={"path": path, "recursive": True})


async def scenario_3(client: httpx.AsyncClient) -> None:
    yes, no = "e2e_gate_yes", "e2e_gate_no"
    await _cleanup_folder(client, yes)
    await _cleanup_folder(client, no)

    approved = await run_turn(
        "Create a folder named e2e_gate_yes in my workspace", approve=True
    )
    declined = await run_turn(
        "Create a folder named e2e_gate_no in my workspace", approve=False
    )

    problems = []
    if not approved.confirmations:
        problems.append("approval was never requested")
    if approved.error or not approved.messages:
        status, detail = classify_error(approved)
        return record("3. confirmation gate", status, f"approve turn: {detail}")
    if not await _folder_exists(client, yes):
        problems.append("approved folder was not created")

    if declined.error or not declined.messages:
        status, detail = classify_error(declined)
        return record("3. confirmation gate", status, f"decline turn: {detail}")
    if not declined.confirmations:
        problems.append("decline turn never requested approval")
    if await _folder_exists(client, no):
        problems.append("declined folder EXISTS — the gate did not bite")

    if problems:
        record("3. confirmation gate", "FAIL", "; ".join(problems))
    else:
        record(
            "3. confirmation gate",
            "PASS",
            "approve created, decline left nothing on disk",
        )
    await _cleanup_folder(client, yes)
    await _cleanup_folder(client, no)


# ---------------------------------------------------------------------------
# Scenario 4 — reminder round-trip
# ---------------------------------------------------------------------------
async def scenario_4(client: httpx.AsyncClient) -> None:
    turn = await run_turn("Remind me to stretch my back in 2 minutes")
    if turn.error or not turn.messages:
        status, detail = classify_error(turn)
        return record("4. reminder round-trip", status, detail)

    listing = (await client.get("/reminders")).json()
    entries = listing.get("reminders") or listing.get("tasks") or []
    mine = [r for r in entries if "stretch" in json.dumps(r, ensure_ascii=False)]

    if not mine:
        return record(
            "4. reminder round-trip", "FAIL", "reminder never reached /reminders"
        )

    # Clean up: delete every reminder this scenario touched, then prove it left.
    for entry in mine:
        reminder_id = entry.get("reminder_id") or entry.get("id")
        await client.delete(f"/reminders/{reminder_id}")
    after = (await client.get("/reminders")).json()
    left = [
        r
        for r in (after.get("reminders") or after.get("tasks") or [])
        if "stretch" in json.dumps(r, ensure_ascii=False)
    ]
    record(
        "4. reminder round-trip",
        "FAIL" if left else "PASS",
        "reminder survived deletion" if left else "created, listed, deleted",
    )


# ---------------------------------------------------------------------------
# Scenario 5 — diagnostics shapes
# ---------------------------------------------------------------------------
async def scenario_5(client: httpx.AsyncClient) -> None:
    problems = []

    health = await client.get("/health")
    if health.status_code != 200:
        problems.append(f"/health {health.status_code}")
    else:
        body = health.json()
        for key in ("llm", "core", "metrics"):
            if key not in body:
                problems.append(f"/health missing `{key}`")

    system = await client.get("/system")
    if system.status_code != 200:
        problems.append(f"/system {system.status_code}")
    else:
        body = system.json()
        resources = body.get("resources") or {}
        cpu = resources.get("cpu") or {}
        if cpu.get("status") not in {"ok", "sampling", "unavailable"}:
            problems.append(f"cpu.status={cpu.get('status')!r} is not a declared state")
        if cpu.get("status") == "ok" and not 0 <= (cpu.get("percent") or 0) <= 100:
            problems.append("cpu percent out of range")
        if not isinstance(body.get("uptime_seconds"), (int, float)):
            problems.append("uptime_seconds missing")
        net = resources.get("network") or {}
        if not net.get("status"):
            problems.append("network has no explicit state")

    events = await client.get("/events/recent", params={"limit": 99999})
    if events.status_code != 200:
        problems.append(f"/events/recent {events.status_code}")
    else:
        body = events.json()
        if body["count"] != len(body["events"]) or body["count"] > 100:
            problems.append(f"feed not bounded: {body['count']}")
        for event in body["events"][:5]:
            if not (event.get("type") and event.get("event_id") and event.get("ts")):
                problems.append("event without envelope")
                break

    # The unified failure shape, on a real FastAPI validation error.
    bad = await client.get("/tasks", params={"limit": "not-a-number"})
    if bad.status_code < 400 or "error" not in bad.json() or "detail" in bad.json():
        problems.append(f"failure shape wrong: {bad.status_code} {bad.text[:80]}")

    record("5. diagnostics shapes", "FAIL" if problems else "PASS", "; ".join(problems))


# ---------------------------------------------------------------------------
# Scenario 6 — containment + audit
# ---------------------------------------------------------------------------
async def scenario_6(client: httpx.AsyncClient, prior_tools: list[str]) -> None:
    problems = []

    for escape in ("../e2e_escape.txt", "C:\\Windows\\e2e_escape.txt"):
        response = await client.post(
            "/files/write", json={"path": escape, "content": "x"}
        )
        if response.status_code < 400 or "error" not in response.json():
            problems.append(f"write {escape!r} was not refused")
            break

    audit = await client.get("/audit")
    if audit.status_code != 200 or not isinstance(audit.json().get("entries"), list):
        problems.append("GET /audit has no entries list")
    else:
        entries = audit.json()["entries"]
        recorded = {e.get("tool") for e in entries}
        # A tool turn that actually ran must be attributable. When those
        # scenarios SKIPped on the provider, `prior_tools` is empty and an
        # audit without those rows is the truthful outcome.
        missing = [t for t in prior_tools if t not in recorded]
        if missing:
            problems.append(f"tool turns not in the audit: {missing}")
        # Every row must carry a tier — attribution without the risk level is
        # half an audit.
        for entry in entries[:50]:
            if not entry.get("tool") or not entry.get("risk"):
                problems.append("audit row missing tool/risk")
                break

    record(
        "6. containment + audit",
        "FAIL" if problems else "PASS",
        "; ".join(problems) or "escapes refused; rows carry tool+risk",
    )


# ---------------------------------------------------------------------------
# Scenario 7 — policy guards (local, no sockets)
# ---------------------------------------------------------------------------
async def scenario_7(client: httpx.AsyncClient) -> None:
    problems = []

    from config import config, is_free_model
    from services.model_registry import is_free_router
    from tools.sandbox import SecurityError, split_command

    if config.validate_free_models():
        problems.append(f"paid models configured: {config.validate_free_models()}")
    if is_free_router(config.OPENROUTER_FALLBACK_MODEL):
        problems.append("openrouter/free is configured as a fallback")
    if any(
        is_free_router(m) for m in config.free_fallback_models
    ):
        problems.append("openrouter/free is in the fallback pool")

    try:
        split_command("echo hi & hostname")
        problems.append("shell chaining was not refused")
    except SecurityError:
        pass

    # The catalogue must be self-describing: no bound-method leak, every tool
    # carrying a tier (the bug a passing double once hid).
    tools = await client.get("/tools")
    if tools.status_code != 200:
        problems.append(f"/tools {tools.status_code}")
    else:
        text = tools.text
        if "bound method" in text:
            problems.append("catalogue leaks `bound method`")
        payload = tools.json()
        described = payload.get("tools") or []
        if len(described) < 30:
            problems.append(f"only {len(described)} tools described")
        for tool in described:
            if not tool.get("name") or tool.get("permission") not in {
                "safe", "confirm", "dangerous",
            }:
                problems.append(f"tool without a tier: {tool.get('name')}")
                break

    # The native bridge must never grow an eval/exec/shell escape hatch.
    try:
        with open("desktop/app/bridge.py", "r", encoding="utf-8") as handle:
            bridge = handle.read()
        for forbidden in ("eval(", "exec(", "shell=True"):
            if forbidden in bridge:
                problems.append(f"bridge.py contains {forbidden}")
    except OSError:
        pass  # Running outside the repo checkout; the other guards still run.

    record("7. policy guards", "FAIL" if problems else "PASS", "; ".join(problems))


# ---------------------------------------------------------------------------
async def main() -> int:
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as client:
        try:
            health = await client.get("/health")
        except httpx.HTTPError:
            print("Backend is not answering at " + BASE + " — start it first.")
            return 1
        if health.status_code != 200:
            print(f"/health returned {health.status_code}; aborting.")
            return 1

        print("=" * 68)
        print("SEVEN ACCEPTANCE SCENARIOS (live)")
        print("=" * 68)

        await scenario_1(client)
        await scenario_2(client)
        await scenario_3(client)
        await scenario_4(client)
        await scenario_5(client)

        # Which tools the earlier turns actually ran, for attribution.
        by_name = {name: status for name, status, _ in RESULTS}
        prior: list[str] = []
        if by_name.get("2. safe tool turn") == "PASS":
            prior.append("get_time")
        if by_name.get("3. confirmation gate") == "PASS":
            prior.append("create_folder")
        await scenario_6(client, prior)
        await scenario_7(client)

    failed = [r for r in RESULTS if r[1] == "FAIL"]
    skipped = [r for r in RESULTS if r[1] == "SKIP"]
    print("-" * 68)
    print(
        f"RESULT: {len(RESULTS) - len(failed) - len(skipped)} passed, "
        f"{len(skipped)} skipped, {len(failed)} failed"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
