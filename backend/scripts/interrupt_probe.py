"""Live check of the STOP path against a running backend.

    python scripts/interrupt_probe.py

Drives the real WebSocket:

  1. send a turn and wait until it is visibly under way (the `intent` frame
     means the dispatch task is running with the LLM call in flight);
  2. hit STOP (`interrupt`) mid-turn;
  3. assert the `interrupted` ack arrives and that the cancelled turn keeps
     delivering nothing (no `message`/`token`/`tool_*` frames);
  4. send another message and assert the session still answers — STOP must
     not wedge the connection.
"""

import asyncio
import json
import sys

try:
    import websockets
except ImportError:
    print("pip install websockets")
    raise SystemExit(1)

WS_URL = "ws://127.0.0.1:8000/ws"

#: Frames that prove the turn is actually executing.
WORK_FRAMES = {"intent", "token", "tool_started", "plan"}
#: Frames that must NOT arrive for a cancelled turn.
DELIVERY_FRAMES = {"message", "token", "tool_started", "audio_chunk"}


async def recv(ws, timeout):
    return json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))


async def main() -> int:
    loop = asyncio.get_event_loop()
    async with websockets.connect(WS_URL, max_size=None) as ws:
        print("[ws] connected")
        await ws.send(json.dumps({
            "type": "message",
            "message": "write a long detailed essay about the history of programming",
        }))
        print("[ws] sent a long turn")

        deadline = loop.time() + 120
        interrupted = False

        while loop.time() < deadline:
            try:
                frame = await recv(ws, timeout=deadline - loop.time())
            except asyncio.TimeoutError:
                print("FAIL: timed out waiting for frames")
                return 1
            kind = frame.get("type")

            if not interrupted:
                if kind in WORK_FRAMES:
                    await ws.send(json.dumps({"type": "interrupt"}))
                    interrupted = True
                    print(f"[stop] turn is live (frame={kind}); sent interrupt")
                continue

            # --- after STOP -------------------------------------------------
            if kind == "interrupted":
                print("[stop] ACK -> interrupted")
                # Nothing more may be delivered for the cancelled turn.
                try:
                    while True:
                        extra = await recv(ws, timeout=10)
                        extra_kind = extra.get("type")
                        print(f"[stop] post-interrupt frame: {extra_kind}")
                        if extra_kind in DELIVERY_FRAMES:
                            print(f"FAIL: cancelled turn still delivering '{extra_kind}'")
                            return 1
                except asyncio.TimeoutError:
                    print("[stop] silence after interrupt (good)")

                # The session must still work.
                await ws.send(json.dumps({"type": "message", "message": "say OK"}))
                print("[ws] sent a follow-up turn")
                while True:
                    try:
                        nxt = await recv(ws, timeout=90)
                    except asyncio.TimeoutError:
                        print("FAIL: session wedged after interrupt")
                        return 1
                    if nxt.get("type") == "message":
                        print(f"[ws] next turn answered: {nxt.get('content')!r}")
                        print("PROBE OK")
                        return 0

            if kind in DELIVERY_FRAMES:
                print(f"FAIL: '{kind}' arrived without an interrupt ack first")
                return 1

        print("FAIL: never saw the interrupt ack")
        return 1


if __name__ == "__main__":
    try:
        code = asyncio.run(main())
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)
