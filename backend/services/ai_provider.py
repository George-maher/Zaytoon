"""Provider abstraction for AI inference backends.

Supports Ollama (local) and OpenRouter (cloud) behind a unified interface.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Dict, List, Optional

from config import config, TIER_FAST

logger = logging.getLogger("Zaytoon.AIProvider")


@dataclass
class ModelInfo:
    """Unified model information across providers."""
    id: str
    name: str
    provider: str
    available: bool = True
    capabilities: List[str] = field(default_factory=list)
    size_bytes: int = 0
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HealthStatus:
    """Provider health status."""
    provider: str
    connected: bool
    endpoint: str
    version: str = ""
    models: List[ModelInfo] = field(default_factory=list)
    error: str = ""
    latency_ms: int = 0


@dataclass
class CompletionOptions:
    """Options for chat completion."""
    max_tokens: int = 1024
    temperature: float = 0.7
    stream: bool = False
    tools: Optional[List[Dict[str, Any]]] = None
    stop: Optional[List[str]] = None


class AIProvider(ABC):
    """Abstract base class for AI providers."""
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier (e.g., 'ollama', 'openrouter')."""
        pass
    
    @property
    @abstractmethod
    def base_url(self) -> str:
        """Provider endpoint URL."""
        pass
    
    @abstractmethod
    async def health_check(self) -> HealthStatus:
        """Check provider connectivity and return status."""
        pass
    
    @abstractmethod
    async def list_models(self) -> List[ModelInfo]:
        """List available models."""
        pass
    
    @abstractmethod
    async def chat(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        options: CompletionOptions,
    ) -> str | AsyncGenerator[str, None]:
        """Chat completion (streaming or non-streaming)."""
        pass
    
    @abstractmethod
    async def close(self) -> None:
        """Close provider connections."""
        pass
    
    def model_for_tier(self, tier: str) -> str:
        """Get the model to use for a capability tier. Override in subclasses."""
        return ""


class OllamaProvider(AIProvider):
    """Ollama local inference provider."""
    
    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        timeout: float = 120.0,
        general_model: str = "qwen3.5:9b",
        coding_model: str = "qwen2.5-coder:7b",
        fast_model: str = "qwen2.5-coder:3b",
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._general_model = general_model
        self._coding_model = coding_model
        self._fast_model = fast_model
        
        # Import here to avoid circular dependency
        from services.ollama_client import OllamaClient
        self._client = OllamaClient(base_url, timeout)
        self._capabilities_cache: Dict[str, List[str]] = {}
    
    @property
    def name(self) -> str:
        return "ollama"
    
    @property
    def base_url(self) -> str:
        return self._base_url
    
    def model_for_tier(self, tier: str) -> str:
        return config.ollama_model_for_tier(tier)
    
    async def health_check(self) -> HealthStatus:
        health = await self._client.health_check()
        
        # Capabilities are *not* probed here: a tool-calling probe spins up a
        # real model for every installed tag, which would turn a health check
        # into a multi-second stall. ``probe_capabilities`` is available for
        # an explicit, user-triggered check instead.
        models = [
            ModelInfo(
                id=m.name,
                name=m.short_name,
                provider="ollama",
                available=True,
                capabilities=["streaming"],
                size_bytes=m.size,
                details=m.details,
            )
            for m in health.models
        ]
        
        return HealthStatus(
            provider="ollama",
            connected=health.connected,
            endpoint=health.endpoint,
            version=health.version,
            models=models,
            error=health.error,
            latency_ms=health.latency_ms,
        )
    
    async def _get_model_capabilities(self, model: str) -> List[str]:
        if model in self._capabilities_cache:
            return self._capabilities_cache[model]
        
        caps = await self._client.probe_capabilities(model)
        result = []
        if caps.supports_streaming:
            result.append("streaming")
        if caps.supports_tools:
            result.append("tools")
        if caps.supports_structured_output:
            result.append("structured_output")
        
        self._capabilities_cache[model] = result
        return result
    
    async def list_models(self) -> List[ModelInfo]:
        health = await self.health_check()
        return health.models
    
    async def chat(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        options: CompletionOptions,
    ) -> str | AsyncGenerator[str, None]:
        return await self._client.chat(
            messages=messages,
            model=model,
            stream=options.stream,
            tools=options.tools,
            options={
                "num_predict": options.max_tokens,
                "temperature": options.temperature,
                "stop": options.stop,
            },
        )
    
    async def close(self) -> None:
        await self._client.aclose()
    
    async def pull_model(self, model: str):
        """Pull a model from Ollama registry."""
        async for progress in self._client.pull_model(model):
            yield progress
    
    def invalidate_cache(self) -> None:
        """Invalidate model and capability caches."""
        self._capabilities_cache.clear()
        self._client._models_cache_time = 0


class OpenRouterProvider(AIProvider):
    """OpenRouter cloud inference provider (preserved for optional use)."""
    
    def __init__(self):
        from services.llm_client import get_llm_client
        self._llm_client = get_llm_client()
        self._configured = config.llm_configured
    
    @property
    def name(self) -> str:
        return "openrouter"
    
    @property
    def base_url(self) -> str:
        return config.LLM_API_URL
    
    def model_for_tier(self, tier: str) -> str:
        return config.model_for_tier(tier)
    
    async def health_check(self) -> HealthStatus:
        if not self._configured:
            return HealthStatus(
                provider="openrouter",
                connected=False,
                endpoint=self.base_url,
                error="OPENROUTER_API_KEY not configured",
            )
        
        try:
            # Try a minimal completion to test connectivity
            result = await self._llm_client.complete(
                messages=[{"role": "user", "content": "OK"}],
                tier=TIER_FAST,
                max_tokens=5,
                temperature=0.0,
            )
            return HealthStatus(
                provider="openrouter",
                connected=True,
                endpoint=self.base_url,
                models=[],  # Would need to fetch from OpenRouter API
                latency_ms=0,  # Not easily available from current client
            )
        except Exception as e:
            return HealthStatus(
                provider="openrouter",
                connected=False,
                endpoint=self.base_url,
                error=str(e),
            )
    
    async def list_models(self) -> List[ModelInfo]:
        # Return configured models from registry
        from services.model_registry import MODEL_REGISTRY, ALLOWED_FREE_MODELS
        models = []
        for model_id in ALLOWED_FREE_MODELS:
            spec = MODEL_REGISTRY.get(model_id)
            if spec:
                models.append(ModelInfo(
                    id=model_id,
                    name=model_id,
                    provider="openrouter",
                    available=True,
                    capabilities=sorted(spec.capabilities),
                ))
        return models
    
    async def chat(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        options: CompletionOptions,
    ) -> str | AsyncGenerator[str, None]:
        from services.completion_validator import CHAT_CONTRACT, STREAM_CONTRACT
        
        contract = STREAM_CONTRACT if options.stream else CHAT_CONTRACT
        
        if options.stream:
            async def stream_gen():
                async for delta in self._llm_client.stream(
                    messages=messages,
                    tier=TIER_FAST,
                    max_tokens=options.max_tokens,
                    temperature=options.temperature,
                    tools=options.tools,
                    contract=contract,
                ):
                    yield delta
            return stream_gen()
        else:
            return await self._llm_client.complete(
                messages=messages,
                tier=TIER_FAST,
                max_tokens=options.max_tokens,
                temperature=options.temperature,
                tools=options.tools,
                contract=contract,
            )
    
    async def close(self) -> None:
        await self._llm_client.aclose()


# ----------------------------------------------------------------------
# Provider factory
# ----------------------------------------------------------------------

_provider_instance: Optional[AIProvider] = None


def get_provider() -> AIProvider:
    """Get the configured AI provider (singleton)."""
    global _provider_instance
    if _provider_instance is None:
        _provider_instance = create_provider()
    return _provider_instance


def create_provider() -> AIProvider:
    """Create a new provider instance based on configuration."""
    provider_name = getattr(config, "AI_PROVIDER", "ollama").lower()
    
    if provider_name == "ollama":
        return OllamaProvider(
            base_url=getattr(config, "OLLAMA_BASE_URL", "http://localhost:11434"),
            timeout=getattr(config, "OLLAMA_TIMEOUT", 120.0),
            general_model=getattr(config, "OLLAMA_GENERAL_MODEL", "qwen3.5:9b"),
            coding_model=getattr(config, "OLLAMA_CODING_MODEL", "qwen2.5-coder:7b"),
            fast_model=getattr(config, "OLLAMA_FAST_MODEL", "qwen2.5-coder:3b"),
        )
    elif provider_name == "openrouter":
        return OpenRouterProvider()
    else:
        logger.warning(f"Unknown provider '{provider_name}', defaulting to Ollama")
        return OllamaProvider()


async def close_provider() -> None:
    """Close the current provider."""
    global _provider_instance
    if _provider_instance:
        await _provider_instance.close()
        _provider_instance = None