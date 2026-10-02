"""Tests for search clarification detection, candidate matching, and cooldown handling."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import unittest
import time
import requests
from unittest.mock import patch, MagicMock, AsyncMock
from telegram.error import BadRequest

# Patch environment before imports
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "TEST_TOKEN")
os.environ.setdefault("GOOGLE_BOOKS_API_KEY", "TEST_GB_KEY")

import src.handlers

_session_patch = patch("src.handlers.get_http_session", return_value=requests)


def setUpModule():
    _session_patch.start()


def tearDownModule():
    _session_patch.stop()


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
    bot._group_admin_status_cache = {}
    bot._group_search_rate_limit = {}
    bot._group_search_notice_rate_limit = {}
    bot._group_search_inflight = set()
    bot._active_result_messages = {}
    bot._rating_refresh_tasks = set()
    bot._clarification_discovery_cache = {}
    bot._clarification_discovery_inflight = {}
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

    def test_plain_titles_ending_in_connectors_are_not_split(self):
        """Don't treat the final title word as an author after a connector."""
        for query in (
            "Crime and Punishment",
            "Angels & Demons",
            "War or Peace",
            "The Old Man and the Sea",
        ):
            with self.subTest(query=query):
                self._check(query, False)

    def test_title_plus_author_after_connector_still_matches(self):
        """A connector inside the complete title must not block its author suffix."""
        self._check("Crime and Punishment Dostoevsky", True)

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

    def test_fallback_searches_other_catalog_languages(self):
        """Language-neutral fallback can discover titles catalogued outside English."""
        bot = make_bot()
        bot._STOPWORDS = frozenset({"the", "a", "an", "of", "in", "for", "with", "on", "at", "to", "by", "and", "or", "is", "are"})
        with patch("requests.get") as mock_get:
            mock_get.side_effect = [
                make_gb_response([]),
                make_gb_response([
                    {"title": "Starting Over 重啟人生", "author": "Sugaru Miaki"}
                ]),
            ]
            result = bot._discover_candidate(
                "Starting over by Sugaru",
                title_hint="Starting over",
                author_hint="Sugaru",
            )

        self.assertIsNotNone(result)
        self.assertEqual(result["author"], "Sugaru Miaki")
        fallback_params = mock_get.call_args_list[1].kwargs["params"]
        self.assertNotIn("langRestrict", fallback_params)
        self.assertEqual(
            fallback_params["q"],
            'intitle:"Starting over" inauthor:"Sugaru"',
        )
        self.assertEqual(fallback_params["maxResults"], 40)

    def test_combined_plain_query_recovers_misindexed_translated_edition(self):
        """Keep title+author constraints while retrying without field operators."""
        bot = make_bot()
        bot._STOPWORDS = frozenset({"the", "a", "an", "of", "in", "for", "with", "on", "at", "to", "by", "and", "or", "is", "are"})
        with patch("requests.get") as mock_get:
            mock_get.side_effect = [
                make_gb_response([]),  # structured English search
                make_gb_response([]),  # structured all-language search
                make_gb_response([   # unstructured combined search
                    {"title": "Starting Over 重啟人生", "author": "Sugaru Miaki"}
                ]),
            ]
            result = bot._discover_candidate(
                "Starting Over by Sugaru",
                title_hint="Starting Over",
                author_hint="Sugaru",
            )

        self.assertEqual(result["title"], "Starting Over 重啟人生")
        self.assertEqual(result["author"], "Sugaru Miaki")
        combined_params = mock_get.call_args_list[2].kwargs["params"]
        self.assertEqual(combined_params["q"], '"Starting Over" "Sugaru"')
        self.assertNotIn("langRestrict", combined_params)
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
    """Clarify explicit/verified title-author pairs without interrupting title searches."""

    async def test_explicit_pair_clarification_precedes_normal_search(self):
        """An explicit title-by-author query is verified before catalog search."""
        bot = make_bot()
        bot.aggregator = AsyncMock()
        bot._clarification = {}
        bot._clarification_rate_limit = {}

        update = make_mock_update("Harry Potter by J.K. Rowling", user_id=42, chat_id=99)

        # Mock _try_clarification to return True (prompt was shown)
        with patch.object(bot, "_try_clarification", return_value=True) as mock_clar:
            with patch.object(bot, "_preload_hardcover_ratings_for_page", new_callable=AsyncMock):
                await bot.search_command(update, MagicMock(args=["Harry", "Potter", "by", "J.K.", "Rowling"]))
                mock_clar.assert_called_once_with(update, "Harry Potter by J.K. Rowling")
                # aggregate_book_data must NOT be called when clarification stops the pipeline
                bot.aggregator.aggregate_book_data.assert_not_called()

    async def test_normal_search_continues_without_clarification(self):
        """No clarification prompt → normal search runs."""
        bot = make_bot()
        bot.aggregator = AsyncMock(return_value=[])
        bot._clarification = {}
        bot._clarification_rate_limit = {}

        update = make_mock_update("Harry Potter", user_id=42, chat_id=99)

        # A title-like query isn't sent to speculative clarification discovery.
        with patch.object(bot, "_try_clarification", new_callable=AsyncMock) as clarify:
            with patch.object(bot, "_preload_hardcover_ratings_for_page", new_callable=AsyncMock):
                await bot.search_command(update, MagicMock(args=["Harry", "Potter"]))
                clarify.assert_not_awaited()
                bot.aggregator.aggregate_book_data.assert_called_once_with(
                    "Harry Potter", limit=10)


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

    async def test_yes_search_includes_supplementary_non_english_books(self):
        bot = make_bot()
        bot.aggregator = MagicMock()
        bot.aggregator.aggregate_book_data = AsyncMock(return_value=[
            {
                "title": "Three Days of Happiness",
                "author": "Sugaru Miaki",
                "language": "ja",
            },
        ])
        bot._aggregate_search_results = AsyncMock(return_value=[
            {"title": "Starting Over 重啟人生", "author": "三秋縋", "language": "zh"},
        ])
        bot._set_cached_books = MagicMock()
        bot._preload_hardcover_ratings_for_page = AsyncMock()
        bot._build_search_results_message = MagicMock(return_value=("results", None))
        bot.app = MagicMock()
        bot.app.bot.send_message = AsyncMock()
        update = make_mock_update("Starting Over by Sugaru Miaki")

        await bot._run_clarified_search(
            update,
            "Starting Over by Sugaru Miaki",
            "Starting Over",
            "Sugaru Miaki",
        )

        cached_books = bot._set_cached_books.call_args.args[1]
        self.assertEqual(
            {book["title"] for book in cached_books},
            {"Starting Over 重啟人生", "Three Days of Happiness"},
        )
        self.assertIn("ja", {book["language"] for book in cached_books})

    async def test_author_supplement_uses_hardcover_search(self):
        bot = make_bot()
        bot.aggregator = src.handlers.MultiSourceBookAggregator()
        bot._aggregate_search_results = AsyncMock(return_value=[
            {"title": "Origin", "author": "Dan Brown", "language": "en"},
        ])
        bot._set_cached_books = MagicMock()
        bot._preload_hardcover_ratings_for_page = AsyncMock()
        bot._build_search_results_message = MagicMock(return_value=("results", None))
        bot.app = MagicMock()
        bot.app.bot.send_message = AsyncMock()
        update = make_mock_update("Origin Dan")
        hardcover_books = [
            {"title": "Dan Brown 4-Book Boxset", "author": "Dan Brown", "language": "en"},
            {"title": "Angels & Demons", "author": "Dan Brown", "language": "en"},
            {"title": "Digital Fortress", "author": "Dan Brown", "language": "en"},
        ]

        with patch.object(
            src.handlers.MultiSourceBookAggregator,
            "search_hardcover",
            return_value=hardcover_books,
        ) as search_hardcover:
            await bot._run_clarified_search(update, "Origin Dan Brown", "Origin", "Dan Brown")

        search_hardcover.assert_called_once_with("Dan Brown", 40)
        cached_books = bot._set_cached_books.call_args.args[1]
        self.assertEqual(
            {book["title"] for book in cached_books},
            {"Origin", "Angels & Demons", "Digital Fortress", "Dan Brown 4-Book Boxset"},
        )
        self.assertEqual(cached_books[-1]["title"], "Dan Brown 4-Book Boxset")

    async def test_romanized_author_hint_recovers_hardcover_author_books(self):
        bot = make_bot()
        bot.aggregator = src.handlers.MultiSourceBookAggregator()
        bot._aggregate_search_results = AsyncMock(return_value=[
            {"title": "Starting Over 重啟人生", "author": "三秋縋", "language": "zh"},
        ])
        bot._set_cached_books = MagicMock()
        bot._preload_hardcover_ratings_for_page = AsyncMock()
        bot._build_search_results_message = MagicMock(return_value=("results", None))
        bot.app = MagicMock()
        bot.app.bot.send_message = AsyncMock()
        update = make_mock_update("Starting Over Sugaru")
        entry = {"author_hint": "Sugaru"}

        def hardcover_search(author_query, limit):
            self.assertEqual(limit, 40)
            if author_query == "Sugaru":
                return [{"title": "Three Days of Happiness", "author": "Sugaru Miaki"}]
            return [{"title": "Starting Over", "author": "Sugaru Miaki"}]

        with patch.object(
            src.handlers.MultiSourceBookAggregator,
            "search_hardcover",
            side_effect=hardcover_search,
        ) as search_hardcover:
            await bot._run_clarified_search(
                update, "Starting Over Sugaru", "Starting Over", "三秋縋", entry=entry
            )

        self.assertEqual(search_hardcover.call_count, 2)
        cached_titles = {
            book["title"] for book in bot._set_cached_books.call_args.args[1]
        }
        self.assertIn("Three Days of Happiness", cached_titles)

    async def test_old_result_message_selection_uses_its_own_search(self):
        bot = make_bot()
        bot._active_result_messages = {(99, 42): {"message_id": 2000, "page": 1}}
        old_books = [{"title": "Starting Over", "author": "Sugaru Miaki"}]
        newer_books = [{"title": "Confessions", "author": "Kanae Minato"}]
        bot._set_cached_books(42, newer_books)
        bot._cache_result_message(99, 1000, 42, old_books, "Starting Over Sugaru", 1)
        bot._set_cached_books = MagicMock(wraps=bot._set_cached_books)
        bot.format_book_message = MagicMock(return_value="Starting Over details")
        bot.download_and_save_image = MagicMock(return_value=None)
        bot.app = MagicMock()
        bot.app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=3000))
        update = _make_callback_update(42, "book_42_0_1", chat_id=99, message_id=1000)
        update.callback_query.message.chat.type = "private"
        context = MagicMock()
        context.bot.send_message = AsyncMock(return_value=MagicMock(message_id=3000))

        with patch.object(
            src.handlers.MultiSourceBookAggregator,
            "_ensure_ratings",
            return_value=(old_books[0], None),
        ), patch.object(
            src.handlers.MultiSourceBookAggregator,
            "_ensure_cover",
            return_value=old_books[0],
        ):
            await bot.button_callback(update, context)

        bot.format_book_message.assert_called_once_with(old_books[0])
        self.assertEqual(bot._active_result_messages[(99, 42)]["message_id"], 2000)

    async def test_repeated_page_edit_is_treated_as_success(self):
        bot = make_bot()
        books = [{"title": f"Book {index}", "author": "Author"} for index in range(15)]
        bot._get_cached_books = MagicMock(return_value=books)
        bot._search_page_cache = {42: 2}
        bot._search_query_cache = {42: "Author"}
        bot._active_result_messages = {}
        bot._build_search_results_message = MagicMock(return_value=("same page", None))
        bot._schedule_result_rating_refresh = MagicMock()
        update = _make_callback_update(42, "page_42_3", chat_id=99, message_id=999)
        update.callback_query.edit_message_text = AsyncMock(
            side_effect=BadRequest("Message is not modified")
        )
        context = MagicMock()

        await bot.button_callback(update, context)

        update.callback_query.answer.assert_awaited_once()
        self.assertEqual(bot._search_page_cache[42], 3)

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
        sample_books = [{"title": "Harry Potter and the Philosopher's Stone",
                         "author": "J.K. Rowling", "cover_url": "http://img", "language": "en"}]
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
        # A primary title search and mock-provider author fallback are made.
        calls = bot.aggregator.aggregate_book_data.call_args_list
        self.assertEqual(len(calls), 2, f"Expected 2 aggregate calls, got {calls}")
        call_queries = {c[0][0] for c in calls}
        self.assertIn("Harry Potter by J.K. Rowling", call_queries)
        self.assertIn("J.K. Rowling", call_queries)
        # Caches must be populated after Yes (merged primary + supplementary, deduped).
        cached_books = bot._set_cached_books.call_args[0][1]
        self.assertTrue(len(cached_books) >= 1)
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

    async def test_partial_sugaru_author_uses_full_name_in_prompt(self):
        bot = make_bot()
        bot.app = MagicMock()
        bot.app.bot.send_message = AsyncMock()
        update = make_mock_update("Starting Over by Sugaru", user_id=42, chat_id=99)

        with patch("requests.get") as mock_get:
            mock_get.side_effect = [
                make_gb_response([]),
                make_gb_response([
                    {"title": "Starting Over 重啟人生", "author": "Sugaru Miaki"}
                ]),
            ]
            shown = await bot._try_clarification(update, "Starting Over by Sugaru")

        self.assertTrue(shown)
        self.assertEqual(
            bot.app.bot.send_message.await_args.args[1],
            "Did you mean Starting Over by Sugaru Miaki?",
        )

    async def test_clarification_miss_cache_expires_quickly(self):
        bot = make_bot()
        bot._discover_candidate = MagicMock(return_value=None)

        await bot._discover_candidate_cached("Origin Dan", "origin", "dan")

        key = (
            bot._normalize_for_matching("Origin Dan"),
            bot._normalize_for_matching("origin"),
            bot._normalize_for_matching("dan"),
        )
        expires_at = bot._clarification_discovery_cache[key][0]
        self.assertLessEqual(
            expires_at - time.monotonic(),
            bot._CLARIFICATION_DISCOVERY_MISS_TTL_SECONDS,
        )

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

        update = make_mock_update("Harry Potter by J.K. Rowling", user_id=42, chat_id=99)

        # Provide a realistic GB response so _discover_candidate finds a match
        gb_response = make_gb_response([
            {"title": "Harry Potter and the Sorcerer's Stone", "author": "J.K. Rowling"},
        ])

        with patch("requests.get", return_value=gb_response):
            with patch.object(bot, "_preload_hardcover_ratings_for_page", new_callable=AsyncMock):
                with patch.object(bot, "_build_search_results_message", new_callable=MagicMock):
                    await bot.search_command(
                        update, MagicMock(args=["Harry", "Potter", "by", "J.K.", "Rowling"])
                    )

        # aggregate_book_data must NOT have been called
        bot.aggregator.aggregate_book_data.assert_not_called()

        # Instead, the clarification state must be present (prompt shown)
        self.assertIn(42, bot._clarification)



    async def test_fallback_empty_structured_items_triggers_clarification(self):
        """When structured GB query returns no items, title-only fallback finds the match.

        The fallback should search the title independently, then verify the author
        against candidate metadata instead of letting a raw all-terms query bury it.
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
            if "inauthor:" in q:
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

        # A non-explicit pair is clarified only after catalog results verify it.
        bot2 = make_bot()
        bot2.aggregator = MagicMock()
        bot2.aggregator.aggregate_book_data = AsyncMock(return_value=[{
            "title": "Harry Potter and the Sorcerer's Stone",
            "author": "J.K. Rowling", "source": "google_books",
        }])
        bot2.app = MagicMock()
        bot2.app.bot.send_message = AsyncMock()
        bot2.app.bot.send_chat_action = AsyncMock()

        update2 = make_mock_update("Harry Potter Rowling", user_id=42, chat_id=99)

        with patch("requests.get", side_effect=gb_side_effect):
            with patch.object(bot2, "_preload_hardcover_ratings_for_page", new_callable=AsyncMock):
                with patch.object(bot2, "_build_search_results_message", new_callable=MagicMock):
                    await bot2.search_command(update2, MagicMock(args=["Harry", "Potter", "Rowling"]))

        bot2.aggregator.aggregate_book_data.assert_awaited_once_with(
            "Harry Potter Rowling", limit=10
        )
        self.assertIn(42, bot2._clarification)


    async def test_plain_combined_fallback_clarifies_origin_dan(self):
        """A broad query can find a title/author pair missed by fielded lookups."""
        bot = make_bot()
        bot.app = MagicMock()
        bot.app.bot.send_message = AsyncMock()
        update = make_mock_update("Origin Dan", user_id=42, chat_id=99)

        def gb_side_effect(_url, **kwargs):
            query = (kwargs.get("params") or {}).get("q", "")
            response = MagicMock()
            response.status_code = 200
            if query == "Origin Dan":
                response.json.return_value = {
                    "items": [{
                        "id": "origin-dan-brown",
                        "volumeInfo": {
                            "title": "Origin",
                            "authors": ["Dan Brown"],
                            "language": "en",
                        },
                    }],
                }
            else:
                response.json.return_value = {"items": []}
            return response

        with patch("requests.get", side_effect=gb_side_effect):
            result = await bot._try_clarification(update, "Origin Dan")

        self.assertTrue(result)
        self.assertIn(42, bot._clarification)
        self.assertEqual(
            bot._clarification[42]["canonical_title"], "Origin"
        )
        self.assertEqual(
            bot._clarification[42]["canonical_author"], "Dan Brown"
        )


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
        reply_parameters = update.message.reply_text.await_args.kwargs["reply_parameters"]
        self.assertEqual(reply_parameters.message_id, 111)
        self.assertTrue(reply_parameters.allow_sending_without_reply)

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


class TestUnicodeRegression(unittest.TestCase):
    """Regression tests for non-ASCII title and author name handling.

    Before the Unicode fix, handlers.py used [a-z0-9]+ for token extraction,
    which only matched ASCII characters. This caused:
    - _author_hint_is_complete to reject non-Latin author names (empty token sets)
    - _discover_candidate to fail matching on CJK, Cyrillic, Arabic author names

    These tests verify the \\w+ fix (Unicode-aware word token extraction).
    """

    def test_author_hint_is_complete_english(self):
        """English author: token sets must match exactly."""
        from src.handlers import GoodreadsBot
        result = GoodreadsBot._author_hint_is_complete("sugaru miaki", "sugaru miaki")
        self.assertTrue(result)

    def test_author_hint_is_complete_english_partial(self):
        """Partial English hint: last token "sugaru" != last token "miaki" → not complete."""
        from src.handlers import GoodreadsBot
        result = GoodreadsBot._author_hint_is_complete("sugaru", "sugaru miaki")
        self.assertFalse(result)

    def test_author_hint_is_complete_native_japanese_script(self):
        """Non-ASCII Japanese author name (hiragana/kanji) must not return False.

        Before the fix, re.findall(r'[a-z0-9]+', '三秋縋') → []  → early-return False.
        With \\w+, tokens are extracted correctly and comparison proceeds.
        """
        from src.handlers import GoodreadsBot
        # Both identical → complete
        result = GoodreadsBot._author_hint_is_complete("三秋縋", "三秋縋")
        self.assertTrue(result)

    def test_author_hint_is_complete_native_japanese_partial(self):
        """Partial Japanese hint: '三秋' (2 tokens) vs '三秋縋' (3 tokens).
        Last tokens differ ('三秋' vs '三秋縋') so not complete. Confirms Unicode
        tokenisation works without crashing.
        """
        from src.handlers import GoodreadsBot
        result = GoodreadsBot._author_hint_is_complete("三秋", "三秋縋")
        self.assertFalse(result)

    def test_author_hint_is_complete_native_chinese(self):
        """Chinese author name in native hanzi must be accepted."""
        from src.handlers import GoodreadsBot
        result = GoodreadsBot._author_hint_is_complete("劉慈欣", "劉慈欣")
        self.assertTrue(result)

    def test_author_hint_is_complete_native_cyrillic(self):
        """Cyrillic: 'достоевский' vs 'фёдор достоевский' — hint has no initials,
        candidate has one. Returns False. Confirms Unicode tokenisation works.
        """
        from src.handlers import GoodreadsBot
        result = GoodreadsBot._author_hint_is_complete("достоевский", "фёдор достоевский")
        self.assertFalse(result)

    def test_author_hint_is_complete_empty_candidate(self):
        """When candidate author is empty, must return False (not crash)."""
        from src.handlers import GoodreadsBot
        result = GoodreadsBot._author_hint_is_complete("sugaru", "")
        self.assertFalse(result)


class TestDiscoverCandidateUnicode(unittest.TestCase):
    """_discover_candidate must handle non-ASCII author names via cross-script detection.

    When Google Books returns a native-script author (CJK, Cyrillic, etc.) in response
    to a Romanized author query, _discover_candidate's cross-script fix boosts
    author_score to 1.0 — trusting Google Books' own transliteration — so that
    results are not incorrectly rejected.
    """

    def _discover(self, query: str, title_hint: str | None, author_hint: str | None,
                  gb_volumes: list[dict]) -> dict | None:
        bot = make_bot()
        bot._STOPWORDS = frozenset({
            "the", "a", "an", "of", "in", "for", "with",
            "on", "at", "to", "by", "and", "or", "is", "are",
        })
        items = [
            {
                "id": f"vol{i}",
                "volumeInfo": {
                    "title": v["title"],
                    "authors": [v["author"]],
                    "language": "en",
                },
            }
            for i, v in enumerate(gb_volumes)
        ]
        empty_resp = MagicMock()
        empty_resp.status_code = 200
        empty_resp.json.return_value = {"items": []}
        match_resp = MagicMock()
        match_resp.status_code = 200
        match_resp.json.return_value = {"items": items}
        # Mock get_http_session to return a session whose .get() cycles through responses.
        mock_session = MagicMock()
        mock_session.get.side_effect = [empty_resp, match_resp, match_resp,
                                        match_resp, match_resp]
        with patch("src.handlers.get_http_session", return_value=mock_session):
            return bot._discover_candidate(query, title_hint, author_hint)

    def test_discover_with_native_japanese_author(self):
        """Native-script Japanese author matched via cross-script detection.

        'sugaru miaki' (ASCII) → GB finds '三秋縋' → cross_script=True →
        author_score boosted to 1.0 → title_score 1.0 → result accepted.
        """
        result = self._discover(
            query="Starting Over by Sugaru",
            title_hint="Starting Over",
            author_hint="sugaru miaki",
            gb_volumes=[
                {"title": "Starting Over 重啟人生", "author": "三秋縋"},
            ],
        )
        self.assertIsNotNone(result, "Cross-script author should be accepted")
        self.assertEqual(result["author"], "三秋縋")
        self.assertIn("Starting Over", result["title"])

    def test_discover_with_native_chinese_author(self):
        """Native-script Chinese author matched via cross-script detection."""
        result = self._discover(
            query="Three-Body Problem by Liu",
            title_hint="Three-Body Problem",
            author_hint="liu",
            gb_volumes=[
                # Include English in title so title_score >= 0.5; author_score comes
                # from cross-script detection (ASCII hint → non-ASCII result author).
                {"title": "The Three-Body Problem 三體", "author": "劉慈欣"},
            ],
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["author"], "劉慈欣")

    def test_discover_with_native_cyrillic_author(self):
        """Cyrillic author name matched via cross-script detection."""
        result = self._discover(
            query="Crime and Punishment by Dostoevsky",
            title_hint="Crime and Punishment",
            author_hint="dostoevsky",
            gb_volumes=[
                # Include English in title so title_score >= 0.5; author_score comes
                # from cross-script detection (ASCII hint → non-ASCII result author).
                {"title": "Crime and Punishment Преступление и наказание", "author": "Фёдор Михайлович Достоевский"},
            ],
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["author"], "Фёдор Михайлович Достоевский")

    def test_discover_with_mixed_script_title(self):
        """Mixed Latin/CJK title correctly tokenised and scored."""
        result = self._discover(
            query="Starting Over by Sugaru",
            title_hint="Starting Over",
            author_hint="sugaru miaki",
            gb_volumes=[
                {"title": "Starting Over 重啟人生", "author": "三秋縋"},
            ],
        )
        self.assertIsNotNone(result)
        # Title: 'starting' + 'over' in 'starting over 重啟人生' → 2/2 = 1.0
        # Author: cross-script → 1.0
        self.assertIn("Starting Over", result["title"])


if __name__ == "__main__":
    unittest.main()
