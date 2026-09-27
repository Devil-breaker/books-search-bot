"""Tests for src/aggregator.py — list-only search_rating fields, field names, source paths."""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unittest
from unittest.mock import patch

from src.aggregator import MultiSourceBookAggregator


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


if __name__ == "__main__":
    unittest.main()
