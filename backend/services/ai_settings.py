"""Persisted AI settings: a choice made in the settings screen survives a restart.

Two stores, deliberately split:

* non-secret values live in the ``kv`` table of ``core.db``;
* the API key lives in ``core.secrets`` (DPAPI / 0600 credential storage) and
  is **never** written to kv, echoed in a response, or logged.

Precedence: a value saved through the UI is applied at startup *over* the
environment default. The environment gives the first value; a later,
deliberate save is newer information, and a save that silently reverted on
restart would be the bug users describe as "the setting did not stick".
``GET /settings/ai`` reports ``persisted`` so the screen can say it is
showing a saved choice rather than the environment's.

Every read here is best-effort: a corrupt row logs and applies nothing, so a
damaged preference degrades to the configured defaults instead of refusing
to boot.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from config import config
from core import db
from core.secrets import get_secret, has_secret, set_secret

logger = logging.getLogger("Zaytoon.Services.AISettings")

#: kv row holding the non-secret settings written by ``POST /settings/ai``.
SETTINGS_KEY = "settings.ai"
#: Credential-storage key for the legacy OpenRouter key. Prefixed and dotted
#: so it can never collide with a registry provider id (those are slugs).
API_KEY_SECRET = "settings.openrouter"

_VALID_PROVIDERS = ("ollama", "openrouter")

#: kv section → config attribute, for the string fields of each provider.
_SECTIONS: Dict[str, Dict[str, str]] = {
    "ollama": {
        "base_url": "OLLAMA_BASE_URL",
        "general_model": "OLLAMA_GENERAL_MODEL",
        "coding_model": "OLLAMA_CODING_MODEL",
        "fast_model": "OLLAMA_FAST_MODEL",
    },
    "openrouter": {
        "base_url": "OPENROUTER_BASE_URL",
        "primary_model": "OPENROUTER_PRIMARY_MODEL",
        "reasoning_model": "OPENROUTER_REASONING_MODEL",
        "fallback_model": "OPENROUTER_FALLBACK_MODEL",
    },
}


def _snapshot() -> Dict[str, Any]:
    """The current config as a JSON-safe payload — no secrets, ever."""
    return {
        "provider": config.PROVIDER,
        "ollama": {
            "base_url": config.OLLAMA_BASE_URL,
            "general_model": config.OLLAMA_GENERAL_MODEL,
            "coding_model": config.OLLAMA_CODING_MODEL,
            "fast_model": config.OLLAMA_FAST_MODEL,
            "timeout": config.OLLAMA_TIMEOUT,
        },
        "openrouter": {
            "base_url": config.OPENROUTER_BASE_URL,
            "primary_model": config.OPENROUTER_PRIMARY_MODEL,
            "reasoning_model": config.OPENROUTER_REASONING_MODEL,
            "fallback_model": config.OPENROUTER_FALLBACK_MODEL,
        },
    }


async def persist_ai_settings() -> bool:
    """Write the current settings to kv (never the key). Did the row land?"""
    return await db.kv_set(SETTINGS_KEY, json.dumps(_snapshot()))


async def is_persisted() -> bool:
    """True when a saved choice exists — what the settings screen reports."""
    try:
        return await db.kv_get(SETTINGS_KEY) is not None
    except Exception as exc:  # noqa: BLE001 - a read must not break a response
        logger.warning("Persisted-settings read failed: %s", type(exc).__name__)
        return False


def save_api_key(api_key: str) -> bool:
    """Store the key in credential storage. Never kv, never a response."""
    return set_secret(API_KEY_SECRET, api_key.strip())


async def apply_persisted_ai_settings() -> List[str]:
    """Re-apply saved settings at startup. Returns the names applied."""
    try:
        raw = await db.kv_get(SETTINGS_KEY)
    except Exception as exc:  # noqa: BLE001 - degrade to environment defaults
        logger.warning("Persisted-settings load failed: %s", type(exc).__name__)
        return []
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("Persisted AI settings in kv are not valid JSON; ignoring")
        return []
    if not isinstance(payload, dict):
        return []

    applied: List[str] = []

    provider = payload.get("provider")
    if provider in _VALID_PROVIDERS:
        config.PROVIDER = provider
        config.AI_PROVIDER = provider
        applied.append("provider")

    for section, mapping in _SECTIONS.items():
        values = payload.get(section)
        if not isinstance(values, dict):
            continue
        for key, attr in mapping.items():
            value = values.get(key)
            if isinstance(value, str) and value.strip():
                setattr(config, attr, value.strip())
                applied.append(attr)

    timeout = (payload.get("ollama") or {}).get("timeout") \
        if isinstance(payload.get("ollama"), dict) else None
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0:
        config.OLLAMA_TIMEOUT = float(timeout)
        applied.append("OLLAMA_TIMEOUT")

    if has_secret(API_KEY_SECRET):
        key = get_secret(API_KEY_SECRET)
        if key:
            config.OPENROUTER_API_KEY = key
            applied.append("api_key")

    if applied:
        logger.info("Applied persisted AI settings: %s", ", ".join(applied))
    return applied
