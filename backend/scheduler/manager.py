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
                due = await self._reminder_service.get_due()
                for reminder in due:
                    logger.info("Reminder due: id=%d task='%s'", reminder.reminder_id, reminder.task)
                    await self._fire_reminder(reminder)
            except asyncio.CancelledError:
                break
            except Exception as e:
                # The loop must survive a bad tick: one failed check would
                # otherwise kill scheduling silently until the next restart.
                safe_error(e, operation="scheduler.check", component="SchedulerManager")
            await asyncio.sleep(self._check_interval)

    async def _fire_reminder(self, reminder: ReminderData):
        reminder_dict = reminder.to_dict()
        for attempt in range(MAX_RETRIES):
            try:
                success = await self._notification_mgr.notify(reminder_dict)
                if success:
                    await self._reminder_service.mark_completed(reminder.reminder_id)
                    recurring = await self._reminder_service.handle_recurring(reminder)
                    if recurring:
                        logger.info("Recurring reminder created: id=%d", recurring.reminder_id)
                    return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                safe_error(
                    e, operation="reminder.fire", component="SchedulerManager",
                    detail=f"attempt {attempt + 1}/{MAX_RETRIES}",
                )

            if attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAYS[attempt] if attempt < len(RETRY_DELAYS) else 30
                logger.info("Retrying reminder #%d in %ds...", reminder.reminder_id, delay)
                await asyncio.sleep(delay)

        logger.error("Reminder #%d failed after %d attempts", reminder.reminder_id, MAX_RETRIES)
        await self._reminder_service.mark_failed(reminder.reminder_id)

    async def create_from_natural(
        self, text: str, language: str = "en", session_id: str = "default", parsed_time=None
    ) -> dict:
        return await self._reminder_service.create_from_natural(text, language, session_id, parsed_time=parsed_time)

    async def list_reminders(self, session_id: str = "default", status: str = "pending") -> list[dict]:
        return await self._reminder_service.list(status=status, session_id=session_id)

    async def delete_reminder(self, reminder_id: int) -> bool:
        return await self._reminder_service.delete(reminder_id)

    async def mark_done(self, reminder_id: int) -> bool:
        return await self._reminder_service.mark_completed(reminder_id)

    async def get_reminder(self, reminder_id: int) -> Optional[dict]:
        return await self._reminder_service.get(reminder_id)
