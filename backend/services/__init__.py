"""Services package exports.

All imports are lazy to avoid circular dependencies:
- config -> services.model_registry -> services -> config
- config -> services.llm_client -> config
"""
from __future__ import annotations

import importlib
from typing import Any

# Public API names
__all__ = [
    # llm_client
    "LLMClient",
    "get_llm_client",
    "close_llm_client",
    # model_registry
    "MODEL_REGISTRY",
    "select_models",
    "is_allowed_model",
    "is_free_form",
    "is_free_router",
    "CAP_TOOLS",
    "CAP_STRUCTURED_OUTPUT",
    "CAP_STREAMING",
    "ALL_CAPABILITIES",
    # completion_validator
    "validate_completion",
    "CompletionContract",
    "CHAT_CONTRACT",
    "STREAM_CONTRACT",
    "Verdict",
    # auth
    "DeviceAuthService",
    "get_device_auth",
    "PermissionScope",
    "DevicePermission",
    "require_device_auth",
    # agent_service
    "AgentService",
    # wake_word_service
    "wake_word_service",
]

# Module name mapping for lazy loading
_MODULE_MAP = {
    "LLMClient": ("services.llm_client", "LLMClient"),
    "get_llm_client": ("services.llm_client", "get_llm_client"),
    "close_llm_client": ("services.llm_client", "close_llm_client"),
    "MODEL_REGISTRY": ("services.model_registry", "MODEL_REGISTRY"),
    "select_models": ("services.model_registry", "select_models"),
    "is_allowed_model": ("services.model_registry", "is_allowed_model"),
    "is_free_form": ("services.model_registry", "is_free_form"),
    "is_free_router": ("services.model_registry", "is_free_router"),
    "CAP_TOOLS": ("services.model_registry", "CAP_TOOLS"),
    "CAP_STRUCTURED_OUTPUT": ("services.model_registry", "CAP_STRUCTURED_OUTPUT"),
    "CAP_STREAMING": ("services.model_registry", "CAP_STREAMING"),
    "ALL_CAPABILITIES": ("services.model_registry", "ALL_CAPABILITIES"),
    "validate_completion": ("services.completion_validator", "validate_completion"),
    "CompletionContract": ("services.completion_validator", "CompletionContract"),
    "CHAT_CONTRACT": ("services.completion_validator", "CHAT_CONTRACT"),
    "STREAM_CONTRACT": ("services.completion_validator", "STREAM_CONTRACT"),
    "Verdict": ("services.completion_validator", "Verdict"),
    "DeviceAuthService": ("services.auth", "DeviceAuthService"),
    "get_device_auth": ("services.auth", "get_device_auth"),
    "PermissionScope": ("services.auth", "PermissionScope"),
    "DevicePermission": ("services.auth", "DevicePermission"),
    "require_device_auth": ("services.auth", "require_device_auth"),
    "AgentService": ("services.agent_service", "AgentService"),
    "wake_word_service": ("services.wake_word_service", "wake_word_service"),
}


def __getattr__(name: str) -> Any:
    """Lazy import for all services."""
    if name not in _MODULE_MAP:
        raise AttributeError(f"module 'services' has no attribute {name!r}")

    module_name, attr_name = _MODULE_MAP[name]
    module = importlib.import_module(module_name)
    return getattr(module, attr_name)


def __dir__() -> list[str]:
    return __all__