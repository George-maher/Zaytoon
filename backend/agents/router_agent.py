import json
import logging
import re
from typing import Dict, Any, List

from .base_agent import BaseAgent

logger = logging.getLogger("Zaytoon.RouterAgent")

KEYWORD_ROUTES: list[tuple[re.Pattern, str, float]] = [
    (re.compile(r"(remind|reminder|remind me|فكرني|ذكرني|تذكير|نبهني|save|schedule)"), "scheduling", 0.9),
    (re.compile(r"(delete|حذف|remove|مسح|شيل)\s+(reminder|task|المهمة|التذكير)"), "scheduling", 0.85),
    (re.compile(r"(list|show|display|all|عرض|المواعيد|المهام)\s*(reminders?|tasks?|المواعيد|المهام)"), "scheduling", 0.85),
    (re.compile(r"(open|launch|start|close|stop|شغل|شغال|فتح|افتح|إغلاق|أغلق|اغلق|اقفل|قفل)\s*\w+"), "tools", 0.85),
    (re.compile(r"(run|تشغيل|execute|نفذ)\s+(python|code|script|command|أمر|كود)"), "action", 0.9),
    (re.compile(r"(search|بحث|ابحث|google|find|دور)"), "tools", 0.85),
    (re.compile(r"(write|اكتب|type|اكتب|build|إنشاء|create|create)\s+(code|app|application|function|component|تطبيق|program|programme|صفحة)"), "code", 0.85),
    (re.compile(r"(type|اكتب|paste|الصق)\s+.+"), "tools", 0.9),
    (re.compile(r"(what|how|why|when|tell|explain|إيه|ازاي|ليه|امتى|قول|اشرح|عرفني)"), "chat", 0.7),
    (re.compile(r"(hello|hi|hey|مرحبا|السلام|أهو|hi|hello)"), "chat", 0.95),
    (re.compile(r"(analyze|حلل|plan|خطط|compare|قارن|compare|complex|complex)"), "complex", 0.8),
    (re.compile(r"(remember|what did we|what was|what did i|last time|before|قبل|امبارح|ince|آخر مرة|فاكر|تفتكر)"), "memory", 0.85),
    (re.compile(r"(weather|temperature|الطقس|درجة الحرارة|news|أخبار)"), "tools", 0.8),
    (re.compile(r"(what time|what date|current time|current date|الوقت|الساعة|التاريخ|كام الساعة|ايه التاريخ|ange time|ange date)"), "tools", 0.95),
    (re.compile(r"(date|time|الوقت|الساعة|التاريخ|اليوم)"), "tools", 0.85),
    (re.compile(r"(system|حالة|systeminfo|info|معلومات|إحصائيات|stats)"), "tools", 0.8),
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

        for pattern, intent, confidence in KEYWORD_ROUTES:
            if pattern.search(msg_lower):
                return {"intent": intent, "confidence": confidence, "reasoning": "keyword_match"}

        result = await self.process(user_message, context)
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return {"intent": "chat", "confidence": 0.5}
