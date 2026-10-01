"""HTTP smoke tests for public Mini App assets and protected API routes."""

import unittest
import threading
from types import SimpleNamespace

from flask import Flask

from src.miniapp import create_miniapp_blueprint
from src.handlers import GoodreadsBot


class MiniAppRouteTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        self.runtime = {}
        app.register_blueprint(create_miniapp_blueprint(self.runtime), url_prefix="/miniapp")
        self.client = app.test_client()

    def test_home_html_preloads_welcome_background(self):
        response = self.client.get("/miniapp/")
        self.addCleanup(response.close)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"/miniapp/assets/images/welcome.jpg", response.data)
        self.assertIn(b"/miniapp/assets/app.js", response.data)

    def test_welcome_background_is_served(self):
        response = self.client.get("/miniapp/assets/images/welcome.jpg")
        self.addCleanup(response.close)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.mimetype.startswith("image/"))
        self.assertGreater(len(response.data), 100_000)
        self.assertIn("max-age=86400", response.headers.get("Cache-Control", ""))

    def test_session_api_rejects_missing_telegram_auth(self):
        response = self.client.get("/miniapp/api/session")
        self.addCleanup(response.close)

        self.assertEqual(response.status_code, 401)

    def test_inline_ticket_exchange_authenticates_followup_api_calls(self):
        bot = object.__new__(GoodreadsBot)
        bot._inline_app_auth_lock = threading.Lock()
        bot._inline_app_tickets = {}
        bot._inline_app_sessions = {}
        bot._INLINE_APP_TICKET_TTL = 120
        bot._INLINE_APP_SESSION_TTL = 3600
        ticket = bot._issue_inline_app_ticket(
            SimpleNamespace(id=987, first_name="Nero", language_code="en")
        )
        self.runtime["bot"] = bot

        exchange = self.client.post(
            "/miniapp/api/inline-session", json={"ticket": ticket}
        )
        self.assertEqual(exchange.status_code, 200)
        payload = exchange.get_json()
        self.assertEqual(payload["user"]["first_name"], "Nero")

        session = self.client.get(
            "/miniapp/api/session",
            headers={"X-Annie-Inline-Session": payload["session_token"]},
        )
        self.assertEqual(session.status_code, 200)
        self.assertEqual(session.get_json()["user"]["language_code"], "en")

        replay = self.client.post(
            "/miniapp/api/inline-session", json={"ticket": ticket}
        )
        self.assertEqual(replay.status_code, 401)


if __name__ == "__main__":
    unittest.main()
