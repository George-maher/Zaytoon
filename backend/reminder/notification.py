import json
import logging
from datetime import datetime
from typing import Optional, Callable, Any

logger = logging.getLogger("Zaytoon.NotificationManager")


class NotificationManager:
    def __init__(self):
        self._webhook_url: Optional[str] = None
        self._broadcast_callback: Optional[Callable[[dict], Any]] = None
        self._fallback_logger = logging.getLogger("Zaytoon.ReminderFallback")

    def set_webhook(self, url: str):
        self._webhook_url = url
        logger.info(f"Notification webhook set: {url}")

    def set_broadcast_callback(self, callback: Callable[[dict], Any]):
        self._broadcast_callback = callback

    async def notify(self, reminder: dict) -> bool:
        task = reminder.get("task") or reminder.get("title", "Reminder")
        reminder_id = reminder.get("reminder_id") or reminder.get("id", "?")
        scheduled = reminder.get("scheduled_time") or reminder.get("remind_time", "")

        payload = {
            "type": "reminder",
            "id": reminder_id,
            "title": task,
            "message": f"\u23F0 {task}",
            "scheduled_time": scheduled,
            "timestamp": datetime.now().isoformat(),
        }

        channels = [
            ("broadcast", self._send_broadcast(payload)),
            ("webhook", self._send_webhook(payload)),
            ("console", self._send_console(payload)),
        ]

        results = {}
        any_success = False
        for name, coro in channels:
            try:
                ok = await coro
                results[name] = ok
                if ok:
                    any_success = True
            except Exception as e:
                results[name] = False
                logger.warning(f"Notification channel '{name}' failed: {e}")

        if not any_success:
            logger.error(f"All notification channels failed for reminder #{reminder_id}")
        else:
            logger.info(f"Reminder #{reminder_id} notified via: {[k for k, v in results.items() if v]}")

        return any_success

    async def _send_broadcast(self, payload: dict) -> bool:
        if not self._broadcast_callback:
            return False
        try:
            await self._broadcast_callback(payload)
            return True
        except Exception as e:
            logger.warning(f"Broadcast notification failed: {e}")
            return False

    async def _send_webhook(self, payload: dict) -> bool:
        if not self._webhook_url:
            return False
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    self._webhook_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
                return True
        except ImportError:
            logger.debug("httpx not available for webhook notification")
            return False
        except Exception as e:
            logger.warning(f"Webhook notification failed: {e}")
            return False

    async def _send_console(self, payload: dict) -> bool:
        try:
            msg = payload.get("message", "")
            rid = payload.get("id", "?")
            self._fallback_logger.info(f"[REMINDER #{rid}] {msg}")
            print(f"\n{'=' * 50}")
            print(f"\u23F0  REMINDER #{rid}: {msg}")
            print(f"{'=' * 50}\n")
            return True
        except Exception as e:
            logger.error(f"Console notification failed: {e}")
            return False
