from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.channel_management import MongoChannelManagementRepository


def _cursor_with(records):
    cursor = MagicMock()
    cursor.sort.return_value = cursor
    cursor.skip.return_value = cursor
    cursor.limit.return_value = cursor
    cursor.__iter__.return_value = iter(records)
    return cursor


class TestChannelManagementRecordRetention(unittest.TestCase):
    def _repository(self, records):
        repository = object.__new__(MongoChannelManagementRepository)
        repository.records = MagicMock()
        repository.records.find.return_value = _cursor_with(records)
        return repository

    def test_published_pruning_only_targets_old_excess_records_in_one_channel(self):
        oldest = {"_id": "oldest", "preview_chat_id": 123}
        raced = {"_id": "changed-before-delete"}
        repository = self._repository([oldest, raced])
        repository.records.delete_one.side_effect = [
            SimpleNamespace(deleted_count=1), SimpleNamespace(deleted_count=0),
        ]

        removed = repository.prune_published_records(-1001, keep=40)

        self.assertEqual(removed, [oldest])
        query = repository.records.find.call_args.args[0]
        self.assertEqual(query, {
            "channel_id": -1001, "kind": "post", "status": "published",
        })
        self.assertEqual(repository.records.find.return_value.skip.call_args.args, (40,))
        self.assertEqual(repository.records.delete_one.call_args_list[0].args[0], {
            "_id": "oldest", "channel_id": -1001,
            "kind": "post", "status": "published",
        })

    def test_published_pruning_rejects_zero_or_negative_limits(self):
        repository = self._repository([])

        for keep in (0, -1):
            with self.subTest(keep=keep), self.assertRaises(ValueError):
                repository.prune_published_records(-1001, keep=keep)

        repository.records.find.assert_not_called()

    def test_draft_cleanup_is_stale_post_only_and_rechecks_before_delete(self):
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        stale = {"_id": "stale", "channel_id": -1001, "preview_chat_id": 123}
        repository = self._repository([stale])
        repository.records.delete_one.return_value = SimpleNamespace(deleted_count=1)

        removed = repository.delete_stale_drafts(cutoff, limit=100)

        self.assertEqual(removed, [stale])
        query = repository.records.find.call_args.args[0]
        self.assertEqual(query["kind"], "post")
        self.assertEqual(query["status"], "draft")
        self.assertEqual(query["$or"][0], {"updated_at": {"$lte": cutoff}})
        delete_query = repository.records.delete_one.call_args.args[0]
        self.assertEqual(delete_query["_id"], "stale")
        self.assertEqual(delete_query["channel_id"], -1001)
        self.assertEqual(delete_query["kind"], "post")
        self.assertEqual(delete_query["status"], "draft")

    def test_draft_cleanup_leaves_records_edited_after_scan(self):
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        stale = {"_id": "recently-edited", "channel_id": -1001}
        repository = self._repository([stale])
        repository.records.delete_one.return_value = SimpleNamespace(deleted_count=0)

        removed = repository.delete_stale_drafts(cutoff)

        self.assertEqual(removed, [])
        repository.records.delete_one.assert_called_once()

    def test_draft_cleanup_does_not_query_when_batch_size_is_invalid(self):
        repository = self._repository([])

        self.assertEqual(repository.delete_stale_drafts(datetime.now(timezone.utc), 0), [])
        repository.records.find.assert_not_called()


if __name__ == "__main__":
    unittest.main()
