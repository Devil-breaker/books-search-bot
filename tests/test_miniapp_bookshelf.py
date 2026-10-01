"""Tests for bounded bookshelf records and their HTTP access rules."""

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from flask import Flask

from src.miniapp import create_miniapp_blueprint
from src.miniapp.bookshelf import MongoBookshelfRepository, normalize_book_key, sanitize_book


class FakeBookshelfRepository:
    def __init__(self):
        self.books = {"saved": [], "favorites": []}
        self.calls = []

    def get_user_books(self, user_id):
        self.calls.append(("get", user_id))
        return self.books

    def upsert_entry(self, user_id, collection, book):
        self.calls.append(("upsert", user_id, collection, book))
        return True

    def remove_entries(self, user_id, collection, keys):
        self.calls.append(("remove", user_id, collection, keys))
        return len(keys)


class FakeMongoCollection:
    def __init__(self):
        self.calls = []

    def update_one(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return type("Result", (), {"matched_count": 1, "upserted_id": None, "modified_count": 1})()

    def find_one(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {"entries": []}

    def find_one_and_update(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        expression = args[1][0]["$set"]["entries"]["$let"]["in"]["$cond"]
        entry = expression[2]["$cond"][1]["$concatArrays"][1][0]["$literal"]
        return {"_id": args[0]["_id"], "entries": [entry]}


class MiniAppBookshelfTests(unittest.TestCase):
    def setUp(self):
        self.repo = FakeBookshelfRepository()
        self.runtime = {"bookshelf_repository": self.repo}
        app = Flask(__name__)
        app.register_blueprint(create_miniapp_blueprint(self.runtime), url_prefix="/miniapp")
        self.client = app.test_client()
        self.auth = patch("src.miniapp.routes.validate_init_data", return_value={
            "id": 1234, "first_name": "Reader", "language_code": "en",
        })
        self.auth.start()
        self.addCleanup(self.auth.stop)
        self.env = patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test", "MONGODB_URI": "mongodb://test"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.headers = {"X-Telegram-Init-Data": "signed-test-data"}

    def test_book_key_uses_isbn_or_normalized_title_and_author(self):
        self.assertEqual(normalize_book_key({"isbn": "978-0-306-40615-7"}), "isbn:9780306406157")
        self.assertEqual(
            normalize_book_key({"title": "  DÚNE! ", "author": "Frank Herbert"}),
            normalize_book_key({"title": "Dúne", "author": "Frank Herbert"}),
        )

    def test_sanitizer_keeps_bounded_description_and_discards_unapproved_fields(self):
        book = sanitize_book({
            "title": "Dune", "author": "Frank Herbert", "description": "x" * 6000,
            "translated_description": "translated", "details_checked": True, "internal_token": "secret",
        })
        self.assertEqual(len(book["description"]), 5000)
        self.assertEqual(book["translated_description"], "translated")
        self.assertTrue(book["details_checked"])
        self.assertNotIn("internal_token", book)

    def test_repository_uses_one_atomic_write_for_add_or_move(self):
        repository = object.__new__(MongoBookshelfRepository)
        repository.users = FakeMongoCollection()
        accepted = repository.upsert_entry(1234, "favorites", {"title": "Dune", "author": "Frank Herbert"})
        self.assertTrue(accepted)
        self.assertEqual(len(repository.users.calls), 1)
        args, kwargs = repository.users.calls[0]
        self.assertEqual(args[0]["_id"], 1234)
        self.assertNotIn("$expr", args[0])
        self.assertTrue(kwargs["upsert"])
        pipeline = args[1]
        self.assertIn("$let", pipeline[0]["$set"]["entries"])
        self.assertIn("$map", str(pipeline[0]["$set"]["entries"]))
        self.assertIn("$size", str(pipeline[0]["$set"]["entries"]))
        self.assertIn("$lt", str(pipeline[0]["$set"]["entries"]))

    def test_bulk_remove_is_one_database_write(self):
        repository = object.__new__(MongoBookshelfRepository)
        repository.users = FakeMongoCollection()
        removed = repository.remove_entries(1234, "saved", ["book:a|b", "book:c|d"])
        self.assertEqual(removed, 1)
        self.assertEqual(len(repository.users.calls), 1)
        args, _ = repository.users.calls[0]
        self.assertEqual(args[0], {"_id": 1234})
        self.assertEqual(args[1]["$pull"]["entries"]["collection"], "saved")

    def test_import_is_single_upsert_without_creating_an_index(self):
        repository = object.__new__(MongoBookshelfRepository)
        repository.users = FakeMongoCollection()
        result = repository.import_entries_if_empty(1234, [{
            "book_key": "book:dune|frank herbert", "collection": "saved",
            "added_at": datetime.now(timezone.utc),
            "book": {"title": "Dune"},
        }])
        self.assertTrue(result)
        self.assertEqual(len(repository.users.calls), 1)
        args, kwargs = repository.users.calls[0]
        self.assertTrue(kwargs["upsert"])
        self.assertIn("$or", args[0])

    def test_read_is_one_repository_call_for_authenticated_user(self):
        response = self.client.get("/miniapp/api/bookshelf", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.repo.calls, [("get", 1234)])

    def test_upsert_is_one_repository_call_and_ignores_client_user_id(self):
        response = self.client.post("/miniapp/api/bookshelf", headers=self.headers, json={
            "action": "upsert", "collection": "favorites", "user_id": 9999,
            "book": {"title": "Dune", "author": "Frank Herbert", "description": "A desert planet."},
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.repo.calls), 1)
        operation, user_id, collection, book = self.repo.calls[0]
        self.assertEqual((operation, user_id, collection), ("upsert", 1234, "favorites"))
        self.assertEqual(book["description"], "A desert planet.")


if __name__ == "__main__":
    unittest.main()
