"""Measure tool-call reliability per local model. Evidence, not a guess.

    cd backend && .venv/Scripts/python scripts/bench_tool_models.py

Every tool model is the wrong choice for some requests, and "the bigger one is
better" is not a safe assumption on CPU. This drives the *engine's real prompt*
through each installed model and applies the *real* contract, so the number it
prints is the one the loop would act on: how often the model produced a tool
call the registry can actually run.

Run it before changing which model serves tool turns.
"""

import asyncio
import sys
import time

sys.path.insert(0, ".")

from agents.engine import (  # noqa: E402
    AgentEngine,
    build_system_prompt,
    extract_tool_calls,
)
from config import config  # noqa: E402
from services.completion_validator import contract_from_registry  # noqa: E402
from tools.registry import build_default_registry  # noqa: E402
from services.ollama_client import get_ollama_client  # noqa: E402

REQUESTS = [
    "What time is it right now? Use the get_time tool.",
    "Read the file AGENTS.md and tell me what it is.",
    "How many files are in the workspace? Use the tools.",
    "Say hello, no tools needed.",
]


async def main() -> int:
    registry = build_default_registry()
    engine = AgentEngine.__new__(AgentEngine)  # prompt only; no LLM needed
    engine.registry = registry
    engine._tool_contract = contract_from_registry(registry, allow_prose=True)
    prompt = build_system_prompt(engine._catalogue("en"), "en")

    models = [
        config.OLLAMA_FAST_MODEL,
        config.OLLAMA_CODING_MODEL,
        config.OLLAMA_GENERAL_MODEL,
    ]
    client = get_ollama_client(base_url=config.OLLAMA_BASE_URL, timeout=240)

    for model in models:
        print(f"\n=== {model} ===")
        valid = 0
        tool_calls = 0
        for request in REQUESTS:
            began = time.monotonic()
            text = ""
            try:
                out = await client.chat(
                    model=model,
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": request},
                    ],
                )
                text = out if isinstance(out, str) else getattr(out, "content", str(out))
            except Exception as exc:  # noqa: BLE001 - this is the measurement
                text = ""
                print(f"  ERROR  {type(exc).__name__} after "
                      f"{time.monotonic() - began:.0f}s")
            took = time.monotonic() - began

            calls, _prose = extract_tool_calls(text or "")
            result = validate = None
            if calls:
                from services.completion_validator import validate_completion

                validate = validate_completion(
                    text, contract_from_registry(registry, allow_prose=True),
                    extract=extract_tool_calls,
                )
            runnable = bool(calls) and bool(validate and validate.ok)
            if runnable:
                valid += 1
            if calls:
                tool_calls += 1
            print(f"  {'ok  ' if runnable else ('prose' if not calls else 'BAD ')} "
                  f"{took:5.0f}s  {request[:44]!r}")
            if calls and not runnable:
                print(f"        names={[c.name for c in calls]} "
                      f"verdict={getattr(validate, 'verdict', '?')}")
        print(f"  -> runnable tool call in {valid}/{len(REQUESTS)}, "
              f"emitted a call in {tool_calls}/{len(REQUESTS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))