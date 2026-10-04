"""How long does the real tool path take against local Ollama?

    cd backend && .venv/Scripts/python scripts/time_tool_turn.py
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import config  # noqa: E402


async def main() -> int:
    from agents import ToolAgent

    agent = ToolAgent()

    # How big is the prompt the model actually sees?
    catalogue = agent.registry.openai_tools()
    print(f"provider={config.PROVIDER} fast={config.ollama_model_for_tier('fast')}")
    print(f"openai tool specs: {len(catalogue)} tools, "
          f"~{len(str(catalogue))} chars")

    started = time.monotonic()
    try:
        result = await asyncio.wait_for(
            agent.process("what time is it", []),
            timeout=300,
        )
    except asyncio.TimeoutError:
        print(f"TIMEOUT after {time.monotonic() - started:.1f}s")
        return 1
    except TypeError as e:
        # ToolAgent may take different kwargs; report it rather than crash.
        print(f"Agent signature mismatch: {e}")
        return 2
    took = time.monotonic() - started
    print(f"tool turn took {took:.1f}s")
    print(f"result: {str(result)[:400]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
