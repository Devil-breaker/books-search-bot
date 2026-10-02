"""Focused tests for Mini App discovery shelves."""

import unittest
from unittest.mock import AsyncMock, patch

from src.miniapp.service import MiniAppSearchService
from src.handlers import GoodreadsBot


def book(title, category, author="Test Author", **extra):
    return {
        "title": title,
        "author": author,
        "rating": 4.0,
        "rating_count": 20,
        "categories": [category],
        "cover_url": "https://example.test/cover.jpg",
        **extra,
    }


class MiniAppTrendingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = MiniAppSearchService(None, None)

    async def test_genre_tab_queries_hardcover_for_that_genre(self):
        fantasy = [book(f"Fantasy Book {i}", "Fantasy") for i in range(12)]

        with patch(
            "src.aggregator.MultiSourceBookAggregator.search_hardcover",
            return_value=fantasy,
        ) as search:
            result = await self.service.trending("Fantasy")

        self.assertEqual(search.call_args.args[0], "Fantasy")
        self.assertEqual(len(result["books"]), 12)
        self.assertTrue(all("Fantasy" in item["categories"] for item in result["books"]))

    async def test_sparse_scifi_tab_tries_alias_queries_and_combines_results(self):
        primary = [book(f"Science Fiction {i}", "Science Fiction") for i in range(4)]
        alias = [book(f"Sci-Fi {i}", {"name": "Science Fiction & Fantasy"}) for i in range(6)]

        with patch(
            "src.aggregator.MultiSourceBookAggregator.search_hardcover",
            side_effect=[primary, alias],
        ) as search:
            result = await self.service.trending("Sci-Fi")

        self.assertEqual([call.args[0] for call in search.call_args_list], ["Science Fiction", "Sci-Fi"])
        self.assertEqual(len(result["books"]), 10)

    async def test_genre_results_do_not_reuse_global_wildcard_candidate_cache(self):
        fantasy = [book(f"Fantasy Book {i}", "Fantasy") for i in range(10)]
        sci_fi = [book(f"Sci-Fi Book {i}", "Science Fiction") for i in range(10)]

        def results(query, *_args, **_kwargs):
            return fantasy if query == "Fantasy" else sci_fi

        with patch(
            "src.aggregator.MultiSourceBookAggregator.search_hardcover",
            side_effect=results,
        ) as search:
            fantasy_result = await self.service.trending("Fantasy")
            sci_fi_result = await self.service.trending("Sci-Fi")

        self.assertEqual(len(search.call_args_list), 2)
        self.assertEqual(fantasy_result["books"][0]["title"], "Fantasy Book 0")
        self.assertEqual(sci_fi_result["books"][0]["title"], "Sci-Fi Book 0")

    async def test_trending_shelf_starts_with_rolling_hardcover_results(self):
        current = [book(f"Recent Fantasy {i}", "Fantasy") for i in range(12)]

        with patch(
            "src.aggregator.MultiSourceBookAggregator.search_hardcover_trending",
            return_value=current,
        ) as trending, patch(
            "src.aggregator.MultiSourceBookAggregator.search_hardcover",
        ) as fallback:
            result = await self.service.trending("Fantasy")

        trending.assert_called_once_with(100, "month")
        fallback.assert_not_called()
        self.assertEqual([item["title"] for item in result["books"]], [
            f"Recent Fantasy {i}" for i in range(12)
        ])

    async def test_genre_filter_rejects_unrelated_categories(self):
        books = [
            book("Actual Sci-Fi", "Science Fiction"),
            book("Fantasy Book", "Fantasy"),
            book("Weakly Rated Sci-Fi", "Science Fiction", rating=3.0),
        ]

        filtered = self.service._filter_trending_books(books, "Sci-Fi")

        self.assertEqual([item["title"] for item in filtered], ["Actual Sci-Fi"])

    def test_related_books_can_fill_a_ten_book_shelf(self):
        books = [
            book("Selected Book", "Fantasy", author="Selected Author"),
            *[
                book(f"Fantasy Pick {index}", "Fantasy", author=f"Author {index}")
                for index in range(12)
            ],
        ]

        result = self.service._related_books(books, books[0], 0)

        self.assertEqual(len(result), 10)


class MiniAppSearchDeduplicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_miniapp_search_uses_shared_non_latin_deduplication(self):
        processor = object.__new__(GoodreadsBot)
        service = MiniAppSearchService(None, processor)
        duplicates = [
            book(
                "三日間の幸福", "Fiction", author="三秋縋",
                isbn="9781111111111", cover_url="", rating=0, rating_count=0,
            ),
            book(
                "三日間の幸福", "Fiction", author="三秋縋",
                isbn="9782222222222", cover_url="https://example.test/complete.jpg",
                rating=4.6, rating_count=250,
            ),
        ]
        processor._aggregate_search_results = AsyncMock(return_value=duplicates)

        results = await service._get_search_books("三日間の幸福")

        processor._aggregate_search_results.assert_awaited_once_with(
            "三日間の幸福", limit=10
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "三日間の幸福")
        self.assertEqual(results[0]["cover_url"], "https://example.test/complete.jpg")
        self.assertEqual(results[0]["rating_count"], 250)

    async def test_search_translation_returns_title_and_description_together(self):
        service = MiniAppSearchService(None, object.__new__(GoodreadsBot))
        service._set_cached("translated book", [
            book("三日間の幸福", "Fiction", description="日本語の説明です。")
        ])
        with patch("src.miniapp.service.is_english_description", return_value=False), patch(
            "src.miniapp.service.translate_to_english",
            side_effect=["Three Days of Happiness", "An English description."],
        ):
            result = await service.translate_description("translated book", 1, 0)

        self.assertEqual(result["title_translation"], "Three Days of Happiness")
        self.assertTrue(result["title_translated"])
        self.assertEqual(result["translation"], "An English description.")
        self.assertTrue(result["translated"])

    async def test_recommendation_translation_returns_title_and_description_together(self):
        service = MiniAppSearchService(None, None)
        with patch("src.miniapp.service.is_english_description", return_value=False), patch(
            "src.miniapp.service.translate_to_english",
            side_effect=["Three Days of Happiness", "An English description."],
        ):
            result = await service.translate_recommendation_description({
                "title": "三日間の幸福",
                "description": "日本語の説明です。",
            })

        self.assertEqual(result["title_translation"], "Three Days of Happiness")
        self.assertEqual(result["translation"], "An English description.")
        self.assertTrue(result["any_translated"])


class MiniAppRecommendationDetailTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = MiniAppSearchService(None, None)

    async def test_complete_recommendation_metadata_skips_google_lookup(self):
        raw_book = {
            "title": "Complete Book",
            "author": "An Author",
            "source": "hardcover",
            "description": "A complete description.",
            "isbn": "9780000000000",
            "page_count": 250,
            "published_date": "2020",
            "language": "English",
            "categories": ["Fantasy"],
        }

        with patch("src.aggregator.MultiSourceBookAggregator.search_google_books") as search:
            result = await self.service.recommendation_book_details(raw_book)

        search.assert_not_called()
        self.assertEqual(result["book"]["title"], "Complete Book")
        self.assertFalse(result["metadata_enriched"])

    async def test_google_books_source_is_not_queried_again(self):
        raw_book = {
            "title": "Google Book",
            "author": "An Author",
            "source": "google_books",
            "description": "Description",
        }

        with patch("src.aggregator.MultiSourceBookAggregator.search_google_books") as search:
            result = await self.service.recommendation_book_details(raw_book)

        search.assert_not_called()
        self.assertFalse(result["metadata_enriched"])

    async def test_incomplete_metadata_still_uses_google_books_as_a_fallback(self):
        raw_book = {
            "title": "Partial Book",
            "author": "An Author",
            "source": "hardcover",
            "description": "Description",
        }
        google_book = {
            "title": "Partial Book",
            "author": "An Author",
            "isbn": "9780000000000",
            "page_count": 250,
            "published_date": "2020",
            "language": "en",
            "categories": ["Fantasy"],
        }

        with patch(
            "src.aggregator.MultiSourceBookAggregator.search_google_books",
            return_value=[google_book],
        ) as search:
            result = await self.service.recommendation_book_details(raw_book)

        search.assert_called_once()
        self.assertEqual(result["book"]["isbn"], "9780000000000")
        self.assertTrue(result["metadata_enriched"])


if __name__ == "__main__":
    unittest.main()
