"""Tests for owner-managed bot-admin authorization."""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.admins import MongoBotAdminRepository
from src.handlers import GoodreadsBot


class TestMongoBotAdminRepository(unittest.TestCase):
    @patch("src.admins.MongoClient")
    def test_authorize_list_and_revoke_ids(self, mongo_client):
        client = mongo_client.return_value
        collection = client.__getitem__.return_value.__getitem__.return_value
        collection.find.return_value = [{"_id": 12}, {"_id": 34}]
        collection.update_one.return_value.upserted_id = 34
        collection.delete_one.return_value.deleted_count = 1

        repository = MongoBotAdminRepository("mongodb://unit.test", "annie_db")

        self.assertEqual(repository.list_user_ids(), {12, 34})
        self.assertTrue(repository.authorize(34, 1))
        self.assertTrue(repository.unauthorize(34))
        collection.update_one.assert_called_once()
        collection.delete_one.assert_called_once_with({"_id": 34})
        client.close.assert_not_called()


class TestOwnerAdminCommands(unittest.IsolatedAsyncioTestCase):
    def _bot(self, owner_id=1):
        bot = object.__new__(GoodreadsBot)
        bot._owner_user_id = owner_id
        bot._authorized_bot_admin_ids = set()
        bot._bot_admin_cache_loaded = True
        bot._bot_admin_cache_expires_at = 0
        bot._clarification_cancel_abuse = {}
        bot._group_admin_status_cache = {}
        return bot

    def _update(self, user_id):
        update = MagicMock()
        update.effective_user.id = user_id
        update.effective_message.reply_text = AsyncMock()
        return update

    async def test_non_owner_cannot_authorize_anyone(self):
        bot = self._bot()
        bot._load_bot_admin_ids = AsyncMock()
        update = self._update(2)
        context = MagicMock(args=["3"])

        await bot.authorize_command(update, context)

        bot._load_bot_admin_ids.assert_not_awaited()
        self.assertIn("only available to the bot owner", update.effective_message.reply_text.await_args.args[0])

    async def test_non_owner_cannot_list_authorized_users(self):
        bot = self._bot()
        bot._load_bot_admin_ids = AsyncMock()
        update = self._update(2)
        context = MagicMock(args=[])

        await bot.admins_command(update, context)

        bot._load_bot_admin_ids.assert_not_awaited()
        self.assertIn("only available to the bot owner", update.effective_message.reply_text.await_args.args[0])

    async def test_admins_command_shows_owner_names_usernames_and_ids(self):
        bot = self._bot()
        bot._authorized_bot_admin_ids = {42, 43}
        bot._load_bot_admin_ids = AsyncMock(return_value=True)
        update = self._update(1)
        update.effective_user.first_name = "Nero"
        update.effective_user.last_name = "Owner"
        update.effective_user.username = "NeroTag"
        context = MagicMock(args=[])
        context.bot.get_chat = AsyncMock(side_effect=[
            MagicMock(first_name="Alex", last_name="Blaze", username="Evangelist"),
            MagicMock(first_name="Taylor", last_name="", username=None),
        ])

        await bot.admins_command(update, context)

        text = update.effective_message.reply_text.await_args.args[0]
        self.assertIn("Total admins: 3", text)
        self.assertIn("Nero Owner", text)
        self.assertIn("@NeroTag", text)
        self.assertIn("@Evangelist", text)
        self.assertIn("Alex Blaze", text)
        self.assertIn("Taylor", text)
        self.assertIn("👑 Owner", text)
        self.assertIn("✅ Admin", text)
        self.assertIn("<code>42</code>", text)
        self.assertIn("<code>43</code>", text)

    async def test_owner_can_authorize_and_revoke_user_and_cache_updates(self):
        bot = self._bot()
        bot._load_bot_admin_ids = AsyncMock(return_value=True)
        repository = MagicMock()
        repository.authorize.return_value = True
        repository.unauthorize.return_value = True
        bot._get_bot_admin_repository = MagicMock(return_value=repository)
        update = self._update(1)
        context = MagicMock(args=["42"])

        await bot.authorize_command(update, context)
        self.assertIn(42, bot._authorized_bot_admin_ids)

        await bot.unauthorize_command(update, context)
        self.assertNotIn(42, bot._authorized_bot_admin_ids)
        repository.authorize.assert_called_once_with(42, 1)
        repository.unauthorize.assert_called_once_with(42)

    async def test_only_bot_owner_is_published_admin_command_scope(self):
        bot = self._bot()
        bot.app = MagicMock()
        bot.app.bot.set_my_commands = AsyncMock()
        bot._bot_admin_cache_loaded = True
        bot._get_bot_admin_repository = MagicMock(return_value=None)

        await bot._configure_telegram_commands()

        self.assertEqual(bot.app.bot.set_my_commands.await_count, 4)
        public_command_names = {
            command.command
            for command in bot.app.bot.set_my_commands.await_args_list[0].args[0]
        }
        self.assertTrue({"authorize", "unauthorize", "admins"}.isdisjoint(public_command_names))
        owner_commands = bot.app.bot.set_my_commands.await_args_list[-1].args[0]
        owner_scope = bot.app.bot.set_my_commands.await_args_list[-1].kwargs["scope"]
        self.assertEqual(owner_scope.chat_id, 1)
        self.assertTrue({"authorize", "unauthorize", "admins"}.issubset(
            {command.command for command in owner_commands}
        ))

    async def test_authorized_user_bypasses_existing_cooldown_and_block(self):
        bot = self._bot()
        bot._authorized_bot_admin_ids.add(42)
        bot._clarification_cancel_abuse[42] = {
            "cooldown_until": 9_999_999_999,
            "blocked_until": 9_999_999_999,
        }

        self.assertTrue(bot._is_privileged_bot_user(42))
        self.assertIsNone(bot._active_clarification_restriction(42))
        self.assertIsNone(bot._record_clarification_cancel(42))

    async def test_allowlist_is_cached_between_privilege_checks(self):
        bot = self._bot()
        repository = MagicMock()
        repository.list_user_ids.return_value = {42}
        bot._bot_admin_repository = repository
        chat = MagicMock(type="private")

        self.assertTrue(await bot._is_owner_or_group_admin(42, chat))
        self.assertTrue(await bot._is_owner_or_group_admin(42, chat))

        repository.list_user_ids.assert_called_once_with()

    async def test_missing_owner_skips_admin_database_reads(self):
        bot = self._bot(owner_id=None)
        with patch.dict("os.environ", {"MONGODB_URI": "mongodb://should-not-connect"}), patch(
            "src.handlers.MongoBotAdminRepository"
        ) as repository:
            self.assertFalse(await bot._load_bot_admin_ids(force=True))
        repository.assert_not_called()


if __name__ == "__main__":
    unittest.main()
