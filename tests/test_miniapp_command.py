"""Tests for launching Annie Search from bot commands."""

import os
import unittest
from unittest.mock import MagicMock, patch
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
    def test_group_chat_links_to_private_bot_start(self):
        update = MagicMock()
        update.effective_chat.type = "group"
        context = MagicMock()
        context.bot.username = "annie_search_bot"

        markup = GoodreadsBot._mini_app_markup(object.__new__(GoodreadsBot), update, context, "recommendations")
        button = markup.inline_keyboard[0][0]

        self.assertEqual(button.url, "https://t.me/annie_search_bot?start=annie_recommend")
        self.assertIsNone(button.web_app)


if __name__ == "__main__":
    unittest.main()
