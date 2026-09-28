"""List the free models OpenRouter actually offers right now.

The allow-list in services/model_registry.py was written from memory and two of
the ids turned out not to exist (HTTP 404 at request time). Free-tier
availability on OpenRouter rotates, so the roster has to be verifiable rather
than recalled.

Run:  python scripts/check_free_models.py
"""

import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def free_models(api_key: str) -> list:
    """Every model OpenRouter reports with a zero prompt price."""
    url = "https://openrouter.ai/api/v1/models"
    response = httpx.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=45)
    response.raise_for_status()
    out = []
    for model in response.json().get("data", []):
        pricing = model.get("pricing") or {}
        prompt = str(pricing.get("prompt", "")).strip()
        if prompt in ("0", "0.0", "0.000"):
            out.append(
                {
                    "id": model.get("id", ""),
                    "context": model.get("context_length"),
                    # OpenRouter exposes the advertised capability set here.
                    "supported": (model.get("supported_parameters") or []),
                }
            )
    return sorted(out, key=lambda m: m["id"])


def main() -> int:
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        print("OPENROUTER_API_KEY is not set.")
        return 1

    models = free_models(api_key)
    if not models:
        print("OpenRouter reported no zero-cost models.")
        return 1

    from services.model_registry import ALLOWED_FREE_MODELS, MODEL_REGISTRY

    print(f"{len(models)} free model(s) currently offered by OpenRouter:\n")
    print(f"{'MODEL':<52} {'TOOLS':<6} {'CTX':>9}  ALLOWLISTED")
    print("-" * 84)
    for model in models:
        tools = "yes" if "tools" in model["supported"] else "-"
        listed = "yes" if model["id"] in ALLOWED_FREE_MODELS else ""
        print(
            f"{model['id']:<52} {tools:<6} {str(model['context'] or ''):>9}  {listed}"
        )

    missing = sorted(m for m in ALLOWED_FREE_MODELS if m not in {x['id'] for x in models})
    print()
    if missing:
        print("ALLOW-LISTED BUT NOT AVAILABLE (will 404):")
        for model in missing:
            note = MODEL_REGISTRY[model].notes
            print(f"  - {model}   {note}")
        print()
        print("Fix: replace or remove them in services/model_registry.py.")
    else:
        print("Every allow-listed model is currently available.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
