import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional, Callable

import httpx

from config import TIER_FAST, config
from common.reporting import client_error, redact
from services.completion_validator import CHAT_CONTRACT, CompletionContract
from services.llm_client import LLMClient, LLMError, get_llm_client

logger = logging.getLogger("Zaytoon")

_SHARED_CLIENT: httpx.AsyncClient | None = None


async def _get_shared_client() -> httpx.AsyncClient:
    global _SHARED_CLIENT
    if _SHARED_CLIENT is None or _SHARED_CLIENT.is_closed:
        _SHARED_CLIENT = httpx.AsyncClient(timeout=config.LLM_TIMEOUT)
    return _SHARED_CLIENT


async def close_shared_client():
    global _SHARED_CLIENT
    if _SHARED_CLIENT and not _SHARED_CLIENT.is_closed:
        await _SHARED_CLIENT.aclose()
        _SHARED_CLIENT = None


class BaseAgent(ABC):
    """Common LLM plumbing for every agent.

    Subclasses declare a ``tier`` which selects the head of the model fallback
    chain (see :class:`services.llm_client.LLMClient`); the actual provider,
    model and failover are resolved at call time by ``LLMClient``.
    """

    #: capability tier used to pick the first model in the fallback chain
    tier: str = TIER_FAST

    def __init__(
        self,
        agent_name: str,
        model_name: str = "",
        max_tokens: int = 1024,
        temperature: float = 0.7,
        tier: Optional[str] = None,
        llm_client: Optional[LLMClient] = None,
    ):
        self.agent_name = agent_name
        if tier:
            self.tier = tier
        # Nominal model kept for backwards compatibility / introspection.
        self.model_name = model_name or config.OPENROUTER_MODEL
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.api_url = config.LLM_API_URL
        self.headers = config.LLM_HEADERS
        self.llm = llm_client or get_llm_client()

    async def _get_client(self) -> httpx.AsyncClient:
        return await _get_shared_client()

    @abstractmethod
    async def process(
        self, user_message: str, context: List[Dict[str, Any]], **kwargs
    ) -> str:
        pass

    def _build_payload(
        self, messages: List[Dict[str, str]], stream: bool = False
    ) -> Dict[str, Any]:
        payload = {
            "model": self.model_name,
            "messages": messages,
            "max_tokens": min(self.max_tokens, config.LLM_MAX_TOKENS_CAP),
            "temperature": self.temperature,
        }
        if stream:
            payload["stream"] = True
        return payload

    # ------------------------------------------------------------------
    # Error formatting (kept so user-facing strings stay consistent)
    # ------------------------------------------------------------------
    def _format_error(self, exc: Exception) -> str:
        """What the user sees when the model layer gave up.

        Two shapes on purpose: an ``LLMError`` is an *expected*, well-understood
        outcome (every free model was rate-limited or refused) and the operator
        detail in ``attempts`` is useful, so it is kept — after
        :func:`common.reporting.redact` has removed anything credential-shaped.
        Anything else is an unanticipated exception, and the user gets a fixed
        bilingual sentence rather than ``str(exc)``.
        """
        if isinstance(exc, LLMError):
            detail = ""
            if exc.attempts:
                detail = redact(str(exc.attempts[-1].detail))
            text = f"{self.agent_name}: All model providers failed."
            if detail:
                text += f" Last error: {detail}"
            return text
        return client_error(
            exc, operation="complete", component=self.agent_name,
            language=getattr(self, "language", "en"),
        )

    async def call_api(
        self,
        messages: List[Dict[str, str]],
        retries: int = 2,
        contract: Optional[CompletionContract] = None,
    ) -> str:
        try:
            return await self.llm.complete(
                messages,
                tier=self.tier,
                max_tokens=min(self.max_tokens, config.LLM_MAX_TOKENS_CAP),
                temperature=self.temperature,
                retries=retries,
                contract=contract or CHAT_CONTRACT,
            )
        except LLMError as e:
            logger.error("%s exhausted all providers: %s", self.agent_name, e)
            return self._format_error(e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("%s error: %s", self.agent_name, e)
            return self._format_error(e)

    async def _stream_response(
        self,
        payload: Dict[str, Any],
        callback: Callable[[str], Any],
    ) -> str:
        """Legacy entry point kept for callers that pre-built a payload."""
        messages = payload.get("messages", [])
        try:
            return await self.llm.stream(
                messages,
                on_delta=callback,
                tier=self.tier,
                max_tokens=min(self.max_tokens, config.LLM_MAX_TOKENS_CAP),
                temperature=payload.get("temperature", self.temperature),
            )
        except LLMError:
            return ""

    async def get_ai_response(
        self,
        messages: List[Dict[str, str]],
        retries: int = 2,
        stream_callback: Optional[Callable[[str], Any]] = None,
    ) -> str:
        max_tokens = min(self.max_tokens, config.LLM_MAX_TOKENS_CAP)
        if stream_callback:
            result = ""
            try:
                result = await self.llm.stream(
                    messages,
                    on_delta=stream_callback,
                    tier=self.tier,
                    max_tokens=max_tokens,
                    temperature=self.temperature,
                    retries=retries,
                )
            except LLMError:
                logger.info(
                    "%s streaming failed across all providers, "
                    "falling back to non-streaming", self.agent_name,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("%s stream error, falling back: %s", self.agent_name, e)
            if result:
                return result
            logger.info("%s produced no stream content, retrying non-streaming",
                        self.agent_name)
        return await self.call_api(messages, retries)
