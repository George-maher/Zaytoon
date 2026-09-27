import logging
from typing import Dict, Any, List, Optional, Callable

from config import TIER_REASONING
from .base_agent import BaseAgent
from .prompts import ANTI_HALLUCINATION_EN, ANTI_HALLUCINATION_AR

logger = logging.getLogger("Zaytoon.PlannerAgent")


class PlannerAgent(BaseAgent):
    """Decomposes a request into verifiable steps.

    The prompt forces explicit deliberation and a stated-uncertainty rule so
    the model flags what it cannot verify instead of inventing detail. The plan
    is advisory: nothing here executes a system action.
    """

    tier = TIER_REASONING

    def __init__(self):
        super().__init__("Planner Agent", max_tokens=2048, temperature=0.3)

    def build_system_prompt(self, lang: str) -> str:
        if lang == "ar":
            return (
                "انت زيتون المخطط. خطّط للمشروع المطلوب خطوة بخطوة قبل ما تجاوب.\n"
                "المنهج الإجباري:\n"
                "1) افهم: اكتب في سطر واحد المطلوب والقيود.\n"
                "2) افحص: حدد ما هو معروف وما هو غير مؤكد بوضوح.\n"
                "3) رتّب الخطوات: 3-6 خطوات مرتبة، كل خطوة تبدأ بفعل.\n"
                "4) تحقق: لكل خطوة اذكر كيف تتأكد أنها نجحت.\n"
                "5) مخاطر: اذكر أي خطوة تتلف بيانات أو تحتاج إذن المستخدم.\n"
                "ممنوع تخمين أي اسم ملف أو أمر أو قيمة. لو مش متأكد اكتب: "
                "«محتاج أتحقق». ممنوع الإنجليزية. رد بالعامية المصرية."
            )
        return (
            "You are Zaytoon the planner. Decompose the request BEFORE answering.\n"
            "Required method:\n"
            "1) Understand: state the goal and constraints in one line.\n"
            "2) Audit: separate what is known from what is uncertain.\n"
            "3) Steps: 3-6 ordered steps, each starting with an action verb.\n"
            "4) Verify: for each step, state how to confirm it actually worked.\n"
            "5) Risks: flag any destructive step or one needing user approval.\n"
            "NEVER invent a file name, command, API, or value. If you are not "
            "certain, write 'NEEDS VERIFICATION' rather than guessing. "
            "You are planning only; you do not perform actions."
        )

    async def process(
        self,
        user_message: str,
        context: List[Dict[str, Any]],
        stream_callback: Optional[Callable] = None,
        **kwargs,
    ) -> str:
        lang = kwargs.get("language", "en")
        guard = ANTI_HALLUCINATION_AR if lang == "ar" else ANTI_HALLUCINATION_EN
        sp = f"{self.build_system_prompt(lang)}\n\n{guard}"

        messages = [
            {"role": "system", "content": sp},
            *[{"role": ctx["role"], "content": ctx["content"]} for ctx in context[-3:]],
            {"role": "user", "content": user_message},
        ]

        return await self.get_ai_response(messages, stream_callback=stream_callback)
