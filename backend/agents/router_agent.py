import json
import logging
import re
from typing import Dict, Any, List

from .base_agent import BaseAgent

logger = logging.getLogger("Zaytoon.RouterAgent")

#: Ordered **most specific first**. `route()` returns on the first match, so
#: order is the whole priority system, and a broad rule placed above a narrow
#: one makes the narrow one unreachable.
#:
#: This list used to put the catch-all `(what|how|why|…) -> chat` on line 19,
#: above the `what time` rule on line 24. Every "what …" request therefore
#: became `chat`, and the time rule was dead code — a live run of "what time is
#: it right now" went to FastAgent, which replied that it had no access to
#: real-time data, while a `get_time` tool sat in the registry unused.
#:
#: `tests/test_router_routing.py` asserts that each rule is reachable, so this
#: ordering cannot silently invert again.
KEYWORD_ROUTES: list[tuple[re.Pattern, str, float]] = [
    # -- narrow, high-confidence phrasings --------------------------------
    (re.compile(r"(what time|what date|current time|current date|what's the time|الوقت|الساعة|التاريخ|كام الساعة|ايه التاريخ|ange time|ange date)"), "tools", 0.95),
    (re.compile(r"(what did we|what was|what did i|remember|last time|قبل|امبارح|ince|آخر مرة|فاكر|تفتكر)"), "memory", 0.85),
    (re.compile(r"(delete|حذف|remove|مسح|شيل)\s+(reminder|task|المهمة|التذكير)"), "scheduling", 0.85),
    (re.compile(r"(list|show|display|عرض)\s*(my\s+)?\s*(reminders?|tasks?|المواعيد|المهام)"), "scheduling", 0.85),
    (re.compile(r"(git|commit|push|pull|branch|merge|rebase|stash)"), "tools", 0.9),
    (re.compile(r"(create|new|generate|build)\s+(a\s+)?(react|node|python|flask|django|html|project|app|application|تطبيق|مشروع)"), "code", 0.85),
    (re.compile(r"(run|تشغيل|execute|نفذ)\s+(the\s+)?(tests?|test suite|اختبارات)"), "code", 0.85),
    (re.compile(r"(npm|yarn|pnpm|pip|install)"), "tools", 0.85),
    (re.compile(r"(read|open|cat)\s+(the\s+)?file|(file)\s+\w+\.(py|js|jsx|txt|md|json|toml)"), "tools", 0.85),
    (re.compile(r"(write|save|create)\s+(a\s+)?(file|script)"), "tools", 0.9),
    (re.compile(r"(rename|move|copy)\s+(the\s+)?file"), "tools", 0.9),
    (re.compile(r"(weather|temperature|الطقس|درجة الحرارة|news|أخبار)"), "tools", 0.8),
    (re.compile(r"(system|حالة|systeminfo|mعلومات|إحصائيات|stats)"), "tools", 0.8),
    (re.compile(r"(calculate|compute|calculator|احسب|حسب)"), "tools", 0.85),
    (re.compile(r"(screenshot|screen shot|صورة الشاشة|سكرين شوت)"), "tools", 0.85),
    (re.compile(r"(volume|صوت|كتم|c mute)"), "tools", 0.85),

    # -- verbs that imply acting on the machine ----------------------------
    (re.compile(r"(remind|reminder|فكرني|ذكرني|تذكير|نبهني)"), "scheduling", 0.9),
    (re.compile(r"\b(schedule)\b"), "scheduling", 0.9),
    # -- virtual machines ---------------------------------------------------
    # Placed *after* the memory/scheduling rules so "remember my vm name"
    # stays a memory request and "remind me to suspend the vm" stays a
    # reminder, and *before* the catch-all so "show me the available virtual
    # machines" cannot become a chat turn. Without this rule those phrasings
    # fell through to the LLM fallback — or matched the catch-all outright —
    # so listing VMs depended on a rate-limited model, and on exhaustion the
    # request became `chat` while `vm_list` sat unused in the registry (the
    # same failure the `what time` rule was written to prevent). `ubuntu` and
    # `parrot` are here because in this product they name the lab guests, not
    # a generic OS discussion: "wait until ubuntu is ready" is
    # `vm_guest_check`, and routing it to `chat` makes the VM workflow
    # unreachable without an LLM.
    (re.compile(r"(vmware|vmrun|virtual machines?|\bvms?\b|\bguest\b|hypervisor|ubuntu|parrot|افتراضي)"), "tools", 0.92),
    # File/folder search by name. These sit above the generic "open" rule
    # because "دورلي على ملف" and "فين ملف report" contain verbs that the open
    # rule would otherwise claim, and a search answered by opening the wrong
    # file is worse than no answer. `test_router_routing.py` pins the ordering.
    (re.compile(r"(فين|لاقي|هاتلي|where is|where's|find|locate)\s+.*(ملف|فايل|فولدر|مجلد|file|folder)"), "tools", 0.92),
    (re.compile(r"(دورلي|دور لي|دوّرلي|دوّر لي|ابحث عن|ابحثلي|ابحث لي|search for|look for)\s+\S+"), "tools", 0.9),
    # `شغّل` (with shadda) is the everyday imperative "start it" — the plain
    # `شغل` alternative never matches it, so every Egyptian "شغّل X" phrasing
    # fell through to the LLM.
    (re.compile(r"(open|launch|start|close|stop|شغل|شغّل|شغال|فتح|افتح|إغلاق|أغلق|اغلق|اقفل|قفل)\s*\w+"), "tools", 0.85),
    (re.compile(r"(run|تشغيل|execute|نفذ)\s+(?:\w+\s+){0,2}(python|code|script|command|أمر|كود)"), "action", 0.9),
    (re.compile(r"(search|بحث|ابحث|google|دور)"), "tools", 0.85),
    (re.compile(r"(write|اكتب|build|إنشاء)\s+(code|app|application|function|component|تطبيق|برنامج|صفحة)"), "code", 0.85),
    (re.compile(r"(type|اكتب|paste|الصق)\s+.+"), "tools", 0.9),
    (re.compile(r"(analyze|حلل|plan|خطط|compare|قارن)"), "complex", 0.8),
    (re.compile(r"(date|time|الوقت|الساعة|التاريخ|اليوم)"), "tools", 0.85),

    # -- greetings ---------------------------------------------------------
    (re.compile(r"^\s*(hello|hi|hey|مرحبا|السلام|أهلا|ازيك)[\s!.,?]*$"), "chat", 0.95),

    # -- catch-all. MUST stay last: it matches the opening word of almost any
    # English question, so anything above it that starts with "what", "how" or
    # "when" is unreachable.
    (re.compile(r"(what|how|why|when|tell|explain|إيه|ازاي|ليه|امتى|قول|اشرح|عرفني)"), "chat", 0.7),
]


class RouterAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            "Router Agent",
            max_tokens=128,
            temperature=0.1,
        )

    async def process(
        self, user_message: str, context: List[Dict[str, Any]], **kwargs
    ) -> str:
        system_prompt = (
            "Classify user request into ONE intent. Return ONLY JSON:\n"
            '{"intent":"chat|code|tools|scheduling|memory|complex","confidence":0.0-1.0}\n\n'
            "Examples: \"open spotify\"->tools, \"write python\"->code, "
            "\"remind me\"->scheduling, \"what was\"->memory, "
            "\"plan a project\"->complex, \"hello\"->chat"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        response = await self.get_ai_response(messages)

        try:
            cleaned = response.strip().removeprefix("```json").removesuffix("```").strip()
            result = json.loads(cleaned)
            intent = result.get("intent", "chat")
            if intent not in ("chat", "code", "tools", "scheduling", "memory", "complex"):
                intent = "chat"
            result["intent"] = intent
            return json.dumps(result)
        except (json.JSONDecodeError, AttributeError):
            return json.dumps({"intent": "chat", "confidence": 0.5})

    async def route(
        self, user_message: str, context: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        msg_lower = user_message.lower().strip()
        # The orchestrator may prepend a project/skill preamble, separated by a
        # blank line, so that "run the tests" is judged with the project in
        # view. The preamble is *context*, not user speech — and a keyword rule
        # reading its facts as a request is a real misroute: "git repo on main"
        # matches the git rule, so **every** message in a detected git project
        # would become a `tools` turn, and "run: npm start" matches the npm
        # rule in a node project. Keyword rules therefore see only what comes
        # after the last blank line — the user's own message. The LLM fallback
        # still sees the full text, where context helps classification instead
        # of misfiring a regex.
        user_part = msg_lower.rsplit("\n\n", 1)[-1]

        for pattern, intent, confidence in KEYWORD_ROUTES:
            if pattern.search(user_part):
                return {"intent": intent, "confidence": confidence, "reasoning": "keyword_match"}

        result = await self.process(user_message, context)
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return {"intent": "chat", "confidence": 0.5}
