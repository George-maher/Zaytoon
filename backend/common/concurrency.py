"""Small concurrency primitives shared by the storage layer.

Kept dependency-free (stdlib ``asyncio`` only) so it can be imported from the
tool layer and from tests without dragging in configuration or the network.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, Deque, Dict, Optional


class ReentrantAsyncLock:
    """An ``asyncio.Lock`` that the *same task* may enter more than once.

    A plain lock is correct until one guarded helper calls another guarded
    helper — then it self-deadlocks, and because ``asyncio`` has no timeout on
    ``acquire()`` the whole turn hangs with no error to point at the cause.

    The case that motivated it is gone: ``MemoryAgent.get_due_reminders`` used to
    mark a reminder complete and re-schedule it from inside the read, calling two
    other guarded writers while holding the lock. Reads no longer mutate (see
    ``tests/test_reminder_lifecycle.py``), so nothing in the store nests today —
    and that is worth knowing, because a re-entrant lock also permits a nesting
    that is really a bug. This class stays because the storage layer is written
    by many hands: a future helper that calls another one must not hang the
    process, and refusing the pattern is the job of review and the tests, not of
    the primitive.

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


class QueueTimeout(Exception):
    """Raised when a bounded gate did not free up within its timeout.

    A distinct type so the caller can choose the *user-visible* answer — "the
    model is busy", with a suggestion — rather than reporting a timeout as if
    every model had failed. Silence-and-hang is the alternative, and a UI that
    spins forever is worse than one that says no.
    """


class BoundedGate:
    """At most ``limit`` concurrent holders, queued in order, re-entrant per task.

    Three properties, each needed by a different caller:

    * **Bounded** — a local runtime serves one generation at a time well and
      two or three at once badly. Unlimited parallel ``complete()`` calls from
      several sessions do not add throughput; they thrash or OOM the daemon, so
      every request pays for the pile-up.
    * **Fair** — a FIFO queue, so a request that arrives while six are running
      is not overtaken by every later arrival. Strict FIFO: a caller may *not*
      take a free slot when a waiter is already queued, or an impatient poller
      starves the queue.
    * **Re-entrant per task** — a streaming call that falls back to a blocking
      one after its first delta is the *same* logical request holding the *same*
      slot; a non-reentrant gate self-deadlocks there, which is precisely the
      nesting ``stream()`` does.

    Timeouts expire waiters only; a holder that already got the slot is never
    interrupted, because killing a half-sent generation wastes the very resource
    the gate exists to protect.
    """

    def __init__(self, limit, queue_timeout=None) -> None:
        #: ``int`` or a zero-argument callable, so a change to config takes
        #: effect for the next acquisition rather than being frozen at import.
        self._limit = limit
        self._timeout = queue_timeout
        self._holders: Dict[asyncio.Task, int] = {}
        self._in_flight = 0
        self._queue: Deque[asyncio.Future] = deque()

    @staticmethod
    def _resolve(value):
        return value() if callable(value) else value

    def stats(self) -> Dict[str, Any]:
        """Live occupancy. Reported by ``/health`` so "why is it slow?" has an answer."""
        return {
            "limit": self._limit_value(),
            "in_flight": self._in_flight,
            "queued": len(self._queue),
        }

    def _limit_value(self) -> int:
        try:
            return max(1, int(self._resolve(self._limit)))
        except (TypeError, ValueError):
            return 1

    async def acquire(self) -> None:
        task = asyncio.current_task()
        if task is not None and self._holders.get(task):
            self._holders[task] += 1
            return

        # No queue and a free slot: take it. Otherwise wait our turn.
        if not self._queue and self._in_flight < self._limit_value():
            self._in_flight += 1
            if task is not None:
                self._holders[task] = 1
            return

        loop = asyncio.get_running_loop()
        ticket: asyncio.Future = loop.create_future()
        self._queue.append(ticket)
        timeout = self._resolve(self._timeout) if self._timeout is not None else None
        try:
            if timeout is None:
                await ticket
            else:
                await asyncio.wait_for(ticket, timeout=timeout)
        except asyncio.TimeoutError:
            # `wait_for` *cancels* the awaited future when it times out, so
            # "is it done" cannot tell a handoff from a cancellation — both are
            # done by now. Only a resolved-with-result ticket is a granted slot;
            # reading `done()` alone made a timed-out caller run as if it owned
            # the gate, which is worse than no gate at all (the first version of
            # this did exactly that, and `in_flight` disagreed with reality).
            granted = ticket.done() and not ticket.cancelled()
            if not granted:
                try:
                    self._queue.remove(ticket)
                except ValueError:  # pragma: no cover - popped by release at once
                    pass
                raise QueueTimeout(
                    f"no slot freed within {timeout:.1f}s "
                    f"({self._in_flight}/{self._limit_value()} busy)"
                ) from None
            # A slot was handed over in the same tick the timeout fired. Taking
            # it and running is honest; discarding it would drop work already
            # paid for and let the next waiter start over.
        except asyncio.CancelledError:
            if not ticket.done():
                try:
                    self._queue.remove(ticket)
                except ValueError:
                    pass
            raise
        if task is not None:
            self._holders[task] = 1
        # `release()` transferred a slot to this ticket; occupancy is unchanged.

    def release(self) -> None:
        task = asyncio.current_task()
        if task is not None and self._holders.get(task, 0) > 1:
            self._holders[task] -= 1
            return
        if task is not None:
            self._holders.pop(task, None)

        while self._queue:
            nxt = self._queue.popleft()
            if not nxt.done():
                nxt.set_result(True)
                return
            # A timed-out or cancelled waiter: its slot never left us, so loop
            # on to the next real one instead of leaking the position.
        self._in_flight = max(0, self._in_flight - 1)

    async def __aenter__(self) -> "BoundedGate":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.release()


__all__ = ["ReentrantAsyncLock", "BoundedGate", "QueueTimeout"]
