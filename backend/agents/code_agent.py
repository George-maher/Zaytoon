import logging
from typing import Dict, Any, List, Optional, Callable

from config import TIER_REASONING
from .base_agent import BaseAgent
from .prompts import ANTI_HALLUCINATION_AR, ANTI_HALLUCINATION_EN

logger = logging.getLogger("Zaytoon.CodeAgent")


class CodeAgent(BaseAgent):
    """Writes code, but only after an explicit reasoning pass.

    The prompt forces the model to state its understanding and self-check the
    code before emitting it, and forbids inventing APIs. Nothing this agent
    returns is executed directly: `run_code` still goes through the tool safety
    gate, and shell interpreters are refused outright.
    """

    tier = TIER_REASONING

    def __init__(self):
        super().__init__("Code Agent", max_tokens=4096, temperature=0.2)

    def build_system_prompt(self, lang: str) -> str:
        if lang == "ar":
            return (
                "أنت زيتون المبرمج. اكتب كود جاهز للإنتاج.\n"
                "المنهج الإجباري قبل ما تكتب:\n"
                "1) افهم المطلوب والقيود في جملة.\n"
                "2) حدد أي API أو مكتبة هتستخدمها بالاسم الكامل. لو مش متأكد "
                "من وجودها اكتب: «محتاج أتحقق» وممتوع تخمنها.\n"
                "3) اكتب الكود.\n"
                "4) تحقق من الكود سطر سطر: هل فيه خطأ منطقي أو حالة حدّ ناقصة؟ "
                "لو أي حاجة مش مضمونة قولها بصراحة.\n"
                "الشرح بالعامية المصرية باختصار، والأسماء داخل الكود بالإنجليزية.\n"
                "إنت مش بتنفّذ حاجة؛ التنفيذ في طبقة الأدوات، وأي عملية تدميرية "
                "أو شِل لازم تأخذ موافقة المستخدم."
            )
        return (
            "You are Zaytoon the programmer. Write production-ready code.\n"
            "Required method before you write anything:\n"
            "1) Restate the requirement and its constraints in one line.\n"
            "2) Name every API/library/function you will use. If you are not "
            "certain it exists, write 'NEEDS VERIFICATION' - never invent an "
            "API signature or import.\n"
            "3) Write the code.\n"
            "4) Self-review the code line by line: logic errors, off-by-one, "
            "missing edge cases, unhandled errors. State anything you are not "
            "certain about instead of hiding it.\n"
            "Prose in English, brief. Identifiers stay in English.\n"
            "You do not execute anything; execution goes through the tool "
            "layer, and destructive or shell actions always require the user's "
            "explicit approval."
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
