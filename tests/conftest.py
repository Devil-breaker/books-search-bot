"""Shared test fixtures."""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from unittest.mock import MagicMock, AsyncMock

# Patch environment before any module imports that read env vars
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "TEST_TOKEN")
os.environ.setdefault("HARDCOVER_API_KEY", "TEST_KEY")
os.environ.setdefault("GOOGLE_BOOKS_API_KEY", "TEST_GB_KEY")


# ── Sample book dicts ────────────────────────────────────────────────────────────

def make_book(
    title="The Great Gatsby",
    author="F. Scott Fitzgerald",
    rating=4.2,
    rating_count=5000,
    rating_formatted=None,
    search_rating=None,
    search_rating_count=None,
    search_rating_formatted=None,
    **kwargs,
):
    """Return a minimally-complete book dict for testing."""
    defaults = dict(
        title=title,
        author=author,
        isbn="9780743273565",
        page_count=180,
        published_date="1925-04-10",
        description="A novel about the American dream.",
        cover_url="https://example.com/cover.jpg",
        source="google_books",
    )
    defaults.update(kwargs)
    if "rating" not in defaults:
        defaults["rating"] = rating
    if "rating_count" not in defaults:
        defaults["rating_count"] = rating_count
    if "rating_formatted" not in defaults:
        defaults["rating_formatted"] = rating_formatted or (f"{rating:.2f}" if rating else "N/A")

    # List-only search fields — mirrors what aggregator sets before lazy reset
    if search_rating is not None:
        defaults["search_rating"] = search_rating
    if search_rating_count is not None:
        defaults["search_rating_count"] = search_rating_count
    if search_rating_formatted is not None:
        defaults["search_rating_formatted"] = search_rating_formatted

    return defaults


def make_bot():
    """Return an uninitialised GoodreadsBot (skips network handlers)."""
    from src.handlers import GoodreadsBot
    bot = object.__new__(GoodreadsBot)
    # Replicate only the state that __init__ sets
    bot.token = "TEST_TOKEN"
    bot.webhook_mode = True
    bot.search_cache = {}
    bot._SEARCH_CACHE_TTL = 60 * 60
    bot._SEARCH_CACHE_MAX = 1000
    bot._search_page_cache = {}
    bot._search_query_cache = {}
    bot._inline_callback_cache = {}
    bot._INLINE_CALLBACK_CACHE_TTL = 30 * 60
    bot.aggregator = MagicMock()
    # New attributes added by recent changes
    bot._owner_user_id = None
    bot._clarification = {}
    bot._clarification_rate_limit = {}
    bot._clarification_cancel_abuse = {}
    bot._clarification_abuse_notice_rate_limit = {}
    bot._clarification_cancel_notice_rate_limit = {}
    bot._cached_books = {}
    bot._cached_users = {}
    bot._set_cached_books = MagicMock()
    bot._aggregate_search_cache = {}
    bot._AGGREGATE_SEARCH_CACHE_TTL = 120
    bot._AGGREGATE_SEARCH_CACHE_MAX = 128
    bot._aggregate_search_inflight = {}
    return bot
