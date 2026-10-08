"""Persistent settings and isolated MTProto listener for channel indexes."""

from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from pymongo import MongoClient


class MongoChannelIndexRepository:
    """Persist per-channel index settings and the posts Annie adds to indexes."""

    def __init__(self, uri: str, database_name: str = "annie_db") -> None:
        self.client = MongoClient(
            uri,
            appname="AnnieSearchChannelIndexes",
            serverSelectionTimeoutMS=3000,
            connectTimeoutMS=3000,
            socketTimeoutMS=5000,
            waitQueueTimeoutMS=3000,
            maxPoolSize=5,
        )
        self.configs = self.client[database_name]["bot_channel_indexes"]
        self.channel_manager_records = self.client[database_name]["bot_channel_manager_records"]

    def is_channel_manager_main_post(self, channel_id: int, message_id: int) -> bool:
        """Only allow Annie-authored posts that Channel Manager recorded as main content."""
        return self.channel_manager_records.find_one(
            {
                "channel_id": int(channel_id),
                "published_main_message_id": int(message_id),
                "kind": "post",
                "status": "published",
            },
            {"_id": 1},
        ) is not None

    def get(self, channel_id: int) -> dict[str, Any] | None:
        return self.configs.find_one({"_id": int(channel_id)})

    def list_enabled(self) -> list[dict[str, Any]]:
        return list(self.configs.find(
            {"enabled": True},
            {"_id": 1, "entries": 1, "pending_posts": 1, "targets": 1},
        ))

    def update(self, channel_id: int, fields: dict[str, Any]) -> None:
        fields = dict(fields)
        fields["updated_at"] = datetime.now(timezone.utc)
        self.configs.update_one(
            {"_id": int(channel_id)}, {"$set": fields}, upsert=True
        )

    def add_excluded_sender(
        self, channel_id: int, sender_id: int, label: str | None = None
    ) -> bool:
        update_fields: dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
        if label:
            update_fields[f"excluded_sender_labels.{int(sender_id)}"] = str(label)[:80]
        result = self.configs.update_one(
            {"_id": int(channel_id)},
            {"$addToSet": {"excluded_sender_ids": int(sender_id)},
             "$set": update_fields},
            upsert=True,
        )
        return bool(result.modified_count or result.upserted_id is not None)

    def remove_excluded_sender(self, channel_id: int, sender_id: int) -> None:
        self.configs.update_one(
            {"_id": int(channel_id)},
            {"$pull": {"excluded_sender_ids": int(sender_id)},
             "$unset": {f"excluded_sender_labels.{int(sender_id)}": ""},
             "$set": {"updated_at": datetime.now(timezone.utc)}},
        )

    def set_targets(self, channel_id: int, targets: list[dict[str, Any]], append: bool) -> None:
        channel_id = int(channel_id)
        if append:
            current = self.get(channel_id) or {}
            combined = list(current.get("targets") or [])
            seen = {int(item["message_id"]) for item in combined}
            combined.extend(
                target for target in targets if int(target["message_id"]) not in seen
            )
            targets = combined
        fields: dict[str, Any] = {"targets": targets}
        if not append:
            fields["entries"] = []
        self.update(channel_id, fields)

    def schedule_post(self, channel_id: int, post: dict[str, Any]) -> bool:
        result = self.configs.update_one(
            {"_id": int(channel_id), "pending_posts.message_id": {"$ne": int(post["message_id"])}},
            {"$push": {"pending_posts": post}},
        )
        return result.modified_count > 0

    def remove_pending(self, channel_id: int, post_id: int) -> None:
        self.configs.update_one(
            {"_id": int(channel_id)},
            {"$pull": {"pending_posts": {"message_id": int(post_id)}}},
        )

    def update_pending_post(
        self, channel_id: int, post_id: int, title: str,
        category_marker: str | None, categorized: bool,
    ) -> bool:
        result = self.configs.update_one(
            {"_id": int(channel_id), "pending_posts.message_id": int(post_id)},
            {"$set": {"pending_posts.$.title": str(title),
                      "pending_posts.$.category_marker": category_marker,
                      "pending_posts.$.categorized": bool(categorized),
                      "updated_at": datetime.now(timezone.utc)}},
        )
        return result.modified_count > 0

    def clear_pending(self, channel_id: int) -> None:
        self.configs.update_one({"_id": int(channel_id)}, {"$set": {"pending_posts": []}})

    def close(self) -> None:
        self.client.close()


class ChannelIndexRuntime:
    """Run a bot-authenticated MTProto update listener beside polling only.

    It is deliberately independent from the Bot API polling transport. If
    MTProto cannot start, the ordinary bot continues running normally.
    """

    MAX_INDEX_TEXT = 4096

    @staticmethod
    def _excluded_sender_ids(config: dict[str, Any]) -> set[int]:
        excluded: set[int] = set()
        for value in config.get("excluded_sender_ids") or []:
            try:
                sender_id = int(value)
            except (TypeError, ValueError):
                continue
            if sender_id > 0:
                excluded.add(sender_id)
        return excluded

    def __init__(self, bot: Any, repository: MongoChannelIndexRepository, api_id: int, api_hash: str, token: str) -> None:
        self.bot = bot
        self.repository = repository
        self.api_id = api_id
        self.api_hash = api_hash
        self.token = token
        self.client: Any = None
        self.bot_user_id: int | None = None
        self._pending: dict[tuple[int, int], asyncio.Task] = {}
        self._retry_counts: dict[tuple[int, int], int] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        from telethon import TelegramClient, events
        from telethon.sessions import StringSession

        self.client = TelegramClient(
            StringSession(), self.api_id, self.api_hash,
            receive_updates=True, auto_reconnect=True,
        )
        await self.client.start(bot_token=self.token)
        me = await self.client.get_me()
        self.bot_user_id = int(me.id)
        self.client.add_event_handler(self._on_new_post, events.NewMessage())
        self.client.add_event_handler(self._on_edited_post, events.MessageEdited())
        self.client.add_event_handler(self._on_deleted_posts, events.MessageDeleted())
        for config in await asyncio.to_thread(self.repository.list_enabled):
            if not await self._reconcile_saved_entries(config):
                # Do not replay persisted work if Telegram could not verify it.
                continue
            for pending in config.get("pending_posts") or []:
                self._schedule(
                    int(config["_id"]), int(pending["message_id"]),
                    str(pending["title"]), pending.get("due_at"),
                )

    async def _messages_by_id(self, channel_id: int, message_ids: list[int]) -> dict[int, Any]:
        """Fetch specific channel messages; unlike history, this is bot-supported."""
        found: dict[int, Any] = {}
        unique_ids = list(dict.fromkeys(int(value) for value in message_ids))
        for start in range(0, len(unique_ids), 100):
            batch = unique_ids[start:start + 100]
            result = await self.client.get_messages(int(channel_id), ids=batch)
            if result is None:
                continue
            messages = result if isinstance(result, (list, tuple)) else [result]
            for message in messages:
                message_id = getattr(message, "id", None)
                if message_id is not None:
                    found[int(message_id)] = message
        return found

    async def get_sender_details(
        self, channel_id: int, message_id: int
    ) -> tuple[int | None, str | None]:
        result = await self.client.get_messages(int(channel_id), ids=int(message_id))
        if isinstance(result, (list, tuple)):
            result = result[0] if result else None
        sender_id = getattr(result, "sender_id", None)
        try:
            sender_id = int(sender_id) if sender_id is not None else None
        except (TypeError, ValueError):
            sender_id = None
        label = str(getattr(result, "post_author", None) or "").strip() or None
        return sender_id, label

    async def resolve_sender_username(self, username: str) -> tuple[int | None, str | None]:
        from telethon.tl.types import User

        entity = await self.client.get_entity(username)
        if not isinstance(entity, User) or int(getattr(entity, "id", 0)) <= 0:
            return None, None
        sender_id = int(entity.id)
        label = str(getattr(entity, "username", None) or username).strip()
        return sender_id, f"@{label.lstrip('@')}"

    @staticmethod
    def _message_text_urls(message: Any) -> set[str]:
        return {
            str(url)
            for entity in (getattr(message, "entities", None) or [])
            if (url := getattr(entity, "url", None))
        }

    async def _reconcile_saved_entries(self, config: dict[str, Any]) -> bool:
        """Drop deleted sources and entries removed from their index post while offline."""
        from src.utils import logger
        from telethon.tl.types import MessageEmpty

        channel_id = int(config["_id"])
        entries = list(config.get("entries") or [])
        pending = list(config.get("pending_posts") or [])
        targets = list(config.get("targets") or [])
        message_ids = [int(entry.get("source_message_id", -1)) for entry in entries]
        message_ids.extend(int(item.get("message_id", -1)) for item in pending)
        message_ids.extend(int(target.get("message_id", -1)) for target in targets)
        message_ids = [message_id for message_id in message_ids if message_id > 0]
        if not message_ids:
            return True
        try:
            messages = await self._messages_by_id(channel_id, message_ids)
        except Exception as exc:
            logger.warning(
                "[channel-index] saved-entry check failed channel_id=%s error=%s; pending entries will not be replayed",
                channel_id, type(exc).__name__,
            )
            return False

        kept_entries = []
        removed_entries = 0
        for entry in entries:
            source_id = int(entry.get("source_message_id", -1))
            source = messages.get(source_id)
            target = messages.get(int(entry.get("target_message_id", -1)))
            if source is None or isinstance(source, MessageEmpty):
                removed_entries += 1
                continue
            # If the placeholder still exists, its live link entities tell us
            # whether an admin removed this entry manually while Annie was off.
            if target is not None and not isinstance(target, MessageEmpty):
                target_urls = self._message_text_urls(target)
                entry_url = str(entry.get("url") or "")
                if entry_url and entry_url not in target_urls:
                    removed_entries += 1
                    continue
            kept_entries.append(entry)

        kept_pending = []
        removed_pending = 0
        for item in pending:
            source = messages.get(int(item.get("message_id", -1)))
            if source is None or isinstance(source, MessageEmpty):
                removed_pending += 1
                continue
            kept_pending.append(item)

        fields: dict[str, Any] = {}
        if removed_entries:
            fields["entries"] = kept_entries
        if removed_pending:
            fields["pending_posts"] = kept_pending
        if fields:
            await asyncio.to_thread(self.repository.update, channel_id, fields)
            logger.info(
                "[channel-index] reconciled saved entries channel_id=%s removed_entries=%s removed_pending=%s",
                channel_id, removed_entries, removed_pending,
            )
        return True

    async def close(self) -> None:
        tasks = list(self._pending.values())
        for task in tasks:
            task.cancel()
        self._pending.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.client is not None:
            await self.client.disconnect()
            self.client = None

    async def cancel_channel(self, channel_id: int) -> None:
        for key, task in list(self._pending.items()):
            if key[0] == int(channel_id):
                task.cancel()
                self._pending.pop(key, None)
                self._retry_counts.pop(key, None)
        await asyncio.to_thread(self.repository.clear_pending, int(channel_id))

    async def resume_pending(self, channel_id: int) -> None:
        """Resume delayed posts after an admin registers a new index slot."""
        config = await asyncio.to_thread(self.repository.get, int(channel_id))
        if not config or not config.get("enabled"):
            return
        target_ids = {
            int(target.get("message_id", -1))
            for target in config.get("targets") or []
        }
        for pending in config.get("pending_posts") or []:
            post_id = int(pending["message_id"])
            key = (int(channel_id), post_id)
            if post_id in target_ids:
                task = self._pending.pop(key, None)
                if task:
                    task.cancel()
                await asyncio.to_thread(self.repository.remove_pending, channel_id, post_id)
                continue
            self._schedule(
                int(channel_id), post_id, str(pending.get("title") or ""), pending.get("due_at")
            )

    async def import_forwarded_post(
        self, channel_id: int, source_message_id: int, message: Any
    ) -> str:
        """Add an older post that an admin forwarded to the bot in private chat."""
        config = await asyncio.to_thread(self.repository.get, int(channel_id))
        if not config or not config.get("enabled") or not config.get("targets"):
            return "unavailable"
        source_message_id = int(source_message_id)
        entries = list(config.get("entries") or [])
        if any(int(item.get("source_message_id", -1)) == source_message_id for item in entries):
            return "duplicate"
        source_url = self._post_url(channel_id, source_message_id)
        if any(source_url in html.unescape(str(target.get("base_html") or ""))
               for target in config.get("targets") or []):
            return "duplicate"
        pending = list(config.get("pending_posts") or [])
        if any(int(item.get("message_id", -1)) == source_message_id for item in pending):
            return "duplicate"
        title = self._extract_entry(message, config)
        if not title:
            return "no_title"
        excluded_sender_ids = self._excluded_sender_ids(config)
        sender_id = None
        if excluded_sender_ids:
            try:
                sender_id, _ = await self.get_sender_details(channel_id, source_message_id)
            except Exception:
                return "sender_check_failed"
            if sender_id is not None and int(sender_id) in excluded_sender_ids:
                return "excluded"
        targets = list(config.get("targets") or [])
        candidates, category_marker, categorized, ambiguous = self._targets_for_post(
            message, targets
        )
        if ambiguous:
            return "ambiguous"
        if not candidates:
            return "unmatched"
        item = {
            "message_id": source_message_id,
            "title": title,
            "due_at": datetime.now(timezone.utc),
            "category_marker": category_marker,
            "categorized": categorized,
            "sender_id": int(sender_id) if sender_id is not None else None,
        }
        queued = await asyncio.to_thread(
            self.repository.schedule_post, int(channel_id), item
        )
        if not queued:
            return "duplicate"
        await self._publish_after_delay(int(channel_id), source_message_id, title, 0)
        current = await asyncio.to_thread(self.repository.get, int(channel_id)) or {}
        if any(int(entry.get("source_message_id", -1)) == source_message_id
               for entry in current.get("entries") or []):
            return "added"
        return "queued"

    @staticmethod
    def _targets_for_post(
        message: Any, targets: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], str | None, bool, bool]:
        markers_by_key = {}
        for target in targets:
            marker = str(target.get("category_marker") or "").strip()
            if marker:
                markers_by_key.setdefault(marker.casefold(), marker)
        markers = list(markers_by_key.values())
        if not markers:
            return targets, None, False, False
        body = str(
            getattr(message, "message", None)
            or getattr(message, "text", None)
            or getattr(message, "caption", None)
            or ""
        ).casefold()
        matches = [marker for marker in markers if ChannelIndexRuntime._marker_matches(body, marker)]
        if len(matches) > 1:
            return [], None, True, True
        if matches:
            marker = matches[0]
            return [target for target in targets
                    if str(target.get("category_marker") or "").strip().casefold() == marker.casefold()], marker, True, False
        fallback = [target for target in targets if not str(target.get("category_marker") or "").strip()]
        return fallback, None, True, False

    @staticmethod
    def _marker_matches(body: str, marker: str) -> bool:
        marker = marker.strip().casefold()
        if marker.startswith("#"):
            return re.search(rf"(?<!\w){re.escape(marker)}(?!\w)", body) is not None
        return marker in body

    async def _on_new_post(self, event: Any) -> None:
        message = event.message
        if not event.is_channel or not getattr(message, "post", False):
            return
        channel_id = int(event.chat_id)
        from src.utils import logger
        try:
            config = await asyncio.to_thread(self.repository.get, channel_id)
        except Exception as exc:
            logger.warning(
                "[channel-index] could not load config for new post channel_id=%s post_id=%s error=%s",
                channel_id, getattr(message, "id", None), type(exc).__name__,
            )
            return
        if not config or not config.get("enabled") or not config.get("targets"):
            return
        if any(int(target.get("message_id", -1)) == int(message.id)
               for target in config.get("targets") or []):
            return
        sender_id = getattr(message, "sender_id", None)
        logger.info(
            "[channel-index] received channel post channel_id=%s post_id=%s sender_id=%s post_author=%s",
            channel_id, int(message.id), sender_id, getattr(message, "post_author", None),
        )
        try:
            sender_id = int(sender_id) if sender_id is not None else None
        except (TypeError, ValueError):
            logger.warning(
                "[channel-index] post has invalid sender ID channel_id=%s post_id=%s",
                channel_id, int(message.id),
            )
            sender_id = None
        sender_is_bot = sender_id is not None and sender_id == self.bot_user_id
        try:
            is_excluded = sender_id is not None and sender_id in self._excluded_sender_ids(config)
        except (TypeError, ValueError):
            is_excluded = False
        if is_excluded:
            logger.info(
                "[channel-index] skipped excluded sender channel_id=%s post_id=%s sender_id=%s",
                channel_id, int(message.id), sender_id,
            )
            return
        title = self._extract_entry(message, config)
        if not title:
            return
        candidates, category_marker, categorized, ambiguous = self._targets_for_post(
            message, list(config.get("targets") or [])
        )
        if ambiguous or not candidates:
            from src.utils import logger
            logger.info(
                "[channel-index] skipped unmatched or ambiguous post channel_id=%s post_id=%s",
                channel_id, int(message.id),
            )
            return
        post_id = int(message.id)
        due_at = datetime.now(timezone.utc) + timedelta(
            minutes=max(1, min(int(config.get("delay_minutes") or 5), 10))
        )
        pending = {
            "message_id": post_id, "title": title, "due_at": due_at,
            "category_marker": category_marker, "categorized": categorized,
            "sender_id": sender_id,
            "sender_is_bot": sender_is_bot,
        }
        try:
            queued = await asyncio.to_thread(self.repository.schedule_post, channel_id, pending)
        except Exception as exc:
            logger.warning(
                "[channel-index] could not queue post channel_id=%s post_id=%s error=%s",
                channel_id, post_id, type(exc).__name__,
            )
            return
        if not queued:
            return
        self._schedule(channel_id, post_id, title, due_at)
        from src.utils import logger
        logger.info(
            "[channel-index] queued channel_id=%s post_id=%s delay_minutes=%s",
            channel_id, post_id, int(config.get("delay_minutes") or 5),
        )

    def _schedule(self, channel_id: int, post_id: int, title: str, due_at: Any) -> None:
        key = (channel_id, post_id)
        previous = self._pending.pop(key, None)
        if previous:
            previous.cancel()
        if isinstance(due_at, datetime):
            if due_at.tzinfo is None:
                due_at = due_at.replace(tzinfo=timezone.utc)
            seconds = max(0, (due_at - datetime.now(timezone.utc)).total_seconds())
        else:
            seconds = 0
        self._pending[key] = asyncio.create_task(
            self._publish_after_delay(channel_id, post_id, title, seconds)
        )

    @staticmethod
    def _extract_entry(message: Any, config: dict[str, Any]) -> str:
        body = str(
            getattr(message, "message", None)
            or getattr(message, "text", None)
            or getattr(message, "caption", None)
            or ""
        )
        mode = str(config.get("entry_mode") or ("prefix" if config.get("title_mode") == "prefix" else "text"))
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        if mode == "hashtags":
            tag = re.search(r"(?<!\w)#[\w\u0080-\uffff]+", body)
            return ChannelIndexRuntime._short_title(tag.group(0)) if tag else ""
        if mode == "links":
            links = re.findall(r"(?:https?://|www\.|t\.me/)[^\s<>]+", body, re.IGNORECASE)
            message_entities = (getattr(message, "entities", None)
                                or getattr(message, "caption_entities", None) or [])
            for entity in message_entities:
                hidden_url = getattr(entity, "url", None)
                if hidden_url:
                    links.append(str(hidden_url))
            if not links:
                return ""
            link = links[0].rstrip(".,;:!?)]}")
            parts = urlsplit(link if "://" in link else f"https://{link}")
            label = (parts.netloc + parts.path).strip("/") or link
            return ChannelIndexRuntime._short_title(label)
        if mode == "image":
            document = getattr(message, "document", None)
            is_image = bool(getattr(message, "photo", None)) or str(
                getattr(document, "mime_type", "") or ""
            ).startswith("image/")
            if not is_image:
                return ""
            caption = lines[0] if lines else "Image"
            return ChannelIndexRuntime._short_title(f"🖼 {caption}")
        if mode == "file":
            document = getattr(message, "document", None)
            if document is None:
                return ""
            file_name = next((str(getattr(attr, "file_name", "")).strip()
                              for attr in getattr(document, "attributes", [])
                              if getattr(attr, "file_name", None)), "")
            file_name = file_name or str(getattr(document, "file_name", "") or "").strip()
            caption = lines[0] if lines else "File attachment"
            return ChannelIndexRuntime._short_title(f"📎 {file_name or caption}")
        if mode == "prefix":
            prefix = str(config.get("entry_prefix") or config.get("title_prefix") or "").strip()
            if not prefix:
                return ""
            for line in lines:
                if line.casefold().startswith(prefix.casefold()):
                    value = line[len(prefix):].strip(" \t:：-–—")
                    if value:
                        return ChannelIndexRuntime._short_title(value)
            return ""
        return ChannelIndexRuntime._short_title(lines[0]) if lines else ""

    @staticmethod
    def _short_title(value: str) -> str:
        value = re.sub(r"\s+", " ", value).strip()
        if len(value) <= 180:
            return value
        prefix = value[:177]
        if " " in prefix:
            prefix = prefix.rsplit(" ", 1)[0]
        return prefix.rstrip(".,;:—- ") + "…"

    @staticmethod
    def _post_url(channel_id: int, message_id: int) -> str:
        internal_id = str(abs(int(channel_id)))
        if internal_id.startswith("100"):
            internal_id = internal_id[3:]
        return f"https://t.me/c/{internal_id}/{int(message_id)}"

    @staticmethod
    def _render_target(
        base_html: str, entries: list[dict[str, Any]], sort_order: str = "added",
        bullet: str = "🔹",
    ) -> str:
        if sort_order == "alphabetical":
            entries = sorted(entries, key=lambda entry: str(entry.get("title") or "").casefold())
        additions = [
            f'<a href="{html.escape(str(entry["url"]), quote=True)}">'
            f'{html.escape(bullet)} {html.escape(str(entry["title"]))}</a>'
            for entry in entries
        ]
        heading = base_html.rstrip()
        if not additions:
            return heading or " "
        # A blank line after a heading gives the first generated entry room.
        # If the placeholder already contains a separated list/body, append to
        # that content directly instead of making the new entry look like a
        # separate section.
        has_existing_body = bool(re.search(r"\n[ \t]*\n(?=\S)", heading))
        separator = ("\n" if has_existing_body else "\n\n") if heading else ""
        formatted_additions = "\n".join(additions)
        return f"{heading}{separator}{formatted_additions}"

    async def _publish_after_delay(self, channel_id: int, post_id: int, title: str, delay_seconds: float) -> None:
        key = (channel_id, post_id)
        try:
            await asyncio.sleep(max(0, delay_seconds))
            async with self._lock:
                config = await asyncio.to_thread(self.repository.get, channel_id)
                if not config or not config.get("enabled"):
                    await asyncio.to_thread(self.repository.remove_pending, channel_id, post_id)
                    return
                if not any(int(item.get("message_id", -1)) == post_id
                           for item in config.get("pending_posts") or []):
                    return
                targets = list(config.get("targets") or [])
                pending_item = next(
                    item for item in config.get("pending_posts") or []
                    if int(item.get("message_id", -1)) == post_id
                )
                sender_id = pending_item.get("sender_id")
                sender_is_bot = bool(pending_item.get("sender_is_bot")) or (
                    sender_id is not None and int(sender_id) == self.bot_user_id
                )
                if sender_is_bot:
                    is_managed_main_post = await asyncio.to_thread(
                        self.repository.is_channel_manager_main_post,
                        channel_id, post_id,
                    )
                    if not is_managed_main_post:
                        await asyncio.to_thread(self.repository.remove_pending, channel_id, post_id)
                        from src.utils import logger
                        logger.info(
                            "[channel-index] skipped Annie post that is not a saved Channel Manager main post channel_id=%s post_id=%s",
                            channel_id, post_id,
                        )
                        return
                excluded_sender_ids = self._excluded_sender_ids(config)
                if sender_id is None and excluded_sender_ids:
                    try:
                        source = await self.client.get_messages(channel_id, ids=post_id)
                        if isinstance(source, (list, tuple)):
                            source = source[0] if source else None
                        sender_id = getattr(source, "sender_id", None)
                    except Exception as exc:
                        from src.utils import logger
                        logger.warning(
                            "[channel-index] could not check excluded sender channel_id=%s post_id=%s error=%s",
                            channel_id, post_id, type(exc).__name__,
                        )
                        await asyncio.to_thread(self.repository.remove_pending, channel_id, post_id)
                        return
                try:
                    sender_is_excluded = sender_id is not None and int(sender_id) in excluded_sender_ids
                except (TypeError, ValueError):
                    sender_is_excluded = False
                if sender_is_excluded:
                    await asyncio.to_thread(self.repository.remove_pending, channel_id, post_id)
                    from src.utils import logger
                    logger.info(
                        "[channel-index] skipped excluded sender channel_id=%s post_id=%s sender_id=%s",
                        channel_id, post_id, sender_id,
                    )
                    return
                if pending_item.get("categorized"):
                    marker = str(pending_item.get("category_marker") or "").strip()
                    targets = [
                        target for target in targets
                        if str(target.get("category_marker") or "").strip().casefold()
                        == marker.casefold()
                    ]
                entries = list(config.get("entries") or [])
                if any(int(entry.get("source_message_id", -1)) == post_id for entry in entries):
                    await asyncio.to_thread(self.repository.remove_pending, channel_id, post_id)
                    return
                url = self._post_url(channel_id, post_id)
                selected = None
                selected_text = None
                for target in targets:
                    target_entries = [entry for entry in entries
                                      if int(entry.get("target_message_id", -1)) == int(target["message_id"])]
                    candidate = {
                        "source_message_id": post_id,
                        "target_message_id": int(target["message_id"]),
                        "title": title,
                        "url": url,
                    }
                    rendered = self._render_target(
                        str(target.get("base_html") or ""), target_entries + [candidate],
                        str(config.get("sort_order") or "added"),
                        str(target.get("entry_bullet") or config.get("entry_bullet") or "🔹"),
                    )
                    if len(rendered.encode("utf-16-le")) // 2 <= self.MAX_INDEX_TEXT:
                        selected, selected_text = candidate, rendered
                        break
                if selected is None:
                    # Wait for an admin-placed placeholder; Telegram cannot insert
                    # a new post between older channel posts.
                    from src.utils import logger
                    logger.warning(
                        "[channel-index] all registered index posts are full; waiting for a placeholder channel_id=%s post_id=%s",
                        channel_id, post_id,
                    )
                    return
                entries.append(selected)
                await asyncio.to_thread(
                    self.repository.update, channel_id,
                    {"entries": entries},
                )
                try:
                    await self.bot.edit_message_text(
                        chat_id=channel_id,
                        message_id=int(selected["target_message_id"]),
                        text=selected_text,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception:
                    entries.pop()
                    await asyncio.to_thread(
                        self.repository.update, channel_id, {"entries": entries}
                    )
                    raise
                await asyncio.to_thread(self.repository.remove_pending, channel_id, post_id)
                self._retry_counts.pop(key, None)
                from src.utils import logger
                logger.info(
                    "[channel-index] updated channel_id=%s source_post_id=%s index_post_id=%s",
                    channel_id, post_id, selected["target_message_id"],
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            from src.utils import logger
            logger.warning(
                "[channel-index] publish failed channel_id=%s post_id=%s error=%s",
                channel_id, post_id, type(exc).__name__,
            )
            attempts = self._retry_counts.get(key, 0) + 1
            self._retry_counts[key] = attempts
            if attempts <= 3:
                self._pending[key] = asyncio.create_task(
                    self._retry_publish(channel_id, post_id, title)
                )
        finally:
            if self._pending.get(key) is asyncio.current_task():
                self._pending.pop(key, None)

    async def _on_edited_post(self, event: Any) -> None:
        message = getattr(event, "message", None)
        if not getattr(event, "is_channel", False) or not getattr(message, "post", False):
            return
        channel_id = int(event.chat_id)
        post_id = int(message.id)
        key = (channel_id, post_id)
        from src.utils import logger
        try:
            async with self._lock:
                config = await asyncio.to_thread(self.repository.get, channel_id)
                if not config:
                    return
                if any(int(target.get("message_id", -1)) == post_id
                       for target in config.get("targets") or []):
                    target_urls = self._message_text_urls(message)
                    entries = list(config.get("entries") or [])
                    kept_entries = [
                        entry for entry in entries
                        if int(entry.get("target_message_id", -1)) != post_id
                        or str(entry.get("url") or "") in target_urls
                    ]
                    removed_count = len(entries) - len(kept_entries)
                    if removed_count:
                        await asyncio.to_thread(
                            self.repository.update, channel_id, {"entries": kept_entries}
                        )
                        logger.info(
                            "[channel-index] removed manually deleted index links channel_id=%s index_post_id=%s entries=%s",
                            channel_id, post_id, removed_count,
                        )
                    return
                title = self._extract_entry(message, config)
                candidates, category_marker, categorized, ambiguous = self._targets_for_post(
                    message, list(config.get("targets") or [])
                )
                target_for_post = candidates[0] if candidates and not ambiguous else None
                pending = next(
                    (item for item in config.get("pending_posts") or []
                     if int(item.get("message_id", -1)) == post_id),
                    None,
                )
                if pending is not None:
                    if not title or target_for_post is None:
                        task = self._pending.pop(key, None)
                        if task:
                            task.cancel()
                        await asyncio.to_thread(self.repository.remove_pending, channel_id, post_id)
                        reason = "no_label" if not title else "no_matching_list"
                        logger.info("[channel-index] removed pending entry after edit channel_id=%s post_id=%s reason=%s", channel_id, post_id, reason)
                        return
                    changed = await asyncio.to_thread(
                        self.repository.update_pending_post, channel_id, post_id, title,
                        category_marker, categorized,
                    )
                    if changed:
                        self._schedule(channel_id, post_id, title, pending.get("due_at"))
                        logger.info("[channel-index] refreshed pending entry after edit channel_id=%s post_id=%s", channel_id, post_id)
                    return

                entries = [dict(entry) for entry in config.get("entries") or []]
                entry_index = next(
                    (index for index, entry in enumerate(entries)
                     if int(entry.get("source_message_id", -1)) == post_id),
                    None,
                )
                if entry_index is None:
                    return
                old_entries = [dict(entry) for entry in entries]
                old_entry = entries[entry_index]
                target_id = int(old_entry.get("target_message_id", -1))
                old_target_id = target_id
                old_title = str(old_entry.get("title") or "")
                if title and candidates and not ambiguous:
                    target_for_post = next(
                        (item for item in candidates
                         if int(item.get("message_id", -1)) == old_target_id),
                        candidates[0],
                    )
                if not title or target_for_post is None:
                    entries.pop(entry_index)
                    title = ""
                else:
                    old_entry["title"] = title
                    old_entry["target_message_id"] = int(target_for_post["message_id"])
                    old_entry["category_marker"] = category_marker
                    old_entry["categorized"] = categorized
                target_id = int(target_for_post["message_id"]) if target_for_post else old_target_id
                if title and title == old_title and target_id == old_target_id:
                    return
                affected_targets = {old_target_id, target_id}
                target_map = {
                    int(item.get("message_id", -1)): item
                    for item in config.get("targets") or []
                    if int(item.get("message_id", -1)) in affected_targets
                }
                if len(target_map) != len(affected_targets):
                    logger.warning("[channel-index] edited source references missing placeholder channel_id=%s post_id=%s", channel_id, post_id)
                    return
                rendered_targets = {}
                for affected_id, affected_target in target_map.items():
                    target_entries = [entry for entry in entries
                                      if int(entry.get("target_message_id", -1)) == affected_id]
                    rendered = self._render_target(
                        str(affected_target.get("base_html") or ""), target_entries,
                        str(config.get("sort_order") or "added"),
                        str(affected_target.get("entry_bullet") or config.get("entry_bullet") or "🔹"),
                    )
                    if len(rendered.encode("utf-16-le")) // 2 > self.MAX_INDEX_TEXT:
                        logger.warning("[channel-index] edited entry exceeds Telegram limit channel_id=%s post_id=%s index_post_id=%s", channel_id, post_id, affected_id)
                        return
                    rendered_targets[affected_id] = rendered
                await asyncio.to_thread(self.repository.update, channel_id, {"entries": entries})
                try:
                    for affected_id, rendered in rendered_targets.items():
                        try:
                            await self.bot.edit_message_text(
                                chat_id=channel_id, message_id=affected_id,
                                text=rendered, parse_mode="HTML", disable_web_page_preview=True,
                            )
                        except BadRequest as exc:
                            if "message is not modified" not in str(exc).casefold():
                                raise
                except BadRequest as exc:
                    if "message is not modified" not in str(exc).casefold():
                        await asyncio.to_thread(self.repository.update, channel_id, {"entries": old_entries})
                        raise
                except Exception:
                    await asyncio.to_thread(self.repository.update, channel_id, {"entries": old_entries})
                    raise
                action = "updated" if title else "removed"
                logger.info("[channel-index] %s entry after source edit channel_id=%s source_post_id=%s index_post_id=%s", action, channel_id, post_id, target_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[channel-index] edit sync failed channel_id=%s post_id=%s error=%s", channel_id, post_id, type(exc).__name__)

    async def _on_deleted_posts(self, event: Any) -> None:
        channel_id = getattr(event, "chat_id", None)
        if channel_id is None:
            return
        from src.utils import logger
        try:
            channel_id = int(channel_id)
            deleted_ids = {int(value) for value in (getattr(event, "deleted_ids", ()) or ())}
        except (TypeError, ValueError):
            logger.warning("[channel-index] deletion update had invalid IDs")
            return
        if not deleted_ids:
            return
        for post_id in deleted_ids:
            key = (channel_id, post_id)
            task = self._pending.pop(key, None)
            if task:
                task.cancel()
            try:
                await asyncio.to_thread(self.repository.remove_pending, channel_id, post_id)
            except Exception as exc:
                logger.warning(
                    "[channel-index] pending deletion cleanup failed channel_id=%s post_id=%s error=%s",
                    channel_id, post_id, type(exc).__name__,
                )
            self._retry_counts.pop(key, None)
        try:
            async with self._lock:
                config = await asyncio.to_thread(self.repository.get, channel_id)
                if not config:
                    return
                entries = list(config.get("entries") or [])
                def source_was_deleted(entry: dict[str, Any]) -> bool:
                    try:
                        return int(entry.get("source_message_id", -1)) in deleted_ids
                    except (TypeError, ValueError):
                        return False

                removed = [entry for entry in entries if source_was_deleted(entry)]
                if not removed:
                    return
                entries = [entry for entry in entries if not source_was_deleted(entry)]
                targets = list(config.get("targets") or [])
                await asyncio.to_thread(self.repository.update, channel_id, {"entries": entries})
                for target in targets:
                    try:
                        target_id = int(target["message_id"])
                    except (KeyError, TypeError, ValueError):
                        logger.warning(
                            "[channel-index] ignored placeholder with invalid message ID channel_id=%s",
                            channel_id,
                        )
                        continue
                    def entry_targets_message(entry: dict[str, Any]) -> bool:
                        try:
                            return int(entry.get("target_message_id", -1)) == target_id
                        except (TypeError, ValueError):
                            return False

                    if not any(entry_targets_message(entry) for entry in removed):
                        continue
                    remaining = [entry for entry in entries if entry_targets_message(entry)]
                    rendered = self._render_target(
                        str(target.get("base_html") or ""), remaining,
                        str(config.get("sort_order") or "added"),
                        str(target.get("entry_bullet") or config.get("entry_bullet") or "🔹"),
                    )
                    try:
                        await self.bot.edit_message_text(
                            chat_id=channel_id, message_id=target_id,
                            text=rendered, parse_mode="HTML", disable_web_page_preview=True,
                        )
                    except Exception as exc:
                        if "message is not modified" in str(exc).casefold():
                            continue
                        logger.warning(
                            "[channel-index] deleted source entry saved but index message refresh failed channel_id=%s source_post_ids=%s index_post_id=%s error=%s",
                            channel_id, sorted(deleted_ids), target_id, type(exc).__name__,
                        )
                        continue
                logger.info(
                    "[channel-index] removed deleted source entries channel_id=%s source_post_ids=%s entries=%s",
                    channel_id, sorted(deleted_ids), len(removed),
                )
        except Exception as exc:
            logger.warning(
                "[channel-index] deletion cleanup failed channel_id=%s error=%s",
                channel_id, type(exc).__name__,
            )

    async def _retry_publish(self, channel_id: int, post_id: int, title: str) -> None:
        await asyncio.sleep(30)
        await self._publish_after_delay(channel_id, post_id, title, 0)
