"""MultiSourceBookAggregator — aggregates book data from multiple sources."""

import asyncio
import os
import json
import re
import time
import requests

from src.utils import (
    HEADERS, get_http_session, logger, is_placeholder_image,
    is_unreliable_gb_cover, translate_to_english,
)
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

            # For an explicit "title by author" query, search the fields
            # separately. The plain query makes Google Books treat every word
            # as a full-text term, so a partial author name can bury the title.
            by_match = re.split(r"\s+by\s+", query, maxsplit=1, flags=re.IGNORECASE)
            title_author = (by_match[0].strip(), by_match[1].strip()) if len(by_match) == 2 else None
            primary_query = query
            if title_author and all(title_author):
                safe_title = title_author[0].replace('"', " ").strip()
                primary_query = f'intitle:"{safe_title}" inauthor:{title_author[1]}'

            def pair_matches(item):
                """Require evidence for both sides before accepting a fallback hit."""
                if not title_author:
                    return True
                info = item.get("volumeInfo") or {}
                title_text = info.get("title", "")
                authors = info.get("authors", [])
                author_text = " ".join(authors) if authors else ""
                # Use \w+ to match word characters across all scripts (Unicode-aware).
                # This correctly handles CJK, Cyrillic, Arabic, etc. in titles and author names.
                title_words = set(re.findall(r"\w+", title_author[0].casefold()))
                author_words = set(re.findall(r"\w+", title_author[1].casefold()))
                result_title = set(re.findall(r"\w+", title_text.casefold()))
                result_author = set(re.findall(r"\w+", author_text.casefold()))
                title_coverage = len(title_words & result_title) / max(1, len(title_words))
                if title_coverage >= 0.5 and bool(author_words & result_author):
                    return True
                # Cross-script author matching: when the query author is ASCII/Latin but the
                # result author contains non-ASCII characters (CJK, Cyrillic, Arabic, etc.),
                # the token-based check above can't connect Romanized names to native script.
                # Trust the title match — Google Books' own transliteration handled the author.
                if title_coverage >= 0.5 and author_words and not (author_words & result_author):
                    try:
                        ascii_author = title_author[1].encode("ascii").decode("ascii")
                        has_non_ascii = any(ord(c) > 127 for c in author_text)
                        if ascii_author and has_non_ascii:
                            return True
                    except (UnicodeDecodeError, UnicodeEncodeError):
                        pass
                return False

            params = {
                "q": primary_query,
                # Google Books permits up to 40 results per request. Keep
                # normal Telegram searches small via their limit=10, while
                # allowing the paginated Mini App author search to ask for more.
                "maxResults": min(limit, 40),
                "printType": "books",
                "orderBy": "relevance",
            }

            if GOOGLE_BOOKS_API_KEY:
                params["key"] = GOOGLE_BOOKS_API_KEY

            def fetch_items(request_params, label):
                try:
                    response = get_http_session().get(
                        url, params=request_params, timeout=8
                    )
                    if response.status_code != 200:
                        logger.warning(
                            "Google Books %s API error: %s",
                            label, response.status_code,
                        )
                        return None
                    return response.json().get("items", []) or []
                except Exception as exc:
                    logger.warning(
                        "Google Books %s request failed: %s: %s",
                        label, type(exc).__name__, exc,
                    )
                    return None

            items = fetch_items(params, "primary")
            if items is None:
                # Existing ordinary searches keep the prior fail-open behavior.
                # Explicit title/author searches may still recover through the
                # validated language-neutral fallbacks below.
                if not title_author:
                    return []
                items = []

            if title_author:
                items = [item for item in items if pair_matches(item)]
                explicit_pair_fallback = False
                if not items:
                    # Retry both fields without a language restriction first,
                    # then query author-only and exact title. Every fallback
                    # candidate must still match both sides.
                    fallback_queries = [
                        (
                            f'intitle:"{title_author[0].replace(chr(34), " ").strip()}" '
                            f'inauthor:"{title_author[1].replace(chr(34), " ").strip()}"',
                            "all-languages",
                        ),
                        (
                            f'"{title_author[0].replace(chr(34), " ").strip()}" '
                            f'"{title_author[1].replace(chr(34), " ").strip()}"',
                            "combined",
                        ),
                        (f'inauthor:"{title_author[1].replace(chr(34), " ").strip()}"', "author"),
                        (f'intitle:"{title_author[0].replace(chr(34), " ").strip()}"', "title"),
                    ]
                    for fallback_query, fallback_label in fallback_queries:
                        fallback_params = {
                            "q": fallback_query,
                            "maxResults": 40,
                            "printType": "books",
                            "orderBy": "relevance",
                        }
                        if GOOGLE_BOOKS_API_KEY:
                            fallback_params["key"] = GOOGLE_BOOKS_API_KEY
                        fallback_items = fetch_items(
                            fallback_params, f"{fallback_label} fallback"
                        )
                        if fallback_items is None:
                            continue
                        matched_items = [
                            item for item in fallback_items if pair_matches(item)
                        ]
                        logger.info(
                            "GB explicit-pair %s fallback: raw=%d matched=%d query=%r",
                            fallback_label, len(fallback_items), len(matched_items),
                            fallback_query,
                        )
                        if matched_items:
                            items = matched_items
                            explicit_pair_fallback = any(
                                (item.get("volumeInfo") or {}).get("language") != "en"
                                for item in matched_items
                            )
                            break
                # The pair matcher already verifies relevance. Preserve matching
                # non-English editions returned by the language-neutral fallback.
            else:
                explicit_pair_fallback = False

            # Preserve results in every catalog language. Language is metadata,
            # not a relevance gate; translated editions may have English titles.
            raw_count = len(items)
            # Also reject items with blank/missing titles as a basic validity check.
            items = [item for item in items if (item.get("volumeInfo") or {}).get("title", "").strip()]
            invalid_count = raw_count - len(items)
            logger.info(
                f"GB: raw={raw_count} invalid_title_filtered={invalid_count} final={len(items)}"
            )
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
                "language": vol.get("language", ""),
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

            response = get_http_session().get(url, params=params, timeout=8)
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
    def search_hardcover(
        query: str,
        limit: int = 10,
        *,
        sort: str = "",
        fields: str = "",
        weights: str = "",
    ) -> list[dict]:
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

            search_arguments = [
                "query: $q",
                'query_type: "Book"',
                "per_page: $limit",
            ]
            variable_definitions = ["$q: String!", "$limit: Int"]
            variables = {"q": search_query, "limit": limit}
            for name, value in (("sort", sort), ("fields", fields), ("weights", weights)):
                if value:
                    search_arguments.append(f"{name}: ${name}")
                    variable_definitions.append(f"${name}: String")
                    variables[name] = value

            query_string = f"""
            query SearchBooks({", ".join(variable_definitions)}) {{
                search({", ".join(search_arguments)}) {{
                    results
                }}
            }}
            """
            resp = get_http_session().post(
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

                # Preserve Hardcover's own tags separately from categories
                # combined from multiple providers later in the pipeline.
                genres = doc.get("genres", [])
                if not genres:
                    genres = MultiSourceBookAggregator._extract_hardcover_tags(
                        doc.get("cached_tags"), "Genre"
                    )
                moods = MultiSourceBookAggregator._extract_hardcover_tags(
                    doc.get("cached_tags"), "Mood"
                )
                if not moods:
                    moods = MultiSourceBookAggregator._extract_hardcover_tags(
                        {"Mood": doc.get("moods")}, "Mood"
                    )
                content_warnings = MultiSourceBookAggregator._extract_hardcover_tags(
                    doc.get("cached_tags"), "ContentWarning"
                )
                if not content_warnings:
                    content_warnings = MultiSourceBookAggregator._extract_hardcover_tags(
                        {"ContentWarning": doc.get("content_warnings") or doc.get("content_warnings_names")},
                        "ContentWarning",
                    )

                # Get published date (Hardcover uses release_date)
                published_date = doc.get("release_date", "") or doc.get("releaseDate", "") or doc.get("publishedDate", "")

                # Get ISBN
                isbn = doc.get("isbns", [])[0] if doc.get("isbns") else ""

                # Get cover URL
                image_data = doc.get("image", {}) or {}
                cover_url = image_data.get("url", "")

                book = {
                    "hardcover_id": int(doc.get("id")) if str(doc.get("id") or "").isdigit() else 0,
                    "title": title,
                    "author": author,
                    "rating": rating,
                    "rating_count": rating_count,
                    "users_read_count": int(doc.get("users_read_count") or 0),
                    "rating_formatted": f"{rating:.2f}" if rating > 0 else "N/A",
                    "description": description,
                    "page_count": page_count,
                    "published_date": published_date,
                    "categories": genres,
                    "genres": genres,  # Keep both for compatibility
                    "hardcover_genres": genres,
                    "hardcover_moods": moods,
                    "hardcover_content_warnings": content_warnings,
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
    def _extract_hardcover_tags(raw_tags, category: str) -> list[str]:
        if isinstance(raw_tags, str):
            try:
                raw_tags = json.loads(raw_tags)
            except (TypeError, ValueError):
                return []
        if not isinstance(raw_tags, dict):
            return []
        normalized = re.sub(r"[^a-z]", "", category.casefold())
        values = next((value for key, value in raw_tags.items()
                       if re.sub(r"[^a-z]", "", str(key).casefold()) == normalized), None)
        if values is None:
            values = raw_tags.get("tags")
            if isinstance(values, dict):
                values = values.get(category)
            elif isinstance(values, list):
                values = [item for item in values if isinstance(item, dict)
                          and re.sub(r"[^a-z]", "", str(item.get("category") or "").casefold()) == normalized]
        if not isinstance(values, list):
            return []
        result, seen = [], set()
        for item in values:
            value = item.get("tag") or item.get("name") or "" if isinstance(item, dict) else item
            value = " ".join(str(value or "").split())
            if value and value.casefold() not in seen:
                result.append(value)
                seen.add(value.casefold())
        return result

    @staticmethod
    def search_hardcover_similar(book_id: int, limit: int = 20) -> list[dict]:
        """Fetch Hardcover's cached, ranked similar-book list for a catalog book."""
        api_key = os.getenv("HARDCOVER_API_KEY", "").strip()
        try:
            book_id = int(book_id)
            limit = max(1, min(int(limit), 50))
        except (TypeError, ValueError, OverflowError):
            return []
        if not api_key or book_id <= 0:
            return []

        endpoint = "https://api.hardcover.app/v1/graphql"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        session = get_http_session()
        try:
            similar_query = """
            query SimilarBookIds($id: Int!) {
              books(where: {id: {_eq: $id}}, limit: 1) {
                id
                cached_similar_book_ids
              }
            }
            """
            response = session.post(endpoint, headers=headers,
                                    json={"query": similar_query, "variables": {"id": book_id}}, timeout=10)
            if response.status_code != 200:
                logger.debug("Hardcover similar-id request failed status=%s", response.status_code)
                return []
            payload = response.json()
            rows = (payload.get("data") or {}).get("books") or []
            if payload.get("errors") or not rows:
                return []
            ids = rows[0].get("cached_similar_book_ids") or []
            if isinstance(ids, str):
                try:
                    ids = json.loads(ids)
                except (TypeError, ValueError):
                    ids = []
            if not isinstance(ids, list):
                return []
            ordered_ids = []
            seen = set()
            for value in ids:
                try:
                    value = int(value)
                except (TypeError, ValueError, OverflowError):
                    continue
                if value > 0 and value != book_id and value not in seen:
                    seen.add(value)
                    ordered_ids.append(value)
                if len(ordered_ids) >= limit:
                    break
            if not ordered_ids:
                return []

            hydrate_query = """
            query HydrateSimilarBooks($ids: [Int!]) {
              books(where: {id: {_in: $ids}}) {
                id title rating ratings_count description pages release_date cached_tags
                image { url }
                contributions(limit: 1) { author { name } }
                default_cover_edition { release_date pages isbn_10 isbn_13 image { url } }
              }
            }
            """
            hydrated = session.post(endpoint, headers=headers,
                                    json={"query": hydrate_query, "variables": {"ids": ordered_ids}}, timeout=12)
            if hydrated.status_code != 200:
                logger.debug("Hardcover similar-book hydration failed status=%s", hydrated.status_code)
                return []
            hydrated_payload = hydrated.json()
            if hydrated_payload.get("errors"):
                return []
            records = (hydrated_payload.get("data") or {}).get("books") or []
            by_id = {int(item["id"]): item for item in records if isinstance(item, dict) and str(item.get("id") or "").isdigit()}
            results = []

            def extract_tags(raw, category):
                if isinstance(raw, str):
                    try:
                        raw = json.loads(raw)
                    except (TypeError, ValueError):
                        return []
                if not isinstance(raw, dict):
                    return []
                values = raw.get(category)
                if values is None:
                    values = raw.get("tags")
                    if isinstance(values, dict):
                        values = values.get(category)
                    elif isinstance(values, list):
                        values = [tag for tag in values if isinstance(tag, dict)
                                  and str(tag.get("category") or "").casefold() == category.casefold()]
                if not isinstance(values, list):
                    return []
                tags = []
                for value in values:
                    value = value.get("tag") or value.get("name") or "" if isinstance(value, dict) else value
                    value = str(value or "").strip()
                    if value and value not in tags:
                        tags.append(value)
                return tags

            for similar_id in ordered_ids:
                doc = by_id.get(similar_id)
                if not doc:
                    continue
                edition = doc.get("default_cover_edition") or {}
                image = doc.get("image") or edition.get("image") or {}
                contributions = doc.get("contributions") or []
                author = ""
                for contribution in contributions:
                    author_data = contribution.get("author") or {}
                    if author_data.get("name"):
                        author = str(author_data["name"])
                        break
                results.append({
                    "hardcover_id": similar_id,
                    "title": str(doc.get("title") or "").strip(),
                    "author": author or "Unknown author",
                    "rating": doc.get("rating") or 0,
                    "rating_count": doc.get("ratings_count") or 0,
                    "description": str(doc.get("description") or ""),
                    "page_count": doc.get("pages") or edition.get("pages") or 0,
                    "published_date": str(doc.get("release_date") or edition.get("release_date") or ""),
                    "categories": extract_tags(doc.get("cached_tags"), "Genre"),
                    "genres": extract_tags(doc.get("cached_tags"), "Genre"),
                    "moods": extract_tags(doc.get("cached_tags"), "Mood"),
                    "cover_url": str(image.get("url") or ""),
                    "isbn": str(edition.get("isbn_13") or edition.get("isbn_10") or ""),
                    "source": "hardcover",
                })
            return results
        except Exception as exc:
            logger.debug("Hardcover similar-books lookup failed: %s", type(exc).__name__)
            return []

    @staticmethod
    def get_hardcover_detail_metadata(book_id: int) -> dict:
        """Fetch Hardcover-only discovery metadata for one known catalog book."""
        api_key = os.getenv("HARDCOVER_API_KEY", "").strip()
        try:
            book_id = int(book_id)
        except (TypeError, ValueError, OverflowError):
            logger.info("[hardcover-details] skipped invalid book id=%r", book_id)
            return {}
        if not api_key:
            logger.warning("[hardcover-details] skipped book_id=%s: HARDCOVER_API_KEY is missing", book_id)
            return {}
        if book_id <= 0:
            logger.info("[hardcover-details] skipped non-positive book_id=%s", book_id)
            return {}

        query = """
        query HardcoverBookDetails($id: Int!) {
          books(where: {id: {_eq: $id}}, limit: 1) {
            id users_read_count cached_tags
            default_cover_edition { language { code2 } }
          }
        }
        """
        try:
            response = get_http_session().post(
                "https://api.hardcover.app/v1/graphql",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"query": query, "variables": {"id": book_id}},
                timeout=10,
            )
            if response.status_code != 200:
                logger.warning("[hardcover-details] book_id=%s metadata HTTP status=%s", book_id, response.status_code)
                return {}
            payload = response.json()
            if payload.get("errors"):
                logger.warning("[hardcover-details] book_id=%s metadata GraphQL errors=%s", book_id, payload["errors"])
            rows = (payload.get("data") or {}).get("books") or []
            if not rows:
                logger.info("[hardcover-details] book_id=%s returned no book rows", book_id)
                return {}
            row = rows[0]
            if int(row.get("id") or 0) != book_id:
                logger.warning("[hardcover-details] requested book_id=%s but response returned id=%s", book_id, row.get("id"))
                return {}

            series_name = ""
            series_position = 0.0
            series_context = {}
            series_query = """
            query HardcoverBookSeries($id: Int!) {
              books(where: {id: {_eq: $id}}, limit: 1) {
                book_series(limit: 5) {
                  position compilation featured
                  series {
                    id name description books_count primary_books_count is_completed
                    book_series(
                      limit: 100,
                      order_by: {position: asc},
                      where: {featured: {_eq: true}}
                    ) {
                      position compilation featured
                      book {
                        id canonical_id title compilation image { url } cached_image
                        default_cover_edition { image { url } language { code2 } }
                        contributions(limit: 1) { author { name } }
                      }
                    }
                  }
                }
              }
            }
            """
            try:
                series_response = get_http_session().post(
                    "https://api.hardcover.app/v1/graphql",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={"query": series_query, "variables": {"id": book_id}},
                    timeout=8,
                )
                if series_response.status_code == 200:
                    series_data = series_response.json()
                    if series_data.get("errors"):
                        logger.warning("[hardcover-details] book_id=%s series GraphQL errors=%s", book_id, series_data["errors"])
                    series_books = (series_data.get("data") or {}).get("books") or []
                    series_rows = series_books[0].get("book_series") or [] if series_books else []
                    series_rows = sorted(
                        (entry for entry in series_rows if isinstance(entry, dict)),
                        key=lambda entry: bool(entry.get("featured")),
                        reverse=True,
                    )
                    for entry in series_rows:
                        if not isinstance(entry, dict):
                            continue
                        series = entry.get("series") or {}
                        if series.get("name"):
                            series_name = str(series["name"]).strip()
                            try:
                                series_position = max(0, float(entry.get("position") or 0))
                            except (TypeError, ValueError, OverflowError):
                                series_position = 0.0
                            primary_books = []
                            collections = []
                            preferred_language = str(
                                ((row.get("default_cover_edition") or {}).get("language") or {}).get("code2") or ""
                            ).casefold()
                            for member in series.get("book_series") or []:
                                if not isinstance(member, dict):
                                    continue
                                member_book = member.get("book") or {}
                                member_title = str(member_book.get("title") or "").strip()
                                if not member_title:
                                    continue
                                member_edition = member_book.get("default_cover_edition") or {}
                                cached_image = member_book.get("cached_image") or {}
                                if isinstance(cached_image, str):
                                    try:
                                        cached_image = json.loads(cached_image)
                                    except (TypeError, ValueError):
                                        cached_image = {}
                                member_image = (
                                    member_book.get("image") or member_edition.get("image")
                                    or (cached_image if isinstance(cached_image, dict) else {})
                                )
                                member_author = ""
                                for contribution in member_book.get("contributions") or []:
                                    author = (contribution or {}).get("author") or {}
                                    if author.get("name"):
                                        member_author = str(author["name"]).strip()
                                        break
                                try:
                                    member_position = max(0, float(member.get("position") or 0))
                                except (TypeError, ValueError, OverflowError):
                                    member_position = 0.0
                                try:
                                    canonical_id = max(0, int(member_book.get("canonical_id") or 0))
                                except (TypeError, ValueError, OverflowError):
                                    canonical_id = 0
                                item = {
                                    "hardcover_id": int(member_book.get("id") or 0),
                                    "canonical_id": canonical_id,
                                    "title": member_title[:250],
                                    "author": member_author[:250] or "Unknown author",
                                    "position": int(member_position) if member_position.is_integer() else member_position,
                                    "cover_url": str(member_image.get("url") or "")[:2000],
                                    "_canonical_id": canonical_id,
                                    "_language": str(((member_edition.get("language") or {}).get("code2") or "")).casefold(),
                                }
                                is_collection = bool(member.get("compilation") or member_book.get("compilation"))
                                # The series relation may contain translations,
                                # adaptations, and auxiliary entries. Hardcover's
                                # featured flag is its curated primary-work signal;
                                # only those records belong in either visible shelf.
                                if not member.get("featured"):
                                    continue
                                # Hardcover can include placeholder series records that are not
                                # shown on its public series page. Keep only real, covered works
                                # in the primary strip so phantom positions do not inflate it.
                                if is_collection and item["cover_url"].startswith(("https://", "http://")):
                                    collections.append(item)
                                elif (not member_book.get("compilation")
                                      and item["cover_url"].startswith(("https://", "http://"))):
                                    primary_books.append(item)
                            try:
                                primary_count = max(0, int(series.get("primary_books_count") or 0))
                            except (TypeError, ValueError, OverflowError):
                                primary_count = 0
                            try:
                                books_count = max(0, int(series.get("books_count") or 0))
                            except (TypeError, ValueError, OverflowError):
                                books_count = 0

                            # Hardcover's series relation can contain alternate-language and
                            # duplicate records sharing a volume position. Keep one best record
                            # per position (prefer the selected edition's language and a cover),
                            # then enforce the catalog's primary-book count.
                            primary_books.sort(key=lambda item: (
                                bool(item.get("cover_url")),
                                bool(preferred_language and item.get("_language") == preferred_language),
                                bool(item.get("author") and item.get("author") != "Unknown author"),
                                -float(item.get("position") or 0),
                            ), reverse=True)
                            unique_primary = []
                            seen_positions = set()
                            seen_canonical = set()
                            seen_titles = set()
                            for item in primary_books:
                                position = float(item.get("position") or 0)
                                position_key = round(position, 4) if position > 0 else None
                                canonical_id = item.get("_canonical_id") or 0
                                title_key = re.sub(r"[^\w]+", " ", item.get("title", "").casefold()).strip()
                                if position_key is not None and position_key in seen_positions:
                                    continue
                                if canonical_id and canonical_id in seen_canonical:
                                    continue
                                if title_key and title_key in seen_titles:
                                    continue
                                if position_key is not None:
                                    seen_positions.add(position_key)
                                if canonical_id:
                                    seen_canonical.add(canonical_id)
                                if title_key:
                                    seen_titles.add(title_key)
                                unique_primary.append({key: value for key, value in item.items() if not key.startswith("_")})
                            unique_primary.sort(key=lambda item: float(item.get("position") or 0))
                            primary_books = unique_primary[:primary_count] if primary_count else unique_primary[:40]

                            unique_collections = []
                            seen_collection_ids = set()
                            seen_collection_titles = set()
                            seen_collection_covers = set()
                            collections.sort(key=lambda item: (
                                bool(item.get("cover_url")),
                                bool(preferred_language and item.get("_language") == preferred_language),
                                bool(item.get("author") and item.get("author") != "Unknown author"),
                            ), reverse=True)
                            for item in collections:
                                canonical_id = item.get("_canonical_id") or 0
                                title_key = re.sub(r"[^\w]+", " ", item.get("title", "").casefold()).strip()
                                cover_key = re.split(r"[?#]", item.get("cover_url", ""), maxsplit=1)[0].rstrip("/").casefold()
                                if ((canonical_id and canonical_id in seen_collection_ids)
                                        or (title_key and title_key in seen_collection_titles)
                                        or (cover_key and cover_key in seen_collection_covers)):
                                    continue
                                if canonical_id:
                                    seen_collection_ids.add(canonical_id)
                                if title_key:
                                    seen_collection_titles.add(title_key)
                                if cover_key:
                                    seen_collection_covers.add(cover_key)
                                unique_collections.append({key: value for key, value in item.items() if not key.startswith("_")})
                            collections = unique_collections
                            series_context = {
                                "id": int(series.get("id") or 0),
                                "name": series_name,
                                "description": str(series.get("description") or "")[:2000],
                                "books_count": books_count,
                                "primary_books_count": len(primary_books) if primary_books else primary_count,
                                "is_completed": bool(series.get("is_completed")),
                                "featured": bool(entry.get("featured")),
                                "books": primary_books,
                                "collections": collections[:12],
                            }
                            break
                    if not series_name:
                        logger.info("[hardcover-details] book_id=%s returned no series association", book_id)
                else:
                    logger.warning("[hardcover-details] book_id=%s series HTTP status=%s", book_id, series_response.status_code)
            except Exception as exc:
                logger.exception("[hardcover-details] book_id=%s series request raised %s", book_id, type(exc).__name__)
            try:
                readers = max(0, int(row.get("users_read_count") or 0))
            except (TypeError, ValueError, OverflowError):
                readers = 0
            metadata = {
                "hardcover_genres": MultiSourceBookAggregator._extract_hardcover_tags(row.get("cached_tags"), "Genre"),
                "hardcover_moods": MultiSourceBookAggregator._extract_hardcover_tags(row.get("cached_tags"), "Mood"),
                "hardcover_content_warnings": MultiSourceBookAggregator._extract_hardcover_tags(row.get("cached_tags"), "ContentWarning"),
                "hardcover_reader_count": readers,
                "hardcover_series_name": series_name,
                "hardcover_series_position": int(series_position) if series_position.is_integer() else series_position,
                "hardcover_series_context": series_context,
            }
            logger.info(
                "[hardcover-details] book_id=%s result series=%r position=%s series_books=%s primary_count=%s collections=%s readers=%s genres=%s moods=%s warnings=%s",
                book_id, series_name or None, metadata["hardcover_series_position"],
                len(series_context.get("books") or []), series_context.get("primary_books_count", 0),
                len(series_context.get("collections") or []), readers,
                len(metadata["hardcover_genres"]), len(metadata["hardcover_moods"]),
                len(metadata["hardcover_content_warnings"]),
            )
            return metadata
        except Exception as exc:
            logger.exception("[hardcover-details] book_id=%s metadata request raised %s", book_id, type(exc).__name__)
            return {}

    @staticmethod
    def search_hardcover_by_tags(genre: str = "", mood: str = "", limit: int = 30) -> list[dict]:
        """Search Hardcover books by cached community genre/mood tags."""
        api_key = os.getenv("HARDCOVER_API_KEY", "").strip()
        genre, mood = str(genre or "").strip(), str(mood or "").strip()
        if not api_key or not (genre or mood):
            return []
        try:
            limit = max(1, min(int(limit), 50))
        except (TypeError, ValueError, OverflowError):
            return []

        # Hardcover has exposed cached_tags in both category-map and tagged
        # object forms. OR the shapes while ANDing the requested dimensions.
        filters = []
        for category, value in (("Genre", genre), ("Mood", mood)):
            if value:
                filters.append((category, value))
        variables = {"limit": limit}
        variable_defs = ["$limit: Int!"]
        structural_conditions = []
        for index, (category, value) in enumerate(filters):
            map_var = f"$tag_map_{index}"
            object_map_var = f"$tag_object_map_{index}"
            list_var = f"$tag_list_{index}"
            variable_defs.extend((f"{map_var}: jsonb!", f"{object_map_var}: jsonb!", f"{list_var}: jsonb!"))
            variables[f"tag_map_{index}"] = {category: [value]}
            variables[f"tag_object_map_{index}"] = {category: [{"tag": value}]}
            variables[f"tag_list_{index}"] = {"tags": [{"category": category, "tag": value}]}
            structural_conditions.append(
                "_or: ["
                f"{{cached_tags: {{_contains: {map_var}}}}}, "
                f"{{cached_tags: {{_contains: {object_map_var}}}}}, "
                f"{{cached_tags: {{_contains: {list_var}}}}}"
                "]"
            )
        where = "_and: [" + ", ".join("{" + item + "}" for item in structural_conditions) + "]"
        query = f"""
        query HardcoverBooksByTags({", ".join(variable_defs)}) {{
          books(where: {{{where}}}, order_by: {{users_read_count: desc}}, limit: $limit) {{
            id title rating ratings_count users_read_count description cached_tags
            pages release_date image {{ url }}
            contributions(limit: 3) {{ author {{ name }} }}
            default_cover_edition {{ pages release_date isbn_10 isbn_13 image {{ url }} }}
          }}
        }}
        """
        try:
            response = get_http_session().post(
                "https://api.hardcover.app/v1/graphql",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"query": query, "variables": variables}, timeout=12,
            )
            if response.status_code != 200:
                logger.debug("Hardcover tag recommendation failed status=%s", response.status_code)
                return []
            payload = response.json()
            if payload.get("errors"):
                logger.debug("Hardcover tag recommendation GraphQL error: %s", payload["errors"])
                return []
            rows = (payload.get("data") or {}).get("books") or []
            results = []

            def extract_tags(raw, category):
                if isinstance(raw, str):
                    try:
                        raw = json.loads(raw)
                    except (TypeError, ValueError):
                        return []
                if not isinstance(raw, dict):
                    return []
                values = raw.get(category)
                if values is None:
                    values = raw.get("tags")
                    if isinstance(values, dict):
                        values = values.get(category)
                    elif isinstance(values, list):
                        values = [item for item in values if isinstance(item, dict)
                                  and str(item.get("category") or "").casefold() == category.casefold()]
                if not isinstance(values, list):
                    return []
                extracted = []
                for item in values:
                    value = item.get("tag") or item.get("name") or "" if isinstance(item, dict) else item
                    value = str(value or "").strip()
                    if value and value not in extracted:
                        extracted.append(value)
                return extracted

            for row in rows:
                if not isinstance(row, dict) or not str(row.get("title") or "").strip():
                    continue
                edition = row.get("default_cover_edition") or {}
                contributions = row.get("contributions") or []
                authors = [str(item["author"]["name"]) for item in contributions
                           if isinstance(item, dict) and isinstance(item.get("author"), dict)
                           and item["author"].get("name")]
                image = edition.get("image") or row.get("image") or {}
                results.append({
                    "hardcover_id": int(row.get("id") or 0),
                    "title": str(row.get("title") or "").strip(),
                    "author": ", ".join(authors) or "Unknown author",
                    "rating": row.get("rating") or 0,
                    "rating_count": row.get("ratings_count") or 0,
                    "users_read_count": row.get("users_read_count") or 0,
                    "description": str(row.get("description") or ""),
                    "categories": extract_tags(row.get("cached_tags"), "Genre"),
                    "moods": extract_tags(row.get("cached_tags"), "Mood"),
                    "cover_url": str(image.get("url") or ""),
                    "isbn": str(edition.get("isbn_13") or edition.get("isbn_10") or ""),
                    "page_count": edition.get("pages") or row.get("pages") or 0,
                    "published_date": str(edition.get("release_date") or row.get("release_date") or ""),
                    "source": "hardcover",
                })
            return results
        except Exception as exc:
            logger.debug("Hardcover tag recommendation failed: %s", type(exc).__name__)
            return []

    @staticmethod
    def search_hardcover_trending(limit: int = 100, duration: str = "month") -> list[dict]:
        """Fetch Hardcover's activity-ranked feed for a rolling time window."""
        api_key = os.getenv("HARDCOVER_API_KEY", "").strip()
        if not api_key or duration not in {"week", "month", "three_month", "one_year", "all"}:
            return []
        limit = max(1, min(int(limit), 200))
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        session = get_http_session()
        try:
            trending_response = session.post(
                "https://api.hardcover.app/v1/graphql",
                headers=headers,
                json={
                    "query": """
                    query TrendingBooks($duration: TrendingDuration!, $limit: Int!) {
                        books_trending(duration: $duration, limit: $limit, offset: 0) {
                            error
                            ids
                        }
                    }
                    """,
                    "variables": {"duration": duration, "limit": limit},
                },
                timeout=10,
            )
            if trending_response.status_code != 200:
                logger.debug("Hardcover trending request failed: %s", trending_response.status_code)
                return []
            trending_payload = trending_response.json()
            if trending_payload.get("errors"):
                logger.debug("Hardcover trending GraphQL error: %s", trending_payload["errors"])
                return []
            trending_data = (trending_payload.get("data") or {}).get("books_trending") or {}
            if trending_data.get("error"):
                logger.debug("Hardcover trending returned error: %s", trending_data["error"])
                return []
            ids = [
                book_id for book_id in (trending_data.get("ids") or [])
                if isinstance(book_id, int) and not isinstance(book_id, bool)
            ]
            if not ids:
                return []

            books_response = session.post(
                "https://api.hardcover.app/v1/graphql",
                headers=headers,
                json={
                    "query": """
                    query TrendingBookDetails($ids: [Int!]) {
                        books(where: {id: {_in: $ids}}) {
                            id title description rating ratings_count users_read_count
                            release_date release_year cached_tags image { url }
                            contributions(limit: 3) { author { name } }
                            default_cover_edition {
                                pages isbn_10 isbn_13 cached_tags
                                release_date language { name }
                                image { url }
                            }
                        }
                    }
                    """,
                    "variables": {"ids": ids},
                },
                timeout=10,
            )
            if books_response.status_code != 200:
                logger.debug("Hardcover trending hydration failed: %s", books_response.status_code)
                return []
            books_payload = books_response.json()
            if books_payload.get("errors"):
                logger.debug("Hardcover trending hydration GraphQL error: %s", books_payload["errors"])
                return []
            rows = (books_payload.get("data") or {}).get("books") or []
            by_id = {row.get("id"): row for row in rows if isinstance(row, dict)}

            def genres_from(*values):
                genres = []
                for raw in values:
                    if isinstance(raw, str):
                        try:
                            raw = json.loads(raw)
                        except ValueError:
                            continue
                    if isinstance(raw, dict):
                        raw = raw.get("tags", raw.get("Genre", raw.get("genres", [])))
                        if isinstance(raw, dict):
                            raw = raw.get("Genre", raw.get("genre", []))
                    if not isinstance(raw, list):
                        continue
                    for tag in raw:
                        if isinstance(tag, dict):
                            category = str(tag.get("category") or "").casefold()
                            if category and category != "genre":
                                continue
                            tag = tag.get("tag") or tag.get("name") or ""
                        if isinstance(tag, str) and tag.strip() and tag.strip() not in genres:
                            genres.append(tag.strip())
                return genres

            books = []
            for book_id in ids:
                row = by_id.get(book_id)
                if not row or not str(row.get("title") or "").strip():
                    continue
                edition = row.get("default_cover_edition") or {}
                contributions = row.get("contributions") or []
                authors = [
                    item["author"]["name"] for item in contributions
                    if isinstance(item, dict) and isinstance(item.get("author"), dict)
                    and item["author"].get("name")
                ]
                image = edition.get("image") or row.get("image") or {}
                categories = genres_from(row.get("cached_tags"), edition.get("cached_tags"))
                rating = float(row.get("rating") or 0)
                published = edition.get("release_date") or row.get("release_date") or row.get("release_year") or ""
                books.append({
                    "title": str(row.get("title") or "").strip(),
                    "author": ", ".join(authors) or "Unknown Author",
                    "rating": rating,
                    "rating_count": int(row.get("ratings_count") or 0),
                    "users_read_count": int(row.get("users_read_count") or 0),
                    "rating_formatted": f"{rating:.2f}" if rating > 0 else "N/A",
                    "description": str(row.get("description") or ""),
                    "page_count": int(edition.get("pages") or 0),
                    "published_date": str(published),
                    "language": (edition.get("language") or {}).get("name", ""),
                    "categories": categories,
                    "genres": categories,
                    "cover_url": str(image.get("url") or ""),
                    "isbn": str(edition.get("isbn_13") or edition.get("isbn_10") or ""),
                    "source": "hardcover",
                })
            return books
        except Exception as e:
            logger.debug("Hardcover trending lookup failed: %s", e)
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

        async def timed_provider(name, search_fn, *args):
            started = time.perf_counter()
            results = []
            try:
                results = await asyncio.to_thread(search_fn, *args)
                return results
            finally:
                count = len(results) if isinstance(results, list) else 0
                logger.info(
                    "[perf] provider=%s elapsed_ms=%d results=%d",
                    name,
                    round((time.perf_counter() - started) * 1000),
                    count,
                )

        # Run independent searches concurrently (both are pure I/O)
        google_task = timed_provider(
            "google_books", MultiSourceBookAggregator.search_google_books,
            query, limit, False,
        )
        itunes_task = timed_provider(
            "itunes", MultiSourceBookAggregator.search_itunes, query,
        )
        aggregate_started = time.perf_counter()
        google_books, itunes_books = await asyncio.gather(
            google_task, itunes_task
        )
        logger.info(
            "[perf] providers_complete elapsed_ms=%d google_books=%d itunes=%d",
            round((time.perf_counter() - aggregate_started) * 1000),
            len(google_books or []),
            len(itunes_books or []),
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
            logger.warning("No results from Google Books or iTunes – trying Hardcover fallback")
            hardcover_started = time.perf_counter()
            hardcover_books = await asyncio.to_thread(
                MultiSourceBookAggregator.search_hardcover, query, limit
            )
            logger.info(
                "[perf] provider=hardcover_fallback elapsed_ms=%d results=%d",
                round((time.perf_counter() - hardcover_started) * 1000),
                len(hardcover_books or []),
            )
            if hardcover_books:
                for book in hardcover_books[:limit]:
                    try:
                        rating = float(book.get("rating") or 0)
                    except (TypeError, ValueError):
                        rating = 0.0
                    try:
                        rating_count = int(book.get("rating_count") or 0)
                    except (TypeError, ValueError):
                        rating_count = 0
                    book["search_rating"] = rating
                    book["search_rating_count"] = rating_count
                    book["search_rating_formatted"] = f"{rating:.2f}" if rating > 0 else "N/A"
                    book["cover_source"] = "hardcover"
                return hardcover_books[:limit]

            logger.warning("Hardcover fallback had no results – trying Goodreads fallback")
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
            resp = get_http_session().post(
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

            # A search fallback may be useful for ratings, but it must not
            # provide a cover unless Hardcover's record exactly matches both
            # the requested title and author. Normalize punctuation so common
            # catalog variants such as "Angels & Demons" / "Angels and Demons"
            # compare equally.
            def _cover_match_key(value):
                value = str(value or "").casefold().replace("&", " and ")
                return " ".join(re.findall(r"[^\W_]+", value, flags=re.UNICODE))

            requested_title_key = _cover_match_key(title)
            requested_author_key = _cover_match_key(author)
            exact_cover_hits = []
            if requested_title_key and requested_author_key:
                for hit in hits:
                    candidate = hit.get("document", {})
                    candidate_title_key = _cover_match_key(candidate.get("title", ""))
                    candidate_authors = candidate.get("author_names", [])
                    if isinstance(candidate_authors, list):
                        candidate_author_key = _cover_match_key(" ".join(candidate_authors))
                    else:
                        candidate_author_key = _cover_match_key(candidate_authors)
                    if (candidate_title_key == requested_title_key
                            and candidate_author_key == requested_author_key):
                        exact_cover_hits.append(hit)

            exact_cover_hits = [
                hit for hit in exact_cover_hits
                if (hit.get("document", {}).get("image", {}) or {}).get("url")
            ]

            cover_doc = max(
                exact_cover_hits,
                key=lambda h: h.get("document", {}).get("ratings_count") or 0,
                default={},
            ).get("document", {})

            rating = doc.get("rating") or 0.0
            ratings_count = doc.get("ratings_count") or 0
            genres = doc.get("genres", []) or []
            image_data = cover_doc.get("image", {}) or {}
            image_url = image_data.get("url", "")

            if rating and ratings_count:
                logger.info(f"⭐ Hardcover.app: {rating}/5 from {ratings_count} ratings for '{title or isbn}'")
                return round(float(rating), 2), int(ratings_count), genres, image_url
            else:
                logger.debug(f"Hardcover hit has no rating: rating={rating}, count={ratings_count}")
                # The exact-match cover remains useful even without ratings.
                return 0.0, 0, [], image_url

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
                if hc_cover:
                    book["cover_url"] = hc_cover
                    book["cover_source"] = "hardcover"
                logger.info(f"Reusing cached Hardcover data for: {book.get('title', '')}")
                return book, (hc_rating, hc_count, hc_genres, hc_cover)

        # 2) If already has a rating, still fetch hc_data for _ensure_cover reuse
        if book.get("rating") and book["rating"] > 0:
            hc_data = MultiSourceBookAggregator._get_hardcover_cached(
                book.get("isbn", ""), book.get("title", ""), book.get("author", "")
            )
            if hc_data[3]:
                book["cover_url"] = hc_data[3]
                book["cover_source"] = "hardcover"
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
            if hc_cover:
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

        # A verified Hardcover cover can improve on a potentially misassigned
        # Google Books image even when neither source has a rating.
        if hc_cover:
            book["cover_url"] = hc_cover
            book["cover_source"] = "hardcover"

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
            r = get_http_session().get(check_url, headers=HEADERS, timeout=10)
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
