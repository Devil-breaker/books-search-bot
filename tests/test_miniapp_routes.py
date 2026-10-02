"""HTTP smoke tests for public Mini App assets and protected API routes."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from src.miniapp import create_miniapp_blueprint


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

    def test_inline_ticket_exchange_works_without_shared_process_state(self):
        from src.miniapp.auth import issue_inline_token
        ticket = issue_inline_token(
            {"id": 987, "first_name": "Nero", "language_code": "en"},
            "test-bot-token", purpose="ticket", ttl_seconds=900,
        )
        self.runtime["bot"] = SimpleNamespace(token="test-bot-token")
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "different-env-token"}):
            exchange = self.client.post(
                "/miniapp/api/inline-session", json={"ticket": ticket}
            )
        self.assertEqual(exchange.status_code, 200)
        payload = exchange.get_json()
        self.assertEqual(payload["user"]["first_name"], "Nero")

        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "different-env-token"}):
            session = self.client.get(
                "/miniapp/api/session",
                headers={"X-Annie-Inline-Session": payload["session_token"]},
            )
        self.assertEqual(session.status_code, 200)
        self.assertEqual(session.get_json()["user"]["language_code"], "en")

        other_app = Flask(__name__)
        other_runtime = {"bot": SimpleNamespace(token="test-bot-token")}
        other_app.register_blueprint(create_miniapp_blueprint(other_runtime), url_prefix="/miniapp")
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "different-env-token"}):
            session = other_app.test_client().get(
                "/miniapp/api/session",
                headers={"X-Annie-Inline-Session": payload["session_token"]},
            )
        self.assertEqual(session.status_code, 200)


if __name__ == "__main__":
    unittest.main()
