"""Live smoke check for the Ollama path.

Runs a real non-streaming completion and a real streaming completion through
the same LLMClient the agents use. No mocks.

    cd backend && .venv/Scripts/python scripts/smoke_ollama.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import TIER_FAST, TIER_REASONING, config  # noqa: E402
from services.llm_client import AllModelsUnavailableError, LLMClient  # noqa: E402


async def main() -> int:
    print(f"provider={config.PROVIDER} endpoint={config.OLLAMA_BASE_URL}")
    print(
        f"general={config.OLLAMA_GENERAL_MODEL} "
        f"coding={config.OLLAMA_CODING_MODEL} fast={config.OLLAMA_FAST_MODEL}"
    )
    client = LLMClient()

    # 1. Health / availability
    from services.ai_provider import get_provider

    provider = get_provider()
    health = await provider.health_check()
    print(
        f"health: connected={health.connected} version={health.version or '-'} "
        f"models={[m.id for m in health.models]}"
    )
    if not health.connected:
        print(f"FAIL: {health.error}")
        return 1

    # 2. Blocking completion on the fast tier
    try:
        out = await client.complete(
            [{"role": "user", "content": "Reply with exactly: PONG"}],
            tier=TIER_FAST,
            max_tokens=16,
            temperature=0.0,
        )
        print(f"complete(fast) -> {out!r}")
    except AllModelsUnavailableError as e:
        print(f"FAIL complete: {e}")
        return 1

    # 3. Streaming completion on the reasoning tier
    deltas = []

    async def on_delta(d):
        deltas.append(d)

    try:
        full = await client.stream(
            [{"role": "user", "content": "Count from 1 to 3, comma separated."}],
            on_delta=on_delta,
            tier=TIER_REASONING,
            max_tokens=32,
            temperature=0.0,
        )
        print(f"stream(reasoning) -> {full!r} ({len(deltas)} deltas)")
    except AllModelsUnavailableError as e:
        print(f"FAIL stream: {e}")
        return 1

    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
