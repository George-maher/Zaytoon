"""Live proof: the provider registry against a real local endpoint (Ollama /v1).

No mocks anywhere in this file. Every step below is a real HTTP request to the
running local Ollama daemon through the real registry code — the same functions
the FastAPI routes call (the route wiring itself is covered by the suite's
TestClient tests, so what is left to prove live is the *transport*).

Proven here:

* an `openai_compatible` provider registers against a live endpoint (loopback
  is deliberately allowed — the local daemon is the primary use case);
* refresh records the daemon's real catalog;
* the reachability probe passes but does NOT complete setup;
* test-chat performs a real completion and flips setup status;
* the registry-first LLMClient answers a real question through this provider;
* a dead endpoint fails with a readable reason, not a stack;
* no result anywhere carries key material.

Usage (Ollama must be running):

    cd backend && .venv/Scripts/python.exe scripts/verify_providers_live.py
"""

import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Storage redirect BEFORE any core import, same discipline as the test fixtures.
_TMP = tempfile.mkdtemp(prefix="zaytoon_live_")
os.environ["CORE_DB_PATH"] = os.path.join(_TMP, "core.db")

OLLAMA = "http://localhost:11434"
# The 3B model, deliberately: on this Windows host Ollama's llama-server
# crashes with a stack-buffer overrun (0xc0000409) loading qwen3.5:9b in the
# OpenAI-compat path — an upstream fault, correctly surfaced by our transport
# as HTTP 500 → kind=server_error. Proving our code must not depend on it.
TEST_MODEL = "qwen2.5-coder:3B-Instruct"
CHECKS: list = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


async def run():
    # One event loop for everything: aiosqlite connections are bound to the
    # loop that created them, so mixing asyncio.run with any second loop is
    # the classic cross-loop break this script must not have.
    from core.api.registry_api import provider_test_chat, setup_status
    from core.providers import get_provider, list_models, save_provider
    from core.providers.router import refresh_models

    snapshots: list = []

    def snap(result):
        """Collect every value that crosses back to a caller, for the leak sweep."""
        snapshots.append(json.dumps(result, ensure_ascii=False, default=str))
        return result

    # 1. Fresh install: setup is required until a real chat test passes.
    status = snap(await setup_status())
    check("fresh install requires setup", status["setup_required"] is True,
          f"providers={status['providers_total']}")

    # 2. Register the local daemon as an OpenAI-compatible provider.
    provider = snap(await save_provider(
        None, name="Local Ollama (live)", kind="openai_compatible",
        base_url=OLLAMA,
    ))
    pid = provider["id"]
    check("provider registered (live loopback endpoint accepted)", bool(pid))

    # 3. Refresh: the real catalog, recorded as rows.
    refreshed = snap(await refresh_models(pid))
    models = snap(await list_models(provider_id=pid, enabled_only=False))
    check("catalog refresh recorded the real models",
          refreshed.get("discovered", 0) >= 1 and len(models) >= 1,
          f"discovered={refreshed.get('discovered')} recorded={len(models)} "
          f"ids={[m['id'] for m in models]}")

    # 4. Reachability probe records verified_at — but not setup.
    # The *route function*, not raw provider_health: the stamping happens in
    # the route (`provider_test`), which is the surface we must prove.
    from core.api.registry_api import provider_test

    probed = snap(await provider_test(pid))
    check("reachability probe passes", probed.get("connected") is True,
          f"endpoint={probed.get('endpoint')}")
    row = snap(await get_provider(pid))
    check("probe set verified_at but not chat_verified_at",
          bool(row.get("verified_at")) and not row.get("chat_verified_at"))
    check("reachability alone does NOT complete setup",
          (await setup_status())["setup_required"] is True)

    # 5. test-chat: a REAL 32-token completion through the registry client.
    chat = snap(await provider_test_chat(
        pid, {"message": "Reply with the single word: OK", "model": TEST_MODEL}))
    check("test-chat generated a real completion", chat.get("ok") is True,
          f"model={chat.get('model')} latency={chat.get('latency_ms')}ms "
          f"reply={chat.get('reply')!r}" if chat.get("ok") else
          f"error={chat.get('error')!r}")
    row = snap(await get_provider(pid))
    check("test-chat stamped chat_verified_at",
          bool(row.get("chat_verified_at")) and
          row["chat_verified_at"] == chat.get("verified_at"))

    # 6. Setup is now complete, per the same function /setup/status serves.
    done = snap(await setup_status())
    check("setup completed after the real chat test",
          done["setup_required"] is False and done["providers_chat_verified"] == 1)

    # 7. Registry-first LLMClient answers a real question through this provider.
    # The 3B model is pinned to priority 1 so `plan()` picks it: the 9b crash
    # is intermittent, and a proof script must not depend on a coin flip.
    from core.providers import save_model as _model, save_provider as _save

    for row in models:
        await _model(pid, row["id"], capabilities=row["capabilities"],
                     priority=1 if row["id"] == TEST_MODEL else 2)
    await _save(pid, name=provider["name"], kind=provider["kind"],
                base_url=provider["base_url"], is_default=True)
    from services.llm_client import LLMClient

    answer = await LLMClient().complete([
        {"role": "user", "content": "What is 2+2? Answer with just the number."},
    ])
    check("LLMClient answered through the registry provider",
          bool(answer and answer.strip()),
          f"reply={answer.strip()[:60]!r}" if answer else "no answer")

    # 8. A dead endpoint fails with a readable reason, not a stack.
    dead = snap(await save_provider(
        None, name="Dead endpoint (live)", kind="openai_compatible",
        base_url="http://localhost:9",
    ))
    from core.providers import save_model

    await save_model(dead["id"], "ghost", capabilities=["chat"])
    failed = snap(await provider_test_chat(dead["id"], {}))
    check("dead endpoint fails with a reason, not an exception",
          failed.get("ok") is False and bool(failed.get("error")),
          f"error={failed.get('error')!r}")
    check("a failed test-chat leaves setup alone",
          (await setup_status())["providers_chat_verified"] == 1)

    # 9. Key-material sweep across every value that crossed to a caller.
    leaked = [s for s in snapshots if '"api_key"' in s]
    check("no result ever carries an api_key field",
          not leaked, f"{len(snapshots)} results swept")


def main():
    t0 = time.time()
    asyncio.run(run())
    failed = [c for c in CHECKS if not c[1]]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} live checks passed")
    if failed:
        print("FAILED: " + "; ".join(c[0] for c in failed))
        sys.exit(1)
    print("RESULT: PASS — the provider registry works against a real endpoint")
    print(f"completed in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
