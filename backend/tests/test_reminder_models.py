import pytest
from datetime import datetime
from reminder.models import (
    ReminderData, ReminderStatus, RecurrenceType,
    detect_recurrence, RECURRENCE_KEYWORDS_EN, RECURRENCE_KEYWORDS_AR,
)


class TestDetectRecurrence:
    def test_daily_en(self):
        assert detect_recurrence("every day at 8am") == RecurrenceType.DAILY
        assert detect_recurrence("daily standup") == RecurrenceType.DAILY

    def test_weekly_en(self):
        assert detect_recurrence("every week meeting") == RecurrenceType.WEEKLY
        assert detect_recurrence("weekly report") == RecurrenceType.WEEKLY

    def test_monthly_en(self):
        assert detect_recurrence("every month review") == RecurrenceType.MONTHLY
        assert detect_recurrence("monthly billing") == RecurrenceType.MONTHLY

    def test_weekdays_en(self):
        assert detect_recurrence("weekdays at 7am") == RecurrenceType.WEEKDAYS

    def test_daily_ar(self):
        assert detect_recurrence("كل يوم الساعة 8") == RecurrenceType.DAILY
        assert detect_recurrence("يوميا") == RecurrenceType.DAILY

    def test_weekly_ar(self):
        assert detect_recurrence("كل اسبوع") == RecurrenceType.WEEKLY
        assert detect_recurrence("اسبوعيا") == RecurrenceType.WEEKLY

    def test_monthly_ar(self):
        assert detect_recurrence("كل شهر") == RecurrenceType.MONTHLY
        assert detect_recurrence("شهريا") == RecurrenceType.MONTHLY

    def test_no_recurrence(self):
        assert detect_recurrence("tomorrow at 6pm") == RecurrenceType.NONE
        assert detect_recurrence("بكرة الساعة 5") == RecurrenceType.NONE
        assert detect_recurrence("random text without keywords") == RecurrenceType.NONE


class TestReminderData:
    def test_create(self):
        now = datetime.now()
        r = ReminderData(
            task="Test task",
            scheduled_time=now,
            status=ReminderStatus.PENDING,
            recurring=RecurrenceType.DAILY,
            session_id="sess_1",
        )
        assert r.task == "Test task"
        assert r.status == ReminderStatus.PENDING
        assert r.recurring == RecurrenceType.DAILY
        assert r.session_id == "sess_1"

    def test_to_dict(self):
        now = datetime.now()
        r = ReminderData(task="Task", scheduled_time=now, reminder_id=42)
        d = r.to_dict()
        assert d["task"] == "Task"
        assert d["reminder_id"] == 42
        assert d["status"] == "pending"
        assert d["recurring"] == "none"
        assert d["scheduled_time"] is not None

    def test_from_db_row(self):
        row = {
            "id": 1,
            "title": "Test",
            "description": "desc",
            "remind_time": "2025-06-01 10:00:00",
            "status": "pending",
            "recurring": "daily",
            "session_id": "default",
            "created_at": "2025-05-01 00:00:00",
        }
        r = ReminderData.from_db_row(row)
        assert r.reminder_id == 1
        assert r.task == "Test"
        assert r.status == ReminderStatus.PENDING
        assert r.recurring == RecurrenceType.DAILY
        assert r.scheduled_time is not None
        assert r.scheduled_time.hour == 10
        assert r.created_at is not None

    def test_from_db_row_no_dates(self):
        row = {"id": 2, "title": "No dates", "remind_time": None, "status": "completed", "recurring": "none"}
        r = ReminderData.from_db_row(row)
        assert r.scheduled_time is None
        assert r.status == ReminderStatus.COMPLETED


class TestStatusEnum:
    def test_values(self):
        assert ReminderStatus.PENDING.value == "pending"
        assert ReminderStatus.COMPLETED.value == "completed"
        assert ReminderStatus.CANCELLED.value == "cancelled"
        assert ReminderStatus.FIRING.value == "firing"
        assert ReminderStatus.FAILED.value == "failed"
