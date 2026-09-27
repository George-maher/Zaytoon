import json
import logging
from typing import Any, Optional

logger = logging.getLogger("Zaytoon.Memory.Preferences")


class UserPreferences:
    def __init__(self, memory_agent):
        self._memory = memory_agent

    async def get(self, key: str, default: Any = None) -> Any:
        val = await self._memory.get_preference(key)
        if not val:
            return default
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return val

    async def set(self, key: str, value: Any):
        serialized = json.dumps(value) if not isinstance(value, str) else value
        await self._memory.set_preference(key, serialized)

    async def get_all(self) -> dict:
        prefs = {}
        for key in [
            "language",
            "theme",
            "tts_enabled",
            "tts_voice",
            "wake_word",
            "push_to_talk",
        ]:
            prefs[key] = await self.get(key)
        return prefs

    async def get_language(self) -> str:
        return await self.get("language", "en")

    async def set_language(self, lang: str):
        await self.set("language", lang)

    async def is_tts_enabled(self) -> bool:
        return await self.get("tts_enabled", True)

    async def get_tts_voice(self) -> str:
        lang = await self.get_language()
        if lang == "ar":
            return await self.get("tts_voice", "ar-EG-ShakirNeural")
        return await self.get("tts_voice", "en-GB-RyanNeural")
