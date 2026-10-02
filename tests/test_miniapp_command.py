"""Tests for launching Annie Search from bot commands."""

import os
import threading
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
    def test_private_chat_uses_direct_web_app_button(self):
        update = MagicMock()
        update.effective_chat.type = "private"
        context = MagicMock()
        context.bot.username = "annie_search_bot"

        markup = GoodreadsBot._mini_app_markup(
            object.__new__(GoodreadsBot), update, context, "recommendations"
        )
        button = markup.inline_keyboard[0][0]

        self.assertIsNone(button.url)
        self.assertEqual(
            button.web_app.url,
            "https://books.example/miniapp/?page=recommendations",
        )

    @patch.dict(os.environ, {"ANNIE_APP_URL": "https://books.example/miniapp/"})
    def test_start_menu_has_help_portal_recommendations_bookshelf_and_favorites(self):
        update = MagicMock()
        update.effective_chat.type = "private"
        context = MagicMock()
        context.bot.username = "annie_search_bot"

        markup = GoodreadsBot._start_keyboard(object.__new__(GoodreadsBot), update, context)
        buttons = [button for row in markup.inline_keyboard for button in row]

        self.assertEqual(buttons[0].callback_data, "start_help")
        self.assertEqual(buttons[1].callback_data, "start_features")
        self.assertEqual(buttons[2].text, "🔎 Annie Search Portal")
        self.assertEqual(buttons[2].web_app.url, "https://books.example/miniapp/")
        self.assertEqual(buttons[3].text, "✨ Annie Recommendations")
        self.assertEqual(buttons[3].web_app.url, "https://books.example/miniapp/?page=recommendations")
        self.assertEqual(buttons[4].text, "📚 My Bookshelf")
        self.assertEqual(buttons[4].web_app.url, "https://books.example/miniapp/?page=bookshelf")
        self.assertEqual(buttons[5].text, "♥ Favourites")
        self.assertEqual(buttons[5].web_app.url, "https://books.example/miniapp/?page=favorites")
        self.assertNotEqual(buttons[2].text.split()[0], buttons[4].text.split()[0])

    def test_features_list_describes_bookshelf(self):
        text = GoodreadsBot._features_text()

        self.assertIn("trending picks", text)
        self.assertIn("Get recommendations", text)
        self.assertIn("\n\n🪄 <b>Explore details</b>", text)
        self.assertIn("Build your Bookshelf", text)
        self.assertIn("grid or list view", text)
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
        self.assertIn("Bookshelf", GoodreadsBot._help_text())
        self.assertIn("/favorites", GoodreadsBot._help_text())


class TestStartCommand(unittest.IsolatedAsyncioTestCase):
    async def test_recommendation_deep_link_opens_recommendations_directly(self):
        bot = object.__new__(GoodreadsBot)
        bot._send_mini_app = AsyncMock()
        update = MagicMock()
        context = MagicMock()
        context.args = ["recom"]

        await bot.start(update, context)

        bot._send_mini_app.assert_awaited_once_with(update, context, "recommendations")

    async def test_bookshelf_and_favorites_deep_links_open_directly(self):
        for parameter, page in (("bookshelf", "bookshelf"), ("favorites", "favorites")):
            with self.subTest(parameter=parameter):
                bot = object.__new__(GoodreadsBot)
                bot._send_mini_app = AsyncMock()
                update = MagicMock()
                context = MagicMock()
                context.args = [parameter]

                await bot.start(update, context)

                if page:
                    bot._send_mini_app.assert_awaited_once_with(update, context, page)
                else:
                    bot._send_mini_app.assert_awaited_once_with(update, context)

    async def test_page_commands_open_the_expected_page(self):
        for method_name, page in (
            ("portal_command", ""),
            ("recom_command", "recommendations"),
            ("bookshelf_command", "bookshelf"),
            ("favorites_command", "favorites"),
        ):
            with self.subTest(command=method_name):
                bot = object.__new__(GoodreadsBot)
                bot._send_mini_app = AsyncMock()
                update = MagicMock()
                context = MagicMock()

                await getattr(bot, method_name)(update, context)

                if page:
                    bot._send_mini_app.assert_awaited_once_with(update, context, page)
                else:
                    bot._send_mini_app.assert_awaited_once_with(update, context)

    async def test_retired_startapp_alias_does_not_launch_a_page(self):
        bot = object.__new__(GoodreadsBot)
        bot._send_mini_app = AsyncMock()
        bot._start_text = MagicMock(return_value="welcome")
        bot._start_keyboard = MagicMock(return_value="keyboard")
        update = MagicMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = ["annie_app"]

        await bot.start(update, context)

        bot._send_mini_app.assert_not_awaited()
        update.message.reply_text.assert_awaited_once()

    async def test_ping_reports_uptime_mode_and_mini_app_configuration(self):
        bot = object.__new__(GoodreadsBot)
        bot._started_at = 100000 - 90061
        bot.webhook_mode = True
        update = MagicMock()
        update.message.reply_text = AsyncMock()
        pong_message = MagicMock()
        pong_message.edit_text = AsyncMock()
        update.message.reply_text.return_value = pong_message
        context = MagicMock()

        with patch("src.handlers.time.time", return_value=100000), patch.dict(
            os.environ, {"ANNIE_APP_URL": "https://books.example/miniapp/"}
        ):
            await bot.ping_command(update, context)

        text = pong_message.edit_text.await_args.args[0]
        self.assertIn("Pong:", text)
        self.assertRegex(text, r"Pong: \d+ ms")
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

    @patch.dict(os.environ, {"ANNIE_APP_URL": "https://books.example/miniapp/"})
    def test_group_chat_bookshelf_button_targets_bookshelf_startapp(self):
        update = MagicMock()
        update.effective_chat.type = "group"
        context = MagicMock()
        context.bot.username = "annie_search_bot"

        markup = GoodreadsBot._mini_app_markup(object.__new__(GoodreadsBot), update, context, "bookshelf")

        self.assertEqual(markup.inline_keyboard[0][0].url,
                         "https://t.me/annie_search_bot?startapp=bookshelf")

    @patch.dict(os.environ, {"ANNIE_APP_URL": "https://books.example/miniapp/"})
    def test_group_chat_favorites_button_targets_favorites_startapp(self):
        update = MagicMock()
        update.effective_chat.type = "group"
        context = MagicMock()
        context.bot.username = "annie_search_bot"

        markup = GoodreadsBot._mini_app_markup(object.__new__(GoodreadsBot), update, context, "favorites")

        self.assertEqual(markup.inline_keyboard[0][0].url,
                         "https://t.me/annie_search_bot?startapp=favorites")

    @patch.dict(os.environ, {"ANNIE_APP_URL": "https://books.example/miniapp/"})
    def test_group_start_menu_buttons_open_all_pages(self):
        update = MagicMock()
        update.effective_chat.type = "group"
        context = MagicMock()
        context.bot.username = "annie_search_bot"

        markup = GoodreadsBot._start_keyboard(object.__new__(GoodreadsBot), update, context)
        buttons = [button for row in markup.inline_keyboard for button in row]
        page_buttons = buttons[2:]

        self.assertEqual(
            [button.url for button in page_buttons],
            [
                "https://t.me/annie_search_bot?startapp=portal",
                "https://t.me/annie_search_bot?startapp=recom",
                "https://t.me/annie_search_bot?startapp=bookshelf",
                "https://t.me/annie_search_bot?startapp=favorites",
            ],
        )

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
        self.assertIn("bookshelf", command_names)
        self.assertIn("favorites", command_names)
        self.assertNotIn("annie_recommendation", command_names)

    def test_old_command_names_are_not_registered_as_aliases(self):
        bot = object.__new__(GoodreadsBot)
        bot.app = MagicMock()

        bot.setup_handlers()

        command_handlers = [
            call.args[0].commands
            for call in bot.app.add_handler.call_args_list
            if call.args and hasattr(call.args[0], "commands")
        ]
        registered = {name for names in command_handlers for name in names}
        self.assertTrue({"portal", "recom", "bookshelf", "favorites"}.issubset(registered))
        self.assertTrue({"annie_app", "annie_recommend", "annie_recommendation"}.isdisjoint(registered))


class TestInlineMiniAppLaunch(unittest.IsolatedAsyncioTestCase):
    async def _run_launch(self, query):
        bot = object.__new__(GoodreadsBot)
        bot.token = "test-bot-token"
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
        parsed = urlsplit(button.web_app.url)
        self.assertEqual(parsed.path, "/miniapp/")
        self.assertTrue(parse_qs(parsed.query)["inline_ticket"][0])

    async def test_recom_inline_query_opens_recommendations_page(self):
        button = await self._run_launch(".recom")

        self.assertEqual(button.text, "✨ Open Annie Recommendations")
        parsed = urlsplit(button.web_app.url)
        self.assertEqual(parse_qs(parsed.query)["page"], ["recommendations"])
        self.assertTrue(parse_qs(parsed.query)["inline_ticket"][0])

    async def test_partial_launch_shortcut_does_not_search_for_books(self):
        bot = object.__new__(GoodreadsBot)
        bot._active_clarification_restriction = MagicMock(return_value=None)
        update = MagicMock()
        update.inline_query.query = ".port"
        update.inline_query.from_user.id = 123
        update.inline_query.answer = AsyncMock()

        await bot.inline_search(update, MagicMock())

        update.inline_query.answer.assert_awaited_once_with(
            [], cache_time=0, is_personal=True
        )

    async def test_portal_launch_is_not_hidden_by_search_cooldown(self):
        bot = object.__new__(GoodreadsBot)
        bot.token = "test-bot-token"
        bot._active_clarification_restriction = MagicMock(return_value=("cooldown", 60))
        bot._load_bot_admin_ids = AsyncMock()
        update = MagicMock()
        update.inline_query.query = ".portal"
        update.inline_query.from_user.id = 123
        update.inline_query.answer = AsyncMock()

        with patch.dict(os.environ, {"ANNIE_APP_URL": "https://books.example/miniapp/"}):
            await bot.inline_search(update, MagicMock())

        kwargs = update.inline_query.answer.await_args.kwargs
        self.assertIsNotNone(kwargs["button"].web_app)
        bot._active_clarification_restriction.assert_not_called()

    def test_inline_launch_ticket_is_signed_and_verifiable_without_bot_state(self):
        bot = object.__new__(GoodreadsBot)
        bot.token = "test-bot-token"
        telegram_user = MagicMock(
            id=321, first_name="Nero", language_code="en"
        )

        ticket = bot._issue_inline_app_ticket(telegram_user)
        from src.miniapp.auth import validate_inline_token
        user = validate_inline_token(
            ticket, "test-bot-token", purpose="ticket", max_lifetime=900
        )
        self.assertEqual(user["id"], 321)


if __name__ == "__main__":
    unittest.main()
