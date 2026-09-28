"""Empirically verify `supports_tools` for candidate free models.

OpenRouter's `supported_parameters` includes "tools", which means the endpoint
*accepts* the parameter -- not that the model reliably emits a well-formed tool
call. Those are different claims, and the registry must record the second one.

This drives each candidate through the real agent contract and reports the
verdict, so `MODEL_REGISTRY` holds observed facts rather than assumptions.

Run:  python scripts/verify_tool_capability.py
"""

import asyncio
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from agents.engine import build_system_prompt, extract_tool_calls  # noqa: E402
from services.completion_validator import (  # noqa: E402
    contract_from_registry,
    validate_completion,
)
from tools.registry import build_default_registry  # noqa: E402

CANDIDATES = [
    "qwen/qwen3.8-27b:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3.5-lightning:free",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "cohere/north-mini-code:free",
    "poolside/laguna-s-2.1:free",
    "thinkingmachines/inkling-small:free",
]

QUESTION = "What time is it right now? Use the get_time tool."


async def probe(model: str, key: str) -> dict:
    registry = build_default_registry()
    contract = contract_from_registry(
        registry, require_tool=True, allow_prose=False, label="probe"
    )
    system = build_system_prompt(
        "\n".join(
            f"- {t.name()} {t.description()}" for t in registry.tools()
        ),
        "en",
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": QUESTION},
        ],
        "max_tokens": 1024,
        "temperature": 0.1,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "HTTP-Referer": "https://github.com/zaytoon-ai",
        "X-Title": "Zaytoon capability probe",
    }
    async with httpx.AsyncClient(timeout=90) as client:
        try:
            response = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                json=payload,
                headers=headers,
            )
        except Exception as e:
            return {"model": model, "ok": False, "why": f"{type(e).__name__}"}

    if response.status_code != 200:
        return {
            "model": model,
            "ok": False,
            "why": f"HTTP {response.status_code}",
        }

    try:
        content = response.json()["choices"][0]["message"]["content"] or ""
    except Exception:
        return {"model": model, "ok": False, "why": "unparseable body"}

    verdict = validate_completion(content, contract, extract=extract_tool_calls)
    calls, _ = extract_tool_calls(content)
    return {
        "model": model,
        "ok": verdict.ok,
        "why": "" if verdict.ok else verdict.verdict,
        "tool": calls[0].name if calls else "",
        "raw": content[:160].replace("\n", " "),
    }


async def main() -> int:
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        print("OPENROUTER_API_KEY is not set.")
        return 1

    print("Probing each free model for a valid get_time tool call.\n")
    print(f"{'MODEL':<48} {'VERDICT':<26} RAW")
    print("-" * 120)

    good = []
    for model in CANDIDATES:
        result = await probe(model, key)
        if result["ok"]:
            good.append(model)
        verdict = "VALID TOOL CALL" if result["ok"] else (result["why"] or "rejected")
        print(f"{result['model']:<48} {verdict:<26} {result.get('raw', '')}")
        # Be gentle: the free tier is heavily contended.
        await asyncio.sleep(2)

    print()
    if good:
        print("Tool-capable (observed):")
        for model in good:
            print(f"  + {model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
