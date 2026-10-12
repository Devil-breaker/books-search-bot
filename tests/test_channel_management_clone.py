from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.channel_management import ChannelManager


class CloneRepository:
    def __init__(self):
        self.records = [{"_id": "post-1", "published_message_ids": [15]}]
        self.mappings = [{"source_message_id": 15, "destination_message_id": 900}]
        self.deleted = []
        self.index_config = None
        self.saved_index_config = None
        self.saved_posts = []

    def list_published_records_for_clone(self, source_id):
        return self.records

    @staticmethod
    def clone_pair_id(source_id, destination_id):
        return f"{int(source_id)}:{int(destination_id)}"

    def list_clone_mappings(self, source_id, destination_id, ids):
        return [item for item in self.mappings if item["source_message_id"] in ids]

    def delete_clone_message(self, source_id, destination_id, message_id):
        self.deleted.append(message_id)
        self.mappings = [item for item in self.mappings if item["source_message_id"] != message_id]
        return True

    def get_index_config(self, channel_id):
        return self.index_config

    def save_cloned_index_config(self, channel_id, fields):
        self.saved_index_config = (channel_id, fields)

    def save_cloned_published_record(self, source_record, pair_id, destination_channel_id,
                                     destination_message_ids, destination_main_message_id):
        self.saved_posts.append((source_record, pair_id, destination_channel_id,
                                 destination_message_ids, destination_main_message_id))

    def update(self, record_id, changes):
        self.updated = (record_id, changes)
        return True


class TestChannelManagerCloning(unittest.IsolatedAsyncioTestCase):
    def make_manager(self):
        return ChannelManager(None, lambda: None, AsyncMock())

    async def test_deleted_destination_copy_is_unmapped_and_cloned_again(self):
        manager = self.make_manager()
        repository = CloneRepository()
        manager._get_repository = lambda: repository
        manager._channel_for_action = AsyncMock(return_value={"name": "Channel"})
        manager.get_channel_messages = AsyncMock(return_value={})
        manager._clone_markup_for_message = lambda record, message_id: None
        manager._copy_message_once = AsyncMock(return_value=901)
        manager._edit_or_send = AsyncMock()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=42))
        context = SimpleNamespace(bot=object())

        await manager._clone_channel_records(update, context, -1001, -1002)

        self.assertEqual(repository.deleted, [15])
        manager.get_channel_messages.assert_awaited_once_with(-1002, [900])
        manager._copy_message_once.assert_awaited_once()
        self.assertEqual(manager._copy_message_once.await_args.args[4], 15)

    async def test_unavailable_destination_verification_stops_before_copy(self):
        manager = self.make_manager()
        repository = CloneRepository()
        manager._get_repository = lambda: repository
        manager._channel_for_action = AsyncMock(return_value={"name": "Channel"})
        manager.get_channel_messages = AsyncMock(return_value=None)
        manager._copy_message_once = AsyncMock()
        manager._edit_or_send = AsyncMock()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=42))
        context = SimpleNamespace(bot=object())

        await manager._clone_channel_records(update, context, -1001, -1002)

        manager._copy_message_once.assert_not_awaited()
        self.assertEqual(repository.deleted, [])

    async def test_extra_message_keeps_its_own_buttons_without_inheriting_main_buttons(self):
        manager = self.make_manager()
        captured = []

        async def capture(bot, chat_id, record, preview=False):
            captured.append(record)
            return SimpleNamespace(message_id=len(captured))

        manager._send_content = capture
        record = {
            "_id": "post-1", "text": "Main", "post_buttons": [{"text": "Main link", "url": "https://main.test"}],
            "followups": [
                {"text": "Extra one", "post_buttons": [{"text": "Extra link", "url": "https://extra.test"}]},
                {"text": "Extra two"},
            ],
        }

        await manager._send_post_messages(object(), -1002, record)

        self.assertEqual(captured[0]["post_buttons"][0]["text"], "Main link")
        self.assertEqual(captured[1]["post_buttons"][0]["text"], "Extra link")
        self.assertEqual(captured[2]["post_buttons"], [])

    async def test_extra_text_edit_saves_to_repository_and_refreshes_preview(self):
        manager = self.make_manager()
        repository = CloneRepository()
        manager._get_repository = lambda: repository
        manager._record_for_user = AsyncMock(return_value=(
            {"_id": "post-1", "channel_id": -1001, "status": "draft", "format_mode": "telegram",
             "followups": [{"text": "Before", "entities": []}]},
            {"id": -1001},
        ))
        manager._content_from_message = lambda message, mode: {"text": "After", "entities": []}
        manager._delete_message = AsyncMock()
        manager._clear_input_prompt = AsyncMock()
        manager._preview = AsyncMock()
        message = SimpleNamespace(
            text="After", chat=SimpleNamespace(id=42), message_id=700,
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=42), effective_message=message,
        )
        context = SimpleNamespace(bot=object())

        await manager._process_followup_input(update, context, {
            "kind": "followup_edit_text", "record_id": "post-1", "followup_index": 0,
            "prompt_chat_id": 42, "prompt_message_id": 699,
        })

        self.assertEqual(repository.updated[0], "post-1")
        self.assertEqual(repository.updated[1]["followups"][0]["text"], "After")
        manager._preview.assert_awaited_once_with(update, context, "post-1")

    async def test_cloned_index_links_are_rewritten_and_destination_is_registered(self):
        manager = self.make_manager()
        repository = CloneRepository()
        repository.index_config = {
            "_id": -1001, "enabled": True, "sort_order": "added", "entry_bullet": "📚",
            "targets": [{"message_id": 50,
                         "base_html": 'Books\n<a href="https://t.me/c/1/15">Old link</a>',
                         "entry_bullet": "•"}],
            "entries": [{"source_message_id": 15, "target_message_id": 50,
                         "title": "Example", "url": "https://t.me/c/1001/15"}],
            "pending_posts": [],
        }
        repository.mappings = [
            {"source_message_id": 15, "destination_message_id": 900},
            {"source_message_id": 50, "destination_message_id": 901},
        ]
        manager._get_repository = lambda: repository
        bot = SimpleNamespace(edit_message_text=AsyncMock())

        result = await manager._clone_index_configuration(bot, repository, -1001, -1002)

        self.assertEqual(result, {"status": "updated", "entries": 1, "omitted": 0, "enabled": True})
        bot.edit_message_text.assert_awaited_once()
        self.assertIn("https://t.me/c/2/900", bot.edit_message_text.await_args.kwargs["text"])
        self.assertEqual(bot.edit_message_text.await_args.kwargs["text"].count("https://t.me/c/2/900"), 2)
        self.assertNotIn("https://t.me/c/1/15", bot.edit_message_text.await_args.kwargs["text"])
        destination_id, config = repository.saved_index_config
        self.assertEqual(destination_id, -1002)
        self.assertTrue(config["enabled"])
        self.assertEqual(config["targets"][0]["message_id"], 901)
        self.assertEqual(config["entries"][0]["source_message_id"], 900)
        self.assertEqual(config["entries"][0]["target_message_id"], 901)

    async def test_cloned_posts_get_destination_published_records(self):
        manager = self.make_manager()
        repository = CloneRepository()
        repository.mappings = [
            {"source_message_id": 10, "destination_message_id": 900},
            {"source_message_id": 11, "destination_message_id": 901},
        ]
        record = {
            "_id": "source-record", "channel_id": -1001, "kind": "post", "status": "published",
            "published_message_ids": [10, 11], "published_main_message_id": 10,
            "text": "A cloned post", "post_buttons": [],
        }

        saved, incomplete = await manager._save_cloned_published_records(
            repository, [record], -1001, -1002,
        )

        self.assertEqual((saved, incomplete), (1, 0))
        self.assertEqual(len(repository.saved_posts), 1)
        self.assertEqual(repository.saved_posts[0][2:], (-1002, [900, 901], 900))


if __name__ == "__main__":
    unittest.main()
