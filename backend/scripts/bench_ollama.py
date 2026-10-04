"""Where does the tool-turn latency go? Asks Ollama directly and prints the
server-side timings (prompt_eval vs generation).

    cd backend && .venv/Scripts/python scripts/bench_ollama.py
"""

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from config import config  # noqa: E402


async def one(client, model, label, messages, options=None):
    payload = {"model": model, "messages": messages, "stream": False,
               "options": options or {}}
    t0 = time.monotonic()
    r = await client.post(f"{config.OLLAMA_BASE_URL}/api/chat", json=payload)
    took = time.monotonic() - t0
    r.raise_for_status()
    d = r.json()
    print(f"[{label}] model={model} wall={took:.1f}s "
          f"prompt_tokens={d.get('prompt_eval_count')} "
          f"prompt_s={d.get('prompt_eval_duration', 0) / 1e9:.1f} "
          f"gen_tokens={d.get('eval_count')} "
          f"gen_s={d.get('eval_duration', 0) / 1e9:.1f} "
          f"load_s={d.get('load_duration', 0) / 1e9:.1f}")
    msg = d.get("message", {}).get("content", "")
    print(f"[{label}] reply={msg[:120]!r}")
    return d


async def main() -> int:
    fast = config.ollama_model_for_tier("fast")
    general = config.ollama_model_for_tier("general")

    # 1. Tiny prompt on the fast model (baseline).
    async with httpx.AsyncClient(timeout=600) as c:
        await one(c, fast, "tiny/fast", [{"role": "user", "content": "Say OK."}])

        # 2. A ~4k-token system prompt, i.e. the tool catalogue's size.
        big = "You are Zaytoon. Tools:\n" + ("JSON tool spec line: "
               '{"action": "tool_name", "params": {...}},\n' * 900)
        print(f"big prompt chars={len(big)}")
        await one(c, fast, "big/fast", [
            {"role": "system", "content": big},
            {"role": "user", "content": "what time is it"},
        ], options={"num_predict": 64})

        # 3. Same big prompt on the general model — only when asked, because
        #    on CPU this is the slow one.
        if "--with-general" in sys.argv:
            await one(c, general, "big/general", [
                {"role": "system", "content": big},
                {"role": "user", "content": "what time is it"},
            ], options={"num_predict": 16})
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
