"""Tests for launching Annie Search from bot commands."""

import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from src.handlers import GoodreadsBot


class TestMiniAppCommandLinks(unittest.TestCase):
    @patch.dict(os.environ, {"ANNIE_APP_URL": "https://books.example/miniapp/?source=telegram"})
    def test_recommendation_url_preserves_existing_query(self):
        url = GoodreadsBot._mini_app_url("recommendations")
        parsed = urlsplit(url)

        self.assertEqual(parsed.path, "/miniapp/")
        self.assertEqual(parse_qs(parsed.query), {"source": ["telegram"], "page": ["recommendations"]})

    @patch.dict(os.environ, {"ANNIE_APP_URL": "http://books.example/miniapp/"})
    def test_rejects_non_https_url(self):
        self.assertIsNone(GoodreadsBot._mini_app_url())

    @patch.dict(os.environ, {"ANNIE_APP_URL": "https://books.example/miniapp/"})
    def test_private_chat_uses_telegram_web_app_button(self):
        update = MagicMock()
        update.effective_chat.type = "private"
        context = MagicMock()

        markup = GoodreadsBot._mini_app_markup(object.__new__(GoodreadsBot), update, context, "recommendations")
        button = markup.inline_keyboard[0][0]

        self.assertEqual(button.web_app.url, "https://books.example/miniapp/?page=recommendations")
        self.assertIsNone(button.url)

    @patch.dict(os.environ, {"ANNIE_APP_URL": "https://books.example/miniapp/"})
    def test_start_menu_has_help_portal_recommendations_and_features(self):
        update = MagicMock()
        update.effective_chat.type = "private"
        context = MagicMock()

        markup = GoodreadsBot._start_keyboard(
            object.__new__(GoodreadsBot), update, context
        )
        buttons = [button for row in markup.inline_keyboard for button in row]

        self.assertEqual(buttons[0].callback_data, "start_help")
        self.assertEqual(buttons[1].callback_data, "start_features")
        self.assertEqual(buttons[2].text, "📚 Annie Search Portal")
        self.assertEqual(buttons[2].web_app.url, "https://books.example/miniapp/")
        self.assertEqual(buttons[3].text, "✨ Annie Recommendations")
        self.assertEqual(
            buttons[3].web_app.url,
            "https://books.example/miniapp/?page=recommendations",
        )

    def test_current_features_list_does_not_advertise_unbuilt_library(self):
        text = GoodreadsBot._features_text()

        self.assertIn("trending picks", text)
        self.assertIn("Get recommendations", text)
        self.assertIn("\n\n🪄 <b>Explore details</b>", text)
        self.assertNotIn("My Library", text)
        self.assertNotIn("Search History", text)

    def test_help_and_features_back_button_returns_to_start(self):
        markup = GoodreadsBot._back_to_start_markup()

        self.assertEqual(markup.inline_keyboard[0][0].text, "← Back to start")
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "start_back")

    def test_help_explains_inline_mini_app_shortcuts(self):
        text = GoodreadsBot._help_text()

        self.assertIn("@AnnieBooks_bot .portal", text)
        self.assertIn("@AnnieBooks_bot .recom", text)
        self.assertIn("launch button above the results", text)

    def test_start_welcome_mentions_inline_mini_app_shortcuts(self):
        text = GoodreadsBot._start_text()

        self.assertIn("@AnnieBooks_bot .portal", text)
        self.assertIn(".recom", text)


class TestStartCommand(unittest.IsolatedAsyncioTestCase):
    async def test_recommendation_deep_link_opens_recommendations_directly(self):
        bot = object.__new__(GoodreadsBot)
        bot._send_mini_app = AsyncMock()
        update = MagicMock()
        context = MagicMock()
        context.args = ["recom"]

        await bot.start(update, context)

        bot._send_mini_app.assert_awaited_once_with(update, context, "recommendations")

    async def test_ping_reports_uptime_mode_and_mini_app_configuration(self):
        bot = object.__new__(GoodreadsBot)
        bot._started_at = 100000 - 90061
        bot.webhook_mode = True
        update = MagicMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        with patch("src.handlers.time.time", return_value=100000), patch.dict(
            os.environ, {"ANNIE_APP_URL": "https://books.example/miniapp/"}
        ):
            await bot.ping_command(update, context)

        text = update.message.reply_text.await_args.args[0]
        self.assertIn("Uptime:</b> 1d 1h 1m 1s", text)
        self.assertIn("Connection:</b> Webhook", text)
        self.assertIn("Mini App URL:</b> Configured", text)

    @patch.dict(os.environ, {"ANNIE_APP_URL": "https://books.example/miniapp/"})
    def test_group_chat_opens_main_mini_app_in_current_chat(self):
        update = MagicMock()
        update.effective_chat.type = "group"
        context = MagicMock()
        context.bot.username = "annie_search_bot"

        markup = GoodreadsBot._mini_app_markup(object.__new__(GoodreadsBot), update, context, "recommendations")
        button = markup.inline_keyboard[0][0]

        self.assertEqual(button.url, "https://t.me/annie_search_bot?startapp=recom")
        self.assertIsNone(button.web_app)

    def test_command_menu_is_registered_for_private_and_group_chats(self):
        bot = object.__new__(GoodreadsBot)
        bot.app = MagicMock()
        bot.app.bot.set_my_commands = AsyncMock()

        import asyncio
        asyncio.run(bot._configure_telegram_commands())

        self.assertEqual(bot.app.bot.set_my_commands.await_count, 3)
        commands = bot.app.bot.set_my_commands.await_args_list[0].args[0]
        command_names = [command.command for command in commands]
        self.assertIn("portal", command_names)
        self.assertIn("recom", command_names)
        self.assertNotIn("annie_app", command_names)


class TestInlineMiniAppLaunch(unittest.IsolatedAsyncioTestCase):
    async def _run_launch(self, query):
        bot = object.__new__(GoodreadsBot)
        bot._active_clarification_restriction = MagicMock(return_value=None)
        update = MagicMock()
        update.inline_query.query = query
        update.inline_query.from_user.id = 123
        update.inline_query.answer = AsyncMock()
        context = MagicMock()

        with patch.dict(os.environ, {"ANNIE_APP_URL": "https://books.example/miniapp/"}):
            await bot.inline_search(update, context)

        update.inline_query.answer.assert_awaited_once()
        self.assertEqual(update.inline_query.answer.await_args.args[0], [])
        return update.inline_query.answer.await_args.kwargs["button"]

    async def test_portal_inline_query_shows_mini_app_button(self):
        button = await self._run_launch(".portal")

        self.assertEqual(button.text, "📚 Open Annie Search Portal")
        self.assertEqual(button.web_app.url, "https://books.example/miniapp/")

    async def test_recom_inline_query_opens_recommendations_page(self):
        button = await self._run_launch(".recom")

        self.assertEqual(button.text, "✨ Open Annie Recommendations")
        self.assertEqual(button.web_app.url, "https://books.example/miniapp/?page=recommendations")


if __name__ == "__main__":
    unittest.main()
