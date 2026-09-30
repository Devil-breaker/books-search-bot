"""HTTP smoke tests for public Mini App assets and protected API routes."""

import unittest

from flask import Flask

from src.miniapp import create_miniapp_blueprint


class MiniAppRouteTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(create_miniapp_blueprint({}), url_prefix="/miniapp")
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


if __name__ == "__main__":
    unittest.main()
