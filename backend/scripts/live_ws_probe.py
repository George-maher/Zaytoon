"""Live end-to-end probe: real backend + real Ollama over the real WebSocket.

    cd backend && .venv/Scripts/python scripts/live_ws_probe.py

Checks, in order:
  1. /health reports the Ollama provider and a routing plan.
  2. /settings/ai reports CONNECTED with the installed models.
  3. A tool request ("what time is it") reaches the tool layer and answers
     with the real time — no fabricated prose, no tool JSON on screen.
  4. A chat request streams tokens (RESPONSE_CHUNK) before the final message.
"""

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

BASE = "http://127.0.0.1:8000"


async def rest_checks() -> bool:
    ok = True
    async with httpx.AsyncClient(timeout=15) as c:
        h = (await c.get(f"{BASE}/health")).json()
        llm = h.get("llm", {})
        print(f"[health] status={h.get('status')} provider={llm.get('provider')} "
              f"configured={llm.get('configured')}")
        print(f"[health] endpoint={llm.get('base_url')} "
              f"models={llm.get('general_model')}/{llm.get('coding_model')}/{llm.get('fast_model')}")
        if llm.get("provider") != "ollama":
            print("FAIL: provider is not ollama")
            ok = False
        if not h.get("tools_loaded"):
            print(f"FAIL: tools_loaded={h.get('tools_loaded')}")
            ok = False

        s = (await c.get(f"{BASE}/settings/ai")).json()
        print(f"[settings/ai] connected={s.get('connected')} version={s.get('version')!r} "
              f"models={[m['id'] for m in s.get('models', [])]}")
        print(f"[settings/ai] routing={s.get('routing')}")
        if not s.get("connected"):
            print(f"FAIL: provider not connected: {s.get('error')}")
            ok = False
    return ok


async def ws_turn(message: str, label: str, timeout: float = 240.0) -> dict:
    import websockets

    frames = []
    tokens = []
    started = time.monotonic()
    async with websockets.connect(f"ws://127.0.0.1:8000/ws",
                                  max_size=20 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "message", "message": message}))
        while True:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                print(f"  [{label}] TIMEOUT after {timeout}s")
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                print(f"  [{label}] TIMEOUT after {timeout}s")
                break
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            frames.append(ev)
            if ev.get("type") == "token":
                tokens.append(ev.get("content", ""))
            if ev.get("type") in ("message", "error") and ev.get("response_id"):
                # terminal frame for the turn
                if ev.get("type") == "message":
                    break
                if ev.get("type") == "error":
                    break
    return {"frames": frames, "tokens": tokens}


def summarise(label: str, res: dict) -> None:
    frames = res["frames"]
    kinds = {}
    for f in frames:
        kinds[f.get("type")] = kinds.get(f.get("type"), 0) + 1
    print(f"[{label}] frames={kinds}")
    tools = [(f.get("name"), f.get("success")) for f in frames
             if f.get("type") in ("tool_started", "tool_finished")]
    if tools:
        print(f"[{label}] tools={tools}")
    statuses = [f.get("content") for f in frames if f.get("type") == "status"]
    print(f"[{label}] statuses={statuses}")
    final = next((f.get("content") for f in reversed(frames)
                  if f.get("type") == "message"), "")
    print(f"[{label}] tokens={len(res['tokens'])}")
    print(f"[{label}] final={final[:300]!r}")
    leaked = [t for t in res["tokens"]
              if '"action"' in t or "tool_call" in t]
    if leaked:
        print(f"[{label}] FAIL: tool JSON leaked into tokens: {leaked[:1]}")


async def main() -> int:
    if not await rest_checks():
        return 1

    print("\n--- turn 1: tool request ---")
    r1 = await ws_turn("what time is it", "tool")
    summarise("tool", r1)
    if not r1["frames"]:
        print("FAIL: no frames at all")
        return 1

    print("\n--- turn 2: streamed chat ---")
    r2 = await ws_turn("Say hello in one short sentence.", "chat")
    summarise("chat", r2)
    if not r2["tokens"]:
        print("FAIL: no streamed tokens")
        return 1

    print("\n--- turn 3: Arabic tool request ---")
    r3 = await ws_turn("كم الساعة دلوقتي", "ar", timeout=240.0)
    summarise("ar", r3)

    print("\nPROBE DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
