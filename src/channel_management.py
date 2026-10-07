"""Private, allowlisted channel post management for Annie."""

from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import MongoClient, ReturnDocument
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, InputMediaAnimation,
    InputMediaAudio, InputMediaDocument, InputMediaPhoto, InputMediaVideo,
    MessageEntity,
    ReplyKeyboardRemove, Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut
from telegram.ext import ContextTypes

from src.utils import html_escape, logger


class PartialPostSendError(Exception):
    """A multi-message post was only partly sent; avoid silent duplicate retries."""

    def __init__(self, sent_message_ids: list[int], cause: Exception) -> None:
        super().__init__(type(cause).__name__)
        self.sent_message_ids = sent_message_ids
        self.cause = cause


class MongoChannelManagementRepository:
    """Persist templates, drafts, and scheduled posts independently of connections."""

    def __init__(self, uri: str, database_name: str = "annie_db") -> None:
        self.client = MongoClient(
            uri,
            appname="AnnieChannelManagement",
            serverSelectionTimeoutMS=3000,
            connectTimeoutMS=3000,
            socketTimeoutMS=10000,
            waitQueueTimeoutMS=3000,
            maxPoolSize=5,
            tz_aware=True,
        )
        self.records = self.client[database_name]["bot_channel_manager_records"]
        self.approvals = self.client[database_name]["bot_channel_manager_approvals"]
        self.marginals = self.client[database_name]["bot_channel_manager_marginals"]
        self.clone_pairs = self.client[database_name]["bot_channel_manager_clone_pairs"]
        self.clone_messages = self.client[database_name]["bot_channel_manager_clone_messages"]
        self._indexes_ready = False

    def ensure_indexes(self) -> None:
        if self._indexes_ready:
            return
        self.records.create_index([("channel_id", 1), ("kind", 1), ("status", 1)])
        self.records.create_index([("status", 1), ("scheduled_at", 1)])
        self.approvals.create_index([("updated_at", -1)])
        self.marginals.create_index([("channel_id", 1)], unique=True)
        self.clone_pairs.create_index([("source_channel_id", 1), ("destination_channel_id", 1)], unique=True)
        self.clone_pairs.create_index([("source_channel_id", 1), ("auto_forward", 1)])
        self.clone_messages.create_index([("pair_id", 1), ("source_message_id", 1)], unique=True)
        self._indexes_ready = True

    def list_approvals(self) -> list[dict[str, Any]]:
        return list(self.approvals.find({}).sort("name", 1))

    def approve_channel(
        self, channel_id: int, name: str, username: str | None, approved_by: int
    ) -> bool:
        now = datetime.now(timezone.utc)
        result = self.approvals.update_one(
            {"_id": int(channel_id)},
            {"$set": {
                "name": str(name), "username": username,
                "approved_by": int(approved_by), "updated_at": now,
            }, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
        return result.upserted_id is not None

    def revoke_channel(self, channel_id: int) -> bool:
        return self.approvals.delete_one({"_id": int(channel_id)}).deleted_count > 0

    def get_marginals(self, channel_id: int) -> dict[str, Any]:
        value = self.marginals.find_one({"channel_id": int(channel_id)}) or {}
        header_items = list(value.get("header_items") or [])
        footer_items = list(value.get("footer_items") or [])
        legacy_header = []
        if (value.get("header") or {}).get("text"):
            legacy_header.append(dict(value["header"]))
        if value.get("header_content"):
            legacy_header.append(dict(value["header_content"]))
        legacy_footer = []
        if (value.get("footer") or {}).get("text"):
            legacy_footer.append(dict(value["footer"]))
        if value.get("footer_content"):
            legacy_footer.append(dict(value["footer_content"]))
        elif (value.get("footer_sticker") or {}).get("file_id"):
            legacy_footer.append({"media": {"type": "sticker", "file_id": value["footer_sticker"]["file_id"]}})
        header_items = [*legacy_header, *(item for item in header_items if item not in legacy_header)]
        footer_items = [*legacy_footer, *(item for item in footer_items if item not in legacy_footer)]
        return {
            "header_items": header_items,
            "footer_items": footer_items,
            "header_buttons": list(value.get("header_buttons") or []),
            "footer_buttons": list(value.get("footer_buttons") or []),
            "types": list(value.get("types", ["text", "photo", "video", "document", "animation", "audio", "voice"])),
        }

    def update_marginals(self, channel_id: int, fields: dict[str, Any]) -> bool:
        values = dict(fields)
        values["updated_at"] = datetime.now(timezone.utc)
        self.marginals.update_one(
            {"channel_id": int(channel_id)},
            {"$set": values, "$setOnInsert": {"channel_id": int(channel_id)}},
            upsert=True,
        )
        return True

    def add_marginal_item(self, channel_id: int, field: str, item: dict[str, Any]) -> None:
        if field not in {"header", "footer"}:
            raise ValueError("Invalid marginal field")
        now = datetime.now(timezone.utc)
        self.marginals.update_one(
            {"channel_id": int(channel_id)},
            {"$push": {f"{field}_items": item}, "$set": {"updated_at": now},
             "$setOnInsert": {"channel_id": int(channel_id)}},
            upsert=True,
        )

    def add_marginal_button(self, channel_id: int, field: str, button: dict[str, Any]) -> None:
        if field not in {"header", "footer"}:
            raise ValueError("Invalid marginal field")
        now = datetime.now(timezone.utc)
        self.marginals.update_one(
            {"channel_id": int(channel_id)},
            {"$push": {f"{field}_buttons": button}, "$set": {"updated_at": now},
             "$setOnInsert": {"channel_id": int(channel_id)}},
            upsert=True,
        )

    def remove_marginal_item(self, channel_id: int, field: str, index: int) -> bool:
        settings = self.get_marginals(channel_id)
        key = f"{field}_items"
        items = list(settings.get(key) or [])
        if field not in {"header", "footer"} or not 0 <= index < len(items):
            return False
        del items[index]
        cleared = {key: items, field: {}, f"{field}_content": {}}
        if field == "footer":
            cleared["footer_sticker"] = {}
        self.update_marginals(channel_id, cleared)
        return True

    def remove_marginal_button(self, channel_id: int, field: str, index: int) -> bool:
        settings = self.get_marginals(channel_id)
        key = f"{field}_buttons"
        buttons = list(settings.get(key) or [])
        if field not in {"header", "footer"} or not 0 <= index < len(buttons):
            return False
        del buttons[index]
        self.update_marginals(channel_id, {key: buttons})
        return True

    def create(self, document: dict[str, Any]) -> str:
        now = datetime.now(timezone.utc)
        value = dict(document)
        value.setdefault("created_at", now)
        value["updated_at"] = now
        return str(self.records.insert_one(value).inserted_id)

    def get(self, record_id: str) -> dict[str, Any] | None:
        try:
            object_id = ObjectId(record_id)
        except (InvalidId, TypeError):
            return None
        return self.records.find_one({"_id": object_id})

    def update(self, record_id: str, fields: dict[str, Any]) -> bool:
        try:
            object_id = ObjectId(record_id)
        except (InvalidId, TypeError):
            return False
        update = dict(fields)
        update["updated_at"] = datetime.now(timezone.utc)
        return self.records.update_one(
            {"_id": object_id}, {"$set": update}
        ).matched_count > 0

    def claim_publish(self, record_id: str, confirmed_retry: bool = False) -> dict[str, Any] | None:
        """Claim a draft atomically so repeated taps cannot publish it twice."""
        try:
            object_id = ObjectId(record_id)
        except (InvalidId, TypeError):
            return None
        allowed = ["draft", "failed"]
        if confirmed_retry:
            allowed.append("needs_review")
        now = datetime.now(timezone.utc)
        return self.records.find_one_and_update(
            {"_id": object_id, "kind": "post", "status": {"$in": allowed}},
            {"$set": {
                "status": "publishing", "publishing_at": now,
                "updated_at": now,
            }},
            return_document=ReturnDocument.AFTER,
        )

    def delete(self, record_id: str) -> bool:
        try:
            object_id = ObjectId(record_id)
        except (InvalidId, TypeError):
            return False
        return self.records.delete_one({"_id": object_id}).deleted_count > 0

    def list_records(
        self,
        channel_id: int,
        kind: str,
        statuses: list[str] | None = None,
        user_id: int | None = None,
        include_shared: bool = False,
        limit: int = 30,
        skip: int = 0,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"channel_id": int(channel_id), "kind": kind}
        if statuses:
            query["status"] = {"$in": list(statuses)}
        if user_id is not None and not include_shared:
            query["created_by"] = int(user_id)
        elif user_id is not None and include_shared:
            query["$or"] = [
                {"created_by": int(user_id)}, {"shared": True},
            ]
        return list(
            self.records.find(query).sort("updated_at", -1)
            .skip(max(0, int(skip))).limit(max(1, int(limit)))
        )

    @staticmethod
    def clone_pair_id(source_channel_id: int, destination_channel_id: int) -> str:
        return f"{int(source_channel_id)}:{int(destination_channel_id)}"

    def get_clone_pair(self, source_channel_id: int, destination_channel_id: int) -> dict[str, Any] | None:
        return self.clone_pairs.find_one({"_id": self.clone_pair_id(source_channel_id, destination_channel_id)})

    def list_clone_pairs(self, source_channel_id: int | None = None) -> list[dict[str, Any]]:
        query = {"source_channel_id": int(source_channel_id)} if source_channel_id is not None else {}
        return list(self.clone_pairs.find(query).sort("updated_at", -1))

    def save_clone_pair(
        self, source_channel_id: int, destination_channel_id: int,
        source_name: str, destination_name: str, configured_by: int,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        pair_id = self.clone_pair_id(source_channel_id, destination_channel_id)
        self.clone_pairs.update_one(
            {"_id": pair_id},
            {"$set": {
                "source_channel_id": int(source_channel_id),
                "destination_channel_id": int(destination_channel_id),
                "source_name": str(source_name), "destination_name": str(destination_name),
                "configured_by": int(configured_by), "updated_at": now,
            }, "$setOnInsert": {"auto_forward": False, "created_at": now}},
            upsert=True,
        )
        return self.get_clone_pair(source_channel_id, destination_channel_id) or {}

    def set_auto_forward(self, source_channel_id: int, destination_channel_id: int, enabled: bool) -> bool:
        result = self.clone_pairs.update_one(
            {"_id": self.clone_pair_id(source_channel_id, destination_channel_id)},
            {"$set": {"auto_forward": bool(enabled), "updated_at": datetime.now(timezone.utc)}},
        )
        return result.matched_count > 0

    def list_published_records_for_clone(self, source_channel_id: int) -> list[dict[str, Any]]:
        return list(self.records.find({
            "channel_id": int(source_channel_id), "kind": "post", "status": "published",
            "$or": [
                {"published_message_ids": {"$exists": True, "$ne": []}},
                {"published_message_id": {"$exists": True, "$ne": None}},
            ],
        }).sort([("published_at", 1), ("_id", 1)]))

    def get_clone_message(self, source_channel_id: int, destination_channel_id: int, source_message_id: int) -> dict[str, Any] | None:
        pair_id = self.clone_pair_id(source_channel_id, destination_channel_id)
        return self.clone_messages.find_one({"pair_id": pair_id, "source_message_id": int(source_message_id)})

    def list_cloned_source_ids(self, source_channel_id: int, destination_channel_id: int, source_message_ids: list[int]) -> set[int]:
        if not source_message_ids:
            return set()
        pair_id = self.clone_pair_id(source_channel_id, destination_channel_id)
        return {
            int(item["source_message_id"])
            for item in self.clone_messages.find(
                {"pair_id": pair_id, "source_message_id": {"$in": [int(value) for value in source_message_ids]}},
                {"source_message_id": 1},
            )
        }

    def save_clone_message(
        self, source_channel_id: int, destination_channel_id: int,
        source_message_id: int, destination_message_id: int, record_id: str | None = None,
    ) -> None:
        pair_id = self.clone_pair_id(source_channel_id, destination_channel_id)
        self.clone_messages.update_one(
            {"pair_id": pair_id, "source_message_id": int(source_message_id)},
            {"$set": {
                "pair_id": pair_id, "source_channel_id": int(source_channel_id),
                "destination_channel_id": int(destination_channel_id),
                "source_message_id": int(source_message_id),
                "destination_message_id": int(destination_message_id),
                "record_id": str(record_id) if record_id else None,
                "updated_at": datetime.now(timezone.utc),
            }}, upsert=True,
        )

    def delete_clone_message(self, source_channel_id: int, destination_channel_id: int, source_message_id: int) -> bool:
        pair_id = self.clone_pair_id(source_channel_id, destination_channel_id)
        return self.clone_messages.delete_one({
            "pair_id": pair_id, "source_message_id": int(source_message_id),
        }).deleted_count > 0

    def claim_due(self, now: datetime) -> dict[str, Any] | None:
        """Atomically claim one scheduled post so concurrent workers cannot send it twice."""
        return self.records.find_one_and_update(
            {"kind": "post", "status": "scheduled", "scheduled_at": {"$lte": now}},
            {"$set": {
                "status": "publishing", "publishing_at": now,
                "updated_at": now,
            }},
            sort=[("scheduled_at", 1)],
            return_document=ReturnDocument.AFTER,
        )

    def finish_schedule(
        self, record_id: Any, status: str, fields: dict[str, Any] | None = None
    ) -> None:
        values = dict(fields or {})
        values["status"] = status
        values["updated_at"] = datetime.now(timezone.utc)
        self.records.update_one({"_id": record_id}, {"$set": values})

    def mark_interrupted_sends(self) -> int:
        """Avoid automatic retries when a process stopped during a Telegram send."""
        threshold = datetime.now(timezone.utc) - timedelta(minutes=10)
        result = self.records.update_many(
            {"kind": "post", "status": "publishing", "publishing_at": {"$lt": threshold}},
            {"$set": {
                "status": "needs_review",
                "last_error": "Publishing was interrupted; check the channel before retrying.",
                "updated_at": datetime.now(timezone.utc),
            }},
        )
        return int(result.modified_count)

    def close(self) -> None:
        self.client.close()


class ChannelManager:
    """Own the Channel Manager menu, access checks, composer, and durable scheduler."""

    INPUT_TTL_SECONDS = 30 * 60
    ALLOWED_MEDIA = (
        "photo", "video", "document", "animation", "audio", "sticker",
        "voice", "video_note",
    )

    def __init__(
        self,
        owner_user_id: int | None,
        get_connection_repository: Callable[[], Any],
        open_index_callback: Callable[[Update, ContextTypes.DEFAULT_TYPE, int], Any],
        resolve_user_username: Callable[[str], Any] | None = None,
        delete_channel_messages: Callable[[int, list[int]], Any] | None = None,
    ) -> None:
        self.owner_user_id = int(owner_user_id) if owner_user_id is not None else None
        self.get_connection_repository = get_connection_repository
        self.open_index_callback = open_index_callback
        self.resolve_user_username = resolve_user_username
        self.delete_channel_messages = delete_channel_messages
        self._repository: MongoChannelManagementRepository | None = None
        self._inputs: dict[int, dict[str, Any]] = {}
        self._visibility_cache: dict[int, tuple[float, bool]] = {}
        self._access_cache: dict[int, tuple[float, bool]] = {}
        self._ambiguous_send_ids: set[str] = set()
        self._published_previews: dict[int, list[int]] = {}
        self._clone_pair_locks: dict[str, asyncio.Lock] = {}

    def _get_repository(self) -> MongoChannelManagementRepository | None:
        uri = os.getenv("MONGODB_URI", "").strip()
        if not uri:
            return None
        if self._repository is None:
            database_name = os.getenv("MONGODB_DB_NAME", "annie_db").strip() or "annie_db"
            self._repository = MongoChannelManagementRepository(uri, database_name)
        return self._repository

    async def close(self) -> None:
        repository, self._repository = self._repository, None
        if repository is not None:
            await asyncio.to_thread(repository.close)

    def has_pending_input(self, user_id: int) -> bool:
        return int(user_id) in self._inputs

    def invalidate_access_cache(self, user_id: int | None = None) -> None:
        if user_id is None:
            self._access_cache.clear()
            self._visibility_cache.clear()
            return
        user_id = int(user_id)
        self._access_cache.pop(user_id, None)
        self._visibility_cache.pop(user_id, None)

    @staticmethod
    def _is_private(update: Update) -> bool:
        return bool(
            update.effective_chat
            and update.effective_chat.type == "private"
            and update.effective_user
        )

    @classmethod
    def _can_post(cls, member: Any) -> bool:
        status = getattr(member, "status", "")
        if status == "creator":
            return True
        return status == "administrator" and bool(
            getattr(member, "can_post_messages", False)
        )

    @classmethod
    def _is_chat_admin(cls, member: Any) -> bool:
        return cls._chat_member_status(member) in {"administrator", "creator"}

    async def _connected_channels(self) -> list[dict[str, Any]]:
        repository = self.get_connection_repository()
        if repository is None:
            raise RuntimeError("channel connections need MongoDB")
        manager_repository = self._get_repository()
        if manager_repository is None:
            raise RuntimeError("channel approvals need MongoDB")
        channels, approvals = await asyncio.gather(
            asyncio.to_thread(repository.list_channels),
            asyncio.to_thread(manager_repository.list_approvals),
        )
        approved = {int(item["_id"]): item for item in approvals}
        return [
            {**item, "name": str(approved[int(item["id"])].get("name") or item["name"])}
            for item in channels if int(item["id"]) in approved
        ]

    async def _eligible_channels(self, user_id: int, bot: Any) -> list[dict[str, Any]]:
        channels = await self._connected_channels()
        is_bot_owner = self.owner_user_id is not None and int(user_id) == self.owner_user_id
        semaphore = asyncio.Semaphore(8)

        async def get_member(channel_id: int, user_id: int) -> Any:
            async with semaphore:
                return await bot.get_chat_member(channel_id, user_id)

        async def check_channel(channel: dict[str, Any]) -> dict[str, Any] | None:
            channel_id = int(channel["id"])
            try:
                if is_bot_owner:
                    bot_member = await get_member(channel_id, int(bot.id))
                    user_member = None
                else:
                    bot_member, user_member = await asyncio.gather(
                        get_member(channel_id, int(bot.id)),
                        get_member(channel_id, int(user_id)),
                    )
            except Exception as exc:
                logger.warning(
                    "[channel-manager] channel access check failed channel_id=%s error=%s",
                    channel_id, type(exc).__name__,
                )
                return None
            if not self._is_chat_admin(bot_member):
                logger.info(
                    "[channel-manager] hidden channel without bot admin rights channel_id=%s",
                    channel_id,
                )
                return None
            if user_member is not None and not self._is_chat_admin(user_member):
                return None
            checked_channel = dict(channel)
            checked_channel["_bot_member"] = bot_member
            checked_channel["_actor_member"] = user_member
            return checked_channel

        checked = await asyncio.gather(*(check_channel(channel) for channel in channels))
        return [channel for channel in checked if channel is not None]

    async def should_show_button(self, update: Update, bot: Any) -> bool:
        if not self._is_private(update):
            return False
        user_id = int(update.effective_user.id)
        cached = self._visibility_cache.get(user_id)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        try:
            visible = bool(await self._eligible_channels(user_id, bot))
            self._visibility_cache[user_id] = (time.monotonic() + 20, visible)
            return visible
        except Exception as exc:
            logger.warning(
                "[channel-manager] menu visibility check failed user_id=%s error=%s",
                user_id, type(exc).__name__,
            )
            self._visibility_cache[user_id] = (time.monotonic() + 5, False)
            return False

    async def has_connected_channel_access(self, user_id: int, bot: Any) -> bool:
        """Whether a user is an admin of any channel already connected to Annie."""
        if self.owner_user_id is not None and int(user_id) == self.owner_user_id:
            return True
        user_id = int(user_id)
        now = time.monotonic()
        cached = self._access_cache.get(user_id)
        if cached and cached[0] > now:
            return cached[1]
        try:
            repository = self.get_connection_repository()
            if repository is None:
                return False
            channels = await asyncio.to_thread(repository.list_channels)
            semaphore = asyncio.Semaphore(8)

            async def is_admin(channel: dict[str, Any]) -> bool:
                try:
                    async with semaphore:
                        member = await bot.get_chat_member(int(channel["id"]), int(user_id))
                except Exception:
                    return False
                return self._is_chat_admin(member)

            checks = await asyncio.gather(*(is_admin(channel) for channel in channels))
            has_access = any(checks)
            self._access_cache[user_id] = (time.monotonic() + 10, has_access)
            return has_access
        except Exception as exc:
            logger.warning("[channel-manager] command visibility check failed user_id=%s error=%s", user_id, type(exc).__name__)
        return False

    async def open_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Open Channel Manager directly from the private /channelmanager command."""
        if not self._is_private(update):
            await update.effective_message.reply_text("Use /channelmanager in a private chat with Annie.")
            return
        if context.args:
            await update.effective_message.reply_text("Usage: /channelmanager")
            return
        if not await self.has_connected_channel_access(update.effective_user.id, context.bot):
            await update.effective_message.reply_text("Channel Manager is only available to connected channel owners and admins.")
            return
        await self._show_home(update, context)

    async def _resolve_channel_argument(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        allow_unresolved_id: bool = False,
    ) -> tuple[int, str, str | None] | None:
        message = update.effective_message
        if not message:
            return None
        if len(context.args) != 1:
            await message.reply_text(
                "Give one channel ID or public @username. Example: /channelapprove @mychannel"
            )
            return None
        target = str(context.args[0]).strip()
        if not (target.startswith("@") or re.fullmatch(r"-?\d+", target)):
            await message.reply_text("Use a channel ID or a public @username.")
            return None
        if target.startswith("@") and not re.fullmatch(r"@[A-Za-z0-9_]{5,32}", target):
            await message.reply_text("That public channel username doesn’t look valid.")
            return None
        if not target.startswith("@") and not target.startswith("-100"):
            await message.reply_text("Use a channel ID that starts with -100, or a public @username.")
            return None
        try:
            chat = await context.bot.get_chat(target)
        except Exception as exc:
            logger.info(
                "[channel-manager] owner channel lookup failed user_id=%s target_kind=%s error=%s",
                getattr(update.effective_user, "id", None),
                "username" if target.startswith("@") else "id",
                type(exc).__name__,
            )
            if allow_unresolved_id and target.startswith("-100"):
                return int(target), "Telegram channel", None
            await message.reply_text(
                "I couldn’t find that channel. Check the ID or username, and make sure Annie can access it."
            )
            return None
        if getattr(chat, "type", None) != "channel":
            await message.reply_text("That ID or username is not a Telegram channel.")
            return None
        return int(chat.id), str(chat.title or "Telegram channel"), getattr(chat, "username", None)

    async def _owner_command_allowed(self, update: Update) -> bool:
        message = update.effective_message
        if not message:
            return False
        if not self._is_private(update):
            await message.reply_text("Use this owner command in a private chat with Annie.")
            return False
        if self.owner_user_id is None or int(update.effective_user.id) != self.owner_user_id:
            logger.warning(
                "[channel-manager] owner command denied user_id=%s command=%s",
                getattr(update.effective_user, "id", None),
                message.text.split(maxsplit=1)[0] if message.text else "unknown",
            )
            await message.reply_text("Only the bot owner can approve Channel Manager channels.")
            return False
        return True

    async def approve_channel_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self._owner_command_allowed(update):
            return
        resolved = await self._resolve_channel_argument(update, context)
        if resolved is None:
            return
        channel_id, name, username = resolved
        repository = self._get_repository()
        if repository is None:
            await update.effective_message.reply_text(
                "Channel approvals need MongoDB. Configure MONGODB_URI first."
            )
            return
        try:
            created = await asyncio.to_thread(
                repository.approve_channel, channel_id, name, username,
                int(update.effective_user.id),
            )
        except Exception as exc:
            logger.warning(
                "[channel-manager] channel approval failed channel_id=%s user_id=%s error=%s",
                channel_id, update.effective_user.id, type(exc).__name__,
            )
            await update.effective_message.reply_text("I couldn’t save that approval. Please try again.")
            return
        self.invalidate_access_cache()
        logger.info(
            "[channel-manager] channel %s channel_id=%s user_id=%s",
            "approved" if created else "approval refreshed", channel_id,
            update.effective_user.id,
        )
        state = "Approved" if created else "Already approved; channel details refreshed"
        await update.effective_message.reply_text(
            f"{state}: <b>{html_escape(name)}</b> (<code>{channel_id}</code>).\n"
            "Connect it with /connect and make Annie an admin with permission to post.",
            parse_mode=ParseMode.HTML,
        )

    async def revoke_channel_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self._owner_command_allowed(update):
            return
        resolved = await self._resolve_channel_argument(update, context, allow_unresolved_id=True)
        if resolved is None:
            return
        channel_id, name, _username = resolved
        repository = self._get_repository()
        if repository is None:
            await update.effective_message.reply_text(
                "Channel approvals need MongoDB. Configure MONGODB_URI first."
            )
            return
        try:
            removed = await asyncio.to_thread(repository.revoke_channel, channel_id)
        except Exception as exc:
            logger.warning(
                "[channel-manager] channel approval revoke failed channel_id=%s user_id=%s error=%s",
                channel_id, update.effective_user.id, type(exc).__name__,
            )
            await update.effective_message.reply_text("I couldn’t remove that approval. Please try again.")
            return
        self.invalidate_access_cache()
        logger.info(
            "[channel-manager] channel approval revoked channel_id=%s user_id=%s removed=%s",
            channel_id, update.effective_user.id, removed,
        )
        if removed:
            await update.effective_message.reply_text(
                f"Channel Manager access removed for <b>{html_escape(name)}</b> (<code>{channel_id}</code>).",
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.effective_message.reply_text("That channel wasn’t approved for Channel Manager.")

    async def list_approved_channels_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self._owner_command_allowed(update):
            return
        if context.args:
            await update.effective_message.reply_text("Usage: /channelapprovals")
            return
        repository = self._get_repository()
        if repository is None:
            await update.effective_message.reply_text(
                "Channel approvals need MongoDB. Configure MONGODB_URI first."
            )
            return
        try:
            approvals = await asyncio.to_thread(repository.list_approvals)
        except Exception as exc:
            logger.warning(
                "[channel-manager] approval list failed user_id=%s error=%s",
                update.effective_user.id, type(exc).__name__,
            )
            await update.effective_message.reply_text("I couldn’t load approved channels. Please try again.")
            return
        if not approvals:
            await update.effective_message.reply_text(
                "No channels are approved yet. Use /channelapprove with a channel ID or public @username."
            )
            return
        lines = [
            f"• <b>{html_escape(str(item.get('name') or 'Telegram channel'))}</b> "
            f"(<code>{int(item['_id'])}</code>)"
            for item in approvals
        ]
        await update.effective_message.reply_text(
            "<b>Approved Channel Manager channels</b>\n" + "\n".join(lines),
            parse_mode=ParseMode.HTML,
        )

    async def _channel_for_action(
        self, channel_id: int, user_id: int, bot: Any
    ) -> dict[str, Any] | None:
        try:
            channels = await self._connected_channels()
            channel = next((item for item in channels if int(item["id"]) == int(channel_id)), None)
            if channel is None:
                return None
            if self.owner_user_id is not None and int(user_id) == self.owner_user_id:
                bot_member = await bot.get_chat_member(int(channel_id), bot.id)
                actor_member = None
            else:
                bot_member, actor_member = await asyncio.gather(
                    bot.get_chat_member(int(channel_id), bot.id),
                    bot.get_chat_member(int(channel_id), int(user_id)),
                )
            if not self._can_post(bot_member):
                return None
            if actor_member is not None:
                if not self._can_post(actor_member):
                    return None
            return channel
        except Exception as exc:
            logger.warning(
                "[channel-manager] permission check failed channel_id=%s user_id=%s error=%s",
                int(channel_id), int(user_id), type(exc).__name__,
            )
            return None

    async def _channel_for_menu(
        self, channel_id: int, user_id: int, bot: Any,
    ) -> tuple[dict[str, Any] | None, Any | None, Any | None]:
        try:
            channels = await self._connected_channels()
            channel = next((item for item in channels if int(item["id"]) == int(channel_id)), None)
            if channel is None:
                return None, None, None
            if self.owner_user_id is not None and int(user_id) == self.owner_user_id:
                bot_member = await bot.get_chat_member(int(channel_id), int(bot.id))
                member = None
            else:
                bot_member, member = await asyncio.gather(
                    bot.get_chat_member(int(channel_id), int(bot.id)),
                    bot.get_chat_member(int(channel_id), int(user_id)),
                )
            if not self._is_chat_admin(bot_member):
                return None, None, None
            if member is None:
                return channel, None, bot_member
            if not self._is_chat_admin(member):
                return None, None, None
            return channel, member, bot_member
        except Exception as exc:
            logger.info("[channel-manager] channel menu access failed channel_id=%s user_id=%s error=%s", channel_id, user_id, type(exc).__name__)
            return None, None, None

    async def _record_for_user(
        self, record_id: str, user_id: int, bot: Any, allow_shared: bool = True
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        repository = self._get_repository()
        record = await asyncio.to_thread(repository.get, record_id) if repository else None
        if not record:
            return None, None
        try:
            channel_id = int(record["channel_id"])
        except (KeyError, TypeError, ValueError):
            logger.warning(
                "[channel-manager] saved post has invalid channel ID record_id=%s",
                record_id,
            )
            return record, None
        channel = await self._channel_for_action(channel_id, user_id, bot)
        if channel is None:
            return record, None
        if record.get("kind") == "post" and record.get("status") == "draft":
            owns = int(record.get("created_by", 0)) == int(user_id)
            if not owns and not (allow_shared and record.get("shared")):
                return record, None
        return record, channel

    @staticmethod
    def _back_button(callback: str, label: str = "← Back") -> list[InlineKeyboardButton]:
        return [InlineKeyboardButton(label, callback_data=callback)]

    @staticmethod
    def _callback_page(parts: list[str], position: int = 3) -> int:
        try:
            return max(0, int(parts[position])) if len(parts) > position else 0
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _record_label(record: dict[str, Any], fallback: str) -> str:
        title = str(record.get("name") or record.get("title") or "").strip()
        if not title:
            text = str(record.get("text") or record.get("caption") or "").strip()
            title = next((line.strip(" *_#") for line in text.splitlines() if line.strip()), "")
        return (title or fallback)[:55]

    @staticmethod
    def _media_from_message(message: Any) -> dict[str, str] | None:
        for kind in ChannelManager.ALLOWED_MEDIA:
            value = getattr(message, kind, None)
            if value:
                file = value[-1] if kind == "photo" else value
                file_id = getattr(file, "file_id", None)
                if file_id:
                    return {"type": kind, "file_id": str(file_id)}
        return None

    @staticmethod
    def _entity_dicts(message: Any, caption: bool = False) -> list[dict[str, Any]]:
        entities = getattr(message, "caption_entities" if caption else "entities", None) or []
        return [entity.to_dict() for entity in entities]

    def _content_from_message(self, message: Any, mode: str) -> dict[str, Any] | None:
        media = self._media_from_message(message)
        if media:
            text = str(message.caption or "")
            entities = self._entity_dicts(message, caption=True) if mode == "telegram" else []
        else:
            text = str(message.text or "")
            entities = self._entity_dicts(message) if mode == "telegram" else []
        source_message = None
        if not text.strip() and not media:
            if not getattr(message, "message_id", None) or not getattr(message, "chat_id", None):
                return None
            # copyMessage preserves Bot API content types without a dedicated send_* method.
            source_message = {
                "chat_id": int(message.chat_id),
                "message_id": int(message.message_id),
            }
        return {
            "text": text if not media else "",
            "caption": text if media else "",
            "entities": entities,
            "format_mode": mode,
            "media": media,
            "source_message": source_message,
            "input_message_id": int(message.message_id),
        }

    @staticmethod
    def _post_keyboard(record: dict[str, Any]) -> InlineKeyboardMarkup | None:
        buttons = record.get("post_buttons") or []
        rows = []
        for item in buttons[:8]:
            label = str(item.get("text") or "Open link")[:40]
            url = str(item.get("url") or "")
            if url.startswith(("https://", "http://")):
                style = str(item.get("style") or "")
                api_kwargs = {"style": style} if style in {"primary", "success", "danger"} else None
                rows.append([InlineKeyboardButton(label, url=url, api_kwargs=api_kwargs)])
        return InlineKeyboardMarkup(rows) if rows else None

    @staticmethod
    def _send_kwargs(record: dict[str, Any], preview: bool = False) -> dict[str, Any]:
        mode = str(record.get("format_mode") or "telegram")
        text = str(record.get("caption") or "") if record.get("media") else str(record.get("text") or "")
        media_type = str((record.get("media") or {}).get("type") or "")
        supports_caption = media_type not in {"sticker", "video_note"}
        kwargs: dict[str, Any] = {}
        if mode == "markdownv2" and (not record.get("media") or (supports_caption and text)):
            kwargs["parse_mode"] = ParseMode.MARKDOWN_V2
        elif mode != "markdownv2" and (not record.get("media") or supports_caption):
            raw_entities = record.get("entities") or []
            if raw_entities:
                kwargs["entities" if not record.get("media") else "caption_entities"] = [
                    MessageEntity.de_json(item, None) for item in raw_entities
                ]
        if record.get("media"):
            media = record["media"]
            kwargs[media["type"] if media["type"] != "animation" else "animation"] = media["file_id"]
            if media["type"] not in {"sticker", "video_note"}:
                kwargs["caption"] = text or None
        else:
            kwargs["text"] = text
        if preview:
            # Put the real post URL buttons first so they sit directly under
            # the content, as they will in the published channel post.
            url_markup = ChannelManager._post_keyboard(record)
            rows = [list(row) for row in url_markup.inline_keyboard] if url_markup else []
            rows.extend([[
                InlineKeyboardButton("Publish", callback_data=f"cm:publish:{record['_id']}"),
                InlineKeyboardButton("Schedule", callback_data=f"cm:schedule:{record['_id']}"),
            ], [
                InlineKeyboardButton("Keep draft", callback_data=f"cm:keep:{record['_id']}"),
                InlineKeyboardButton("Edit", callback_data=f"cm:edit:{record['_id']}"),
            ]])
            if not record.get("source_message") and media_type not in {"sticker", "video_note"}:
                rows.append([InlineKeyboardButton(
                    "Add text / caption", callback_data=f"cm:edit_append:{record['_id']}"
                )])
            rows.append([
                InlineKeyboardButton(
                    f"Buttons ({len(record.get('post_buttons') or [])})",
                    callback_data=f"cm:buttons:{record['_id']}",
                ),
            ])
            rows.append([InlineKeyboardButton(
                "＋ Add content below", callback_data=f"cm:add_followup:{record['_id']}"
            )])
            if record.get("followups"):
                rows.append([InlineKeyboardButton(
                    f"Extra content ({len(record['followups'])})",
                    callback_data=f"cm:followups:{record['_id']}",
                )])
            rows.append([
                InlineKeyboardButton("Discard", callback_data=f"cm:delete:{record['_id']}")
            ])
            kwargs["reply_markup"] = InlineKeyboardMarkup(rows)
        else:
            kwargs["reply_markup"] = ChannelManager._post_keyboard(record)
        return kwargs

    async def _send_content(self, bot: Any, chat_id: int, record: dict[str, Any], preview: bool = False) -> Any:
        source_message = record.get("source_message")
        if source_message:
            kwargs: dict[str, Any] = {
                "chat_id": chat_id,
                "from_chat_id": int(source_message["chat_id"]),
                "message_id": int(source_message["message_id"]),
                "reply_markup": (
                    self._send_kwargs(record, preview=True)["reply_markup"]
                    if preview else self._post_keyboard(record)
                ),
            }
            return await bot.copy_message(**kwargs)
        kwargs = self._send_kwargs(record, preview)
        media = record.get("media")
        if not media:
            return await bot.send_message(chat_id=chat_id, **kwargs)
        method = {
            "photo": bot.send_photo,
            "video": bot.send_video,
            "document": bot.send_document,
            "animation": bot.send_animation,
            "audio": bot.send_audio,
            "sticker": bot.send_sticker,
            "voice": bot.send_voice,
            "video_note": bot.send_video_note,
        }[media["type"]]
        return await method(chat_id=chat_id, **kwargs)

    async def _send_post_messages(
        self, bot: Any, chat_id: int, record: dict[str, Any], preview: bool = False,
        marginal_settings: dict[str, Any] | None = None,
    ) -> list[Any]:
        """Send the main post and any separately appended messages in order."""
        items = [record, *(record.get("followups") or [])]
        sent: list[Any] = []
        allowed_types = set((marginal_settings or {}).get("types") or [])
        main_type = str((record.get("media") or {}).get("type") or "text")
        add_media_margins = marginal_settings is not None and main_type in allowed_types
        header_items = list((marginal_settings or {}).get("header_items") or [])
        footer_items = list((marginal_settings or {}).get("footer_items") or [])
        # Older saved previews contain the former one-item setting shape.
        for field, items in (("header", header_items), ("footer", footer_items)):
            if items:
                continue
            legacy = (marginal_settings or {}).get(field) or {}
            if legacy.get("text"):
                items.append(dict(legacy))
            content = (marginal_settings or {}).get(f"{field}_content") or {}
            if content:
                items.append(dict(content))
            if field == "footer" and not items:
                sticker = (marginal_settings or {}).get("footer_sticker") or {}
                if sticker.get("file_id"):
                    items.append({"media": {"type": "sticker", "file_id": sticker["file_id"]}})
        async def send_margin_items(items: list[dict[str, Any]], field: str) -> None:
            buttons = list((marginal_settings or {}).get(f"{field}_buttons") or [])
            for margin_index, item in enumerate(items):
                margin_record = {
                    **item, "_id": str(record.get("_id")), "followups": [],
                    "post_buttons": buttons if margin_index == len(items) - 1 else [],
                }
                try:
                    sent.append(await self._send_content(bot, chat_id, margin_record))
                except Exception as exc:
                    if sent:
                        ids = [int(value.message_id) for value in sent if getattr(value, "message_id", None)]
                        raise PartialPostSendError(ids, exc) from exc
                    raise
        if add_media_margins:
            await send_margin_items(header_items, "header")
        for index, item in enumerate(items):
            is_last = index == len(items) - 1
            message_record = {**record, **item, "_id": str(record.get("_id")), "followups": []}
            # Channel post URL buttons belong to the main post, even when
            # extra messages follow it. The preview controls stay on the last item.
            if index != 0:
                message_record["post_buttons"] = []
            try:
                sent.append(await self._send_content(
                    bot, chat_id, message_record, preview=preview and is_last
                ))
            except Exception as exc:
                if sent:
                    ids = [int(item.message_id) for item in sent if getattr(item, "message_id", None)]
                    raise PartialPostSendError(ids, exc) from exc
                raise
        if add_media_margins:
            await send_margin_items(footer_items, "footer")
        return sent

    async def _edit_or_send(
        self, update: Update, text: str, markup: InlineKeyboardMarkup | None = None,
        parse_mode: str | None = None, disable_web_page_preview: bool = False,
    ) -> None:
        preview_options = (
            {"disable_web_page_preview": True} if disable_web_page_preview else {}
        )
        query = update.callback_query
        if query and query.message:
            message = query.message
            if any(getattr(message, media_type, None) for media_type in self.ALLOWED_MEDIA):
                await message.reply_text(
                    text, reply_markup=markup, parse_mode=parse_mode, **preview_options
                )
                return
            try:
                await query.edit_message_text(
                    text, reply_markup=markup, parse_mode=parse_mode, **preview_options
                )
                return
            except BadRequest as exc:
                if "message is not modified" in str(exc).casefold():
                    return
                logger.debug("[channel-manager] menu edit failed error=%s", type(exc).__name__)
        message = update.effective_message
        if message:
            await message.reply_text(
                text, reply_markup=markup, parse_mode=parse_mode, **preview_options
            )

    async def _delete_message(self, bot: Any, chat_id: int, message_id: int | None) -> None:
        if not message_id:
            return
        try:
            await bot.delete_message(chat_id=int(chat_id), message_id=int(message_id))
        except Exception as exc:
            logger.info("[channel-manager] message cleanup skipped chat_id=%s message_id=%s error=%s", chat_id, message_id, type(exc).__name__)

    async def _send_clean_menu(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        text: str, markup: InlineKeyboardMarkup | None = None,
        parse_mode: str | None = None,
    ) -> None:
        query = update.callback_query
        if query and query.message:
            await self._delete_message(context.bot, query.message.chat.id, query.message.message_id)
            await context.bot.send_message(
                chat_id=query.message.chat.id, text=text,
                reply_markup=markup, parse_mode=parse_mode,
            )
            return
        await self._edit_or_send(update, text, markup, parse_mode)

    async def _show_home(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        auto_open_single: bool = True,
    ) -> None:
        if not self._is_private(update):
            if update.callback_query:
                await update.callback_query.answer("Open Channel Manager in a private chat with Annie.", show_alert=True)
            return
        user_id = int(update.effective_user.id)
        try:
            channels = await self._eligible_channels(user_id, context.bot)
        except Exception as exc:
            logger.warning("[channel-manager] channels load failed user_id=%s error=%s", user_id, type(exc).__name__)
            await self._edit_or_send(
                update, "I couldn’t load the approved channels. Please try again later.",
                InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data="start_back")]]),
            )
            return
        if not channels:
            text = (
                "No approved channel is ready yet. The bot owner must approve the channel with "
                "/channelapprove, connect it with /connect, and give Annie posting permission."
            )
            await self._edit_or_send(
                update, text,
                InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data="start_back")]]),
            )
            return
        if len(channels) == 1 and auto_open_single:
            await self._show_channel(
                update, context, int(channels[0]["id"]), validated_channel=channels[0]
            )
            return
        rows = [[InlineKeyboardButton(
            str(channel["name"])[:55], callback_data=f"cm:channel:{int(channel['id'])}"
        )] for channel in channels]
        rows.append(self._back_button("start_back"))
        await self._edit_or_send(
            update, "<b>Channel Manager</b>\nChoose a channel:",
            InlineKeyboardMarkup(rows), ParseMode.HTML,
        )

    async def _show_channel(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: int,
        validated_channel: dict[str, Any] | None = None,
    ) -> None:
        if validated_channel is not None:
            channel = {
                key: value for key, value in validated_channel.items()
                if not key.startswith("_")
            }
            actor_member = validated_channel.get("_actor_member")
            bot_member = validated_channel.get("_bot_member")
        else:
            channel, actor_member, bot_member = await self._channel_for_menu(
                channel_id, update.effective_user.id, context.bot
            )
        if channel is None:
            await self._access_denied(update)
            return
        is_bot_owner = self.owner_user_id is not None and int(update.effective_user.id) == self.owner_user_id
        can_manage_posts = self._can_post(bot_member) and (is_bot_owner or self._can_post(actor_member))
        name = html_escape(str(channel.get("name") or "Channel"))
        rows = []
        if can_manage_posts:
            rows = [
                [InlineKeyboardButton("✍️ Create post", callback_data=f"cm:create:{channel_id}")],
                [InlineKeyboardButton("🧩 Header & Footer", callback_data=f"cm:marginals:{channel_id}")],
                [InlineKeyboardButton("📝 Drafts", callback_data=f"cm:drafts:{channel_id}"),
                 InlineKeyboardButton("🕒 Scheduled", callback_data=f"cm:scheduled:{channel_id}")],
                [InlineKeyboardButton("📢 Published posts", callback_data=f"cm:published:{channel_id}:0"),
                 InlineKeyboardButton("📋 Templates", callback_data=f"cm:templates:{channel_id}")],
                [InlineKeyboardButton("📇 Index Manager", callback_data=f"cm:index:{channel_id}"),
                 InlineKeyboardButton("🔁 Cloning", callback_data=f"cm:cloning:{channel_id}")],
            ]
        rows += [
            [InlineKeyboardButton("👥 Channel admins", callback_data=f"cm:admins:{channel_id}")],
            [InlineKeyboardButton("← Channels", callback_data="cm:home")],
        ]
        await self._edit_or_send(
            update,
            f"<b>Channel Manager · {name}</b>\n\n" + (
                "Create and manage posts, or manage this channel’s admins."
                if can_manage_posts else "Manage this channel’s admins."
            ),
            InlineKeyboardMarkup(rows), ParseMode.HTML,
        )

    async def _show_cloning_home(self, update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: int | None = None) -> None:
        repository = self._get_repository()
        if repository is None:
            await self._storage_error(update)
            return
        try:
            channels = await self._eligible_channels(int(update.effective_user.id), context.bot)
            pairs = await asyncio.to_thread(repository.list_clone_pairs, channel_id)
        except Exception as exc:
            logger.warning("[channel-manager] cloning menu failed user_id=%s error=%s", update.effective_user.id, type(exc).__name__)
            await self._edit_or_send(update, "I couldn’t load Cloning. Please try again.", None)
            return
        allowed = {
            int(item["id"]): item for item in channels
            if self._can_post(item.get("_bot_member"))
            and (item.get("_actor_member") is None or self._can_post(item.get("_actor_member")))
        }
        if channel_id is not None and int(channel_id) not in allowed:
            await self._access_denied(update)
            return
        visible = [pair for pair in pairs if int(pair["source_channel_id"]) in allowed and int(pair["destination_channel_id"]) in allowed]
        rows = [[InlineKeyboardButton("📋 Choose channels / clone posts", callback_data=f"cm:clone_start:{int(channel_id or 0)}")]]
        for pair in visible[:20]:
            source_id, destination_id = int(pair["source_channel_id"]), int(pair["destination_channel_id"])
            source_name = str(allowed[source_id].get("name") or pair.get("source_name") or source_id)
            destination_name = str(allowed[destination_id].get("name") or pair.get("destination_name") or destination_id)
            rows.append([InlineKeyboardButton(
                f"{source_name[:22]} → {destination_name[:22]} · Auto {'ON' if pair.get('auto_forward') else 'OFF'}",
                callback_data=f"cm:clone_toggle:{source_id}:{destination_id}:{0 if pair.get('auto_forward') else 1}",
            )])
            rows.append([InlineKeyboardButton("Clone missing posts", callback_data=f"cm:clone_pair:{source_id}:{destination_id}")])
        rows.append([InlineKeyboardButton("← Channel Manager", callback_data=f"cm:channel:{int(channel_id)}" if channel_id else "cm:home")])
        text = (
            "<b>Cloning</b>\nCopy saved posts from one connected channel to another.\n\n"
            "• Clone copies only posts Annie published through Channel Manager.\n"
            "• Auto-forward copies new Annie posts and mirrors Annie’s edits while it is on.\n"
            "• Other admins’ posts aren’t included.\n\n"
            "<b>Commands</b>\n"
            "• <code>/clone</code> — choose channels and copy Annie’s saved posts.\n"
            "• <code>/autoforward on</code> (or <code>yes</code>) — turn automatic backups on.\n"
            "• <code>/autoforward off</code> (or <code>no</code>) — turn automatic backups off."
        )
        if not channels:
            text += "\n\nNo connected channel is currently available to you."
        elif not visible:
            text += "\n\nNo source/destination pairs are saved yet. Choose channels to clone posts or save a pair for Auto-forward."
        await self._edit_or_send(update, text, InlineKeyboardMarkup(rows), ParseMode.HTML)

    async def _clone_source_prompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE, preferred_source: int | None = None) -> None:
        channels = await self._eligible_channels(int(update.effective_user.id), context.bot)
        channels = [item for item in channels if self._can_post(item.get("_bot_member")) and (
            item.get("_actor_member") is None or self._can_post(item.get("_actor_member"))
        )]
        if preferred_source and any(int(item["id"]) == int(preferred_source) for item in channels):
            await self._clone_destination_prompt(update, context, int(preferred_source))
            return
        rows = [[InlineKeyboardButton(str(item.get("name") or item["id"])[:55], callback_data=f"cm:clone_source:{int(item['id'])}")] for item in channels]
        rows.append([InlineKeyboardButton("Cancel", callback_data="cm:cloning")])
        text = "<b>Choose the source channel</b>\nAnnie copies only posts she recorded in Channel Manager."
        if not channels:
            text += "\n\nNo connected channel is available where you and Annie can post."
        await self._edit_or_send(update, text, InlineKeyboardMarkup(rows), ParseMode.HTML)

    async def _clone_destination_prompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE, source_channel_id: int) -> None:
        source = await self._channel_for_menu(source_channel_id, int(update.effective_user.id), context.bot)
        if source[0] is None:
            await self._access_denied(update)
            return
        channels = await self._eligible_channels(int(update.effective_user.id), context.bot)
        destinations = [item for item in channels if int(item["id"]) != int(source_channel_id)
                        and self._can_post(item.get("_bot_member"))
                        and (item.get("_actor_member") is None or self._can_post(item.get("_actor_member")))]
        rows = [[InlineKeyboardButton(str(item.get("name") or item["id"])[:55], callback_data=f"cm:clone_dest:{source_channel_id}:{int(item['id'])}")] for item in destinations]
        rows.append([InlineKeyboardButton("← Choose source", callback_data="cm:clone_start:0")])
        text = "<b>Choose the destination channel</b>\nAnnie needs permission to post in both channels."
        if not destinations:
            text += "\n\nNo other connected channel is available for this pair."
        await self._edit_or_send(update, text, InlineKeyboardMarkup(rows), ParseMode.HTML)

    async def _clone_confirm_prompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE, source_channel_id: int, destination_channel_id: int) -> None:
        source = await self._channel_for_action(source_channel_id, int(update.effective_user.id), context.bot)
        destination = await self._channel_for_action(destination_channel_id, int(update.effective_user.id), context.bot)
        if source is None or destination is None or source_channel_id == destination_channel_id:
            await self._access_denied(update)
            return
        repository = self._get_repository()
        try:
            records = await asyncio.to_thread(repository.list_published_records_for_clone, source_channel_id) if repository else []
            rows = []
            if records:
                rows.append([InlineKeyboardButton(f"Clone {len(records)} saved post{'s' if len(records) != 1 else ''}", callback_data=f"cm:clone_confirm:{source_channel_id}:{destination_channel_id}")])
            rows.extend([
                [InlineKeyboardButton("Save pair for Auto-forward", callback_data=f"cm:clone_save:{source_channel_id}:{destination_channel_id}")],
                [InlineKeyboardButton("Cancel", callback_data="cm:cloning")],
            ])
            text = (
                "<b>Confirm channel pair</b>\n"
                f"Source: {html_escape(str(source.get('name') or source_channel_id))}\n"
                f"Destination: {html_escape(str(destination.get('name') or destination_channel_id))}\n\n"
                f"Annie found {len(records)} saved published post{'s' if len(records) != 1 else ''}. "
                "Only posts Annie recorded in Channel Manager can be cloned."
            )
            await self._edit_or_send(update, text, InlineKeyboardMarkup(rows), ParseMode.HTML)
        except Exception as exc:
            logger.warning("[channel-manager] clone confirmation failed source=%s destination=%s error=%s", source_channel_id, destination_channel_id, type(exc).__name__)
            await self._edit_or_send(update, "I couldn’t check the saved posts. Please try again.", None)

    async def clone_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_private(update):
            await update.effective_message.reply_text("Use /clone in a private chat with Annie.")
            return
        await self._clone_source_prompt(update, context)

    async def autoforward_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_private(update):
            await update.effective_message.reply_text("Use /autoforward in a private chat with Annie.")
            return
        if len(context.args) != 1 or context.args[0].casefold() not in {"yes", "no", "on", "off"}:
            await update.effective_message.reply_text("Choose a pair in Cloning, then use /autoforward on or /autoforward off. You can also use yes/no.")
            return
        enabled = context.args[0].casefold() in {"yes", "on"}
        repository = self._get_repository()
        try:
            pairs = await asyncio.to_thread(repository.list_clone_pairs) if repository else []
            eligible = []
            for pair in pairs:
                source_id, destination_id = int(pair["source_channel_id"]), int(pair["destination_channel_id"])
                source, destination = await asyncio.gather(
                    self._channel_for_menu(source_id, int(update.effective_user.id), context.bot),
                    self._channel_for_menu(destination_id, int(update.effective_user.id), context.bot),
                )
                if source[0] and destination[0]:
                    eligible.append(pair)
        except Exception as exc:
            logger.warning("[channel-manager] autoforward command failed user_id=%s error=%s", update.effective_user.id, type(exc).__name__)
            await update.effective_message.reply_text("I couldn’t load your channel pairs. Please try again.")
            return
        if not eligible:
            await update.effective_message.reply_text("Set a source and destination first in Cloning, then run this command again.")
            return
        if len(eligible) == 1:
            pair = eligible[0]
            await self._set_auto_forward(update, context, int(pair["source_channel_id"]), int(pair["destination_channel_id"]), enabled)
            return
        rows = [[InlineKeyboardButton(
            f"{str(pair.get('source_name') or pair['source_channel_id'])[:22]} → {str(pair.get('destination_name') or pair['destination_channel_id'])[:22]}",
            callback_data=f"cm:autofwd_set:{int(pair['source_channel_id'])}:{int(pair['destination_channel_id'])}:{int(enabled)}",
        )] for pair in eligible[:20]]
        rows.append([InlineKeyboardButton("Cancel", callback_data="cm:cloning")])
        await self._edit_or_send(update, f"Which pair should Auto-forward be {'turned on' if enabled else 'turned off'} for?", InlineKeyboardMarkup(rows))

    async def _set_auto_forward(self, update: Update, context: ContextTypes.DEFAULT_TYPE, source_channel_id: int, destination_channel_id: int, enabled: bool) -> None:
        if (await self._channel_for_action(source_channel_id, int(update.effective_user.id), context.bot) is None
                or await self._channel_for_action(destination_channel_id, int(update.effective_user.id), context.bot) is None):
            await self._access_denied(update)
            return
        repository = self._get_repository()
        try:
            changed = await asyncio.to_thread(repository.set_auto_forward, source_channel_id, destination_channel_id, enabled) if repository else False
        except Exception as exc:
            logger.warning("[channel-manager] autoforward setting failed source=%s destination=%s error=%s", source_channel_id, destination_channel_id, type(exc).__name__)
            changed = False
        if not changed:
            await self._edit_or_send(update, "Save this source and destination pair in Cloning first.", None)
            return
        logger.info("[channel-manager] autoforward %s source=%s destination=%s user_id=%s", "enabled" if enabled else "disabled", source_channel_id, destination_channel_id, update.effective_user.id)
        await self._show_cloning_home(update, context)

    async def _save_clone_pair(self, update: Update, context: ContextTypes.DEFAULT_TYPE, source_channel_id: int, destination_channel_id: int) -> None:
        source = await self._channel_for_action(source_channel_id, int(update.effective_user.id), context.bot)
        destination = await self._channel_for_action(destination_channel_id, int(update.effective_user.id), context.bot)
        repository = self._get_repository()
        if source is None or destination is None or repository is None or source_channel_id == destination_channel_id:
            await self._access_denied(update)
            return
        try:
            await asyncio.to_thread(
                repository.save_clone_pair, source_channel_id, destination_channel_id,
                str(source.get("name") or source_channel_id),
                str(destination.get("name") or destination_channel_id), int(update.effective_user.id),
            )
            logger.info("[channel-manager] clone pair saved source=%s destination=%s user_id=%s", source_channel_id, destination_channel_id, update.effective_user.id)
            await self._show_cloning_home(update, context, source_channel_id)
        except Exception as exc:
            logger.exception("[channel-manager] clone pair save failed source=%s destination=%s error=%s", source_channel_id, destination_channel_id, type(exc).__name__)
            await self._edit_or_send(update, "I couldn’t save this channel pair. Please try again.", None)

    @staticmethod
    def _clone_message_ids(record: dict[str, Any]) -> list[int]:
        values = record.get("published_message_ids") or []
        if not values and record.get("published_message_id"):
            values = [record["published_message_id"]]
        ids = []
        for value in values:
            try:
                message_id = int(value)
            except (TypeError, ValueError):
                continue
            if message_id > 0 and message_id not in ids:
                ids.append(message_id)
        return ids

    def _clone_markup_for_message(self, record: dict[str, Any], message_id: int) -> InlineKeyboardMarkup | None:
        components = self._published_components(record)
        for component in components:
            if int(component.get("message_id") or 0) != int(message_id):
                continue
            kind = component.get("kind")
            if kind == "main":
                return self._post_keyboard(record)
            settings = record.get("marginal_snapshot") or {}
            field = "header" if kind == "header" else "footer" if kind == "footer" else None
            if field:
                field_components = [item for item in components if item.get("kind") == kind]
                last_index = max((int(item.get("source_index", -1)) for item in field_components), default=-1)
                if int(component.get("source_index", -1)) == last_index:
                    return self._post_keyboard({"post_buttons": settings.get(f"{field}_buttons") or []})
            return None
        return None

    async def _copy_message_once(
        self, bot: Any, repository: MongoChannelManagementRepository,
        source_channel_id: int, destination_channel_id: int, source_message_id: int,
        markup: InlineKeyboardMarkup | None, record_id: str | None = None,
    ) -> int | None:
        pair_id = repository.clone_pair_id(source_channel_id, destination_channel_id)
        lock = self._clone_pair_locks.setdefault(pair_id, asyncio.Lock())
        async with lock:
            existing = await asyncio.to_thread(
                repository.get_clone_message, source_channel_id,
                destination_channel_id, source_message_id,
            )
            if existing:
                return None
            try:
                copied = await bot.copy_message(
                    chat_id=destination_channel_id, from_chat_id=source_channel_id,
                    message_id=source_message_id, reply_markup=markup,
                )
            except RetryAfter as exc:
                await asyncio.sleep(max(1, int(getattr(exc, "retry_after", 1))))
                copied = await bot.copy_message(
                    chat_id=destination_channel_id, from_chat_id=source_channel_id,
                    message_id=source_message_id, reply_markup=markup,
                )
            destination_message_id = int(copied.message_id)
            await asyncio.to_thread(
                repository.save_clone_message, source_channel_id,
                destination_channel_id, source_message_id,
                destination_message_id, record_id,
            )
            return destination_message_id

    async def _clone_channel_records(self, update: Update, context: ContextTypes.DEFAULT_TYPE, source_channel_id: int, destination_channel_id: int) -> None:
        source = await self._channel_for_action(source_channel_id, int(update.effective_user.id), context.bot)
        destination = await self._channel_for_action(destination_channel_id, int(update.effective_user.id), context.bot)
        repository = self._get_repository()
        if source is None or destination is None or repository is None or source_channel_id == destination_channel_id:
            await self._access_denied(update)
            return
        try:
            await asyncio.to_thread(
                repository.save_clone_pair, source_channel_id, destination_channel_id,
                str(source.get("name") or source_channel_id),
                str(destination.get("name") or destination_channel_id), int(update.effective_user.id),
            )
            records = await asyncio.to_thread(repository.list_published_records_for_clone, source_channel_id)
        except Exception as exc:
            logger.exception("[channel-manager] clone load failed source=%s destination=%s error=%s", source_channel_id, destination_channel_id, type(exc).__name__)
            await self._edit_or_send(update, "I couldn’t load Annie’s saved posts. No posts were copied.", None)
            return
        all_source_ids = [
            source_message_id
            for record in records for source_message_id in self._clone_message_ids(record)
        ]
        total = len(all_source_ids)
        mapped_ids: set[int] = set()
        try:
            for start in range(0, len(all_source_ids), 500):
                mapped_ids.update(await asyncio.to_thread(
                    repository.list_cloned_source_ids, source_channel_id,
                    destination_channel_id, all_source_ids[start:start + 500],
                ))
        except Exception as exc:
            logger.warning("[channel-manager] clone mapping lookup failed source=%s destination=%s error=%s", source_channel_id, destination_channel_id, type(exc).__name__)
            await self._edit_or_send(update, "I couldn’t check which posts are already copied. Nothing was copied; please try again.", None)
            return
        await self._edit_or_send(update, f"Cloning Annie’s saved posts… 0 of {total} messages copied.", None)
        copied = skipped = already_copied = processed = 0
        for record in records:
            record_id = str(record.get("_id") or "")
            for source_message_id in self._clone_message_ids(record):
                try:
                    if source_message_id in mapped_ids:
                        already_copied += 1
                        processed += 1
                        continue
                    destination_message_id = await self._copy_message_once(
                        context.bot, repository, source_channel_id, destination_channel_id,
                        source_message_id, self._clone_markup_for_message(record, source_message_id),
                        record_id,
                    )
                    if destination_message_id is None:
                        already_copied += 1
                    else:
                        copied += 1
                except Exception as exc:
                    skipped += 1
                    logger.warning("[channel-manager] clone message skipped source=%s destination=%s message_id=%s error=%s", source_channel_id, destination_channel_id, source_message_id, type(exc).__name__)
                processed += 1
                if processed % 25 == 0:
                    await self._edit_or_send(update, f"Cloning Annie’s saved posts… {copied} of {total} messages copied.", None)
        logger.info("[channel-manager] clone complete source=%s destination=%s copied=%s existing=%s skipped=%s user_id=%s", source_channel_id, destination_channel_id, copied, already_copied, skipped, update.effective_user.id)
        result_text = f"Clone finished. {copied} messages copied."
        if already_copied:
            result_text += f" {already_copied} were already there."
        if skipped:
            result_text += f" {skipped} couldn’t be copied; check Annie’s channel access and the log."
        await self._edit_or_send(
            update, result_text,
            InlineKeyboardMarkup([[InlineKeyboardButton("← Cloning", callback_data=f"cm:cloning:{source_channel_id}")]]),
        )

    async def _auto_forward_published(self, bot: Any, source_channel_id: int, sent_messages: list[Any], record: dict[str, Any]) -> int:
        repository = self._get_repository()
        if repository is None:
            return 0
        try:
            pairs = await asyncio.to_thread(repository.list_clone_pairs, source_channel_id)
        except Exception as exc:
            logger.warning("[channel-manager] autoforward config load failed source=%s error=%s", source_channel_id, type(exc).__name__)
            return 1
        source_ids = [int(message.message_id) for message in sent_messages if getattr(message, "message_id", None)]
        failed = 0
        for pair in pairs:
            if not pair.get("auto_forward"):
                continue
            destination_id = int(pair["destination_channel_id"])
            for source_message_id in source_ids:
                try:
                    markup = self._clone_markup_for_message(
                        {**record, "published_message_ids": source_ids}, source_message_id
                    )
                    await self._copy_message_once(
                        bot, repository, source_channel_id, destination_id,
                        source_message_id, markup, str(record.get("_id") or ""),
                    )
                except Exception as exc:
                    failed += 1
                    logger.warning("[channel-manager] autoforward message failed source=%s destination=%s message_id=%s error=%s", source_channel_id, destination_id, source_message_id, type(exc).__name__)
        return failed

    async def _mirror_edit_to_backups(
        self, bot: Any, source_channel_id: int, source_message_id: int,
        updated_record: dict[str, Any], *, reply_markup: Any = None,
        preserve_reply_markup: bool = False,
    ) -> None:
        repository = self._get_repository()
        if repository is None:
            return
        try:
            pairs = await asyncio.to_thread(repository.list_clone_pairs, source_channel_id)
        except Exception as exc:
            logger.warning("[channel-manager] backup edit config load failed source=%s message_id=%s error=%s", source_channel_id, source_message_id, type(exc).__name__)
            return
        media = updated_record.get("media") or {}
        media_type = str(media.get("type") or "")
        try:
            send_kwargs = self._send_kwargs(updated_record)
            if media:
                media_classes = {"photo": InputMediaPhoto, "video": InputMediaVideo, "animation": InputMediaAnimation,
                                 "document": InputMediaDocument, "audio": InputMediaAudio}
                media_class = media_classes.get(media_type)
                if media_class is None:
                    return
                format_kwargs = {key: send_kwargs[key] for key in ("parse_mode", "caption_entities") if key in send_kwargs}
                payload = media_class(media=str(media.get("file_id") or ""), caption=send_kwargs.get("caption"), **format_kwargs)
            else:
                payload = None
        except Exception as exc:
            logger.warning("[channel-manager] backup edit payload failed source=%s message_id=%s error=%s", source_channel_id, source_message_id, type(exc).__name__)
            return
        for pair in pairs:
            if not pair.get("auto_forward"):
                continue
            destination_id = int(pair["destination_channel_id"])
            try:
                mapping = await asyncio.to_thread(repository.get_clone_message, source_channel_id, destination_id, source_message_id)
                if not mapping:
                    continue
                backup_id = int(mapping["destination_message_id"])
                options = {} if preserve_reply_markup else {"reply_markup": reply_markup}
                if media:
                    await bot.edit_message_media(chat_id=destination_id, message_id=backup_id, media=payload, **options)
                elif updated_record.get("source_message"):
                    continue
                elif send_kwargs.get("text") is not None:
                    text_options = {key: send_kwargs[key] for key in ("parse_mode", "entities") if key in send_kwargs}
                    await bot.edit_message_text(chat_id=destination_id, message_id=backup_id, text=send_kwargs.get("text") or "", **text_options, **options)
                else:
                    caption_options = {key: send_kwargs[key] for key in ("parse_mode", "caption_entities") if key in send_kwargs}
                    await bot.edit_message_caption(chat_id=destination_id, message_id=backup_id, caption=send_kwargs.get("caption") or "", **caption_options, **options)
            except BadRequest as exc:
                if "message is not modified" not in str(exc).casefold():
                    logger.warning("[channel-manager] backup edit failed source=%s destination=%s message_id=%s error=%s", source_channel_id, destination_id, source_message_id, type(exc).__name__)
            except Exception as exc:
                logger.warning("[channel-manager] backup edit failed source=%s destination=%s message_id=%s error=%s", source_channel_id, destination_id, source_message_id, type(exc).__name__)

    async def _delete_from_backups(self, bot: Any, source_channel_id: int, source_message_ids: list[int]) -> None:
        repository = self._get_repository()
        if repository is None:
            return
        try:
            pairs = await asyncio.to_thread(repository.list_clone_pairs, source_channel_id)
        except Exception as exc:
            logger.warning("[channel-manager] backup delete config load failed source=%s error=%s", source_channel_id, type(exc).__name__)
            return
        for pair in pairs:
            if not pair.get("auto_forward"):
                continue
            destination_id = int(pair["destination_channel_id"])
            for source_message_id in source_message_ids:
                try:
                    mapping = await asyncio.to_thread(repository.get_clone_message, source_channel_id, destination_id, source_message_id)
                    if not mapping:
                        continue
                    await bot.delete_message(chat_id=destination_id, message_id=int(mapping["destination_message_id"]))
                    await asyncio.to_thread(repository.delete_clone_message, source_channel_id, destination_id, source_message_id)
                except Exception as exc:
                    logger.warning("[channel-manager] backup delete failed source=%s destination=%s message_id=%s error=%s", source_channel_id, destination_id, source_message_id, type(exc).__name__)

    @staticmethod
    def _chat_member_status(member: Any) -> str:
        status = getattr(member, "status", "")
        return str(getattr(status, "value", status))

    async def _show_channel_admins(self, update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: int) -> None:
        channel, _, _ = await self._channel_for_menu(channel_id, update.effective_user.id, context.bot)
        if channel is None:
            await self._access_denied(update)
            return
        try:
            admins = await context.bot.get_chat_administrators(channel_id)
            admins = sorted(
                admins,
                key=lambda member: self._chat_member_status(member) != "creator",
            )
            name = html_escape(str(channel.get("name") or "Channel"))
            lines = [
                "👥 <b>Channel Admins</b>",
                f"Channel: <b>{name}</b>",
                f"Total admins: {len(admins)}",
                "",
            ]
            entries = []
            for member in admins:
                user = member.user
                display_name = html_escape(" ".join(filter(None, [user.first_name, user.last_name])) or "Telegram user")
                username = (
                    f' (<a href="https://t.me/{html_escape(user.username)}">@{html_escape(user.username)}</a>)'
                    if user.username else ""
                )
                role = "Owner" if self._chat_member_status(member) == "creator" else "Admin"
                role_line = "👑 Owner" if role == "Owner" else "✅ Admin"
                entries.append(
                    f"{len(entries) + 1}. <b>{display_name}</b>{username}\n"
                    f"   {role_line}\n"
                    f"   ID: <code>{int(user.id)}</code>\n"
                )
            visible = []
            current_length = len("\n".join(lines))
            for entry in entries:
                if current_length + len(entry) > 2850:
                    break
                visible.append(entry)
                current_length += len(entry)
            lines.extend(visible)
            if len(entries) > len(visible):
                lines.append(f"… and {len(entries) - len(visible)} more.")
            if not admins:
                lines.append("No channel admins were found.")
            body = "\n".join(lines).rstrip()
        except Exception as exc:
            logger.warning("[channel-manager] admin list failed channel_id=%s user_id=%s error=%s", channel_id, update.effective_user.id, type(exc).__name__)
            body = "I couldn’t load the admin list. Check Annie’s channel access and try again."
        rows = [
            [InlineKeyboardButton("↻ Refresh list", callback_data=f"cm:admins:{channel_id}")],
            [InlineKeyboardButton("← Channel Manager", callback_data=f"cm:channel:{channel_id}")],
        ]
        await self._edit_or_send(
            update,
            f"{body}\n\n"
            "<b>Admin commands</b> (send in a private chat with Annie)\n"
            "• <code>/promote @user</code> or <code>/promote user_id</code> — allow posting only.\n"
            "• <code>/fullpromote @user</code> or <code>/fullpromote user_id</code> — give all rights Annie can grant.\n"
            "• <code>/demote @user</code> or <code>/demote user_id</code> — remove admin rights.\n"
            "The user must already be a member. Annie checks before changing their role.\n"
            f"For multiple channels: <code>/promote {channel_id} @user</code>. Use the same format for the other commands.",
            InlineKeyboardMarkup(rows), ParseMode.HTML,
            disable_web_page_preview=True,
        )

    async def _begin_admin_change(self, update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: int, operation: str) -> None:
        if operation not in {"promote", "fullpromote", "demote"}:
            await self._access_denied(update)
            return
        channel, _, _ = await self._channel_for_menu(channel_id, update.effective_user.id, context.bot)
        if channel is None:
            await self._access_denied(update)
            return
        try:
            actor = await context.bot.get_chat_member(channel_id, int(update.effective_user.id))
            bot_member = await context.bot.get_chat_member(channel_id, int(context.bot.id))
            actor_is_owner = self._chat_member_status(actor) == "creator"
            if not actor_is_owner and not bool(getattr(actor, "can_promote_members", False)):
                await self._edit_or_send(update, "Telegram only lets the channel owner or an admin with permission to add admins change admin roles.", InlineKeyboardMarkup([[InlineKeyboardButton("← Admins", callback_data=f"cm:admins:{channel_id}")]]))
                return
            if not bool(getattr(bot_member, "can_promote_members", False)):
                await self._edit_or_send(update, "Annie needs Telegram’s ‘Add new admins’ permission in this channel first.", InlineKeyboardMarkup([[InlineKeyboardButton("← Admins", callback_data=f"cm:admins:{channel_id}")]]))
                return
        except Exception as exc:
            logger.warning("[channel-manager] admin permissions check failed channel_id=%s user_id=%s error=%s", channel_id, update.effective_user.id, type(exc).__name__)
            await self._edit_or_send(update, "I couldn’t check the permissions needed to change admins.", InlineKeyboardMarkup([[InlineKeyboardButton("← Admins", callback_data=f"cm:admins:{channel_id}")]]))
            return
        await self._edit_or_send(
            update,
            "Use /promote, /fullpromote, or /demote in a private chat with Annie. Send the member’s @username or numeric user ID.",
            InlineKeyboardMarkup([[InlineKeyboardButton("← Admins", callback_data=f"cm:admins:{channel_id}")]]),
        )

    async def admin_change_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, operation: str,
    ) -> None:
        message = update.effective_message
        if not self._is_private(update) or message is None or update.effective_user is None:
            if message:
                await message.reply_text("Use this command in a private chat with Annie.")
            return
        if operation not in {"promote", "fullpromote", "demote"}:
            await message.reply_text("That admin command isn’t available.")
            return
        args = list(context.args or [])
        if len(args) not in {1, 2}:
            await message.reply_text(
                f"Usage: /{operation} <@username or user ID>\n"
                f"For a specific channel: /{operation} <channel ID> <@username or user ID>"
            )
            return

        try:
            channels = await self._connected_channels()
        except Exception as exc:
            logger.warning(
                "[channel-manager] admin command channel lookup failed user_id=%s error=%s",
                update.effective_user.id, type(exc).__name__,
            )
            await message.reply_text("I couldn’t load connected channels. Please try again.")
            return

        channel_id: int | None = None
        target_arg = args[-1]
        if len(args) == 2:
            raw_channel = args[0]
            try:
                channel_id = int(raw_channel)
            except ValueError:
                if not raw_channel.startswith("@"):
                    await message.reply_text("Use a channel ID before the member, or use a member’s @username or user ID.")
                    return
                try:
                    chat = await context.bot.get_chat(raw_channel)
                    channel_id = int(chat.id)
                except Exception as exc:
                    logger.info(
                        "[channel-manager] admin command channel resolution failed user_id=%s error=%s",
                        update.effective_user.id, type(exc).__name__,
                    )
                    await message.reply_text("I couldn’t find that channel. Check its public @username.")
                    return
            if not any(int(channel["id"]) == channel_id for channel in channels):
                await message.reply_text("That channel isn’t connected and approved for Channel Manager.")
                return
        else:
            manageable: list[int] = []
            for channel in channels:
                candidate_id = int(channel["id"])
                if (await self._channel_for_menu(candidate_id, int(update.effective_user.id), context.bot))[0] is None:
                    continue
                try:
                    actor = await context.bot.get_chat_member(candidate_id, int(update.effective_user.id))
                    if self.owner_user_id == int(update.effective_user.id) or bool(
                        getattr(actor, "can_promote_members", False)
                    ):
                        manageable.append(candidate_id)
                except Exception as exc:
                    logger.info(
                        "[channel-manager] admin command permission lookup failed channel_id=%s user_id=%s error=%s",
                        candidate_id, update.effective_user.id, type(exc).__name__,
                    )
            if len(manageable) != 1:
                if not manageable:
                    await message.reply_text("You don’t have admin-management access to a connected channel.")
                else:
                    await message.reply_text(
                        "Choose a channel by adding its ID. Example:\n"
                        f"/{operation} {manageable[0]} {target_arg}"
                    )
                return
            channel_id = manageable[0]

        try:
            actor = await context.bot.get_chat_member(channel_id, int(update.effective_user.id))
            bot_member = await context.bot.get_chat_member(channel_id, int(context.bot.id))
            actor_can_promote = (
                self._chat_member_status(actor) == "creator"
                or bool(getattr(actor, "can_promote_members", False))
            )
            if not actor_can_promote:
                await message.reply_text("You need the channel owner’s permission to add or remove admins.")
                return
            if not bool(getattr(bot_member, "can_promote_members", False)):
                await message.reply_text("Annie needs the ‘Add new admins’ permission in that channel.")
                return
        except Exception as exc:
            logger.info(
                "[channel-manager] admin command access check failed channel_id=%s user_id=%s error=%s",
                channel_id, update.effective_user.id, type(exc).__name__,
            )
            await message.reply_text("I couldn’t check your admin permissions. Please try again.")
            return

        if target_arg.startswith("@"):
            if self.resolve_user_username is None:
                await message.reply_text("I can’t look up @usernames right now. Send the user’s numeric ID instead.")
                return
            try:
                resolved = await self.resolve_user_username(target_arg.lstrip("@"))
                target_id = int(resolved[0]) if resolved and resolved[0] else None
            except Exception as exc:
                logger.info(
                    "[channel-manager] admin target username lookup failed channel_id=%s user_id=%s error=%s",
                    channel_id, update.effective_user.id, type(exc).__name__,
                )
                target_id = None
            if target_id is None:
                await message.reply_text("I couldn’t find that Telegram user. Check the @username or send their numeric ID.")
                return
        elif re.fullmatch(r"\d+", target_arg):
            target_id = int(target_arg)
        else:
            await message.reply_text("Send a member’s @username or numeric Telegram user ID.")
            return

        assert channel_id is not None
        logger.info(
            "[channel-manager] admin command requested channel_id=%s actor_id=%s target_id=%s action=%s",
            channel_id, update.effective_user.id, target_id, operation,
        )
        await self._confirm_admin_change(update, context, channel_id, target_id, operation)

    async def promote_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.admin_change_command(update, context, "promote")

    async def fullpromote_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.admin_change_command(update, context, "fullpromote")

    async def demote_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.admin_change_command(update, context, "demote")

    async def _confirm_admin_change(self, update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: int, user_id: int, operation: str) -> None:
        if operation not in {"promote", "fullpromote", "demote"} or (await self._channel_for_menu(channel_id, update.effective_user.id, context.bot))[0] is None:
            await self._access_denied(update)
            return
        try:
            actor = await context.bot.get_chat_member(channel_id, int(update.effective_user.id))
            bot_member = await context.bot.get_chat_member(channel_id, int(context.bot.id))
            if self._chat_member_status(actor) != "creator" and not bool(getattr(actor, "can_promote_members", False)):
                await self._edit_or_send(update, "You no longer have permission to change channel admins.", None)
                return
            if not bool(getattr(bot_member, "can_promote_members", False)):
                await self._edit_or_send(update, "Annie no longer has Telegram’s ‘Add new admins’ permission.", None)
                return
            target = await context.bot.get_chat_member(channel_id, user_id)
            target_status = self._chat_member_status(target)
            if target_status in {"left", "kicked"}:
                await self._edit_or_send(update, "That person must join the channel before their role can be changed.", InlineKeyboardMarkup([[InlineKeyboardButton("← Admins", callback_data=f"cm:admins:{channel_id}")]]))
                return
            if operation in {"promote", "fullpromote"} and target_status in {"creator", "administrator"}:
                await self._edit_or_send(update, "That person is already a channel admin.", InlineKeyboardMarkup([[InlineKeyboardButton("← Admins", callback_data=f"cm:admins:{channel_id}")]]))
                return
            if operation == "demote" and target_status == "creator":
                await self._edit_or_send(update, "The channel owner can’t be demoted.", InlineKeyboardMarkup([[InlineKeyboardButton("← Admins", callback_data=f"cm:admins:{channel_id}")]]))
                return
            name = html_escape(" ".join(filter(None, [target.user.first_name, target.user.last_name])) or "Telegram user")
            verb = "Promote" if operation == "promote" else "Demote"
            change_note = ""
            if operation in {"promote", "fullpromote"}:
                if operation == "promote":
                    change_note = "\n\nThey will only be able to publish posts. They won’t be able to edit or delete posts, manage members, or promote admins."
                    verb = "Promote with posting rights"
                else:
                    change_note = "\n\nThey will receive all admin rights that you and Annie can grant, including the ability to promote other admins."
                    verb = "Fully promote"
            await self._edit_or_send(
                update, f"<b>{verb} {name}?</b>\n\nConfirm to change their channel role.{change_note}",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("Confirm", callback_data=f"cm:admin_apply:{operation}:{channel_id}:{user_id}")],
                    [InlineKeyboardButton("Cancel", callback_data=f"cm:admins:{channel_id}")],
                ]), ParseMode.HTML,
            )
        except Exception as exc:
            logger.warning("[channel-manager] target admin check failed channel_id=%s user_id=%s target_id=%s error=%s", channel_id, update.effective_user.id, user_id, type(exc).__name__)
            await self._edit_or_send(update, "I couldn’t find that member. Check the user ID and confirm they have joined the channel.", InlineKeyboardMarkup([[InlineKeyboardButton("← Admins", callback_data=f"cm:admins:{channel_id}")]]))

    async def _apply_admin_change(self, update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: int, user_id: int, operation: str) -> None:
        if (await self._channel_for_menu(channel_id, update.effective_user.id, context.bot))[0] is None:
            await self._access_denied(update)
            return
        try:
            actor = await context.bot.get_chat_member(channel_id, int(update.effective_user.id))
            bot_member = await context.bot.get_chat_member(channel_id, int(context.bot.id))
            if self._chat_member_status(actor) != "creator" and not bool(getattr(actor, "can_promote_members", False)):
                await self._edit_or_send(update, "You no longer have permission to change channel admins.", None)
                return
            if not bool(getattr(bot_member, "can_promote_members", False)):
                await self._edit_or_send(update, "Annie no longer has Telegram’s ‘Add new admins’ permission.", None)
                return
            if operation in {"promote", "fullpromote"}:
                grantable = [
                    "can_change_info", "can_post_messages", "can_edit_messages",
                    "can_delete_messages", "can_invite_users", "can_restrict_members",
                    "can_pin_messages", "can_promote_members", "is_anonymous",
                    "can_manage_chat", "can_manage_video_chats", "can_manage_topics",
                    "can_post_stories", "can_edit_stories", "can_delete_stories",
                ]
                actor_member = await context.bot.get_chat_member(channel_id, int(update.effective_user.id))
                if self.owner_user_id == int(update.effective_user.id):
                    actor_rights = {name: True for name in grantable}
                else:
                    actor_rights = {name: bool(getattr(actor_member, name, False)) for name in grantable}
                if operation == "promote":
                    if not bool(getattr(bot_member, "can_post_messages", False)) or not actor_rights["can_post_messages"]:
                        await self._edit_or_send(update, "Annie and you must both have permission to post in this channel before using /promote.", InlineKeyboardMarkup([[InlineKeyboardButton("← Admins", callback_data=f"cm:admins:{channel_id}")]]))
                        return
                    rights = {name: False for name in grantable}
                    rights["can_post_messages"] = True
                else:
                    rights = {
                        name: bool(getattr(bot_member, name, False)) and actor_rights[name]
                        for name in grantable
                    }
                    if not any(rights.values()):
                        await self._edit_or_send(update, "You and Annie don’t have any admin rights that can be granted.", InlineKeyboardMarkup([[InlineKeyboardButton("← Admins", callback_data=f"cm:admins:{channel_id}")]]))
                        return
                target = await context.bot.get_chat_member(channel_id, user_id)
                if self._chat_member_status(target) in {"creator", "administrator"}:
                    await self._edit_or_send(update, "That person is already a channel admin.", InlineKeyboardMarkup([[InlineKeyboardButton("← Admins", callback_data=f"cm:admins:{channel_id}")]]))
                    return
                await context.bot.promote_chat_member(channel_id, user_id, **rights)
                result = "Member promoted with posting rights only." if operation == "promote" else "Member promoted with all admin rights you and Annie can grant."
            elif operation == "demote":
                target = await context.bot.get_chat_member(channel_id, user_id)
                if self._chat_member_status(target) == "creator":
                    await self._edit_or_send(update, "The channel owner can’t be demoted.", InlineKeyboardMarkup([[InlineKeyboardButton("← Admins", callback_data=f"cm:admins:{channel_id}")]]))
                    return
                if self._chat_member_status(target) != "administrator":
                    await self._edit_or_send(update, "That person is no longer a channel admin.", InlineKeyboardMarkup([[InlineKeyboardButton("← Admins", callback_data=f"cm:admins:{channel_id}")]]))
                    return
                rights = {
                    name: False for name in [
                        "can_change_info", "can_post_messages", "can_edit_messages",
                        "can_delete_messages", "can_invite_users", "can_restrict_members",
                        "can_pin_messages", "can_promote_members", "is_anonymous",
                        "can_manage_chat", "can_manage_video_chats", "can_manage_topics",
                        "can_post_stories", "can_edit_stories", "can_delete_stories",
                    ]
                }
                await context.bot.promote_chat_member(channel_id, user_id, **rights)
                result = "Admin demoted."
            else:
                await self._edit_or_send(update, "That admin action isn’t available.", None)
                return
            logger.info("[channel-manager] channel admin changed channel_id=%s actor_id=%s target_id=%s action=%s", channel_id, update.effective_user.id, user_id, operation)
            await self._edit_or_send(update, result, InlineKeyboardMarkup([[InlineKeyboardButton("← Admins", callback_data=f"cm:admins:{channel_id}")]]))
        except Exception as exc:
            logger.warning("[channel-manager] channel admin change failed channel_id=%s actor_id=%s target_id=%s action=%s error=%s", channel_id, update.effective_user.id, user_id, operation, type(exc).__name__)
            await self._edit_or_send(update, "Telegram couldn’t change that admin. Check Annie’s permissions and the target’s role, then try again.", InlineKeyboardMarkup([[InlineKeyboardButton("← Admins", callback_data=f"cm:admins:{channel_id}")]]))

    async def _show_marginals(self, update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: int) -> None:
        channel = await self._channel_for_action(channel_id, update.effective_user.id, context.bot)
        repo = self._get_repository()
        if channel is None:
            await self._access_denied(update)
            return
        if repo is None:
            await self._storage_error(update)
            return
        try:
            settings = await asyncio.to_thread(repo.get_marginals, channel_id)
        except Exception as exc:
            logger.warning("[channel-manager] post margins load failed channel_id=%s error=%s", channel_id, type(exc).__name__)
            await self._storage_error(update)
            return
        header_items = settings.get("header_items") or []
        footer_items = settings.get("footer_items") or []
        header_buttons = settings.get("header_buttons") or []
        footer_buttons = settings.get("footer_buttons") or []
        name = html_escape(str(channel.get("name") or "Channel"))
        types = set(settings.get("types") or [])
        type_names = ["Text", "Photo", "Video", "File", "Animation", "Audio", "Voice"]
        type_keys = ["text", "photo", "video", "document", "animation", "audio", "voice"]
        rows = [
            [InlineKeyboardButton(f"Header items ({len(header_items)})", callback_data=f"cm:marginal_items:{channel_id}:header"),
             InlineKeyboardButton(f"Footer items ({len(footer_items)})", callback_data=f"cm:marginal_items:{channel_id}:footer")],
            [InlineKeyboardButton(f"Header buttons ({len(header_buttons)})", callback_data=f"cm:marginal_buttons:{channel_id}:header"),
             InlineKeyboardButton(f"Footer buttons ({len(footer_buttons)})", callback_data=f"cm:marginal_buttons:{channel_id}:footer")],
            [InlineKeyboardButton("Clear header", callback_data=f"cm:marginal_clear:{channel_id}:header"),
             InlineKeyboardButton("Clear footer", callback_data=f"cm:marginal_clear:{channel_id}:footer")],
        ]
        for index in range(0, len(type_keys), 2):
            row = []
            for key, label in zip(type_keys[index:index + 2], type_names[index:index + 2]):
                mark = "✓ " if key in types else ""
                row.append(InlineKeyboardButton(mark + label, callback_data=f"cm:marginal_type:{channel_id}:{key}"))
            rows.append(row)
        rows.append([InlineKeyboardButton("← Channel Manager", callback_data=f"cm:channel:{channel_id}")])
        def summarize(items: list[dict[str, Any]]) -> str:
            labels = []
            for item in items[:5]:
                media_type = str((item.get("media") or {}).get("type") or "")
                if media_type:
                    labels.append(media_type.title())
                elif item.get("source_message"):
                    labels.append("Forwarded item")
                else:
                    text_value = str(item.get("text") or item.get("caption") or "").strip()
                    labels.append((text_value.splitlines()[0][:28] if text_value else "Text"))
            if len(items) > 5:
                labels.append(f"+{len(items) - 5} more")
            return ", ".join(labels) if labels else "Off"
        text = (
            f"<b>Header &amp; Footer · {name}</b>\n\n"
            "Annie adds these to matching messages she publishes. Posts sent directly by other people or bots stay unchanged.\n\n"
            f"<b>Header items:</b> {html_escape(summarize(header_items))}\n"
            f"<b>Header buttons:</b> {len(header_buttons)}\n"
            f"<b>Footer items:</b> {html_escape(summarize(footer_items))}\n"
            f"<b>Footer buttons:</b> {len(footer_buttons)}\n\n"
            "Add several messages to either list. Link buttons appear under the last item. Choose which post types use them below."
        )
        await self._edit_or_send(update, text, InlineKeyboardMarkup(rows), ParseMode.HTML)

    async def _show_marginal_items(self, update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: int, field: str) -> None:
        if field not in {"header", "footer"} or await self._channel_for_action(channel_id, update.effective_user.id, context.bot) is None:
            await self._access_denied(update)
            return
        repo = self._get_repository()
        if repo is None:
            await self._storage_error(update)
            return
        settings = await asyncio.to_thread(repo.get_marginals, channel_id)
        items = list(settings.get(f"{field}_items") or [])
        rows = [[InlineKeyboardButton("＋ Add content", callback_data=f"cm:marginal_set:{channel_id}:{field}")]]
        for index, item in enumerate(items):
            media_type = str((item.get("media") or {}).get("type") or "")
            label = media_type.title() if media_type else str(item.get("text") or item.get("caption") or "Forwarded item").splitlines()[0][:28]
            rows.append([InlineKeyboardButton(f"{index + 1}. {label} · Remove", callback_data=f"cm:marginal_item_remove:{channel_id}:{field}:{index}")])
        rows.append([InlineKeyboardButton("← Header & Footer", callback_data=f"cm:marginals:{channel_id}")])
        title = "Header" if field == "header" else "Footer"
        await self._edit_or_send(update, f"<b>{title} content</b>\n\nAnnie sends these messages in order. Add more than one if needed.", InlineKeyboardMarkup(rows), ParseMode.HTML)

    async def _show_marginal_buttons(self, update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: int, field: str) -> None:
        if field not in {"header", "footer"} or await self._channel_for_action(channel_id, update.effective_user.id, context.bot) is None:
            await self._access_denied(update)
            return
        repo = self._get_repository()
        if repo is None:
            await self._storage_error(update)
            return
        settings = await asyncio.to_thread(repo.get_marginals, channel_id)
        buttons = list(settings.get(f"{field}_buttons") or [])
        rows = [[InlineKeyboardButton("＋ Add link button", callback_data=f"cm:marginal_button_add:{channel_id}:{field}")]]
        for index, button in enumerate(buttons):
            label = str(button.get("text") or "Open link")[:35]
            rows.append([InlineKeyboardButton(f"{index + 1}. {label} · Remove", callback_data=f"cm:marginal_button_remove:{channel_id}:{field}:{index}")])
        rows.append([InlineKeyboardButton("← Header & Footer", callback_data=f"cm:marginals:{channel_id}")])
        title = "Header" if field == "header" else "Footer"
        text = f"<b>{title} link buttons</b>\n\nButtons are placed under the last {title.lower()} item. Add at least one item first."
        await self._edit_or_send(update, text, InlineKeyboardMarkup(rows), ParseMode.HTML)

    async def _access_denied(self, update: Update) -> None:
        if update.callback_query:
            await self._edit_or_send(
                update,
                "This channel is not available to your account.",
                InlineKeyboardMarkup([[InlineKeyboardButton("← Channel Manager", callback_data="cm:home")]]),
            )
        else:
            await update.effective_message.reply_text(
                "This channel is not available to your account. Ask the channel owner to check access."
            )

    async def _show_templates(self, update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: int) -> None:
        if await self._channel_for_action(channel_id, update.effective_user.id, context.bot) is None:
            await self._access_denied(update)
            return
        repository = self._get_repository()
        if repository is None:
            await self._storage_error(update)
            return
        try:
            records = await asyncio.to_thread(repository.list_records, channel_id, "template", ["active"], None, True)
        except Exception as exc:
            logger.warning("[channel-manager] template list failed channel_id=%s error=%s", channel_id, type(exc).__name__)
            await self._storage_error(update)
            return
        rows = []
        for item in records:
            rows.append([
                InlineKeyboardButton(
                    f"Use · {self._record_label(item, 'Template')}",
                    callback_data=f"cm:template_use:{item['_id']}",
                ),
                InlineKeyboardButton("Manage", callback_data=f"cm:template:{item['_id']}"),
            ])
        rows.append([InlineKeyboardButton("＋ New template", callback_data=f"cm:template_new:{channel_id}")])
        rows.append(self._back_button(f"cm:channel:{channel_id}"))
        text = "<b>Templates</b>\nChoose a saved layout to start a post, or create a new one."
        if not records:
            text = "<b>Templates</b>\nNo templates yet. Create one from a post draft or start a new template."
        await self._edit_or_send(update, text, InlineKeyboardMarkup(rows), ParseMode.HTML)

    async def _show_drafts(self, update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: int) -> None:
        if await self._channel_for_action(channel_id, update.effective_user.id, context.bot) is None:
            await self._access_denied(update)
            return
        repository = self._get_repository()
        if repository is None:
            await self._storage_error(update)
            return
        try:
            records = await asyncio.to_thread(
                repository.list_records, channel_id, "post", ["draft"],
                int(update.effective_user.id), True,
            )
        except Exception as exc:
            logger.warning("[channel-manager] draft list failed channel_id=%s user_id=%s error=%s", channel_id, update.effective_user.id, type(exc).__name__)
            await self._storage_error(update)
            return
        rows = [[InlineKeyboardButton(
            f"{self._record_label(item, 'Untitled draft')}{' · shared' if item.get('shared') else ''}",
            callback_data=f"cm:draft:{item['_id']}",
        )] for item in records]
        rows.append(self._back_button(f"cm:channel:{channel_id}"))
        text = "<b>Drafts</b>\nYour drafts are private unless you share them with this channel’s admins."
        if not records:
            text = "<b>Drafts</b>\nNo drafts yet. Create a post and choose <i>Keep draft</i>."
        await self._edit_or_send(update, text, InlineKeyboardMarkup(rows), ParseMode.HTML)

    async def _show_scheduled(self, update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: int) -> None:
        if await self._channel_for_action(channel_id, update.effective_user.id, context.bot) is None:
            await self._access_denied(update)
            return
        repository = self._get_repository()
        if repository is None:
            await self._storage_error(update)
            return
        try:
            records = await asyncio.to_thread(repository.list_records, channel_id, "post", ["scheduled", "failed", "needs_review"], None, True)
        except Exception as exc:
            logger.warning("[channel-manager] schedule list failed channel_id=%s error=%s", channel_id, type(exc).__name__)
            await self._storage_error(update)
            return
        rows = []
        for item in records:
            label = self._record_label(item, "Scheduled post")
            if item.get("status") in {"failed", "needs_review"}:
                label = f"⚠ Review · {label}"
            when = item.get("scheduled_at")
            if when:
                try:
                    tz = ZoneInfo(str(item.get("timezone") or "UTC"))
                    local = when.astimezone(tz).strftime("%d %b %H:%M")
                    label = f"{label} · {local}"
                except Exception:
                    pass
            rows.append([InlineKeyboardButton(label[:60], callback_data=f"cm:scheduled_post:{item['_id']}")])
        rows.append(self._back_button(f"cm:channel:{channel_id}"))
        text = "<b>Scheduled posts</b>\nAdmins can review or cancel queued posts. Times use the timezone shown when scheduling."
        if not records:
            text = "<b>Scheduled posts</b>\nNothing is scheduled for this channel yet."
        await self._edit_or_send(update, text, InlineKeyboardMarkup(rows), ParseMode.HTML)

    async def _show_published_posts(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        channel_id: int, page: int = 0,
    ) -> None:
        if await self._channel_for_action(channel_id, update.effective_user.id, context.bot) is None:
            await self._access_denied(update)
            return
        repository = self._get_repository()
        if repository is None:
            await self._storage_error(update)
            return
        page = max(0, int(page))
        page_size = 10
        try:
            records = await asyncio.to_thread(
                repository.list_records, channel_id, "post", ["published"],
                None, True, page_size + 1, page * page_size,
            )
        except Exception as exc:
            logger.warning(
                "[channel-manager] published post list failed channel_id=%s user_id=%s error=%s",
                channel_id, update.effective_user.id, type(exc).__name__,
            )
            await self._storage_error(update)
            return
        has_next = len(records) > page_size
        records = records[:page_size]
        rows = [[InlineKeyboardButton(
            self._record_label(record, "Published post"),
            callback_data=f"cm:published_post:{record['_id']}:{page}",
        )] for record in records]
        navigation = []
        if page:
            navigation.append(InlineKeyboardButton(
                "← Newer", callback_data=f"cm:published:{channel_id}:{page - 1}"
            ))
        if has_next:
            navigation.append(InlineKeyboardButton(
                "Older →", callback_data=f"cm:published:{channel_id}:{page + 1}"
            ))
        if navigation:
            rows.append(navigation)
        rows.append(self._back_button(f"cm:channel:{channel_id}"))
        text = "<b>Published posts</b>\nChoose a post Annie sent to edit or delete it."
        if not records:
            text = (
                "<b>Published posts</b>\nNo saved posts here yet. "
                "This list only shows posts Annie published through Channel Manager."
            )
        await self._edit_or_send(update, text, InlineKeyboardMarkup(rows), ParseMode.HTML)

    @staticmethod
    def _published_main_message_id(record: dict[str, Any]) -> int | None:
        value = record.get("published_main_message_id")
        if value:
            try:
                parsed = int(value)
                if parsed > 0:
                    return parsed
            except (TypeError, ValueError):
                pass
        ids: list[int] = []
        for item in record.get("published_message_ids") or []:
            try:
                parsed = int(item)
                if parsed > 0:
                    ids.append(parsed)
            except (TypeError, ValueError):
                continue
        if not ids and record.get("published_message_id"):
            try:
                parsed = int(record["published_message_id"])
                if parsed > 0:
                    ids = [parsed]
            except (TypeError, ValueError):
                pass
        if not ids:
            return None
        settings = record.get("marginal_snapshot") or {}
        media_type = str((record.get("media") or {}).get("type") or "text")
        if media_type not in set(settings.get("types") or []):
            return ids[0]
        header_items = list(settings.get("header_items") or [])
        if not header_items and (settings.get("header") or {}).get("text"):
            header_items.append(settings["header"])
        if not header_items and settings.get("header_content"):
            header_items.append(settings["header_content"])
        return ids[len(header_items)] if len(header_items) < len(ids) else ids[0]

    @classmethod
    def _published_components(cls, record: dict[str, Any]) -> list[dict[str, Any]]:
        """Map saved message IDs to the main post, margins, and extra messages."""
        ids: list[int] = []
        for value in record.get("published_message_ids") or []:
            try:
                message_id = int(value)
                if message_id > 0:
                    ids.append(message_id)
            except (TypeError, ValueError):
                continue
        if not ids and record.get("published_message_id"):
            try:
                ids = [int(record["published_message_id"])]
            except (TypeError, ValueError):
                return []

        settings = record.get("marginal_snapshot") or {}
        main_type = str((record.get("media") or {}).get("type") or "text")
        margins_active = main_type in set(settings.get("types") or [])

        def margin_items(field: str) -> list[dict[str, Any]]:
            items = list(settings.get(f"{field}_items") or [])
            if items:
                return items
            legacy = settings.get(field) or {}
            if legacy:
                return [dict(legacy)]
            content = settings.get(f"{field}_content") or {}
            if content:
                return [dict(content)]
            if field == "footer":
                sticker = settings.get("footer_sticker") or {}
                if sticker.get("file_id"):
                    return [{"media": {"type": "sticker", "file_id": sticker["file_id"]}}]
            return []

        components: list[dict[str, Any]] = []
        if margins_active:
            components.extend({"kind": "header", "source_index": i, "data": item}
                              for i, item in enumerate(margin_items("header")))
        components.append({"kind": "main", "source_index": 0, "data": record})
        components.extend({"kind": "extra", "source_index": i, "data": item}
                          for i, item in enumerate(record.get("followups") or []))
        if margins_active:
            components.extend({"kind": "footer", "source_index": i, "data": item}
                              for i, item in enumerate(margin_items("footer")))
        for component, message_id in zip(components, ids):
            component["message_id"] = message_id
        return [item for item in components if item.get("message_id")]

    async def _show_published_components(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        record_id: str, page: int = 0,
    ) -> None:
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot)
        components = self._published_components(record or {})
        if not record or channel is None or record.get("status") != "published":
            await self._access_denied(update)
            return
        extras = [item for item in components if item.get("kind") != "main"]
        rows: list[list[InlineKeyboardButton]] = []
        for index, component in enumerate(extras):
            kind = str(component["kind"])
            data = component.get("data") or {}
            media_type = str((data.get("media") or {}).get("type") or "")
            label = {"header": "Header", "footer": "Footer", "extra": "Extra content"}.get(kind, "Message")
            if media_type:
                label += f" · {media_type.title()}"
            else:
                label += f" · {self._record_label(data, 'Text')[:28]}"
            rows.append([InlineKeyboardButton(label[:60], callback_data=f"cm:pub_comp:{record_id}:{index}:{page}")])
        rows.append(self._back_button(f"cm:pub_edit:{record_id}:{page}", "← Edit published post"))
        await self._edit_or_send(
            update,
            "<b>Header, footer & extra messages</b>\nChoose an item to edit or remove. Changes apply to this published post only.",
            InlineKeyboardMarkup(rows), ParseMode.HTML,
        )

    async def _show_published_component(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        record_id: str, index: int, page: int = 0,
    ) -> None:
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot)
        components = self._published_components(record or {})
        extras = [item for item in components if item.get("kind") != "main"]
        if not record or channel is None or record.get("status") != "published" or not 0 <= index < len(extras):
            await self._access_denied(update)
            return
        component = extras[index]
        data = component.get("data") or {}
        media_type = str((data.get("media") or {}).get("type") or "")
        rows: list[list[InlineKeyboardButton]] = []
        if media_type not in {"sticker", "video_note"} and not data.get("source_message"):
            rows.append([InlineKeyboardButton("Edit text / caption", callback_data=f"cm:pub_comp_txt:{record_id}:{index}:{page}")])
        if media_type in {"photo", "video", "animation", "document", "audio"}:
            rows.append([InlineKeyboardButton("Replace media", callback_data=f"cm:pub_comp_med:{record_id}:{index}:{page}")])
        rows.append([InlineKeyboardButton("Remove this message", callback_data=f"cm:pub_comp_ask:{record_id}:{index}:{page}")])
        rows.append(self._back_button(f"cm:pub_comps:{record_id}:{page}", "← All extra messages"))
        label = str(component.get("kind") or "message").replace("extra", "extra content").title()
        note = (
            "Telegram doesn’t let Annie edit a sticker in place. You can remove this message."
            if media_type == "sticker" else
            "This Telegram message type can only be removed."
            if not rows[:-2] else
            ""
        )
        await self._edit_or_send(
            update, f"<b>{html_escape(label)}</b>\nChoose what to change." + (f"\n\n{note}" if note else ""),
            InlineKeyboardMarkup(rows), ParseMode.HTML,
        )

    async def _start_published_component_input(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        record_id: str, index: int, mode: str, page: int = 0,
    ) -> None:
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot)
        components = self._published_components(record or {})
        extras = [item for item in components if item.get("kind") != "main"]
        if not record or channel is None or record.get("status") != "published" or not 0 <= index < len(extras):
            await self._access_denied(update)
            return
        component = extras[index]
        media_type = str(((component.get("data") or {}).get("media") or {}).get("type") or "")
        if mode == "text" and media_type in {"sticker", "video_note"}:
            await self._edit_or_send(update, "Telegram doesn’t support text or captions on stickers or video notes.", None)
            return
        if mode == "media" and media_type not in {"photo", "video", "animation", "document", "audio"}:
            await self._edit_or_send(update, "This message type can’t have its media replaced in Telegram.", None)
            return
        query = update.callback_query
        prompt_message = query.message
        self._inputs[int(update.effective_user.id)] = {
            "kind": f"published_component_{mode}", "record_id": record_id,
            "channel_id": int(record["channel_id"]), "component_index": index,
            "page": page, "expires_at": time.time() + self.INPUT_TTL_SECONDS,
            "prompt_chat_id": int(prompt_message.chat.id),
            "prompt_message_id": int(prompt_message.message_id),
        }
        prompt = ("Send the new text or caption for this published message."
                  if mode == "text" else
                  "Send a new photo, video, animation, audio, or file. Annie will keep the current caption.")
        await self._edit_or_send(
            update, prompt,
            InlineKeyboardMarkup([[InlineKeyboardButton(
                "Cancel", callback_data=f"cm:pub_comp:{record_id}:{index}:{page}"
            )]]),
        )

    async def _component_record_changes(
        self, record: dict[str, Any], component: dict[str, Any],
        new_data: dict[str, Any] | None = None, remove: bool = False,
    ) -> dict[str, Any]:
        kind, source_index = str(component["kind"]), int(component["source_index"])
        if kind == "extra":
            items = list(record.get("followups") or [])
            if not 0 <= source_index < len(items):
                raise ValueError("extra message is no longer present")
            if remove:
                items.pop(source_index)
            else:
                items[source_index] = new_data or {}
            return {"followups": items}

        settings = dict(record.get("marginal_snapshot") or {})
        field = kind
        items = list(settings.get(f"{field}_items") or [])
        if not items:
            legacy = settings.get(field) or {}
            content = settings.get(f"{field}_content") or {}
            if legacy:
                items = [dict(legacy)]
            elif content:
                items = [dict(content)]
            elif field == "footer" and (settings.get("footer_sticker") or {}).get("file_id"):
                items = [{"media": dict(settings["footer_sticker"])}]
        if not 0 <= source_index < len(items):
            raise ValueError(f"{field} message is no longer present")
        if remove:
            items.pop(source_index)
        else:
            items[source_index] = new_data or {}
        settings[f"{field}_items"] = items
        settings.pop(field, None)
        settings.pop(f"{field}_content", None)
        if field == "footer":
            settings.pop("footer_sticker", None)
        return {"marginal_snapshot": settings}

    async def _apply_published_component_edit(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        record: dict[str, Any], index: int, content: dict[str, Any], mode: str,
        page: int = 0,
    ) -> bool:
        components = self._published_components(record)
        extras = [item for item in components if item.get("kind") != "main"]
        if not 0 <= index < len(extras):
            await update.effective_message.reply_text("That message is no longer part of this post.")
            return False
        component = extras[index]
        data = dict(component.get("data") or {})
        message_id = int(component["message_id"])
        channel_id = int(record["channel_id"])
        if mode == "text":
            text = str(content.get("text") or "")
            if not text.strip():
                await update.effective_message.reply_text("Send some text for this message.")
                return False
            if data.get("media"):
                send_kwargs = self._send_kwargs({**data, "caption": text, "entities": content.get("entities") or []})
                format_kwargs = {key: send_kwargs[key] for key in ("parse_mode", "caption_entities") if key in send_kwargs}
                await context.bot.edit_message_caption(
                    chat_id=channel_id, message_id=message_id,
                    caption=text, **format_kwargs,
                )
                data.update({"caption": text, "entities": content.get("entities") or [], "format_mode": "telegram"})
            else:
                send_kwargs = self._send_kwargs({**data, "text": text, "entities": content.get("entities") or []})
                format_kwargs = {key: send_kwargs[key] for key in ("parse_mode", "entities") if key in send_kwargs}
                await context.bot.edit_message_text(
                    chat_id=channel_id, message_id=message_id,
                    text=text, **format_kwargs,
                )
                data.update({"text": text, "entities": content.get("entities") or [], "format_mode": "telegram"})
        else:
            media = content.get("media") or {}
            media_classes = {
                "photo": InputMediaPhoto, "video": InputMediaVideo,
                "animation": InputMediaAnimation, "document": InputMediaDocument,
                "audio": InputMediaAudio,
            }
            media_class = media_classes.get(str(media.get("type") or ""))
            if not media_class:
                await update.effective_message.reply_text("Send a photo, video, animation, audio, or file.")
                return False
            caption = str(data.get("caption") or data.get("text") or "")
            temp_record = {**data, "media": media, "caption": caption}
            send_kwargs = self._send_kwargs(temp_record)
            format_kwargs = {key: send_kwargs[key] for key in ("parse_mode", "caption_entities") if key in send_kwargs}
            input_media = media_class(media=str(media.get("file_id") or ""), caption=caption or None, **format_kwargs)
            await context.bot.edit_message_media(
                chat_id=channel_id, message_id=message_id, media=input_media,
            )
            data.update({"media": media, "caption": caption, "text": "", "source_message": None})

        changes = await self._component_record_changes(record, component, new_data=data)
        try:
            saved = await asyncio.to_thread(self._get_repository().update, str(record["_id"]), changes)
        except Exception as exc:
            logger.error("[channel-manager] extra message edited but save failed channel_id=%s post_id=%s message_id=%s error=%s", channel_id, record.get("_id"), message_id, type(exc).__name__)
            await update.effective_message.reply_text("The channel message changed, but Annie couldn’t save its details.")
            return False
        if not saved:
            await update.effective_message.reply_text("The channel message changed, but Annie couldn’t save its details.")
            return False
        await self._mirror_edit_to_backups(
            context.bot, channel_id, message_id, data, preserve_reply_markup=True,
        )
        return True

    async def _preview_published_post(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        record_id: str, page: int = 0,
    ) -> None:
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot)
        if not record or channel is None or record.get("status") != "published":
            await self._access_denied(update)
            return
        user_id = int(update.effective_user.id)
        for old_id in self._published_previews.pop(user_id, []):
            await self._delete_message(context.bot, user_id, old_id)
        ids = []
        for value in record.get("published_message_ids") or []:
            try:
                message_id = int(value)
                if message_id > 0:
                    ids.append(message_id)
            except (TypeError, ValueError):
                continue
        if not ids and record.get("published_message_id"):
            try:
                ids = [int(record["published_message_id"])]
            except (TypeError, ValueError):
                ids = []
        sent_ids: list[int] = []
        try:
            for message_id in ids:
                copied = await context.bot.copy_message(
                    chat_id=user_id, from_chat_id=int(record["channel_id"]),
                    message_id=message_id,
                )
                sent_ids.append(int(copied.message_id))
        except Exception as exc:
            for sent_id in sent_ids:
                await self._delete_message(context.bot, user_id, sent_id)
            logger.warning("[channel-manager] published preview failed channel_id=%s user_id=%s post_id=%s error=%s", record.get("channel_id"), user_id, record_id, type(exc).__name__)
            await self._edit_or_send(update, "Annie couldn’t show the preview. Check that she can access the post in the channel.", None)
            return
        self._published_previews[user_id] = sent_ids
        controls = await context.bot.send_message(
            chat_id=user_id,
            text="Current preview. Changes here update the channel post.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Edit post", callback_data=f"cm:published_edit:{record_id}:{page}")],
                [InlineKeyboardButton("← Published post", callback_data=f"cm:published_post:{record_id}:{page}")],
            ]),
        )
        self._published_previews[user_id].append(int(controls.message_id))

    async def _remove_published_component(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        record_id: str, index: int, page: int = 0,
    ) -> None:
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot)
        components = self._published_components(record or {})
        extras = [item for item in components if item.get("kind") != "main"]
        if not record or channel is None or record.get("status") != "published" or not 0 <= index < len(extras):
            await self._access_denied(update)
            return
        component = extras[index]
        message_id = int(component["message_id"])
        try:
            deleted = False
            if self.delete_channel_messages:
                try:
                    deleted = bool(await self.delete_channel_messages(int(record["channel_id"]), [message_id]))
                except Exception as exc:
                    logger.warning("[channel-manager] extra message MTProto delete failed channel_id=%s message_id=%s error=%s", record["channel_id"], message_id, type(exc).__name__)
            if not deleted:
                await context.bot.delete_message(chat_id=int(record["channel_id"]), message_id=message_id)
        except Exception as exc:
            logger.warning("[channel-manager] extra message delete failed channel_id=%s post_id=%s message_id=%s error=%s", record["channel_id"], record_id, message_id, type(exc).__name__)
            await self._edit_or_send(update, "Annie couldn’t remove that message. Check her channel permissions and try again.", None)
            return
        ids = [int(value) for value in (record.get("published_message_ids") or []) if str(value).isdigit()]
        remaining_ids = [value for value in ids if value != message_id]
        try:
            changes = await self._component_record_changes(record, component, remove=True)
            changes.update({
                "published_message_ids": remaining_ids,
                "published_message_id": remaining_ids[0] if remaining_ids else None,
            })
            repository = self._get_repository()
            saved = await asyncio.to_thread(repository.update, record_id, changes) if repository else False
        except Exception as exc:
            logger.error("[channel-manager] removed extra message but save failed channel_id=%s post_id=%s message_id=%s error=%s", record["channel_id"], record_id, message_id, type(exc).__name__)
            saved = False
        if not saved:
            await self._edit_or_send(update, "The channel message was removed, but Annie couldn’t update the saved post details.", None)
            return
        await self._delete_from_backups(context.bot, int(record["channel_id"]), [message_id])
        await self._preview_published_post(update, context, record_id, page)

    async def _show_published_post(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        record_id: str, page: int = 0,
    ) -> None:
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot)
        if not record or channel is None or record.get("kind") != "post" or record.get("status") != "published":
            await self._access_denied(update)
            return
        main_message_id = self._published_main_message_id(record)
        rows = []
        if main_message_id is not None:
            rows.append([InlineKeyboardButton("Edit post", callback_data=f"cm:published_edit:{record_id}:{page}")])
        rows.extend([
            [InlineKeyboardButton("Delete post", callback_data=f"cm:published_delete:{record_id}:{page}")],
            [InlineKeyboardButton("Preview post", callback_data=f"cm:published_preview:{record_id}:{page}")],
            self._back_button(f"cm:published:{record['channel_id']}:{page}", "← Published posts"),
        ])
        sent_at = record.get("published_at")
        when = sent_at.strftime("%d %b %Y, %H:%M UTC") if isinstance(sent_at, datetime) else ""
        text = f"<b>{html_escape(self._record_label(record, 'Published post'))}</b>"
        if when:
            text += f"\nPublished: {html_escape(when)}"
        if main_message_id is None:
            text += "\n\nAnnie doesn’t have a saved message ID for editing this post."
        else:
            text += "\n\nEdits update the post in the channel. Delete removes the post and any extra messages Annie sent with it."
        await self._edit_or_send(update, text, InlineKeyboardMarkup(rows), ParseMode.HTML)

    async def _confirm_sensitive_action(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        action: str, record_id: str, page: int = 0,
    ) -> None:
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot)
        if not record or channel is None:
            await self._access_denied(update)
            return
        title = html_escape(self._record_label(record, "Untitled"))
        if action == "publish":
            if record.get("kind") != "post" or record.get("status") not in {"draft", "failed"}:
                await self._edit_or_send(update, "This post isn’t ready to publish. Open it from Drafts and check its status.", None)
                return
            prompt = (
                f"<b>Publish this post to {html_escape(str(channel.get('name') or 'the channel'))}?</b>\n\n"
                f"{title}\n\nAnnie will send the post and its extra messages. You can’t undo this."
            )
            confirm_label, confirm_action = "Publish now", "confirm_publish"
            cancel_action = "cancel_publish"
        elif action == "delete":
            if record.get("kind") != "post" or record.get("status") != "draft" or int(record.get("created_by", 0)) != int(update.effective_user.id):
                await self._access_denied(update)
                return
            prompt = f"<b>Delete this draft?</b>\n\n{title}\n\nThis permanently removes the draft and its saved previews."
            confirm_label, confirm_action = "Delete draft", "confirm_delete"
            cancel_action = "cancel_delete"
        elif action == "template_delete":
            if record.get("kind") != "template":
                await self._access_denied(update)
                return
            prompt = f"<b>Delete this template?</b>\n\n{title}\n\nChannel admins won’t be able to use it anymore."
            confirm_label, confirm_action = "Delete template", "confirm_template_delete"
            cancel_action = "cancel_template_delete"
        elif action == "unschedule":
            if record.get("status") not in {"scheduled", "failed", "needs_review"}:
                await self._access_denied(update)
                return
            prompt = f"<b>Cancel this scheduled post?</b>\n\n{title}\n\nIt will return to Drafts and won’t be published at the scheduled time."
            confirm_label, confirm_action = "Cancel schedule", "confirm_unschedule"
            cancel_action = "cancel_unschedule"
        elif action == "published_delete":
            if record.get("kind") != "post" or record.get("status") != "published":
                await self._access_denied(update)
                return
            prompt = (
                f"<b>Delete this published post?</b>\n\n{title}\n\n"
                "Annie will remove the post and its extra messages from the channel."
            )
            confirm_label, confirm_action = "Delete published post", "confirm_published_delete"
            cancel_action = "cancel_published_delete"
        else:
            await self._access_denied(update)
            return
        await self._edit_or_send(
            update, prompt,
            InlineKeyboardMarkup([
                [InlineKeyboardButton(confirm_label, callback_data=f"cm:{confirm_action}:{record_id}:{page}")],
                [InlineKeyboardButton("Go back", callback_data=f"cm:{cancel_action}:{record_id}:{page}")],
            ]),
            ParseMode.HTML,
        )

    async def _show_record(self, update: Update, context: ContextTypes.DEFAULT_TYPE, record_id: str) -> None:
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot)
        if record is None:
            await self._edit_or_send(update, "That saved post no longer exists.", None)
            return
        if channel is None:
            await self._access_denied(update)
            return
        if record.get("kind") == "template":
            rows = [[InlineKeyboardButton("Use this template", callback_data=f"cm:template_use:{record_id}")],
                    [InlineKeyboardButton("Delete template", callback_data=f"cm:template_delete:{record_id}")],
                    self._back_button(f"cm:templates:{record['channel_id']}")]
            await self._edit_or_send(update, f"<b>Template · {html_escape(self._record_label(record, 'Template'))}</b>", InlineKeyboardMarkup(rows), ParseMode.HTML)
            return
        status = str(record.get("status") or "draft")
        if status == "scheduled":
            scheduled_at = record.get("scheduled_at")
            try:
                zone = ZoneInfo(str(record.get("timezone") or "UTC"))
                when = scheduled_at.astimezone(zone).strftime("%Y-%m-%d %H:%M %Z")
            except Exception:
                when = str(scheduled_at or "time unavailable")
            rows = [[InlineKeyboardButton("Cancel schedule", callback_data=f"cm:unschedule:{record_id}")],
                    self._back_button(f"cm:scheduled:{record['channel_id']}")]
            await self._edit_or_send(update, f"<b>Scheduled post</b>\n{html_escape(when)}", InlineKeyboardMarkup(rows), ParseMode.HTML)
            return
        if status in {"failed", "needs_review"}:
            issue = html_escape(str(record.get("last_error") or "Check the channel before retrying."))
            retry_action = "retry" if status == "needs_review" else "publish"
            rows = [[InlineKeyboardButton("I checked the channel — retry" if status == "needs_review" else "Retry publish", callback_data=f"cm:{retry_action}:{record_id}")],
                    self._back_button(f"cm:scheduled:{record['channel_id']}")]
            await self._edit_or_send(update, f"<b>Post needs attention</b>\n{issue}", InlineKeyboardMarkup(rows), ParseMode.HTML)
            return
        rows = [[InlineKeyboardButton("Preview post", callback_data=f"cm:post_preview:{record_id}")],
                [InlineKeyboardButton("Publish", callback_data=f"cm:publish:{record_id}"),
                 InlineKeyboardButton("Schedule", callback_data=f"cm:schedule:{record_id}")],
                [InlineKeyboardButton("Edit", callback_data=f"cm:edit:{record_id}"),
                 InlineKeyboardButton("Share with admins", callback_data=f"cm:share:{record_id}")],
                [InlineKeyboardButton("Save as template", callback_data=f"cm:save_template:{record_id}")],
                [InlineKeyboardButton("Delete draft", callback_data=f"cm:delete:{record_id}")],
                self._back_button(f"cm:drafts:{record['channel_id']}")]
        await self._edit_or_send(update, f"<b>Draft · {html_escape(self._record_label(record, 'Untitled'))}</b>", InlineKeyboardMarkup(rows), ParseMode.HTML)

    async def _preview(self, update: Update, context: ContextTypes.DEFAULT_TYPE, record_id: str) -> bool:
        repository = self._get_repository()
        record = await asyncio.to_thread(repository.get, record_id) if repository else None
        if not record:
            await self._edit_or_send(update, "I couldn’t save that draft. Please try again.", None)
            return False
        try:
            old_chat_id = record.get("preview_chat_id")
            old_message_ids = list(record.get("preview_message_ids") or [])
            if not old_message_ids and record.get("preview_message_id"):
                old_message_ids = [record["preview_message_id"]]
            settings = await asyncio.to_thread(repository.get_marginals, int(record["channel_id"]))
            sent = await self._send_post_messages(
                context.bot, int(update.effective_user.id), record, preview=True,
                marginal_settings=settings,
            )
            message_ids = [int(item.message_id) for item in sent if getattr(item, "message_id", None)]
            message_id = message_ids[-1] if message_ids else None
            if message_id:
                saved = await asyncio.to_thread(repository.update, record_id, {
                    "preview_chat_id": int(update.effective_user.id),
                    "preview_message_id": int(message_id),
                    "preview_message_ids": message_ids,
                    "marginal_snapshot": settings,
                })
                if saved and old_chat_id:
                    for old_id in old_message_ids:
                        if int(old_id) not in message_ids:
                            await self._delete_message(context.bot, int(old_chat_id), int(old_id))
                elif not saved:
                    logger.warning("[channel-manager] preview reference save failed user_id=%s post_id=%s", int(update.effective_user.id), record_id)
                    for new_id in message_ids:
                        await self._delete_message(context.bot, int(update.effective_user.id), new_id)
                    await context.bot.send_message(
                        chat_id=int(update.effective_user.id),
                        text="I couldn’t update the saved preview. Your previous preview is still there; please try again.",
                    )
                    return False
            logger.info("[channel-manager] preview sent channel_id=%s user_id=%s post_id=%s", int(record["channel_id"]), int(update.effective_user.id), record_id)
            return True
        except PartialPostSendError as exc:
            for sent_id in exc.sent_message_ids:
                await self._delete_message(context.bot, int(update.effective_user.id), sent_id)
            logger.warning("[channel-manager] partial preview cleaned user_id=%s post_id=%s sent=%s cause=%s", int(update.effective_user.id), record_id, len(exc.sent_message_ids), type(exc.cause).__name__)
            await self._edit_or_send(update, "I couldn’t preview every part of this post. Your draft is saved; try again or edit the extra content.", None)
        except BadRequest as exc:
            logger.info("[channel-manager] invalid post formatting channel_id=%s user_id=%s post_id=%s", int(record["channel_id"]), int(update.effective_user.id), record_id)
            await self._edit_or_send(update, "Telegram couldn’t read that formatting. Check the text or MarkdownV2 symbols, then edit the draft.", None)
        except ValueError as exc:
            await self._edit_or_send(update, str(exc), None)
        except Exception as exc:
            logger.warning("[channel-manager] preview failed channel_id=%s user_id=%s post_id=%s error=%s", int(record["channel_id"]), int(update.effective_user.id), record_id, type(exc).__name__)
            await self._edit_or_send(update, "I couldn’t show the preview. Your draft is saved; please try again.", None)
        return False

    async def _storage_error(self, update: Update) -> None:
        await self._edit_or_send(
            update, "Channel Manager storage is unavailable. Please try again later.",
            InlineKeyboardMarkup([[InlineKeyboardButton("← Channel Manager", callback_data="cm:home")]]),
        )

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query:
            return
        data = str(query.data or "")
        try:
            if not self._is_private(update):
                await query.answer("Open Channel Manager in a private chat with Annie.", show_alert=True)
                return
            await query.answer()
            parts = data.split(":")
            action = parts[1] if len(parts) > 1 else ""
            if action in {"marginals", "marginal_items", "marginal_buttons"}:
                pending = self._inputs.get(int(update.effective_user.id)) or {}
                if str(pending.get("kind") or "").startswith("marginal_"):
                    self._inputs.pop(int(update.effective_user.id), None)
            if action == "home":
                if not await self.has_connected_channel_access(update.effective_user.id, context.bot):
                    await self._access_denied(update)
                    return
                # The back button should always show the channel picker. If
                # there is only one channel, auto-opening it looks like the
                # button did nothing because the user is already on that page.
                await self._show_home(update, context, auto_open_single=False)
                return
            if action == "cloning":
                await self._show_cloning_home(update, context, int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None)
                return
            if action == "clone_start":
                preferred = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() and int(parts[2]) else None
                await self._clone_source_prompt(update, context, preferred)
                return
            if action == "clone_source":
                await self._clone_destination_prompt(update, context, int(parts[2]))
                return
            if action == "clone_dest":
                await self._clone_confirm_prompt(update, context, int(parts[2]), int(parts[3]))
                return
            if action == "clone_save":
                await self._save_clone_pair(update, context, int(parts[2]), int(parts[3]))
                return
            if action == "clone_pair":
                await self._clone_confirm_prompt(update, context, int(parts[2]), int(parts[3]))
                return
            if action == "clone_confirm":
                await self._clone_channel_records(update, context, int(parts[2]), int(parts[3]))
                return
            if action == "clone_toggle":
                await self._set_auto_forward(update, context, int(parts[2]), int(parts[3]), parts[4] == "1")
                return
            if action == "autofwd_set":
                await self._set_auto_forward(update, context, int(parts[2]), int(parts[3]), parts[4] == "1")
                return
            if action == "index":
                channel_id = int(parts[2])
                if await self._channel_for_action(channel_id, update.effective_user.id, context.bot) is None:
                    await self._access_denied(update)
                    return
                await self.open_index_callback(update, context, channel_id)
                return
            if action == "admins":
                await self._show_channel_admins(update, context, int(parts[2]))
                return
            if action == "admin_start":
                await self._begin_admin_change(update, context, int(parts[2]), parts[3])
                return
            if action == "admin_target":
                await self._confirm_admin_change(
                    update, context, int(parts[3]), int(parts[4]), parts[2]
                )
                return
            if action == "admin_apply":
                await self._apply_admin_change(update, context, int(parts[3]), int(parts[4]), parts[2])
                return
            if action == "marginals":
                await self._show_marginals(update, context, int(parts[2]))
                return
            if action == "marginal_items":
                await self._show_marginal_items(update, context, int(parts[2]), parts[3])
                return
            if action == "marginal_buttons":
                await self._show_marginal_buttons(update, context, int(parts[2]), parts[3])
                return
            if action == "marginal_button_add":
                channel_id, field = int(parts[2]), parts[3]
                if field not in {"header", "footer"} or await self._channel_for_action(channel_id, update.effective_user.id, context.bot) is None:
                    await self._access_denied(update)
                    return
                repo = self._get_repository()
                if repo is None:
                    await self._storage_error(update)
                    return
                settings = await asyncio.to_thread(repo.get_marginals, channel_id)
                if not settings.get(f"{field}_items"):
                    await self._edit_or_send(update, "Add at least one content item before adding its buttons.", InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data=f"cm:marginal_items:{channel_id}:{field}")]]))
                    return
                buttons = settings.get(f"{field}_buttons") or []
                if len(buttons) >= 8:
                    await self._edit_or_send(update, "You can add up to 8 buttons to each header or footer.", InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data=f"cm:marginal_buttons:{channel_id}:{field}")]]))
                    return
                self._inputs[int(update.effective_user.id)] = {
                    "kind": "marginal_button_label", "channel_id": channel_id, "field": field,
                    "expires_at": time.time() + self.INPUT_TTL_SECONDS,
                }
                await self._edit_or_send(update, "Send the button label (up to 40 characters).", InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data=f"cm:marginal_buttons:{channel_id}:{field}")]]))
                return
            if action in {"marginal_item_remove", "marginal_button_remove"}:
                channel_id, field, index = int(parts[2]), parts[3], int(parts[4])
                if field not in {"header", "footer"} or await self._channel_for_action(channel_id, update.effective_user.id, context.bot) is None:
                    await self._access_denied(update)
                    return
                repo = self._get_repository()
                if repo is None:
                    await self._storage_error(update)
                    return
                remover = repo.remove_marginal_item if action == "marginal_item_remove" else repo.remove_marginal_button
                try:
                    removed = await asyncio.to_thread(remover, channel_id, field, index)
                except Exception as exc:
                    logger.warning("[channel-manager] marginal item removal failed channel_id=%s field=%s error=%s", channel_id, field, type(exc).__name__)
                    removed = False
                if not removed:
                    await self._edit_or_send(update, "That item is no longer available. Please reopen the settings.", None)
                    return
                if action == "marginal_item_remove":
                    await self._show_marginal_items(update, context, channel_id, field)
                else:
                    await self._show_marginal_buttons(update, context, channel_id, field)
                return
            if action == "marginal_set":
                channel_id, field = int(parts[2]), parts[3]
                if field not in {"header", "footer"} or await self._channel_for_action(channel_id, update.effective_user.id, context.bot) is None:
                    await self._access_denied(update)
                    return
                self._inputs[int(update.effective_user.id)] = {
                    "kind": "marginal_content", "channel_id": channel_id, "field": field,
                    "expires_at": time.time() + self.INPUT_TTL_SECONDS,
                }
                await self._edit_or_send(
                    update,
                    f"Send one message for the {field}: text, photo, video, file, audio, sticker, or a forwarded post. Annie will reuse it automatically. Use /cancel to stop.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data=f"cm:marginals:{channel_id}")]]),
                )
                return
            if action == "marginal_sticker":
                channel_id = int(parts[2])
                if await self._channel_for_action(channel_id, update.effective_user.id, context.bot) is None:
                    await self._access_denied(update)
                    return
                self._inputs[int(update.effective_user.id)] = {
                    "kind": "marginal_content", "channel_id": channel_id, "field": "footer",
                    "expires_at": time.time() + self.INPUT_TTL_SECONDS,
                }
                await self._edit_or_send(
                    update,
                    "Send text, a photo, video, file, audio, sticker, or a forwarded post for Annie to reuse as the footer.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data=f"cm:marginals:{channel_id}")]]),
                )
                return
            if action == "marginal_clear":
                channel_id, field = int(parts[2]), parts[3]
                if field not in {"header", "footer", "footer_sticker"} or await self._channel_for_action(channel_id, update.effective_user.id, context.bot) is None:
                    await self._access_denied(update)
                    return
                repo = self._get_repository()
                if repo is None:
                    await self._storage_error(update)
                    return
                try:
                    fields = ({"header": {}, "header_content": {}, "header_items": [], "header_buttons": []}
                              if field == "header" else
                              {"footer": {}, "footer_content": {}, "footer_sticker": {}, "footer_items": [], "footer_buttons": []})
                    await asyncio.to_thread(repo.update_marginals, channel_id, fields)
                except Exception as exc:
                    logger.warning("[channel-manager] post margin clear failed channel_id=%s field=%s error=%s", channel_id, field, type(exc).__name__)
                    await self._storage_error(update)
                    return
                await self._show_marginals(update, context, channel_id)
                return
            if action == "marginal_type":
                channel_id, media_type = int(parts[2]), parts[3]
                allowed = {"text", "photo", "video", "document", "animation", "audio", "voice"}
                if media_type not in allowed or await self._channel_for_action(channel_id, update.effective_user.id, context.bot) is None:
                    await self._access_denied(update)
                    return
                repo = self._get_repository()
                if repo is None:
                    await self._storage_error(update)
                    return
                settings = await asyncio.to_thread(repo.get_marginals, channel_id)
                types = set(settings.get("types") or [])
                if media_type in types:
                    types.remove(media_type)
                else:
                    types.add(media_type)
                try:
                    await asyncio.to_thread(repo.update_marginals, channel_id, {"types": sorted(types)})
                except Exception as exc:
                    logger.warning("[channel-manager] post margin type update failed channel_id=%s error=%s", channel_id, type(exc).__name__)
                    await self._storage_error(update)
                    return
                await self._show_marginals(update, context, channel_id)
                return
            if action == "channel":
                channel_id = int(parts[2])
                # A preview may be an image/file message. Remove it before
                # opening a text menu so the menu does not stay attached to it.
                query_message = query.message
                if query_message and any(getattr(query_message, kind, None) for kind in self.ALLOWED_MEDIA):
                    await self._delete_message(context.bot, query_message.chat.id, query_message.message_id)
                    channel, actor_member, bot_member = await self._channel_for_menu(
                        channel_id, update.effective_user.id, context.bot
                    )
                    if channel is None:
                        await context.bot.send_message(query_message.chat.id, "This channel is not available to your account.")
                        return
                    name = html_escape(str(channel.get("name") or "Channel"))
                    is_bot_owner = self.owner_user_id is not None and int(update.effective_user.id) == self.owner_user_id
                    can_manage_posts = self._can_post(bot_member) and (is_bot_owner or self._can_post(actor_member))
                    rows = []
                    if can_manage_posts:
                        rows = [
                            [InlineKeyboardButton("✍️ Create post", callback_data=f"cm:create:{channel_id}")],
                            [InlineKeyboardButton("🧩 Header & Footer", callback_data=f"cm:marginals:{channel_id}")],
                            [InlineKeyboardButton("📝 Drafts", callback_data=f"cm:drafts:{channel_id}"), InlineKeyboardButton("🕒 Scheduled", callback_data=f"cm:scheduled:{channel_id}")],
                            [InlineKeyboardButton("📢 Published posts", callback_data=f"cm:published:{channel_id}:0")],
                            [InlineKeyboardButton("📋 Templates", callback_data=f"cm:templates:{channel_id}")],
                            [InlineKeyboardButton("📇 Index Manager", callback_data=f"cm:index:{channel_id}")],
                        ]
                    rows.extend([
                        [InlineKeyboardButton("👥 Channel admins", callback_data=f"cm:admins:{channel_id}")],
                        [InlineKeyboardButton("← Channels", callback_data="cm:home")],
                    ])
                    await context.bot.send_message(
                        query_message.chat.id,
                        f"<b>Channel Manager · {name}</b>\n\n" + ("Create and manage posts, or manage this channel’s admins." if can_manage_posts else "Manage this channel’s admins."),
                        reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML,
                    )
                else:
                    await self._show_channel(update, context, channel_id)
                return
            if action == "create":
                channel_id = int(parts[2])
                if await self._channel_for_action(channel_id, update.effective_user.id, context.bot) is None:
                    await self._access_denied(update)
                    return
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("Use Telegram formatting", callback_data=f"cm:compose:{channel_id}:telegram")],
                    [InlineKeyboardButton("Paste MarkdownV2", callback_data=f"cm:compose:{channel_id}:markdownv2")],
                    self._back_button(f"cm:channel:{channel_id}"),
                ])
                await self._edit_or_send(
                    update,
                    "<b>Create a post</b>\n\nChoose how to format it. Telegram formatting is easiest: use Telegram’s text tools, then send the text or media here. MarkdownV2 is for users who already know its syntax.",
                    markup, ParseMode.HTML,
                )
                return
            if action == "compose":
                channel_id = int(parts[2])
                mode = parts[3]
                await self._begin_composition(update, context, channel_id, mode)
                return
            if action == "compose_begin":
                await self._begin_composition(update, context, int(parts[2]), parts[3])
                return
            if action == "format_help":
                if len(parts) == 3:
                    await self._show_record_format_help(update, context, parts[2])
                else:
                    await self._show_format_help(update, int(parts[2]), parts[3], parts[4] if len(parts) > 4 else None)
                return
            if action == "compose_resume":
                await self._resume_composition(update, context, parts[2])
                return
            if action == "template_new":
                channel_id = int(parts[2])
                if await self._channel_for_action(channel_id, update.effective_user.id, context.bot) is None:
                    await self._access_denied(update)
                    return
                await self._edit_or_send(
                    update,
                    "<b>New template</b>\nChoose how to format the template layout.",
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton("Telegram formatting", callback_data=f"cm:template_begin:{channel_id}:telegram")],
                        [InlineKeyboardButton("MarkdownV2", callback_data=f"cm:template_begin:{channel_id}:markdownv2")],
                        self._back_button(f"cm:templates:{channel_id}"),
                    ]),
                    ParseMode.HTML,
                )
                return
            if action == "template_begin":
                channel_id = int(parts[2])
                mode = parts[3]
                if mode not in {"telegram", "markdownv2"} or await self._channel_for_action(channel_id, update.effective_user.id, context.bot) is None:
                    await self._access_denied(update)
                    return
                self._inputs[int(update.effective_user.id)] = {
                    "kind": "template_name", "channel_id": channel_id,
                    "mode": mode,
                    "expires_at": time.time() + self.INPUT_TTL_SECONDS,
                }
                await self._edit_or_send(update, "Send a short name for this template.", InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data=f"cm:templates:{channel_id}")]]))
                return
            if action == "templates":
                await self._show_templates(update, context, int(parts[2]))
                return
            if action == "template":
                await self._show_record(update, context, parts[2])
                return
            if action == "template_use":
                await self._use_template(update, context, parts[2])
                return
            if action == "template_delete":
                await self._confirm_sensitive_action(update, context, "template_delete", parts[2])
                return
            if action == "confirm_publish":
                await self._publish_draft(update, context, parts[2])
                return
            if action == "confirm_delete":
                await self._delete_draft(update, context, parts[2])
                return
            if action == "confirm_template_delete":
                await self._delete_template(update, context, parts[2])
                return
            if action == "confirm_unschedule":
                await self._unschedule(update, context, parts[2])
                return
            if action == "published":
                await self._show_published_posts(update, context, int(parts[2]), self._callback_page(parts))
                return
            if action == "published_post":
                await self._show_published_post(update, context, parts[2], self._callback_page(parts))
                return
            if action in {"published_edit", "pub_edit"}:
                if len(parts) < 3:
                    await self._access_denied(update)
                    return
                await self._begin_published_edit(update, context, parts[2], self._callback_page(parts))
                return
            if action == "published_preview":
                await self._preview_published_post(update, context, parts[2], self._callback_page(parts))
                return
            if action in {"published_components", "pub_comps"}:
                await self._show_published_components(update, context, parts[2], self._callback_page(parts))
                return
            if action in {"published_component", "pub_comp"}:
                state = self._inputs.get(int(update.effective_user.id)) or {}
                if state.get("record_id") == parts[2] and str(state.get("kind") or "").startswith("published_component_"):
                    self._inputs.pop(int(update.effective_user.id), None)
                await self._show_published_component(update, context, parts[2], int(parts[3]), self._callback_page(parts, 4))
                return
            if action in {"published_component_text", "published_component_media", "pub_comp_txt", "pub_comp_med"}:
                await self._start_published_component_input(
                    update, context, parts[2], int(parts[3]),
                    "text" if action.endswith("text") or action == "pub_comp_txt" else "media", self._callback_page(parts, 4),
                )
                return
            if action in {"published_component_remove", "pub_comp_ask"}:
                record, channel = await self._record_for_user(parts[2], update.effective_user.id, context.bot)
                components = self._published_components(record or {})
                extras = [item for item in components if item.get("kind") != "main"]
                index, page = int(parts[3]), self._callback_page(parts, 4)
                if not record or channel is None or record.get("status") != "published" or not 0 <= index < len(extras):
                    await self._access_denied(update)
                    return
                item = extras[index]
                label = html_escape(str(item.get("kind") or "message").replace("extra", "extra content"))
                await self._edit_or_send(
                    update, f"<b>Remove this {label.lower()} message?</b>\n\nThis deletes it from the channel.",
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton("Remove message", callback_data=f"cm:pub_comp_rm:{parts[2]}:{index}:{page}")],
                        [InlineKeyboardButton("Cancel", callback_data=f"cm:pub_comp:{parts[2]}:{index}:{page}")],
                    ]), ParseMode.HTML,
                )
                return
            if action == "pub_comp_rm":
                await self._remove_published_component(update, context, parts[2], int(parts[3]), self._callback_page(parts, 4))
                return
            if action == "published_delete":
                await self._confirm_sensitive_action(update, context, "published_delete", parts[2], self._callback_page(parts))
                return
            if action == "confirm_published_delete":
                await self._delete_published_post(update, context, parts[2], self._callback_page(parts))
                return
            if action == "cancel_published_delete":
                await self._show_published_post(update, context, parts[2], self._callback_page(parts))
                return
            if action == "confirm_schedule":
                await self._confirm_schedule_submission(update, context, parts[2])
                return
            if action == "cancel_schedule_confirmation":
                await self._cancel_schedule_confirmation(update, context, parts[2])
                return
            if action in {"cancel_publish", "cancel_delete", "cancel_template_delete", "cancel_unschedule"}:
                record_id = parts[2]
                if action == "cancel_publish":
                    if query.message:
                        await self._delete_message(context.bot, query.message.chat.id, query.message.message_id)
                    await self._preview(update, context, record_id)
                else:
                    await self._show_record(update, context, record_id)
                return
            if action == "drafts":
                await self._show_drafts(update, context, int(parts[2]))
                return
            if action == "draft":
                await self._show_post_preview(update, context, parts[2])
                return
            if action == "scheduled":
                await self._show_scheduled(update, context, int(parts[2]))
                return
            if action == "scheduled_post":
                await self._show_record(update, context, parts[2])
                return
            if action == "edit":
                await self._begin_edit(update, context, parts[2])
                return
            if action == "edit_back":
                state = self._inputs.get(int(update.effective_user.id)) or {}
                if state.get("record_id") == parts[2] and str(state.get("kind") or "").startswith("edit_"):
                    self._inputs.pop(int(update.effective_user.id), None)
                await self._show_post_preview(update, context, parts[2])
                return
            if action == "edit_buttons":
                await self._show_post_buttons(update, context, parts[2])
                return
            if action == "edit_replace":
                await self._start_edit_input(update, context, parts[2], "replace")
                return
            if action == "edit_append":
                await self._start_edit_input(update, context, parts[2], "append")
                return
            if action == "edit_text":
                await self._start_edit_input(update, context, parts[2], "text")
                return
            if action == "edit_media":
                await self._start_edit_input(update, context, parts[2], "media")
                return
            if action == "buttons":
                state = self._inputs.get(int(update.effective_user.id)) or {}
                if state.get("record_id") == parts[2] and state.get("kind") in {
                    "button_label", "button_url", "button_edit_label", "button_edit_url"
                }:
                    self._inputs.pop(int(update.effective_user.id), None)
                    if not (query.message and int(state.get("prompt_message_id", -1)) == int(query.message.message_id)):
                        await self._clear_input_prompt(context, state)
                await self._show_post_buttons(update, context, parts[2])
                return
            if action == "button_add":
                await self._start_button_input(update, context, parts[2])
                return
            if action == "button_item":
                await self._show_button_item(update, context, parts[2], int(parts[3]))
                return
            if action == "button_style":
                await self._show_button_style(update, context, parts[2], int(parts[3]))
                return
            if action == "button_set_style":
                await self._set_button_style(update, context, parts[2], int(parts[3]), parts[4])
                return
            if action in {"button_edit_label", "button_edit_url"}:
                await self._start_button_field_input(
                    update, context, parts[2], int(parts[3]),
                    "text" if action == "button_edit_label" else "url",
                )
                return
            if action == "button_remove":
                await self._remove_post_button(update, context, parts[2], int(parts[3]))
                return
            if action == "post_preview":
                await self._show_post_preview(update, context, parts[2])
                return
            if action == "add_followup":
                await self._start_followup_input(update, context, parts[2])
                return
            if action == "followups":
                await self._show_followups(update, context, parts[2])
                return
            if action == "followup_done":
                await self._finish_followup_input(update, context, parts[2])
                return
            if action == "followup_remove":
                await self._remove_followup(update, context, parts[2], int(parts[3]))
                return
            if action == "edit_cancel":
                state = self._inputs.get(int(update.effective_user.id)) or {}
                existing = await asyncio.to_thread(self._get_repository().get, parts[2]) if self._get_repository() else None
                if existing and existing.get("status") == "published":
                    if state.get("record_id") == parts[2]:
                        self._inputs.pop(int(update.effective_user.id), None)
                    await self._begin_published_edit(update, context, parts[2])
                    return
                followup_record = None
                if state.get("record_id") != parts[2] or state.get("kind") != "add_followup":
                    repository = self._get_repository()
                    followup_record = await asyncio.to_thread(repository.get, parts[2]) if repository else None
                    if followup_record and followup_record.get("pending_followups") and query.message:
                        state = {
                            "kind": "add_followup", "record_id": parts[2],
                            "channel_id": int(followup_record["channel_id"]),
                            "prompt_chat_id": int(query.message.chat.id),
                            "prompt_message_id": int(query.message.message_id),
                        }
                followup_cancelled = (
                    state.get("kind") == "add_followup"
                    and state.get("record_id") == parts[2]
                )
                if followup_cancelled and state.get("record_id") == parts[2]:
                    cleared = await self._cancel_followup_input(context, int(update.effective_user.id), state, clear_prompt=False)
                    if not cleared:
                        await update.callback_query.answer("I couldn’t clear the saved extra messages. Please try again.", show_alert=True)
                        return
                elif state.get("record_id") == parts[2]:
                    self._inputs.pop(int(update.effective_user.id), None)
                await self._edit_or_send(
                    update,
                    "Adding extra content was cancelled. Your draft is unchanged." if followup_cancelled else "Edit cancelled. Your saved draft is unchanged.",
                    InlineKeyboardMarkup([[InlineKeyboardButton(
                        "← Back to post" if followup_cancelled else "← Draft options",
                        callback_data=f"cm:post_preview:{parts[2]}" if followup_cancelled else f"cm:draft:{parts[2]}"
                    )]]),
                )
                return
            if action == "caption_add":
                await self._begin_caption_input(update, context, parts[2])
                return
            if action == "caption_skip":
                await self._finish_without_caption(update, context, parts[2])
                return
            if action == "caption_cancel":
                state = self._inputs.get(int(update.effective_user.id)) or {}
                if state.get("record_id") == parts[2] and state.get("kind") in {"await_caption", "caption_input"}:
                    self._inputs.pop(int(update.effective_user.id), None)
                await self._edit_or_send(
                    update, "Caption step cancelled. The post is saved as a draft.",
                    InlineKeyboardMarkup([[InlineKeyboardButton(
                        "Open draft options", callback_data=f"cm:draft:{parts[2]}"
                    )]]),
                )
                return
            if action == "publish":
                await self._confirm_sensitive_action(update, context, "publish", parts[2])
                return
            if action == "retry":
                await self._publish_draft(update, context, parts[2], confirmed_retry=True)
                return
            if action == "keep":
                record, channel = await self._record_for_user(parts[2], update.effective_user.id, context.bot)
                if not record or channel is None:
                    await self._access_denied(update)
                else:
                    await self._show_record(update, context, parts[2])
                return
            if action == "delete":
                await self._confirm_sensitive_action(update, context, "delete", parts[2])
                return
            if action == "share":
                await self._share_draft(update, context, parts[2])
                return
            if action == "save_template":
                await self._begin_save_template(update, context, parts[2])
                return
            if action == "schedule":
                await self._begin_schedule(update, context, parts[2])
                return
            if action == "unschedule":
                await self._confirm_sensitive_action(update, context, "unschedule", parts[2])
                return
            await self._edit_or_send(update, "That Channel Manager action isn’t available. Open the menu and try again.", None)
        except (ValueError, IndexError) as exc:
            logger.warning(
                "[channel-manager] invalid callback user_id=%s callback_data=%r error=%s detail=%s",
                getattr(update.effective_user, "id", None), data,
                type(exc).__name__, str(exc),
            )
            await self._edit_or_send(
                update, "This menu is out of date. Open Channel Manager again and retry.", None
            )
        except Exception as exc:
            logger.exception(
                "[channel-manager] callback failed user_id=%s callback=%s error=%s",
                getattr(update.effective_user, "id", None), data.split(":", 2)[1:2], type(exc).__name__,
            )
            await self._edit_or_send(update, "I couldn’t complete that action. Please try again.", None)

    async def _show_composer_types(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        channel_id: int, mode: str,
    ) -> None:
        if mode not in {"telegram", "markdownv2"} or await self._channel_for_action(
            channel_id, update.effective_user.id, context.bot
        ) is None:
            await self._access_denied(update)
            return
        mode_label = "Telegram formatting" if mode == "telegram" else "MarkdownV2"
        await self._edit_or_send(
            update,
            f"<b>Create a post · {mode_label}</b>\n\nChoose what you’re making. Annie will guide you through the next step.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("Text post", callback_data=f"cm:compose_begin:{channel_id}:{mode}:text")],
                [InlineKeyboardButton("Photo or video", callback_data=f"cm:compose_begin:{channel_id}:{mode}:visual")],
                [InlineKeyboardButton("Document or audio", callback_data=f"cm:compose_begin:{channel_id}:{mode}:file")],
                [InlineKeyboardButton("Formatting examples", callback_data=f"cm:format_help:{channel_id}:{mode}")],
                self._back_button(f"cm:create:{channel_id}"),
            ]),
            ParseMode.HTML,
        )

    async def _show_format_help(
        self, update: Update, channel_id: int, mode: str, record_id: str | None = None,
    ) -> None:
        if mode == "telegram":
            text = (
                "<b>Telegram formatting</b>\n\n"
                "• Type your post in Telegram. Before sending, select words and choose <b>Bold</b>, <i>Italic</i>, or another style.\n"
                "• For a photo or file caption, attach the media and type its caption in the same message.\n"
                "• Annie keeps that formatting in the channel post."
            )
        elif mode == "markdownv2":
            text = (
                "<b>MarkdownV2 formatting</b>\n\n"
                "Add the symbols shown around the words you want to format:\n"
                "• Bold: <code>*bold*</code>\n"
                "• Italic: <code>_italic_</code>\n"
                "• Underline: <code>__underline__</code>\n"
                "• Strikethrough: <code>~removed~</code>\n"
                "• Spoiler: <code>||hidden||</code>\n"
                "• Monospace: <code>`code`</code>\n"
                "• Multiline code: <code>```\ncode here\n```</code>\n"
                "• Clickable text link: <code>[Annie](https://example.com)</code>\n\n"
                "<b>Special symbols</b>\n"
                "MarkdownV2 uses symbols such as <code>_ * [ ] ( ) ~ ` &gt; # + - = | { } . !</code>. "
                "To show one as normal text, put a backslash before it, like <code>\\_</code>.\n\n"
                "To add a button below the post, use the <b>Buttons</b> option after Annie shows the preview."
            )
        else:
            await self._edit_or_send(update, "Choose a supported format.", None)
            return
        await self._edit_or_send(
            update, text,
            InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "← Send post content",
                    callback_data=(f"cm:compose_resume:{record_id}" if record_id else f"cm:compose:{channel_id}:{mode}"),
                )
            ]]),
            ParseMode.HTML,
        )

    async def _show_record_format_help(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, record_id: str,
    ) -> None:
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot)
        if not record or channel is None or record.get("status") != "draft":
            await self._access_denied(update)
            return
        await self._show_format_help(
            update, int(record["channel_id"]),
            str(record.get("format_mode") or "telegram"), record_id,
        )

    async def _begin_composition(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        channel_id: int, mode: str,
    ) -> None:
        if mode not in {"telegram", "markdownv2"}:
            await self._edit_or_send(update, "Choose a supported text format.", None)
            return
        channel = await self._channel_for_action(channel_id, update.effective_user.id, context.bot)
        repository = self._get_repository()
        if channel is None:
            await self._access_denied(update)
            return
        if repository is None:
            await self._storage_error(update)
            return
        try:
            record_id = await asyncio.to_thread(repository.create, {
                "channel_id": channel_id, "kind": "post", "status": "draft",
                "created_by": int(update.effective_user.id), "shared": False,
                "text": "", "caption": "", "entities": [], "media": None,
                "format_mode": mode,
            })
        except Exception as exc:
            logger.warning("[channel-manager] draft create failed channel_id=%s user_id=%s error=%s", channel_id, update.effective_user.id, type(exc).__name__)
            await self._storage_error(update)
            return
        self._inputs[int(update.effective_user.id)] = {
            "kind": "compose", "record_id": record_id, "channel_id": channel_id,
            "mode": mode,
            "prompt_chat_id": int(update.callback_query.message.chat.id),
            "prompt_message_id": int(update.callback_query.message.message_id),
            "expires_at": time.time() + self.INPUT_TTL_SECONDS,
        }
        await self._edit_or_send(
            update,
            "Send your post content here. It can be text, a photo, video, file, sticker, or a forwarded post.\n\nYou can add a caption, more text, or buttons after Annie shows the preview.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("Formatting examples", callback_data=f"cm:format_help:{record_id}")],
                [InlineKeyboardButton("Cancel post", callback_data=f"cm:delete:{record_id}")],
            ]),
        )

    async def _resume_composition(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, record_id: str,
    ) -> None:
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot)
        if not record or channel is None or record.get("status") != "draft":
            await self._access_denied(update)
            return
        mode = str(record.get("format_mode") or "telegram")
        query = update.callback_query
        self._inputs[int(update.effective_user.id)] = {
            "kind": "compose", "record_id": record_id,
            "channel_id": int(record["channel_id"]), "mode": mode,
            "prompt_chat_id": int(query.message.chat.id),
            "prompt_message_id": int(query.message.message_id),
            "expires_at": time.time() + self.INPUT_TTL_SECONDS,
        }
        await self._edit_or_send(
            update,
            "Send your post content here. It can be text, a photo, video, file, sticker, or a forwarded post.\n\nYou can add a caption, more text, or buttons after Annie shows the preview.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("Formatting examples", callback_data=f"cm:format_help:{record_id}")],
                [InlineKeyboardButton("Cancel post", callback_data=f"cm:delete:{record_id}")],
            ]),
        )

    async def _save_post_buttons(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        record: dict[str, Any], buttons: list[dict[str, Any]],
    ) -> bool:
        repository = self._get_repository()
        record_id = str(record.get("_id") or "")
        if record.get("status") == "published":
            message_id = self._published_main_message_id(record)
            if not message_id:
                await update.effective_message.reply_text("Annie can’t find the published post to update its buttons.")
                return False
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=int(record["channel_id"]), message_id=message_id,
                    reply_markup=self._post_keyboard({**record, "post_buttons": buttons}),
                )
            except BadRequest as exc:
                if "message is not modified" not in str(exc).casefold():
                    await update.effective_message.reply_text("Telegram couldn’t update those buttons. The saved settings are unchanged.")
                    return False
            except Exception as exc:
                logger.warning("[channel-manager] published button update failed channel_id=%s post_id=%s error=%s", record.get("channel_id"), record_id, type(exc).__name__)
                await update.effective_message.reply_text("Telegram couldn’t update those buttons. The saved settings are unchanged.")
                return False
        try:
            saved = await asyncio.to_thread(repository.update, record_id, {"post_buttons": buttons}) if repository else False
        except Exception as exc:
            logger.warning("[channel-manager] button save failed channel_id=%s post_id=%s error=%s", record.get("channel_id"), record_id, type(exc).__name__)
            saved = False
        if saved and record.get("status") == "published":
            await self._mirror_edit_to_backups(
                context.bot, int(record["channel_id"]), int(message_id),
                {**record, "post_buttons": buttons},
                reply_markup=self._post_keyboard({**record, "post_buttons": buttons}),
            )
        return bool(saved)

    async def _show_post_buttons(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, record_id: str,
    ) -> None:
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot)
        if not record or channel is None or record.get("status") not in {"draft", "published"}:
            await self._access_denied(update)
            return
        buttons = record.get("post_buttons") or []
        rows = [[InlineKeyboardButton("＋ Add URL button", callback_data=f"cm:button_add:{record_id}")]]
        for index, item in enumerate(buttons):
            label = str(item.get("text") or "Link")[:22]
            rows.append([InlineKeyboardButton(f"🔗 {index + 1}. {label}", callback_data=f"cm:button_item:{record_id}:{index}")])
        back = (f"cm:published_edit:{record_id}:0" if record.get("status") == "published"
                else f"cm:post_preview:{record_id}")
        rows.append([InlineKeyboardButton("← Back to post", callback_data=back)])
        await self._edit_or_send(
            update,
            "<b>Post buttons</b>\nAdd a link button under the post, or tap one to remove it. Telegram shows one button per row.",
            InlineKeyboardMarkup(rows), ParseMode.HTML,
        )

    async def _show_button_item(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        record_id: str, index: int,
    ) -> None:
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot)
        buttons = list((record or {}).get("post_buttons") or [])
        if not record or channel is None or record.get("status") not in {"draft", "published"} or not 0 <= index < len(buttons):
            await self._access_denied(update)
            return
        item = buttons[index]
        rows = [
            [InlineKeyboardButton("Edit label", callback_data=f"cm:button_edit_label:{record_id}:{index}")],
            [InlineKeyboardButton("Change link", callback_data=f"cm:button_edit_url:{record_id}:{index}")],
            [InlineKeyboardButton("Button color", callback_data=f"cm:button_style:{record_id}:{index}")],
            [InlineKeyboardButton("Remove button", callback_data=f"cm:button_remove:{record_id}:{index}")],
            [InlineKeyboardButton("← All buttons", callback_data=f"cm:buttons:{record_id}")],
        ]
        await self._edit_or_send(
            update,
            f"<b>Button {index + 1}</b>\nLabel: {html_escape(str(item.get('text') or ''))}\nLink: {html_escape(str(item.get('url') or ''))}",
            InlineKeyboardMarkup(rows), ParseMode.HTML,
        )

    async def _show_button_style(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        record_id: str, index: int,
    ) -> None:
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot)
        buttons = list((record or {}).get("post_buttons") or [])
        if not record or channel is None or record.get("status") not in {"draft", "published"} or not 0 <= index < len(buttons):
            await self._access_denied(update)
            return
        style = str(buttons[index].get("style") or "default")
        choices = [("default", "Default"), ("primary", "Blue"), ("success", "Green"), ("danger", "Red")]
        rows = [[InlineKeyboardButton(
            f"{'✓ ' if style == value else ''}{label}",
            callback_data=f"cm:button_set_style:{record_id}:{index}:{value}",
        )] for value, label in choices]
        rows.append([InlineKeyboardButton("← Button options", callback_data=f"cm:button_item:{record_id}:{index}")])
        await self._edit_or_send(
            update,
            "<b>URL button color</b>\nChoose a Telegram preset. Colors may look different across Telegram themes and apps.",
            InlineKeyboardMarkup(rows), ParseMode.HTML,
        )

    async def _set_button_style(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        record_id: str, index: int, style: str,
    ) -> None:
        allowed = {"default", "primary", "success", "danger"}
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot)
        buttons = list((record or {}).get("post_buttons") or [])
        if (style not in allowed or not record or channel is None
                or record.get("status") not in {"draft", "published"} or not 0 <= index < len(buttons)):
            await self._access_denied(update)
            return
        if style == "default":
            buttons[index].pop("style", None)
        else:
            buttons[index]["style"] = style
        saved = await self._save_post_buttons(update, context, record, buttons)
        if not saved:
            await self._storage_error(update)
            return
        logger.info("[channel-manager] button color changed channel_id=%s user_id=%s post_id=%s button=%s style=%s", record["channel_id"], update.effective_user.id, record_id, index, style)
        if record.get("status") == "published":
            await self._preview_published_post(update, context, record_id)
        else:
            await self._show_post_preview(update, context, record_id)

    async def _start_button_field_input(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        record_id: str, index: int, field: str,
    ) -> None:
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot)
        buttons = list((record or {}).get("post_buttons") or [])
        if not record or channel is None or record.get("status") not in {"draft", "published"} or not 0 <= index < len(buttons):
            await self._access_denied(update)
            return
        if field not in {"text", "url"}:
            await self._access_denied(update)
            return
        query = update.callback_query
        prompt_message = query.message
        self._inputs[int(update.effective_user.id)] = {
            "kind": "button_edit_label" if field == "text" else "button_edit_url",
            "record_id": record_id, "channel_id": int(record["channel_id"]),
            "button_index": index,
            "prompt_chat_id": int(prompt_message.chat.id),
            "prompt_message_id": int(prompt_message.message_id),
            "expires_at": time.time() + self.INPUT_TTL_SECONDS,
        }
        prompt = "Send the new button label (up to 40 characters)." if field == "text" else "Send the new link, starting with https:// or http://."
        await self._edit_or_send(
            update, prompt,
            InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data=f"cm:buttons:{record_id}")]]),
        )

    async def _start_button_input(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, record_id: str,
    ) -> None:
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot)
        if not record or channel is None or record.get("status") not in {"draft", "published"}:
            await self._access_denied(update)
            return
        if len(record.get("post_buttons") or []) >= 8:
            await self._edit_or_send(update, "A post can have up to 8 link buttons.", None)
            return
        query = update.callback_query
        prompt_message = query.message
        self._inputs[int(update.effective_user.id)] = {
            "kind": "button_label", "record_id": record_id,
            "channel_id": int(record["channel_id"]),
            "prompt_chat_id": int(prompt_message.chat.id), "prompt_message_id": int(prompt_message.message_id),
            "expires_at": time.time() + self.INPUT_TTL_SECONDS,
        }
        await self._edit_or_send(
            update,
            "Send the short label for your link button (up to 40 characters).",
            InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data=f"cm:buttons:{record_id}")]]),
        )

    async def _remove_post_button(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, record_id: str, index: int,
    ) -> None:
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot)
        if not record or channel is None or record.get("status") not in {"draft", "published"}:
            await self._access_denied(update)
            return
        buttons = list(record.get("post_buttons") or [])
        if index < 0 or index >= len(buttons):
            await self._edit_or_send(update, "That button is no longer on this post.", None)
            return
        buttons.pop(index)
        repository = self._get_repository()
        saved = await self._save_post_buttons(update, context, record, buttons)
        if not saved:
            await self._storage_error(update)
            return
        if record.get("status") == "published":
            await self._preview_published_post(update, context, record_id)
        else:
            await self._show_post_buttons(update, context, record_id)

    async def _show_post_preview(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, record_id: str,
    ) -> None:
        query = update.callback_query
        if query and query.message:
            await self._delete_message(context.bot, query.message.chat.id, query.message.message_id)
        await self._preview(update, context, record_id)

    async def _start_followup_input(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, record_id: str,
    ) -> None:
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot)
        if not record or channel is None or record.get("status") != "draft":
            await self._access_denied(update)
            return
        existing = list(record.get("followups") or [])
        pending = list(record.get("pending_followups") or [])
        if len(existing) + len(pending) >= 10:
            await self._edit_or_send(update, "A post can have up to 10 extra messages.", None)
            return
        query_message = update.callback_query.message
        state = {
            "kind": "add_followup", "record_id": record_id,
            "channel_id": int(record["channel_id"]),
            "mode": str(record.get("format_mode") or "telegram"),
            "expires_at": time.time() + self.INPUT_TTL_SECONDS,
        }
        prompt = await query_message.reply_text(
            self._followup_prompt_text(len(pending)),
            reply_markup=self._followup_prompt_markup(record_id),
        )
        state["prompt_chat_id"] = int(prompt.chat.id)
        state["prompt_message_id"] = int(prompt.message_id)
        self._inputs[int(update.effective_user.id)] = state

    @staticmethod
    def _followup_prompt_text(count: int) -> str:
        heading = f"Captured {count} extra message{'s' if count != 1 else ''}.\n\n" if count else ""
        return (
            f"{heading}Send as many extra messages as you need, one at a time. "
            "Text, photos, videos, files, stickers, and forwarded posts are supported. "
            "Tap Done when finished. Annie removes your sent messages after saving them."
        )

    @staticmethod
    def _followup_prompt_markup(record_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Done", callback_data=f"cm:followup_done:{record_id}")],
            [InlineKeyboardButton("Cancel", callback_data=f"cm:edit_cancel:{record_id}")],
        ])

    async def _update_followup_prompt(
        self, context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any], count: int,
    ) -> None:
        try:
            await context.bot.edit_message_text(
                chat_id=int(state["prompt_chat_id"]),
                message_id=int(state["prompt_message_id"]),
                text=self._followup_prompt_text(count),
                reply_markup=self._followup_prompt_markup(str(state["record_id"])),
            )
        except BadRequest as exc:
            if "message is not modified" not in str(exc).casefold():
                logger.info("[channel-manager] followup prompt update skipped post_id=%s error=%s", state.get("record_id"), type(exc).__name__)
        except Exception as exc:
            logger.info("[channel-manager] followup prompt update failed post_id=%s error=%s", state.get("record_id"), type(exc).__name__)

    async def _cancel_followup_input(
        self, context: ContextTypes.DEFAULT_TYPE, user_id: int, state: dict[str, Any],
        clear_prompt: bool = True,
    ) -> bool:
        repository = self._get_repository()
        record_id = str(state.get("record_id") or "")
        try:
            record = await asyncio.to_thread(repository.get, record_id) if repository else None
            pending = list((record or {}).get("pending_followups") or [])
            if record and pending:
                cleared = await asyncio.to_thread(repository.update, record_id, {"pending_followups": []})
                if not cleared:
                    return False
            for item in pending:
                source = item.get("source_message") or {}
                if source.get("chat_id") and source.get("message_id"):
                    await self._delete_message(context.bot, int(source["chat_id"]), int(source["message_id"]))
        except Exception as exc:
            logger.warning("[channel-manager] pending followup cleanup failed user_id=%s post_id=%s error=%s", user_id, record_id, type(exc).__name__)
            return False
        self._inputs.pop(user_id, None)
        if clear_prompt:
            await self._clear_input_prompt(context, state)
        return True

    async def _finish_followup_input(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, record_id: str,
    ) -> None:
        user_id = int(update.effective_user.id)
        state = self._inputs.get(user_id) or {}
        record, channel = await self._record_for_user(record_id, user_id, context.bot)
        pending = list((record or {}).get("pending_followups") or [])
        if not record or channel is None or record.get("status") != "draft":
            await self._access_denied(update)
            return
        if state.get("kind") != "add_followup" or str(state.get("record_id")) != record_id:
            if not pending or not update.callback_query.message:
                await self._access_denied(update)
                return
            state = {
                "kind": "add_followup", "record_id": record_id,
                "channel_id": int(record["channel_id"]),
                "prompt_chat_id": int(update.callback_query.message.chat.id),
                "prompt_message_id": int(update.callback_query.message.message_id),
            }
        if not pending:
            await update.callback_query.answer("Send at least one extra message first.", show_alert=True)
            return
        repository = self._get_repository()
        try:
            saved = await asyncio.to_thread(repository.update, record_id, {
                "followups": [*(record.get("followups") or []), *pending],
                "pending_followups": [],
            }) if repository else False
        except Exception as exc:
            logger.warning("[channel-manager] followup batch commit failed channel_id=%s user_id=%s post_id=%s error=%s", record["channel_id"], user_id, record_id, type(exc).__name__)
            saved = False
        if not saved:
            await update.callback_query.answer("I couldn’t save those messages. Please try again.", show_alert=True)
            return
        self._inputs.pop(user_id, None)
        await self._clear_input_prompt(context, state)
        logger.info("[channel-manager] followup batch saved channel_id=%s user_id=%s post_id=%s count=%s", record["channel_id"], user_id, record_id, len(pending))
        await self._preview(update, context, record_id)

    @staticmethod
    def _followup_label(item: dict[str, Any], index: int) -> str:
        media_type = str((item.get("media") or {}).get("type") or "")
        if media_type:
            return f"{media_type.replace('_', ' ').title()} {index + 1}"
        text = str(item.get("text") or item.get("caption") or "").strip()
        if text:
            line = next((part.strip() for part in text.splitlines() if part.strip()), "Text")
            return f"{line[:32]}"
        return f"Forwarded item {index + 1}"

    async def _show_followups(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, record_id: str,
    ) -> None:
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot)
        if not record or channel is None or record.get("status") != "draft":
            await self._access_denied(update)
            return
        followups = list(record.get("followups") or [])
        rows = [[InlineKeyboardButton(
            f"Remove {self._followup_label(item, index)}"[:60],
            callback_data=f"cm:followup_remove:{record_id}:{index}",
        )] for index, item in enumerate(followups)]
        rows.append([InlineKeyboardButton("← Back to post", callback_data=f"cm:post_preview:{record_id}")])
        await self._edit_or_send(
            update,
            "<b>Extra content</b>\nThese items appear below your main post. Remove any item you don’t want.",
            InlineKeyboardMarkup(rows), ParseMode.HTML,
        )

    async def _remove_followup(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        record_id: str, index: int,
    ) -> None:
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot)
        followups = list((record or {}).get("followups") or [])
        if not record or channel is None or record.get("status") != "draft" or not 0 <= index < len(followups):
            await self._access_denied(update)
            return
        followup = followups.pop(index)
        repository = self._get_repository()
        try:
            saved = await asyncio.to_thread(repository.update, record_id, {"followups": followups}) if repository else False
        except Exception as exc:
            logger.warning("[channel-manager] followup removal failed channel_id=%s user_id=%s post_id=%s error=%s", record["channel_id"], update.effective_user.id, record_id, type(exc).__name__)
            saved = False
        if not saved:
            await self._storage_error(update)
            return
        source_message = followup.get("source_message") or {}
        if source_message.get("chat_id") and source_message.get("message_id"):
            await self._delete_message(context.bot, int(source_message["chat_id"]), int(source_message["message_id"]))
        query = update.callback_query
        if query and query.message:
            await self._delete_message(context.bot, query.message.chat.id, query.message.message_id)
        await self._preview(update, context, record_id)
        logger.info("[channel-manager] followup removed channel_id=%s user_id=%s post_id=%s index=%s", record["channel_id"], update.effective_user.id, record_id, index)

    async def _begin_published_edit(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        record_id: str, page: int = 0,
    ) -> None:
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot)
        if not record or channel is None or record.get("kind") != "post" or record.get("status") != "published":
            await self._access_denied(update)
            return
        main_id = self._published_main_message_id(record)
        if main_id is None:
            await self._edit_or_send(update, "Annie doesn’t have a saved message ID for this post.", None)
            return
        media_type = str((record.get("media") or {}).get("type") or "")
        rows: list[list[InlineKeyboardButton]] = []
        if not record.get("source_message") and media_type not in {"sticker", "video_note"}:
            rows.append([InlineKeyboardButton("Edit text / caption", callback_data=f"cm:edit_text:{record_id}")])
            rows.append([InlineKeyboardButton("Add text / caption", callback_data=f"cm:edit_append:{record_id}")])
        if media_type in {"photo", "video", "animation", "document", "audio"}:
            rows.append([InlineKeyboardButton("Replace media", callback_data=f"cm:edit_media:{record_id}")])
        rows.append([InlineKeyboardButton(
            f"Edit URL buttons ({len(record.get('post_buttons') or [])})",
            callback_data=f"cm:edit_buttons:{record_id}",
        )])
        extra_count = sum(1 for item in self._published_components(record) if item.get("kind") != "main")
        if extra_count:
            rows.append([InlineKeyboardButton(
                f"Edit header, footer & extra content ({extra_count})",
                callback_data=f"cm:published_components:{record_id}:{page}",
            )])
        rows.append([InlineKeyboardButton(
            "Preview post", callback_data=f"cm:published_preview:{record_id}:{page}"
        )])
        rows.append(self._back_button(f"cm:published_post:{record_id}:{page}", "← Published post"))
        await self._edit_or_send(
            update,
            "<b>Edit published post</b>\nChanges update the post in the channel.",
            InlineKeyboardMarkup(rows), ParseMode.HTML,
        )

    async def _delete_published_post(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        record_id: str, page: int = 0,
    ) -> None:
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot)
        if not record or channel is None or record.get("kind") != "post" or record.get("status") != "published":
            await self._access_denied(update)
            return
        message_ids = [int(value) for value in (record.get("published_message_ids") or []) if value]
        if not message_ids and record.get("published_message_id"):
            message_ids = [int(record["published_message_id"])]
        if not message_ids:
            await self._edit_or_send(update, "Annie has no saved message IDs for this post, so it can’t delete it automatically.", None)
            return

        deleted_ids: set[int] = set()
        try:
            if self.delete_channel_messages:
                try:
                    deleted = await self.delete_channel_messages(int(record["channel_id"]), message_ids)
                    if deleted:
                        deleted_ids.update(message_ids)
                except Exception as exc:
                    logger.warning("[channel-manager] MTProto post delete failed channel_id=%s post_id=%s error=%s", record["channel_id"], record_id, type(exc).__name__)
            for message_id in message_ids:
                if message_id in deleted_ids:
                    continue
                try:
                    await context.bot.delete_message(chat_id=int(record["channel_id"]), message_id=message_id)
                    deleted_ids.add(message_id)
                except BadRequest as exc:
                    error_text = str(exc).casefold()
                    if "message to delete not found" in error_text or "message_id_invalid" in error_text:
                        deleted_ids.add(message_id)
                    else:
                        logger.warning("[channel-manager] post message delete failed channel_id=%s post_id=%s message_id=%s error=%s", record["channel_id"], record_id, message_id, type(exc).__name__)
                except Exception as exc:
                    logger.warning("[channel-manager] post message delete failed channel_id=%s post_id=%s message_id=%s error=%s", record["channel_id"], record_id, message_id, type(exc).__name__)

            repository = self._get_repository()
            remaining = [value for value in message_ids if value not in deleted_ids]
            if remaining:
                if repository:
                    await asyncio.to_thread(repository.update, record_id, {"published_message_ids": remaining})
                await self._delete_from_backups(context.bot, int(record["channel_id"]), sorted(deleted_ids))
                await self._edit_or_send(
                    update,
                    f"Annie removed {len(deleted_ids)} of {len(message_ids)} messages. Some could not be deleted; try again or remove them in Telegram.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("← Published post", callback_data=f"cm:published_post:{record_id}:{page}")]]),
                )
                return
            if not repository:
                await self._storage_error(update)
                return
            saved = await asyncio.to_thread(repository.update, record_id, {
                "status": "deleted", "published_message_ids": [],
                "published_message_id": None, "published_main_message_id": None,
                "deleted_at": datetime.now(timezone.utc),
            })
            if not saved:
                await self._edit_or_send(update, "The channel messages were deleted, but Annie couldn’t update the saved record. Reopen the list to refresh it.", None)
                return
            await self._delete_from_backups(context.bot, int(record["channel_id"]), message_ids)
        except Exception as exc:
            logger.exception("[channel-manager] published post deletion failed channel_id=%s user_id=%s post_id=%s error=%s", record.get("channel_id"), update.effective_user.id, record_id, type(exc).__name__)
            await self._edit_or_send(update, "Annie couldn’t finish deleting this post. Check the channel before trying again.", None)
            return
        logger.info("[channel-manager] published post deleted channel_id=%s user_id=%s post_id=%s messages=%s", record["channel_id"], update.effective_user.id, record_id, len(message_ids))
        await self._show_published_posts(update, context, int(record["channel_id"]), page)

    async def _apply_published_content_edit(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        record: dict[str, Any], changes: dict[str, Any],
    ) -> bool:
        repository = self._get_repository()
        record_id = str(record.get("_id") or "")
        channel_id = int(record["channel_id"])
        main_id = self._published_main_message_id(record)
        if not repository or not main_id:
            await update.effective_message.reply_text("Annie can’t find the published message to edit.")
            return False
        updated_record = {**record, **changes}
        media = updated_record.get("media") or {}
        keyboard = self._post_keyboard(updated_record)
        try:
            if "media" in changes:
                media_type = str(media.get("type") or "")
                media_classes = {
                    "photo": InputMediaPhoto,
                    "video": InputMediaVideo,
                    "animation": InputMediaAnimation,
                    "document": InputMediaDocument,
                    "audio": InputMediaAudio,
                }
                media_class = media_classes.get(media_type)
                if media_class is None:
                    await update.effective_message.reply_text(
                        "Telegram can’t replace this post’s media type. The channel post is unchanged."
                    )
                    return False
                send_kwargs = self._send_kwargs(updated_record)
                format_kwargs = {}
                if "parse_mode" in send_kwargs:
                    format_kwargs["parse_mode"] = send_kwargs["parse_mode"]
                if "caption_entities" in send_kwargs:
                    format_kwargs["caption_entities"] = send_kwargs["caption_entities"]
                input_media = media_class(
                    media=str(media.get("file_id") or ""),
                    caption=send_kwargs.get("caption"),
                    **format_kwargs,
                )
                await context.bot.edit_message_media(
                    chat_id=channel_id, message_id=main_id,
                    media=input_media, reply_markup=keyboard,
                )
            elif media:
                media_type = str(media.get("type") or "")
                if media_type in {"sticker", "video_note"}:
                    await update.effective_message.reply_text(
                        "This post type can’t have an edited caption. The channel post is unchanged."
                    )
                    return False
                send_kwargs = self._send_kwargs(updated_record)
                caption_kwargs = {
                    key: send_kwargs[key]
                    for key in ("parse_mode", "caption_entities")
                    if key in send_kwargs
                }
                await context.bot.edit_message_caption(
                    chat_id=channel_id, message_id=main_id,
                    caption=send_kwargs.get("caption") or "",
                    reply_markup=keyboard, **caption_kwargs,
                )
            else:
                send_kwargs = self._send_kwargs(updated_record)
                text_kwargs = {
                    key: send_kwargs[key]
                    for key in ("parse_mode", "entities")
                    if key in send_kwargs
                }
                await context.bot.edit_message_text(
                    chat_id=channel_id, message_id=main_id,
                    text=send_kwargs.get("text") or "",
                    reply_markup=keyboard, **text_kwargs,
                )
        except BadRequest as exc:
            if "message is not modified" not in str(exc).casefold():
                raise
        try:
            saved = await asyncio.to_thread(repository.update, record_id, changes)
        except Exception as exc:
            logger.error(
                "[channel-manager] published post changed but record save failed channel_id=%s user_id=%s post_id=%s error=%s",
                channel_id, update.effective_user.id, record_id, type(exc).__name__,
            )
            await update.effective_message.reply_text(
                "The channel post changed, but Annie couldn’t save the updated details."
            )
            return False
        if not saved:
            await update.effective_message.reply_text(
                "The channel post changed, but Annie couldn’t save the updated details."
            )
            return False
        logger.info(
            "[channel-manager] published post edited channel_id=%s user_id=%s post_id=%s fields=%s",
            channel_id, update.effective_user.id, record_id, ",".join(sorted(changes)),
        )
        await self._mirror_edit_to_backups(
            context.bot, channel_id, main_id, updated_record,
            reply_markup=keyboard,
        )
        return True

    async def _begin_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE, record_id: str) -> None:
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot)
        if not record or channel is None or record.get("status") != "draft":
            await self._access_denied(update)
            return
        rows: list[list[InlineKeyboardButton]] = []
        media_type = str((record.get("media") or {}).get("type") or "")
        editable_text = not record.get("source_message") and media_type not in {"sticker", "video_note"}
        if editable_text:
            rows.append([InlineKeyboardButton("Edit text / caption", callback_data=f"cm:edit_text:{record_id}")])
        if record.get("media"):
            rows.append([InlineKeyboardButton("Replace media", callback_data=f"cm:edit_media:{record_id}")])
        if editable_text:
            rows.append([InlineKeyboardButton("Add text / caption", callback_data=f"cm:edit_append:{record_id}")])
        rows.append([InlineKeyboardButton(
            f"Edit URL buttons ({len(record.get('post_buttons') or [])})",
            callback_data=f"cm:edit_buttons:{record_id}",
        )])
        rows.append([InlineKeyboardButton("Replace entire post", callback_data=f"cm:edit_replace:{record_id}")])
        rows.append([InlineKeyboardButton("Preview post", callback_data=f"cm:edit_back:{record_id}")])
        rows.append([InlineKeyboardButton("Cancel", callback_data=f"cm:edit_cancel:{record_id}")])
        await self._edit_or_send(
            update,
            (
                "<b>Edit this post</b>\nThis forwarded post type can’t be edited part by part. You can still edit its URL buttons, or replace the entire post."
                if record.get("source_message") else
                "<b>Edit this post</b>\nChoose what to change. Use Edit URL buttons to add or change button labels, links, and colors."
            ),
            InlineKeyboardMarkup(rows),
            ParseMode.HTML,
        )

    async def _start_edit_input(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        record_id: str, edit_mode: str,
    ) -> None:
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot)
        published_edit = bool(record and record.get("status") == "published")
        if (not record or channel is None or record.get("status") not in {"draft", "published"}
                or edit_mode not in {"replace", "append", "text", "media"}
                or (published_edit and edit_mode == "replace")):
            await self._access_denied(update)
            return
        if edit_mode == "text" and ((record.get("source_message") and not published_edit) or str((record.get("media") or {}).get("type") or "") in {"sticker", "video_note"}):
            await self._edit_or_send(update, "This post type doesn’t support text editing. You can replace the entire post instead.", None)
            return
        if edit_mode == "media" and not record.get("media") and not published_edit:
            await self._edit_or_send(update, "This draft has no media to replace. Choose Replace entire post to add media.", None)
            return
        prompt_message = update.callback_query.message
        input_kind = {
            "replace": "edit_replace", "append": "edit_append",
            "text": "edit_text", "media": "edit_media",
        }[edit_mode]
        self._inputs[int(update.effective_user.id)] = {
            "kind": input_kind,
            "record_id": record_id, "channel_id": int(record["channel_id"]),
            "published_edit": published_edit,
            "mode": str(record.get("format_mode") or "telegram"),
            "prompt_chat_id": int(prompt_message.chat.id),
            "prompt_message_id": int(prompt_message.message_id),
            "expires_at": time.time() + self.INPUT_TTL_SECONDS,
        }
        prompt = (
            {
                "replace": "Send the new text or media. This replaces the whole post.",
                "append": "Send text to add after the current post. Annie will keep the existing content.",
                "text": "Send the replacement text or caption. The current media and link buttons will stay.",
                "media": "Send a new photo, video, animation, audio, or file. Annie will keep the caption and link buttons.",
            }[edit_mode]
        )
        await self._edit_or_send(
            update, prompt,
            InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data=f"cm:edit_cancel:{record_id}")]]),
        )

    async def _begin_caption_input(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, record_id: str,
    ) -> None:
        user_id = int(update.effective_user.id)
        state = self._inputs.get(user_id)
        record, channel = await self._record_for_user(record_id, user_id, context.bot)
        if not state or state.get("kind") != "await_caption" or state.get("record_id") != record_id or not record or channel is None:
            await self._access_denied(update)
            return
        prompt_message = update.callback_query.message
        state.update({
            "kind": "caption_input",
            "prompt_chat_id": int(prompt_message.chat.id),
            "prompt_message_id": int(prompt_message.message_id),
            "expires_at": time.time() + self.INPUT_TTL_SECONDS,
        })
        await self._edit_or_send(
            update,
            "Send the caption text now. You can format it in Telegram or use the selected MarkdownV2 format.",
            InlineKeyboardMarkup([[InlineKeyboardButton(
                "Cancel caption", callback_data=f"cm:caption_cancel:{record_id}"
            )]]),
        )

    async def _clear_input_prompt(
        self, context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any]
    ) -> None:
        chat_id = state.get("prompt_chat_id")
        message_id = state.get("prompt_message_id")
        if chat_id is None or message_id is None:
            return
        try:
            await context.bot.delete_message(chat_id=int(chat_id), message_id=int(message_id))
        except Exception as exc:
            logger.debug("[channel-manager] composer prompt cleanup skipped error=%s", type(exc).__name__)

    async def _finish_without_caption(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, record_id: str,
    ) -> None:
        user_id = int(update.effective_user.id)
        state = self._inputs.get(user_id)
        record, channel = await self._record_for_user(record_id, user_id, context.bot)
        if not state or state.get("kind") != "await_caption" or state.get("record_id") != record_id or not record or channel is None:
            await self._access_denied(update)
            return
        self._inputs.pop(user_id, None)
        shown = await self._preview(update, context, record_id)
        if shown:
            await self._clear_input_prompt(context, state)

    async def _begin_save_template(self, update: Update, context: ContextTypes.DEFAULT_TYPE, record_id: str) -> None:
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot)
        if not record or channel is None or record.get("status") != "draft":
            await self._access_denied(update)
            return
        self._inputs[int(update.effective_user.id)] = {
            "kind": "save_template", "record_id": record_id,
            "channel_id": int(record["channel_id"]),
            "expires_at": time.time() + self.INPUT_TTL_SECONDS,
        }
        await self._edit_or_send(update, "Send a short name for this template. It will be shared with this channel’s admins.", InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data=f"cm:draft:{record_id}")]]))

    async def _begin_schedule(self, update: Update, context: ContextTypes.DEFAULT_TYPE, record_id: str) -> None:
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot)
        if not record or channel is None or record.get("status") != "draft":
            await self._access_denied(update)
            return
        if record.get("pending_followups"):
            await self._edit_or_send(update, "Finish adding extra messages with Done or Cancel before scheduling this post.", None)
            return
        self._inputs[int(update.effective_user.id)] = {
            "kind": "schedule_time", "record_id": record_id,
            "channel_id": int(record["channel_id"]),
            "expires_at": time.time() + self.INPUT_TTL_SECONDS,
        }
        await self._edit_or_send(
            update,
            "Send the date, time, and timezone. Example: <code>2026-10-08 19:30 Asia/Kolkata</code>.",
            InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data=f"cm:draft:{record_id}")]]),
            ParseMode.HTML,
        )

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Capture only private messages for a current Channel Manager prompt."""
        if not self._is_private(update) or not update.effective_message:
            return
        user_id = int(update.effective_user.id)
        state = self._inputs.get(user_id)
        if not state:
            return
        if float(state.get("expires_at", 0)) < time.time():
            self._inputs.pop(user_id, None)
            await self._clear_input_prompt(context, state)
            await update.effective_message.reply_text(
                "That step expired. Open Channel Manager and start again.",
                reply_markup=ReplyKeyboardRemove() if state.get("kind") == "channel_admin_target" else None,
            )
            return
        if update.effective_message.text and update.effective_message.text.strip().casefold() in {"/cancel", "cancel"}:
            if state.get("kind") == "compose" and state.get("record_id"):
                await self._delete_draft(update, context, str(state["record_id"]))
            elif state.get("kind") == "add_followup":
                cleared = await self._cancel_followup_input(context, user_id, state)
                if not cleared:
                    await update.effective_message.reply_text("I couldn’t clear those extra messages. Please try /cancel again.")
                    return
                await update.effective_message.reply_text("Cancelled. Your saved draft is unchanged.")
            else:
                self._inputs.pop(user_id, None)
                await self._clear_input_prompt(context, state)
                await update.effective_message.reply_text(
                    "Cancelled the admin change." if state.get("kind") == "channel_admin_target" else "Cancelled. Your saved draft is unchanged.",
                    reply_markup=ReplyKeyboardRemove() if state.get("kind") == "channel_admin_target" else None,
                )
            return
        try:
            await self._process_input(update, context, state)
        except Exception as exc:
            logger.exception(
                "[channel-manager] input failed user_id=%s channel_id=%s kind=%s error=%s",
                user_id, state.get("channel_id"), state.get("kind"), type(exc).__name__,
            )
            await update.effective_message.reply_text("I couldn’t save that. Please try again or send /cancel.")

    async def cancel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_private(update):
            await update.effective_message.reply_text("Use /cancel in a private chat with Annie.")
            return
        user_id = int(update.effective_user.id)
        state = self._inputs.get(user_id)
        if state:
            logger.info(
                "[channel-manager] input cancelled user_id=%s channel_id=%s kind=%s",
                user_id, state.get("channel_id"), state.get("kind"),
            )
            if state.get("kind") == "compose" and state.get("record_id"):
                await self._delete_draft(update, context, str(state["record_id"]))
            elif state.get("kind") == "add_followup":
                cleared = await self._cancel_followup_input(context, user_id, state)
                if not cleared:
                    await update.effective_message.reply_text("I couldn’t clear those extra messages. Please try /cancel again.")
                    return
                await update.effective_message.reply_text("Cancelled. Your saved draft is unchanged.")
            else:
                self._inputs.pop(user_id, None)
                await self._clear_input_prompt(context, state)
                await update.effective_message.reply_text(
                    "Cancelled the admin change." if state.get("kind") == "channel_admin_target" else "Cancelled. Your saved draft is unchanged.",
                    reply_markup=ReplyKeyboardRemove() if state.get("kind") == "channel_admin_target" else None,
                )
        else:
            await update.effective_message.reply_text("There’s no Channel Manager step to cancel.")

    async def _process_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any]) -> None:
        user_id = int(update.effective_user.id)
        channel_id = int(state["channel_id"])
        channel = await self._channel_for_action(channel_id, user_id, context.bot)
        if channel is None:
            self._inputs.pop(user_id, None)
            await update.effective_message.reply_text("You no longer have posting access to that channel.")
            return
        repo = self._get_repository()
        if repo is None:
            self._inputs.pop(user_id, None)
            await update.effective_message.reply_text("Channel Manager storage is unavailable. Please try again later.")
            return
        kind = state["kind"]
        message = update.effective_message
        if kind in {"published_component_text", "published_component_media"}:
            record, channel = await self._record_for_user(str(state["record_id"]), user_id, context.bot)
            if not record or channel is None or record.get("status") != "published":
                self._inputs.pop(user_id, None)
                await message.reply_text("That published post is no longer available.")
                return
            content = self._content_from_message(message, "telegram")
            if not content:
                await message.reply_text("Send text, a photo, video, animation, audio, or a file.")
                return
            mode = "text" if kind.endswith("_text") else "media"
            if mode == "text":
                text_value = str(content.get("caption") or content.get("text") or "")
                if content.get("media") or content.get("source_message") or not text_value.strip():
                    await message.reply_text("Send text only for this message.")
                    return
                components = [item for item in self._published_components(record) if item.get("kind") != "main"]
                component_index = int(state.get("component_index", -1))
                component_data = (components[component_index].get("data") or {}) if 0 <= component_index < len(components) else {}
                limit = 1024 if component_data.get("media") else 4096
                if len(text_value) > limit:
                    await message.reply_text(f"Keep the text under {limit} characters.")
                    return
                content = {"text": text_value, "entities": list(content.get("entities") or [])}
            elif not content.get("media"):
                await message.reply_text("Send a photo, video, animation, audio, or file to replace this message.")
                return
            if await self._apply_published_component_edit(
                update, context, record, int(state.get("component_index", -1)),
                content, mode, int(state.get("page", 0)),
            ):
                self._inputs.pop(user_id, None)
                await self._delete_message(context.bot, message.chat.id, message.message_id)
                await self._clear_input_prompt(context, state)
                await self._preview_published_post(
                    update, context, str(state["record_id"]), int(state.get("page", 0)),
                )
            return
        if kind == "channel_admin_target":
            target_id = None
            shared = getattr(message, "users_shared", None)
            if shared and getattr(shared, "user_ids", None):
                target_id = int(shared.user_ids[0])
            if target_id is None:
                for entity in getattr(message, "entities", None) or []:
                    if getattr(entity, "type", None) == MessageEntity.TEXT_MENTION and getattr(entity, "user", None):
                        target_id = int(entity.user.id)
                        break
            if target_id is None:
                raw = (message.text or "").strip()
                if re.fullmatch(r"\d+", raw):
                    target_id = int(raw)
            if target_id is None:
                await message.reply_text("Choose a user with Select Telegram user, mention them by tapping their name, or send their numeric user ID. A username by itself can’t be resolved.")
                return
            operation = str(state.get("operation") or "")
            self._inputs.pop(user_id, None)
            await message.reply_text("Checking channel membership…", reply_markup=ReplyKeyboardRemove())
            await self._confirm_admin_change(update, context, channel_id, target_id, operation)
            return
        if kind == "await_caption":
            await message.reply_text("Tap Add caption, or continue without one, using the buttons above.")
            return
        if kind in {"marginal_content", "marginal_sticker"}:
            content = self._content_from_message(message, "telegram")
            if content is None:
                await message.reply_text("Send text, a photo, video, file, audio, sticker, or a forwarded post.")
                return
            field = str(state.get("field") or "")
            if field not in {"header", "footer"}:
                self._inputs.pop(user_id, None)
                await message.reply_text("That setting expired. Open Header & Footer and try again.")
                return
            input_id = content.pop("input_message_id", None)
            preserved_copy_id: int | None = None
            if content.get("source_message"):
                source = content["source_message"]
                try:
                    copied = await context.bot.copy_message(
                        chat_id=user_id, from_chat_id=int(source["chat_id"]),
                        message_id=int(source["message_id"]),
                    )
                    preserved_copy_id = int(copied.message_id)
                    content["source_message"] = {"chat_id": user_id, "message_id": int(copied.message_id)}
                except Exception as exc:
                    logger.warning("[channel-manager] marginal content preservation failed channel_id=%s user_id=%s type=%s error=%s", channel_id, user_id, field, type(exc).__name__)
                    await message.reply_text("I couldn’t save that item for reuse. Try sending it directly or forwarding it again.")
                    return
            value = str(content.get("text") or content.get("caption") or "")
            max_length = 1024 if content.get("media") else 4096
            if len(value) > max_length:
                await message.reply_text(f"That message is too long. Telegram allows up to {max_length} characters here.")
                return
            try:
                await asyncio.to_thread(repo.add_marginal_item, channel_id, field, content)
                saved = True
            except Exception as exc:
                logger.warning("[channel-manager] post margin save failed channel_id=%s user_id=%s field=%s error=%s", channel_id, user_id, field, type(exc).__name__)
                saved = False
            if not saved:
                if preserved_copy_id:
                    await self._delete_message(context.bot, user_id, preserved_copy_id)
                await message.reply_text("I couldn’t save that content. Please try again.")
                return
            self._inputs.pop(user_id, None)
            if input_id and preserved_copy_id:
                await self._delete_message(context.bot, message.chat.id, int(input_id))
            await message.reply_text(
                f"{field.title()} saved for {html_escape(str(channel.get('name') or 'Channel'))}. Annie will add it automatically.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Open settings", callback_data=f"cm:marginals:{channel_id}")]]),
            )
            logger.info("[channel-manager] post margin saved channel_id=%s user_id=%s field=%s", channel_id, user_id, field)
            return
        if kind == "marginal_button_label":
            label = (message.text or "").strip()
            if not label or len(label) > 40:
                await message.reply_text("Send a button label between 1 and 40 characters.")
                return
            state["button_label"] = label
            state["kind"] = "marginal_button_url"
            state["expires_at"] = time.time() + self.INPUT_TTL_SECONDS
            prompt = await message.reply_text(
                "Now send the full link, starting with https:// or http://.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                    "Cancel", callback_data=f"cm:marginal_buttons:{channel_id}:{state['field']}"
                )]]),
            )
            state["prompt_chat_id"] = int(prompt.chat.id)
            state["prompt_message_id"] = int(prompt.message_id)
            return
        if kind == "marginal_button_url":
            url = (message.text or "").strip()
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or any(character.isspace() for character in url):
                await message.reply_text("That link doesn’t look right. Send a full http:// or https:// link.")
                return
            field = str(state.get("field") or "")
            if field not in {"header", "footer"}:
                self._inputs.pop(user_id, None)
                await message.reply_text("That step expired. Open Channel Manager and try again.")
                return
            try:
                await asyncio.to_thread(repo.add_marginal_button, channel_id, field, {
                    "text": str(state["button_label"]), "url": url,
                })
            except Exception as exc:
                logger.warning("[channel-manager] marginal button save failed channel_id=%s user_id=%s field=%s error=%s", channel_id, user_id, field, type(exc).__name__)
                await message.reply_text("I couldn’t save that button. Please try again.")
                return
            self._inputs.pop(user_id, None)
            await self._clear_input_prompt(context, state)
            await message.reply_text("Link button saved. Annie will place it under the last item in this header or footer.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Open settings", callback_data=f"cm:marginal_buttons:{channel_id}:{field}")]]))
            return
        if kind == "caption_input":
            content = self._content_from_message(message, str(state.get("mode") or "telegram"))
            caption = str((content or {}).get("text") or "")
            if not caption.strip() or (content or {}).get("media"):
                await message.reply_text("Send caption text, or tap Cancel caption.")
                return
            if len(caption) > 1024:
                await message.reply_text("Telegram captions can be up to 1,024 characters. Please shorten it.")
                return
            try:
                updated = await asyncio.to_thread(repo.update, str(state["record_id"]), {
                    "caption": caption,
                    "entities": list((content or {}).get("entities") or []),
                })
            except Exception as exc:
                logger.warning("[channel-manager] draft caption save failed channel_id=%s user_id=%s post_id=%s error=%s", channel_id, user_id, state.get("record_id"), type(exc).__name__)
                updated = False
            if not updated:
                await message.reply_text("I couldn’t save the caption. Please try again.")
                return
            self._inputs.pop(user_id, None)
            previewed = await self._preview(update, context, str(state["record_id"]))
            if previewed:
                await self._clear_input_prompt(context, state)
            return
        if kind == "template_name":
            name = (message.text or "").strip()
            if not name or len(name) > 60:
                await message.reply_text("Send a template name between 1 and 60 characters.")
                return
            state["kind"] = "template_body"
            state["name"] = name
            state["expires_at"] = time.time() + self.INPUT_TTL_SECONDS
            await message.reply_text("Now send the template layout. You can use fill-in labels such as {{Title}} and {{Author}}.")
            return
        if kind == "template_body":
            content = self._content_from_message(message, str(state.get("mode") or "telegram"))
            if not content or not (content.get("text") or content.get("caption") or "").strip():
                await message.reply_text("A template needs some text. Send the layout again, or /cancel.")
                return
            try:
                record_id = await asyncio.to_thread(repo.create, {
                    **content, "channel_id": channel_id, "kind": "template", "status": "active",
                    "name": str(state["name"]), "created_by": user_id, "shared": True,
                })
            except Exception as exc:
                logger.warning("[channel-manager] template save failed channel_id=%s user_id=%s error=%s", channel_id, user_id, type(exc).__name__)
                self._inputs.pop(user_id, None)
                await message.reply_text("I couldn’t save that template. Please try again later.")
                return
            self._inputs.pop(user_id, None)
            logger.info("[channel-manager] template saved channel_id=%s user_id=%s template_id=%s", channel_id, user_id, record_id)
            await message.reply_text("Template saved for this channel’s admins.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Templates", callback_data=f"cm:templates:{channel_id}")]]))
            return
        if kind == "button_label":
            label = (message.text or "").strip()
            if not label or len(label) > 40:
                await message.reply_text("Send a button label between 1 and 40 characters.")
                return
            state["button_label"] = label
            state["kind"] = "button_url"
            state["expires_at"] = time.time() + self.INPUT_TTL_SECONDS
            prompt = await message.reply_text(
                "Now send the full link, starting with https:// or http://.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data=f"cm:buttons:{state['record_id']}")]]),
            )
            await self._clear_input_prompt(context, state)
            state["prompt_chat_id"] = int(prompt.chat.id)
            state["prompt_message_id"] = int(prompt.message_id)
            return
        if kind == "button_url":
            url = (message.text or "").strip()
            parsed = urlparse(url)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or any(character.isspace() for character in url)):
                await message.reply_text("That link doesn’t look right. Send a full http:// or https:// link.")
                return
            current, current_channel = await self._record_for_user(str(state["record_id"]), user_id, context.bot)
            if not current or current_channel is None or current.get("status") not in {"draft", "published"}:
                self._inputs.pop(user_id, None)
                await message.reply_text("That post is no longer available.")
                return
            buttons = list(current.get("post_buttons") or [])
            if len(buttons) >= 8:
                self._inputs.pop(user_id, None)
                await message.reply_text("This post already has 8 buttons, the Telegram limit used here.")
                return
            buttons.append({"text": str(state["button_label"]), "url": url})
            saved = await self._save_post_buttons(update, context, current, buttons)
            if not saved:
                await message.reply_text("I couldn’t save that button. Please try again.")
                return
            record_id = str(state["record_id"])
            self._inputs.pop(user_id, None)
            if current.get("status") == "published":
                await self._delete_message(context.bot, message.chat.id, message.message_id)
                await self._clear_input_prompt(context, state)
                await self._preview_published_post(update, context, record_id)
                return
            previewed = await self._preview(update, context, record_id)
            if previewed:
                await self._clear_input_prompt(context, state)
            return
        if kind in {"button_edit_label", "button_edit_url"}:
            value = (message.text or "").strip()
            field = "text" if kind == "button_edit_label" else "url"
            if field == "text" and (not value or len(value) > 40):
                await message.reply_text("Send a button label between 1 and 40 characters.")
                return
            if field == "url":
                parsed = urlparse(value)
                if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                        or any(character.isspace() for character in value)):
                    await message.reply_text("That link doesn’t look right. Send a full http:// or https:// link.")
                    return
            current, current_channel = await self._record_for_user(str(state["record_id"]), user_id, context.bot)
            buttons = list((current or {}).get("post_buttons") or [])
            index = int(state.get("button_index", -1))
            if not current or current_channel is None or current.get("status") not in {"draft", "published"} or not 0 <= index < len(buttons):
                self._inputs.pop(user_id, None)
                await message.reply_text("That button or draft is no longer available.")
                return
            buttons[index][field] = value
            try:
                saved = await self._save_post_buttons(update, context, current, buttons)
            except Exception as exc:
                logger.warning("[channel-manager] button edit failed channel_id=%s user_id=%s post_id=%s error=%s", channel_id, user_id, state["record_id"], type(exc).__name__)
                saved = False
            if not saved:
                await message.reply_text("I couldn’t save that button change. Please try again.")
                return
            record_id = str(state["record_id"])
            self._inputs.pop(user_id, None)
            if current.get("status") == "published":
                await self._delete_message(context.bot, message.chat.id, message.message_id)
                await self._clear_input_prompt(context, state)
                await self._preview_published_post(update, context, record_id)
                return
            previewed = await self._preview(update, context, record_id)
            if previewed:
                await self._delete_message(context.bot, message.chat.id, message.message_id)
                await self._clear_input_prompt(context, state)
            return
        if kind == "add_followup":
            content = self._content_from_message(message, str(state.get("mode") or "telegram"))
            if content is None:
                await message.reply_text("Send text, a photo, video, file, sticker, or forward a post.")
                return
            input_message_id = content.pop("input_message_id", None)
            if len(str(content.get("text") or content.get("caption") or "")) > (1024 if content.get("media") else 4096):
                await message.reply_text("That text is too long for one Telegram message. Shorten it and send again.")
                return
            current, current_channel = await self._record_for_user(str(state["record_id"]), user_id, context.bot)
            pending = list((current or {}).get("pending_followups") or [])
            followups = list((current or {}).get("followups") or [])
            if not current or current_channel is None or current.get("status") != "draft":
                self._inputs.pop(user_id, None)
                await message.reply_text("That draft is no longer available.")
                return
            if len(followups) + len(pending) >= 10:
                await message.reply_text("This draft already has 10 extra messages. Remove one before adding another.")
                return
            copied_source = None
            if content.get("source_message"):
                source = content["source_message"]
                try:
                    copied = await context.bot.copy_message(
                        chat_id=user_id,
                        from_chat_id=int(source["chat_id"]),
                        message_id=int(source["message_id"]),
                    )
                    copied_source = {
                        "chat_id": user_id,
                        "message_id": int(copied.message_id),
                    }
                    content["source_message"] = copied_source
                except Exception as exc:
                    logger.warning("[channel-manager] followup preservation copy failed channel_id=%s user_id=%s post_id=%s error=%s", channel_id, user_id, state["record_id"], type(exc).__name__)
                    await message.reply_text("I couldn’t safely save that forwarded item. It’s still in the chat; try forwarding it again.")
                    return
            pending.append(content)
            try:
                saved = await asyncio.to_thread(repo.update, str(state["record_id"]), {"pending_followups": pending})
            except Exception as exc:
                logger.warning("[channel-manager] followup save failed channel_id=%s user_id=%s post_id=%s error=%s", channel_id, user_id, state["record_id"], type(exc).__name__)
                saved = False
            if not saved:
                if copied_source:
                    await self._delete_message(context.bot, copied_source["chat_id"], copied_source["message_id"])
                await message.reply_text("I couldn’t save that extra item. The draft is unchanged; please try again.")
                return
            state["expires_at"] = time.time() + self.INPUT_TTL_SECONDS
            await self._delete_message(context.bot, message.chat.id, int(input_message_id or message.message_id))
            await self._update_followup_prompt(context, state, len(pending))
            logger.info("[channel-manager] followup staged channel_id=%s user_id=%s post_id=%s type=%s count=%s", channel_id, user_id, state["record_id"], (content.get("media") or {}).get("type") or ("forwarded" if content.get("source_message") else "text"), len(pending))
            return
        if kind == "save_template":
            name = (message.text or "").strip()
            if not name or len(name) > 60:
                await message.reply_text("Send a template name between 1 and 60 characters.")
                return
            source, _ = await self._record_for_user(str(state["record_id"]), user_id, context.bot)
            if not source:
                self._inputs.pop(user_id, None)
                await message.reply_text("That draft is no longer available.")
                return
            template = {
                key: source.get(key) for key in ("text", "caption", "entities", "format_mode", "media")
            }
            try:
                template_id = await asyncio.to_thread(repo.create, {
                    **template, "channel_id": channel_id, "kind": "template", "status": "active",
                    "name": name, "created_by": user_id, "shared": True,
                })
            except Exception as exc:
                logger.warning("[channel-manager] template save failed channel_id=%s user_id=%s error=%s", channel_id, user_id, type(exc).__name__)
                self._inputs.pop(user_id, None)
                await message.reply_text("I couldn’t save that template. Please try again later.")
                return
            self._inputs.pop(user_id, None)
            logger.info("[channel-manager] template saved from draft channel_id=%s user_id=%s template_id=%s", channel_id, user_id, template_id)
            await message.reply_text("Template saved for this channel’s admins.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Templates", callback_data=f"cm:templates:{channel_id}")]]))
            return
        if kind in {"compose", "edit_replace", "edit_append", "edit_text", "edit_media"}:
            content = self._content_from_message(message, str(state.get("mode") or "telegram"))
            if content is None:
                await message.reply_text("Send text or one supported media item.")
                return
            input_message_id = content.pop("input_message_id", None)
            if kind == "edit_text":
                current = await asyncio.to_thread(repo.get, str(state["record_id"]))
                published_edit = bool(state.get("published_edit"))
                if not current or (current.get("source_message") and not published_edit):
                    await message.reply_text("This post type can’t be edited as text. Replace the entire post instead.")
                    return
                media_type = str((current.get("media") or {}).get("type") or "")
                if media_type in {"sticker", "video_note"}:
                    await message.reply_text("Telegram doesn’t support captions on stickers or video notes. This draft is unchanged.")
                    return
                if content.get("media") or content.get("source_message") or not str(content.get("text") or "").strip():
                    await message.reply_text("Send text only. To change the image or file, choose Replace media.")
                    return
                field = "caption" if current.get("media") else "text"
                content = {
                    field: str(content.get("text") or ""),
                    "entities": list(content.get("entities") or []),
                }
            elif kind == "edit_media":
                current = await asyncio.to_thread(repo.get, str(state["record_id"]))
                if not current or (not current.get("media") and not state.get("published_edit")):
                    await message.reply_text("There’s no media in this draft to replace.")
                    return
                if not content.get("media"):
                    await message.reply_text("Send one photo, video, or file to replace the current media.")
                    return
                new_media_type = str(content["media"].get("type") or "")
                media_fields: dict[str, Any] = {
                    "media": content["media"], "source_message": None,
                }
                if new_media_type in {"sticker", "video_note"}:
                    media_fields.update({"caption": "", "entities": []})
                content = media_fields
            if kind == "edit_append" and (current := await asyncio.to_thread(repo.get, str(state["record_id"]))) is not None:
                media_type = str((current.get("media") or {}).get("type") or "")
                if current.get("source_message") and not state.get("published_edit"):
                    await message.reply_text("Annie can’t add text to this Telegram post type. Replace it with text or media to edit its content.")
                    return
                if media_type in {"sticker", "video_note"}:
                    await message.reply_text("Telegram doesn’t support captions on stickers or video notes. This draft is unchanged.")
                    return
            if kind == "edit_append" and (content.get("media") or not str(content.get("text") or "").strip()):
                await message.reply_text("Add text only. To replace the post with new media, choose Replace post.")
                return
            if kind == "edit_append":
                current = await asyncio.to_thread(repo.get, str(state["record_id"]))
                if not current:
                    await message.reply_text("That draft is no longer available.")
                    self._inputs.pop(user_id, None)
                    return
                incoming = str(content.get("text") or "")
                field = "caption" if current.get("media") else "text"
                existing = str(current.get(field) or "")
                separator = "\n" if existing and incoming else ""
                offset_units = len((existing + separator).encode("utf-16-le")) // 2
                old_entities = list(current.get("entities") or [])
                new_entities = [dict(item, offset=int(item.get("offset", 0)) + offset_units)
                                for item in (content.get("entities") or [])]
                content = {
                    field: existing + separator + incoming,
                    "entities": old_entities + new_entities,
                }
            current_record = await asyncio.to_thread(repo.get, str(state["record_id"]))
            if not current_record or current_record.get("status") != (
                "published" if state.get("published_edit") else "draft"
            ):
                await message.reply_text("That post is no longer available to edit.")
                self._inputs.pop(user_id, None)
                return
            has_media = bool((current_record or {}).get("media")) if kind in {"edit_text", "edit_append"} else bool(content.get("media"))
            if len(content.get("text") or content.get("caption") or "") > (1024 if has_media else 4096):
                await message.reply_text("That text is too long for one Telegram post. Shorten it or use a separate message.")
                return
            if state.get("published_edit"):
                updated = await self._apply_published_content_edit(
                    update, context, current_record, content,
                )
                if not updated:
                    return
                self._inputs.pop(user_id, None)
                if input_message_id:
                    await self._delete_message(context.bot, message.chat.id, int(input_message_id))
                await self._clear_input_prompt(context, state)
                await self._preview_published_post(update, context, str(state["record_id"]))
                return
            try:
                updated = await asyncio.to_thread(repo.update, str(state["record_id"]), content)
            except Exception as exc:
                logger.warning("[channel-manager] draft content save failed channel_id=%s user_id=%s post_id=%s error=%s", channel_id, user_id, state.get("record_id"), type(exc).__name__)
                updated = False
            if not updated:
                await message.reply_text("I couldn’t save the post. Your existing draft is unchanged; please try again.")
                return
            self._inputs.pop(user_id, None)
            logger.info("[channel-manager] draft content saved channel_id=%s user_id=%s post_id=%s", channel_id, user_id, state["record_id"])
            previewed = await self._preview(update, context, str(state["record_id"]))
            if previewed:
                if input_message_id and not content.get("source_message"):
                    await self._delete_message(context.bot, message.chat.id, int(input_message_id))
                await self._clear_input_prompt(context, state)
            return
        if kind == "schedule_time":
            raw = (message.text or "").strip()
            match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s+([A-Za-z0-9_+./-]+)", raw)
            if not match:
                await message.reply_text("Use this format: 2026-10-08 19:30 Asia/Kolkata. Include the timezone.")
                return
            try:
                zone = ZoneInfo(match.group(3))
                scheduled_at = datetime.strptime(
                    f"{match.group(1)} {match.group(2)}", "%Y-%m-%d %H:%M"
                ).replace(tzinfo=zone).astimezone(timezone.utc)
            except (ValueError, ZoneInfoNotFoundError):
                await message.reply_text("I couldn’t read that date or timezone. Try a name such as Asia/Kolkata or UTC.")
                return
            if scheduled_at < datetime.now(timezone.utc) + timedelta(minutes=1):
                await message.reply_text("Choose a time at least one minute in the future.")
                return
            record_id = str(state["record_id"])
            try:
                updated = await asyncio.to_thread(repo.update, record_id, {
                    "pending_scheduled_at": scheduled_at,
                    "pending_timezone": match.group(3),
                })
            except Exception as exc:
                logger.warning("[channel-manager] schedule confirmation save failed channel_id=%s user_id=%s post_id=%s error=%s", channel_id, user_id, record_id, type(exc).__name__)
                updated = False
            if not updated:
                await message.reply_text("I couldn’t save that schedule time. Your draft is still saved.")
                return
            self._inputs.pop(user_id, None)
            local_time = scheduled_at.astimezone(zone).strftime("%Y-%m-%d %H:%M %Z")
            await self._clear_input_prompt(context, state)
            logger.info("[channel-manager] schedule awaiting confirmation channel_id=%s user_id=%s post_id=%s scheduled_at=%s", channel_id, user_id, record_id, scheduled_at.isoformat())
            await message.reply_text(
                f"Schedule this post for <b>{html_escape(local_time)}</b>?",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Confirm schedule", callback_data=f"cm:confirm_schedule:{record_id}")],
                    [InlineKeyboardButton("Go back", callback_data=f"cm:cancel_schedule_confirmation:{record_id}")],
                ]),
            )
            return

    async def _use_template(self, update: Update, context: ContextTypes.DEFAULT_TYPE, template_id: str) -> None:
        template, channel = await self._record_for_user(template_id, update.effective_user.id, context.bot)
        if not template or template.get("kind") != "template" or channel is None:
            await self._access_denied(update)
            return
        repo = self._get_repository()
        if repo is None:
            await self._storage_error(update)
            return
        fields = {key: template.get(key) for key in ("text", "caption", "entities", "format_mode", "media")}
        try:
            draft_id = await asyncio.to_thread(repo.create, {
                **fields, "channel_id": int(template["channel_id"]), "kind": "post", "status": "draft",
                "created_by": int(update.effective_user.id), "shared": False,
                "started_from_template": str(template["_id"]),
            })
        except Exception as exc:
            logger.warning("[channel-manager] template use failed channel_id=%s user_id=%s template_id=%s error=%s", template["channel_id"], update.effective_user.id, template_id, type(exc).__name__)
            await self._storage_error(update)
            return
        logger.info("[channel-manager] template selected channel_id=%s user_id=%s template_id=%s post_id=%s", template["channel_id"], update.effective_user.id, template_id, draft_id)
        await self._preview(update, context, draft_id)

    async def _delete_template(self, update: Update, context: ContextTypes.DEFAULT_TYPE, record_id: str) -> None:
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot)
        if not record or record.get("kind") != "template" or channel is None:
            await self._access_denied(update)
            return
        repo = self._get_repository()
        try:
            deleted = await asyncio.to_thread(repo.delete, record_id) if repo else False
        except Exception as exc:
            logger.warning("[channel-manager] template delete failed channel_id=%s user_id=%s template_id=%s error=%s", record["channel_id"], update.effective_user.id, record_id, type(exc).__name__)
            deleted = False
        if deleted:
            logger.info("[channel-manager] template deleted channel_id=%s user_id=%s template_id=%s", record["channel_id"], update.effective_user.id, record_id)
        await self._show_templates(update, context, int(record["channel_id"]))

    async def _publish_draft(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        record_id: str,
        confirmed_retry: bool = False,
    ) -> None:
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot)
        if not record or record.get("kind") != "post" or channel is None:
            await self._access_denied(update)
            return
        if record.get("status") not in {"draft", "failed", "needs_review"}:
            await self._edit_or_send(update, "This post has already been scheduled or published.", None)
            return
        if record.get("pending_followups"):
            await self._edit_or_send(update, "Finish adding extra messages with Done or Cancel before publishing this post.", None)
            return
        if record.get("status") == "needs_review" and not confirmed_retry:
            await self._edit_or_send(
                update,
                "The last send may have reached Telegram. Check the channel before retrying.",
                InlineKeyboardMarkup([[InlineKeyboardButton(
                    "I checked the channel — retry", callback_data=f"cm:retry:{record_id}"
                )]]),
            )
            return
        if record_id in self._ambiguous_send_ids and not confirmed_retry:
            await self._edit_or_send(
                update,
                "An earlier send had an unclear result. Check the channel before retrying.",
                InlineKeyboardMarkup([[InlineKeyboardButton(
                    "I checked the channel — retry", callback_data=f"cm:retry:{record_id}"
                )]]),
            )
            return
        repo = self._get_repository()
        if repo is None:
            await self._storage_error(update)
            return
        try:
            claimed = await asyncio.to_thread(repo.claim_publish, record_id, confirmed_retry)
        except Exception as exc:
            logger.warning("[channel-manager] publish claim failed channel_id=%s user_id=%s post_id=%s error=%s", record["channel_id"], update.effective_user.id, record_id, type(exc).__name__)
            await self._storage_error(update)
            return
        if not claimed:
            await self._edit_or_send(
                update,
                "This post is already being sent or is no longer ready to publish.",
                InlineKeyboardMarkup([[InlineKeyboardButton("Channel Manager", callback_data=f"cm:channel:{record['channel_id']}")]]),
            )
            return
        record = claimed
        try:
            settings = record.get("marginal_snapshot")
            if not isinstance(settings, dict):
                settings = await asyncio.to_thread(repo.get_marginals, int(record["channel_id"]))
            sent_messages = await self._send_post_messages(
                context.bot, int(record["channel_id"]), record, marginal_settings=settings
            )
        except PartialPostSendError as exc:
            self._ambiguous_send_ids.add(record_id)
            try:
                await asyncio.to_thread(repo.update, record_id, {
                    "status": "needs_review", "publishing_at": None,
                    "published_message_ids": exc.sent_message_ids,
                    "last_error": "Only some post messages were sent. Check the channel before retrying.",
                })
            except Exception as state_exc:
                logger.warning("[channel-manager] partial publish state save failed channel_id=%s post_id=%s error=%s", record["channel_id"], record_id, type(state_exc).__name__)
            logger.error("[channel-manager] partial post sent channel_id=%s user_id=%s post_id=%s sent=%s cause=%s", record["channel_id"], update.effective_user.id, record_id, len(exc.sent_message_ids), type(exc.cause).__name__)
            await self._edit_or_send(
                update,
                "Telegram received only part of this post. Check the channel before retrying to avoid duplicates.",
                InlineKeyboardMarkup([[InlineKeyboardButton("I checked the channel — retry", callback_data=f"cm:retry:{record_id}")]]),
            )
            return
        except RetryAfter as exc:
            seconds = int(getattr(exc, "retry_after", 5))
            await asyncio.to_thread(repo.update, record_id, {"status": "draft", "publishing_at": None})
            await self._edit_or_send(update, f"Telegram asked Annie to wait {seconds} seconds. Your draft is safe; try again shortly.", None)
            return
        except BadRequest as exc:
            logger.warning("[channel-manager] publish rejected channel_id=%s user_id=%s post_id=%s error=%s", record["channel_id"], update.effective_user.id, record_id, type(exc).__name__)
            await asyncio.to_thread(repo.update, record_id, {"status": "draft", "publishing_at": None})
            await self._edit_or_send(update, "Telegram rejected this post. Check its formatting, media, or Annie’s channel posting permission; your draft is still saved.", None)
            return
        except (TimedOut, NetworkError) as exc:
            self._ambiguous_send_ids.add(record_id)
            try:
                await asyncio.to_thread(repo.update, record_id, {
                    "status": "needs_review",
                    "publishing_at": None,
                    "last_error": "Connection lost during send; check the channel before retrying.",
                })
            except Exception as state_exc:
                logger.warning("[channel-manager] uncertain publish state save failed channel_id=%s user_id=%s post_id=%s error=%s", record["channel_id"], update.effective_user.id, record_id, type(state_exc).__name__)
            logger.warning("[channel-manager] publish outcome uncertain channel_id=%s user_id=%s post_id=%s error=%s", record["channel_id"], update.effective_user.id, record_id, type(exc).__name__)
            await self._edit_or_send(
                update,
                "Annie lost the connection while publishing. Check the channel before retrying to avoid a duplicate.",
                InlineKeyboardMarkup([[InlineKeyboardButton(
                    "I checked the channel — retry", callback_data=f"cm:retry:{record_id}"
                )]]),
            )
            return
        except Exception as exc:
            logger.exception("[channel-manager] publish failed channel_id=%s user_id=%s post_id=%s error=%s", record["channel_id"], update.effective_user.id, record_id, type(exc).__name__)
            try:
                await asyncio.to_thread(repo.update, record_id, {"status": "draft", "publishing_at": None})
            except Exception as state_exc:
                logger.warning("[channel-manager] publish failure state save failed channel_id=%s user_id=%s post_id=%s error=%s", record["channel_id"], update.effective_user.id, record_id, type(state_exc).__name__)
            await self._edit_or_send(update, "I couldn’t publish that post. Your draft is still saved.", None)
            return
        try:
            sent_ids = [int(item.message_id) for item in sent_messages if getattr(item, "message_id", None)]
            receipt_saved = await asyncio.to_thread(repo.update, record_id, {
                "status": "published", "published_at": datetime.now(timezone.utc),
                "published_message_id": sent_ids[0] if sent_ids else None,
                "published_main_message_id": self._published_main_message_id({
                    **record, "published_message_ids": sent_ids,
                    "marginal_snapshot": settings,
                }),
                "published_message_ids": sent_ids, "last_error": None,
                "marginal_snapshot": settings,
                "publishing_at": None,
            })
        except Exception as exc:
            logger.error("[channel-manager] publish receipt save failed channel_id=%s user_id=%s post_id=%s error=%s", record["channel_id"], update.effective_user.id, record_id, type(exc).__name__)
            receipt_saved = False
        logger.info("[channel-manager] post published channel_id=%s user_id=%s post_id=%s messages=%s", record["channel_id"], update.effective_user.id, record_id, len(sent_ids))
        backup_failures = 0
        if receipt_saved:
            backup_failures = await self._auto_forward_published(
                context.bot, int(record["channel_id"]), sent_messages,
                {**record, "marginal_snapshot": settings},
            )
        preview_ids = list(record.get("preview_message_ids") or [])
        if not preview_ids and record.get("preview_message_id"):
            preview_ids = [record["preview_message_id"]]
        if record.get("preview_chat_id"):
            for preview_id in preview_ids:
                await self._delete_message(context.bot, int(record["preview_chat_id"]), int(preview_id))
        source_messages = [record.get("source_message"), *[
            item.get("source_message") for item in (record.get("followups") or [])
        ]]
        for source_message in source_messages:
            source_message = source_message or {}
            if source_message.get("chat_id") and source_message.get("message_id"):
                await self._delete_message(context.bot, int(source_message["chat_id"]), int(source_message["message_id"]))
        result_text = (
            "Published to the channel."
            if receipt_saved else
            "Published, but Annie couldn’t save the receipt. Check the channel before retrying to avoid a duplicate."
        )
        if backup_failures:
            result_text += f" Auto-forward missed {backup_failures} message(s); check the backup channel and Annie’s permissions."
        await self._edit_or_send(update, result_text, InlineKeyboardMarkup([[InlineKeyboardButton("Channel Manager", callback_data=f"cm:channel:{record['channel_id']}")]]))

    async def _delete_draft(self, update: Update, context: ContextTypes.DEFAULT_TYPE, record_id: str) -> None:
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot, allow_shared=False)
        if not record or record.get("kind") != "post" or channel is None or record.get("status") != "draft":
            await self._access_denied(update)
            return
        repo = self._get_repository()
        try:
            deleted = await asyncio.to_thread(repo.delete, record_id) if repo else False
        except Exception as exc:
            logger.warning("[channel-manager] draft delete failed channel_id=%s user_id=%s post_id=%s error=%s", record["channel_id"], update.effective_user.id, record_id, type(exc).__name__)
            deleted = False
        if deleted:
            preview_chat_id = record.get("preview_chat_id")
            preview_ids = list(record.get("preview_message_ids") or [])
            if not preview_ids and record.get("preview_message_id"):
                preview_ids = [record["preview_message_id"]]
            if preview_chat_id:
                for preview_id in preview_ids:
                    await self._delete_message(context.bot, int(preview_chat_id), int(preview_id))
            source_messages = [record.get("source_message"), *[
                item.get("source_message") for item in [
                    *(record.get("followups") or []),
                    *(record.get("pending_followups") or []),
                ]
            ]]
            for source_message in source_messages:
                source_message = source_message or {}
                if source_message.get("chat_id") and source_message.get("message_id"):
                    await self._delete_message(context.bot, int(source_message["chat_id"]), int(source_message["message_id"]))
            state = self._inputs.get(int(update.effective_user.id)) or {}
            if state.get("record_id") == record_id:
                self._inputs.pop(int(update.effective_user.id), None)
                query = update.callback_query
                if not (query and query.message and
                        int(state.get("prompt_message_id", -1)) == int(query.message.message_id)):
                    await self._clear_input_prompt(context, state)
            logger.info("[channel-manager] draft deleted channel_id=%s user_id=%s post_id=%s", record["channel_id"], update.effective_user.id, record_id)
            query = update.callback_query
            if query and query.message and any(getattr(query.message, kind, None) for kind in self.ALLOWED_MEDIA):
                await self._delete_message(context.bot, query.message.chat.id, query.message.message_id)
                await context.bot.send_message(
                    chat_id=query.message.chat.id, text="Draft deleted.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Drafts", callback_data=f"cm:drafts:{record['channel_id']}")]]),
                )
            else:
                await self._edit_or_send(update, "Draft deleted.", InlineKeyboardMarkup([[InlineKeyboardButton("← Drafts", callback_data=f"cm:drafts:{record['channel_id']}")]]))
        else:
            await self._storage_error(update)

    async def _share_draft(self, update: Update, context: ContextTypes.DEFAULT_TYPE, record_id: str) -> None:
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot, allow_shared=False)
        if not record or channel is None or record.get("status") != "draft":
            await self._access_denied(update)
            return
        repo = self._get_repository()
        shared = not bool(record.get("shared"))
        try:
            saved = await asyncio.to_thread(repo.update, record_id, {"shared": shared}) if repo else False
        except Exception as exc:
            logger.warning("[channel-manager] draft share update failed channel_id=%s user_id=%s post_id=%s error=%s", record["channel_id"], update.effective_user.id, record_id, type(exc).__name__)
            saved = False
        if not saved:
            await self._storage_error(update)
            return
        logger.info("[channel-manager] draft sharing changed channel_id=%s user_id=%s post_id=%s shared=%s", record["channel_id"], update.effective_user.id, record_id, shared)
        await self._show_record(update, context, record_id)

    async def _unschedule(self, update: Update, context: ContextTypes.DEFAULT_TYPE, record_id: str) -> None:
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot)
        if not record or channel is None or record.get("status") not in {"scheduled", "failed", "needs_review"}:
            await self._access_denied(update)
            return
        repo = self._get_repository()
        try:
            saved = await asyncio.to_thread(repo.update, record_id, {"status": "draft", "scheduled_at": None}) if repo else False
        except Exception as exc:
            logger.warning("[channel-manager] schedule cancel failed channel_id=%s user_id=%s post_id=%s error=%s", record["channel_id"], update.effective_user.id, record_id, type(exc).__name__)
            saved = False
        if saved:
            logger.info("[channel-manager] schedule cancelled channel_id=%s user_id=%s post_id=%s", record["channel_id"], update.effective_user.id, record_id)
            await self._show_drafts(update, context, int(record["channel_id"]))
        else:
            await self._storage_error(update)

    async def _confirm_schedule_submission(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, record_id: str,
    ) -> None:
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot)
        scheduled_at = (record or {}).get("pending_scheduled_at")
        timezone_name = str((record or {}).get("pending_timezone") or "UTC")
        if not record or channel is None or record.get("status") != "draft" or not scheduled_at:
            await self._access_denied(update)
            return
        if scheduled_at < datetime.now(timezone.utc) + timedelta(minutes=1):
            repo = self._get_repository()
            if repo:
                await asyncio.to_thread(repo.update, record_id, {
                    "pending_scheduled_at": None, "pending_timezone": None,
                })
            await self._edit_or_send(update, "That time has passed. Open the draft and choose a new schedule time.", None)
            return
        repo = self._get_repository()
        try:
            updated = await asyncio.to_thread(repo.update, record_id, {
                "status": "scheduled", "scheduled_at": scheduled_at,
                "timezone": timezone_name, "last_error": None,
                "pending_scheduled_at": None, "pending_timezone": None,
            }) if repo else False
        except Exception as exc:
            logger.warning("[channel-manager] schedule confirmation failed channel_id=%s user_id=%s post_id=%s error=%s", record["channel_id"], update.effective_user.id, record_id, type(exc).__name__)
            updated = False
        if not updated:
            await self._storage_error(update)
            return
        try:
            zone = ZoneInfo(timezone_name)
            local_time = scheduled_at.astimezone(zone).strftime("%Y-%m-%d %H:%M %Z")
        except Exception:
            local_time = scheduled_at.strftime("%Y-%m-%d %H:%M UTC")
        logger.info("[channel-manager] post scheduled channel_id=%s user_id=%s post_id=%s scheduled_at=%s", record["channel_id"], update.effective_user.id, record_id, scheduled_at.isoformat())
        await self._edit_or_send(
            update, f"Scheduled for {html_escape(local_time)}.",
            InlineKeyboardMarkup([[InlineKeyboardButton("Scheduled posts", callback_data=f"cm:scheduled:{record['channel_id']}")]]),
            ParseMode.HTML,
        )

    async def _cancel_schedule_confirmation(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, record_id: str,
    ) -> None:
        record, channel = await self._record_for_user(record_id, update.effective_user.id, context.bot)
        if not record or channel is None or record.get("status") != "draft":
            await self._access_denied(update)
            return
        repo = self._get_repository()
        try:
            saved = await asyncio.to_thread(repo.update, record_id, {
                "pending_scheduled_at": None, "pending_timezone": None,
            }) if repo else False
        except Exception as exc:
            logger.warning("[channel-manager] schedule confirmation cancel failed channel_id=%s user_id=%s post_id=%s error=%s", record["channel_id"], update.effective_user.id, record_id, type(exc).__name__)
            saved = False
        if not saved:
            await self._storage_error(update)
            return
        await self._show_record(update, context, record_id)

    async def run_scheduler(self, bot: Any) -> None:
        """Poll durable due posts; each item is atomically claimed before sending."""
        repo: MongoChannelManagementRepository | None = None
        while repo is None:
            try:
                repo = self._get_repository()
                if repo is None:
                    await asyncio.sleep(60)
                    continue
                await asyncio.to_thread(repo.ensure_indexes)
                interrupted = await asyncio.to_thread(repo.mark_interrupted_sends)
                if interrupted:
                    logger.warning("[channel-manager] interrupted sends need review count=%s", interrupted)
                logger.info("[channel-manager] scheduler started")
            except asyncio.CancelledError:
                logger.info("[channel-manager] scheduler stopped before startup")
                raise
            except Exception as exc:
                repo = None
                logger.warning("[channel-manager] scheduler startup failed; retrying error=%s", type(exc).__name__)
                await asyncio.sleep(30)
        while True:
            try:
                record = await asyncio.to_thread(repo.claim_due, datetime.now(timezone.utc))
                if record:
                    await self._publish_scheduled(bot, repo, record)
                    continue
            except asyncio.CancelledError:
                logger.info("[channel-manager] scheduler stopped")
                raise
            except Exception as exc:
                logger.warning("[channel-manager] scheduler poll failed error=%s", type(exc).__name__)
            await asyncio.sleep(10)

    async def _publish_scheduled(self, bot: Any, repo: MongoChannelManagementRepository, record: dict[str, Any]) -> None:
        channel_id = int(record["channel_id"])
        record_id = str(record["_id"])
        try:
            channels = await self._connected_channels()
            if channel_id not in {int(item["id"]) for item in channels}:
                await asyncio.to_thread(repo.finish_schedule, record["_id"], "failed", {"last_error": "Channel is no longer connected or approved."})
                logger.warning("[channel-manager] scheduled post blocked channel_id=%s post_id=%s reason=not_connected_or_approved", channel_id, record_id)
                return
            bot_member = await bot.get_chat_member(channel_id, bot.id)
            if not self._can_post(bot_member):
                await asyncio.to_thread(repo.finish_schedule, record["_id"], "failed", {"last_error": "Annie no longer has channel posting permission."})
                logger.warning("[channel-manager] scheduled post blocked channel_id=%s post_id=%s reason=bot_permission", channel_id, record_id)
                await self._notify_creator(bot, record, "Annie no longer has permission to post there. The scheduled post was not sent.")
                return
            settings = record.get("marginal_snapshot")
            if not isinstance(settings, dict):
                settings = await asyncio.to_thread(repo.get_marginals, channel_id)
            sent_messages = await self._send_post_messages(
                bot, channel_id, record, marginal_settings=settings
            )
            sent_ids = [int(item.message_id) for item in sent_messages if getattr(item, "message_id", None)]
            try:
                await asyncio.to_thread(repo.finish_schedule, record["_id"], "published", {
                    "published_at": datetime.now(timezone.utc),
                    "published_message_id": sent_ids[0] if sent_ids else None,
                    "published_main_message_id": self._published_main_message_id({
                        **record, "published_message_ids": sent_ids,
                        "marginal_snapshot": settings,
                    }),
                    "published_message_ids": sent_ids, "last_error": None,
                    "marginal_snapshot": settings,
                })
            except Exception as exc:
                logger.error("[channel-manager] scheduled post receipt save failed channel_id=%s post_id=%s error=%s", channel_id, record_id, type(exc).__name__)
                await self._notify_creator(bot, record, "Your scheduled post was sent, but Annie couldn’t save the receipt. Check the channel before retrying.")
                return
            logger.info("[channel-manager] scheduled post published channel_id=%s post_id=%s messages=%s", channel_id, record_id, len(sent_ids))
            backup_failures = await self._auto_forward_published(
                bot, channel_id, sent_messages, {**record, "marginal_snapshot": settings}
            )
            notification = "Your scheduled post was published."
            if backup_failures:
                notification += f" Auto-forward missed {backup_failures} message(s); check the backup channel and Annie’s permissions."
            await self._notify_creator(bot, record, notification)
        except PartialPostSendError as exc:
            await asyncio.to_thread(repo.finish_schedule, record["_id"], "needs_review", {
                "published_message_ids": exc.sent_message_ids,
                "last_error": "Only some post messages were sent. Check the channel before retrying.",
            })
            logger.error("[channel-manager] scheduled post partially sent channel_id=%s post_id=%s sent=%s cause=%s", channel_id, record_id, len(exc.sent_message_ids), type(exc.cause).__name__)
            await self._notify_creator(bot, record, "Telegram received only part of your scheduled post. Check the channel before retrying to avoid duplicates.")
        except RetryAfter as exc:
            delay = max(5, int(getattr(exc, "retry_after", 10)))
            next_attempt = datetime.now(timezone.utc) + timedelta(seconds=delay)
            await asyncio.to_thread(repo.finish_schedule, record["_id"], "scheduled", {"scheduled_at": next_attempt, "last_error": "Telegram rate limit; automatically delayed."})
            logger.warning("[channel-manager] scheduled post delayed channel_id=%s post_id=%s retry_after=%s", channel_id, record_id, delay)
        except BadRequest as exc:
            await asyncio.to_thread(repo.finish_schedule, record["_id"], "failed", {"last_error": "Telegram rejected the post. Check its formatting or media."})
            logger.warning("[channel-manager] scheduled post rejected channel_id=%s post_id=%s error=%s", channel_id, record_id, type(exc).__name__)
            await self._notify_creator(bot, record, "Telegram rejected a scheduled post. Check its formatting, media, or Annie’s permissions.")
        except (TimedOut, NetworkError) as exc:
            await asyncio.to_thread(repo.finish_schedule, record["_id"], "needs_review", {"last_error": "Connection lost during send; check the channel before retrying."})
            logger.warning("[channel-manager] scheduled post outcome uncertain channel_id=%s post_id=%s error=%s", channel_id, record_id, type(exc).__name__)
            await self._notify_creator(bot, record, "Annie lost the connection while publishing a scheduled post. Check the channel before retrying to avoid a duplicate.")
        except Exception as exc:
            try:
                await asyncio.to_thread(repo.finish_schedule, record["_id"], "failed", {"last_error": "An unexpected error stopped this scheduled post."})
            except Exception as storage_exc:
                logger.error("[channel-manager] failed to persist schedule error state channel_id=%s post_id=%s error=%s", channel_id, record_id, type(storage_exc).__name__)
            logger.exception("[channel-manager] scheduled post failed channel_id=%s post_id=%s error=%s", channel_id, record_id, type(exc).__name__)
            await self._notify_creator(bot, record, "Annie couldn’t publish a scheduled post. It’s marked for review in Channel Manager.")

    @staticmethod
    async def _notify_creator(bot: Any, record: dict[str, Any], text: str) -> None:
        try:
            await bot.send_message(chat_id=int(record["created_by"]), text=text)
        except Exception as exc:
            logger.info("[channel-manager] creator notification unavailable user_id=%s post_id=%s error=%s", record.get("created_by"), str(record.get("_id")), type(exc).__name__)
