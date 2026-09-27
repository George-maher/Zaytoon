import time
from typing import Any
from collections import OrderedDict


class ShortTermMemory:
    def __init__(self, max_size: int = 100, ttl_seconds: int = 3600):
        self._store: OrderedDict[str, dict] = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl_seconds

    def _evict(self):
        now = time.time()
        expired = [k for k, v in self._store.items() if now - v["timestamp"] > self._ttl]
        for k in expired:
            del self._store[k]
        while len(self._store) > self._max_size:
            self._store.popitem(last=False)

    def put(self, key: str, value: Any, metadata: dict | None = None):
        self._store[key] = {"value": value, "metadata": metadata or {}, "timestamp": time.time()}
        self._evict()

    def get(self, key: str, default: Any = None) -> Any:
        entry = self._store.get(key)
        if entry is None:
            return default
        if time.time() - entry["timestamp"] > self._ttl:
            del self._store[key]
            return default
        return entry["value"]

    def get_all(self) -> list[dict]:
        self._evict()
        now = time.time()
        return [
            {"key": k, "value": v["value"], "metadata": v["metadata"], "age": now - v["timestamp"]}
            for k, v in self._store.items()
        ]

    def delete(self, key: str):
        self._store.pop(key, None)

    def clear(self):
        self._store.clear()

    def get_context_string(self) -> str:
        entries = self.get_all()
        if not entries:
            return ""
        parts = [f"[{e['metadata'].get('label', 'info')}] {e['value']}" for e in entries]
        return "\n".join(parts)
