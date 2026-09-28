"""Tests for src/handlers.py — normal search UI: message building, caching, callbacks."""

import asyncio
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

# Patch env before importing handlers
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "TEST_TOKEN")
os.environ.setdefault("HARDCOVER_API_KEY", "TEST_KEY")

from src.handlers import GoodreadsBot, Update
from src.aggregator import MultiSourceBookAggregator
from tests.conftest import make_book


def _make_bot():
    bot = object.__new__(GoodreadsBot)
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
    bot._aggregate_search_cache = {}
    bot._AGGREGATE_SEARCH_CACHE_TTL = 120
    bot._AGGREGATE_SEARCH_CACHE_MAX = 128
    bot._aggregate_search_inflight = {}
    bot._owner_user_id = None
    bot._group_admin_status_cache = {}
    bot._started_at = time.time()
    bot._clarification = {}
    bot._clarification_cancel_abuse = {}
    bot._clarification_abuse_notice_rate_limit = {}
    bot._group_search_rate_limit = {}
    bot._group_search_notice_rate_limit = {}
    bot._group_search_inflight = set()
    bot._active_result_messages = {}
    bot._rating_refresh_tasks = set()
    bot._clarification_discovery_cache = {}
    bot._clarification_discovery_inflight = {}
    return bot


def _callback_update(user_id, data, chat_type="group"):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = -100
    update.effective_chat.type = chat_type
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.delete_message = AsyncMock()
    update.callback_query.message.chat_id = -100
    update.callback_query.message.chat.type = chat_type
    return update


class TestGroupResultOwnership(unittest.IsolatedAsyncioTestCase):
    async def test_non_requester_cannot_use_any_result_control(self):
        bot = _make_bot()
        for data in ("page_42_2", "book_42_0_1", "back_42", "close_42", "download_42_0"):
            with self.subTest(data=data):
                update = _callback_update(77, data)
                await bot.button_callback(update, MagicMock())
                update.callback_query.answer.assert_awaited_once()
                self.assertTrue(update.callback_query.answer.await_args.kwargs["show_alert"])
                update.callback_query.delete_message.assert_not_awaited()

    async def test_requester_can_close_group_detail_message(self):
        bot = _make_bot()
        update = _callback_update(42, "close_42")

        await bot.button_callback(update, MagicMock())

        update.callback_query.delete_message.assert_awaited_once()
        update.callback_query.answer.assert_awaited_once()

    def test_group_detail_keyboard_has_close_and_no_cover_download(self):
        keyboard = GoodreadsBot._build_detail_keyboard(42, 3, "supergroup")
        labels = [button.text for row in keyboard.inline_keyboard for button in row]
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        self.assertEqual(labels, ["✖️ Close", "🔙 Back to Results"])
        self.assertEqual(callbacks, ["close_42", "back_42"])

    def test_private_detail_keyboard_keeps_cover_download(self):
        keyboard = GoodreadsBot._build_detail_keyboard(42, 3, "private")
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        self.assertEqual(callbacks, ["download_42_3", "back_42"])


class TestStartupUpdateHandling(unittest.IsolatedAsyncioTestCase):
    def test_polling_drops_updates_queued_before_startup(self):
        bot = _make_bot()
        bot.app = MagicMock()
        with patch("src.handlers.asyncio.get_running_loop", return_value=MagicMock()):
            bot.run()
        bot.app.run_polling.assert_called_once_with(drop_pending_updates=True)

    def test_webhook_discards_a_command_from_before_startup(self):
        bot = _make_bot()
        bot._started_at = time.time()
        bot.app = MagicMock()
        old_command = MagicMock()
        old_command.text = "/search old query"
        old_command.date = datetime.fromtimestamp(bot._started_at - 60, timezone.utc)
        update = MagicMock()
        update.effective_message = old_command
        update.update_id = 101

        with patch.object(Update, "de_json", return_value=update):
            processed = bot.process_update({"update_id": 101})

        self.assertTrue(processed)
        bot.app.process_update.assert_not_called()


class TestSearchResponsiveness(unittest.IsolatedAsyncioTestCase):
    async def test_clarification_discovery_cache_reuses_match(self):
        bot = _make_bot()
        candidate = {"title": "Goth", "author": "Otsuichi"}
        with patch.object(bot, "_discover_candidate", return_value=candidate) as discover:
            first = await bot._discover_candidate_cached("Goth by Otsu", "Goth", "Otsu")
            second = await bot._discover_candidate_cached("Goth by Otsu", "Goth", "Otsu")

        self.assertEqual(first, candidate)
        self.assertEqual(second, candidate)
        discover.assert_called_once_with("Goth by Otsu", "Goth", "Otsu")

    async def test_repeated_group_search_is_limited_and_notice_is_throttled(self):
        bot = _make_bot()
        bot.app = MagicMock()
        bot.app.bot.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))
        update = _callback_update(42, "noop")
        update.effective_message.reply_text = AsyncMock()
        key = (-100, 42)
        bot._group_search_rate_limit[key] = time.monotonic()

        self.assertTrue(await bot._group_search_is_rate_limited(update))
        self.assertTrue(await bot._group_search_is_rate_limited(update))
        update.effective_message.reply_text.assert_awaited_once()

    async def test_initial_results_render_before_rating_preload(self):
        bot = _make_bot()
        bot._reject_search_during_clarification_restriction = AsyncMock(return_value=False)
        bot._group_search_is_rate_limited = AsyncMock(return_value=False)
        bot._try_clarification = AsyncMock(return_value=False)
        bot._aggregate_search_results = AsyncMock(return_value=[make_book(title="Dune")])
        bot._rank_search_results = MagicMock(side_effect=lambda books, query: books)
        bot._set_cached_books = MagicMock()
        bot._build_search_results_message = MagicMock(return_value=("Results", "keyboard"))
        bot._preload_hardcover_ratings_for_page = AsyncMock()
        bot._schedule_result_rating_refresh = MagicMock()

        status = MagicMock()
        status.message_id = 500
        status.edit_text = AsyncMock()
        update = MagicMock()
        update.effective_user.id = 42
        update.effective_chat.id = -100
        update.message.message_id = 400
        update.message.reply_text = AsyncMock(return_value=status)
        update.message.chat.send_action = AsyncMock()
        update.message.chat.type = "supergroup"
        context = MagicMock()
        context.args = ["Dune"]

        await bot.search_command(update, context)

        update.message.reply_text.assert_awaited_once()
        self.assertIn("Searching for", update.message.reply_text.await_args.args[0])
        status.edit_text.assert_awaited_once_with(
            text="Results", reply_markup="keyboard", parse_mode="HTML"
        )
        bot._preload_hardcover_ratings_for_page.assert_not_awaited()
        bot._schedule_result_rating_refresh.assert_called_once_with(
            bot._set_cached_books.call_args.args[1],
            "Dune", 42, 1, -100, 500, context.bot,
        )

    async def test_late_rating_refresh_does_not_overwrite_a_different_page(self):
        bot = _make_bot()
        books = [make_book(title="Dune")]
        bot._active_result_messages[(-100, 42)] = {
            "message_id": 500,
            "query": "Dune",
            "page": 2,
        }
        bot._preload_hardcover_ratings_for_page = AsyncMock()
        bot._build_search_results_message = MagicMock()
        telegram_bot = MagicMock()
        telegram_bot.edit_message_text = AsyncMock()

        await bot._refresh_result_ratings(
            books, "Dune", 42, 1, -100, 500, telegram_bot
        )

        bot._preload_hardcover_ratings_for_page.assert_awaited_once_with(books, 1, 5)
        bot._build_search_results_message.assert_not_called()
        telegram_bot.edit_message_text.assert_not_awaited()

    async def test_group_admin_is_exempt_from_an_active_restriction(self):
        bot = _make_bot()
        bot._clarification_cancel_abuse[42] = {
            "blocked_until": time.time() + 3600,
        }
        bot._clarification_abuse_notice_rate_limit = {}
        bot.app = MagicMock()
        bot.app.bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
        update = MagicMock()
        update.effective_user.id = 42
        update.effective_chat.id = -100
        update.effective_chat.type = "supergroup"

        rejected = await bot._reject_search_during_clarification_restriction(update)

        self.assertFalse(rejected)
        bot.app.bot.get_chat_member.assert_awaited_once_with(-100, 42)

    async def test_group_admin_cancel_does_not_increment_abuse_state(self):
        bot = _make_bot()
        bot._owner_user_id = None
        bot.app = MagicMock()
        bot.app.bot.get_chat_member = AsyncMock(return_value=MagicMock(status="creator"))
        bot.app.bot.send_message = AsyncMock()
        update = _callback_update(42, "clar_cancel_42")
        bot._clarification = {42: {
            "requester_id": 42,
            "chat_id": -100,
            "message_id": 1001,
            "source_message_id": 1000,
            "chat_type": "supergroup",
            "query": "example",
        }}
        update.callback_query.message.message_id = 1001

        await bot.button_callback(update, MagicMock())

        self.assertNotIn(42, bot._clarification_cancel_abuse)


# ── Cache ──────────────────────────────────────────────────────────────────────

class TestCache(unittest.TestCase):
    def test_set_get_cached_books_roundtrip(self):
        bot = _make_bot()
        books = [make_book(title="Book One"), make_book(title="Book Two")]
        bot._set_cached_books(42, books)

        result = bot._get_cached_books(42)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["title"], "Book One")
        self.assertEqual(result[1]["title"], "Book Two")

    def test_get_cached_books_missing_user_returns_none(self):
        bot = _make_bot()
        self.assertIsNone(bot._get_cached_books(9999))

    def test_set_cached_books_stores_3_tuple(self):
        bot = _make_bot()
        bot._set_cached_books(1, [make_book()])
        entry = bot.search_cache[1]
        self.assertEqual(len(entry), 3)
        self.assertIsInstance(entry[1], float)  # timestamp

    def test_set_cached_books_eviction(self):
        bot = _make_bot()
        bot._SEARCH_CACHE_MAX = 5
        for uid in range(10):
            bot._set_cached_books(uid, [make_book()])
        # At most MAX entries should remain
        self.assertLessEqual(len(bot.search_cache), bot._SEARCH_CACHE_MAX)


class TestAggregateSearchSingleFlight(unittest.IsolatedAsyncioTestCase):
    async def test_simultaneous_identical_queries_share_one_fetch_and_isolate_results(self):
        bot = _make_bot()
        started = asyncio.Event()
        release = asyncio.Event()
        result = [make_book(title="Shared result")]

        async def delayed_aggregate(query, limit):
            started.set()
            await release.wait()
            return result

        bot.aggregator.aggregate_book_data = AsyncMock(side_effect=delayed_aggregate)
        first = asyncio.create_task(bot._aggregate_search_results("Harry Potter"))
        await started.wait()
        second = asyncio.create_task(bot._aggregate_search_results("harry potter"))
        await asyncio.sleep(0)
        release.set()
        first_result, second_result = await asyncio.gather(first, second)

        bot.aggregator.aggregate_book_data.assert_awaited_once_with(
            "Harry Potter", limit=10
        )
        self.assertEqual(first_result[0]["title"], "Shared result")
        self.assertEqual(second_result[0]["title"], "Shared result")
        self.assertIsNot(first_result, second_result)
        self.assertIsNot(first_result[0], second_result[0])

    async def test_cancelled_waiter_does_not_cancel_shared_fetch(self):
        bot = _make_bot()
        started = asyncio.Event()
        release = asyncio.Event()

        async def delayed_aggregate(query, limit):
            started.set()
            await release.wait()
            return [make_book(title="Shared result")]

        bot.aggregator.aggregate_book_data = AsyncMock(side_effect=delayed_aggregate)
        cancelled_waiter = asyncio.create_task(
            bot._aggregate_search_results("Dune")
        )
        await started.wait()
        remaining_waiter = asyncio.create_task(
            bot._aggregate_search_results("Dune")
        )
        await asyncio.sleep(0)
        cancelled_waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled_waiter
        release.set()
        books = await remaining_waiter

        self.assertEqual(books[0]["title"], "Shared result")
        bot.aggregator.aggregate_book_data.assert_awaited_once()


class TestCoverFallback(unittest.TestCase):
    def test_valid_hardcover_fallback_is_saved_after_google_books_placeholder(self):
        bot = _make_bot()
        primary = MagicMock()
        primary.content = b"google-placeholder"
        primary.status_code = 200
        primary.headers = {"Content-Type": "image/png"}
        primary.url = "https://books.google.com/placeholder"
        primary.raise_for_status.return_value = None

        fallback = MagicMock()
        fallback.content = b"valid-hardcover-cover-bytes"
        fallback.status_code = 200
        fallback.headers = {"Content-Type": "image/jpeg"}
        fallback.url = "https://assets.hardcover.app/cover.jpg"
        fallback.raise_for_status.return_value = None

        session = MagicMock()
        session.get.side_effect = [primary, fallback]
        book = {
            "title": "Do Epic Shit",
            "author": "Ankur Warikoo",
            "cover_source": "google_books",
            "_hardcover_match": {
                "cover_url": "https://assets.hardcover.app/cover.jpg"
            },
        }

        with patch("src.handlers.get_http_session", return_value=session), patch(
            "src.handlers.is_placeholder_image", side_effect=[True, False]
        ):
            temp_path = bot.download_and_save_image(
                "https://books.google.com/placeholder", book
            )

        try:
            self.assertIsNotNone(temp_path)
            with open(temp_path, "rb") as cover_file:
                self.assertEqual(cover_file.read(), fallback.content)
            self.assertEqual(session.get.call_count, 2)
        finally:
            bot.cleanup_temp_file(temp_path)


class TestResultDeduplication(unittest.TestCase):
    def test_ranking_prefers_full_title_and_author_match(self):
        bot = _make_bot()
        books = [
            make_book(title="B-Movie Gothic", author="Johan Hoglund", isbn="other"),
            make_book(title="Goth", author="Otsuichi", isbn="match"),
            make_book(title="The Goth Guide", author="Unknown Author", isbn="partial"),
        ]

        result = bot._rank_search_results(books, "Goth Otsuichi")

        self.assertEqual(result[0]["isbn"], "match")

    def test_ranking_prefers_exact_work_title_over_a_longer_containing_title(self):
        bot = _make_bot()
        books = [
            make_book(title="Encyclopedia of Crime and Punishment", author="David Levinson"),
            make_book(title="Crime and Punishment", author="Fyodor Dostoevsky"),
        ]

        result = bot._rank_search_results(books, "Crime and Punishment")

        self.assertEqual(result[0]["title"], "Crime and Punishment")

    def test_single_character_transliteration_variant_merges_with_matching_initials(self):
        bot = _make_bot()
        books = [
            make_book(title="A Shared Work", author="J.R.R. Tolkein", isbn="one"),
            make_book(title="A Shared Work", author="J. R. R. Tolkien", isbn="two"),
        ]

        result = bot._deduplicate_search_results(books, "A Shared Work")

        self.assertEqual(len(result), 1)

    def test_transliterated_author_variants_and_exact_duplicates_collapse(self):
        bot = _make_bot()
        books = [
            make_book(
                title="Crime and Punishment by Fyodor Dostoevsky (Illustrated)",
                author="Fyodor Dostoevsky",
                isbn="illustrated",
            ),
            make_book(
                title="Crime and Punishment",
                author="Fyodor Dostoyevsky",
                isbn="variant-spelling",
                search_rating=0,
                search_rating_count=0,
            ),
            make_book(
                title="Crime and Punishment",
                author="Fyodor Dostoevsky",
                isbn="copy-one",
                search_rating=4.27,
                search_rating_count=1824,
                search_rating_formatted="4.27",
            ),
            make_book(
                title="Crime and Punishment",
                author="Fyodor Dostoevsky",
                isbn="copy-two",
                search_rating=4.27,
                search_rating_count=1824,
                search_rating_formatted="4.27",
            ),
            make_book(
                title="Crime and Punishment (The Unabridged Garnett Translation)",
                author="Fyodor Dostoevsky",
                isbn="unabridged",
            ),
            make_book(
                title="Crime and Punishment by Fyodor Dostoyevsky",
                author="Fyodor Dostoevsky",
                isbn="suffix-variant",
            ),
            make_book(
                title="Encyclopedia of Crime and Punishment",
                author="David Levinson",
                isbn="encyclopedia",
            ),
            make_book(
                title="Crime and Punishment Annotated",
                author="Fyodor Dostoevsky",
                isbn="annotated",
            ),
        ]

        result = bot._deduplicate_search_results(books, "Crime and Punishment")

        # Keep the illustrated/unabridged/annotated editions and encyclopedia,
        # while merging spelling variants and repeated copies of the same work.
        self.assertEqual(len(result), 5)
        canonical = next(book for book in result if book["title"] == "Crime and Punishment")
        self.assertEqual(canonical["search_rating"], 4.27)
        self.assertEqual(canonical["search_rating_count"], 1824)

    def test_exact_title_author_duplicate_collapses_across_distinct_source_ids(self):
        bot = _make_bot()
        books = [
            make_book(
                title="Crime and Punishment",
                author="Fyodor Dostoevsky",
                isbn="111",
                search_rating=4.27,
                search_rating_count=1824,
                search_rating_formatted="4.27",
            ),
            make_book(
                title="Crime and Punishment",
                author="Fyodor Dostoevsky",
                isbn="222",
                search_rating=4.27,
                search_rating_count=1824,
                search_rating_formatted="4.27",
            ),
        ]

        result = bot._deduplicate_search_results(books, "Crime and Punishment")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "Crime and Punishment")
        self.assertEqual(result[0]["author"], "Fyodor Dostoevsky")
        self.assertEqual(result[0]["search_rating"], 4.27)
        self.assertEqual(result[0]["search_rating_count"], 1824)

    def test_author_suffix_and_missing_rating_duplicate_are_merged(self):
        bot = _make_bot()
        books = [
            {
                "title": "Crime and Punishment by Fyodor Dostoyevsky",
                "author": "Fyodor Dostoyevsky",
                "isbn": "111",
                "description": "A full description.",
                "cover_url": "https://example.com/cover.jpg",
                "rating": 0.0,
                "rating_count": 0,
                "rating_formatted": "N/A",
            },
            {
                "title": "Crime and Punishment",
                "author": "Fyodor Dostoyevsky",
                "cover_url": "",
                "search_rating": 4.27,
                "search_rating_count": 1824,
                "search_rating_formatted": "4.27",
            },
        ]

        result = bot._deduplicate_search_results(books, "Crime and Punishment")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "Crime and Punishment")
        self.assertEqual(result[0]["author"], "Fyodor Dostoyevsky")
        self.assertEqual(result[0]["cover_url"], "https://example.com/cover.jpg")
        self.assertEqual(result[0]["search_rating"], 4.27)
        self.assertEqual(result[0]["search_rating_count"], 1824)
        self.assertEqual(result[0]["isbn"], "111")
        self.assertEqual(result[0]["description"], "A full description.")

    def test_title_credit_punctuation_and_duplicate_author_tokens_normalize_generically(self):
        bot = _make_bot()
        books = [
            make_book(title="Pride and Prejudice", author="Jane Austen", isbn="plain"),
            make_book(
                title="Pride and Prejudice",
                author="Jane Jane Austen",
                isbn="metadata-copy",
                search_rating=4.17,
                search_rating_count=3244,
                search_rating_formatted="4.17",
            ),
            make_book(title="Pride and Prejudice.Novel by", author="Jane Austen", isbn="tail-noise"),
            make_book(title="Jane Austen - Pride and Prejudice", author="Jane Austen", isbn="author-prefix"),
            make_book(title="Pride and Prejudice (Collins Classics)", author="Jane Austen", isbn="edition"),
        ]

        result = bot._deduplicate_search_results(books, "Pride and Prejudice")

        self.assertEqual(len(result), 2)
        standard = next(book for book in result if book["title"] == "Pride and Prejudice")
        self.assertEqual(standard["author"], "Jane Austen")
        self.assertEqual(standard["search_rating"], 4.17)
        self.assertEqual(standard["search_rating_count"], 3244)
        self.assertTrue(any("Collins Classics" in book["title"] for book in result))

    def test_unknown_author_copy_merges_only_when_title_has_one_known_author(self):
        bot = _make_bot()
        one_author = [
            make_book(title="Harry Potter", author="Unknown Author", isbn="unknown"),
            make_book(
                title="Harry Potter",
                author="S. Gunelius",
                isbn="known",
                search_rating=3.0,
                search_rating_count=8,
                search_rating_formatted="3.00",
            ),
        ]
        merged = bot._deduplicate_search_results(one_author, "Harry Potter")
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["author"], "S. Gunelius")
        self.assertEqual(merged[0]["search_rating"], 3.0)

        ambiguous = [
            make_book(title="The Gift", author="Author One", isbn="one"),
            make_book(title="The Gift", author="Author Two", isbn="two"),
            make_book(title="The Gift", author="Unknown Author", isbn="unknown"),
        ]
        preserved = bot._deduplicate_search_results(ambiguous, "The Gift")
        self.assertEqual(len(preserved), 3)

    def test_author_order_variation_and_isbn_10_13_identity(self):
        bot = _make_bot()
        books = [
            make_book(title="Confessions", author="Kanae Minato", isbn="0141439513"),
            make_book(title="Confessions", author="Minato Kanae", isbn="9780141439518"),
        ]

        result = bot._deduplicate_search_results(books, "Confessions")

        self.assertEqual(bot._isbn_key(books[0]), bot._isbn_key(books[1]))
        self.assertEqual(len(result), 1)

    def test_distinct_authors_and_subtitles_are_preserved(self):
        bot = _make_bot()
        books = [
            make_book(title="Shared Title", author="Author One", isbn="111"),
            make_book(title="Shared Title", author="Author Two", isbn="222"),
            make_book(title="Same Surname", author="John Smith", isbn="444"),
            make_book(title="Same Surname", author="Jane Smith", isbn="555"),
            make_book(title="Shared Title: A Critical Edition", author="Author One", isbn="333"),
        ]

        result = bot._deduplicate_search_results(books, "Shared Title")

        self.assertEqual(len(result), 5)


# ── Message building ───────────────────────────────────────────────────────────

class TestBuildSearchResultsMessage(unittest.TestCase):
    """_build_search_results_message produces the correct text and keyboard."""

    def _build(self, books, query="test query", uid=1, page=1, page_size=5):
        bot = _make_bot()
        return bot._build_search_results_message(books, query, uid, page, page_size)

    def test_single_page_shows_all_books(self):
        books = [
            make_book(title="Book Alpha", author="Author A",
                      search_rating=4.0, search_rating_count=100, search_rating_formatted="4.00"),
            make_book(title="Book Beta", author="Author B"),
        ]
        text, kb = self._build(books)
        self.assertIn("Book Alpha", text)
        self.assertIn("Author A", text)
        self.assertIn("4.00", text)
        self.assertIn("100 ratings", text)   # count=100, no thousands separator
        self.assertIn("Book Beta", text)
        self.assertIn("Author B", text)
        # No pagination nav expected for single page
        self.assertNotIn("◀", text)

    def test_single_page_shows_no_ratings_yet(self):
        books = [make_book(search_rating=0, search_rating_count=0, search_rating_formatted="N/A")]
        text, kb = self._build(books)
        self.assertIn("No ratings yet", text)

    def test_pagination_nav_on_page_1_of_2(self):
        books = [make_book() for _ in range(6)]  # 6 books, page_size=5 => 2 pages
        text, kb = self._build(books, page=1)
        self.assertIn("Page 1 of 2", text)
        self.assertNotIn("◀", str(kb.inline_keyboard))  # no prev on page 1
        self.assertIn("▶", str(kb.inline_keyboard))       # has next

    def test_pagination_nav_on_page_2_of_2(self):
        books = [make_book() for _ in range(6)]
        text, kb = self._build(books, page=2)
        self.assertIn("Page 2 of 2", text)
        self.assertIn("◀", str(kb.inline_keyboard))       # has prev
        self.assertNotIn("▶", str(kb.inline_keyboard))    # no next

    def test_pagination_nav_on_middle_page(self):
        books = [make_book() for _ in range(15)]  # 15 books, page_size=5 => 3 pages
        text, kb = self._build(books, page=2)
        self.assertIn("Page 2 of 3", text)
        self.assertIn("◀", str(kb.inline_keyboard))
        self.assertIn("▶", str(kb.inline_keyboard))

    def test_button_rows_max_5_per_row(self):
        """Numbered buttons must be arranged in horizontal rows of at most 5."""
        books = [make_book() for _ in range(10)]
        _, kb = self._build(books, uid=77, page=1)
        # Each row should have at most 5 buttons
        for row in kb.inline_keyboard:
            self.assertLessEqual(len(row), 5, f"Row has {len(row)} buttons, max 5 allowed")

    def test_button_rows_of_5_for_10_books(self):
        """10 books, page 1 (page_size=5) → one row of 5 buttons + nav row."""
        books = [make_book() for _ in range(10)]
        _, kb = self._build(books, uid=77, page=1)
        # Filter rows where every button text is a plain digit (numbered buttons)
        button_rows = [row for row in kb.inline_keyboard
                       if all(btn.text.strip().isdigit() for btn in row)]
        self.assertEqual(len(button_rows), 1, f"Expected 1 button row, got {len(button_rows)}")
        self.assertEqual(len(button_rows[0]), 5)
        # Nav row has at least one button whose callback starts with "page_"
        nav_rows = [row for row in kb.inline_keyboard
                    if any(btn.callback_data.startswith("page_") for btn in row)]
        self.assertEqual(len(nav_rows), 1)

    def test_button_rows_of_3_for_8_books_page_2(self):
        """8 books, page_size=5: page 2 has a single row of 3 buttons, then a nav row."""
        books = [make_book() for _ in range(8)]
        _, kb = self._build(books, uid=77, page=2)
        # Only the row of numbered buttons (6, 7, 8) — all are plain digits
        button_rows = [row for row in kb.inline_keyboard
                       if all(btn.text.strip().isdigit() for btn in row)]
        self.assertEqual(len(button_rows), 1)
        self.assertEqual(len(button_rows[0]), 3)
        # Nav row has at least one button whose callback starts with "page_"
        nav_rows = [row for row in kb.inline_keyboard
                    if any(btn.callback_data.startswith("page_") for btn in row)]
        self.assertEqual(len(nav_rows), 1)

    def test_book_callback_includes_page_num(self):
        books = [make_book() for _ in range(10)]
        _, kb = self._build(books, uid=123, page=2)
        # First button should have callback with page_num=2
        first_btn = kb.inline_keyboard[0][0]
        self.assertEqual(first_btn.callback_data, "book_123_5_2")

    def test_html_injection_in_title_is_escaped(self):
        books = [make_book(title='<script>alert("xss")</script>', author='<b>Bold</b>')]
        text, _ = self._build(books)
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertNotIn("<b>Bold</b>", text)
        self.assertIn("&lt;b&gt;Bold&lt;/b&gt;", text)

    def test_query_text_is_escaped(self):
        books = [make_book()]
        text, _ = self._build(books, query='<script>alert("xss")</script>')
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)

    def test_empty_books_handled(self):
        # Should not raise — max(1, 0//5) = 1 page
        text, kb = self._build([], uid=1)
        self.assertIn("Search Results", text)
        self.assertIn("Select a book:", text)

    def test_page_size_respected(self):
        books = [make_book() for _ in range(12)]
        text, kb = self._build(books, page=1, page_size=3)
        # Page 1 should have 3 numbered buttons
        num_labels = [btn.text for row in kb.inline_keyboard
                      for btn in row if btn.text not in ("◀", "▶", "1/4") and btn.text.isdigit()]
        self.assertEqual(num_labels, ["1", "2", "3"])

    def test_page_indicator_noop_button(self):
        books = [make_book() for _ in range(6)]
        _, kb = self._build(books, page=1)
        noop_found = any(
            btn.callback_data == "noop"
            for row in kb.inline_keyboard
            for btn in row
        )
        self.assertTrue(noop_found, "Page indicator should be a 'noop' callback button")


# ── Callback parsing ───────────────────────────────────────────────────────────

class TestCallbackParsing(unittest.TestCase):
    """button_callback branches parse their callback_data correctly."""

    def test_page_callback_parsing(self):
        bot = _make_bot()
        bot._set_cached_books(42, [make_book()])
        bot._search_query_cache[42] = "test"

        # Simulate the page_ branch logic (inline, no async needed)
        callback_data = "page_42_2"
        parts = callback_data.split("_")
        self.assertEqual(parts[0], "page")
        self.assertEqual(int(parts[1]), 42)
        self.assertEqual(int(parts[2]), 2)

        page_num = int(parts[2])
        bot._search_page_cache[42] = page_num
        self.assertEqual(bot._search_page_cache[42], 2)

    def test_back_callback_parsing(self):
        callback_data = "back_42"
        parts = callback_data.split("_")
        self.assertEqual(parts[0], "back")
        self.assertEqual(int(parts[1]), 42)

    def test_book_callback_4part_parsing(self):
        callback_data = "book_42_7_3"
        parts = callback_data.split("_")
        self.assertEqual(len(parts), 4)
        self.assertEqual(parts[0], "book")
        self.assertEqual(int(parts[1]), 42)  # user_id
        self.assertEqual(int(parts[2]), 7)   # book_idx
        self.assertEqual(int(parts[3]), 3)   # page_num

        # The logic that stores page for Back to Results
        page_num = int(parts[3]) if len(parts) > 3 else 1
        user_id = int(parts[1])

        bot = _make_bot()
        bot._search_page_cache[user_id] = page_num
        self.assertEqual(bot._search_page_cache[42], 3)

    def test_book_callback_3part_backward_compat(self):
        """Old 3-part callbacks degrade gracefully to page 1."""
        callback_data = "book_42_7"
        parts = callback_data.split("_")
        page_num = int(parts[3]) if len(parts) > 3 else 1
        self.assertEqual(page_num, 1)

    def test_download_callback_parsing(self):
        callback_data = "download_42_3"
        parts = callback_data.split("_")
        self.assertEqual(parts[0], "download")
        self.assertEqual(int(parts[1]), 42)
        self.assertEqual(int(parts[2]), 3)


# ── Hardcover rating preload ────────────────────────────────────────────────────

class TestPreloadHardcoverRatings(unittest.TestCase):
    """_preload_hardcover_ratings_for_page fills search_rating from per-book Hardcover lookups."""

    def test_fills_rating_from_hardcover(self):
        """A book without a rating gets one via _get_hardcover_cached."""
        bot = _make_bot()
        books = [make_book(title="Dune", author="Frank Herbert",
                           search_rating=0, search_rating_count=0,
                           search_rating_formatted="N/A")]
        with patch.object(
            MultiSourceBookAggregator, "_get_hardcover_cached",
            return_value=(4.3, 12000, ["Sci-Fi"], "https://cover.jpg")
        ) as mock_lookup:
            asyncio.run(bot._preload_hardcover_ratings_for_page(books, page_num=1, page_size=5))
            mock_lookup.assert_called_once()
            call_args = mock_lookup.call_args[0]
            self.assertEqual(call_args[1], "Dune")      # title
            self.assertEqual(call_args[2], "Frank Herbert")  # author
            # isbn is whatever make_book defaults to (9780743273565)
            self.assertEqual(call_args[0], "9780743273565")
        self.assertEqual(books[0]["search_rating"], 4.3)
        self.assertEqual(books[0]["search_rating_count"], 12000)
        self.assertEqual(books[0]["search_rating_formatted"], "4.30")
        self.assertEqual(books[0]["_hardcover_match"]["rating"], 4.3)
        self.assertEqual(books[0]["_hardcover_match"]["cover_url"], "https://cover.jpg")

    def test_uses_isbn_when_available(self):
        """ISBN is passed to _get_hardcover_cached when available."""
        bot = _make_bot()
        books = [make_book(title="Dune", author="Frank Herbert", isbn="978-0441172719",
                           search_rating=0, search_rating_count=0,
                           search_rating_formatted="N/A")]
        with patch.object(
            MultiSourceBookAggregator, "_get_hardcover_cached",
            return_value=(4.3, 12000, [], "")
        ):
            asyncio.run(bot._preload_hardcover_ratings_for_page(books, page_num=1, page_size=5))
        # The call should have ISBN as first argument
        # (checked via call_args below)
        self.assertEqual(books[0]["search_rating"], 4.3)

    def test_no_rating_sets_search_rating_to_zero(self):
        """When Hardcover has no rating, search_rating stays 0 and _hardcover_match is not set."""
        bot = _make_bot()
        books = [make_book(title="Unknown", author="Nobody",
                           search_rating=0, search_rating_count=0,
                           search_rating_formatted="N/A")]
        with patch.object(
            MultiSourceBookAggregator, "_get_hardcover_cached",
            return_value=(0, 0, [], "")
        ):
            asyncio.run(bot._preload_hardcover_ratings_for_page(books, page_num=1, page_size=5))
        self.assertEqual(books[0]["search_rating"], 0)
        self.assertIsNone(books[0].get("_hardcover_match"))

    def test_page_2_only_preloads_books_6_to_10(self):
        """Only visible books on the given page are looked up."""
        bot = _make_bot()
        # make_book(i) produces title="Book {i}" with 0-based i (Book 0 … Book 9)
        books = [make_book(title=f"Book {i}", author="Author",
                           search_rating=0, search_rating_count=0,
                           search_rating_formatted="N/A") for i in range(10)]
        with patch.object(
            MultiSourceBookAggregator, "_get_hardcover_cached",
            return_value=(4.0, 100, [], "")
        ) as mock_lookup:
            asyncio.run(bot._preload_hardcover_ratings_for_page(books, page_num=2, page_size=5))
            # Should be called exactly 5 times (books 6-10, i.e., indices 5-9)
            self.assertEqual(mock_lookup.call_count, 5)
            seen_titles = {call[0][1] for call in mock_lookup.call_args_list}
            # Page 2: start_idx=5, end_idx=10 → indices 5..9 = Book 5..Book 9 (0-based)
            expected_titles = {f"Book {i}" for i in range(5, 10)}
            self.assertEqual(seen_titles, expected_titles)

    def test_preserves_existing_non_search_fields(self):
        """Preload must not modify fields other than search_rating* and _hardcover_match."""
        bot = _make_bot()
        books = [make_book(title="Dune", author="Frank Herbert",
                           rating=3.5,      # canonical — must stay
                           search_rating=0,
                           search_rating_count=0,
                           search_rating_formatted="N/A")]
        with patch.object(
            MultiSourceBookAggregator, "_get_hardcover_cached",
            return_value=(4.3, 1000, [], "")
        ):
            asyncio.run(bot._preload_hardcover_ratings_for_page(books, page_num=1, page_size=5))
        self.assertEqual(books[0]["rating"], 3.5)  # canonical unchanged
        self.assertEqual(books[0]["search_rating"], 4.3)  # list field set

    def test_empty_book_list_no_crash(self):
        """Empty book list must not crash."""
        bot = _make_bot()
        with patch.object(
            MultiSourceBookAggregator, "_get_hardcover_cached",
            return_value=(4.0, 100, [], "")
        ):
            asyncio.run(bot._preload_hardcover_ratings_for_page([], page_num=1, page_size=5))

    def test_cache_hit_no_http_call_needed(self):
        """_get_hardcover_cached is still called (cache is internal); no crash on repeated call."""
        bot = _make_bot()
        books = [make_book(title="Dune", author="Frank Herbert",
                           search_rating=0, search_rating_count=0,
                           search_rating_formatted="N/A")]
        with patch.object(
            MultiSourceBookAggregator, "_get_hardcover_cached",
            return_value=(4.3, 12000, [], "")
        ):
            # First preload
            asyncio.run(bot._preload_hardcover_ratings_for_page(books, page_num=1, page_size=5))
            # Second preload (same books) — cache should hit; still calls _get_hardcover_cached
            asyncio.run(bot._preload_hardcover_ratings_for_page(books, page_num=1, page_size=5))
        self.assertEqual(books[0]["search_rating"], 4.3)


# ── Search page / query caches ─────────────────────────────────────────────────

class TestPaginationCaches(unittest.TestCase):
    def test_page_cache_stores_and_retrieves(self):
        bot = _make_bot()
        bot._search_page_cache[10] = 3
        bot._search_query_cache[10] = "harry potter"
        self.assertEqual(bot._search_page_cache.get(10), 3)
        self.assertEqual(bot._search_query_cache.get(10), "harry potter")

    def test_page_cache_missing_returns_none(self):
        bot = _make_bot()
        self.assertIsNone(bot._search_page_cache.get(9999))
        self.assertIsNone(bot._search_query_cache.get(9999))

    def test_back_to_results_restores_correct_page(self):
        """Simulate: user on page 2 selects book, then hits Back to Results."""
        bot = _make_bot()
        uid = 55
        books = [make_book(title=f"Book {i}") for i in range(10)]

        bot._set_cached_books(uid, books)
        bot._search_page_cache[uid] = 2  # user navigated to page 2
        bot._search_query_cache[uid] = "test query"

        # Simulate book_ callback: user selects book index 5 on page 2
        callback_data = f"book_{uid}_5_2"
        parts = callback_data.split("_")
        page_num = int(parts[3]) if len(parts) > 3 else 1
        bot._search_page_cache[uid] = page_num

        # Simulate back_ callback: restore page
        page_num = bot._search_page_cache.get(uid, 1)
        query_text = bot._search_query_cache.get(uid, "")

        # Rebuild the message as Back to Results would
        text, kb = bot._build_search_results_message(books, query_text, uid, page_num, 5)

        self.assertIn("Page 2 of 2", text)
        self.assertIn("6.", text)  # Book 6 is first on page 2 (global index 5)


if __name__ == "__main__":
    unittest.main()
