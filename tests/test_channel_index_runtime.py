from __future__ import annotations

import copy
import unittest
from unittest.mock import AsyncMock
from types import SimpleNamespace

from src.channel_index import ChannelIndexRuntime, MongoChannelIndexRepository


class MemoryIndexRepository:
    def __init__(self, config: dict, managed_main: bool = True):
        self.config = copy.deepcopy(config)
        self.managed_main = managed_main
        self.managed_main_checks: list[tuple[int, int]] = []

    def get(self, channel_id: int):
        return copy.deepcopy(self.config)

    def is_channel_manager_main_post(self, channel_id: int, message_id: int) -> bool:
        self.managed_main_checks.append((int(channel_id), int(message_id)))
        return self.managed_main

    def update(self, channel_id: int, fields: dict) -> None:
        self.config.update(copy.deepcopy(fields))

    def remove_pending(self, channel_id: int, post_id: int) -> None:
        self.config["pending_posts"] = [
            item for item in self.config.get("pending_posts", [])
            if int(item.get("message_id", -1)) != int(post_id)
        ]


class TestMongoChannelIndexCloneEligibility(unittest.TestCase):
    def test_recognizes_cloned_main_post_but_not_cloned_extra_message(self):
        class Collection:
            def __init__(self, mapping=None):
                self.mapping = mapping
                self.queries = []

            def find_one(self, query, projection=None):
                self.queries.append(query)
                if "destination_channel_id" in query:
                    return self.mapping
                if query.get("channel_id") == -1001 and query.get("published_main_message_id") == 15:
                    return {"_id": "original-record"}
                return None

        repository = MongoChannelIndexRepository.__new__(MongoChannelIndexRepository)
        repository.channel_manager_records = Collection()
        repository.channel_manager_clone_messages = Collection({
            "source_channel_id": -1001, "source_message_id": 15,
        })

        self.assertTrue(repository.is_channel_manager_main_post(-1002, 900))

        repository.channel_manager_clone_messages.mapping = {
            "source_channel_id": -1001, "source_message_id": 16,
        }
        self.assertFalse(repository.is_channel_manager_main_post(-1002, 901))


def _runtime(config: dict, *, managed_main: bool = True, bot=None):
    runtime = ChannelIndexRuntime(
        bot or AsyncMock(), MemoryIndexRepository(config, managed_main), 1, "hash", "token"
    )
    runtime.bot_user_id = 777
    return runtime


class TestChannelIndexManagedPosts(unittest.IsolatedAsyncioTestCase):
    async def test_user_session_registers_only_deletion_handler_without_login_prompt(self):
        runtime = _runtime({"enabled": True, "entries": [], "targets": []})

        class FakeUserClient:
            def __init__(self, session, api_id, api_hash, **kwargs):
                self.session = session
                self.events = []
                self.disconnected = False

            async def connect(self):
                return None

            async def is_user_authorized(self):
                return True

            async def get_me(self):
                return SimpleNamespace(bot=False)

            def add_event_handler(self, callback, event_builder):
                self.events.append((callback, event_builder))

            async def disconnect(self):
                self.disconnected = True

        class FakeStringSession:
            def __init__(self, value):
                self.value = value

        class FakeEvents:
            @staticmethod
            def MessageDeleted():
                return "deletion-event"

        runtime.user_session = "private-session-string"
        started = await runtime._start_user_deletion_listener(
            FakeUserClient, FakeStringSession, FakeEvents
        )

        self.assertTrue(started)
        self.assertIsNotNone(runtime.user_client)
        self.assertEqual(len(runtime.user_client.events), 1)
        self.assertEqual(runtime.user_client.events[0][0], runtime._on_user_deleted_posts)
        self.assertEqual(runtime.user_client.events[0][1], "deletion-event")
        self.assertEqual(runtime.user_client.session.value, "private-session-string")

    async def test_user_session_ignores_deletions_outside_enabled_indexes(self):
        runtime = _runtime({"enabled": False, "entries": [], "targets": []})
        runtime._on_deleted_posts = AsyncMock()

        await runtime._on_user_deleted_posts(
            SimpleNamespace(chat_id=-100123, deleted_ids=[123])
        )

        runtime._on_deleted_posts.assert_not_awaited()

    async def test_annie_managed_main_post_is_added_to_index(self):
        config = {
            "enabled": True,
            "targets": [{"message_id": 900, "base_html": "Book Index"}],
            "entries": [],
            "pending_posts": [{
                "message_id": 123, "title": "The Example Book",
                "sender_id": 777, "sender_is_bot": True, "categorized": False,
            }],
        }
        bot = AsyncMock()
        runtime = _runtime(config, bot=bot)

        await runtime._publish_after_delay(-100123, 123, "The Example Book", 0)

        self.assertEqual(runtime.repository.config["entries"][0]["source_message_id"], 123)
        self.assertEqual(runtime.repository.managed_main_checks, [(-100123, 123)])
        bot.edit_message_text.assert_awaited_once()

    async def test_annie_auxiliary_post_is_skipped(self):
        config = {
            "enabled": True,
            "targets": [{"message_id": 900, "base_html": "Book Index"}],
            "entries": [],
            "pending_posts": [{
                "message_id": 124, "title": "A reusable footer",
                "sender_id": 777, "sender_is_bot": True,
            }],
        }
        bot = AsyncMock()
        runtime = _runtime(config, managed_main=False, bot=bot)

        await runtime._publish_after_delay(-100123, 124, "A reusable footer", 0)

        self.assertEqual(runtime.repository.config["entries"], [])
        self.assertEqual(runtime.repository.managed_main_checks, [(-100123, 124)])
        bot.edit_message_text.assert_not_awaited()

    async def test_deleted_annie_post_removes_entry_and_refreshes_index(self):
        config = {
            "enabled": True,
            "targets": [{"message_id": 900, "base_html": "Book Index"}],
            "entries": [{
                "source_message_id": 123, "target_message_id": 900,
                "title": "The Example Book", "url": "https://t.me/c/123/123",
            }],
            "pending_posts": [],
        }
        bot = AsyncMock()
        runtime = _runtime(config, bot=bot)

        await runtime._on_deleted_posts(SimpleNamespace(chat_id=-100123, deleted_ids=[123]))

        self.assertEqual(runtime.repository.config["entries"], [])
        bot.edit_message_text.assert_awaited_once()
        self.assertEqual(bot.edit_message_text.await_args.kwargs["text"], "Book Index")

    async def test_index_edit_failure_does_not_block_other_targets(self):
        config = {
            "enabled": True,
            "targets": [
                {"message_id": 900, "base_html": "Index A"},
                {"message_id": 901, "base_html": "Index B"},
            ],
            "entries": [
                {"source_message_id": 123, "target_message_id": 900, "title": "A", "url": "u1"},
                {"source_message_id": 123, "target_message_id": 901, "title": "A", "url": "u2"},
            ],
            "pending_posts": [],
        }
        bot = AsyncMock()
        bot.edit_message_text.side_effect = [RuntimeError("temporary Telegram error"), None]
        runtime = _runtime(config, bot=bot)

        await runtime._on_deleted_posts(SimpleNamespace(chat_id=-100123, deleted_ids=[123]))

        self.assertEqual(runtime.repository.config["entries"], [])
        self.assertEqual(bot.edit_message_text.await_count, 2)

    async def test_reconciliation_removes_deleted_source_and_refreshes_index(self):
        config = {
            "_id": -100123,
            "enabled": True,
            "targets": [{"message_id": 900, "base_html": "Book Index"}],
            "entries": [{
                "source_message_id": 123, "target_message_id": 900,
                "title": "The Example Book", "url": "https://t.me/c/123/123",
            }],
            "pending_posts": [],
        }
        bot = AsyncMock()
        runtime = _runtime(config, bot=bot)
        runtime._messages_by_id = AsyncMock(return_value={
            900: SimpleNamespace(
                id=900,
                entities=[SimpleNamespace(url="https://t.me/c/123/123")],
            ),
        })

        verified = await runtime._reconcile_saved_entries(config)

        self.assertTrue(verified)
        self.assertEqual(runtime.repository.config["entries"], [])
        bot.edit_message_text.assert_awaited_once()
        self.assertEqual(bot.edit_message_text.await_args.kwargs["text"], "Book Index")


if __name__ == "__main__":
    unittest.main()
