import asyncio
import logging
from typing import Optional

from reminder.service import ReminderService
from reminder.notification import NotificationManager
from reminder.models import ReminderData
from common.reporting import safe_error

logger = logging.getLogger("Zaytoon.Scheduler.Manager")

MAX_RETRIES = 3
RETRY_DELAYS = [1, 5, 15]

#: How many reminders are delivered at the same time inside one tick. One
#: notification manager and one event loop do not benefit from 200 concurrent
#: sends, but a serial loop means the first reminder's `1 + 5 + 15` s of
#: backoff delays *every* reminder behind it — a 15 s tick can therefore be
#: minutes late for the last one. Four is enough to hide a slow send behind the
#: others without turning the ceiling into the interesting part.
MAX_CONCURRENT_DELIVERIES = 4


class SchedulerManager:
    def __init__(self, reminder_service: ReminderService, notification_mgr: NotificationManager):
        self._reminder_service = reminder_service
        self._notification_mgr = notification_mgr
        self._check_interval = 15
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        if self._running:
            return
        # Before the first tick: a process that died between claiming a reminder
        # and delivering it left the row in `firing`, where no query looks for it.
        # Recovered here rather than in the store's constructor so the recovery
        # is tied to "a scheduler is now responsible for these rows" — and so a
        # read-only process does not rewrite state it will never act on.
        try:
            await self._reminder_service.requeue_stale_claims()
        except Exception as e:  # noqa: BLE001 - startup must not fail on this
            safe_error(e, operation="scheduler.requeue", component="SchedulerManager")
        self._running = True
        self._task = asyncio.create_task(self._check_loop())
        logger.info("Scheduler manager started (interval=%ds)", self._check_interval)

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Scheduler manager stopped")

    async def _check_loop(self):
        while self._running:
            try:
                await self.deliver_due()
            except asyncio.CancelledError:
                break
            except Exception as e:
                # The loop must survive a bad tick: one failed check would
                # otherwise kill scheduling silently until the next restart.
                safe_error(e, operation="scheduler.check", component="SchedulerManager")
            await asyncio.sleep(self._check_interval)

    async def deliver_due(self) -> int:
        """Claim and deliver every reminder that is due now. Returns how many.

        Split out from the loop so one tick can be driven from a test, a manual
        refresh, or the timer without anyone reaching into privates — and so the
        loop body stays exactly "tick, then sleep".
        """
        due = await self._reminder_service.get_due()
        if due:
            logger.info("Reminders due: %d", len(due))
            gate = asyncio.Semaphore(MAX_CONCURRENT_DELIVERIES)

            async def deliver(reminder: ReminderData) -> None:
                async with gate:
                    await self._fire_reminder(reminder)

            # `return_exceptions=True`: one reminder whose delivery raises must
            # not cancel its siblings mid-send, which is what a bare gather does
            # to the tasks still in flight.
            await asyncio.gather(*(deliver(r) for r in due), return_exceptions=True)
        return len(due)

    async def _fire_reminder(self, reminder: ReminderData):
        """Deliver one claimed reminder, then settle the row either way.

        The row was already claimed by `get_due`, so *this* function owns its
        fate — the single place where completion, rescheduling and retry are
        decided. Settling it anywhere else is how a daily reminder ended up with
        two next occurrences and how a failed notification consumed one.
        """
        reminder_dict = reminder.to_dict()
        for attempt in range(MAX_RETRIES):
            try:
                success = await self._notification_mgr.notify(reminder_dict)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                safe_error(
                    e, operation="reminder.fire", component="SchedulerManager",
                    detail=f"attempt {attempt + 1}/{MAX_RETRIES}",
                )
            else:
                if success:
                    await self._reminder_service.mark_completed(reminder.reminder_id)
                    recurring = await self._reminder_service.handle_recurring(reminder)
                    if recurring:
                        logger.info("Recurring reminder created: id=%d", recurring.reminder_id)
                    return
                # `notify()` returning False is a failure, not an exception; it
                # used to fall through to the same `mark_failed` as a crash,
                # which made an unanswered panel look identical to a bug.
                logger.debug("Reminder #%d: no delivery channel accepted it",
                             reminder.reminder_id)

            if attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAYS[attempt] if attempt < len(RETRY_DELAYS) else 30
                logger.info("Retrying reminder #%d in %ds...", reminder.reminder_id, delay)
                await asyncio.sleep(delay)

        # Release rather than consume: the row goes back to `pending` for the
        # next tick and is only marked `failed` once its persisted attempt count
        # reaches the ceiling, so a notification backend that was briefly down
        # no longer swallows the reminder the user asked for.
        status = await self._reminder_service.release(
            reminder.reminder_id, max_attempts=MAX_RETRIES
        )
        logger.error(
            "Reminder #%d not delivered after %d attempts (row: %s)",
            reminder.reminder_id, MAX_RETRIES, status,
        )

    async def create_from_natural(
        self, text: str, language: str = "en", session_id: str = "default", parsed_time=None
    ) -> dict:
        return await self._reminder_service.create_from_natural(text, language, session_id, parsed_time=parsed_time)

    async def list_reminders(self, session_id: Optional[str] = None, status: str = "pending") -> list[dict]:
        """Workspace-wide by default; a named session filters (see the service)."""
        return await self._reminder_service.list(status=status, session_id=session_id)

    async def delete_reminder(self, reminder_id: int) -> bool:
        return await self._reminder_service.delete(reminder_id)

    async def mark_done(self, reminder_id: int) -> bool:
        return await self._reminder_service.mark_completed(reminder_id)

    async def get_reminder(self, reminder_id: int) -> Optional[dict]:
        return await self._reminder_service.get(reminder_id)
