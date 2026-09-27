"""MultiSourceBookAggregator — aggregates book data from multiple sources."""

import asyncio
import os
import json
import requests

from src.utils import HEADERS, logger, is_placeholder_image, is_unreliable_gb_cover, translate_to_english
from src.search import scrape_goodreads

# Google Books API key is optional; without it the API still works, just unauthenticated.
GOOGLE_BOOKS_API_KEY = os.getenv("GOOGLE_BOOKS_API_KEY")


class MultiSourceBookAggregator:
    """Aggregates book data from multiple sources for best quality"""

    @staticmethod
    def search_google_books(query, limit=10, translate_description=True):
        """Search Google Books API

        Args:
            query: search string
            limit: max results to return
            translate_description: if True, translate non-English descriptions
                to English (default for /search). Set False for inline mode
                where speed is critical.
        """
        try:
            logger.info(f"🔍 Searching Google Books: {query}")
            url = "https://www.googleapis.com/books/v1/volumes"

            params = {
                "q": query,
                "maxResults": min(limit, 8),
                "printType": "books",
                "orderBy": "relevance",
                "langRestrict": "en",
            }

            if GOOGLE_BOOKS_API_KEY:
                params["key"] = GOOGLE_BOOKS_API_KEY

            response = requests.get(url, params=params, timeout=8)
            if response.status_code != 200:
                logger.warning(f"Google Books API error: {response.status_code}")
                return []

            data = response.json()
            items = data.get("items", [])

            # ── PART 1/2 diagnostic + language filter ─────────────────────────────
            # langRestrict=en in the API params should filter by language metadata,
            # but Koyeb Frankfurt IPs still return German results in practice.
            # Belt-and-suspenders: apply an application-side filter as a safety net.
            raw_count = len(items)
            items = [
                item
                for item in items
                if (item.get("volumeInfo") or {}).get("language") == "en"
            ]
            filtered_count = raw_count - len(items)
            # Also reject items with blank/missing titles as a basic validity check.
            items = [item for item in items if (item.get("volumeInfo") or {}).get("title", "").strip()]
            invalid_count = raw_count - filtered_count - len(items)
            logger.info(
                f"GB: raw={raw_count} lang_filtered={filtered_count} "
                f"invalid_title_filtered={invalid_count} final={len(items)}"
            )
            # ── end filter ─────────────────────────────────────────────────
            for idx, item in enumerate(items[:8]):
                vol = item.get("volumeInfo", {})
                title = vol.get("title", "")
                authors = vol.get("authors", [])
                lang = vol.get("language", "")
                vid = item.get("id", "")
                logger.info(
                    f"GB result [{idx}] id={vid} title={title[:50]} author={authors[0] if authors else '?'} lang={lang}"
                )

            books = []
            for item in items:
                book = MultiSourceBookAggregator._parse_google_book(
                    item, translate_description=translate_description
                )
                if book:
                    books.append(book)

            logger.info(f"✅ Google Books: found {len(books)} books")
            return books

        except Exception as e:
            logger.error(f"Google Books error: {e}")
            return []

    @staticmethod
    def _parse_google_book(item, translate_description=True):
        """Parse Google Books item.

        Args:
            item: raw JSON volume item from Google Books API.
            translate_description: if True (default), translate non-English
                descriptions via Google Translate.  Set False for inline mode
                where speed is critical.
        """
        try:
            vol = item.get("volumeInfo", {})
            volume_id = item.get("id", "")

            title = vol.get("title", "").strip()
            if not title:
                return None

            authors = vol.get("authors", [])
            author = authors[0] if authors else "Unknown Author"

            # Get rating
            rating = vol.get("averageRating", 0.0)
            rating_count = vol.get("ratingsCount", 0)

            # Get ISBN
            isbn = ""
            for id_obj in vol.get("industryIdentifiers", []):
                if id_obj.get("type") in ["ISBN_13", "ISBN_10"]:
                    isbn = id_obj.get("identifier", "")
                    break

            # Get cover (all sizes)
            img = vol.get("imageLinks", {})
            cover_url = (
                img.get("extraLarge")
                or img.get("large")
                or img.get("medium")
                or img.get("thumbnail")
                or img.get("smallThumbnail", "")
            )
            if cover_url:
                cover_url = cover_url.replace("http://", "https://").replace("&zoom=1", "&zoom=0")
            # Catalog-only Google Books records (volume IDs ending in "AACAAJ")
            # only ever return the "image not available" placeholder — treat them
            # as having no cover so a real one is sourced later (Open Library/iTunes).
            if is_unreliable_gb_cover(volume_id):
                cover_url = ""

            # Translate non-English descriptions to English (opt-out for inline)
            description = vol.get("description", "")
            if description and translate_description:
                description = translate_to_english(description)

            return {
                "title": title,
                "author": author,
                "rating": rating,
                "rating_count": rating_count,
                "description": description,
                "isbn": isbn,
                "cover_url": cover_url,
                "page_count": vol.get("pageCount", 0),
                "published_date": vol.get("publishedDate", ""),
                "categories": vol.get("categories", []),
                "info_link": vol.get("infoLink", ""),
                "gb_volume_id": volume_id,
                "source": "google_books",
            }
        except Exception as e:
            logger.debug(f"Error parsing Google book: {e}")
            return None

    @staticmethod
    def search_itunes(query):
        """Search iTunes API for high-quality covers"""
        try:
            logger.info(f"🔍 Searching iTunes: {query}")
            url = "https://itunes.apple.com/search"
            params = {
                "term": query,
                "media": "ebook",
                "entity": "ebook",
                "limit": 8,
            }

            response = requests.get(url, params=params, timeout=8)
            if response.status_code != 200:
                return []

            data = response.json()
            results = data.get("results", [])

            books = []
            for result in results:
                book = MultiSourceBookAggregator._parse_itunes_book(result)
                if book:
                    books.append(book)

            logger.info(f"✅ iTunes: found {len(books)} books")
            return books

        except Exception as e:
            logger.error(f"iTunes error: {e}")
            return []

    @staticmethod
    def search_hardcover(query: str, limit: int = 10) -> list[dict]:
        """Search Hardcover.app for books, return list of dicts with metadata.

        Returns up to `limit` books with: title, author, rating, rating_count,
        page_count, description, genres, cover_url, isbn, published_date.
        Uses caching for individual book lookups to avoid redundant API calls.
        """
        api_key = os.getenv("HARDCOVER_API_KEY", "").strip()
        if not api_key:
            return []

        try:
            # Build search query
            if not query or not query.strip():
                return []

            search_query = query.strip()

            query_string = """
            query SearchBooks($q: String!, $limit: Int) {
                search(query: $q, query_type: "Book", per_page: $limit) {
                    results
                }
            }
            """
            variables = {"q": search_query, "limit": limit}
            resp = requests.post(
                "https://api.hardcover.app/v1/graphql",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={"query": query_string, "variables": variables},
                timeout=10,
            )
            if resp.status_code != 200:
                logger.debug(f"Hardcover GraphQL error {resp.status_code}: {resp.text[:200]}")
                return []

            data = resp.json()
            if "errors" in data:
                logger.debug(f"Hardcover GraphQL errors: {data['errors']}")
                return []

            search_result = data.get("data", {}).get("search", {})
            raw_results = search_result.get("results", {})
            # results is a JSONB object, may be string or dict
            results_json = json.loads(raw_results) if isinstance(raw_results, str) else raw_results
            hits = results_json.get("hits", [])

            if not hits:
                return []

            books = []
            for hit in hits:
                doc = hit.get("document", {})
                if not doc:
                    continue

                title = doc.get("title", "").strip()
                if not title:
                    continue

                # Get author from author_names array
                author_names = doc.get("author_names", [])
                if isinstance(author_names, list) and author_names:
                    author = author_names[0]  # Take first author
                elif isinstance(author_names, str):
                    author = author_names
                else:
                    author = "Unknown Author"

                # Get rating
                rating = doc.get("rating", 0.0)
                rating_count = doc.get("ratings_count", 0)

                # Get page count
                page_count = doc.get("pages", 0)

                # Get description
                description = doc.get("description", "")

                # Get categories/genres
                genres = doc.get("genres", [])

                # Get published date (Hardcover uses release_date)
                published_date = doc.get("release_date", "") or doc.get("releaseDate", "") or doc.get("publishedDate", "")

                # Get ISBN
                isbn = doc.get("isbns", [])[0] if doc.get("isbns") else ""

                # Get cover URL
                image_data = doc.get("image", {}) or {}
                cover_url = image_data.get("url", "")

                book = {
                    "title": title,
                    "author": author,
                    "rating": rating,
                    "rating_count": rating_count,
                    "rating_formatted": f"{rating:.2f}" if rating > 0 else "N/A",
                    "description": description,
                    "page_count": page_count,
                    "published_date": published_date,
                    "categories": genres,
                    "genres": genres,  # Keep both for compatibility
                    "cover_url": cover_url,
                    "isbn": isbn,
                    "source": "hardcover",
                }

                books.append(book)

            logger.info(f"✅ Hardcover: found {len(books)} books for query '{query}'")
            return books

        except Exception as e:
            logger.debug(f"Hardcover.app search failed: {e}")
            return []

    @staticmethod
    def _parse_itunes_book(result):
        """Parse iTunes result"""
        try:
            title = result.get("trackName", "").strip()
            if not title:
                return None

            author = result.get("artistName", "Unknown Author")

            # Get highest quality artwork
            artwork_url = result.get("artworkUrl100", "")
            if artwork_url:
                # Upgrade to maximum resolution
                artwork_url = artwork_url.replace("100x100", "2048x2048")
                artwork_url = artwork_url.replace("60x60", "2048x2048")

            return {
                "title": title,
                "author": author,
                "cover_url": artwork_url,
                "description": result.get("description", ""),
                "source": "itunes",
            }
        except Exception as e:
            logger.debug(f"Error parsing iTunes book: {e}")
            return None

    @staticmethod
    async def aggregate_book_data(query, limit=10):
        """
        Aggregate book data from multiple sources and combine best information.

        Strategy:
        1. Search Google Books and iTunes concurrently
        2. For each Google Books result, try to find a matching iTunes result for cover
        3. Keep Google Books as primary source for description, categories, etc.
        4. Ratings and genres will be fetched lazily from Hardcover/StoryGraph on selection.
        """
        logger.info(f"📚 Aggregating data from multiple sources for: {query}")

        # Run independent searches concurrently (both are pure I/O)
        google_task = asyncio.to_thread(
            MultiSourceBookAggregator.search_google_books, query, limit, False
        )
        itunes_task = asyncio.to_thread(
            MultiSourceBookAggregator.search_itunes, query
        )
        google_books, itunes_books = await asyncio.gather(
            google_task, itunes_task
        )

        # ── If the first Google Books result has no reliable cover, look for a later
        #     result with the same title/author that does have a usable cover.
        if google_books:
            first = google_books[0]
            # A missing or empty cover_url means the volume is unreliable (AACAAJ) or
            # the placeholder image was stripped during parsing.
            if not first.get("cover_url"):
                for later in google_books[1:]:
                    # Compare title and author (already normalized in the parsed dict)
                    if (later["title"] == first["title"] and
                        later["author"] == first["author"] and
                        later.get("cover_url")):          # reliable cover exists
                        # Substitute the first entry with the later one – order of the list
                        # stays the same, only the first element's data changes.
                        first.update(later)
                        break

        # If no results from any source – try Goodreads scraping as a last resort
        if not google_books and not itunes_books:
            logger.warning("No results from any source – trying Goodreads fallback")
            gr_data = await asyncio.to_thread(scrape_goodreads, query)
            if gr_data:
                book = {
                    "title": gr_data.get("title", ""),
                    "author": gr_data.get("author", ""),
                    "rating": gr_data.get("rating", 0.0),
                    "rating_count": gr_data.get("rating_count", 0),
                    "description": gr_data.get("description", ""),
                    "cover_url": gr_data.get("cover_url", ""),
                    "isbn": gr_data.get("isbn", ""),
                    "page_count": gr_data.get("page_count", 0),
                    "published_date": str(gr_data.get("published_date", "")),
                    "info_link": f"https://www.goodreads.com/search?q={requests.utils.quote(query)}",
                    "source": "goodreads",
                    "rating_formatted": f"{gr_data.get('rating', 0):.2f}" if gr_data.get("rating") else "N/A",
                }
                # Goodreads fallback bypasses the lazy reset, so copy rating to list-only fields too
                book["search_rating"] = book.get("rating", 0.0)
                book["search_rating_count"] = book.get("rating_count", 0)
                book["search_rating_formatted"] = (
                    f"{book['search_rating']:.2f}" if book["search_rating"] else "N/A"
                )
                logger.info(f"✅ Goodreads fallback found: {book['title']}")
                return [book]
            logger.warning("No results from any source")
            return []

        # Use Google Books as primary source (best descriptions and metadata)
        aggregated_books = []

        for gb_book in google_books[:limit]:
            # Start with Google Books data
            book = gb_book.copy()

            # Try to enhance with iTunes cover (higher quality) – only if confident match
            itunes_match = MultiSourceBookAggregator._find_matching_book_strict(
                book["title"], book["author"], itunes_books
            )
            if itunes_match and itunes_match.get("cover_url"):
                logger.info(f"📸 Using iTunes cover for: {book['title']}")
                book["cover_url"] = itunes_match["cover_url"]
                book["cover_source"] = "itunes"
            else:
                book["cover_source"] = "google_books"

            # Store Google Books rating in list-only fields before lazy reset (used for search result display)
            # These fields are NOT overwritten by the rating=0.0 reset below.
            book["search_rating"] = book.get("rating", 0.0)
            book["search_rating_count"] = book.get("rating_count", 0)
            book["search_rating_formatted"] = (
                f"{book['search_rating']:.2f}" if book["search_rating"] else "N/A"
            )

            # Hide rating in search results; will be fetched lazily on selection
            book["rating"] = 0.0
            book["rating_count"] = 0
            book["rating_formatted"] = "N/A"

            aggregated_books.append(book)

        # If Google Books had no results, use iTunes as primary
        if not aggregated_books and itunes_books:
            for itunes_book in itunes_books[:limit]:
                book = itunes_book.copy()
                book["cover_source"] = "itunes"

                # iTunes does not provide ratings; safe default prevents KeyError when GB
                # fails/returns no English results and iTunes fallback is used.
                book["rating"] = 0.0
                book["rating_count"] = 0

                # Store list-only rating fields (same as Google Books path above)
                book["search_rating"] = 0.0
                book["search_rating_count"] = 0
                book["search_rating_formatted"] = "N/A"

                # Format rating
                book["rating_formatted"] = "N/A"
                aggregated_books.append(book)

        logger.info(f"✅ Aggregated {len(aggregated_books)} books with enhanced data")
        return aggregated_books

    @staticmethod
    def _find_matching_book_strict(title, author, book_list):
        """Find matching book in list by title/author similarity with stricter threshold."""
        title_lower = title.lower()
        author_lower = author.lower()

        for book in book_list:
            book_title = book.get("title", "").lower()
            book_author = book.get("author", "").lower()

            # Check if titles match (fuzzy) – require high similarity
            if title_lower in book_title or book_title in title_lower or MultiSourceBookAggregator._similarity(
                title_lower, book_title
            ) > 0.8:
                # Check if authors match – require high similarity
                if author_lower in book_author or book_author in author_lower or MultiSourceBookAggregator._similarity(
                    author_lower, book_author
                ) > 0.8:
                    return book

        return None

    @staticmethod
    def _similarity(s1, s2):
        """Enhanced string similarity that handles author name variations"""
        import re

        try:
            # Normalize strings: lowercase, remove extra spaces
            norm_s1 = re.sub(r"\s+", " ", s1.strip().lower())
            norm_s2 = re.sub(r"\s+", " ", s2.strip().lower())

            # Basic Jaccard similarity on words
            words1 = set(norm_s1.split())
            words2 = set(norm_s2.split())
            intersection = words1.intersection(words2)
            union = words1.union(words2)
            jaccard_sim = len(intersection) / len(union) if union else 0

            # Also check for substring containment (good for "J.R.R. Tolkien" in "J. R. R. Tolkien")
            substr_sim = 0
            if norm_s1 in norm_s2 or norm_s2 in norm_s1:
                substr_sim = 1.0
            # Also try without spaces around periods
            norm_s1_no_spaces = norm_s1.replace(" ", "")
            norm_s2_no_spaces = norm_s2.replace(" ", "")
            if norm_s1_no_spaces in norm_s2_no_spaces or norm_s2_no_spaces in norm_s1_no_spaces:
                substr_sim = 1.0

            # Return the best match
            return max(jaccard_sim, substr_sim)
        except Exception:
            return 0

    @staticmethod
    def _get_hardcover_data(isbn: str, title: str = "", author: str = "") -> tuple:
        """Fetch book data from Hardcover.app GraphQL API.

        Returns tuple: (rating, ratings_count, genres, image_url)
        Uses search with query_type: 'Book' and selects the hit with highest ratings_count.
        """
        api_key = os.getenv("HARDCOVER_API_KEY", "").strip()
        if not api_key:
            return 0.0, 0, [], ""

        try:
            # Build search query - prefer title+author for accuracy
            if title and author:
                search_query = f"{title} {author}".strip()
            elif title:
                search_query = title.strip()
            elif isbn:
                search_query = isbn.replace("-", "").strip()
            else:
                return 0.0, 0, [], ""

            if not search_query:
                return 0.0, 0, [], ""

            query = """
            query SearchBooks($q: String!, $limit: Int) {
                search(query: $q, query_type: "Book", per_page: $limit) {
                    ids
                    results
                }
            }
            """
            variables = {"q": search_query, "limit": 10}
            resp = requests.post(
                "https://api.hardcover.app/v1/graphql",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={"query": query, "variables": variables},
                timeout=10,
            )
            if resp.status_code != 200:
                logger.debug(f"Hardcover GraphQL error {resp.status_code}: {resp.text[:200]}")
                return 0.0, 0, [], ""

            data = resp.json()
            if "errors" in data:
                logger.debug(f"Hardcover GraphQL errors: {data['errors']}")
                return 0.0, 0, [], ""

            search_result = data.get("data", {}).get("search", {})
            raw_results = search_result.get("results", {})
            # results is a JSONB object, may be string or dict
            results_json = json.loads(raw_results) if isinstance(raw_results, str) else raw_results
            hits = results_json.get("hits", [])

            if not hits:
                return 0.0, 0, [], ""

            # Determine if we have title/author to filter by
            title_lower = title.lower() if title else ""
            author_lower = author.lower() if author else ""
            have_title = bool(title_lower)
            have_author = bool(author_lower)

            # Helper to check if a hit matches the title/author
            def _matches_title_author(doc):
                doc_title = doc.get("title", "").lower()
                doc_authors = doc.get("author_names", [])
                if isinstance(doc_authors, list):
                    doc_authors_str = " ".join(doc_authors).lower()
                else:
                    doc_authors_str = str(doc_authors).lower()

                title_match = (not have_title) or (title_lower and title_lower in doc_title)
                author_match = (not have_author) or (author_lower and author_lower in doc_authors_str)
                return title_match and author_match

            # First, try to find hits that match the title/author
            if have_title or have_author:
                filtered_hits = [h for h in hits if _matches_title_author(h.get("document", {}))]
                if filtered_hits:
                    best_hit = max(
                        filtered_hits, key=lambda h: h.get("document", {}).get("ratings_count") or 0
                    )
                    logger.debug(f"Hardcover: selected from {len(filtered_hits)} title/author matches")
                else:
                    # Fallback to highest ratings_count among all hits if no title/author match
                    best_hit = max(hits, key=lambda h: h.get("document", {}).get("ratings_count") or 0)
                    logger.debug(
                        f"Hardcover: no title/author match, falling back to highest ratings_count among {len(hits)} hits"
                    )
            else:
                # No title/author to filter by, just take the highest ratings_count
                best_hit = max(hits, key=lambda h: h.get("document", {}).get("ratings_count") or 0)
                logger.debug(f"Hardcover: no title/author filter, selecting highest ratings_count among {len(hits)} hits")

            doc = best_hit.get("document", {})

            rating = doc.get("rating") or 0.0
            ratings_count = doc.get("ratings_count") or 0
            genres = doc.get("genres", []) or []
            image_data = doc.get("image", {}) or {}
            image_url = image_data.get("url", "")

            if rating and ratings_count:
                logger.info(f"⭐ Hardcover.app: {rating}/5 from {ratings_count} ratings for '{title or isbn}'")
                return round(float(rating), 2), int(ratings_count), genres, image_url
            else:
                logger.debug(f"Hardcover hit has no rating: rating={rating}, count={ratings_count}")
                return 0.0, 0, [], ""

        except Exception as e:
            logger.debug(f"Hardcover.app data lookup failed: {e}")
            return 0.0, 0, [], ""

    @staticmethod
    def _get_storygraph_ratings(isbn: str, title: str = "", author: str = "") -> tuple:
        """Fetch ratings from StoryGraph API.

        Uses the storygraph-api package: pip install storygraph-api
        API docs: https://pypi.org/project/storygraph-api/

        Note: storygraph-api uses underscores (storygraph_api, not storygraph).
        """
        try:
            from storygraph_api import Book

            book_client = Book()
            query = f"{title} {author}".strip() if title or author else ""
            results = book_client.search(query) if query else []

            result = None
            if results and isinstance(results, list) and len(results) > 0:
                # Try to parse JSON from results
                import json
                try:
                    parsed = json.loads(results[0])
                    if isinstance(parsed, list) and len(parsed) > 0:
                        result = parsed[0]
                    elif isinstance(parsed, dict):
                        result = parsed
                except (json.JSONDecodeError, ValueError):
                    pass

            if not result:
                return 0.0, 0

            avg = result.get("rating") or result.get("avg_rating") or 0
            count = result.get("rating_count") or result.get("num_ratings") or 0

            if avg and count:
                logger.info(f"⭐ StoryGraph: {avg}/5 from {count} ratings")
                return round(float(avg), 2), int(count)
        except ImportError:
            logger.debug("storygraph-api not installed: pip install storygraph-api")
        except Exception as e:
            logger.debug(f"StoryGraph rating lookup failed: {e}")
        return 0.0, 0

    @staticmethod
    def _ensure_ratings(book: dict) -> tuple:
        """Ensure book has ratings by fetching from Hardcover/StoryGraph if missing.

        When a normal `/search` result is selected, the book dict may already
        contain `_hardcover_match` — a cached Hardcover result from the search.
        If so, reuse that data directly without making another API call.

        Args:
            book: Dictionary with book data (must have 'title', 'author', optionally 'isbn')

        Returns:
            Tuple of (updated_book, hc_data_tuple) where hc_data_tuple is the
            cached Hardcover result (rating, count, genres, cover_url) so callers
            like _ensure_cover can reuse it without a redundant API call.
        """
        # 1) Reuse cached Hardcover match from normal search if available
        hc_match = book.get("_hardcover_match")
        if hc_match:
            hc_rating = hc_match.get("rating") or 0.0
            hc_count = hc_match.get("rating_count") or 0
            hc_genres = hc_match.get("categories", [])
            hc_cover = hc_match.get("cover_url") or ""

            if hc_rating > 0:
                book["rating"] = hc_rating
                book["rating_count"] = hc_count
                book["rating_source"] = "hardcover"
                book["rating_formatted"] = f"{hc_rating:.2f}"
                if hc_genres:
                    book["categories"] = hc_genres
                if hc_cover and not book.get("cover_url"):
                    book["cover_url"] = hc_cover
                    book["cover_source"] = "hardcover"
                logger.info(f"Reusing cached Hardcover data for: {book.get('title', '')}")
                return book, (hc_rating, hc_count, hc_genres, hc_cover)

        # 2) If already has a rating, still fetch hc_data for _ensure_cover reuse
        if book.get("rating") and book["rating"] > 0:
            hc_data = MultiSourceBookAggregator._get_hardcover_cached(
                book.get("isbn", ""), book.get("title", ""), book.get("author", "")
            )
            return book, hc_data

        # 3) Hardcover API lookup (cached to avoid repeat calls)
        hc_rating, hc_count, hc_genres, hc_cover = MultiSourceBookAggregator._get_hardcover_cached(
            book.get("isbn", ""), book.get("title", ""), book.get("author", "")
        )
        if hc_rating > 0:
            book["rating"] = hc_rating
            book["rating_count"] = hc_count
            book["rating_source"] = "hardcover"
            book["rating_formatted"] = f"{hc_rating:.2f}"
            # Store Hardcover genres and cover for later use
            if hc_genres:
                book["categories"] = hc_genres
            if hc_cover and not book.get("cover_url"):
                book["cover_url"] = hc_cover
                book["cover_source"] = "hardcover"
            return book, (hc_rating, hc_count, hc_genres, hc_cover)

        # Fallback to StoryGraph
        sg_rating, sg_count = MultiSourceBookAggregator._get_storygraph_ratings(
            book.get("isbn", ""), book.get("title", ""), book.get("author", "")
        )
        if sg_rating > 0:
            book["rating"] = sg_rating
            book["rating_count"] = sg_count
            book["rating_source"] = "storygraph"
            book["rating_formatted"] = f"{sg_rating:.2f}"

        return book, (hc_rating, hc_count, hc_genres, hc_cover)

    @staticmethod
    def _get_openlibrary_cover(isbn: str) -> str:
        """Return a real Open Library cover URL for an ISBN, or '' if none.

        Open Library's ``?default=false`` responds 404 when it has no cover, so
        a 200 carrying a genuine (non-placeholder) image means success.
        """
        if not isbn:
            return ""
        clean = isbn.replace("-", "").strip()
        if not clean:
            return ""
        check_url = f"https://covers.openlibrary.org/b/isbn/{clean}-L.jpg?default=false"
        try:
            r = requests.get(check_url, headers=HEADERS, timeout=10)
            if r.status_code == 200 and len(r.content) > 3000 and not is_placeholder_image(r.content):
                logger.info(f"🖼️ Open Library cover found for ISBN {clean}")
                return f"https://covers.openlibrary.org/b/isbn/{clean}-L.jpg"
        except Exception as e:
            logger.debug(f"Open Library cover lookup failed: {e}")
        return ""

    @staticmethod
    def _ensure_cover(book: dict, hc_data: tuple = None) -> dict:
        """Ensure the book has a real cover image.

        Google Books placeholder covers are dropped during parsing, so this
        fills a missing cover — priority: iTunes (high quality) → Hardcover → Open Library.
        Called lazily when the user selects a book so searches stay fast.

        Args:
            book: Book dictionary.
            hc_data: Optional cached Hardcover result (rating, count, genres, cover_url)
                     from _ensure_ratings to avoid a redundant API call.
        """
        if book.get("cover_url"):
            return book  # already have a cover — keep it

        # 1) iTunes by title/author (highest quality artwork)
        try:
            title = book.get("title", "")
            author = book.get("author", "")
            itunes = MultiSourceBookAggregator.search_itunes(f"{title} {author}".strip())
            match = MultiSourceBookAggregator._find_matching_book_strict(title, author, itunes)
            if match and match.get("cover_url"):
                book["cover_url"] = match["cover_url"]
                book["cover_source"] = "itunes"
                logger.info(f"🖼️ iTunes cover found for: {title}")
                return book
        except Exception as e:
            logger.debug(f"iTunes cover lookup failed: {e}")

        # 2) Hardcover by title/author/isbn (good quality, community-driven)
        #    Reuse cached data from _ensure_ratings when available to avoid a
        #    redundant network request for the same book.
        try:
            title = book.get("title", "")
            author = book.get("author", "")
            isbn = book.get("isbn", "")
            if hc_data is not None:
                _, _, _, hc_cover = hc_data
            else:
                _, _, _, hc_cover = MultiSourceBookAggregator._get_hardcover_cached(isbn, title, author)
            if hc_cover:
                book["cover_url"] = hc_cover
                book["cover_source"] = "hardcover"
                logger.info(f"🖼️ Hardcover cover found for: {title}")
                return book
        except Exception as e:
            logger.debug(f"Hardcover cover lookup failed: {e}")

        # 3) Open Library by ISBN — reliable, 404s when it has no cover
        ol = MultiSourceBookAggregator._get_openlibrary_cover(book.get("isbn", ""))
        if ol:
            book["cover_url"] = ol
            book["cover_source"] = "open_library"
            logger.info(f"🖼️ Open Library cover found for ISBN {book.get('isbn', '')}")
            return book

        return book

    # ── Hardcover rating cache ─────────────────────────────────────────────────
    # Per-entry TTL cache so repeated lookups for the same book don't waste API
    # calls.  Entries expire after _HC_CACHE_TTL seconds; the cron no longer
    # needs to flush the whole cache daily.
    # Key = (norm_isbn, norm_title, norm_author)
    # Value = (result_tuple, insert_timestamp)
    _hc_cache: dict = {}
    _HC_CACHE_TTL: int = 6 * 3600  # 6 hours

    @staticmethod
    def _get_hardcover_cached(isbn: str, title: str, author: str) -> tuple:
        """Cached wrapper around _get_hardcover_data with per-entry TTL.

        Cache is keyed by (isbn, title, author) so the same book looked up
        multiple times (e.g. same ISBN in GB + iTunes results) hits cache.
        """
        import time as _time
        now = _time.time()
        norm = (
            isbn.replace("-", "").strip().lower() if isbn else "",
            title.strip().lower() if title else "",
            author.strip().lower() if author else "",
        )
        cached = MultiSourceBookAggregator._hc_cache.get(norm)
        if cached is not None:
            result, ts = cached
            if now - ts < MultiSourceBookAggregator._HC_CACHE_TTL:
                logger.debug(f"🔁 Hardcover cache hit: {title or isbn}")
                return result
            # Expired — remove so we re-fetch below
            MultiSourceBookAggregator._hc_cache.pop(norm, None)

        result = MultiSourceBookAggregator._get_hardcover_data(isbn, title, author)

        # Cap cache size to bound memory usage (drop oldest entries)
        if len(MultiSourceBookAggregator._hc_cache) >= 512:
            # Evict the oldest 64 entries by timestamp
            sorted_keys = sorted(
                MultiSourceBookAggregator._hc_cache,
                key=lambda k: MultiSourceBookAggregator._hc_cache[k][1],
            )
            for old_key in sorted_keys[:64]:
                MultiSourceBookAggregator._hc_cache.pop(old_key, None)

        MultiSourceBookAggregator._hc_cache[norm] = (result, now)
        return result

    @staticmethod
    def _flush_hc_cache():
        """Clear the Hardcover cache (called by Vercel cron for housekeeping)."""
        MultiSourceBookAggregator._hc_cache.clear()

    # ── Hardcover cover fallback ─────────────────────────────────────────────
    @staticmethod
    def _get_hardcover_cover(isbn: str = "", title: str = "", author: str = "") -> str:
        """Fetch cover image URL from Hardcover.app as a fallback.

        Delegates to the cached unified lookup (_get_hardcover_data) so it shares
        the same single API call as ratings/genres. Returns a URL or ''.
        """
        try:
            _, _, _, cover_url = MultiSourceBookAggregator._get_hardcover_cached(isbn, title, author)
            return cover_url or ""
        except Exception as e:
            logger.debug(f"Hardcover cover lookup error: {e}")
            return ""
