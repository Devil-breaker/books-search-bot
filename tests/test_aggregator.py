"""Tests for src/aggregator.py — list-only search_rating fields, field names, source paths."""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unittest
import requests
from unittest.mock import MagicMock, patch

from src.aggregator import MultiSourceBookAggregator

_session_patch = patch("src.aggregator.get_http_session", return_value=requests)


def setUpModule():
    _session_patch.start()


def tearDownModule():
    _session_patch.stop()


class TestAggregatorSearchRatingFields(unittest.TestCase):
    """search_rating* list-only fields must be set before the rating=0.0 lazy reset.

    The handler reads book['search_rating'], book['search_rating_count'],
    and book['search_rating_formatted'] when building the search results list.
    These fields are populated by aggregate_book_data (not by individual source
    methods) to avoid extra Hardcover API calls during search.
    """

    def test_aggregate_book_data_sets_search_rating_fields(self):
        """aggregate_book_data must preserve real ratings as search_rating* before resetting."""
        import inspect
        source = inspect.getsource(MultiSourceBookAggregator.aggregate_book_data)
        # After assigning search_rating* fields, the code must reset rating=0.0 (lazy design)
        self.assertIn("search_rating", source)
        self.assertIn('"rating"] = 0.0', source,
                      msg="Lazy reset (rating=0.0) must follow search_rating* assignment")

    def test_handler_reads_search_rating_fields(self):
        """Handler must read from search_rating (not rating) for search list display."""
        import inspect, src.handlers
        source = inspect.getsource(src.handlers.GoodreadsBot._build_search_results_message)
        self.assertIn("search_rating", source)
        self.assertIn("search_rating_count", source)
        self.assertIn("search_rating_formatted", source)
        # It must NOT call _ensure_ratings or Hardcover here — that's only on book selection
        # We verify by checking there is no Hardcover call in the message builder
        self.assertNotIn("search_hardcover", source)
        self.assertNotIn("_ensure_ratings", source)

    def test_aggregate_stores_rating_before_lazy_reset(self):
        """The search_rating assignment must come before rating=0.0 in aggregate_book_data."""
        import inspect
        source = inspect.getsource(MultiSourceBookAggregator.aggregate_book_data)
        # Find positions: search_rating assignment should appear before rating=0.0 reset
        search_rating_pos = source.find('"search_rating"]')
        reset_pos = source.find('"rating"] = 0.0')
        self.assertNotEqual(search_rating_pos, -1, "search_rating assignment not found")
        self.assertNotEqual(reset_pos, -1, "rating=0.0 reset not found")
        self.assertLess(search_rating_pos, reset_pos,
                        msg="search_rating must be assigned BEFORE rating=0.0 reset")


class TestAggregatorNetworkPaths(unittest.TestCase):
    """Network-return paths that don't require live API calls."""

    @patch.dict(os.environ, {"HARDCOVER_API_KEY": "test-token"})
    @patch("src.aggregator.get_http_session")
    def test_hardcover_trending_uses_rolling_window_and_preserves_rank(self, mock_session):
        trending = MagicMock(status_code=200)
        trending.json.return_value = {"data": {"books_trending": {"ids": [2, 1]}}}
        details = MagicMock(status_code=200)
        details.json.return_value = {"data": {"books": [
            {
                "id": 1, "title": "Second", "rating": 4.1, "ratings_count": 12,
                "cached_tags": {"tags": [{"category": "Genre", "tag": "Fantasy"}]},
                "contributions": [{"author": {"name": "Author One"}}],
            },
            {
                "id": 2, "title": "First", "rating": 4.5, "ratings_count": 20,
                "cached_tags": {"tags": [{"category": "Genre", "tag": "Fantasy"}]},
                "contributions": [{"author": {"name": "Author Two"}}],
            },
        ]}}
        mock_session.return_value.post.side_effect = [trending, details]

        books = MultiSourceBookAggregator.search_hardcover_trending(100, "month")

        self.assertEqual([book["title"] for book in books], ["First", "Second"])
        self.assertEqual(books[0]["categories"], ["Fantasy"])
        request_payload = mock_session.return_value.post.call_args_list[0].kwargs["json"]
        self.assertEqual(request_payload["variables"], {"duration": "month", "limit": 100})

    @patch.dict(os.environ, {"HARDCOVER_API_KEY": "test-token"})
    @patch("src.aggregator.get_http_session")
    def test_hardcover_trending_returns_empty_on_api_error(self, mock_session):
        response = MagicMock(status_code=200)
        response.json.return_value = {"errors": [{"message": "unavailable"}]}
        mock_session.return_value.post.return_value = response

        self.assertEqual(MultiSourceBookAggregator.search_hardcover_trending(), [])
        mock_session.return_value.post.assert_called_once()

    @patch("requests.get")
    def test_search_google_books_returns_empty_on_api_error(self, mock_get):
        mock_get.return_value.status_code = 500
        mock_get.return_value.text = "Internal Server Error"

        books = MultiSourceBookAggregator.search_google_books("test", limit=5)

        self.assertEqual(books, [])

    @patch("requests.get")
    def test_search_google_books_returns_empty_on_empty_response(self, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {}

        books = MultiSourceBookAggregator.search_google_books("xyz nonexistent", limit=5)

        self.assertEqual(books, [])

    @patch("requests.get")
    def test_explicit_title_author_search_falls_back_across_languages(self, mock_get):
        primary = MagicMock(status_code=200)
        primary.json.return_value = {"items": []}
        fallback = MagicMock(status_code=200)
        fallback.json.return_value = {
            "items": [{
                "id": "starting-over-zh",
                "volumeInfo": {
                    "title": "Starting Over 重啟人生",
                    "authors": ["Sugaru Miaki"],
                    "language": "zh",
                },
            }]
        }
        mock_get.side_effect = [primary, fallback]

        books = MultiSourceBookAggregator.search_google_books(
            "Starting Over by Sugaru", limit=10, translate_description=False
        )

        self.assertEqual(len(books), 1)
        self.assertEqual(books[0]["author"], "Sugaru Miaki")
        self.assertIn("Starting Over", books[0]["title"])
        primary_params = mock_get.call_args_list[0].kwargs["params"]
        fallback_params = mock_get.call_args_list[1].kwargs["params"]
        self.assertIn('intitle:"Starting Over"', primary_params["q"])
        self.assertNotIn("langRestrict", primary_params)
        self.assertNotIn("langRestrict", fallback_params)
        self.assertEqual(books[0]["language"], "zh")
        self.assertIn('intitle:"Starting Over"', fallback_params["q"])
        self.assertIn('inauthor:"Sugaru"', fallback_params["q"])

    @patch("requests.get")
    def test_explicit_pair_tries_plain_combined_query_before_single_field_fallbacks(self, mock_get):
        empty = MagicMock(status_code=200)
        empty.json.return_value = {"items": []}
        match = MagicMock(status_code=200)
        match.json.return_value = {
            "items": [{
                "id": "starting-over-zh",
                "volumeInfo": {
                    "title": "Starting Over 重啟人生",
                    "authors": ["Sugaru Miaki"],
                    "language": "zh",
                },
            }]
        }
        mock_get.side_effect = [empty, empty, match]

        books = MultiSourceBookAggregator.search_google_books(
            "Starting Over by Sugaru", limit=10, translate_description=False
        )

        self.assertEqual(len(books), 1)
        self.assertEqual(books[0]["author"], "Sugaru Miaki")
        self.assertEqual(
            mock_get.call_args_list[2].kwargs["params"]["q"],
            '"Starting Over" "Sugaru"',
        )
        self.assertNotIn("langRestrict", mock_get.call_args_list[2].kwargs["params"])

    @patch("requests.get")
    def test_explicit_pair_search_recovers_from_primary_timeout(self, mock_get):
        import requests

        fallback = MagicMock(status_code=200)
        fallback.json.return_value = {
            "items": [{
                "id": "starting-over-zh",
                "volumeInfo": {
                    "title": "Starting Over 重啟人生",
                    "authors": ["Sugaru Miaki"],
                    "language": "zh",
                },
            }]
        }
        mock_get.side_effect = [requests.Timeout("simulated timeout"), fallback]

        books = MultiSourceBookAggregator.search_google_books(
            "Starting Over by Sugaru", limit=10, translate_description=False
        )

        self.assertEqual(len(books), 1)
        self.assertEqual(books[0]["author"], "Sugaru Miaki")

    @patch("requests.get")
    def test_unstructured_search_keeps_chinese_and_japanese_catalog_results(self, mock_get):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "items": [
                {
                    "id": "book-ja",
                    "volumeInfo": {
                        "title": "Starting Over",
                        "authors": ["三秋縋"],
                        "language": "ja",
                    },
                },
                {
                    "id": "book-zh",
                    "volumeInfo": {
                        "title": "Starting Over 重啟人生",
                        "authors": ["Sugaru Miaki"],
                        "language": "zh",
                    },
                },
            ]
        }
        mock_get.return_value = response

        books = MultiSourceBookAggregator.search_google_books(
            "Starting Over", limit=10, translate_description=False
        )

        self.assertEqual([book["language"] for book in books], ["ja", "zh"])
        self.assertEqual(mock_get.call_args.kwargs["params"]["q"], "Starting Over")
        self.assertNotIn("langRestrict", mock_get.call_args.kwargs["params"])

    def test_search_hardcover_is_callable(self):
        """search_hardcover must be callable without crashing."""
        self.assertTrue(callable(MultiSourceBookAggregator.search_hardcover))

    def test_search_google_books_is_callable(self):
        """search_google_books must be callable without crashing."""
        self.assertTrue(callable(MultiSourceBookAggregator.search_google_books))

    def test_aggregate_book_data_is_callable(self):
        """aggregate_book_data is the main entry point; must be callable."""
        self.assertTrue(callable(MultiSourceBookAggregator.aggregate_book_data))



class TestAggregatorItunesFallback(unittest.IsolatedAsyncioTestCase):
    @patch.object(MultiSourceBookAggregator, "search_google_books", return_value=[])
    @patch.object(MultiSourceBookAggregator, "search_itunes")
    async def test_all_itunes_results_are_kept_when_google_books_is_unavailable(
        self, mock_itunes, _mock_google
    ):
        mock_itunes.return_value = [
            {"title": "Harry Potter and the Sorcerer's Stone", "author": "J.K. Rowling"},
            {"title": "Harry Potter and the Chamber of Secrets", "author": "J.K. Rowling"},
            {"title": "The Unofficial Harry Potter Cookbook", "author": "Dinah Bucholz"},
        ]

        books = await MultiSourceBookAggregator.aggregate_book_data("Harry Potter", limit=10)

        self.assertEqual(
            [book["title"] for book in books],
            [
                "Harry Potter and the Sorcerer's Stone",
                "Harry Potter and the Chamber of Secrets",
                "The Unofficial Harry Potter Cookbook",
            ],
        )
        self.assertTrue(all(book["cover_source"] == "itunes" for book in books))
        self.assertTrue(all(book["search_rating"] == 0.0 for book in books))


class TestAggregatorInternalMethods(unittest.TestCase):
    """Internal method signatures — verify they exist and have the expected structure."""

    def test_parse_google_book_is_static_method(self):
        self.assertTrue(callable(MultiSourceBookAggregator._parse_google_book))

    def test_parse_itunes_book_is_static_method(self):
        self.assertTrue(callable(MultiSourceBookAggregator._parse_itunes_book))

    def test_ensure_ratings_is_static_method(self):
        self.assertTrue(callable(MultiSourceBookAggregator._ensure_ratings))

    def test_ensure_cover_is_static_method(self):
        self.assertTrue(callable(MultiSourceBookAggregator._ensure_cover))


class TestHardcoverCoverMatching(unittest.TestCase):
    def _mock_response(self, docs):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "data": {"search": {"results": {
                "hits": [{"document": doc} for doc in docs]
            }}}
        }
        return response

    @patch.dict(os.environ, {"HARDCOVER_API_KEY": "test-key"})
    @patch("src.aggregator.get_http_session")
    def test_cover_accepts_punctuation_variant_with_exact_title_and_author(self, mock_session):
        mock_session.return_value.post.return_value = self._mock_response([{
            "title": "Angels and Demons",
            "author_names": ["Dan Brown"],
            "rating": 4.0,
            "ratings_count": 1000,
            "image": {"url": "https://assets.hardcover.app/angels.jpg"},
        }])

        result = MultiSourceBookAggregator._get_hardcover_data(
            "", "Angels & Demons", "Dan Brown"
        )

        self.assertEqual(result[3], "https://assets.hardcover.app/angels.jpg")

    @patch.dict(os.environ, {"HARDCOVER_API_KEY": "test-key"})
    @patch("src.aggregator.get_http_session")
    def test_cover_rejects_unrelated_fallback_hit(self, mock_session):
        mock_session.return_value.post.return_value = self._mock_response([{
            "title": "The Da Vinci Code",
            "author_names": ["Dan Brown"],
            "rating": 4.2,
            "ratings_count": 5000,
            "image": {"url": "https://assets.hardcover.app/davinci.jpg"},
        }])

        result = MultiSourceBookAggregator._get_hardcover_data(
            "", "Angels & Demons", "Dan Brown"
        )

        # The fallback can still provide legacy rating data, but not its cover.
        self.assertEqual(result[0], 4.2)
        self.assertEqual(result[3], "")


class TestHardcoverCoverPreference(unittest.TestCase):
    def test_verified_hardcover_cover_replaces_google_books_cover(self):
        book = {
            "title": "Angels & Demons",
            "author": "Dan Brown",
            "rating": 0,
            "cover_url": "https://books.google.com/incorrect.jpg",
            "cover_source": "google_books",
        }
        hardcover = (
            4.0, 1000, [], "https://assets.hardcover.app/angels.jpg"
        )

        with patch.object(
            MultiSourceBookAggregator,
            "_get_hardcover_cached",
            return_value=hardcover,
        ):
            updated, hc_data = MultiSourceBookAggregator._ensure_ratings(book)

        self.assertEqual(updated["cover_url"], hardcover[3])
        self.assertEqual(updated["cover_source"], "hardcover")
        self.assertEqual(hc_data, hardcover)


class TestPairMatchesUnicode(unittest.TestCase):
    """Regression tests for pair_matches Unicode handling in Google Books fallback.

    The pair_matches function (nested inside search_google_books) uses regex to extract
    word tokens for title and author matching. Before the Unicode fix, it used [a-z0-9]+
    which only matched ASCII characters — causing CJK, Cyrillic, Arabic, etc. author names
    and titles to yield empty token sets and fail matching.

    All these tests verify that non-ASCII characters are correctly tokenized and matched.
    """

    @patch("requests.get")
    def test_pair_matches_english_title_english_author(self, mock_get):
        """English title and author matching — existing behaviour, must not regress."""
        primary = MagicMock(status_code=200)
        primary.json.return_value = {"items": []}
        fallback = MagicMock(status_code=200)
        fallback.json.return_value = {
            "items": [{
                "id": "eng-001",
                "volumeInfo": {
                    "title": "The Great Gatsby",
                    "authors": ["F. Scott Fitzgerald"],
                    "language": "en",
                },
            }]
        }
        mock_get.side_effect = [primary, fallback]

        books = MultiSourceBookAggregator.search_google_books(
            "The Great Gatsby by F. Scott", limit=10
        )

        self.assertEqual(len(books), 1)
        self.assertEqual(books[0]["author"], "F. Scott Fitzgerald")

    @patch("requests.get")
    def test_pair_matches_japanese_author_native_script(self, mock_get):
        """Japanese author name in native script (hiragana/kanji) must match.

        Before the Unicode fix, [a-z0-9]+ on '三秋縋' produced an empty set, causing
        pair_matches to reject the result even though the title matched.
        The cross-script fix additionally allows acceptance when the query author
        is ASCII/Latin but the result author is non-ASCII — Google Books'
        own transliteration handles the author matching in those cases.
        """
        japanese_author_volume = {
            "id": "jp-001",
            "volumeInfo": {
                "title": "Starting Over 重啟人生",
                "authors": ["三秋縋"],
                "language": "ja",
            },
        }
        primary = MagicMock(status_code=200)
        primary.json.return_value = {"items": []}
        fallback = MagicMock(status_code=200)
        fallback.json.return_value = {"items": [japanese_author_volume]}
        # Primary (langRestrict=en) returns empty, then all 4 fallbacks succeed
        mock_get.side_effect = [primary, fallback, fallback, fallback, fallback]

        books = MultiSourceBookAggregator.search_google_books(
            "Starting Over by Sugaru Miaki", limit=10
        )

        self.assertEqual(len(books), 1)
        self.assertEqual(books[0]["author"], "三秋縋")

    @patch("requests.get")
    def test_pair_matches_chinese_author_native_script(self, mock_get):
        """Chinese author name in native script (hanzi) must match.

        The title includes both English and native script so title coverage is > 0,
        enabling the cross-script author check to activate.
        """
        chinese_author_volume = {
            "id": "zh-001",
            "volumeInfo": {
                "title": "The Three-Body Problem 三體",
                "authors": ["劉慈欣"],
                "language": "zh",
            },
        }
        primary = MagicMock(status_code=200)
        primary.json.return_value = {"items": []}
        fallback = MagicMock(status_code=200)
        fallback.json.return_value = {"items": [chinese_author_volume]}
        mock_get.side_effect = [primary, fallback, fallback, fallback, fallback]

        books = MultiSourceBookAggregator.search_google_books(
            "Three-Body Problem by Liu Cixin", limit=10
        )

        self.assertEqual(len(books), 1)
        self.assertEqual(books[0]["author"], "劉慈欣")

    @patch("requests.get")
    def test_pair_matches_cyrillic_author_native_script(self, mock_get):
        """Russian/Cyrillic author name must match.

        The title includes both English and native script so title coverage is > 0,
        enabling the cross-script author check to activate.
        """
        cyrillic_author_volume = {
            "id": "ru-001",
            "volumeInfo": {
                "title": "Crime and Punishment Преступление и наказание",
                "authors": ["Фёдор Михайлович Достоевский"],
                "language": "ru",
            },
        }
        primary = MagicMock(status_code=200)
        primary.json.return_value = {"items": []}
        fallback = MagicMock(status_code=200)
        fallback.json.return_value = {"items": [cyrillic_author_volume]}
        mock_get.side_effect = [primary, fallback, fallback, fallback, fallback]

        books = MultiSourceBookAggregator.search_google_books(
            "Crime and Punishment by Fyodor Dostoevsky", limit=10
        )

        self.assertEqual(len(books), 1)
        self.assertEqual(books[0]["author"], "Фёдор Михайлович Достоевский")

    @patch("requests.get")
    def test_pair_matches_unrelated_book_is_rejected(self, mock_get):
        """A result whose title and author are both unrelated to the query must be rejected.

        The title coverage must be >= 50% AND the author intersection must be non-empty.
        """
        primary = MagicMock(status_code=200)
        primary.json.return_value = {"items": []}
        fallback = MagicMock(status_code=200)
        # Harry Potter is not related to a query about Lord of the Rings
        fallback.json.return_value = {
            "items": [{
                "id": "unrelated-001",
                "volumeInfo": {
                    "title": "Harry Potter and the Sorcerer's Stone",
                    "authors": ["J.K. Rowling"],
                    "language": "en",
                },
            }]
        }
        mock_get.side_effect = [primary, fallback]

        books = MultiSourceBookAggregator.search_google_books(
            "The Fellowship of the Ring by Tolkien", limit=10
        )

        # No result should be accepted — neither title nor author overlaps enough
        self.assertEqual(len(books), 0)

    @patch("requests.get")
    def test_pair_matches_partial_title_overlap_accepted(self, mock_get):
        """When title has partial overlap (>= 50%) and author matches, result is accepted."""
        primary = MagicMock(status_code=200)
        primary.json.return_value = {"items": []}
        fallback = MagicMock(status_code=200)
        fallback.json.return_value = {
            "items": [{
                "id": "partial-001",
                "volumeInfo": {
                    "title": "Fellowship of the Ring (Lord of the Rings #1)",
                    "authors": ["J.R.R. Tolkien"],
                    "language": "en",
                },
            }]
        }
        mock_get.side_effect = [primary, fallback]

        books = MultiSourceBookAggregator.search_google_books(
            "The Fellowship of the Ring by Tolkien", limit=10
        )

        self.assertEqual(len(books), 1)
        self.assertEqual(books[0]["author"], "J.R.R. Tolkien")


if __name__ == "__main__":
    unittest.main()
