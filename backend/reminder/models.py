from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Optional


class ReminderStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FIRING = "firing"
    FAILED = "failed"


class RecurrenceType(str, Enum):
    NONE = "none"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    WEEKDAYS = "weekdays"
    CUSTOM = "custom"


RECURRENCE_DELTAS = {
    RecurrenceType.DAILY: lambda: {"days": 1},
    RecurrenceType.WEEKLY: lambda: {"weeks": 1},
    RecurrenceType.MONTHLY: lambda: {"days": 30},
    RecurrenceType.WEEKDAYS: lambda: {"days": 1},
}

RECURRENCE_KEYWORDS_EN = {
    "every day": RecurrenceType.DAILY,
    "daily": RecurrenceType.DAILY,
    "every morning": RecurrenceType.DAILY,
    "every night": RecurrenceType.DAILY,
    "every week": RecurrenceType.WEEKLY,
    "weekly": RecurrenceType.WEEKLY,
    "every monday": RecurrenceType.WEEKLY,
    "every tuesday": RecurrenceType.WEEKLY,
    "every wednesday": RecurrenceType.WEEKLY,
    "every thursday": RecurrenceType.WEEKLY,
    "every friday": RecurrenceType.WEEKLY,
    "every saturday": RecurrenceType.WEEKLY,
    "every sunday": RecurrenceType.WEEKLY,
    "every month": RecurrenceType.MONTHLY,
    "monthly": RecurrenceType.MONTHLY,
    "weekdays": RecurrenceType.WEEKDAYS,
    "every weekday": RecurrenceType.WEEKDAYS,
}

RECURRENCE_KEYWORDS_AR = {
    "كل يوم": RecurrenceType.DAILY,
    "يوميا": RecurrenceType.DAILY,
    "يومي": RecurrenceType.DAILY,
    "كل صباح": RecurrenceType.DAILY,
    "كل مساء": RecurrenceType.DAILY,
    "كل اسبوع": RecurrenceType.WEEKLY,
    "كل أسبوع": RecurrenceType.WEEKLY,
    "اسبوعيا": RecurrenceType.WEEKLY,
    "اسبوعي": RecurrenceType.WEEKLY,
    "كل شهر": RecurrenceType.MONTHLY,
    "شهريا": RecurrenceType.MONTHLY,
    "شهري": RecurrenceType.MONTHLY,
    "كل اثنين": RecurrenceType.WEEKLY,
    "كل ثلاثاء": RecurrenceType.WEEKLY,
    "كل اربعاء": RecurrenceType.WEEKLY,
    "كل خميس": RecurrenceType.WEEKLY,
    "كل جمعة": RecurrenceType.WEEKLY,
    "كل سبت": RecurrenceType.WEEKLY,
    "كل احد": RecurrenceType.WEEKLY,
    "في الاسبوع": RecurrenceType.WEEKLY,
    "ايام العمل": RecurrenceType.WEEKDAYS,
    "كل يومين": RecurrenceType.DAILY,
}


@dataclass
class ReminderData:
    task: str
    scheduled_time: datetime
    status: ReminderStatus = ReminderStatus.PENDING
    recurring: RecurrenceType = RecurrenceType.NONE
    session_id: str = "default"
    description: str = ""
    reminder_id: Optional[int] = None
    created_at: Optional[datetime] = None
    timezone_offset: int = 0
    notify_attempts: int = 0
    notify_channel: str = "websocket"
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        d["recurring"] = self.recurring.value
        d["scheduled_time"] = self.scheduled_time.isoformat() if self.scheduled_time else None
        d["created_at"] = self.created_at.isoformat() if self.created_at else None
        return d

    @classmethod
    def from_db_row(cls, row: dict) -> "ReminderData":
        scheduled = None
        created = None
        if row.get("remind_time"):
            try:
                scheduled = datetime.strptime(str(row["remind_time"])[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                scheduled = None
        if row.get("created_at"):
            try:
                created = datetime.strptime(str(row["created_at"])[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                created = None

        return cls(
            reminder_id=row.get("id"),
            task=row.get("title", ""),
            description=row.get("description", ""),
            scheduled_time=scheduled,
            status=ReminderStatus(row.get("status", "pending")),
            recurring=RecurrenceType(row.get("recurring", "none")),
            session_id=row.get("session_id", "default"),
            created_at=created,
        )


def detect_recurrence(text: str) -> RecurrenceType:
    t = text.lower().strip()
    merged = {**RECURRENCE_KEYWORDS_EN, **RECURRENCE_KEYWORDS_AR}
    for keyword, rtype in merged.items():
        if keyword in t:
            return rtype
    return RecurrenceType.NONE
