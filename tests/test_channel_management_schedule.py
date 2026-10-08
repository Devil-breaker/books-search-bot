from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.channel_management import ChannelManager


class TestChannelManagerScheduleCalendar(unittest.IsolatedAsyncioTestCase):
    def _manager(self) -> ChannelManager:
        manager = ChannelManager(None, lambda: None, AsyncMock())
        manager._record_for_user = AsyncMock(return_value=(
            {"_id": "draft-1", "channel_id": -1001, "status": "draft"},
            {"id": -1001},
        ))
        manager._edit_or_send = AsyncMock()
        return manager

    async def test_calendar_has_date_buttons_and_callback_data_within_telegram_limit(self):
        manager = self._manager()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=123))

        await manager._show_schedule_calendar(update, SimpleNamespace(bot=Mock()), "draft-1", 2026, 10)

        markup = manager._edit_or_send.await_args.args[2]
        buttons = [button for row in markup.inline_keyboard for button in row]
        callbacks = [button.callback_data for button in buttons]
        self.assertIn("cm:sch_date:draft-1:2026-10-01", callbacks)
        self.assertTrue(all(len(callback.encode("utf-8")) <= 64 for callback in callbacks))

    async def test_timezone_menu_callback_data_stays_within_telegram_limit(self):
        manager = self._manager()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=123))

        await manager._show_schedule_timezones(
            update, SimpleNamespace(bot=Mock()), "0123456789abcdef01234567", "2026-10-10 19:30"
        )

        markup = manager._edit_or_send.await_args.args[2]
        callbacks = [
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
        ]
        self.assertTrue(all(len(callback.encode("utf-8")) <= 64 for callback in callbacks))
        self.assertIn(
            "cm:sch_tz:0123456789abcdef01234567:202610101930:America/New_York",
            callbacks,
        )

    async def test_selected_timezone_is_saved_as_utc_before_confirmation(self):
        manager = self._manager()
        repository = Mock()
        repository.update.return_value = True
        manager._get_repository = Mock(return_value=repository)
        future_local = (datetime.now(timezone.utc) + timedelta(days=2)).replace(
            second=0, microsecond=0
        ).strftime("%Y-%m-%d %H:%M")
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=123),
            callback_query=None,
            effective_message=SimpleNamespace(reply_text=AsyncMock()),
        )

        with patch("src.channel_management.ZoneInfo", return_value=timezone.utc):
            await manager._set_pending_schedule(
                update, SimpleNamespace(bot=Mock()), "draft-1", future_local, "UTC"
            )

        self.assertIsNotNone(
            repository.update.call_args,
            f"record={manager._record_for_user.await_args}; message={manager._edit_or_send.await_args}",
        )
        changes = repository.update.call_args.args[1]
        self.assertEqual(changes["pending_timezone"], "UTC")
        self.assertIsInstance(changes["pending_scheduled_at"], datetime)
        self.assertEqual(changes["pending_scheduled_at"].tzinfo, timezone.utc)
        manager._edit_or_send.assert_awaited()


if __name__ == "__main__":
    unittest.main()
