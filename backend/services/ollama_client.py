"""Ollama local inference client.

Provides chat completion (streaming + non-streaming), health checks,
model management, and capability probing for local Ollama models.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx

logger = logging.getLogger("Zaytoon.Ollama")


@dataclass
class ModelInfo:
    """Information about an Ollama model."""
    name: str
    size: int
    modified_at: str
    digest: str
    details: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def short_name(self) -> str:
        """Model name without tag, e.g. 'qwen3.5' from 'qwen3.5:9b'."""
        return self.name.split(":")[0]


@dataclass
class HealthStatus:
    """Ollama service health status."""
    connected: bool
    endpoint: str
    version: str = ""
    models: List[ModelInfo] = field(default_factory=list)
    error: str = ""
    latency_ms: int = 0


@dataclass
class PullProgress:
    """Progress update for model pull."""
    status: str
    digest: str = ""
    total: int = 0
    completed: int = 0


@dataclass
class ModelCapabilities:
    """Detected capabilities of a model."""
    model: str
    supports_tools: bool = False
    supports_streaming: bool = True
    supports_structured_output: bool = False
    context_window: int = 4096
    last_probed: float = 0.0


class OllamaClient:
    """Ollama HTTP client with streaming, health checks, and model management."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        timeout: float = 120.0,
        connect_timeout: float = 10.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self._client: Optional[httpx.AsyncClient] = None
        self._capabilities_cache: Dict[str, ModelCapabilities] = {}
        self._models_cache: List[ModelInfo] = []
        self._models_cache_time: float = 0
        self._models_cache_ttl: float = 30.0

    # ------------------------------------------------------------------
    # Client lifecycle
    # ------------------------------------------------------------------
    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=self.connect_timeout),
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # ------------------------------------------------------------------
    # Health & model listing
    # ------------------------------------------------------------------
    async def health_check(self) -> HealthStatus:
        """Check Ollam connectivity and return version + model list."""
        started = time.monotonic()
        try:
            client = await self._get_client()
            
            # Check /api/version
            version_resp = await client.get(f"{self.base_url}/api/version")
            version_resp.raise_for_status()
            version_data = version_resp.json()
            version = version_data.get("version", "unknown")
            
            # List models
            models = await self.list_models()
            
            return HealthStatus(
                connected=True,
                endpoint=self.base_url,
                version=version,
                models=models,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        except httpx.ConnectError as e:
            return HealthStatus(
                connected=False,
                endpoint=self.base_url,
                error=f"Connection failed: {e}",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        except httpx.TimeoutException as e:
            return HealthStatus(
                connected=False,
                endpoint=self.base_url,
                error=f"Timeout: {e}",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as e:
            return HealthStatus(
                connected=False,
                endpoint=self.base_url,
                error=f"Health check failed: {e}",
                latency_ms=int((time.monotonic() - started) * 1000),
            )

    async def list_models(self, force_refresh: bool = False) -> List[ModelInfo]:
        """List available models from Ollama."""
        now = time.monotonic()
        if not force_refresh and self._models_cache and (now - self._models_cache_time) < self._models_cache_ttl:
            return self._models_cache
        
        try:
            client = await self._get_client()
            resp = await client.get(f"{self.base_url}/api/tags")
            resp.raise_for_status()
            data = resp.json()
            
            models = []
            for m in data.get("models", []):
                models.append(ModelInfo(
                    name=m.get("name", ""),
                    size=m.get("size", 0),
                    modified_at=m.get("modified_at", ""),
                    digest=m.get("digest", ""),
                    details=m.get("details", {}),
                ))
            
            self._models_cache = models
            self._models_cache_time = now
            return models
        except Exception as e:
            logger.warning(f"Failed to list Ollama models: {e}")
            return self._models_cache

    async def model_exists(self, model: str) -> bool:
        """Check if a specific model is available locally."""
        models = await self.list_models()
        return any(m.name == model or m.name.startswith(model + ":") for m in models)

    # ------------------------------------------------------------------
    # Model pulling
    # ------------------------------------------------------------------
    async def pull_model(self, name: str) -> AsyncGenerator[PullProgress, None]:
        """Pull a model from Ollama registry. Yields progress updates."""
        client = await self._get_client()
        
        async with client.stream(
            "POST",
            f"{self.base_url}/api/pull",
            json={"name": name, "stream": True},
            timeout=self.timeout,
        ) as response:
            response.raise_for_status()
            
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                
                status = data.get("status", "")
                digest = data.get("digest", "")
                total = data.get("total", 0)
                completed = data.get("completed", 0)
                
                yield PullProgress(
                    status=status,
                    digest=digest,
                    total=total,
                    completed=completed,
                )
                
                if status == "success":
                    # Invalidate cache so next list_models picks it up
                    self._models_cache_time = 0
                    break

    # ------------------------------------------------------------------
    # Chat completion
    # ------------------------------------------------------------------
    async def chat(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        stream: bool = False,
        tools: Optional[List[Dict[str, Any]]] = None,
        options: Optional[Dict[str, Any]] = None,
        keep_alive: str = "5m",
    ) -> str | AsyncGenerator[str, None]:
        """Chat completion, streaming or non-streaming.
        
        Returns full text if stream=False, otherwise yields text deltas.
        """
        payload = {
            "model": model,
            "messages": messages,
            "stream": stream,
            "keep_alive": keep_alive,
        }
        
        if tools:
            payload["tools"] = tools
        if options:
            payload["options"] = options
        
        client = await self._get_client()
        
        if stream:
            return self._stream_chat(client, payload)
        else:
            return await self._blocking_chat(client, payload)

    async def _blocking_chat(
        self,
        client: httpx.AsyncClient,
        payload: Dict[str, Any],
    ) -> str:
        """Non-streaming chat completion."""
        resp = await client.post(f"{self.base_url}/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()
        
        message = data.get("message", {})
        content = message.get("content", "")
        
        # Handle tool calls in response: render them in the documented
        # JSON-in-text dialect ({"action": ..., "params": ...}) so the
        # agent engine's parsers see exactly what a cloud model would emit.
        tool_calls = message.get("tool_calls", [])
        if tool_calls:
            calls = [
                json.dumps({
                    "action": tc.get("function", {}).get("name", ""),
                    "params": tc.get("function", {}).get("arguments", {}),
                })
                for tc in tool_calls
            ]
            rendered = "\n".join(calls)
            if content:
                return content + "\n" + rendered
            return rendered
        
        return content

    async def _stream_chat(
        self,
        client: httpx.AsyncClient,
        payload: Dict[str, Any],
    ) -> AsyncGenerator[str, None]:
        """Streaming chat completion - yields content deltas."""
        async with client.stream("POST", f"{self.base_url}/api/chat", json=payload) as response:
            response.raise_for_status()
            
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                
                message = data.get("message", {})
                content = message.get("content", "")
                
                if content:
                    yield content
                
                # Tool calls arrive on the final chunk; render them in the
                # documented JSON-in-text dialect for the agent engine.
                tool_calls = message.get("tool_calls", [])
                if tool_calls and data.get("done", False):
                    for tc in tool_calls:
                        yield "\n" + json.dumps({
                            "action": tc.get("function", {}).get("name", ""),
                            "params": tc.get("function", {}).get("arguments", {}),
                        })
                
                if data.get("done", False):
                    break

    # ------------------------------------------------------------------
    # Capability probing
    # ------------------------------------------------------------------
    async def probe_capabilities(self, model: str) -> ModelCapabilities:
        """Probe a model's capabilities by sending test requests."""
        if model in self._capabilities_cache:
            cached = self._capabilities_cache[model]
            if time.monotonic() - cached.last_probed < 3600:  # 1 hour cache
                return cached
        
        caps = ModelCapabilities(model=model)
        
        # Test basic chat + streaming
        try:
            test_messages = [{"role": "user", "content": "Reply with just 'OK'"}]
            stream = await self.chat(
                test_messages, model, stream=True, options={"num_predict": 10}
            )
            async for _ in stream:
                caps.supports_streaming = True
                break
        except Exception as e:
            logger.debug(f"Streaming probe failed for {model}: {e}")
        
        # Test tool calling
        try:
            test_tools = [{
                "type": "function",
                "function": {
                    "name": "get_time",
                    "description": "Get current time",
                    "parameters": {"type": "object", "properties": {}},
                }
            }]
            test_messages = [
                {"role": "system", "content": "You have access to tools. Use get_time when asked for time."},
                {"role": "user", "content": "What time is it?"},
            ]
            result = await self.chat(test_messages, model, stream=False, tools=test_tools, options={"num_predict": 50})
            
            # Check if response contains a tool call
            if "get_time" in result or ("tool_calls" in result and "get_time" in str(result)):
                caps.supports_tools = True
                caps.supports_structured_output = True
        except Exception as e:
            logger.debug(f"Tool calling probe failed for {model}: {e}")
        
        # Context window, when the registry reports one.
        models = await self.list_models()
        for m in models:
            if m.name == model or m.name.startswith(model + ":"):
                ctx = m.details.get("context_length")
                if isinstance(ctx, int) and ctx > 0:
                    caps.context_window = ctx
                break
        
        caps.last_probed = time.monotonic()
        self._capabilities_cache[model] = caps
        return caps


# Module-level singleton
_default_client: Optional[OllamaClient] = None


def get_ollama_client(base_url: str = "http://localhost:11434", timeout: float = 120.0) -> OllamaClient:
    """Get or create the default Ollama client.

    Re-created when the configured endpoint or timeout changes, so a runtime
    settings update takes effect without a restart.
    """
    global _default_client
    wanted = base_url.rstrip("/")
    if _default_client is None or _default_client.base_url != wanted:
        _default_client = OllamaClient(base_url, timeout)
    return _default_client


async def close_ollama_client() -> None:
    """Close the default Ollama client."""
    global _default_client
    if _default_client:
        await _default_client.aclose()
        _default_client = None