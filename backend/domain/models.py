from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
import time
import asyncio
import hashlib
import uuid


class Intent(str, Enum):
    CHAT = "chat"
    CODE = "code"
    TOOLS = "tools"
    SCHEDULING = "scheduling"
    MEMORY = "memory"
    COMPLEX = "complex"
    ACTION = "action"


class Language(str, Enum):
    AR = "ar"
    EN = "en"


class AgentType(str, Enum):
    FAST = "fast"
    PLANNER = "planner"
    CODE = "code"
    TOOL = "tool"
    MEMORY = "memory"
    SCHEDULER = "scheduler"
    ROUTER = "router"


@dataclass
class Message:
    id: int
    role: str
    content: str
    language: Language = Language.EN
    session_id: str = "default"
    streaming: bool = False
    timestamp: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "role": self.role,
            "content": self.content,
            "language": self.language.value,
            "streaming": self.streaming,
            "timestamp": self.timestamp or datetime.now().isoformat(),
        }


@dataclass
class Reminder:
    id: int
    title: str
    remind_time: str
    recurring: str = "none"
    status: str = "pending"
    session_id: str = "default"
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "remind_time": self.remind_time,
            "recurring": self.recurring,
            "status": self.status,
        }


@dataclass
class TokenBucketRateLimiter:
    max_requests: int = 60
    window: int = 60
    _buckets: dict = field(default_factory=dict)
    _last_cleanup: float = field(default_factory=time.time)

    def _cleanup_stale(self):
        now = time.time()
        if now - self._last_cleanup < 300:
            return
        self._last_cleanup = now
        cutoff = now - self.window * 2
        stale = [k for k, v in self._buckets.items() if not v or v[-1] < cutoff]
        for k in stale:
            del self._buckets[k]

    def is_allowed(self, key: str) -> bool:
        self._cleanup_stale()
        now = time.time()
        if key not in self._buckets:
            self._buckets[key] = []
        bucket = self._buckets[key]
        bucket[:] = [t for t in bucket if now - t <= self.window]
        if len(bucket) >= self.max_requests:
            return False
        bucket.append(now)
        return True


@dataclass
class SpokenSentenceTracker:
    _spoken: set = field(default_factory=set)

    def mark_spoken(self, text: str):
        normalized = text.strip().lower().rstrip(".!?\u061F\u060C")
        if len(normalized) > 2:
            self._spoken.add(normalized)

    def is_spoken(self, text: str) -> bool:
        return text.strip().lower().rstrip(".!?\u061F\u060C") in self._spoken

    def get_new_sentences(self, full_text: str, split_fn) -> list[str]:
        if not full_text.strip():
            return []
        sentences = split_fn(full_text)
        return [s for s in sentences if s.strip() and not self.is_spoken(s) and len(s.strip()) >= 3]

    def clear(self):
        self._spoken.clear()


@dataclass
class SessionState:
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    current_task: Optional[asyncio.Task] = None
    tts_task: Optional[asyncio.Task] = None
    is_responding: bool = False
    spoken_tracker: SpokenSentenceTracker = field(default_factory=SpokenSentenceTracker)
    current_response_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    awaiting_command: bool = False
    last_active_ts: float = 0.0
    confirmation_manager: Optional[object] = None
    _sent_hashes: set = field(default_factory=set)
    _created_at: float = field(default_factory=time.time)

    def cancel(self):
        if self.confirmation_manager is not None:
            try:
                self.confirmation_manager.cancel_all("session cancelled")
            except Exception as e:  # noqa: BLE001 - cancel must always complete
                # Not merely a log line: if this fails, pending confirmations
                # outlive the session that asked for them, and an approval
                # granted afterwards could still be honoured for a session that
                # is gone. A security-relevant swallow, so it is reported as one.
                import logging

                logging.getLogger("Zaytoon.Domain").error(
                    "pending confirmations were NOT cancelled on session "
                    "disconnect (%s: %s) - approvals may still be honoured for "
                    "a dead session", type(e).__name__, e,
                )
        self.cancel_event.set()
        if self.tts_task and not self.tts_task.done():
            self.tts_task.cancel()
        if self.current_task and not self.current_task.done():
            self.current_task.cancel()

    def reset(self):
        self.cancel_event.clear()
        self.is_responding = False
        self.current_response_id = str(uuid.uuid4())[:8]
        self.spoken_tracker.clear()

    def is_duplicate(self, content: str) -> bool:
        h = hashlib.md5(content.strip().encode("utf-8")).hexdigest()
        if h in self._sent_hashes:
            return True
        self._sent_hashes.add(h)
        if len(self._sent_hashes) > 200:
            self._sent_hashes.clear()
        return False
