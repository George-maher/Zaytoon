import logging
from datetime import datetime, timedelta
from typing import Optional, List

from .models import ReminderData, ReminderStatus, RecurrenceType, detect_recurrence, RECURRENCE_DELTAS

logger = logging.getLogger("Zaytoon.ReminderService")


class ReminderService:
    def __init__(self, memory_agent):
        self._memory = memory_agent

    async def create(
        self,
        task: str,
        scheduled_time: datetime,
        description: str = "",
        recurring: str = "none",
        session_id: str = "default",
        timezone_offset: int = 0,
    ) -> dict:
        utc_dt = scheduled_time - timedelta(hours=timezone_offset)
        utc_str = utc_dt.strftime("%Y-%m-%d %H:%M:%S")

        result = await self._memory.add_reminder(
            title=task,
            remind_time=utc_str,
            description=description,
            recurring=recurring,
            timezone_offset=0,
            session_id=session_id,
        )

        if "error" in result:
            return {"success": False, "error": result["error"]}

        reminder = ReminderData(
            reminder_id=result.get("id"),
            task=task,
            scheduled_time=scheduled_time,
            status=ReminderStatus.PENDING,
            recurring=RecurrenceType(recurring),
            session_id=session_id,
            description=description,
            timezone_offset=timezone_offset,
        )

        logger.info(f"Reminder created: id={reminder.reminder_id} task='{task}' at {scheduled_time}")
        return {"success": True, "reminder": reminder.to_dict()}

    async def create_from_natural(
        self,
        text: str,
        language: str = "en",
        session_id: str = "default",
        parsed_time: Optional[datetime] = None,
        timezone_offset: int = 0,
    ) -> dict:
        from scheduler.parser import TimeParser

        if parsed_time is None:
            parsed_time = TimeParser.parse_time(text)
        if parsed_time is None:
            if language == "ar":
                return {
                    "success": False,
                    "error": "ما فهمتش الوقت. لو سمحت حدد الوقت بوضوح زي 'بكرة الساعة 8' أو 'فكرني بعد ساعة'.",
                    "confirmation": "ما فهمتش الوقت. لو سمحت حدد الوقت بوضوح زي 'بكرة الساعة 8' أو 'فكرني بعد ساعة'.",
                }
            return {
                "success": False,
                "error": "Could not parse time from message",
                "confirmation": "I couldn't understand the time. Please specify clearly like 'tomorrow at 8 AM'.",
            }

        task = TimeParser.extract_task(text, parsed_time)
        recurring = detect_recurrence(text)

        result = await self.create(
            task=task,
            scheduled_time=parsed_time,
            recurring=recurring.value,
            session_id=session_id,
            timezone_offset=timezone_offset,
        )

        if not result["success"]:
            return {"success": False, "error": result["error"], "confirmation": ""}

        confirmation = self._build_confirmation(task, parsed_time, recurring, language)
        reminder_data = result["reminder"]
        return {
            "success": True,
            "id": reminder_data["reminder_id"],
            "task": task,
            "time": parsed_time.isoformat(),
            "recurring": recurring.value,
            "confirmation": confirmation,
        }

    def _build_confirmation(self, task: str, dt: datetime, recurring: RecurrenceType, language: str) -> str:
        if language == "ar":
            hour = dt.hour
            minute = dt.minute
            period = "مساءً" if hour >= 12 else "صباحاً"
            display_hour = hour - 12 if hour > 12 else (12 if hour == 0 else hour)
            time_str = f"{display_hour}:{minute:02d} {period}"
            day_str = dt.strftime("%A")
            ar_days = {
                "Monday": "الاثنين", "Tuesday": "الثلاثاء", "Wednesday": "الأربعاء",
                "Thursday": "الخميس", "Friday": "الجمعة", "Saturday": "السبت", "Sunday": "الأحد",
            }
            day_ar = ar_days.get(day_str, day_str)
            if recurring != RecurrenceType.NONE:
                rec_ar = {"daily": "كل يوم", "weekly": "كل أسبوع", "monthly": "كل شهر", "weekdays": "أيام العمل"}
                return f"تم يا فندم. هذكرك بـ '{task}' يوم {day_ar} الساعة {time_str} {rec_ar.get(recurring.value, '')}"
            return f"تم يا فندم. هذكرك بـ '{task}' الساعة {time_str} يوم {day_ar}."
        else:
            time_str = dt.strftime("%I:%M %p").lstrip("0")
            day_str = dt.strftime("%A, %B %d")
            if recurring != RecurrenceType.NONE:
                rec_en = {"daily": "every day", "weekly": "every week", "monthly": "every month", "weekdays": "on weekdays"}
                return f"Done. I'll remind you to '{task}' on {day_str} at {time_str} {rec_en.get(recurring.value, '')}."
            return f"Done. Reminder set: '{task}' for {day_str} at {time_str}."

    async def list(
        self, status: str = "pending", session_id: Optional[str] = None
    ) -> List[dict]:
        """Pending reminders (or ``status``), due-first.

        ``session_id=None`` lists **every** session — the default, because a
        reminder outlives the conversation that created it (see
        ``MemoryAgent.list_reminders``). Pass an id to filter deliberately.
        """
        rows = await self._memory.list_reminders(status=status, session_id=session_id)
        return [ReminderData.from_db_row(r).to_dict() for r in rows]

    async def get(self, reminder_id: int) -> Optional[dict]:
        all_reminders = await self._memory.list_reminders(status="pending")
        all_reminders += await self._memory.list_reminders(status="completed")
        for r in all_reminders:
            if r["id"] == reminder_id:
                return ReminderData.from_db_row(r).to_dict()
        return None

    async def delete(self, reminder_id: int) -> bool:
        return await self._memory.delete_reminder(reminder_id)

    async def mark_completed(self, reminder_id: int) -> bool:
        return await self._memory.complete_reminder(reminder_id)

    async def mark_failed(self, reminder_id: int) -> None:
        await self._memory.fail_reminder(reminder_id)

    async def handle_recurring(self, reminder: ReminderData) -> Optional[ReminderData]:
        if reminder.recurring == RecurrenceType.NONE:
            return None
        delta_fn = RECURRENCE_DELTAS.get(reminder.recurring)
        if not delta_fn:
            return None
        delta = delta_fn()
        new_time = reminder.scheduled_time + timedelta(**delta)
        result = await self.create(
            task=reminder.task,
            scheduled_time=new_time,
            description=reminder.description,
            recurring=reminder.recurring.value,
            session_id=reminder.session_id,
            timezone_offset=reminder.timezone_offset,
        )
        if result["success"]:
            raw = result["reminder"]
            return ReminderData(
                reminder_id=raw.get("reminder_id"),
                task=raw.get("task", ""),
                scheduled_time=new_time,
                description=raw.get("description", ""),
                status=ReminderStatus(raw.get("status", "pending")),
                recurring=RecurrenceType(raw.get("recurring", "none")),
                session_id=raw.get("session_id", "default"),
                created_at=datetime.fromisoformat(raw["created_at"]) if raw.get("created_at") else None,
                timezone_offset=raw.get("timezone_offset", 0),
                # The persisted count, not the data class's ephemeral one: it
                # used to restart at 0 on every tick because nothing ever wrote
                # it back, so "attempt 3 of 3" was only ever true in memory.
                notify_attempts=raw.get("attempts", raw.get("notify_attempts", 0)),
                notify_channel=raw.get("notify_channel", "websocket"),
                tags=raw.get("tags", []),
            )
        return None

    async def get_due(self) -> List[ReminderData]:
        """Due reminders, already **claimed** (status `firing`) in the store.

        The status used to be set on the returned object only — the row stayed
        `pending`, so a second tick saw the same reminder again while the first
        was still being delivered. The claim is the store's job now
        (`MemoryAgent.get_due_reminders`), and this reflects it instead of
        dressing up the value.
        """
        rows = await self._memory.get_due_reminders()
        return [ReminderData.from_db_row(dict(row)) for row in rows]

    async def release(self, reminder_id: int, max_attempts: int = 3) -> str:
        """Undo a claim after a failed delivery; returns the new row status.

        This is the counterpart to `mark_completed`. Without it a delivery
        failure had exactly one outcome — the reminder was consumed anyway — and
        the "retry" in the scheduler only re-notified inside one tick.
        """
        return await self._memory.release_reminder(reminder_id, max_attempts)

    async def requeue_stale_claims(self) -> int:
        """Recover reminders orphaned by a crash between claim and delivery."""
        return await self._memory.requeue_stale_claims()

    async def format_list_for_display(self, reminders: List[dict], language: str) -> str:
        if not reminders:
            if language == "ar":
                return "لا توجد مهام معلقة حالياً."
            return "No pending reminders."

        lines = []
        for r in reminders:
            rid = r.get("reminder_id") or r.get("id", "?")
            task = r.get("task") or r.get("title", "?")
            time_raw = r.get("scheduled_time") or r.get("remind_time", "")
            rec = r.get("recurring", "none")
            rec_label = ""
            if rec and rec != "none":
                if language == "ar":
                    labels = {"daily": " (كل يوم)", "weekly": " (كل أسبوع)", "monthly": " (كل شهر)", "weekdays": " (أيام العمل)"}
                else:
                    labels = {"daily": " (daily)", "weekly": " (weekly)", "monthly": " (monthly)", "weekdays": " (weekdays)"}
                rec_label = labels.get(rec, "")

            if language == "ar":
                display_time = time_raw
                try:
                    dt = datetime.fromisoformat(time_raw)
                    hour = dt.hour
                    period = "مساءً" if hour >= 12 else "صباحاً"
                    display_hour = hour - 12 if hour > 12 else (12 if hour == 0 else hour)
                    display_time = f"{display_hour}:{dt.minute:02d} {period}"
                except (ValueError, TypeError):
                    pass
                lines.append(f"#{rid}: {task} ⏰ {display_time}{rec_label}")
            else:
                lines.append(f"#{rid}: {task} @ {time_raw}{rec_label}")

        if language == "ar":
            return "مهامك:\n" + "\n".join(lines)
        return "Your reminders:\n" + "\n".join(lines)
