"""Entry point for the Multi-Source Book Bot (polling mode)."""

import logging
import os
import re
import socket
import threading
import time

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN in .env file")

# ── Suppress noisy library HTTP logging ───────────────────────────────────────
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# ── Token redaction filter ────────────────────────────────────────────────────
# Matches Telegram bot tokens: 123456789:AAHaaa...  (8+ digits + colon + 35+ chars)
_TOKEN_RE = re.compile(r'\b(\d{8,11}:[\w-]{30,45})\b')


class _TokenRedactingFilter(logging.Filter):
    """Scrubs Telegram bot tokens from every log record.message()."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _TOKEN_RE.sub("[TELEGRAM_BOT_TOKEN_REDACTED]", record.msg)
        return True


logging.root.addFilter(_TokenRedactingFilter())

from src.handlers import GoodreadsBot  # noqa: E402, F401

# ── Tiny web server (keep-alive) ─────────────────────────────────────────────
# Koyeb's free tier sleeps an instance after ~30 min of no traffic. A Telegram
# polling bot only outbound-polls, so it generates no traffic and would go to
# sleep. We expose a trivial /ping route and let UptimeRobot (or any external
# cron) hit it every ~20 min to keep the instance awake.

PORT = int(os.getenv("PORT", "8080"))
_MINIAPP_RUNTIME = {}


def _run_webserver():
    from flask import Flask, Response
    from src.miniapp import create_miniapp_blueprint

    app = Flask(__name__)
    app.register_blueprint(
        create_miniapp_blueprint(_MINIAPP_RUNTIME), url_prefix="/miniapp"
    )

    @app.route("/ping")
    def ping():
        return Response("ok", status=200, mimetype="text/plain")

    @app.route("/health")
    def health():
        return Response("ok", status=200, mimetype="text/plain")

    # TLS isn't needed on Koyeb (it terminates at the edge). Run plain HTTP.
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


def _start_webserver():
    """Launch the Flask keep-alive server on a background daemon thread."""
    t = threading.Thread(target=_run_webserver, daemon=True, name="webserver")
    t.start()
    # Confirm the port actually bound so we fail fast if it's taken.
    for _ in range(25):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.connect(("127.0.0.1", PORT))
            s.close()
            break
        except OSError:
            s.close()
            time.sleep(0.2)


bot = GoodreadsBot(TELEGRAM_BOT_TOKEN)
_MINIAPP_RUNTIME["bot"] = bot
_start_webserver()
bot.run()
