"""Personalized Mini App book recommendations, kept separate from search."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import threading
import time
from difflib import SequenceMatcher

from src.utils import logger


GENRE_LABELS = (
    "Fantasy", "Romance", "Mystery", "Thriller", "Sci-Fi", "Horror",
    "Classics", "Literary fiction", "Historical", "Adventure", "Nonfiction",
    "Spiritual", "Biography", "Crime", "Contemporary", "Dystopian", "Family",
    "Humor", "Poetry", "Short stories", "War", "Young adult",
)

MOOD_GUIDES = {
    "Joyful": ("feel good fiction", ("humor", "feel good", "uplifting")),
    "Playful": ("witty humorous fiction", ("humor", "comedy", "satire")),
    "Hopeful": ("hopeful uplifting fiction", ("inspirational", "hope", "uplifting")),
    "Cozy": ("cozy fiction", ("cozy mystery", "cozy fantasy", "small town")),
    "Curious": ("curiosity discovery nonfiction", ("science", "history", "mystery")),
    "Adventurous": ("adventure fiction", ("adventure", "quest", "exploration")),
    "Thoughtful": ("thought provoking literary fiction", ("literary fiction", "philosophy", "ideas")),
    "Spiritual": ("spirituality reflective fiction", ("spirituality", "faith", "religion")),
    "A little thrill": ("suspense thriller fiction", ("thriller", "suspense", "mystery")),
    "Ready to cry": ("emotional literary fiction", ("literary fiction", "grief", "family")),
    "Calm": ("calm gentle fiction", ("quiet fiction", "nature", "healing")),
    "Inspired": ("inspiring biography fiction", ("inspirational", "biography", "personal growth")),
    "Nostalgic": ("nostalgic historical fiction", ("historical fiction", "coming of age", "memory")),
    "Romantic": ("romantic fiction", ("romance", "romantic fiction", "love")),
    "Courageous": ("courage resilience fiction", ("war", "resilience", "survival")),
    "Reflective": ("reflective literary fiction", ("literary fiction", "philosophy", "memoir")),
    "Surprised": ("plot twist mystery fiction", ("mystery", "crime", "thriller")),
    "Escapist": ("escapist fantasy fiction", ("fantasy", "adventure", "science fiction")),
    "Motivated": ("motivational nonfiction", ("personal growth", "business", "inspiration")),
    "Mysterious": ("mystery detective fiction", ("mystery", "detective fiction", "crime")),
}

HARDCOVER_MOOD_TAGS = {
    "Joyful": ("Lighthearted", "Hopeful"),
    "Playful": ("Funny", "Lighthearted"),
    "Hopeful": ("Hopeful", "Uplifting"),
    "Cozy": ("Cozy",),
    "Curious": ("Curious", "Mysterious"),
    "Adventurous": ("Adventurous", "Fast-paced"),
    "Thoughtful": ("Reflective",),
    "Spiritual": ("Reflective", "Hopeful"),
    "A little thrill": ("Dark", "Fast-paced", "Mysterious"),
    "Ready to cry": ("Emotional", "Sad"),
    "Calm": ("Lighthearted", "Reflective"),
    "Inspired": ("Hopeful", "Uplifting"),
    "Nostalgic": ("Nostalgic", "Reflective"),
    "Romantic": ("Romantic", "Hopeful"),
    "Courageous": ("Hopeful", "Emotional"),
    "Reflective": ("Reflective",),
    "Surprised": ("Mysterious", "Fast-paced"),
    "Escapist": ("Adventurous", "Fast-paced"),
    "Motivated": ("Hopeful", "Inspirational"),
    "Mysterious": ("Mysterious", "Dark"),
}

GENRE_ALIASES = {
    "sci fi": ("science fiction", "sci fi", "science fiction fantasy"),
    "classics": ("classic", "classics", "classic literature"),
    "literary fiction": ("literary fiction", "literature"),
    "young adult": ("young adult", "teen fiction"),
    "nonfiction": ("nonfiction", "non fiction"),
    "historical": ("historical", "history"),
}

GENERIC_TAGS = {"fiction", "nonfiction", "general", "books", "book", "literature", "adult"}
RESULT_LIMIT = 20
OPEN_LIBRARY_CACHE_TTL = 3600
_open_library_lock = threading.Lock()
_open_library_cache: dict[str, tuple[float, list[dict]]] = {}
_open_library_last_request = 0.0
_bigbook_quota_lock = threading.Lock()
_bigbook_quota_day = 0
_bigbook_requests_today = 0
_bigbook_reported_quota_left: int | None = None
_bigbook_last_request_time = 0.0


def _normalize(value: object) -> str:
    return re.sub(r"[^\w]+", " ", str(value or "").casefold(), flags=re.UNICODE).strip()


def _canonical_title(value: object) -> str:
    title = str(value or "").casefold().replace("&", " and ")
    # Edition/format wording is provider noise; remove only common markers so
    # genuine subtitles and sequels remain distinct recommendations.
    title = re.sub(r"\s*\((?:[^)]*\b(?:edition|illustrated|annotated|unabridged|paperback|hardcover|ebook|revised|deluxe|translation)\b[^)]*)\)", " ", title)
    title = re.sub(r"\b(?:illustrated|annotated|unabridged|paperback|hardcover|ebook|revised|deluxe)\s+edition\b", " ", title)
    return _normalize(title)


def _int_value(value: object) -> int:
    try:
        return max(0, int(float(str(value or "0").replace(",", ""))))
    except (TypeError, ValueError, OverflowError):
        return 0


def _as_list(value: object, *, limit: int, max_length: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    seen = set()
    for item in value:
        if not isinstance(item, str):
            continue
        clean = " ".join(item.split())[:max_length].strip()
        key = _normalize(clean)
        if clean and key and key not in seen:
            seen.add(key)
            result.append(clean)
        if len(result) >= limit:
            break
    return result


class BookRecommendationEngine:
    """Small in-process cache and quality/relevance ranker for recommendation requests."""

    CACHE_TTL_SECONDS = 1800
    EMPTY_CACHE_TTL_SECONDS = 180
    CACHE_MAX_ENTRIES = 96

    def __init__(self):
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def normalize_preferences(payload: object) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("invalid_preferences")

        read = _as_list(payload.get("read"), limit=10, max_length=160)
        liked = _as_list(payload.get("liked"), limit=10, max_length=160)
        allowed_genres = {_normalize(item): item for item in GENRE_LABELS}
        genres = [allowed_genres[_normalize(item)] for item in _as_list(payload.get("genres"), limit=5, max_length=40)
                  if _normalize(item) in allowed_genres]
        allowed_moods = {_normalize(item): item for item in MOOD_GUIDES}
        moods = [allowed_moods[_normalize(item)] for item in _as_list(payload.get("moods"), limit=5, max_length=40)
                 if _normalize(item) in allowed_moods]
        if not (read or liked or genres or moods):
            raise ValueError("recommendation_input_required")
        return {"read": read, "liked": liked, "genres": genres, "moods": moods}

    async def recommend(self, raw_preferences: object) -> dict:
        preferences = self.normalize_preferences(raw_preferences)
        excluded_books = []
        if isinstance(raw_preferences, dict) and isinstance(raw_preferences.get("exclude_books"), list):
            for item in raw_preferences["exclude_books"][:100]:
                if not isinstance(item, dict):
                    continue
                title = item.get("title")
                author = item.get("author", "")
                if isinstance(title, str) and title.strip():
                    excluded_books.append({
                        "title": title.strip()[:250],
                        "author": author.strip()[:250] if isinstance(author, str) else "",
                    })
        fallback_only = isinstance(raw_preferences, dict) and raw_preferences.get("fallback_only") is True
        if not fallback_only:
            hardcover_books = await self._fetch_hardcover_similar(preferences, excluded_books)
            if hardcover_books:
                logger.info("[miniapp] Hardcover primary recommendations results=%d", len(hardcover_books))
                return {"books": hardcover_books, "cached": False, "sources_used": ["hardcover"]}
        # Big Book API's terms require prior written permission for caching its
        # user-requested results, so bypass the aggregate response cache while
        # that provider is enabled.
        bigbook_enabled = bool(os.getenv("BIGBOOK_API_KEY", "").strip())
        cache_key = hashlib.sha256(
            json.dumps(
                {"preferences": preferences, "exclude_books": excluded_books},
                ensure_ascii=False, sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        now = time.time()
        if not bigbook_enabled:
            with self._lock:
                cached = self._cache.get(cache_key)
                cached_ttl = self.CACHE_TTL_SECONDS if cached and cached[1] else self.EMPTY_CACHE_TTL_SECONDS
                if cached and now - cached[0] < cached_ttl:
                    return {"books": [dict(book) for book in cached[1]], "cached": True}
                self._cache.pop(cache_key, None)

        started = time.perf_counter()
        candidates = await self._fetch_candidates(preferences)
        ranked = self._rank(candidates, preferences, excluded_books=excluded_books)
        if not bigbook_enabled:
            with self._lock:
                if len(self._cache) >= self.CACHE_MAX_ENTRIES:
                    oldest = min(self._cache, key=lambda key: self._cache[key][0])
                    self._cache.pop(oldest, None)
                self._cache[cache_key] = (time.time(), [dict(book) for book in ranked])
        logger.info(
            "[miniapp] recommendations complete elapsed_ms=%d candidates=%d results=%d genres=%d moods=%d seeds=%d",
            round((time.perf_counter() - started) * 1000), len(candidates), len(ranked),
            len(preferences["genres"]), len(preferences["moods"]),
            len(preferences["read"]) + len(preferences["liked"]),
        )
        sources_used = sorted({str(book.get("_provider") or "") for book in candidates if book.get("_provider")})
        return {"books": ranked, "cached": False, "sources_used": sources_used}

    async def _fetch_hardcover_similar(self, preferences: dict, excluded_books: list[dict]) -> list[dict]:
        """Return Hardcover's pre-ranked similar lists before invoking fallback providers."""
        from src.aggregator import MultiSourceBookAggregator

        seed_values = list(dict.fromkeys(preferences["liked"] + preferences["read"]))[:3]
        async def similar_for_seed(seed: str) -> list[dict]:
            try:
                matches = await asyncio.to_thread(MultiSourceBookAggregator.search_hardcover, seed, 10)
                if not matches:
                    return []
                seed_key = _canonical_title(seed)
                exact = next((book for book in matches if
                              _canonical_title(book.get("title")) == seed_key), None)
                selected = exact or matches[0]
                hardcover_id = selected.get("hardcover_id")
                if not str(hardcover_id or "").isdigit() or int(hardcover_id) <= 0:
                    return []
                similar = await asyncio.to_thread(
                    MultiSourceBookAggregator.search_hardcover_similar, int(hardcover_id), RESULT_LIMIT,
                )
                return similar if isinstance(similar, list) else []
            except Exception as exc:
                logger.info("[miniapp] Hardcover similar lookup failed error=%s", type(exc).__name__)
                return []

        batches = await asyncio.gather(*(similar_for_seed(seed) for seed in seed_values)) if seed_values else []
        seed_titles = {_canonical_title(seed) for seed in seed_values}
        candidates = []
        seen = set()
        for batch in batches:
            for book in batch:
                if not isinstance(book, dict):
                    continue
                title = str(book.get("title") or "").strip()
                author = str(book.get("author") or "").strip()
                key = (_canonical_title(title), _normalize(author))
                if not title or not author or key[0] in seed_titles or key in seen:
                    continue
                book_categories = self._categories(book)
                if preferences["genres"] and not any(
                    self._tag_matches(category, selected_genre)
                    for category in book_categories for selected_genre in preferences["genres"]
                ):
                    continue
                book_moods = book.get("moods") or []
                if isinstance(book_moods, str):
                    book_moods = [book_moods]
                expected_moods = [tag for mood in preferences["moods"]
                                  for tag in HARDCOVER_MOOD_TAGS.get(mood, (mood,))]
                if expected_moods and not any(
                    self._tag_matches(tag, expected)
                    for tag in book_moods if isinstance(tag, str) for expected in expected_moods
                ):
                    continue
                if any(_canonical_title(title) == _canonical_title(item.get("title"))
                       and (not item.get("author") or _normalize(author) == _normalize(item.get("author")))
                       for item in excluded_books):
                    continue
                seen.add(key)
                book["_provider"] = "hardcover"
                book["source"] = "hardcover"
                book["recommendation_reason"] = "Similar to your book picks"
                candidates.append(book)
                if len(candidates) >= RESULT_LIMIT:
                    return candidates
        if candidates:
            return candidates

        # With only genres or moods, use Hardcover's own catalog search as the
        # primary shelf. The existing multi-provider ranker remains available
        # when Hardcover has no usable matches or when the user asks for more.
        genre_values = []
        for genre in preferences["genres"]:
            genre_values.extend(GENRE_ALIASES.get(_normalize(genre), (genre,)))
        genre_values = list(dict.fromkeys(genre_values))[:5]
        mood_values = list(dict.fromkeys(
            tag for mood in preferences["moods"] for tag in HARDCOVER_MOOD_TAGS.get(mood, (mood,))
        ))[:6]
        if not genre_values and not mood_values:
            return []

        if genre_values and mood_values:
            tag_pairs = [(genre, mood) for genre in genre_values for mood in mood_values][:6]
        elif genre_values:
            tag_pairs = [(genre, "") for genre in genre_values[:6]]
        else:
            tag_pairs = [("", mood) for mood in mood_values[:6]]

        async def search_tags(genre: str, mood: str) -> list[dict]:
            try:
                batch = await asyncio.to_thread(
                    MultiSourceBookAggregator.search_hardcover_by_tags, genre, mood, 30,
                )
            except Exception as exc:
                logger.info("[miniapp] Hardcover tag recommendation failed error=%s", type(exc).__name__)
                return []
            output = []
            for item in batch if isinstance(batch, list) else []:
                if not isinstance(item, dict):
                    continue
                candidate = dict(item)
                candidate.update(
                    _provider="hardcover",
                    _match_kind="genre" if genre else "mood",
                    _match_label=genre or mood,
                )
                output.append(candidate)
            return output

        term_batches = await asyncio.gather(*(search_tags(*pair) for pair in tag_pairs))
        hardcover_candidates = [book for batch in term_batches for book in batch]
        return self._rank(hardcover_candidates, preferences, excluded_books=excluded_books)

    async def _fetch_candidates(self, preferences: dict) -> list[dict]:
        from src.aggregator import MultiSourceBookAggregator
        provider_semaphore = asyncio.Semaphore(4)

        seed_values = list(dict.fromkeys(preferences["liked"] + preferences["read"]))[:3]

        async def run_provider(provider: str, query: str, limit: int) -> list[dict]:
            started = time.perf_counter()
            try:
                async with provider_semaphore:
                    if provider == "hardcover":
                        result = await asyncio.to_thread(MultiSourceBookAggregator.search_hardcover, query, limit)
                    elif provider == "bigbookapi":
                        result = await asyncio.to_thread(self._search_big_book_api, preferences)
                    else:
                        result = await asyncio.to_thread(self._search_google_books, query, limit)
                return result if isinstance(result, list) else []
            except Exception as exc:
                logger.info("[miniapp] recommendation provider failed provider=%s error=%s", provider, type(exc).__name__)
                return []
            finally:
                logger.info("[miniapp] recommendation provider=%s elapsed_ms=%d", provider, round((time.perf_counter() - started) * 1000))

        terms: list[tuple[str, str, str]] = []
        for genre in preferences["genres"]:
            terms.append((genre, "genre", genre))
        mood_limit = 1 if preferences["genres"] else 4
        for mood in preferences["moods"][:mood_limit]:
            terms.append((MOOD_GUIDES[mood][0], "mood", mood))
        # Keep requests bounded even when several preference groups are filled.
        terms = terms[:6]

        async def fetch_term_batches(search_terms, *, include_google):
            provider_calls = [("hardcover", query, kind, label, 30)
                              for query, kind, label in search_terms]
            if include_google:
                provider_calls.extend(("google_books", query, kind, label, 20)
                                      for query, kind, label in search_terms[:3])
            batches = await asyncio.gather(
                *(run_provider(provider, query, limit)
                  for provider, query, _kind, _label, limit in provider_calls),
                return_exceptions=True,
            )
            collected = []
            for (provider, query, kind, label, _limit), batch in zip(provider_calls, batches):
                if not isinstance(batch, list):
                    continue
                for item in batch:
                    if not isinstance(item, dict):
                        continue
                    candidate = dict(item)
                    candidate["_provider"] = provider
                    candidate["_match_kind"] = kind
                    candidate["_match_label"] = label
                    candidate["_query"] = query
                    collected.append(candidate)
            return collected

        async def fetch_seed_batches():
            return await asyncio.gather(
                *(run_provider("hardcover", seed, 20) for seed in seed_values),
                return_exceptions=True,
            )

        seed_task = asyncio.create_task(fetch_seed_batches()) if seed_values else None
        base_task = asyncio.create_task(fetch_term_batches(terms, include_google=True)) if terms else None
        bigbook_task = (asyncio.create_task(run_provider("bigbookapi", "personalized preferences", 40))
                        if os.getenv("BIGBOOK_API_KEY", "").strip() else None)
        seed_batches = await seed_task if seed_task else []
        seed_books = [book for batch in seed_batches if isinstance(batch, list) for book in batch]
        seed_tags = self._infer_seed_tags(seed_books, seed_values)
        # Start the single Open Library request while the other provider batches
        # are still running so the supplemental source adds little wall time.
        ol_subject = (preferences["genres"] or
                      ([MOOD_GUIDES[preferences["moods"][0]][1][0]] if preferences["moods"] else []) or
                      seed_tags[:1])
        ol_task = asyncio.create_task(asyncio.to_thread(self._search_open_library, ol_subject[0])) if ol_subject else None
        candidates = await base_task if base_task else []
        if bigbook_task:
            bigbook_books = await bigbook_task
            candidates.extend(bigbook_books)

        # A book or author alone often has no directly usable mood/genre. Learn
        # its catalogue themes first, then look outward through those themes.
        slots = max(0, 6 - len(terms))
        inferred_terms = [(tag, "seed_genre", tag) for tag in seed_tags[:min(3, slots)]]
        if inferred_terms:
            candidates.extend(await fetch_term_batches(inferred_terms, include_google=not terms))

        # Open Library contributes one cached, structured search per preference
        # set; it is deliberately not fanned out across every genre or mood.
        if ol_task:
            try:
                ol_books = await ol_task
                for item in ol_books:
                    item["_provider"] = "open_library"
                    item["_match_kind"] = "genre" if preferences["genres"] else "mood" if preferences["moods"] else "seed_genre"
                    item["_match_label"] = ol_subject[0]
                    candidates.append(item)
            except Exception as exc:
                logger.info("[miniapp] recommendation provider failed provider=open_library error=%s", type(exc).__name__)

        if not candidates and not seed_tags:
            for item in seed_books:
                if isinstance(item, dict):
                    candidate = dict(item)
                    candidate["_provider"] = "hardcover"
                    candidate["_match_kind"] = "seed"
                    candidate["_match_label"] = "reading history"
                    candidates.append(candidate)
        else:
            # If seeds have no usable genre metadata, retain their candidate
            # records as a weak backstop; explicit genres/moods stay stronger.
            if not seed_tags and not preferences["genres"] and not preferences["moods"]:
                for item in seed_books:
                    if isinstance(item, dict):
                        candidate = dict(item)
                        candidate["_provider"] = "hardcover"
                        candidate["_match_kind"] = "seed"
                        candidate["_match_label"] = "reading history"
                        candidates.append(candidate)

        for candidate in candidates:
            candidate["_inferred_seed_tags"] = seed_tags

        return candidates

    @staticmethod
    def _search_google_books(query: str, limit: int) -> list[dict]:
        """Recommendation-only Google Books query, allowing its documented 40-result page."""
        import os

        from src.aggregator import MultiSourceBookAggregator
        from src.utils import get_http_session

        params = {"q": query, "maxResults": min(max(int(limit), 1), 40), "printType": "books", "orderBy": "relevance"}
        api_key = os.getenv("GOOGLE_BOOKS_API_KEY")
        if api_key:
            params["key"] = api_key
        response = get_http_session().get("https://www.googleapis.com/books/v1/volumes", params=params, timeout=8)
        if response.status_code != 200:
            return []
        books = []
        for item in response.json().get("items", []) or []:
            book = MultiSourceBookAggregator._parse_google_book(item, translate_description=False)
            if book:
                books.append(book)
        return books

    @staticmethod
    def _search_big_book_api(preferences: dict) -> list[dict]:
        """Fetch one recommendations-only candidate batch; never log the secret key."""
        from src.utils import get_http_session

        api_key = os.getenv("BIGBOOK_API_KEY", "").strip()
        if not api_key:
            return []
        global _bigbook_quota_day, _bigbook_requests_today, _bigbook_reported_quota_left, _bigbook_last_request_time
        daily_budget = 45
        try:
            daily_budget = max(1, int(os.getenv("BIGBOOK_API_DAILY_BUDGET", "45")))
        except ValueError:
            pass
        utc_day = int(time.time() // 86400)
        with _bigbook_quota_lock:
            if utc_day != _bigbook_quota_day:
                _bigbook_quota_day = utc_day
                _bigbook_requests_today = 0
                _bigbook_reported_quota_left = None
            if _bigbook_requests_today >= daily_budget or _bigbook_reported_quota_left == 0:
                logger.info("[miniapp] recommendation provider=bigbookapi skipped daily_budget_reached")
                return []
            wait = max(0.0, 1.05 - (time.time() - _bigbook_last_request_time))
            if wait:
                time.sleep(wait)
            _bigbook_last_request_time = time.time()
            _bigbook_requests_today += 1
        query_parts = []
        if preferences.get("liked"):
            query_parts.append("books similar to " + ", ".join(preferences["liked"]))
        if preferences.get("read"):
            query_parts.append("books for readers of " + ", ".join(preferences["read"]))
        if preferences.get("genres"):
            query_parts.append("genres " + ", ".join(preferences["genres"]))
        if preferences.get("moods"):
            query_parts.append("books with a " + ", ".join(preferences["moods"]) + " mood")
        params = {"query": ", ".join(query_parts), "number": 40}
        started = time.perf_counter()
        try:
            response = get_http_session().get(
                "https://api.bigbookapi.com/search-books", params=params,
                headers={"x-api-key": api_key, "Accept": "application/json"}, timeout=(3, 8),
            )
        except Exception as exc:
            logger.info("[miniapp] recommendation provider=bigbookapi failed error=%s", type(exc).__name__)
            return []
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        quota_left = response.headers.get("X-API-Quota-Left", "unknown")
        try:
            with _bigbook_quota_lock:
                _bigbook_reported_quota_left = int(float(quota_left))
        except (TypeError, ValueError):
            pass
        if response.status_code != 200:
            logger.info("[miniapp] recommendation provider=bigbookapi status=%d elapsed_ms=%d quota_left=%s",
                        response.status_code, elapsed_ms, quota_left)
            return []
        payload = response.json()
        raw_books = payload.get("books", []) if isinstance(payload, dict) else []
        books = []
        match_kind = "genre" if preferences.get("genres") else "mood" if preferences.get("moods") else "preference"
        match_label = ", ".join(preferences.get("genres") or preferences.get("moods") or []) or "your reading history"
        groups = list(raw_books) if isinstance(raw_books, list) else []
        for group in groups:
            item = group
            while isinstance(item, list):
                item = item[0] if item else None
            if not isinstance(item, dict):
                continue
            authors = item.get("authors") or []
            if isinstance(authors, str):
                author = authors
            elif isinstance(authors, list):
                author = ", ".join(str(author.get("name") or "") if isinstance(author, dict) else str(author)
                                    for author in authors if author)
            else:
                author = ""
            title = str(item.get("title") or "").strip()
            if not title or not author:
                continue
            rating_data = item.get("rating") or {}
            rating = rating_data.get("average", 0) if isinstance(rating_data, dict) else rating_data
            try:
                rating = float(rating or 0)
                # Big Book documents rating.average in [0, 1], while the Mini App
                # renders ratings on a five-star scale.
                if 0 < rating <= 1:
                    rating *= 5
            except (TypeError, ValueError, OverflowError):
                rating = 0
            identifiers = item.get("identifiers") or {}
            isbn = str(identifiers.get("isbn_13") or identifiers.get("isbn_10") or "") if isinstance(identifiers, dict) else ""
            books.append({
                "title": title + ((": " + str(item["subtitle"]).strip()) if item.get("subtitle") else ""),
                "author": author,
                "cover_url": str(item.get("image") or ""),
                "rating": rating,
                "rating_count": _int_value(rating_data.get("count") if isinstance(rating_data, dict) else item.get("rating_count")),
                "categories": item.get("genres") or item.get("categories") or [],
                "isbn": isbn,
                "page_count": item.get("number_of_pages") or item.get("page_count") or 0,
                "published_date": item.get("publish_date") or item.get("published_date") or "",
                "description": item.get("description") or "",
                "info_link": f"https://api.bigbookapi.com/{item['id']}" if item.get("id") else "",
                "source": "bigbookapi",
                "_provider": "bigbookapi",
                "_match_kind": match_kind,
                "_match_label": match_label,
            })
        logger.info("[miniapp] recommendation provider=bigbookapi status=200 elapsed_ms=%d results=%d quota_left=%s",
                    elapsed_ms, len(books), quota_left)
        return books

    @staticmethod
    def _search_open_library(subject: str) -> list[dict]:
        """One cached Open Library search; use its API, not page scraping."""
        import os

        from src.utils import get_http_session

        global _open_library_last_request
        query = _normalize(subject)
        if not query:
            return []
        now = time.time()
        with _open_library_lock:
            cached = _open_library_cache.get(query)
            if cached and now - cached[0] < OPEN_LIBRARY_CACHE_TTL:
                return [dict(book) for book in cached[1]]
            # Respect Open Library's low-volume API guidance across concurrent users.
            wait = max(0.0, 1.05 - (now - _open_library_last_request))
            if wait:
                time.sleep(wait)
            _open_library_last_request = time.time()
        params = {
            "q": f"subject:{subject}", "limit": 40,
            "fields": "key,title,author_name,cover_i,subject,first_publish_year,ratings_average,ratings_count,edition_count",
        }
        contact = os.getenv("OPEN_LIBRARY_CONTACT_EMAIL", "").strip()
        headers = {"User-Agent": f"AnnieSearchBookDiscovery/1.0 ({contact})" if contact else "AnnieSearchBookDiscovery/1.0"}
        response = get_http_session().get("https://openlibrary.org/search.json", params=params, headers=headers, timeout=8)
        if response.status_code != 200:
            return []
        books = []
        for doc in response.json().get("docs", []) or []:
            title = str(doc.get("title") or "").strip()
            authors = doc.get("author_name") or []
            author = str(authors[0]).strip() if authors else ""
            if not title or not author:
                continue
            cover_id = doc.get("cover_i")
            isbns = doc.get("isbn") or []
            books.append({
                "title": title, "author": author,
                "cover_url": f"https://covers.openlibrary.org/b/id/{int(cover_id)}-M.jpg" if cover_id else "",
                "rating": doc.get("ratings_average") or 0,
                "rating_count": doc.get("ratings_count") or 0,
                "categories": doc.get("subject") or [],
                "isbn": str(isbns[0]) if isbns else "",
                "published_date": str(doc.get("first_publish_year") or ""),
                "info_link": "https://openlibrary.org" + str(doc.get("key") or ""),
                "source": "open_library",
            })
        with _open_library_lock:
            _open_library_cache[query] = (time.time(), [dict(book) for book in books])
            if len(_open_library_cache) > 64:
                oldest = min(_open_library_cache, key=lambda key: _open_library_cache[key][0])
                _open_library_cache.pop(oldest, None)
        return books

    @classmethod
    def _infer_seed_tags(cls, books: list[dict], seed_values: list[str]) -> list[str]:
        seed_keys = {_normalize(value) for value in seed_values}
        counts: dict[str, int] = {}
        for book in books:
            title = _normalize(book.get("title"))
            if title in seed_keys:
                weight = 2
            else:
                weight = 1
            for tag in cls._categories(book):
                if tag not in GENERIC_TAGS:
                    counts[tag] = counts.get(tag, 0) + weight
        return [tag for tag, _count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:6]]

    @staticmethod
    def _categories(book: dict) -> set[str]:
        raw = book.get("categories") or book.get("genres") or []
        if isinstance(raw, (str, dict)):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            return set()
        values = set()
        for category in raw:
            if isinstance(category, dict):
                category = category.get("name") or category.get("title") or ""
            for part in re.split(r"\s*(?:/|>|;|,)\s*", str(category or "")):
                normalized = _normalize(part)
                if normalized and normalized not in GENERIC_TAGS:
                    values.add(normalized)
        return values

    @staticmethod
    def _tag_matches(tag: str, expected: str) -> bool:
        tag_norm, expected_norm = _normalize(tag), _normalize(expected)
        aliases = GENRE_ALIASES.get(expected_norm, (expected_norm,))
        return any(
            tag_norm == alias
            or tag_norm.startswith(alias + " ")
            or alias.startswith(tag_norm + " ")
            for alias in aliases
        )

    @classmethod
    def _rank(cls, candidates: list[dict], preferences: dict, *, excluded_books: list[dict] | None = None) -> list[dict]:
        seeds = [_canonical_title(value) for value in preferences["liked"] + preferences["read"]]
        seed_authors = set()
        for value in preferences["liked"] + preferences["read"]:
            if re.search(r"\bby\b", value, re.IGNORECASE):
                seed_authors.add(_normalize(re.split(r"\bby\b", value, maxsplit=1, flags=re.IGNORECASE)[-1]))
        explicit_genres = preferences["genres"]
        mood_tags = [tag for mood in preferences["moods"] for tag in MOOD_GUIDES[mood][1]]
        seed_tags = set()
        for candidate in candidates:
            for tag in candidate.get("_inferred_seed_tags") or []:
                if isinstance(tag, str) and tag.strip():
                    seed_tags.add(_normalize(tag))
            if candidate.get("_match_kind") == "seed":
                continue
            if candidate.get("_match_kind") == "seed_genre":
                seed_tags.add(_normalize(candidate.get("_match_label")))

        merged: dict[tuple[str, str], dict] = {}
        for raw in candidates:
            title = str(raw.get("title") or "").strip()
            author = str(raw.get("author") or "").strip()
            if not title or not author:
                continue
            title_key = _canonical_title(title)
            author_key = _normalize(author)
            if not title_key or any(title_key == seed or title_key in seed for seed in seeds if seed):
                continue
            isbn = re.sub(r"\D", "", str(raw.get("isbn") or ""))
            key = ("isbn", isbn) if len(isbn) >= 10 else (title_key, author_key)
            current = merged.get(key)
            if current is None:
                merged[key] = dict(raw)
                continue
            for field in ("cover_url", "description", "isbn", "published_date", "page_count", "info_link"):
                if not current.get(field) and raw.get(field):
                    current[field] = raw[field]
            current_count = _int_value(current.get("rating_count"))
            raw_count = _int_value(raw.get("rating_count"))
            if (not current.get("rating") or current_count == 0) and raw.get("rating"):
                current["rating"] = raw["rating"]
                current["rating_count"] = raw_count
            current_categories = current.get("categories") or []
            if not current_categories and raw.get("categories"):
                current["categories"] = raw["categories"]

        scored = []
        for book in merged.values():
            try:
                rating = float(str(book.get("rating") or 0).replace(",", ""))
            except (TypeError, ValueError, OverflowError):
                rating, count = 0.0, 0
            else:
                count = _int_value(book.get("rating_count"))
            if rating and rating < 3.2:
                continue

            categories = cls._categories(book)
            matching_genres = [genre for genre in explicit_genres if any(cls._tag_matches(tag, genre) for tag in categories)]
            matching_seed_tags = [tag for tag in seed_tags if any(cls._tag_matches(category, tag) for category in categories)]
            matching_moods = [tag for tag in mood_tags if any(cls._tag_matches(category, tag) for category in categories)]
            text = " ".join((str(book.get("title") or ""), str(book.get("description") or ""))).casefold()
            mood_text_match = any(len(tag) > 4 and tag in text for tag in mood_tags)
            author = _normalize(book.get("author"))
            author_match = any(seed_author and (seed_author in author or author in seed_author) for seed_author in seed_authors)

            relevance = 0.24
            reasons = []
            if matching_genres:
                relevance = max(relevance, 0.97)
                reasons.append("Fits " + ", ".join(matching_genres[:2]))
            match_kind = book.get("_match_kind")
            if match_kind == "genre":
                relevance = max(relevance, 0.92)
                if not reasons:
                    reasons.append("Picked for " + str(book.get("_match_label") or "your genre"))
            elif match_kind == "seed_genre":
                relevance = max(relevance, 0.78 if explicit_genres else 0.86)
                if not reasons:
                    reasons.append("Shares themes with your picks")
            elif match_kind == "mood":
                relevance = max(relevance, 0.82 if not explicit_genres else 0.70)
                if not reasons:
                    reasons.append("Matches your chosen mood")
            elif match_kind == "preference":
                relevance = max(relevance, 0.80)
                if not reasons:
                    reasons.append("Matched to your reading preferences")
            if matching_seed_tags:
                relevance = max(relevance, 0.77 if explicit_genres else 0.87)
                if not reasons:
                    reasons.append("Shares themes with your picks")
            if matching_moods or mood_text_match:
                relevance = max(relevance, 0.83 if not explicit_genres else 0.70)
                if not reasons:
                    reasons.append("Matches your chosen mood")
            if author_match:
                relevance = max(relevance, 0.38)
                if not reasons:
                    reasons.append("From an author you mentioned")
            if book.get("_match_kind") == "seed":
                relevance = max(relevance, 0.30)
                if not reasons:
                    reasons.append("Related to your reading history")

            # Bayesian quality rewards high ratings while shrinking low-count
            # scores toward a neutral 3.7. Popularity adds only a small tie-break.
            effective_rating = rating if rating > 0 else 3.7
            # Big Book's search response exposes an average but usually no count;
            # give that average modest influence without inventing a displayed count.
            confidence_count = 12 if rating and count == 0 and book.get("_provider") == "bigbookapi" else count
            bayesian_rating = ((effective_rating * confidence_count) + (3.7 * 45)) / (confidence_count + 45)
            quality = bayesian_rating / 5.0
            popularity = min(math.log1p(count) / math.log1p(100000), 1.0)
            score = relevance * 0.65 + quality * 0.31 + popularity * 0.04
            hidden_gem = bool(rating >= 4.0 and 8 <= count <= 1200 and relevance >= 0.65)
            if hidden_gem:
                reasons.append("A well-rated, less-discovered pick")
            if not reasons:
                reasons.append("Strong reader ratings and a good fit")

            book["rating"] = rating
            book["rating_count"] = count
            book["recommendation_reason"] = " · ".join(reasons[:2])
            book["_score"] = score
            book["_relevance"] = relevance
            book["_hidden_gem"] = hidden_gem
            scored.append(book)

        scored.sort(key=lambda item: (-item["_score"], -item["_relevance"], _normalize(item.get("title"))))
        selected = []
        authors: dict[str, int] = {}
        used = list(excluded_books or [])

        def same_work(left, right):
            if _canonical_title(left.get("title")) != _canonical_title(right.get("title")):
                return False
            left_author, right_author = _normalize(left.get("author")), _normalize(right.get("author"))
            if not left_author or not right_author:
                return True
            return left_author == right_author or SequenceMatcher(None, left_author, right_author).ratio() >= 0.84

        def add(item, *, enforce_author_cap=True):
            author = _normalize(item.get("author"))
            if any(same_work(item, existing) for existing in used) or (enforce_author_cap and authors.get(author, 0) >= 2):
                return False
            selected.append(item)
            used.append(item)
            authors[author] = authors.get(author, 0) + 1
            return True

        hidden = next((book for book in scored if book["_hidden_gem"]), None)
        for book in scored:
            if len(selected) == 4 and hidden is not None and not any(item["_hidden_gem"] for item in selected):
                add(hidden)
            if len(selected) >= RESULT_LIMIT:
                break
            add(book)
        if len(selected) < RESULT_LIMIT:
            for book in scored:
                if len(selected) >= RESULT_LIMIT:
                    break
                # Keep the list author-diverse when possible, then relax the
                # soft cap to reach up to 20 distinct works for narrow inputs.
                add(book, enforce_author_cap=False)

        result = []
        for book in selected[:RESULT_LIMIT]:
            public = {
                "hardcover_id": int(book.get("hardcover_id") or 0) if str(book.get("hardcover_id") or "").isdigit() else 0,
                "title": str(book.get("title") or ""),
                "author": str(book.get("author") or ""),
                "cover_url": str(book.get("cover_url") or ""),
                "rating": book.get("rating", 0),
                "rating_count": book.get("rating_count", 0),
                "categories": list(book.get("categories") or []) if isinstance(book.get("categories") or [], (list, tuple)) else [book.get("categories")],
                "recommendation_reason": book.get("recommendation_reason", "A good fit for your reading preferences"),
                "source": str(book.get("source") or book.get("_provider") or ""),
                "info_link": str(book.get("info_link") or ""),
                "description": str(book.get("description") or ""),
                "isbn": str(book.get("isbn") or ""),
                "page_count": book.get("page_count") or 0,
                "published_date": str(book.get("published_date") or ""),
                "language": str(book.get("language") or ""),
                "metadata_source": str(book.get("metadata_source") or ""),
            }
            result.append(public)
        return result
