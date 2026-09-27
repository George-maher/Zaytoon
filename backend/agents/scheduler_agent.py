import json
import logging
import re
from datetime import datetime
from typing import Dict, Any, List

from config import TIER_REASONING
from .base_agent import BaseAgent
from scheduler.parser import TimeParser

logger = logging.getLogger("Zaytoon.SchedulerAgent")


class SchedulerAgent(BaseAgent):
    # Date/time extraction benefits from the reasoning model.
    tier = TIER_REASONING

    def __init__(self):
        super().__init__("Scheduler Agent", max_tokens=256, temperature=0.1)

    async def process(
        self, user_message: str, context: List[Dict[str, Any]], **kwargs
    ) -> str:
        lang = kwargs.get("language", "en")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if lang == "ar":
            sp = (
                f"الوقت الحالي: {now}\n"
                + "استخرج معلومات التذكير. ارجع JSON فقط:\n"
                + '{"action":"create|list|delete|done","title":"...",'
                + '"datetime":"ISO8601|null","recurring":"none|daily|weekly|monthly",'
                + '"id":null}\nمختصر. بالعامية المصرية.'
            )
        else:
            sp = (
                f"Current time: {now}\n"
                + "Extract scheduling info. Return ONLY JSON:\n"
                + '{"action":"create|list|delete|done","title":"...",'
                + '"datetime":"ISO8601|null","recurring":"none|daily|weekly|monthly",'
                + '"id":null}'
            )

        messages = [
            {"role": "system", "content": sp},
            {"role": "user", "content": user_message},
        ]

        response = await self.get_ai_response(messages)

        try:
            cleaned = response.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned.rsplit("```", 1)[0]
            cleaned = cleaned.strip()
            result = json.loads(cleaned)
            return json.dumps(result)
        except (json.JSONDecodeError, AttributeError) as e:
            logger.warning(f"SchedulerAgent parse failed: {response[:100]} - {e}")
            fallback = self._fallback_parse(user_message, lang)
            return json.dumps(fallback)

    def _fallback_parse(self, text: str, lang: str) -> dict:
        parsed_time = TimeParser.parse_time(text)
        dt_str = parsed_time.isoformat() if parsed_time else None

        if parsed_time:
            from reminder.models import detect_recurrence
            recurring = detect_recurrence(text).value
            return {
                "action": "create", "title": text, "datetime": dt_str,
                "recurring": recurring, "id": None, "language": lang,
                "confirmation": "Done." if lang == "en" else "تمام.",
            }

        text_lower = text.lower()
        if any(w in text_lower for w in ["list", "show", "all", "عرض", "المواعيد", "المهام"]):
            return {"action": "list", "title": "", "datetime": None, "recurring": "none", "id": None, "language": lang}
        if any(w in text for w in ["delete", "حذف", "remove", "مسح"]):
            m = re.search(r"(?:delete|حذف|remove|مسح)\s+(?:reminder|task|المهمة|التذكير)?\s*[#]?(\d+)", text, re.IGNORECASE)
            rid = m.group(1) if m else None
            return {"action": "delete", "title": "", "datetime": None, "recurring": "none", "id": rid, "language": lang}
        if any(w in text for w in ["done", "complete", "finished", "خلص", "تم"]):
            m = re.search(r"(?:task|reminder|المهمة|التذكير)\s*[#]?(\d+)", text, re.IGNORECASE)
            rid = m.group(1) if m else None
            return {"action": "done", "title": "", "datetime": None, "recurring": "none", "id": rid, "language": lang}

        return {"action": "create", "title": text, "datetime": None, "recurring": "none", "id": None, "language": lang}

    async def parse_schedule(
        self, user_message: str, context: List[Dict[str, Any]], language: str = "en"
    ) -> Dict[str, Any]:
        result = await self.process(user_message, context, language=language)
        try:
            parsed = json.loads(result)
            if not isinstance(parsed, dict):
                raise ValueError("Not a dict")
            return parsed
        except (json.JSONDecodeError, ValueError):
            return {
                "action": "create", "title": user_message, "datetime": None,
                "recurring": "none", "id": None, "language": language,
            }
