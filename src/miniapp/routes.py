"""HTTP endpoints for the Telegram Mini App backend."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, request, send_from_directory

from .auth import InitDataError, issue_inline_token, validate_init_data, validate_inline_token
from .bookshelf import (
    BOOKSHELF_COLLECTIONS,
    BOOKSHELF_LIMIT,
    MongoBookshelfRepository,
    normalize_book_key,
    sanitize_book,
)
from .service import MiniAppSearchService


INIT_DATA_HEADER = "X-Telegram-Init-Data"
INLINE_SESSION_HEADER = "X-Annie-Inline-Session"
_RATE_LIMIT_WINDOW_SECONDS = 60
_RATE_LIMIT_MAX_REQUESTS = 12


def create_miniapp_blueprint(runtime: dict) -> Blueprint:
    """Build an isolated Blueprint; ``runtime`` is populated by Koyeb startup."""
    blueprint = Blueprint("miniapp_api", __name__)
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    rate_lock = threading.Lock()
    bookshelf_repo_lock = threading.Lock()
    request_times: dict[tuple[int, str], deque[float]] = defaultdict(deque)

    def authenticate():
        inline_token = request.headers.get(INLINE_SESSION_HEADER, "")
        if inline_token:
            try:
                user = validate_inline_token(
                    inline_token, os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
                    purpose="session", max_lifetime=3600,
                )
                return user, None
            except InitDataError:
                return None, (jsonify({"success": False, "error": "unauthorized"}), 401)
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        try:
            return validate_init_data(
                request.headers.get(INIT_DATA_HEADER, ""), token
            ), None
        except InitDataError:
            return None, (jsonify({"success": False, "error": "unauthorized"}), 401)

    def within_rate_limit(user_id: int, action: str) -> bool:
        now = time.monotonic()
        with rate_lock:
            bucket = (user_id, action)
            requests_for_user = request_times[bucket]
            while requests_for_user and now - requests_for_user[0] >= _RATE_LIMIT_WINDOW_SECONDS:
                requests_for_user.popleft()
            action_limit = 4 if action == "recommendations" else 30 if action == "bookshelf_write" else _RATE_LIMIT_MAX_REQUESTS
            if len(requests_for_user) >= action_limit:
                return False
            requests_for_user.append(now)
            if len(request_times) > 2000:
                for old_user, old_times in list(request_times.items()):
                    if not old_times or now - old_times[-1] >= _RATE_LIMIT_WINDOW_SECONDS:
                        request_times.pop(old_user, None)
            return True

    def get_service():
        service = runtime.get("miniapp_search_service")
        bot = runtime.get("bot")
        if service is None and bot is not None:
            service = MiniAppSearchService(bot.aggregator, bot)
            runtime["miniapp_search_service"] = service
        return service

    def get_bookshelf_repository():
        repository = runtime.get("bookshelf_repository")
        if repository is not None:
            return repository
        uri = os.getenv("MONGODB_URI", "").strip()
        if not uri:
            return None
        with bookshelf_repo_lock:
            repository = runtime.get("bookshelf_repository")
            if repository is None:
                repository = MongoBookshelfRepository(
                    uri,
                    os.getenv("MONGODB_DB_NAME", "annie_db").strip() or "annie_db",
                )
                runtime["bookshelf_repository"] = repository
        return repository

    @blueprint.get("/")
    def index():
        """Serve the standalone Mini App interface from the same origin as its API."""
        response = send_from_directory(static_dir, "index.html")
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return response

    @blueprint.get("/assets/<path:filename>")
    def assets(filename):
        # The HTML is always revalidated, while static files are versioned in
        # their URLs. Cache them briefly so Mini App relaunches reuse images,
        # styles, and scripts instead of downloading them again.
        return send_from_directory(static_dir, filename, max_age=86400)

    @blueprint.get("/api/session")
    def session():
        user, error = authenticate()
        if error:
            return error
        return jsonify({"success": True, "user": {
            "id": user["id"],
            "first_name": user["first_name"],
            "language_code": user["language_code"],
        }, "bookshelf_enabled": bool(os.getenv("MONGODB_URI", "").strip()),
            "bookshelf_limit": BOOKSHELF_LIMIT})

    @blueprint.post("/api/inline-session")
    def inline_session():
        """Exchange a signed inline launch ticket for a temporary API session."""
        payload = request.get_json(silent=True)
        ticket = payload.get("ticket") if isinstance(payload, dict) else None
        if not isinstance(ticket, str):
            return jsonify({"success": False, "error": "unauthorized"}), 401
        try:
            user = validate_inline_token(
                ticket, os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
                purpose="ticket", max_lifetime=300,
            )
        except InitDataError:
            return jsonify({"success": False, "error": "unauthorized"}), 401
        session_token = issue_inline_token(
            user, os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            purpose="session", ttl_seconds=3600,
        )
        return jsonify({
            "success": True,
            "session_token": session_token,
            "user": {
                "id": user["id"],
                "first_name": user["first_name"],
                "language_code": user["language_code"],
            },
            "bookshelf_enabled": bool(os.getenv("MONGODB_URI", "").strip()),
            "bookshelf_limit": BOOKSHELF_LIMIT,
        })

    @blueprint.get("/api/bookshelf")
    def get_bookshelf():
        user, error = authenticate()
        if error:
            return error
        if not within_rate_limit(user["id"], "bookshelf_read"):
            return jsonify({"success": False, "error": "rate_limited"}), 429
        try:
            repository = get_bookshelf_repository()
            if repository is None:
                return jsonify({"success": False, "error": "bookshelf_not_configured"}), 503
            books = repository.get_user_books(user["id"])
        except Exception as exc:
            # Driver errors may contain connection details; never log the URI.
            current_app.logger.error("Mini App bookshelf read failed (%s)", type(exc).__name__)
            return jsonify({"success": False, "error": "bookshelf_unavailable"}), 503
        return jsonify({"success": True, "data": {"bookshelf": books, "limit": BOOKSHELF_LIMIT}})

    @blueprint.post("/api/bookshelf")
    def update_bookshelf():
        user, error = authenticate()
        if error:
            return error
        if not within_rate_limit(user["id"], "bookshelf_write"):
            return jsonify({"success": False, "error": "rate_limited"}), 429
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"success": False, "error": "invalid_json"}), 400
        action = payload.get("action")
        collection = payload.get("collection")
        if collection not in BOOKSHELF_COLLECTIONS:
            return jsonify({"success": False, "error": "invalid_collection"}), 400
        try:
            repository = get_bookshelf_repository()
            if repository is None:
                return jsonify({"success": False, "error": "bookshelf_not_configured"}), 503
            if action == "upsert":
                book = sanitize_book(payload.get("book"))
                book_key = normalize_book_key(book)
                accepted = repository.upsert_entry(user["id"], collection, book)
                if not accepted:
                    return jsonify({"success": False, "error": "bookshelf_full", "limit": BOOKSHELF_LIMIT}), 409
                return jsonify({"success": True, "data": {"book_key": book_key, "limit": BOOKSHELF_LIMIT}})
            if action == "import_if_empty":
                raw_entries = payload.get("entries")
                if not isinstance(raw_entries, list):
                    return jsonify({"success": False, "error": "invalid_bookshelf"}), 400
                if len(raw_entries) > BOOKSHELF_LIMIT:
                    return jsonify({"success": False, "error": "bookshelf_full", "limit": BOOKSHELF_LIMIT}), 409
                entries_by_key = {}
                for raw_entry in raw_entries:
                    if not isinstance(raw_entry, dict) or raw_entry.get("collection") not in BOOKSHELF_COLLECTIONS:
                        return jsonify({"success": False, "error": "invalid_bookshelf"}), 400
                    book = sanitize_book(raw_entry.get("book"))
                    key = normalize_book_key(book)
                    collection_name = raw_entry["collection"]
                    # Keep the favourite copy if old local data contains a duplicate.
                    if key in entries_by_key and entries_by_key[key]["collection"] == "favorites":
                        continue
                    try:
                        added_ms = int(raw_entry.get("addedAt") or 0)
                    except (TypeError, ValueError, OverflowError):
                        added_ms = 0
                    added_at = time.time() if added_ms <= 0 else min(added_ms / 1000, time.time())
                    entries_by_key[key] = {
                        "book_key": key,
                        "collection": collection_name,
                        "added_at": datetime.fromtimestamp(added_at, timezone.utc),
                        "book": book,
                    }
                if len(entries_by_key) > BOOKSHELF_LIMIT:
                    return jsonify({"success": False, "error": "bookshelf_full", "limit": BOOKSHELF_LIMIT}), 409
                imported = repository.import_entries_if_empty(user["id"], list(entries_by_key.values()))
                return jsonify({"success": True, "data": {"imported": imported, "limit": BOOKSHELF_LIMIT}})
            if action in {"remove", "remove_many"}:
                raw_keys = [payload.get("book_key")] if action == "remove" else payload.get("book_keys")
                if not isinstance(raw_keys, list) or not raw_keys or len(raw_keys) > BOOKSHELF_LIMIT:
                    return jsonify({"success": False, "error": "invalid_book_keys"}), 400
                if any(not isinstance(key, str) or not key or len(key) > 600 for key in raw_keys):
                    return jsonify({"success": False, "error": "invalid_book_keys"}), 400
                removed = repository.remove_entries(user["id"], collection, list(set(raw_keys)))
                return jsonify({"success": True, "data": {"removed": removed}})
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc) or "invalid_book"}), 400
        except Exception as exc:
            current_app.logger.error("Mini App bookshelf write failed (%s)", type(exc).__name__)
            return jsonify({"success": False, "error": "bookshelf_unavailable"}), 503
        return jsonify({"success": False, "error": "invalid_action"}), 400

    @blueprint.post("/api/search")
    def search():
        user, error = authenticate()
        if error:
            return error
        if not within_rate_limit(user["id"], "search"):
            return jsonify({"success": False, "error": "rate_limited"}), 429

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"success": False, "error": "invalid_json"}), 400
        query = payload.get("query")
        page = payload.get("page", 1)
        if not isinstance(query, str) or not 2 <= len(query.strip()) <= 160:
            return jsonify({"success": False, "error": "invalid_query"}), 400
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 20:
            return jsonify({"success": False, "error": "invalid_page"}), 400

        service = get_service()
        if service is None:
            return jsonify({"success": False, "error": "service_unavailable"}), 503
        try:
            result = asyncio.run(service.search(query.strip(), page))
        except Exception:
            current_app.logger.exception("Mini App search failed")
            return jsonify({"success": False, "error": "search_failed"}), 502
        return jsonify({"success": True, "data": result})

    @blueprint.post("/api/trending")
    def trending():
        """Return cached, provider-ranked top books for the home discovery view."""
        user, error = authenticate()
        if error:
            return error
        if not within_rate_limit(user["id"], "trending"):
            return jsonify({"success": False, "error": "rate_limited"}), 429

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"success": False, "error": "invalid_json"}), 400
        genre = payload.get("genre")
        service = get_service()
        if service is None:
            return jsonify({"success": False, "error": "service_unavailable"}), 503
        try:
            result = asyncio.run(service.trending(genre))
        except ValueError:
            return jsonify({"success": False, "error": "invalid_genre"}), 400
        except Exception:
            current_app.logger.exception("Mini App top books lookup failed")
            return jsonify({"success": False, "error": "trending_failed"}), 502
        return jsonify({"success": True, "data": result})

    @blueprint.post("/api/details")
    def details():
        """Fetch richer Google Books metadata only for a selected result."""
        user, error = authenticate()
        if error:
            return error
        if not within_rate_limit(user["id"], "details"):
            return jsonify({"success": False, "error": "rate_limited"}), 429

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"success": False, "error": "invalid_json"}), 400
        query = payload.get("query")
        page = payload.get("page", 1)
        index = payload.get("index")
        if not isinstance(query, str) or not 2 <= len(query.strip()) <= 160:
            return jsonify({"success": False, "error": "invalid_query"}), 400
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 20:
            return jsonify({"success": False, "error": "invalid_page"}), 400
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < 5:
            return jsonify({"success": False, "error": "invalid_selection"}), 400

        service = get_service()
        if service is None:
            return jsonify({"success": False, "error": "service_unavailable"}), 503
        try:
            result = asyncio.run(service.book_details(query.strip(), page, index))
        except Exception:
            current_app.logger.exception("Mini App book details lookup failed")
            return jsonify({"success": False, "error": "details_failed"}), 502
        if result is None:
            return jsonify({"success": False, "error": "search_expired"}), 404
        return jsonify({"success": True, "data": result})

    @blueprint.post("/api/recommendation-details")
    def recommendation_details():
        """Enrich a recommendation selected from the independent recommendations module."""
        user, error = authenticate()
        if error:
            return error
        if not within_rate_limit(user["id"], "details"):
            return jsonify({"success": False, "error": "rate_limited"}), 429

        payload = request.get_json(silent=True)
        raw_book = payload.get("book") if isinstance(payload, dict) else None
        if not isinstance(raw_book, dict):
            return jsonify({"success": False, "error": "invalid_book"}), 400
        title = raw_book.get("title")
        author = raw_book.get("author", "")
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 250:
            return jsonify({"success": False, "error": "invalid_book"}), 400
        if not isinstance(author, str) or len(author) > 250:
            raw_book["author"] = ""

        service = get_service()
        if service is None:
            return jsonify({"success": False, "error": "service_unavailable"}), 503
        try:
            result = asyncio.run(service.recommendation_book_details(raw_book))
        except Exception:
            current_app.logger.exception("Mini App recommendation detail lookup failed")
            return jsonify({"success": False, "error": "details_failed"}), 502
        return jsonify({"success": True, "data": result})

    @blueprint.post("/api/translate")
    def translate():
        """Translate a selected description only after an explicit user request."""
        user, error = authenticate()
        if error:
            return error
        if not within_rate_limit(user["id"], "translate"):
            return jsonify({"success": False, "error": "rate_limited"}), 429

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"success": False, "error": "invalid_json"}), 400
        if isinstance(payload.get("book"), dict):
            raw_book = payload["book"]
            title = raw_book.get("title")
            description = raw_book.get("description", "")
            if not isinstance(title, str) or not title.strip() or len(title) > 250:
                return jsonify({"success": False, "error": "invalid_book"}), 400
            if not isinstance(description, str) or len(description) > 20000:
                return jsonify({"success": False, "error": "invalid_book"}), 400
            service = get_service()
            if service is None:
                return jsonify({"success": False, "error": "service_unavailable"}), 503
            try:
                result = asyncio.run(service.translate_recommendation_description(raw_book))
            except Exception:
                current_app.logger.exception("Mini App recommendation description translation failed")
                return jsonify({"success": False, "error": "translation_failed"}), 502
            return jsonify({"success": True, "data": result})
        query = payload.get("query")
        page = payload.get("page", 1)
        index = payload.get("index")
        if not isinstance(query, str) or not 2 <= len(query.strip()) <= 160:
            return jsonify({"success": False, "error": "invalid_query"}), 400
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 20:
            return jsonify({"success": False, "error": "invalid_page"}), 400
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < 5:
            return jsonify({"success": False, "error": "invalid_selection"}), 400

        service = get_service()
        if service is None:
            return jsonify({"success": False, "error": "service_unavailable"}), 503
        try:
            result = asyncio.run(service.translate_description(query.strip(), page, index))
        except Exception:
            current_app.logger.exception("Mini App description translation failed")
            return jsonify({"success": False, "error": "translation_failed"}), 502
        if result is None:
            return jsonify({"success": False, "error": "search_expired"}), 404
        return jsonify({"success": True, "data": result})

    @blueprint.post("/api/related")
    def related():
        """Load book-specific suggestions after the main detail view is visible."""
        user, error = authenticate()
        if error:
            return error
        if not within_rate_limit(user["id"], "related"):
            return jsonify({"success": False, "error": "rate_limited"}), 429

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"success": False, "error": "invalid_json"}), 400
        raw_book = payload.get("book")
        if not isinstance(raw_book, dict):
            return jsonify({"success": False, "error": "invalid_book"}), 400
        title = raw_book.get("title")
        author = raw_book.get("author", "")
        categories = raw_book.get("categories", [])
        isbn = raw_book.get("isbn", "")
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 250:
            return jsonify({"success": False, "error": "invalid_book"}), 400
        if not isinstance(author, str) or len(author) > 250:
            author = ""
        if isinstance(categories, str):
            categories = [categories]
        if not isinstance(categories, list):
            categories = []
        cleaned_categories = []
        for item in categories:
            if isinstance(item, dict):
                item = item.get("name") or item.get("title") or ""
            if isinstance(item, str) and item.strip():
                cleaned_categories.append(item.strip()[:120])
            if len(cleaned_categories) >= 10:
                break
        categories = cleaned_categories
        if not isinstance(isbn, str):
            isbn = ""
        selected = {"title": title.strip(), "author": author.strip(), "categories": categories, "isbn": isbn[:30]}

        service = get_service()
        if service is None:
            return jsonify({"success": False, "error": "service_unavailable"}), 503
        try:
            result = asyncio.run(service.related_books_for_book(selected))
        except Exception:
            current_app.logger.exception("Mini App related-book lookup failed")
            return jsonify({"success": False, "error": "related_failed"}), 502
        return jsonify({"success": True, "data": {"books": result}})

    @blueprint.post("/api/recommendations")
    def recommendations():
        """Build a personalized shelf from the Mini App preference form."""
        user, error = authenticate()
        if error:
            return error
        if not within_rate_limit(user["id"], "recommendations"):
            return jsonify({"success": False, "error": "rate_limited"}), 429

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"success": False, "error": "invalid_json"}), 400
        service = get_service()
        if service is None:
            return jsonify({"success": False, "error": "service_unavailable"}), 503
        try:
            result = asyncio.run(service.recommend_books(payload))
        except ValueError as exc:
            code = str(exc) or "invalid_preferences"
            status = 400
            return jsonify({"success": False, "error": code}), status
        except Exception:
            current_app.logger.exception("Mini App recommendations failed")
            return jsonify({"success": False, "error": "recommendations_failed"}), 502
        return jsonify({"success": True, "data": result})

    return blueprint
