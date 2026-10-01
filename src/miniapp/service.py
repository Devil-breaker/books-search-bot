"""Mini App book-search service, separate from Telegram message formatting."""

from __future__ import annotations

import asyncio
import copy
import re
import threading
import time

from src.utils import is_english_description, logger, translate_to_english
from .recommendations import BookRecommendationEngine


class MiniAppSearchService:
    """Expose existing provider aggregation and result ranking to web clients."""

    CACHE_TTL_SECONDS = 120
    CACHE_MAX_ENTRIES = 64
    PAGE_SIZE = 5
    TRENDING_CACHE_TTL_SECONDS = 3600
    TRENDING_LIMIT = 20
    TRENDING_TARGET_SIZE = 10
    RELATED_LIMIT = 10
    TRENDING_GENRES = ("All", "Fantasy", "Romance", "Mystery", "Thriller", "Sci-Fi", "Horror", "Classics")
    TRENDING_SEARCH_TERMS = {
        "All": ("*",),
        "Fantasy": ("Fantasy",),
        "Romance": ("Romance",),
        "Mystery": ("Mystery", "Detective fiction"),
        "Thriller": ("Thriller", "Suspense"),
        "Sci-Fi": ("Science Fiction", "Sci-Fi", "Science-Fiction"),
        "Horror": ("Horror",),
        "Classics": ("Classics", "Classic literature"),
    }

    def __init__(self, aggregator, result_processor):
        self.aggregator = aggregator
        # GoodreadsBot's deduplication and ranking helpers are pure with respect
        # to bot state. Reuse them so the Mini App and /search order results the
        # same way without routing HTTP requests through Telegram handlers.
        self.result_processor = result_processor
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self._trending_cache: dict[str, tuple[float, list[dict]]] = {}
        self._trending_source_cache: dict[str, tuple[float, list[dict]]] = {}
        self._related_cache: dict[str, tuple[float, list[dict]]] = {}
        self._cache_lock = threading.Lock()
        self.recommendation_engine = BookRecommendationEngine()

    async def recommend_books(self, preferences: object) -> dict:
        """Run the standalone recommendation module without changing search flows."""
        return await self.recommendation_engine.recommend(preferences)

    async def trending(self, genre: str) -> dict:
        """Return genre-specific Hardcover picks for the Mini App discovery shelf."""
        if genre not in self.TRENDING_GENRES:
            raise ValueError("invalid_genre")

        cache_key = f"topbooks:{genre}"
        now = time.time()
        with self._cache_lock:
            cached = self._trending_cache.get(genre)
            cached_ttl = self.TRENDING_CACHE_TTL_SECONDS if cached and cached[1] else 300
            if cached and now - cached[0] < cached_ttl:
                books = copy.deepcopy(cached[1])
            else:
                self._trending_cache.pop(genre, None)
                books = None

        if books is None:
            from src.aggregator import MultiSourceBookAggregator

            started = time.perf_counter()
            recent_cache_key = "activity:month"
            with self._cache_lock:
                recent_cache = self._trending_source_cache.get(recent_cache_key)
                recent_candidates = (
                    copy.deepcopy(recent_cache[1])
                    if recent_cache and now - recent_cache[0] < self.TRENDING_CACHE_TTL_SECONDS
                    else None
                )
            if recent_candidates is None:
                recent_candidates = await asyncio.to_thread(
                    MultiSourceBookAggregator.search_hardcover_trending,
                    100,
                    "month",
                )
                if not isinstance(recent_candidates, list):
                    recent_candidates = []
                with self._cache_lock:
                    self._trending_source_cache[recent_cache_key] = (
                        time.time(), copy.deepcopy(recent_candidates)
                    )

            # Start with Hardcover's rolling one-month activity ranking. If a
            # genre has too few recent hits, broaden it with genre-filtered
            # catalog search, sorting that fallback by current activity.
            candidates = list(recent_candidates)
            books = self._filter_trending_books(candidates, genre)
            if len(books) < self.TRENDING_TARGET_SIZE:
                for term in self.TRENDING_SEARCH_TERMS[genre]:
                    batch = await asyncio.to_thread(
                        MultiSourceBookAggregator.search_hardcover,
                        term,
                        100,
                        sort="activities_count:desc",
                    )
                    if isinstance(batch, list):
                        candidates.extend(batch)
                    books = self._filter_trending_books(candidates, genre)
                    if len(books) >= self.TRENDING_TARGET_SIZE:
                        break
            if books:
                with self._cache_lock:
                    self._trending_cache[genre] = (time.time(), copy.deepcopy(books))
                self._set_cached(cache_key, books)
            logger.info(
                "[miniapp] top books genre=%s elapsed_ms=%d candidates=%d results=%d",
                genre,
                round((time.perf_counter() - started) * 1000),
                len(candidates),
                len(books),
            )
        elif books:
            self._set_cached(cache_key, books)

        return {
            "genre": genre,
            "query": cache_key,
            "books": [self._public_book(book) for book in books],
        }

    def _filter_trending_books(self, candidates: list[dict], genre: str) -> list[dict]:
        wanted_genre = self._normalize_genre(genre)
        filtered: list[dict] = []
        seen: set[tuple[str, str]] = set()

        for candidate in candidates:
            title = str(candidate.get("title") or "").strip()
            author = str(candidate.get("author") or "").strip()
            categories = candidate.get("categories") or []
            if isinstance(categories, (str, dict)):
                categories = [categories]
            if not isinstance(categories, list):
                categories = []
            if not title or not author:
                continue
            if genre != "All" and not any(
                self._genre_category_match(wanted_genre, self._category_name(category))
                for category in categories
                if isinstance(category, (str, dict))
            ):
                continue

            rating = float(candidate.get("rating") or 0)
            if rating and rating < 3.5:
                continue
            key = (self._normalize_genre(title), self._normalize_genre(author))
            if key in seen:
                continue
            seen.add(key)
            filtered.append(candidate)
        return filtered[: self.TRENDING_LIMIT]

    @staticmethod
    def _category_name(category) -> str:
        if isinstance(category, dict):
            return str(category.get("name") or category.get("title") or "")
        return str(category or "")

    @classmethod
    def _genre_category_match(cls, wanted_genre: str, category: str) -> bool:
        """Match common provider labels without treating unrelated tags as genres."""
        normalized = cls._normalize_genre(category)
        if not normalized:
            return False
        aliases = {
            "fantasy": ("fantasy", "fantasy fiction"),
            "romance": ("romance", "romantic fiction"),
            "mystery": ("mystery", "detective fiction", "crime fiction"),
            "thriller": ("thriller", "suspense"),
            "science fiction": ("science fiction", "science fiction fantasy", "sci fi"),
            "horror": ("horror", "horror fiction"),
            "classics": ("classic", "classics", "classic literature"),
        }.get(wanted_genre, (wanted_genre,))
        return any(
            normalized == alias
            or normalized.startswith(alias + " ")
            or (wanted_genre == "classics" and "classic" in normalized)
            for alias in aliases
        )

    @staticmethod
    def _normalize_genre(value: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()
        if normalized in {"sci fi", "science fiction"}:
            return "science fiction"
        return normalized

    async def search(self, query: str, page: int = 1) -> dict:
        """Search with the inline provider strategy, then page the ranked results."""
        books = await self._get_search_books(query)
        return self._page_result(query, page, books)

    async def book_details(self, query: str, page: int, index: int) -> dict | None:
        """Add Google Books metadata only after a user selects a suggestion."""
        cache_key = query.casefold().strip()
        books = self._get_cached(cache_key)
        if books is None:
            return None
        book_index = (page - 1) * self.PAGE_SIZE + index
        if not 0 <= index < self.PAGE_SIZE or book_index >= len(books):
            return None

        book = books[book_index]
        enriched = book.get("source") == "google_books"
        if not enriched:
            from src.aggregator import MultiSourceBookAggregator

            title = (book.get("title") or "").replace('"', " ").strip()
            author = (book.get("author") or "").replace('"', " ").strip()
            google_query = f'intitle:"{title}"'
            if author and author.casefold() not in {"unknown", "unknown author"}:
                google_query += f' inauthor:"{author}"'
            try:
                candidates = await asyncio.to_thread(
                    MultiSourceBookAggregator.search_google_books,
                    google_query, 5, False,
                )
                match = MultiSourceBookAggregator._find_matching_book_strict(
                    title, author, candidates
                )
            except Exception:
                logger.exception("[miniapp] Google Books detail lookup failed")
                match = None

            if match:
                # Prefer Google's fuller description. Keep Hardcover/iTunes
                # cover and rating choices intact; fill other missing metadata.
                if match.get("description"):
                    description = match["description"]
                    book["description"] = description
                for field in ("isbn", "page_count", "published_date", "language", "info_link"):
                    if not book.get(field) and match.get(field):
                        book[field] = match[field]
                if not book.get("categories") and match.get("categories"):
                    book["categories"] = match["categories"]
                book["metadata_source"] = "Google Books"
                enriched = True
                self._set_cached(cache_key, books)

        description = str(book.get("description") or "")
        public_book = self._public_book(book)
        public_book["description_needs_translation"] = bool(
            description and not is_english_description(description)
        )
        public_book["title_needs_translation"] = bool(
            public_book["title"] and not is_english_description(public_book["title"])
        )
        return {
            "book": public_book,
            "metadata_enriched": enriched,
        }

    async def recommendation_book_details(self, raw_book: object) -> dict:
        """Enrich a selected recommendation without relying on search-page state."""
        if not isinstance(raw_book, dict):
            raise ValueError("invalid_book")
        title = str(raw_book.get("title") or "").strip()[:250]
        author = str(raw_book.get("author") or "").strip()[:250]
        if not title:
            raise ValueError("invalid_book")

        categories = raw_book.get("categories") or []
        if isinstance(categories, str):
            categories = [categories]
        if not isinstance(categories, list):
            categories = []
        categories = [str(value).strip()[:120] for value in categories[:10] if str(value).strip()]
        try:
            page_count = int(raw_book.get("page_count") or 0)
        except (TypeError, ValueError):
            page_count = 0
        book = {
            "title": title,
            "author": author or "Unknown author",
            "cover_url": str(raw_book.get("cover_url") or "")[:2000],
            "description": str(raw_book.get("description") or "")[:20000],
            "rating": raw_book.get("rating") or 0,
            "rating_count": raw_book.get("rating_count") or 0,
            "categories": categories,
            "isbn": str(raw_book.get("isbn") or "")[:30],
            "page_count": max(0, page_count),
            "published_date": str(raw_book.get("published_date") or "")[:40],
            "language": str(raw_book.get("language") or "")[:40],
            "info_link": str(raw_book.get("info_link") or "")[:2000],
            "source": str(raw_book.get("source") or "")[:80],
        }
        enriched = False
        google_already_supplied = book["source"].casefold().replace(" ", "_") == "google_books"
        needs_metadata = not all((
            book["description"], book["isbn"], book["page_count"],
            book["published_date"], book["language"], book["categories"],
        ))
        if needs_metadata and not google_already_supplied:
            try:
                from src.aggregator import MultiSourceBookAggregator

                google_query = f'{title} by {author}' if author and author != "Unknown author" else title
                candidates = await asyncio.to_thread(
                    MultiSourceBookAggregator.search_google_books,
                    google_query, 5, False,
                )
                match = MultiSourceBookAggregator._find_matching_book_strict(title, author, candidates)
                if match:
                    if match.get("description"):
                        book["description"] = match["description"]
                    for field in ("isbn", "page_count", "published_date", "language", "info_link"):
                        if not book.get(field) and match.get(field):
                            book[field] = match[field]
                    if not book.get("categories") and match.get("categories"):
                        book["categories"] = match["categories"]
                    book["metadata_source"] = "Google Books"
                    enriched = True
            except Exception:
                logger.exception("[miniapp] recommendation Google Books detail lookup failed")

        description = str(book.get("description") or "")
        public_book = self._public_book(book)
        public_book["description_needs_translation"] = bool(
            description and not is_english_description(description)
        )
        public_book["title_needs_translation"] = bool(
            public_book["title"] and not is_english_description(public_book["title"])
        )
        return {"book": public_book, "metadata_enriched": enriched}

    async def translate_recommendation_description(self, raw_book: object) -> dict:
        """Translate title and description from a recommendation detail on demand."""
        if not isinstance(raw_book, dict):
            raise ValueError("invalid_book")
        title = str(raw_book.get("title") or "")[:250]
        description = str(raw_book.get("description") or "")[:20000]
        title_translation, description_translation = await self._translate_book_fields(
            title, description
        )
        title_translated = bool(title_translation and title_translation != title)
        description_translated = bool(
            description_translation and description_translation != description
        )
        return {
            "title_translation": title_translation or title,
            "title_translated": title_translated,
            "translation": description_translation or description,
            "translated": description_translated,
            "any_translated": title_translated or description_translated,
        }

    async def translate_description(self, query: str, page: int, index: int) -> dict | None:
        """Translate one selected description only after an explicit user action."""
        cache_key = query.casefold().strip()
        books = self._get_cached(cache_key)
        if books is None:
            return None
        book_index = (page - 1) * self.PAGE_SIZE + index
        if not 0 <= index < self.PAGE_SIZE or book_index >= len(books):
            return None

        book = books[book_index]
        title = str(book.get("title") or "")[:250]
        description = str(book.get("description") or "")[:20000]
        title_translation = str(book.get("translated_title") or "")
        description_translation = str(book.get("translated_description") or "")
        if not title_translation or (description and not description_translation):
            new_title, new_description = await self._translate_book_fields(
                title if not title_translation else "",
                description if not description_translation else "",
            )
            if new_title and new_title != title:
                title_translation = new_title
                book["translated_title"] = new_title
            if new_description and new_description != description:
                description_translation = new_description
                book["translated_description"] = new_description
            self._set_cached(cache_key, books)
        return {
            "title_translation": title_translation or title,
            "title_translated": bool(title_translation and title_translation != title),
            "translation": description_translation or description,
            "translated": bool(description_translation and description_translation != description),
            "any_translated": bool(
                (title_translation and title_translation != title)
                or (description_translation and description_translation != description)
            ),
        }

    @staticmethod
    async def _translate_book_fields(title: str, description: str) -> tuple[str, str]:
        """Translate only non-English fields concurrently, returning each original on failure."""
        values = {"title": title, "description": description}
        tasks = {}
        for field, value in values.items():
            if value and not is_english_description(value):
                tasks[field] = asyncio.to_thread(translate_to_english, value)
        if tasks:
            names = list(tasks)
            results = await asyncio.gather(*(tasks[name] for name in names), return_exceptions=True)
            for name, result in zip(names, results):
                if not isinstance(result, Exception) and result:
                    values[name] = str(result).strip()
        return values["title"], values["description"]

    async def related_books(self, query: str, page: int, index: int) -> list[dict] | None:
        """Find similar books from current results, then cached Hardcover searches."""
        cache_key = query.casefold().strip()
        books = self._get_cached(cache_key)
        if books is None:
            return None
        book_index = (page - 1) * self.PAGE_SIZE + index
        if not 0 <= index < self.PAGE_SIZE or book_index >= len(books):
            return None

        selected = books[book_index]
        related = [item["book"] for item in self._related_books(books, selected, book_index)]
        provider_books = await self.related_books_for_book(selected)
        return self._merge_related_books(related, provider_books)[: self.RELATED_LIMIT]

    async def related_books_for_book(self, selected: dict) -> list[dict]:
        """Fetch related books from selected-book metadata, independent of search cache."""
        if not isinstance(selected, dict) or not str(selected.get("title") or "").strip():
            return []
        selected_key = "genre-v2|" + "|".join((
            str(selected.get("isbn") or ""),
            re.sub(r"[^a-z0-9]+", " ", str(selected.get("title") or "").casefold()).strip(),
            re.sub(r"[^a-z0-9]+", " ", str(selected.get("author") or "").casefold()).strip(),
        ))
        now = time.time()
        with self._cache_lock:
            cached = self._related_cache.get(selected_key)
            cached_ttl = self.TRENDING_CACHE_TTL_SECONDS if cached and cached[1] else 300
            if cached and now - cached[0] < cached_ttl:
                provider_books = copy.deepcopy(cached[1])
            else:
                self._related_cache.pop(selected_key, None)
                provider_books = None

        if provider_books is None:
            try:
                from src.aggregator import MultiSourceBookAggregator

                author = str(selected.get("author") or "").strip()
                if author.casefold() in {"unknown", "unknown author"}:
                    author = ""
                category_terms = []
                generic_categories = {"fiction", "nonfiction", "general", "books", "book", "literature"}
                raw_categories = selected.get("categories") or []
                if isinstance(raw_categories, str):
                    raw_categories = [raw_categories]
                for category in raw_categories:
                    if isinstance(category, dict):
                        category = category.get("name") or category.get("title") or ""
                    for term in re.split(r"\s*(?:/|>|;)\s*", str(category or "")):
                        term = term.strip()
                        normalized_term = re.sub(r"[^a-z0-9]+", " ", term.casefold()).strip()
                        if (normalized_term and normalized_term not in generic_categories
                                and normalized_term not in {item.casefold() for item in category_terms}):
                            category_terms.append(term)
                    if len(category_terms) >= 2:
                        break

                hardcover_term = category_terms[0] if category_terms else author
                google_term = category_terms[0] if category_terms else author
                requests = []
                if hardcover_term:
                    requests.append(("hardcover", asyncio.to_thread(
                        MultiSourceBookAggregator.search_hardcover,
                        hardcover_term,
                        30,
                        sort="users_read_count:desc",
                    )))
                if google_term:
                    google_query = (
                        f'subject:"{google_term}"' if category_terms
                        else f'inauthor:"{google_term}"'
                    )
                    requests.append(("google_books", asyncio.to_thread(
                        MultiSourceBookAggregator.search_google_books,
                        google_query,
                        30,
                        False,
                    )))
                itunes_term = author or str(selected.get("title") or "").strip()
                if itunes_term:
                    requests.append(("itunes", asyncio.to_thread(
                        MultiSourceBookAggregator.search_itunes,
                        itunes_term,
                    )))

                provider_candidates: dict[str, list[dict]] = {}
                if requests:
                    results = await asyncio.gather(
                        *(request_task for _provider, request_task in requests),
                        return_exceptions=True,
                    )
                    for (provider, _request_task), batch in zip(requests, results):
                        if isinstance(batch, Exception):
                            logger.warning(
                                "[miniapp] related provider failed provider=%s error=%s",
                                provider, type(batch).__name__,
                            )
                        elif isinstance(batch, list):
                            logger.info(
                                "[miniapp] related provider=%s results=%d",
                                provider, len(batch),
                            )
                            provider_candidates[provider] = batch

                # Keep genre-matched Hardcover results first, then fill from
                # Google Books. iTunes has no dependable genre field, so its
                # hits are recommendation candidates only when the selected
                # book itself has no usable genre metadata.
                provider_groups = []
                for provider in ("hardcover", "google_books", "itunes"):
                    if provider == "itunes" and category_terms:
                        continue
                    batch = provider_candidates.get(provider, [])
                    matches = [
                        item["book"]
                        for item in self._related_books([selected, *batch], selected, 0)
                    ]
                    provider_groups.append(matches)
                itunes_candidates = provider_candidates.get("itunes", [])
                if itunes_candidates:
                    for group in provider_groups:
                        for candidate in group:
                            if candidate.get("cover_url"):
                                continue
                            match = MultiSourceBookAggregator._find_matching_book_strict(
                                candidate.get("title", ""), candidate.get("author", ""), itunes_candidates
                            )
                            if match and match.get("cover_url"):
                                candidate["cover_url"] = match["cover_url"]
                provider_books = self._merge_related_books(*provider_groups)[: self.RELATED_LIMIT]
                with self._cache_lock:
                    if len(self._related_cache) >= 128:
                        oldest = min(self._related_cache, key=lambda item: self._related_cache[item][0])
                        self._related_cache.pop(oldest, None)
                    self._related_cache[selected_key] = (time.time(), copy.deepcopy(provider_books))
            except Exception:
                # A provider or malformed record must not hide local matches or
                # break the selected book details view.
                logger.exception("[miniapp] related-book fallback failed")
                provider_books = []

        return self._merge_related_books([], provider_books or [])[: self.RELATED_LIMIT]

    @staticmethod
    def _merge_related_books(*groups: list[dict]) -> list[dict]:
        def normalize(value: object) -> str:
            return re.sub(r"[^\w]+", " ", str(value or "").casefold(), flags=re.UNICODE).strip()

        merged = []
        for group in groups:
            for book in group:
                title = normalize(book.get("title"))
                isbn = re.sub(r"[^0-9xX]", "", str(book.get("isbn") or "")).casefold()
                if not title:
                    continue
                duplicate = False
                for existing in merged:
                    existing_isbn = re.sub(r"[^0-9xX]", "", str(existing.get("isbn") or "")).casefold()
                    if isbn and existing_isbn and isbn == existing_isbn:
                        duplicate = True
                        break
                    if title != normalize(existing.get("title")):
                        continue
                    author = normalize(book.get("author"))
                    existing_author = normalize(existing.get("author"))
                    if not author or not existing_author or author == existing_author:
                        duplicate = True
                        break
                    author_tokens = set(author.split())
                    existing_tokens = set(existing_author.split())
                    if author_tokens & existing_tokens and min(len(author_tokens), len(existing_tokens)) <= 2:
                        duplicate = True
                        break
                if duplicate:
                    continue
                merged.append(book)
        return merged

    def _related_books(self, books: list[dict], selected: dict, selected_index: int) -> list[dict]:
        """Find relevant alternatives in the already-fetched result set."""
        def clean(value: object) -> str:
            return re.sub(r"[^\w]+", " ", str(value or "").casefold(), flags=re.UNICODE).strip()

        selected_title = clean(selected.get("title"))
        selected_author = clean(selected.get("author"))
        if selected_author in {"", "unknown", "unknown author"}:
            selected_author = ""

        def categories(book: dict) -> set[str]:
            values = set()
            raw_categories = book.get("categories") or []
            if isinstance(raw_categories, (str, dict)):
                raw_categories = [raw_categories]
            if not isinstance(raw_categories, (list, tuple)):
                return values
            generic = {"fiction", "nonfiction", "general", "books", "book", "literature", "edition", "editions"}
            for category in raw_categories:
                if isinstance(category, dict):
                    category = category.get("name") or category.get("title") or ""
                for segment in re.split(r"\s*(?:/|>|;)\s*", str(category or "")):
                    normalized = clean(segment)
                    values.update(word for word in normalized.split() if word not in generic and len(word) > 2)
            return values

        selected_categories = categories(selected)
        scored = []
        for candidate_index, candidate in enumerate(books):
            if candidate_index == selected_index:
                continue
            title = clean(candidate.get("title"))
            author = clean(candidate.get("author"))
            if title == selected_title and (not selected_author or author == selected_author):
                continue

            shared_author_terms = {
                token for token in selected_author.split()
                if len(token) > 2
            } & {token for token in author.split() if len(token) > 2}
            same_author = bool(selected_author and (author == selected_author or shared_author_terms))
            shared_categories = selected_categories & categories(candidate)
            if selected_categories and not shared_categories:
                continue
            if not selected_categories and not same_author:
                continue

            try:
                rating_count = int(candidate.get("rating_count") or candidate.get("search_rating_count") or 0)
            except (TypeError, ValueError):
                rating_count = 0
            scored.append((5 * len(shared_categories) + same_author, rating_count, candidate_index, candidate))

        scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
        related = []
        for _score, _rating_count, candidate_index, candidate in scored[: self.RELATED_LIMIT]:
            related.append({
                "book": self._public_book(candidate),
                "page": candidate_index // self.PAGE_SIZE + 1,
                "index": candidate_index % self.PAGE_SIZE,
            })
        return related

    async def _get_search_books(self, query: str) -> list[dict]:
        cache_key = query.casefold().strip()
        books = self._get_cached(cache_key)
        if books is None:
            started = time.perf_counter()
            books, itunes_books = await self._search_fast_sources(query)
            if not books:
                # Preserve catalog coverage for titles missing from Hardcover.
                # iTunes was already queried, so only call Google Books here.
                from src.aggregator import MultiSourceBookAggregator

                google_books = await asyncio.to_thread(
                    MultiSourceBookAggregator.search_google_books, query, 10, False
                )
                books = self._merge_google_fallback(google_books, itunes_books)
            books = self.result_processor._rank_search_results(
                self.result_processor._deduplicate_search_results(books, query), query
            )
            self._set_cached(cache_key, books)
            logger.info(
                "[miniapp] search completed elapsed_ms=%d results=%d",
                round((time.perf_counter() - started) * 1000), len(books),
            )
        return books

    async def _search_fast_sources(self, query: str) -> tuple[list[dict], list[dict]]:
        """Fetch Hardcover and iTunes together, like inline search does."""
        from src.aggregator import MultiSourceBookAggregator

        async def timed_provider(name: str, function, *args) -> list[dict]:
            started = time.perf_counter()
            try:
                result = await asyncio.to_thread(function, *args)
                return result if isinstance(result, list) else []
            finally:
                logger.info(
                    "[miniapp] provider=%s elapsed_ms=%d",
                    name, round((time.perf_counter() - started) * 1000),
                )

        hardcover, itunes = await asyncio.gather(
            timed_provider("hardcover", MultiSourceBookAggregator.search_hardcover, query, 10),
            timed_provider("itunes", MultiSourceBookAggregator.search_itunes, query),
        )
        if not hardcover:
            return [], itunes

        merged: list[dict] = []
        matched_itunes: set[int] = set()
        for hardcover_book in hardcover:
            book = dict(hardcover_book)
            for index, itunes_book in enumerate(itunes):
                if index in matched_itunes:
                    continue
                if self.result_processor._inline_title_author_match(
                    book.get("title", ""), book.get("author", ""),
                    itunes_book.get("title", ""), itunes_book.get("author", ""),
                ):
                    if itunes_book.get("cover_url") and not book.get("cover_url"):
                        book["cover_url"] = itunes_book["cover_url"]
                    matched_itunes.add(index)
                    break
            merged.append(book)

        # Keep useful iTunes-only suggestions, matching inline search behavior.
        merged.extend(
            dict(book) for index, book in enumerate(itunes) if index not in matched_itunes
        )
        return merged, itunes

    @staticmethod
    def _merge_google_fallback(google_books: list[dict], itunes_books: list[dict]) -> list[dict]:
        """Build a normal-search-style fallback without repeating provider calls."""
        from src.aggregator import MultiSourceBookAggregator

        if not google_books:
            return [dict(book) for book in itunes_books[:10]]

        books = []
        for google_book in google_books[:10]:
            book = dict(google_book)
            match = MultiSourceBookAggregator._find_matching_book_strict(
                book.get("title", ""), book.get("author", ""), itunes_books
            )
            if match and match.get("cover_url"):
                book["cover_url"] = match["cover_url"]
                book["cover_source"] = "itunes"
            book.setdefault("search_rating", book.get("rating", 0.0))
            book.setdefault("search_rating_count", book.get("rating_count", 0))
            books.append(book)
        return books

    def _page_result(self, query: str, page: int, books: list[dict]) -> dict:
        total = len(books)
        start = (page - 1) * self.PAGE_SIZE
        visible = copy.deepcopy(books[start : start + self.PAGE_SIZE])
        return {
            "query": query,
            "page": page,
            "page_size": self.PAGE_SIZE,
            "total": total,
            "has_more": start + len(visible) < total,
            "books": [self._public_book(book) for book in visible],
        }

    @staticmethod
    def _public_book(book: dict) -> dict:
        """Return only JSON-safe book fields used by search/details screens."""
        rating = book.get("search_rating") or book.get("rating") or 0
        rating_count = book.get("search_rating_count") or book.get("rating_count") or 0
        try:
            rating = float(str(rating).replace(",", ""))
        except (TypeError, ValueError):
            rating = 0.0
        try:
            rating_count = int(float(str(rating_count).replace(",", "")))
        except (TypeError, ValueError):
            rating_count = 0
        categories = book.get("categories") or []
        if isinstance(categories, str):
            categories = [categories]
        elif not isinstance(categories, (list, tuple)):
            categories = []
        return {
            "title": str(book.get("title") or ""),
            "author": str(book.get("author") or ""),
            "cover_url": str(book.get("cover_url") or ""),
            "description": str(book.get("description") or ""),
            "rating": rating,
            "rating_count": rating_count,
            "categories": list(categories),
            "isbn": str(book.get("isbn") or ""),
            "page_count": int(book.get("page_count") or 0),
            "published_date": str(book.get("published_date") or ""),
            "language": str(book.get("language") or ""),
            "info_link": str(book.get("info_link") or ""),
            "source": str(book.get("source") or ""),
            "metadata_source": str(book.get("metadata_source") or ""),
        }

    def _get_cached(self, key: str) -> list[dict] | None:
        now = time.time()
        with self._cache_lock:
            entry = self._cache.get(key)
            if entry and now - entry[0] <= self.CACHE_TTL_SECONDS:
                return copy.deepcopy(entry[1])
            self._cache.pop(key, None)
        return None

    def _set_cached(self, key: str, books: list[dict]) -> None:
        if not books:
            return
        now = time.time()
        with self._cache_lock:
            for old_key, (created_at, _) in list(self._cache.items()):
                if now - created_at > self.CACHE_TTL_SECONDS:
                    self._cache.pop(old_key, None)
            if key not in self._cache and len(self._cache) >= self.CACHE_MAX_ENTRIES:
                oldest = min(self._cache, key=lambda entry: self._cache[entry][0])
                self._cache.pop(oldest, None)
            self._cache[key] = (now, copy.deepcopy(books))
