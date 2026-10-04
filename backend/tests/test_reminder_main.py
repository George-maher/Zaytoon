"""
Sanity check: verify all new modules can be imported without errors.
"""


def test_import_reminder_models():
    from reminder.models import ReminderData, ReminderStatus, RecurrenceType, detect_recurrence
    assert ReminderStatus.PENDING.value == "pending"


def test_import_reminder_service():
    from reminder.service import ReminderService
    # Can't instantiate without mock, but import should work


def test_import_reminder_notification():
    from reminder.notification import NotificationManager
    mgr = NotificationManager()
    assert mgr is not None


def test_import_enhanced_parser():
    from scheduler.parser import TimeParser
    assert hasattr(TimeParser, "extract_task")


def test_import_enhanced_scheduler():
    from reminder.service import ReminderService
    from reminder.notification import NotificationManager
    from scheduler.manager import SchedulerManager
    # Imports should succeed
