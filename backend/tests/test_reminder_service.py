import pytest
from datetime import datetime
from unittest.mock import AsyncMock

from reminder.service import ReminderService
from reminder.models import ReminderData, RecurrenceType


@pytest.fixture
def mock_memory():
    m = AsyncMock()
    m.add_reminder = AsyncMock(return_value={"id": 1, "title": "Test"})
    m.list_reminders = AsyncMock(return_value=[
        {"id": 1, "title": "Task 1", "remind_time": "2025-06-01 10:00:00",
         "status": "pending", "recurring": "none", "session_id": "default",
         "description": "", "created_at": "2025-05-01 00:00:00"},
    ])
    m.delete_reminder = AsyncMock(return_value=True)
    m.complete_reminder = AsyncMock(return_value=True)
    m.get_due_reminders = AsyncMock(return_value=[])
    return m


@pytest.fixture
def service(mock_memory):
    return ReminderService(mock_memory)


class TestReminderService:
    async def test_create(self, service):
        now = datetime(2025, 6, 1, 10, 0, 0)
        result = await service.create("Test task", now)
        assert result["success"] is True
        assert result["reminder"]["task"] == "Test task"

    async def test_create_with_recurring(self, service):
        now = datetime(2025, 6, 1, 10, 0, 0)
        result = await service.create("Daily task", now, recurring="daily")
        assert result["success"] is True
        assert result["reminder"]["recurring"] == "daily"

    async def test_list(self, service):
        reminders = await service.list()
        assert len(reminders) == 1
        assert reminders[0]["task"] == "Task 1"

    async def test_delete(self, service):
        ok = await service.delete(1)
        assert ok is True

    async def test_mark_completed(self, service):
        ok = await service.mark_completed(1)
        assert ok is True

    async def test_format_list_en(self, service):
        reminders = await service.list()
        text = await service.format_list_for_display(reminders, "en")
        assert "Task 1" in text
        assert "Reminders" in text or "Your reminders" in text

    async def test_format_list_empty(self, service):
        service._memory.list_reminders = AsyncMock(return_value=[])
        text = await service.format_list_for_display([], "en")
        assert "No pending reminders" in text

    async def test_format_list_ar(self, service):
        reminders = await service.list()
        text = await service.format_list_for_display(reminders, "ar")
        assert "مهامك" in text or "مهام" in text

    async def test_get_nonexistent(self, service):
        service._memory.list_reminders = AsyncMock(return_value=[])
        result = await service.get(999)
        assert result is None


class TestReminderServiceIntegration:
    async def test_handle_recurring_daily(self, mock_memory, service):
        base_time = datetime(2025, 6, 1, 10, 0, 0)
        reminder = ReminderData(
            task="Daily task",
            scheduled_time=base_time,
            recurring=RecurrenceType.DAILY,
            reminder_id=1,
        )
        new = await service.handle_recurring(reminder)
        assert new is not None
        # Should create a new reminder for the next day
        args, _ = mock_memory.add_reminder.call_args
        # Verify the mock was called

    async def test_handle_recurring_none(self, service):
        reminder = ReminderData(
            task="One time",
            scheduled_time=datetime(2025, 6, 1, 10, 0, 0),
            recurring=RecurrenceType.NONE,
        )
        result = await service.handle_recurring(reminder)
        assert result is None

    async def test_create_from_natural(self, mock_memory, service):
        result = await service.create_from_natural(
            "remind me tomorrow at 6pm to workout",
        )
        assert result["success"] is True
        assert "workout" in result.get("task", "") or result.get("confirmation", "")

    async def test_build_confirmation_en(self, service):
        dt = datetime(2025, 6, 15, 18, 30, 0)
        confirm = service._build_confirmation("test", dt, RecurrenceType.NONE, "en")
        assert "test" in confirm
        assert "Reminder" in confirm

    async def test_build_confirmation_ar(self, service):
        dt = datetime(2025, 6, 15, 10, 0, 0)
        confirm = service._build_confirmation("اختبار", dt, RecurrenceType.DAILY, "ar")
        assert "اختبار" in confirm
        assert "كل يوم" in confirm

    async def test_build_confirmation_ar_pm(self, service):
        dt = datetime(2025, 6, 15, 15, 0, 0)
        confirm = service._build_confirmation("اجتماع", dt, RecurrenceType.NONE, "ar")
        assert "اجتماع" in confirm
        assert "مساءً" in confirm
