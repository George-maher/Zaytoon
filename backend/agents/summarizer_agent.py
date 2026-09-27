"""Compress old conversation turns into a single rolling summary.

This agent exists because :class:`~agents.base_agent.BaseAgent` is an ABC with
an abstract ``process`` method. ``MemoryAgent.summarize_and_prune`` used to do
``BaseAgent("Summarizer", ...)``, which raised
``TypeError: Can't instantiate abstract class`` on every call — swallowed by a
bare ``except``, so every stored summary was the constant
``"Previous conversation"``.

Kept deliberately cheap: it runs on the fast tier, asks for a handful of
words, and the caller falls back to a mechanical extract if the provider is
unreachable.
"""

import logging
from typing import Any, Dict, List, Optional

from config import TIER_FAST
from .base_agent import BaseAgent

logger = logging.getLogger("Zaytoon.SummarizerAgent")

#: Used when even the extract fallback has nothing to work with.
FALLBACK_SUMMARY = "Previous conversation"

#: Substrings that mean the model returned a *failure report*, not a summary.
#: ``BaseAgent._format_error`` produces strings like
#: "Summarizer Agent: All model providers failed. Last error: ...".
_ERROR_MARKERS = (
    "all model providers failed",
    "provider",
    "api key",
    "unauthorized",
    "rate limited",
    "exhausted",
    "traceback",
)


class SummarizerAgent(BaseAgent):
    """One-line rolling summary of a conversation so far."""

    tier = TIER_FAST

    FALLBACK_SUMMARY = FALLBACK_SUMMARY

    def __init__(self):
        super().__init__("Summarizer Agent", max_tokens=128, temperature=0.1)

    def build_prompt(self, transcript: str) -> List[Dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "Summarise the conversation so far in at most 10 words.\n"
                    "Keep concrete facts: names, decisions, file paths, numbers, "
                    "and anything still pending.\n"
                    "No preamble, no quotation marks, no full sentences."
                ),
            },
            {"role": "user", "content": transcript},
        ]

    @staticmethod
    def looks_like_an_error(text: str) -> bool:
        """True when the model replied with a failure report, not a summary."""
        if not text:
            return True
        lowered = text.strip().lower()
        return any(marker in lowered for marker in _ERROR_MARKERS)

    async def process(
        self,
        user_message: str,
        context: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> str:
        summary = await self.get_ai_response(self.build_prompt(user_message))
        return (summary or "").strip().strip('"').strip()[:200]
