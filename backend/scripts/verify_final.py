"""Live smoke check: free-only policy + bilingual voice routing + safety gate."""

import asyncio
import json
import logging
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

sys.path.insert(0, ".")

from config import config, is_free_model, NonFreeModelError  # noqa: E402
from services.llm_client import get_llm_client, _AttemptBudget  # noqa: E402
from services.model_registry import is_free_router, spec_for  # noqa: E402
from services.completion_validator import validate_completion  # noqa: E402
from voice import routing  # noqa: E402
from voice.tts import TextToSpeech  # noqa: E402
from tools.safety import (  # noqa: E402
    PERMISSION_CONFIRM,
    PERMISSION_DANGEROUS,
    RiskAssessor,
)
from tools.sandbox import split_command, SecurityError, is_safe_path  # noqa: E402

ARABIC = "مرحبا يا زيتون، إيه الساعة دلوقتي؟"
ENGLISH = "Hello Zaytoon, what time is it right now?"

ok = True


def check(label, condition, detail=""):
    global ok
    if not condition:
        ok = False
    print(f"  [{'PASS' if condition else 'FAIL'}] {label} {detail}")


print("=" * 68)
print("1. FREE-ONLY POLICY")
print("=" * 68)
print(f"  primary   : {config.OPENROUTER_PRIMARY_MODEL}")
print(f"  reasoning : {config.OPENROUTER_REASONING_MODEL}")
print(f"  fallback  : {config.OPENROUTER_FALLBACK_MODEL}")
print(f"  pool      : {config.free_fallback_models}")
print(f"  attempts  : {config.OPENROUTER_MAX_ATTEMPTS}")
print(f"  enforced  : {config.free_models_enforced}")
print(f"  offenders : {config.validate_free_models()}")
check("configured models are all free", config.validate_free_models() == [])
check("primary is :free", is_free_model(config.OPENROUTER_PRIMARY_MODEL))
check("reasoning is :free", is_free_model(config.OPENROUTER_REASONING_MODEL))
check("fallback is :free", is_free_model(config.OPENROUTER_FALLBACK_MODEL))
check(
    "no paid model in any pool member",
    all(is_free_model(m) for m in config.model_pool("fast")
        + config.model_pool("reasoning")),
)

# The auto-router must never be reachable. This is the specific thing that
# produced "User Safety: safe" as the answer to a get_time request.
check(
    "openrouter/free is NOT configured as a fallback",
    not is_free_router(config.OPENROUTER_FALLBACK_MODEL),
)
check(
    "openrouter/free is NOT in the fallback pool",
    not any(is_free_router(m) for m in config.free_fallback_models),
)

chain = get_llm_client().build_chain("fast")
print(f"  chain     : {[e.label for e in chain]}")
check("every chain endpoint is free", all(e.is_free for e in chain))
check("the auto-router is never in a chain", not any(is_free_router(e.model) for e in chain))

tool_chain = get_llm_client().build_chain("fast", ("tools",))
print(f"  tool chain: {[e.label for e in tool_chain]}")
check(
    "every model in a tool chain can call tools",
    all(spec_for(e.model) and spec_for(e.model).supports_tools for e in tool_chain),
)

budget = _AttemptBudget(config.OPENROUTER_MAX_ATTEMPTS)
taken = [budget.take() for _ in range(config.OPENROUTER_MAX_ATTEMPTS + 3)]
check(
    "the attempt budget is finite",
    taken == list(range(1, config.OPENROUTER_MAX_ATTEMPTS + 1)) + [None] * 3,
    f"-> {taken}",
)

leak = validate_completion("User Safety: safe")
check(
    "a provider-metadata leak is rejected",
    not leak.ok,
    f"-> {leak.verdict}",
)
check(
    "ordinary prose is accepted",
    validate_completion("It is 10:30.").ok,
)

print()
print("  attempting a billable model override...")
# A billable model in the pool is *dropped*, not fatal: a typo in one model id
# should degrade to the remaining free models rather than break the assistant.
# The invariant is that it is never called.
originals = {
    "OPENROUTER_PRIMARY_MODEL": config.OPENROUTER_PRIMARY_MODEL,
    "OPENROUTER_REASONING_MODEL": config.OPENROUTER_REASONING_MODEL,
    "OPENROUTER_FALLBACK_MODEL": config.OPENROUTER_FALLBACK_MODEL,
    "OPENROUTER_FREE_FALLBACK_MODELS": config.OPENROUTER_FREE_FALLBACK_MODELS,
}
try:
    config.OPENROUTER_PRIMARY_MODEL = "openai/gpt-4o"
    degraded = [e.model for e in get_llm_client().build_chain("fast")]
    check(
        "a billable model is dropped from the pool, not called",
        "openai/gpt-4o" not in degraded and degraded,
        f"-> {degraded[:2]}",
    )

    # ...and with nothing free left, it must refuse rather than call.
    for attr in (
        "OPENROUTER_PRIMARY_MODEL",
        "OPENROUTER_REASONING_MODEL",
        "OPENROUTER_FALLBACK_MODEL",
    ):
        setattr(config, attr, "openai/gpt-4o")
    config.OPENROUTER_FREE_FALLBACK_MODELS = "openai/gpt-4o"
    try:
        get_llm_client().build_chain("fast")
        check("a billable-only pool is refused", False, "(it was allowed!)")
    except NonFreeModelError as e:
        check("a billable-only pool is refused before any request", True, f"-> {e}")
finally:
    for attr, value in originals.items():
        setattr(config, attr, value)

print()
print("=" * 68)
print("2. BILINGUAL VOICE ROUTING")
print("=" * 68)
tts = TextToSpeech()
print(f"  edge-tts available: {tts.available}")
print(f"  profiles: {json.dumps(routing.describe(), ensure_ascii=False)[:200]}")

ar_voice = tts.voice_for_text(ARABIC)
en_voice = tts.voice_for_text(ENGLISH)
print(f"  arabic text -> {ar_voice}")
print(f"  english text -> {en_voice}")
check("arabic routes to Egyptian voice", ar_voice.startswith("ar-EG"), ar_voice)
check("english routes to British voice", en_voice.startswith("en-GB"), en_voice)
check("voices differ per language", ar_voice != en_voice)
check(
    "fallback chain for ar",
    "ar-EG-SalmaNeural" in routing.voice_chain("ar"),
)
check(
    "auto-switch back to english",
    tts.voice_for_text("Tell me a joke") == en_voice,
)
check("explicit override honoured", tts.resolve_voice("ar-EG") == ar_voice)

print()
print("=" * 68)
print("3. SECURITY SANDBOX")
print("=" * 68)
assessor = RiskAssessor()

# Command chaining is a hard *block*, not an approval prompt: no amount of
# consent should let `del x & shutdown` reach the shell.
BLOCKED_ALWAYS = [
    ("system_command", {"command": "echo hi & del C:\\secret.txt"}),
    ("system_command", {"command": "echo %USERNAME%"}),
]
for action, params in BLOCKED_ALWAYS:
    try:
        split_command(params["command"])
        check(f"chaining {params['command']!r} blocked", False, "(allowed)")
    except SecurityError:
        check(f"chaining {params['command']!r} blocked unconditionally", True)

# Genuinely destructive single commands are gated for human approval.
cases = [
    ("system_command", {"command": "rm -rf /"}),
    ("system_command", {"command": "shutdown /s /t 0"}),
    ("system_command", {"command": "del C:\\Windows\\System32\\config /s"}),
    # Arbitrary terminal execution is confirmation-required for *every*
    # command, including an innocuous-looking one. It used to be gated only
    # when a HIGH_RISK pattern matched, on the reasoning that `echo hello` is
    # harmless — but any command reaches an interpreter, so the harmless case
    # is exactly what an attacker would use to reach a harmful one.
    ("system_command", {"command": "echo hello"}),
    ("file_write", {"path": "C:\\Windows\\System32\\drivers\\etc\\hosts"}),
    ("file_write", {"path": "src/app.py"}),
    ("create_folder", {"path": "AI Projects"}),
    ("run_npm", {"args": "install"}),
    ("type_text", {"text": "hello"}),
    ("key_press", {"keys": "^c"}),
    ("git_commit", {"args": "-m wip"}),
    ("git_status", {}),  # read-only git stays safe; see below
    ("file_delete", {"path": "report.pdf"}),
    ("close_app", {"app": "explorer"}),
]
for action, params in cases:
    a = assessor.assess(action, params)
    gated = a.requires_confirmation
    if action == "git_status":
        # Listed above to prove read-only git is NOT gated.
        gated = not gated
        label = f"{action} {str(params)[:28]!r} read-only (not gated)"
    else:
        label = f"{action} {str(params)[:34]!r} gated"
    check(label, gated, f"-> tier={a.permission} {a.reasons[:1]}")

safe = [
    ("get_time", {}),
    ("system_info", {}),
    ("search_web", {"query": "python"}),
    ("file_read", {"path": "README.md"}),
    ("git_status", {}),
    ("git_log", {}),
    ("git_diff", {}),
    ("open_app", {"app": "notepad"}),
]
for action, params in safe:
    a = assessor.assess(action, params)
    check(
        f"{action} {str(params)[:28]!r} not gated",
        not a.requires_confirmation,
        f"-> tier={a.permission}",
    )

for payload in ("echo a & hostname", "cat x; rm -rf /", "dir > out.txt"):
    try:
        split_command(payload)
        check(f"injection {payload!r} blocked", False, "(allowed)")
    except SecurityError:
        check(f"injection {payload!r} blocked", True)

print()
print("  structured local execution (the two newest tools):")
# The tier is argument-dependent for execute_local_command: `confirm` for an
# ordinary command, escalated to `dangerous` when a hazard pattern matches.
# A regression here would show the user a calm approval dialog for `rm -rf /`.
for action, params, want in [
    ("execute_local_command", {"command": "echo hi"}, PERMISSION_CONFIRM),
    ("execute_local_command", {"command": "rm -rf /"}, PERMISSION_DANGEROUS),
    ("execute_code", {"code": "print(1)", "language": "python"}, PERMISSION_DANGEROUS),
]:
    a = assessor.assess(action, params)
    check(
        f"{action} {params['command'] if 'command' in params else 'print(1)'!r} "
        f"-> {want}",
        a.permission == want and a.requires_confirmation,
        f"-> tier={a.permission} {a.reasons[:1]}",
    )

# system_command's own classification must not have moved when the structured
# tool was added: a stricter tier on a *different* action is fine, a looser
# one on this action would be a silent downgrade.
check(
    "system_command 'rm -rf /' still gated at its own tier",
    assessor.assess("system_command", {"command": "rm -rf /"}).permission
    == PERMISSION_CONFIRM,
)

# Both tools must actually be in the registry the model is shown, and neither
# may offer a `shell` switch — the sandbox checks and argv-only exec mean such
# a flag would add attack surface and nothing else.
from tools.registry import build_default_registry  # noqa: E402

registry = build_default_registry()
names = set(registry.list())
check(
    "execute_local_command + execute_code are registered",
    {"execute_local_command", "execute_code"} <= names,
    f"-> {len(names)} tools",
)
for tool_name in ("execute_local_command", "execute_code"):
    schema = registry.get(tool_name).schema()
    check(
        f"{tool_name} offers no shell parameter",
        "shell" not in (schema.get("properties") or {}),
        f"-> {sorted((schema.get('properties') or {}))}",
    )

# The process tools (increment E). The policy that matters is not their tier —
# the suite checks that — but that starting and stopping ask, and that the
# registry cannot be used to reach a process Zaytoon did not start.
check(
    "process tools are registered",
    {"process_start", "process_list", "process_logs", "process_stop"} <= names,
    f"-> {sorted(n for n in names if n.startswith('process_'))}",
)
_assessor = RiskAssessor()
for _action, _params in (
    ("process_start", {"command": "npm run dev"}),
    ("process_stop", {"id": "proc_1"}),
):
    check(
        f"{_action} always asks",
        _assessor.assess(_action, _params).requires_confirmation,
        f"-> {_assessor.assess(_action, _params).permission}",
    )
for _action in ("process_list", "process_logs"):
    check(
        f"{_action} never asks",
        not _assessor.assess(_action, {"id": "proc_1"}).requires_confirmation,
        "-> inspection is read-only",
    )

from tools.processes import registry as _proc_registry  # noqa: E402

check(
    "an untracked process id cannot be stopped",
    asyncio.run(_proc_registry.stop("proc_never_started")) is None,
    "-> no kill switch for foreign pids",
)

print()
print("  path containment (sibling-prefix escape):")
import tempfile  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    root = f"{td}{'/' if sys.platform != 'win32' else chr(92)}app"
    import os  # noqa: E402

    os.makedirs(root, exist_ok=True)
    sibling = root + "_evil"
    os.makedirs(sibling, exist_ok=True)
    check("inside root allowed", is_safe_path(f"{root}/f.txt", root))
    check("sibling prefix rejected", not is_safe_path(f"{sibling}/f.txt", root))
    check("old startswith check would pass", f"{sibling}".startswith(root))

print()
print("=" * 68)
print("RESULT:", "ALL CHECKS PASSED" if ok else "FAILURES PRESENT")
print("=" * 68)
sys.exit(0 if ok else 1)
