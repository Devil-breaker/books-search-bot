"""Compact MongoDB-backed storage for Telegram users' saved books."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError


BOOKSHELF_LIMIT = 100
BOOKSHELF_COLLECTIONS = frozenset({"saved", "favorites"})


def normalize_book_key(book: dict[str, Any]) -> str:
    isbn = re.sub(r"[^0-9X]", "", str(book.get("isbn") or book.get("isbn13") or book.get("isbn_10") or "").upper())
    if isbn:
        return f"isbn:{isbn.lower()}"

    def normalize(value: object) -> str:
        value = unicodedata.normalize("NFKC", str(value or "")).lower()
        return " ".join("".join(char if char.isalnum() else " " for char in value).split())

    return f"book:{normalize(book.get('title'))}|{normalize(book.get('author'))}"


def sanitize_book(book: object) -> dict[str, Any]:
    if not isinstance(book, dict):
        raise ValueError("invalid_book")
    title = book.get("title")
    author = book.get("author", "")
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 250:
        raise ValueError("invalid_book")
    if not isinstance(author, str) or len(author) > 250:
        raise ValueError("invalid_book")

    cover = book.get("cover_url", "")
    if not isinstance(cover, str) or len(cover) > 2048 or (cover and not cover.startswith(("https://", "http://"))):
        cover = ""
    raw_isbn = book.get("isbn", book.get("isbn13", book.get("isbn_10", "")))
    isbn = str(raw_isbn or "")[:30]
    description = book.get("description", "")
    if not isinstance(description, str):
        description = ""
    translated_description = book.get("translated_description", "")
    if not isinstance(translated_description, str):
        translated_description = ""
    translated_title = book.get("translated_title", "")
    if not isinstance(translated_title, str):
        translated_title = ""
    raw_categories = book.get("categories", [])
    if isinstance(raw_categories, str):
        raw_categories = [raw_categories]
    categories = [value.strip()[:120] for value in raw_categories[:12] if isinstance(value, str) and value.strip()] if isinstance(raw_categories, list) else []

    def safe_int(value: object, maximum: int = 2_000_000_000) -> int:
        try:
            return max(0, min(int(value or 0), maximum))
        except (TypeError, ValueError, OverflowError):
            return 0

    try:
        rating = max(0.0, min(float(book.get("rating") or 0), 5.0))
    except (TypeError, ValueError, OverflowError):
        rating = 0.0

    return {
        "title": title.strip(),
        "author": author.strip() or "Unknown author",
        "cover_url": cover,
        "categories": categories,
        "rating": rating,
        "rating_count": safe_int(book.get("rating_count")),
        "published_date": str(book.get("published_date") or "")[:40],
        "page_count": safe_int(book.get("page_count"), 100_000),
        "language": str(book.get("language") or "")[:40],
        "isbn": isbn,
        "source": str(book.get("source") or "")[:80],
        "metadata_source": str(book.get("metadata_source") or "")[:80],
        "info_link": str(book.get("info_link") or "")[:2048],
        # A bounded metadata snapshot prevents reopening saved books from
        # triggering the same catalog lookup and description translation.
        "description": description[:5000],
        "translated_title": translated_title[:250],
        "translated_description": translated_description[:5000],
        "title_needs_translation": bool(book.get("title_needs_translation")),
        "description_needs_translation": bool(book.get("description_needs_translation")),
        "details_checked": bool(book.get("details_checked")),
    }


class MongoBookshelfRepository:
    """Store each user's entries in one atomic, bounded document."""

    def __init__(self, uri: str, database_name: str = "annie_db") -> None:
        self.client = MongoClient(
            uri,
            appname="AnnieSearchBookshelf",
            serverSelectionTimeoutMS=3000,
            connectTimeoutMS=3000,
            socketTimeoutMS=5000,
            waitQueueTimeoutMS=3000,
            maxPoolSize=10,
            tz_aware=True,
        )
        self.users = self.client[database_name]["bookshelf_users"]

    @staticmethod
    def _entry_pipeline(entry: dict[str, Any]) -> list[dict[str, Any]]:
        key = entry["book_key"]
        current = {"$ifNull": ["$entries", []]}
        matching_key = {"$in": [key, {"$map": {"input": "$$current", "as": "item", "in": "$$item.book_key"}}]}
        can_add = {"$lt": [{"$size": "$$current"}, BOOKSHELF_LIMIT]}
        replace_entry = {
            "$map": {
                "input": "$$current",
                "as": "item",
                "in": {
                    "$cond": [
                        {"$eq": ["$$item.book_key", key]},
                        {"$mergeObjects": ["$$item", {"$literal": entry}]},
                        "$$item",
                    ]
                },
            }
        }
        return [{"$set": {
            "entries": {
                "$let": {
                    "vars": {"current": current},
                    "in": {"$cond": [
                        matching_key,
                        replace_entry,
                        {"$cond": [
                            can_add,
                            {"$concatArrays": ["$$current", [{"$literal": entry}]]},
                            "$$current",
                        ]},
                    ]},
                }
            },
            "updated_at": {"$literal": entry["added_at"]},
        }}]

    def get_user_books(self, user_id: int) -> dict[str, list[dict[str, Any]]]:
        document = self.users.find_one({"_id": int(user_id)}, {"entries": 1})
        result: dict[str, list[dict[str, Any]]] = {"saved": [], "favorites": []}
        for entry in (document or {}).get("entries", []):
            collection = entry.get("collection")
            if collection not in BOOKSHELF_COLLECTIONS:
                continue
            book = dict(entry.get("book") or {})
            book["id"] = entry["book_key"]
            added_at = entry.get("added_at")
            book["addedAt"] = int(added_at.timestamp() * 1000) if isinstance(added_at, datetime) else 0
            result[collection].append(book)
        return result

    def upsert_entry(self, user_id: int, collection: str, book: dict[str, Any]) -> bool:
        now = datetime.now(timezone.utc)
        key = normalize_book_key(book)
        entry = {
            "book_key": key,
            "collection": collection,
            "added_at": now,
            "book": book,
        }
        try:
            document = self.users.find_one_and_update(
                {"_id": int(user_id)},
                self._entry_pipeline(entry),
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            # Another request may have created this user's document concurrently.
            document = self.users.find_one_and_update(
                {"_id": int(user_id)},
                self._entry_pipeline(entry),
                upsert=False,
                return_document=ReturnDocument.AFTER,
            )
        return any(
            item.get("book_key") == key and item.get("collection") == collection
            for item in (document or {}).get("entries", [])
        )

    def remove_entries(self, user_id: int, collection: str, keys: list[str]) -> int:
        if not keys:
            return 0
        result = self.users.update_one(
            {"_id": int(user_id)},
            {"$pull": {"entries": {"collection": collection, "book_key": {"$in": keys}}}},
        )
        return int(result.modified_count)

    def import_entries_if_empty(self, user_id: int, entries: list[dict[str, Any]]) -> bool:
        """Import a browser's prototype cache once, without overwriting cloud data."""
        if not entries:
            return False
        now = datetime.now(timezone.utc)
        try:
            result = self.users.update_one(
                {
                    "_id": int(user_id),
                    "$or": [
                        {"entries": {"$exists": False}},
                        {"entries": {"$size": 0}},
                    ],
                },
                {"$set": {"entries": entries, "updated_at": now}},
                upsert=True,
            )
        except DuplicateKeyError:
            # Another device has already populated this user's entry document.
            return False
        return bool(result.matched_count or result.upserted_id is not None)
