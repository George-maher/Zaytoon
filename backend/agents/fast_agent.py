import logging
from typing import Dict, Any, List, Optional, Callable

from .base_agent import BaseAgent

logger = logging.getLogger("Zaytoon.FastAgent")


class FastAgent(BaseAgent):
    def __init__(self):
        super().__init__("Fast Agent", max_tokens=256, temperature=0.5)

    async def process(
        self,
        user_message: str,
        context: List[Dict[str, Any]],
        stream_callback: Optional[Callable] = None,
        **kwargs,
    ) -> str:
        lang = kwargs.get("language", "en")

        if lang == "ar":
            sp = (
                "أنت زيتون، مساعد ذكي. رد بالعامية المصرية فقط. "
                "مختصر جداً. جملة أو جملتين كحد أقصى. "
                "لا تذكر برمجياتك الداخلية. جاوب مباشرة."
            )
        else:
            sp = (
                "You are Zaytoon, an AI assistant. "
                "Be VERY concise. Max 2 sentences. "
                "Answer directly. No pleasantries. No self-reference."
            )

        messages = [
            {"role": "system", "content": sp},
            *[{"role": ctx["role"], "content": ctx["content"]} for ctx in context[-3:]],
            {"role": "user", "content": user_message},
        ]

        return await self.get_ai_response(messages, stream_callback=stream_callback)
