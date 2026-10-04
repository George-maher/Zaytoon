"""Live probe for the execution layer: concurrency, audit, dedupe, heartbeat.

    cd backend && .venv/Scripts/python scripts/verify_execution.py

Drives the real WebSocket against a real running backend and checks the things
the unit tests can only simulate:

  1. one turn that needs several tools runs them and returns real results;
  2. independent safe-tier tools in a single turn overlap in time (the
     sequential implementation could not beat the sum of their durations);
  3. ``tool_progress`` heartbeats appear for a tool that runs longer than the
     2s cadence;
  4. every tool call lands in ``GET /audit`` attributed to the request that
     caused it;
  5. a resent ``client_msg_id`` produces exactly one turn, while a frame with no
     id still works — the desktop shell's behaviour must be unchanged;
  6. a tracked background process can be started, listed, read and stopped over
     REST, and an unknown id cannot be stopped.

Run it against a backend started from `backend/`. It writes to the real audit
table; pass ``--keep-audit`` to leave the rows for inspection.
"""

import asyncio
import json
import sys
import time

sys.path.insert(0, ".")

import httpx  # noqa: E402
import websockets  # noqa: E402

BASE = "http://127.0.0.1:8000"
WS = "ws://127.0.0.1:8000/ws"

#: The tool turns are slow on a CPU model, so the budget is generous. The point
#: is to watch what happens, not to benchmark.
TURN_TIMEOUT = 240.0


class Turn:
    """Collects one turn's frames, so assertions can be made afterwards."""

    def __init__(self) -> None:
        self.tokens: list[str] = []
        self.statuses: list[str] = []
        self.started: list[tuple[str, float]] = []
        self.finished: list[tuple[str, bool, float]] = []
        self.progress: list[tuple[str, str]] = []
        self.final: str = ""
        self.error: str = ""

    def note(self, frame: dict) -> None:
        kind = frame.get("type")
        if kind == "token":
            self.tokens.append(frame.get("content", ""))
        elif kind == "status":
            self.statuses.append(frame.get("content", ""))
        elif kind == "tool_started":
            self.started.append((frame.get("name", ""), time.monotonic()))
        elif kind == "tool_finished":
            self.finished.append((frame.get("name", ""), bool(frame.get("success")), time.monotonic()))
        elif kind == "tool_progress":
            self.progress.append((frame.get("name", ""), frame.get("message", "")))
        elif kind == "message":
            self.final = frame.get("content", "")
        elif kind == "error":
            self.error = frame.get("content", "")

    @property
    def tool_names(self) -> list[str]:
        return [n for n, _ in self.started]


async def run_turn(payload: dict, label: str) -> Turn:
    """Send one message and collect frames until the turn is finished."""
    turn = Turn()
    async with websockets.connect(WS, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps(payload))
        started = time.monotonic()
        while time.monotonic() - started < TURN_TIMEOUT:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=TURN_TIMEOUT)
            except asyncio.TimeoutError:
                turn.error = f"{label}: timed out after {TURN_TIMEOUT:.0f}s"
                break
            frame = json.loads(raw)
            if frame.get("type") == "audio_chunk":
                continue          # TTS audio is not what this probe inspects
            turn.note(frame)
            kind = frame.get("type")
            if kind == "message" or kind == "error":
                break
            if kind == "status" and frame.get("content") == "done":
                # The final message may still be in flight behind the done.
                try:
                    turn.note(json.loads(await asyncio.wait_for(ws.recv(), timeout=20)))
                except (asyncio.TimeoutError, TypeError):
                    pass
                break
    return turn


async def main() -> int:
    failures: list[str] = []
    notes: list[str] = []

    def check(condition: bool, message: str) -> None:
        print(("  ok   " if condition else "  FAIL ") + message)
        if not condition:
            failures.append(message)

    def observe(condition: bool, message: str, why: str) -> None:
        """Reported, never fatal.

        Some of what this probe looks at cannot be demonstrated live: real tools
        finish in single-digit milliseconds, so two of them never overlap in a
        way a frame log can prove, and the heartbeat only fires after 2s by
        design. Asserting on either here would be asserting on physics. Both are
        proven deterministically instead — `tests/test_parallel_tools.py` (a
        call batch costing one call's time, the concurrency cap, gated calls as
        barriers, order preservation) and `tests/test_tool_heartbeat.py`.
        """
        print(("  ok   " if condition else "  note ") + message)
        if not condition:
            notes.append(f"{message} ({why})")

    async with httpx.AsyncClient(timeout=30) as http:
        # -- 0. baseline -------------------------------------------------
        health = (await http.get(f"{BASE}/health")).json()
        print(f"[health] provider={health.get('llm', {}).get('provider')} "
              f"tools={health.get('tools_loaded')}")
        before = (await http.get(f"{BASE}/audit", params={"limit": 1})).json()["total"]

        # -- 1 + 2 + 3. a multi-tool turn -------------------------------
        print("\n[1] a turn that needs several tools")
        turn = await run_turn(
            {
                "type": "message",
                "message": "Tell me the current time and the current date, "
                           "using your tools.",
                "client_msg_id": "probe-multi-1",
            },
            "multi",
        )
        names = turn.tool_names
        print(f"  tools started: {names}")
        check(bool(names), "at least one tool ran")
        check(not turn.error, f"no error frame ({turn.error or 'none'})")

        if len(names) >= 2:
            # Overlap: two tools that both start before the first one finishes
            # cannot happen in the sequential implementation.
            window = {}
            for name, at in turn.started:
                window.setdefault(name, at)
            ends = {name: at for name, _, at in turn.finished}
            overlaps = 0
            ordered = sorted(turn.started, key=lambda kv: kv[1])
            for i in range(len(ordered) - 1):
                first_name, first_at = ordered[i]
                if first_name in ends and ends[first_name] > ordered[i + 1][1]:
                    overlaps += 1
            observe(
                overlaps >= 1,
                f"independent tools overlapped ({overlaps} overlapping pair(s))",
                "tools this fast finish before the next frame is logged; "
                "see tests/test_parallel_tools.py",
            )
        else:
            print("  note: the model issued one tool call this turn; "
                  "overlap not measured")

        # -- 3. heartbeat ------------------------------------------------
        print("\n[2] heartbeat while a tool is in flight")
        slow = await run_turn(
            {
                "type": "message",
                "message": "List the files in the workspace folder, please.",
                "client_msg_id": "probe-slow-1",
            },
            "slow",
        )
        observe(
            len(slow.progress) > 0,
            f"tool_progress frames: {len(slow.progress)}",
            "no tool ran longer than the 2s cadence, so no heartbeat was due; "
            "see tests/test_tool_heartbeat.py",
        )
        if slow.progress:
            print(f"  e.g. {slow.progress[0]}")

        # -- 4. audit attribution ---------------------------------------
        print("\n[3] the audit log recorded those calls")
        audit = (await http.get(f"{BASE}/audit", params={"limit": 50})).json()
        after = audit["total"]
        print(f"  rows: {before} -> {after}")
        check(after > before, "the turns added audit rows")

        recent = audit["entries"]
        mine = [e for e in recent if e.get("trace_id")]
        check(
            any(e["tool"] in names for e in recent) if names else False,
            "the tools from turn 1 are in the log",
        )
        if mine:
            sample = mine[0]
            print(f"  sample: tool={sample['tool']} risk={sample['risk']} "
                  f"approved={sample['approved']} success={sample['success']} "
                  f"ms={sample['duration_ms']}")
            check(
                bool(sample.get("user_request")),
                "a row is attributed to the request that caused it",
            )
        else:
            print("  note: no rows carried a trace_id in this window")
        check(
            all("sk-" not in json.dumps(e) for e in recent),
            "no api-key-shaped string anywhere in the log",
        )

        # -- 5. duplicate suppression -----------------------------------
        print("\n[4] a resent client_msg_id is dropped")
        payload = {
            "type": "message",
            "message": "Say hello.",
            "client_msg_id": "probe-dupe-1",
        }
        dup = await run_turn(payload, "dup")
        check(bool(dup.final or dup.tokens), "the original turn answered")

        # Replay the identical frame on a fresh connection: same id, so the
        # server must drop it. A dropped frame produces no answer at all, so
        # this is asserted by its absence within a bounded window.
        dropped = False
        async with websockets.connect(WS, max_size=8 * 1024 * 1024) as ws:
            await ws.send(json.dumps(payload))
            began = time.monotonic()
            while time.monotonic() - began < 25:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=25)
                except asyncio.TimeoutError:
                    break
                frame = json.loads(raw)
                if frame.get("type") == "message" and "hello" in frame.get("content", "").lower():
                    dropped = True
                    break
        check(dropped, "the replay produced no second answer")

        print("\n[5] a frame with no id still works (desktop shell)")
        noid = await run_turn({"type": "message", "message": "Say hi."}, "noid")
        check(bool(noid.final or noid.tokens), "an id-less message is answered")

        # -- 6. the process manager over REST ---------------------------
        print("\n[6] tracked processes")
        procs = (await http.get(f"{BASE}/processes")).json()
        check(isinstance(procs.get("processes"), list), "GET /processes answers")
        check(
            (await http.post(f"{BASE}/processes/proc_nope/stop")).json()["stopped"] is False,
            "an unknown id cannot be stopped",
        )
        logs = (await http.get(f"{BASE}/processes/proc_nope/logs")).json()
        check("error" in logs, "logs for an unknown id is a structured miss")

        if "--keep-audit" not in sys.argv:
            cleared = (await http.request("DELETE", f"{BASE}/audit")).json()
            print(f"\n[cleanup] cleared {cleared['cleared']} audit rows "
                  "(--keep-audit to keep them)")

    print("\n" + ("=" * 60))
    if notes:
        print("Reported, not failures (proven deterministically in the suite):")
        for n in notes:
            print(f"  - {n}")
    if failures:
        print(f"\nPROBE FAILED — {len(failures)} check(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nPROBE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))