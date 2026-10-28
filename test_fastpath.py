import sys
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# Replicate fast_path components
_TRAILING_PUNCT = " \t\r\n.!?؟،,؛:"

def _capture(name: str) -> str:
    return f"(?P<{name}>[^.,؛;]+?)"

def normalise(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    return cleaned.strip(_TRAILING_PUNCT).lower()

# ALLOWED_TOOLS from fast_path
ALLOWED_TOOLS = frozenset({
    "get_time",
    "get_volume",
    "system_info",
    "screenshot",
    "process_list",
    "open_app",
})

# INTENTS from fast_path - open_app intent
app_patterns = (
    rf"(?:open|launch|start)(?: up| the)? {_capture('app')}",
    rf"(?:please )?open (?:the )?{_capture('app')} (?:app|application)",
    rf"(?:افتح|شغل|قم بتشغيل|تشغيل)(?: ليا| لي)? {_capture('app')}",
)

# Build INTENTS like fast_path does
@dataclass
class Intent:
    tool: str
    patterns: Tuple[str, ...]
    params: Dict[str, Any]
    label: Dict[str, str]
    
    def compiled(self) -> List[re.Pattern]:
        return [re.compile(p, re.IGNORECASE) for p in self.patterns]

INTENTS = (
    Intent(
        tool="open_app",
        patterns=app_patterns,
        params={},
        label={"en": "Opening {value}", "ar": "أفتحت {value}"},
    ),
)

# CONJUNCTIONS from fast_path
_CONJUNCTIONS = re.compile(
    r"""(?:\b(?:and|then|plus|afterwards|also)\b|،|\sو\s|ثم|وبعد)""",
    re.IGNORECASE,
)

def match(text: str):
    value = normalise(text)
    if not value or len(value) > 120:
        return None
    if _CONJUNCTIONS.search(value):
        return None

    for intent in INTENTS:
        if intent.tool not in ALLOWED_TOOLS:
            continue
        for pattern in intent.compiled():
            found = pattern.fullmatch(value)
            if not found:
                continue
            params: Dict[str, Any] = dict(intent.params)
            for key, captured in (found.groupdict() or {}).items():
                if captured and captured.strip():
                    params[key] = captured.strip(_TRAILING_PUNCT).strip()
            if intent.tool == "open_app":
                app = str(params.get("app", "")).strip()
                if not app or len(app) > 60 or " " in app and len(app.split()) > 3:
                    return None
                params["app"] = app
            return intent.tool, params
    return None

# Test cases
test_cases = [
    'open VS Code',
    'open notepad',
    'open calculator', 
    'open chrome',
    'شغل VS Code',
    'شغل calculator',
    'launch VS Code',
    'start notepad',
]

for text in test_cases:
    result = match(text)
    print(f'{text!r} -> match: {result}')