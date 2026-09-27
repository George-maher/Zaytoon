"""Manual end-to-end check of the tool-confirmation round trip.

Run the backend, then:  python scripts/smoke_confirmation.py [approve|deny|none]
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


async def main(decision):
    async with websockets.connect(WS_URL, max_size=None) as ws:
        print("connected")
        # `diskpart` is on HIGH_RISK_COMMAND_PATTERNS, so the risk assessor
        # must gate it and wait for a human decision.
        await ws.send(json.dumps({
            "type": "message",
            "message": "run the shell command `diskpart`",
        }))

        deadline = asyncio.get_event_loop().time() + 120
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=120)
            except asyncio.TimeoutError:
                print("timed out")
                return
            msg = json.loads(raw)
            kind = msg.get("type")

            if kind == "require_confirmation":
                print("GATED ->", msg["summary"])
                print("  level  :", msg["level"])
                print("  reasons:", msg["reasons"])
                print("  request :", msg["request_id"])
                if decision == "none":
                    print("  (not answering; expecting timeout denial)")
                    continue
                await ws.send(json.dumps({
                    "type": "tool_confirm",
                    "request_id": msg["request_id"],
                    "approved": decision == "approve",
                }))
                print("  replied approved =", decision == "approve")

            elif kind == "confirmation_result":
                print("ACK ->", {k: msg[k] for k in ("approved", "resolved")})

            elif kind == "token":
                print("token:", repr(msg["content"]))

            elif kind == "message":
                print("final:", msg["content"][:200])
                return

            elif kind == "error":
                print("error:", msg.get("content"))
                return

            elif kind == "status":
                print("status:", msg.get("content"))


if __name__ == "__main__":
    decision = sys.argv[1] if len(sys.argv) > 1 else "approve"
    if decision not in ("approve", "deny", "none"):
        print("usage: smoke_confirmation.py [approve|deny|none]")
        raise SystemExit(2)
    asyncio.run(main(decision))
