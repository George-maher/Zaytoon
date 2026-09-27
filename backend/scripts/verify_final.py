"""Live smoke check: free-only policy + bilingual voice routing + safety gate."""

import json
import logging
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

sys.path.insert(0, ".")

from config import config, is_free_model, NonFreeModelError  # noqa: E402
from services.llm_client import get_llm_client  # noqa: E402
from voice import routing  # noqa: E402
from voice.tts import TextToSpeech  # noqa: E402
from tools.safety import RiskAssessor  # noqa: E402
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
print(f"  enforced  : {config.free_models_enforced}")
print(f"  offenders : {config.validate_free_models()}")
check("configured models are all free", config.validate_free_models() == [])
check("primary is :free or router", is_free_model(config.OPENROUTER_PRIMARY_MODEL))
check("reasoning is :free or router", is_free_model(config.OPENROUTER_REASONING_MODEL))
check("fallback is :free or router", is_free_model(config.OPENROUTER_FALLBACK_MODEL))

chain = get_llm_client().build_chain("fast")
print(f"  chain     : {[e.label for e in chain]}")
check(
    "every chain endpoint is free",
    all(e.is_free for e in chain),
)

print()
print("  attempting a billable model override...")
original = config.OPENROUTER_PRIMARY_MODEL
try:
    config.OPENROUTER_PRIMARY_MODEL = "openai/gpt-4o"
    try:
        get_llm_client().build_chain("fast")
        check("billable model rejected", False, "(it was allowed!)")
    except NonFreeModelError as e:
        check("billable model rejected before any request", True, f"-> {e}")
finally:
    config.OPENROUTER_PRIMARY_MODEL = original

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
