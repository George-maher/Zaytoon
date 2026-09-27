import pytest
from unittest.mock import AsyncMock
from reminder.notification import NotificationManager


@pytest.fixture
def mgr():
    return NotificationManager()


class TestNotificationManager:
    async def test_console_fallback_when_no_channels(self, mgr):
        """With no broadcast callback and no webhook, the console channel is
        always available, so a reminder is never silently dropped.

        (This test used to assert the opposite -- "no channels -> False" --
        but that predates the console fallback, which test_broadcast_failure
        already relies on.)
        """
        result = await mgr.notify({"id": 1, "title": "Test", "message": "Test"})
        assert result is True

    async def test_returns_false_when_every_channel_fails(self, mgr):
        callback = AsyncMock(side_effect=Exception("broadcast down"))
        mgr.set_broadcast_callback(callback)
        mgr.set_webhook("http://127.0.0.1:1/unreachable")
        # Make the console fallback fail too.
        mgr._send_console = AsyncMock(side_effect=Exception("console down"))

        result = await mgr.notify({"id": 1, "title": "Test", "message": "Test"})
        assert result is False

    async def test_broadcast_channel(self, mgr):
        callback = AsyncMock(return_value=None)
        mgr.set_broadcast_callback(callback)
        result = await mgr.notify({"id": 1, "title": "Test", "message": "Test"})
        assert result is True
        callback.assert_called_once()

    async def test_broadcast_failure(self, mgr):
        callback = AsyncMock(side_effect=Exception("Fail"))
        mgr.set_broadcast_callback(callback)
        # Console fallback should still succeed
        result = await mgr.notify({"id": 1, "title": "Test", "message": "Test"})
        assert result is True

    async def test_reminder_payload(self, mgr):
        callback = AsyncMock()
        mgr.set_broadcast_callback(callback)
        await mgr.notify({
            "id": 42,
            "title": "Meeting",
            "message": "Time for meeting",
        })
        payload = callback.call_args[0][0]
        assert payload["type"] == "reminder"
        assert payload["id"] == 42
        assert "Meeting" in payload["message"]
