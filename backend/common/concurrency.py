"""Small concurrency primitives shared by the storage layer.

Kept dependency-free (stdlib ``asyncio`` only) so it can be imported from the
tool layer and from tests without dragging in configuration or the network.
"""

from __future__ import annotations

import asyncio
from typing import Optional


class ReentrantAsyncLock:
    """An ``asyncio.Lock`` that the *same task* may enter more than once.

    A plain lock is correct until one guarded helper calls another guarded
    helper — then it self-deadlocks, and because ``asyncio`` has no timeout on
    ``acquire()`` the whole turn hangs with no error to point at the cause.
    That is exactly the shape of ``MemoryAgent.get_due_reminders``, which marks
    a reminder complete and re-schedules it by calling two other writers.

    Re-entrancy is per **task**, not per object: a second task still blocks,
    which is the property the lock exists for. Ownership is recorded on
    acquisition rather than checked on every wait, so a task that is cancelled
    while waiting never becomes the owner.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: Optional[asyncio.Task] = None
        self._depth = 0

    async def acquire(self) -> None:
        task = asyncio.current_task()
        if task is not None and self._owner is task:
            self._depth += 1
            return
        await self._lock.acquire()
        self._owner = task
        self._depth = 1

    def release(self) -> None:
        if self._owner is not asyncio.current_task():
            raise RuntimeError("ReentrantAsyncLock released by a non-owner task")
        self._depth -= 1
        if self._depth <= 0:
            self._owner = None
            self._depth = 0
            self._lock.release()

    @property
    def locked(self) -> bool:
        return self._lock.locked()

    @property
    def owner(self) -> Optional[asyncio.Task]:
        return self._owner

    async def __aenter__(self) -> "ReentrantAsyncLock":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.release()


__all__ = ["ReentrantAsyncLock"]
