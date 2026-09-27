"""Tests for search clarification detection, candidate matching, and cooldown handling."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import unittest
import time
from unittest.mock import patch, MagicMock, AsyncMock

# Patch environment before imports
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "TEST_TOKEN")
os.environ.setdefault("GOOGLE_BOOKS_API_KEY", "TEST_GB_KEY")


# ── Helpers ─────────────────────────────────────────────────────────────────

def make_mock_update(query_text: str, user_id: int = 42, chat_id: int = 99):
    """Return a mock Update with a message and the given query text."""
    msg = MagicMock()
    msg.text = query_text
    msg.message_id = 1
    msg.chat_id = chat_id
    msg.reply_text = AsyncMock()
    msg.reply_to_message_id = 0
    msg.chat = MagicMock()
    msg.chat.send_action = AsyncMock()

    user = MagicMock()
    user.id = user_id

    update = MagicMock(spec=type("Update", (), {}))
    update.message = msg
    update.effective_user = user
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.type = "private"
    return update


def make_bot():
    """Return a GoodreadsBot with mocked aggregator."""
    from src.handlers import GoodreadsBot
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
    bot._clarification = {}
    bot._clarification_rate_limit = {}
    # New attributes added by recent changes
    bot._owner_user_id = None
    bot._clarification_cancel_abuse = {}
    bot._clarification_abuse_notice_rate_limit = {}
    bot._clarification_cancel_notice_rate_limit = {}
    bot._cached_books = {}
    bot._cached_users = {}
    bot._set_cached_books = MagicMock()
    return bot


# ── Mock Google Books API responses ─────────────────────────────────────────

def make_gb_response(volumes: list[dict]) -> MagicMock:
    """Return a mock `requests.Response` for Google Books API with given volumes.

    Each volume dict: {"title": str, "author": str}
    """
    items = [
        {
            "id": f"vol{i}",
            "volumeInfo": {
                "title": v["title"],
                "authors": [v["author"]],
                "language": "en",
            },
        }
        for i, v in enumerate(volumes)
    ]
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"items": items}
    return resp


# ── Test: _is_clarification_query ───────────────────────────────────────────

class TestIsClarificationQuery(unittest.TestCase):
    """Identify possible title/author splits; candidate matching decides whether to prompt."""

    def _check(self, query: str, expected: bool) -> None:
        from src.handlers import GoodreadsBot
        bot = make_bot()
        result = bot._is_clarification_query(query)
        self.assertEqual(bool(result[0]), expected,
                         f"query={query!r}: expected matches={expected}, got {result[0]}")

    def test_title_by_author_matches(self):
        self._check("confessions by kanae minato", True)

    def test_title_by_author_with_punctuation(self):
        self._check("confessions by kanae minato,", True)

    def test_two_word_query_is_candidate_checked(self):
        """Two-word queries may be title/author pairs; discovery rejects false splits."""
        self._check("Harry Potter", True)

    def test_non_by_one_title_one_author_matches(self):
        """'confessions minato' (non-'by', 1 title word, 1 author word) should match."""
        self._check("confessions minato", True)

    def test_non_by_two_title_one_author_matches(self):
        """'harry potter rowling' (non-'by', 2 title words, 1 author word) should match."""
        self._check("harry potter rowling", True)

    def test_non_by_two_title_two_author_matches(self):
        """'harry potter j.k. rowling' (2+2) should match."""
        self._check("harry potter j.k. rowling", True)

    def test_short_title_not_matched(self):
        """Single-char or 2-char title hints should not match."""
        self._check("a smith", False)
        self._check("to be", False)

    def test_queries_without_two_meaningful_tokens_are_not_matched(self):
        """Single-character fragments are ignored before candidate discovery."""
        self._check("x yz", False)            # 'yz' = 2 chars


# ── Test: _generate_plausible_splits ────────────────────────────────────────

class TestGeneratePlausibleSplits(unittest.TestCase):
    """_generate_plausible_splits generates (title, author) pairs for non-'by' queries."""

    def _splits(self, query: str) -> list[tuple[str, str]]:
        bot = make_bot()
        bot._STOPWORDS = frozenset({
            "the", "a", "an", "of", "in", "for", "with",
            "on", "at", "to", "by", "and", "or", "is", "are",
        })
        return bot._generate_plausible_splits(query)

    def test_confessions_minato(self):
        splits = self._splits("confessions minato")
        self.assertIn(("confessions", "minato"), splits)

    def test_harry_potter_rowling(self):
        splits = self._splits("harry potter rowling")
        self.assertIn(("harry potter", "rowling"), splits)

    def test_long_title_larsson(self):
        """Long title with multi-word author: 'The Girl with the Dragon Tattoo Larsson'"""
        splits = self._splits("The Girl with the Dragon Tattoo Larsson")
        self.assertIn(("The Girl with the Dragon Tattoo", "Larsson"), splits)

    def test_no_empty_author(self):
        """Splits with empty author should be excluded."""
        splits = self._splits("confessions")
        self.assertEqual(splits, [])

    def test_stops_at_max_title_words(self):
        """For 3-word queries with plausible 1-2 author words, only 1-title-word split is generated."""
        splits = self._splits("confessions of minato")  # 'of' is stopword
        titles = [t for t, a in splits]
        self.assertIn("confessions", titles)

    def test_rejects_initial_only_author(self):
        """Author must have at least one meaningful word; single initials rejected."""
        splits = self._splits("confessions a")
        self.assertEqual(splits, [])  # 'a' is a stopword → no meaningful author word


# ── Test: _score_title_hint ─────────────────────────────────────────────────

class TestScoreTitleHint(unittest.TestCase):
    """Title scoring: fraction of hint words found in candidate title."""

    def _score(self, hint: str, candidate: str) -> float:
        bot = make_bot()
        bot._STOPWORDS = frozenset({
            "the", "a", "an", "of", "in", "for", "with",
            "on", "at", "to", "by", "and", "or", "is", "are",
        })
        return bot._score_title_hint(
            bot._normalize_for_matching(hint),
            bot._normalize_for_matching(candidate),
        )

    def test_full_match(self):
        """Both hint words found → 1.0."""
        score = self._score("harry potter", "Harry Potter and the Sorcerer's Stone")
        self.assertEqual(score, 1.0)

    def test_partial_match(self):
        """One of two hint words found → 0.5."""
        score = self._score("harry potter", "Harry")
        self.assertAlmostEqual(score, 0.5)

    def test_extra_subtitle_words_allowed(self):
        """Extra subtitle words don't penalise the score."""
        score = self._score("harry potter", "Harry Potter and the Order of the Phoenix")
        self.assertEqual(score, 1.0)

    def test_no_match(self):
        score = self._score("confessions", "The Great Gatsby")
        self.assertEqual(score, 0.0)

    def test_stopwords_not_counted(self):
        """Stopwords in hint are excluded from denominator."""
        bot = make_bot()
        bot._STOPWORDS = frozenset({"the", "a", "an"})
        hint_norm = bot._normalize_for_matching("the confessions")
        cand_norm = bot._normalize_for_matching("The Confessions")
        # 'the' is a stopword → only 'confessions' is meaningful → 1/1 = 1.0
        score = bot._score_title_hint(hint_norm, cand_norm)
        self.assertEqual(score, 1.0)


# ── Test: _score_author_hint ─────────────────────────────────────────────────

class TestScoreAuthorHint(unittest.TestCase):
    """Author scoring: surname matches full name, initials handled."""

    def _score(self, hint: str, candidate: str) -> float:
        bot = make_bot()
        bot._STOPWORDS = frozenset()
        return bot._score_author_hint(
            bot._normalize_for_matching(hint),
            bot._normalize_for_matching(candidate),
        )

    def test_surname_matches_full_name(self):
        """'rowling' matches 'j.k. rowling'."""
        self.assertEqual(self._score("rowling", "j.k. rowling"), 1.0)

    def test_full_name_matches(self):
        """'j.k. rowling' matches 'j.k. rowling'."""
        self.assertEqual(self._score("j.k. rowling", "j.k. rowling"), 1.0)

    def test_reversed_name_order(self):
        """'kana' matches 'kana e minato' (reversed name order)."""
        self.assertEqual(self._score("kana", "kana e minato"), 1.0)

    def test_multiword_partial_match(self):
        """Multi-word hint requires token coverage — 'kanae minato' matches 'kanae minato' but 'kanae smith' doesn't."""
        self.assertEqual(self._score("kanae minato", "kanae minato"), 1.0)

    def test_weak_multiword_hint(self):
        """'kanae smith' vs 'kanae minato' should not score 1.0 (only 1 of 2 tokens matches)."""
        score = self._score("kanae smith", "kanae minato")
        # Only 'kanae' matches, 'smith' doesn't → 0.5
        self.assertLess(score, 1.0)

    def test_no_match(self):
        self.assertEqual(self._score("tolkien", "j.k. rowling"), 0.0)

    def test_empty_strings(self):
        self.assertEqual(self._score("", "j.k. rowling"), 0.0)
        self.assertEqual(self._score("rowling", ""), 0.0)


# ── Test: _discover_candidate with mock ──────────────────────────────────────

class TestDiscoverCandidate(unittest.TestCase):
    """_discover_candidate scores candidates and returns None for weak/ambiguous matches."""

    def _discover(self, query: str, title_hint: str | None, author_hint: str | None,
                  gb_volumes: list[dict]) -> dict | None:
        bot = make_bot()
        bot._STOPWORDS = frozenset({
            "the", "a", "an", "of", "in", "for", "with",
            "on", "at", "to", "by", "and", "or", "is", "are",
        })
        with patch("requests.get") as mock_get:
            mock_get.return_value = make_gb_response(gb_volumes)
            return bot._discover_candidate(query, title_hint, author_hint)

    def test_harry_potter_rowling_matches_jk_rowling(self):
        """'harry potter rowling' (no hints) → best match: title 'Harry Potter...', author 'J.K. Rowling'."""
        result = self._discover(
            query="harry potter rowling",
            title_hint=None,
            author_hint=None,
            gb_volumes=[
                {"title": "Harry Potter and the Sorcerer's Stone", "author": "J.K. Rowling"},
                {"title": "Harry Potter: The Complete Collection", "author": "J.K. Rowling"},
                {"title": "Harry Potter and the Deathly Hallows", "author": "J.K. Rowling"},
                # Non-match
                {"title": "The Fellowship of the Ring", "author": "J.R.R. Tolkien"},
            ],
        )
        self.assertIsNotNone(result, "Expected a match for 'harry potter rowling'")
        self.assertIn("Harry Potter", result["title"])
        self.assertIn("Rowling", result["author"])

    def test_confessions_minato_matches(self):
        """'confessions minato' (no hints) → matches 'Confessions of an Inquiring Spirit' by 'Minato Kanae'."""
        result = self._discover(
            query="confessions minato",
            title_hint=None,
            author_hint=None,
            gb_volumes=[
                {"title": "Confessions of an Inquiring Spirit", "author": "Minato Kanae"},
                {"title": "The Great Confessions", "author": "Unknown Author"},
            ],
        )
        self.assertIsNotNone(result)
        self.assertIn("Confessions", result["title"])
        self.assertIn("Minato", result["author"])

    def test_title_only_harry_potter_not_triggered(self):
        """Candidate title words alone must not count as author evidence."""
        result = self._discover(
            query="Harry Potter",
            title_hint="Harry",
            author_hint="Potter",
            gb_volumes=[
                {"title": "Harry Potter and the Sorcerer's Stone", "author": "J.K. Rowling"},
            ],
        )
        self.assertIsNone(result)

    def test_weak_match_no_clarification(self):
        """No candidate with both title_score >= 0.5 AND author_score >= 0.5 → returns None."""
        result = self._discover(
            query="confessions minato",
            title_hint=None,
            author_hint=None,
            gb_volumes=[
                # Title matches ('confessions' in title) but author 'tolkien' doesn't match 'minato'
                {"title": "Confessions of a Peasant", "author": "J.R.R. Tolkien"},
            ],
        )
        self.assertIsNone(result, "Weak match should not trigger clarification")

    def test_explicit_by_query_with_hints(self):
        """'confessions by kanae minato' passes explicit hints."""
        result = self._discover(
            query="confessions by kanae minato",
            title_hint="confessions",
            author_hint="kana e minato",
            gb_volumes=[
                {"title": "Confessions", "author": "Kanae Minato"},
            ],
        )
        self.assertIsNotNone(result)
        self.assertIn("Confessions", result["title"])



    def test_fallback_when_structured_returns_empty_items(self):
        """Fallback runs when structured query returns 200 with empty items."""
        bot = make_bot()
        bot._STOPWORDS = frozenset({
            "the", "a", "an", "of", "in", "for", "with",
            "on", "at", "to", "by", "and", "or", "is", "are",
        })
        with patch("requests.get") as mock_get:
            # First call: structured query -> 200, empty items
            # Second call: raw query -> match
            mock_get.side_effect = [
                make_gb_response([]),  # empty items
                make_gb_response([
                    {"title": "Harry Potter and the Sorcerer's Stone", "author": "J.K. Rowling"}
                ]),
            ]
            result = bot._discover_candidate(
                query="Harry Potter Rowling",
                title_hint="harry potter",
                author_hint="rowling",
            )
            self.assertIsNotNone(result)
            self.assertIn("Harry Potter", result["title"])
            self.assertIn("Rowling", result["author"])
            self.assertEqual(mock_get.call_count, 2)
# ── Test: cooldown / rate-limiting ───────────────────────────────────────────

class TestCooldown(unittest.IsolatedAsyncioTestCase):
    """Per-user, per-normalised-query cooldown: same query blocked (notice sent), different query proceeds."""

    @patch("requests.get")
    @patch("asyncio.to_thread")
    async def test_cooldown_same_query_returns_true(self, mock_thread, mock_get):
        """Same-user, same-normalised-query retry during cooldown: _try_clarification returns True."""
        import asyncio

        bot = make_bot()
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
        bot._clarification = {}
        bot._clarification_rate_limit = {}
        bot.app = MagicMock()
        bot.app.bot.send_message = AsyncMock()
        # Pre-seed cooldown: same user (42), same normalised query ("confessions minato")
        now = time.time()
        bot._clarification_rate_limit[(42, "confessions minato")] = now - 1  # 1 second ago

        update = make_mock_update("confessions minato", user_id=42, chat_id=99)

        # Should return True (stop caller) without calling Google Books
        result = await bot._try_clarification(update, "confessions minato")

        self.assertTrue(result, "Should return True during cooldown to stop caller")
        mock_get.assert_not_called()  # No API call during cooldown

    @patch("requests.get")
    @patch("asyncio.to_thread")
    async def test_cooldown_different_query_proceeds(self, mock_thread, mock_get):
        """Different query during cooldown: cooldown key differs → clarification proceeds."""
        import asyncio
        from src.handlers import GoodreadsBot

        # Make asyncio.to_thread actually execute the function (synchronously in tests)
        mock_thread.side_effect = lambda fn: fn()

        bot = make_bot()
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
        bot._clarification = {}
        bot._clarification_rate_limit = {}
        bot.app = MagicMock()
        bot.app.bot.send_message = AsyncMock()
        # Pre-seed cooldown: same user (42), DIFFERENT normalised query
        now = time.time()
        bot._clarification_rate_limit[(42, "harry potter rowling")] = now - 1

        update = make_mock_update("confessions minato", user_id=42, chat_id=99)

        # Mock Google Books to return a match
        mock_get.return_value = make_gb_response([
            {"title": "Confessions of an Inquiring Spirit", "author": "Minato Kanae"},
        ])

        result = await bot._try_clarification(update, "confessions minato")

        # Should return True (clarification was shown) — cooldown didn't block it
        mock_get.assert_called()  # API was called since cooldown key is different


# ── Test: search_command ordering ───────────────────────────────────────────────

class TestSearchCommandOrdering(unittest.IsolatedAsyncioTestCase):
    """Clarification must run BEFORE aggregate_book_data; stop on prompt, continue on miss."""

    async def test_clarification_before_normal_search(self):
        """_try_clarification is called before aggregate_book_data; stop on prompt."""
        bot = make_bot()
        bot.aggregator = AsyncMock()
        bot._clarification = {}
        bot._clarification_rate_limit = {}

        update = make_mock_update("harry potter rowling", user_id=42, chat_id=99)

        # Mock _try_clarification to return True (prompt was shown)
        with patch.object(bot, "_try_clarification", return_value=True) as mock_clar:
            with patch.object(bot, "_preload_hardcover_ratings_for_page", new_callable=AsyncMock):
                await bot.search_command(update, MagicMock(args=["harry","potter","rowling"]))
                mock_clar.assert_called_once_with(update, "harry potter rowling")
                # aggregate_book_data must NOT be called when clarification stops the pipeline
                bot.aggregator.aggregate_book_data.assert_not_called()

    async def test_normal_search_continues_without_clarification(self):
        """No clarification prompt → normal search runs."""
        bot = make_bot()
        bot.aggregator = AsyncMock(return_value=[])
        bot._clarification = {}
        bot._clarification_rate_limit = {}

        update = make_mock_update("harry potter rowling", user_id=42, chat_id=99)

        # _try_clarification returns False (not a clarification query)
        with patch.object(bot, "_try_clarification", return_value=False):
            with patch.object(bot, "_preload_hardcover_ratings_for_page", new_callable=AsyncMock):
                await bot.search_command(update, MagicMock(args=["harry","potter","rowling"]))
                bot.aggregator.aggregate_book_data.assert_called_once_with(
                    "harry potter rowling", limit=10)


# ── Test: button actions ───────────────────────────────────────────────────────

def _make_callback_update(user_id: int, callback_data: str, chat_id: int = 99,
                         message_id: int = 999):
    """Return a mock Update simulating a real Telegram callback_query (no update.message)."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.effective_chat.type = "private"
    update.callback_query = MagicMock()
    update.callback_query.data = callback_data
    update.callback_query.answer = AsyncMock()
    update.callback_query.delete_message = AsyncMock()
    update.callback_query.message = MagicMock()
    update.callback_query.message.chat_id = chat_id
    update.callback_query.message.message_id = message_id
    update.message = None  # Real Telegram callbacks have no message attribute
    return update


class TestClarificationButtonActions(unittest.IsolatedAsyncioTestCase):
    """Yes/No/Cancel buttons: delete message, correct search dispatch."""

    async def test_yes_deletes_and_searches_title_plus_author(self):
        """Yes: deletes prompt, sends result via chat_id from effective_chat."""
        bot = make_bot()
        bot._clarification = {42: {
            "query": "harry potter rowling",
            "title_hint": "harry potter",
            "author_hint": "rowling",
            "canonical_title": "Harry Potter",
            "canonical_author": "J.K. Rowling",
            "chat_id": 99,
            "message_id": 999,
            "source_message_id": 123,
            "chat_type": "group",
            "requester_id": 42,
        }}
        bot._clarification_rate_limit = {}
        sample_books = [{"title": "Harry Potter and the Philosopher's Stone", "author": "J.K. Rowling", "cover_url": "http://img"}]
        bot.aggregator = MagicMock()
        bot.aggregator.aggregate_book_data = AsyncMock(return_value=sample_books)
        bot._set_cached_books = MagicMock()
        bot._preload_hardcover_ratings_for_page = AsyncMock()
        bot._build_search_results_message = MagicMock(return_value=("Results text", None))
        bot.app = MagicMock()
        bot.app.bot.send_message = AsyncMock()
        bot.app.bot.send_chat_action = AsyncMock()

        update = _make_callback_update(42, "clar_yes_42", chat_id=99)
        update.effective_chat.type = "group"
        context = MagicMock()

        # Capture created background tasks so we can await them deterministically
        background_tasks = []
        orig_create_task = asyncio.create_task
        def tracking_create_task(coro):
            t = orig_create_task(coro)
            background_tasks.append(t)
            return t

        with patch("asyncio.create_task", side_effect=tracking_create_task):
            await bot.button_callback(update, context)

        if background_tasks:
            await asyncio.gather(*background_tasks)

        update.callback_query.delete_message.assert_awaited_once()
        self.assertNotIn(42, bot._clarification)
        self.assertIn((42, "harry potter rowling"), bot._clarification_rate_limit)
        # The aggregator must be called with the canonical title and author
        bot.aggregator.aggregate_book_data.assert_awaited_once_with("Harry Potter J.K. Rowling", limit=10)
        # Caches must be populated after Yes
        bot._set_cached_books.assert_called_once_with(42, sample_books)
        self.assertEqual(bot._search_page_cache.get(42), 1)
        self.assertEqual(bot._search_query_cache.get(42), "Harry Potter J.K. Rowling")
        bot._preload_hardcover_ratings_for_page.assert_awaited_once_with(sample_books, 1, 5)
        sent = bot.app.bot.send_message.await_args.kwargs
        self.assertEqual(sent["chat_id"], 99)
        self.assertEqual(sent["text"], "Results text")
        self.assertEqual(sent["reply_to_message_id"], 123)

    async def test_no_deletes_and_searches_original_query(self):
        """No: deletes prompt, searches with original query."""
        bot = make_bot()
        bot._clarification = {42: {
            "query": "harry potter rowling",
            "title_hint": "harry potter",
            "author_hint": "rowling",
            "canonical_title": "Harry Potter",
            "canonical_author": "J.K. Rowling",
            "chat_id": 99,
            "message_id": 999,
            "requester_id": 42,
        }}
        bot._clarification_rate_limit = {}
        sample_books = [{"title": "Harry Potter", "author": "J.K. Rowling", "cover_url": "http://img"}]
        bot._set_cached_books = MagicMock()
        bot._preload_hardcover_ratings_for_page = AsyncMock()
        bot._build_search_results_message = MagicMock(return_value=("", None))
        bot.aggregator = MagicMock()
        bot.aggregator.aggregate_book_data = AsyncMock(return_value=sample_books)
        bot.app = MagicMock()
        bot.app.bot.send_message = AsyncMock()
        bot.app.bot.send_chat_action = AsyncMock()

        update = _make_callback_update(42, "clar_no_42", chat_id=99)
        context = MagicMock()

        background_tasks = []
        orig_create_task = asyncio.create_task
        def tracking_create_task(coro):
            t = orig_create_task(coro)
            background_tasks.append(t)
            return t

        with patch("asyncio.create_task", side_effect=tracking_create_task):
            await bot.button_callback(update, context)

        if background_tasks:
            await asyncio.gather(*background_tasks)

        update.callback_query.delete_message.assert_awaited_once()
        self.assertNotIn(42, bot._clarification)
        # Should have searched with the original query (not canonical or hint)
        bot.aggregator.aggregate_book_data.assert_awaited_once_with("harry potter rowling", limit=10)
        bot._set_cached_books.assert_called_once_with(42, sample_books)
        self.assertEqual(bot._search_page_cache.get(42), 1)
        self.assertEqual(bot._search_query_cache.get(42), "harry potter rowling")
        bot._preload_hardcover_ratings_for_page.assert_awaited_once_with(sample_books, 1, 5)

    async def test_cancel_deletes_prompt_and_stops(self):
        """Cancel: deletes prompt, no search is dispatched."""
        bot = make_bot()
        bot._clarification = {42: {
            "query": "harry potter rowling",
            "title_hint": "harry potter",
            "author_hint": "rowling",
            "canonical_title": "Harry Potter",
            "canonical_author": "J.K. Rowling",
            "chat_id": 99,
            "message_id": 999,
            "requester_id": 42,
        }}
        bot._clarification_rate_limit = {}
        bot.aggregator = AsyncMock()
        bot.app = MagicMock()
        bot.app.bot.send_message = AsyncMock()
        bot.app.bot.send_chat_action = AsyncMock()

        update = _make_callback_update(42, "clar_cancel_42", chat_id=99)
        context = MagicMock()

        await bot.button_callback(update, context)

        update.callback_query.delete_message.assert_awaited_once()
        self.assertNotIn(42, bot._clarification)
        # No search should have been dispatched
        bot.aggregator.aggregate_book_data.assert_not_called()
        self.assertIn("search with a different query", bot.app.bot.send_message.await_args.kwargs["text"])

    async def test_double_click_guard(self):
        """Second click on same prompt: no-op (already consumed)."""
        bot = make_bot()
        bot._clarification = {}  # Already consumed
        bot._clarification_rate_limit = {}
        bot.aggregator = AsyncMock()
        bot.app = MagicMock()

        update = _make_callback_update(42, "clar_yes_42")

        await bot.button_callback(update, MagicMock())

        update.callback_query.delete_message.assert_not_called()


# ── Test: cooldown notice ──────────────────────────────────────────────────────

class TestCooldownNotice(unittest.IsolatedAsyncioTestCase):
    """Cooldown: sends wait notice and stops pipeline."""

    async def test_cooldown_sends_notice_and_returns_true(self):
        """During cooldown, _try_clarification sends a wait notice and returns True."""
        bot = make_bot()
        bot.aggregator = AsyncMock()
        bot.app = MagicMock()
        bot.app.bot.send_message = AsyncMock()

        now = time.time()
        bot._clarification_rate_limit[(42, "harry potter rowling")] = now - 1  # in cooldown
        # Cooldown check requires user to have an active clarification state
        bot._clarification[42] = {
            "query": "harry potter rowling",
            "title_hint": "harry potter",
            "author_hint": "rowling",
            "canonical_title": "Harry Potter",
            "canonical_author": "J.K. Rowling",
            "chat_id": 99,
            "message_id": 999,
        }

        update = make_mock_update("harry potter rowling", user_id=42, chat_id=99)

        with patch("requests.get"):
            with patch.object(bot, "_is_clarification_query", return_value=(False, None, None)):
                result = await bot._try_clarification(update, "harry potter rowling")

        self.assertTrue(result)
        bot.app.bot.send_message.assert_awaited_once()
        call_args = bot.app.bot.send_message.await_args
        # send_message(chat_id, text, ...) — positional: args[0]=chat_id, args[1]=text
        text = call_args.kwargs.get("text") or (call_args.args[1] if len(call_args.args) > 1 else "")
        self.assertIn("wait", text.lower())


    async def test_same_query_retry_does_not_fall_through_to_normal_search(self):
        bot = make_bot()
        bot.aggregator = MagicMock()
        bot.aggregator.aggregate_book_data = AsyncMock(return_value=[])
        bot.app = MagicMock()
        bot.app.bot.send_message = AsyncMock()
        bot._clarification_rate_limit[(42, "confessions minato")] = time.time()
        update = make_mock_update("/search confessions minato", user_id=42)
        context = MagicMock(args=["confessions", "minato"])

        await bot.search_command(update, context)

        bot.aggregator.aggregate_book_data.assert_not_called()
        bot.app.bot.send_message.assert_awaited_once()
        notice = bot.app.bot.send_message.await_args.args[1]
        self.assertIn("wait", notice.lower())


class TestCancellationAbuseLimits(unittest.IsolatedAsyncioTestCase):
    def test_three_cancels_within_window_apply_cooldown(self):
        bot = make_bot()
        actions = [bot._record_clarification_cancel(42) for _ in range(3)]

        self.assertEqual(actions, [None, None, "cooldown"])
        self.assertEqual(bot._active_clarification_restriction(42)[0], "cooldown")

    def test_repeat_cancellations_after_cooldown_apply_one_hour_block(self):
        bot = make_bot()
        now = time.time()
        bot._clarification_cancel_abuse[42] = {
            "count": 0,
            "window_start": now,
            "level": 1,
            "escalation_expires": now + 86400,
            "cooldown_until": now - 1,
        }

        actions = [bot._record_clarification_cancel(42) for _ in range(3)]

        self.assertEqual(actions, [None, None, "blocked"])
        self.assertEqual(bot._active_clarification_restriction(42)[0], "blocked")

    def test_owner_is_exempt_from_cancel_escalation(self):
        bot = make_bot()
        bot._owner_user_id = 42

        self.assertEqual(
            [bot._record_clarification_cancel(42) for _ in range(6)],
            [None] * 6,
        )
        self.assertIsNone(bot._active_clarification_restriction(42))

    async def test_active_restriction_stops_search_command(self):
        bot = make_bot()
        bot.aggregator = MagicMock()
        bot.aggregator.aggregate_book_data = AsyncMock(return_value=[])
        now = time.time()
        bot._clarification_cancel_abuse[42] = {
            "cooldown_until": now + 120,
            "blocked_until": 0,
            "count": 0,
            "window_start": now,
            "level": 1,
            "escalation_expires": now + 86400,
        }
        update = make_mock_update("/search Harry Potter", user_id=42)
        update.effective_message = update.message
        context = MagicMock(args=["Harry", "Potter"])

        await bot.search_command(update, context)

        bot.aggregator.aggregate_book_data.assert_not_called()
        update.message.reply_text.assert_awaited_once()


# ── Test: strong author matching ───────────────────────────────────────────────

class TestStrongAuthorMatching(unittest.IsolatedAsyncioTestCase):
    """Multi-word author hints: partial match is rejected."""

    def test_harry_potter_rowling_full_author_match(self):
        """'rowling' (single token) fully matches 'j.k. rowling'."""
        from src.handlers import GoodreadsBot

        bot = make_bot()
        bot._STOPWORDS = frozenset({
            "the", "a", "an", "of", "in", "for", "with",
            "on", "at", "to", "by", "and", "or", "is", "are",
        })

        score = GoodreadsBot._score_author_hint(bot, "rowling", "j.k. rowling")
        self.assertEqual(score, 1.0, "Single-surname 'rowling' should score 1.0 against 'j.k. rowling'")

    def test_weak_multiword_author_rejected(self):
        """Two-token hint 'minato confessions' against 'minato kanae': only 1/2 tokens
        match → score is 0.0 (partial multi-word matches are rejected)."""
        from src.handlers import GoodreadsBot

        bot = make_bot()
        bot._STOPWORDS = frozenset({
            "the", "a", "an", "of", "in", "for", "with",
            "on", "at", "to", "by", "and", "or", "is", "are",
        })

        # Only 'minato' matches (1/2 tokens)
        score = GoodreadsBot._score_author_hint(bot, "minato confessions", "minato kanae")
        self.assertEqual(score, 0.0,
            "Two-token partial match (1/2) should be rejected (0.0)")

    def test_explicit_by_query_with_multiword_author_weak_hint_rejected(self):
        """'by confessions yuki minato' with hints (confessions, yuki minato):
        only 'minato' matches in 'Kanae Minato' → 1/2 tokens → rejected."""
        result = self._discover(
            query="by confessions yuki minato",
            title_hint="confessions",
            author_hint="yuki minato",
            gb_volumes=[
                {"title": "Confessions", "author": "Kanae Minato"},
            ],
        )
        # Partial match (1 of 2 author tokens) → rejected
        self.assertIsNone(result, "Partial multi-word author match must be rejected")

    def _discover(self, query, title_hint, author_hint, gb_volumes):
        bot = make_bot()
        bot._STOPWORDS = frozenset({
            "the", "a", "an", "of", "in", "for", "with",
            "on", "at", "to", "by", "and", "or", "is", "are",
        })
        with patch("requests.get") as mock_get:
            mock_get.return_value = make_gb_response(gb_volumes)
            return bot._discover_candidate(query, title_hint, author_hint)


# ── Test: long titles ──────────────────────────────────────────────────────────

class TestLongTitles(unittest.IsolatedAsyncioTestCase):
    """Long titles with a trailing surname author: title+author correctly split."""

    def test_long_title_larsson_split(self):
        """'the girl with the dragon tattoo larsson' splits correctly:
        title='the girl with the dragon tattoo', author='larsson'."""
        from src.handlers import GoodreadsBot

        bot = make_bot()
        bot._STOPWORDS = frozenset({
            "the", "a", "an", "of", "in", "for", "with",
            "on", "at", "to", "by", "and", "or", "is", "are",
        })

        splits = GoodreadsBot._generate_plausible_splits(
            bot, "the girl with the dragon tattoo larsson")
        self.assertIn(("the girl with the dragon tattoo", "larsson"), splits)

    def test_long_title_with_candidate_found(self):
        """'the girl with the dragon tattoo larsson' → discovers candidate via GB."""
        from src.handlers import GoodreadsBot

        bot = make_bot()
        bot._STOPWORDS = frozenset({
            "the", "a", "an", "of", "in", "for", "with",
            "on", "at", "to", "by", "and", "or", "is", "are",
        })

        with patch("requests.get") as mock_get:
            mock_get.return_value = make_gb_response([
                {"title": "The Girl with the Dragon Tattoo", "author": "Stieg Larsson"},
            ])
            result = bot._discover_candidate(
                "the girl with the dragon tattoo larsson",
                title_hint=None, author_hint=None)

        self.assertIsNotNone(result)
        self.assertIn("Stieg Larsson", result["author"])




# ─── Integration: Harry Potter Rowling end-to-end ─────────────────────────────────

class TestHPClarificationIntegration(unittest.IsolatedAsyncioTestCase):
    """End-to-end: `Harry Potter Rowling` triggers clarification prompt via real split discovery.

    This mirrors the runtime scenario that previously skipped clarification because
    _discover_candidate failed to match J.K. Rowling against the hint "rowling".
    """

    async def test_harry_potter_rowling_clarification_triggered(self):
        """_try_clarification("Harry Potter Rowling") sends a confirmation prompt.

        Google Books returns a Harry Potter volume by J.K. Rowling; the author
        scoring must match "rowling" against "j.k. rowling" (score 1.0) and the
        title scoring must match "harry potter" against the volume title (>= 0.5).
        """
        import time
        from src.handlers import GoodreadsBot, InlineKeyboardButton, InlineKeyboardMarkup, ParseMode

        bot = make_bot()
        bot.aggregator = MagicMock()
        bot.app = MagicMock()
        bot.app.bot.send_message = AsyncMock()

        update = make_mock_update("Harry Potter Rowling", user_id=42, chat_id=99)

        # Realistic Google Books response: Harry Potter + J.K. Rowling + a distractor
        gb_response = make_gb_response([
            {"title": "Harry Potter and the Philosopher's Stone", "author": "J.K. Rowling"},
            {"title": "Harry Potter and the Chamber of Secrets", "author": "J.K. Rowling"},
            {"title": "The Hobbit", "author": "J.R.R. Tolkien"},
        ])

        with patch("requests.get", return_value=gb_response):
            result = await bot._try_clarification(update, "Harry Potter Rowling")

        # _try_clarification must return True (showed prompt / halted pipeline)
        self.assertTrue(result, "Expected True — clarification prompt should have been sent")

        # Clarification state must be stored for this user
        self.assertIn(42, bot._clarification)
        state = bot._clarification[42]
        self.assertEqual(state["query"], "Harry Potter Rowling")
        self.assertEqual(state["title_hint"], "harry potter")
        self.assertEqual(state["author_hint"], "rowling")
        self.assertIn("Harry Potter", state["canonical_title"])
        self.assertIn("Rowling", state["canonical_author"])

        # Rate-limit key must be recorded so repeated prompts are blocked
        norm_q = "harry potter rowling"
        self.assertIn((42, norm_q), bot._clarification_rate_limit)
        self.assertGreaterEqual(bot._clarification_rate_limit[(42, norm_q)], time.time() - 2)

        # send_message must have been called with the inline keyboard
        bot.app.bot.send_message.assert_awaited_once()
        call_kwargs = bot.app.bot.send_message.await_args.kwargs
        keyboard: InlineKeyboardMarkup = call_kwargs.get("reply_markup")
        self.assertIsInstance(keyboard, InlineKeyboardMarkup)
        buttons = [btn.text for row in keyboard.inline_keyboard for btn in row]
        self.assertIn("Yes, correct!", buttons)
        self.assertIn("No, search my query", buttons)
        self.assertIn("Cancel", buttons)

    async def test_harry_potter_title_only_does_not_clarify_from_title_word(self):
        """The word Potter in the title is not proof it is the author."""
        bot = make_bot()
        bot.app = MagicMock()
        bot.app.bot.send_message = AsyncMock()
        update = make_mock_update("Harry Potter", user_id=42, chat_id=99)
        response = make_gb_response([
            {"title": "Harry Potter and the Philosopher's Stone", "author": "J.K. Rowling"},
        ])

        with patch("requests.get", return_value=response):
            shown = await bot._try_clarification(update, "Harry Potter")

        self.assertFalse(shown)
        self.assertNotIn(42, bot._clarification)
        bot.app.bot.send_message.assert_not_awaited()

    async def test_harry_potter_joanne_knight_rowling_also_matches(self):
        """The same flow works with "Joanne Kathleen Rowling" in the GB response."""
        import time

        bot = make_bot()
        bot.aggregator = MagicMock()
        bot.app = MagicMock()
        bot.app.bot.send_message = AsyncMock()

        update = make_mock_update("Harry Potter Rowling", user_id=42, chat_id=99)

        gb_response = make_gb_response([
            {"title": "Harry Potter: The Complete Collection", "author": "Joanne Kathleen Rowling"},
        ])

        with patch("requests.get", return_value=gb_response):
            result = await bot._try_clarification(update, "Harry Potter Rowling")

        self.assertTrue(result)
        state = bot._clarification[42]
        self.assertIn("Joanne", state["canonical_author"])

    async def test_search_command_skips_aggregator_when_clarification_shown(self):
        """search_command must NOT call aggregate_book_data when clarification is active.

        This is the top-level assertion: a user running `/search Harry Potter Rowling`
        should see a confirmation prompt and the bot must not simultaneously run the
        normal multi-source aggregator.
        """
        from src.handlers import GoodreadsBot

        bot = make_bot()
        bot.aggregator = MagicMock()
        bot.aggregator.aggregate_book_data = AsyncMock(return_value=[])
        bot.app = MagicMock()
        bot.app.bot.send_message = AsyncMock()
        bot.app.bot.send_chat_action = AsyncMock()

        update = make_mock_update("Harry Potter Rowling", user_id=42, chat_id=99)

        # Provide a realistic GB response so _discover_candidate finds a match
        gb_response = make_gb_response([
            {"title": "Harry Potter and the Sorcerer's Stone", "author": "J.K. Rowling"},
        ])

        with patch("requests.get", return_value=gb_response):
            with patch.object(bot, "_preload_hardcover_ratings_for_page", new_callable=AsyncMock):
                with patch.object(bot, "_build_search_results_message", new_callable=MagicMock):
                    await bot.search_command(
                        update, MagicMock(args=["Harry", "Potter", "Rowling"])
                    )

        # aggregate_book_data must NOT have been called
        bot.aggregator.aggregate_book_data.assert_not_called()

        # Instead, the clarification state must be present (prompt shown)
        self.assertIn(42, bot._clarification)



    async def test_fallback_empty_structured_items_triggers_clarification(self):
        """When structured GB query returns 200 with empty items, raw fallback finds the match.

        Regression: previously the condition `if parts and items:` skipped fallback
        when the structured query returned an empty items list. Now `if parts:` runs
        the raw query, which successfully finds J.K. Rowling.
        """
        import time
        from src.handlers import GoodreadsBot

        bot = make_bot()
        bot.aggregator = MagicMock()
        bot.app = MagicMock()
        bot.app.bot.send_message = AsyncMock()

        update = make_mock_update("Harry Potter Rowling", user_id=42, chat_id=99)

        def gb_side_effect(url, **kwargs):
            # Structured query -> 200, empty items
            params = kwargs.get("params", {}) or {}
            q = params.get("q", "")
            if "intitle" in q or "inauthor" in q:
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = {"items": []}
                return resp
            # Raw fallback query -> match
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "items": [
                    {
                        "id": "vol0",
                        "volumeInfo": {
                            "title": "Harry Potter and the Sorcerer's Stone",
                            "authors": ["J.K. Rowling"],
                            "language": "en",
                        },
                    }
                ]
            }
            return resp

        with patch("requests.get", side_effect=gb_side_effect):
            result = await bot._try_clarification(update, "Harry Potter Rowling")

        self.assertTrue(result, "Clarification should trigger via raw fallback")
        self.assertIn(42, bot._clarification)
        state = bot._clarification[42]
        self.assertIn("Harry Potter", state["canonical_title"])
        self.assertIn("Rowling", state["canonical_author"])

        # search_command must not call aggregator when clarification is shown
        bot2 = make_bot()
        bot2.aggregator = MagicMock()
        bot2.aggregator.aggregate_book_data = AsyncMock(return_value=[])
        bot2.app = MagicMock()
        bot2.app.bot.send_message = AsyncMock()
        bot2.app.bot.send_chat_action = AsyncMock()

        update2 = make_mock_update("Harry Potter Rowling", user_id=42, chat_id=99)

        with patch("requests.get", side_effect=gb_side_effect):
            with patch.object(bot2, "_preload_hardcover_ratings_for_page", new_callable=AsyncMock):
                with patch.object(bot2, "_build_search_results_message", new_callable=MagicMock):
                    await bot2.search_command(update2, MagicMock(args=["Harry", "Potter", "Rowling"]))

        bot2.aggregator.aggregate_book_data.assert_not_called()
        self.assertIn(42, bot2._clarification)


    async def test_clarification_with_explicit_jk_rowling_author_hint(self):
        """When GB returns the canonical "J.K. Rowling" match, author_score must be 1.0."""
        bot = make_bot()
        bot.aggregator = MagicMock()
        bot.app = MagicMock()
        bot.app.bot.send_message = AsyncMock()

        update = make_mock_update("Harry Potter Rowling", user_id=42, chat_id=99)

        gb_response = make_gb_response([
            {"title": "Harry Potter and the Deathly Hallows", "author": "J.K. Rowling"},
        ])

        with patch("requests.get", return_value=gb_response):
            result = await bot._try_clarification(update, "Harry Potter Rowling")

        self.assertTrue(result)
        state = bot._clarification[42]
        self.assertEqual(state["canonical_author"], "J.K. Rowling")

    async def test_title_words_alone_do_not_become_author_evidence(self):
        """A title-only query must not clarify on a candidate author copied from its title."""
        bot = make_bot()
        bot.app = MagicMock()
        bot.app.bot.send_message = AsyncMock()
        update = make_mock_update("Crime and Punishment", user_id=42, chat_id=99)
        response = make_gb_response([
            {
                "title": "Crime and Punishment",
                "author": "Crime and Crime and Punishment",
            },
        ])

        with patch("requests.get", return_value=response):
            shown = await bot._try_clarification(update, "Crime and Punishment")

        self.assertFalse(shown)
        self.assertNotIn(42, bot._clarification)
        bot.app.bot.send_message.assert_not_awaited()


# ── Test: group-chat clarification binding ─────────────────────────────────────

class TestGroupChatClarificationBinding(unittest.IsolatedAsyncioTestCase):
    """Group-chat callbacks must verify (user_id, chat_id, message_id) match."""

    def _clar_state(
        self,
        user_id: int = 42,
        chat_id: int = 99,
        message_id: int = 888,
    ) -> dict:
        """Return a valid clarification entry for the given identity fields."""
        return {
            "query": "harry potter rowling",
            "title_hint": "harry potter",
            "author_hint": "rowling",
            "canonical_title": "Harry Potter",
            "canonical_author": "J.K. Rowling",
            "chat_id": chat_id,
            "message_id": message_id,
            "requester_id": user_id,
        }

    async def test_requester_taps_in_same_group(self):
        """When all three IDs match, the button proceeds normally."""
        bot = make_bot()
        bot._clarification = {42: self._clar_state(chat_id=99, message_id=888)}
        bot._clarification_rate_limit = {}
        bot.aggregator = MagicMock()
        bot.aggregator.aggregate_book_data = AsyncMock(return_value=[])
        bot._build_search_results_message = MagicMock(return_value=("", None))
        bot._preload_hardcover_ratings_for_page = AsyncMock()
        bot._set_cached_books = MagicMock()
        bot.app = MagicMock()
        bot.app.bot.send_message = AsyncMock()
        bot.app.bot.send_chat_action = AsyncMock()

        # Same user, same chat, same message → all match
        update = _make_callback_update(42, "clar_yes_42", chat_id=99, message_id=888)

        await bot.button_callback(update, MagicMock())

        # State must be consumed
        self.assertNotIn(42, bot._clarification)
        # No rejection alert
        update.callback_query.answer.assert_called_once()
        call_kwargs = update.callback_query.answer.call_args.kwargs
        self.assertNotIn("show_alert", call_kwargs or {})

    async def test_requester_callback_uses_prompt_id_not_search_message_id(self):
        """The sent clarification's ID differs from the original search message ID."""
        bot = make_bot()
        bot._clarification = {}
        bot._clarification_rate_limit = {}
        bot.app = MagicMock()
        bot.app.bot.send_message = AsyncMock()

        update = make_mock_update("Goth by Otsu", user_id=42, chat_id=99)
        update.effective_chat.type = "group"
        update.message.message_id = 111
        sent_prompt = MagicMock()
        sent_prompt.message_id = 222
        update.message.reply_text = AsyncMock(return_value=sent_prompt)

        candidate = {
            "title": "Goth",
            "author": "Otsuichi",
            "source": "google_books",
        }
        with patch.object(bot, "_discover_candidate", return_value=candidate):
            shown = await bot._try_clarification(update, "Goth by Otsu")

        self.assertTrue(shown)
        state = bot._clarification[42]
        self.assertEqual(state["source_message_id"], 111)
        self.assertEqual(state["message_id"], 222)
        update.message.reply_text.assert_awaited_once()
        self.assertEqual(
            update.message.reply_text.await_args.kwargs["reply_to_message_id"], 111
        )

        # The callback reports the prompt's ID (222), not the search's ID (111).
        callback = _make_callback_update(
            42, "clar_cancel_42", chat_id=99, message_id=222
        )
        await bot.button_callback(callback, MagicMock())

        self.assertNotIn(42, bot._clarification)
        callback.callback_query.answer.assert_awaited_once_with()

    async def test_other_group_member_taps_clarification_alert(self):
        """A different user's tap shows an alert and does NOT consume state."""
        bot = make_bot()
        bot._clarification = {42: self._clar_state(chat_id=99, message_id=888)}
        bot._clarification_rate_limit = {}

        # User 77 (not the requester) taps user 42's button in the same chat
        update = _make_callback_update(77, "clar_yes_42", chat_id=99, message_id=888)

        await bot.button_callback(update, MagicMock())

        # State must remain intact
        self.assertIn(42, bot._clarification)
        # Alert shown to the interloper
        update.callback_query.answer.assert_called_once()
        self.assertEqual(
            update.callback_query.answer.call_args[0][0],
            "This clarification prompt is for another user.",
        )

    async def test_other_group_member_cannot_cancel_prompt(self):
        bot = make_bot()
        bot._clarification = {42: self._clar_state(chat_id=99, message_id=888)}
        update = _make_callback_update(77, "clar_cancel_42", chat_id=99, message_id=888)

        await bot.button_callback(update, MagicMock())

        self.assertIn(42, bot._clarification)
        self.assertNotIn(42, bot._clarification_cancel_abuse)
        update.callback_query.delete_message.assert_not_awaited()
        update.callback_query.answer.assert_awaited_once()
        self.assertTrue(update.callback_query.answer.await_args.kwargs["show_alert"])

    async def test_callback_chat_id_mismatch_alert(self):
        """Callback from a different chat shows an alert and does NOT consume state."""
        bot = make_bot()
        bot._clarification = {42: self._clar_state(chat_id=99, message_id=888)}
        bot._clarification_rate_limit = {}

        # Correct user, correct message, but different chat
        update = _make_callback_update(42, "clar_yes_42", chat_id=55, message_id=888)

        await bot.button_callback(update, MagicMock())

        self.assertIn(42, bot._clarification)
        update.callback_query.answer.assert_called_once()
        self.assertEqual(
            update.callback_query.answer.call_args[0][0],
            "This clarification prompt is for another user.",
        )

    async def test_callback_message_id_mismatch_alert(self):
        """Callback from a different message shows an alert and does NOT consume state."""
        bot = make_bot()
        bot._clarification = {42: self._clar_state(chat_id=99, message_id=888)}
        bot._clarification_rate_limit = {}

        # Correct user, correct chat, but different message
        update = _make_callback_update(42, "clar_yes_42", chat_id=99, message_id=777)

        await bot.button_callback(update, MagicMock())

        self.assertIn(42, bot._clarification)
        update.callback_query.answer.assert_called_once()
        self.assertEqual(
            update.callback_query.answer.call_args[0][0],
            "This clarification prompt is for another user.",
        )


if __name__ == "__main__":
    unittest.main()
