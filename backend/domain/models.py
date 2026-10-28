from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
import time
import asyncio
import hashlib
import uuid


def _new_voice_session():
    """Build the per-session voice state without an import cycle.

    ``services.voice_session`` is deliberately dependency-free (no domain, no
    transport), but importing it at module scope would still put the services
    package in domain's import path — a direction this tree keeps one-way.
    """
    from services.voice_session import VoiceSession

    return VoiceSession()


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
    #: The in-flight microphone utterance (transcribe → dispatch). Its own
    #: handle, so a stop command reaches work that is sitting inside the STT
    #: call rather than only the turn it has not created yet.
    audio_task: Optional[asyncio.Task] = None
    #: When the socket last sent anything. Read by the heartbeat sweep; it is
    #: evidence, not a policy input.
    last_seen_ts: float = 0.0
    is_responding: bool = False
    spoken_tracker: SpokenSentenceTracker = field(default_factory=SpokenSentenceTracker)
    current_response_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    #: The voice lifecycle (state, wake arming, utterance idempotency). It owns
    #: `awaiting_command`/`last_active_ts`; the properties below are the
    #: backwards-compatible spelling used by the wake gate, so the two cannot
    #: disagree about whether the follow-up window is armed.
    voice: "VoiceSession" = field(default_factory=_new_voice_session)
    confirmation_manager: Optional[object] = None
    _sent_hashes: set = field(default_factory=set)
    _created_at: float = field(default_factory=time.time)

    #: Compatibility spellings for the wake gate and its tests. The state is
    #: stored exactly once, on `voice`.
    @property
    def awaiting_command(self) -> bool:
        return self.voice.awaiting_command

    @awaiting_command.setter
    def awaiting_command(self, value: bool) -> None:
        self.voice.awaiting_command = bool(value)

    @property
    def last_active_ts(self) -> float:
        return self.voice.last_active_ts

    @last_active_ts.setter
    def last_active_ts(self, value: float) -> None:
        self.voice.last_active_ts = float(value or 0.0)

    def is_current(self, response_id: Optional[str]) -> bool:
        """Whether ``response_id`` is still the turn this session is running.

        The guard against a *stale* worker — a turn that was cancelled,
        superseded by a newer utterance, or orphaned by a reconnect — emitting
        frames after the fact. `reset()` mints a new id at the start of every
        turn, so anything holding an older one is by definition no longer the
        current turn: its tokens would append to the wrong bubble and its audio
        would play over the answer the user actually asked for.

        An unknown/absent id returns False for the same reason: the transport
        always stamps the id it is running with, so a missing one is a bug in
        the caller rather than a blanket permission to write.
        """
        return bool(response_id) and response_id == self.current_response_id

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
        # Never cancel the caller. The audio handler calls `cancel()` itself —
        # it is superseding whatever ran before the recording — and the audio
        # task *is* `state.audio_task`, so a blanket cancel killed the handler
        # in the middle of its own sequence: the transcript arrived on the wire
        # and the turn behind it never started, with nothing logged, because
        # the cancellation landed between two awaits of the same coroutine.
        try:
            caller = asyncio.current_task()
        except RuntimeError:  # no running loop (a synchronous teardown)
            caller = None
        for handle in (self.audio_task, self.tts_task, self.current_task):
            if handle is not None and handle is not caller and not handle.done():
                handle.cancel()

    def reset(self):
        self.cancel_event.clear()
        self.is_responding = False
        self.current_response_id = str(uuid.uuid4())[:8]
        self.spoken_tracker.clear()

    def is_duplicate(self, content: str) -> bool:
        """Whether this exact reply text was already delivered in this session.

        **Observability only.** It used to gate delivery, and that was a bug
        twice over: a user who asked "say that again" got a turn whose text was
        suppressed entirely (no `message`, no `done`), and the identity of a
        reply is the turn that produced it — `response_id` — not its text. Two
        legitimate answers that happen to read the same are two answers.
        """
        h = hashlib.md5(content.strip().encode("utf-8")).hexdigest()
        if h in self._sent_hashes:
            return True
        self._sent_hashes.add(h)
        if len(self._sent_hashes) > 200:
            self._sent_hashes.clear()
        return False
